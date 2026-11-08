#!/usr/bin/env python3
"""The Kolibri lane serves on vLLM behind this proxy (v6.37), and vLLM is not SGLang where the
proxy had assumed it was. Each test here is one difference, measured on the reference box
against vLLM 0.29.0 serving Kolibri-1 (2026-10-14), and the fake engine below answers the way
that engine answered:
  - no /server_info and no /get_server_info (404): its pool is the kv_cache_size_tokens label
    of the cache_config_info gauge on /metrics, its window the max_model_len of /v1/models;
    without them every body past 256 KiB was held as "not measured yet" for good;
  - /v1/models says "owned_by": "vllm";
  - a model it does not serve is a 404 ("The model `x` does not exist."), where SGLang answers
    any name, so a client set up for one lane broke on this one;
  - a request whose connection closes is stopped (one running before the client left, none
    0.5 s after, no token in the next 2 s, stream and plain answer alike), and there is no
    /abort_request: the proxy drained such a request up to DRAIN_MAX_S;
  - logit_bias past the vocabulary is refused with "Vocabulary size: 128000", and only for the
    served model name (the probe named "probe", which vLLM refuses first).
And on SGLang nothing changes: a name it does not serve goes as it came."""
import http.server
import importlib.util
import io
import json
import os
import select
import socket
import sys
import threading
import tempfile
import time
import unittest
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localports  # noqa: E402

HERE = Path(__file__).resolve()
KEY = "test-key-vllm-lane"
METRICS = (
    '# HELP vllm:cache_config_info Information of the LLMEngine CacheConfig\n'
    '# TYPE vllm:cache_config_info gauge\n'
    'vllm:cache_config_info{_block_size_resolved="True",block_size="16",cache_dtype="fp8",'
    'enable_prefix_caching="True",engine="0",gpu_memory_utilization="0.55",'
    'kv_cache_size_tokens="2014877",num_gpu_blocks="134815"} 1.0\n'
    'vllm:num_requests_running{engine="0",model_name="kolibri-1"} 0.0\n')


def _keep_env(cls):
    saved = dict(os.environ)
    cls.addClassCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))


class FakeVllm(http.server.BaseHTTPRequestHandler):
    """vLLM 0.29.0 serving kolibri-1, as far as this proxy meets it."""
    protocol_version = "HTTP/1.1"
    served = "kolibri-1"
    owner = "vllm"
    metrics_fail = False   # /metrics answering 500, as a read of the pool that fails
    info_hits = []         # SGLang's info routes asked of it (it has none: 404)
    bodies = []            # every chat body it received, parsed
    closed_at = {}         # prompt -> when the proxy closed a stream it was writing
    aborts = []            # /abort_request calls (vLLM has none: 404)
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def refused(self, path):
        """vLLM's key check (and the Kolibri lane's middleware): the value as it comes, compared
        exactly; /health and /metrics open."""
        if path in ("/health", "/metrics"):
            return False
        scheme, _, param = (self.headers.get("Authorization") or "").partition(" ")
        if scheme.lower() == "bearer" and param == KEY:
            return False
        self._json(401, {"error": "Unauthorized"})
        return True

    def do_GET(self):
        path = self.path.split("?")[0]
        if self.refused(path):
            return
        if path == "/metrics" and FakeVllm.metrics_fail:
            self._json(500, {"detail": "Internal Server Error"})
        elif path == "/metrics":
            # SGLang's page has none of vLLM's gauges
            out = (METRICS if FakeVllm.owner == "vllm" else "sglang:num_running_reqs 0.0\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        elif path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": FakeVllm.served, "object": "model",
                                                        "owned_by": FakeVllm.owner, "max_model_len": 262144}]})
        elif path in ("/server_info", "/get_server_info") and FakeVllm.owner == "vllm":
            with FakeVllm.lock:
                FakeVllm.info_hits.append(path)
            self._json(404, {"detail": "Not Found"})
        elif path == "/server_info" and FakeVllm.owner == "sglang":
            self._json(200, {"max_total_num_tokens": 863644, "max_req_input_len": 262138})
        else:
            self._json(404, {"detail": "Not Found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.refused(path):
            return
        if path == "/abort_request":
            with FakeVllm.lock:
                FakeVllm.aborts.append(raw)
            self._json(404, {"detail": "Not Found"})
            return
        body = json.loads(raw)
        with FakeVllm.lock:
            FakeVllm.bodies.append(body)
        if FakeVllm.owner == "vllm" and body.get("model") != FakeVllm.served:
            self._json(404, {"error": {"message": f"The model `{body.get('model')}` does not exist.",
                                       "type": "NotFoundError", "param": "model", "code": 404}})
            return
        if body.get("logit_bias"):
            self._json(400, {"error": {"message": "token_id(s) [999999999] in logit_bias contain out-of-vocab "
                                                  "token ids. Vocabulary size: 128000 (parameter=logit_bias, "
                                                  "value=[999999999])", "type": "BadRequestError", "code": 400}})
            return
        prompt = body["messages"][0]["content"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        if prompt.startswith("prefill"):
            # vLLM sends a stream's headers before its prefill, then nothing until the first
            # token: here 5 s, watching for the end of the connection as vLLM does
            end = time.time() + 5
            while time.time() < end:
                r, _, _ = select.select([self.connection], [], [], 0.05)
                if r and not self.connection.recv(1, socket.MSG_PEEK):
                    with FakeVllm.lock:
                        FakeVllm.closed_at[prompt] = time.time()
                    return
        n = 200 if prompt.startswith("slow") else 2
        try:
            for i in range(n):
                ev = {"id": "chatcmpl-0123456789abcdef", "object": "chat.completion.chunk",
                      "model": FakeVllm.served, "choices": [{"index": 0, "delta": {"content": f"t{i} "}}]}
                self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                self.wfile.flush()
                time.sleep(0.05 if n > 2 else 0)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            with FakeVllm.lock:
                FakeVllm.closed_at[prompt] = time.time()


def load_proxy(name):
    spec = importlib.util.spec_from_file_location(name, HERE.parents[1] / "keepalive-proxy.py")
    mod = importlib.util.module_from_spec(spec)
    sys.argv = ["keepalive-proxy.py"]
    spec.loader.exec_module(mod)
    return mod


class PureParts(unittest.TestCase):
    """What needs no engine: reading /metrics, renaming a body, forgetting an engine."""

    @classmethod
    def setUpClass(cls):
        _keep_env(cls)
        os.environ["UPSTREAM"] = "http://127.0.0.1:9"
        cls.mod = load_proxy("kproxy_vllm_pure")

    def setUp(self):
        self.mod._SERVED.update(names=(), engine="", ts=0.0)
        self.mod._ENGINE_SEEN.update(names=(), engine="")

    def test_the_pool_is_the_kv_cache_size_label(self):
        self.assertEqual(self.mod.vllm_pool_facts(METRICS, [{"id": "kolibri-1", "max_model_len": 262144}]),
                         (2014877, 262144))

    def test_without_the_label_blocks_times_block_size(self):
        text = METRICS.replace('kv_cache_size_tokens="2014877",', "")
        self.assertEqual(self.mod.vllm_pool_facts(text, [])[0], 134815 * 16)

    def test_an_engine_with_neither_stays_unmeasured(self):
        self.assertEqual(self.mod.vllm_pool_facts("sglang:num_running_reqs 0\n", None), (0, 0))

    def body(self, model="qwen3.8-27b"):
        return json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}]}).encode()

    def test_vllm_gets_the_served_name(self):
        self.mod._SERVED.update(names=("kolibri-1",), engine="vllm", ts=time.time())
        out, was = self.mod.route_model_name(self.body(), "/v1/chat/completions")
        self.assertEqual(was, "qwen3.8-27b")
        self.assertEqual(json.loads(out)["model"], "kolibri-1")
        for path in ("/v1/messages", "/v1/messages/count_tokens", "/v1/completions", "/tokenize", "/invocations"):
            self.assertEqual(json.loads(self.mod.route_model_name(self.body(), path)[0])["model"], "kolibri-1", path)

    def test_a_served_name_a_foreign_route_or_a_doubt_go_as_they_came(self):
        self.mod._SERVED.update(names=("kolibri-1",), engine="vllm", ts=time.time())
        for body, path in ((self.body("kolibri-1"), "/v1/chat/completions"),
                           (self.body(), "/v1/systemone"),
                           (b'{"model": "x", "messages": [', "/v1/chat/completions"),
                           (b'[{"model": "x"}]', "/v1/chat/completions"),
                           (json.dumps({"model": 7}).encode(), "/v1/chat/completions")):
            with self.subTest(path=path, body=body[:30]):
                self.assertEqual(self.mod.route_model_name(body, path), (body, None))

    def test_sglang_bodies_are_never_touched(self):
        self.mod._SERVED.update(names=("qwen3.8-flash-next",), engine="sglang", ts=time.time())
        body = self.body()
        self.assertEqual(self.mod.route_model_name(body, "/v1/chat/completions"), (body, None))

    def test_another_engine_drops_the_vocabulary_and_the_rid_rule(self):
        m = self.mod
        m.note_engine(("qwen3.8-27b",), "sglang")
        m._VOCAB.update(size=248320, ts=time.time())
        m._rid_override_honoured = True
        m.note_engine(("qwen3.8-27b",), "sglang")          # the same engine, restarted
        self.assertEqual((m._VOCAB["size"], m._rid_override_honoured), (248320, True))
        with redirect_stderr(io.StringIO()):
            m.note_engine(("kolibri-1",), "vllm")              # a lane switch
        self.assertEqual((m._VOCAB["size"], m._rid_override_honoured), (0, None))

    def test_system_one_names_the_lanes_it_serves_on(self):
        # on every vLLM lane, the Qwen one included: "needs a Qwen lane" read as a contradiction
        # while the Qwen3.8 Flash-Next FP4-Local lane, a Qwen lane, served (found in review, 2026-10-19)
        for served in ("kolibri-1", "qwen3.8-flash-next-fp4"):
            with self.subTest(served=served):
                self.mod._SERVED.update(names=(served,), engine="vllm", ts=time.time())
                self.mod.served_models = lambda s=served: (s,)
                with self.assertRaises(self.mod.SystemOneUsage) as e:
                    self.mod.systemone_model("jev-latest")
                self.assertIn("serves on the 27B and the flash lanes (SGLang)", str(e.exception))
                self.assertIn(served, str(e.exception))
                self.assertNotIn("needs a Qwen lane", str(e.exception))


class ThroughTheProxy(unittest.TestCase):
    """A fake vLLM behind the real proxy."""

    @classmethod
    def setUpClass(cls):
        _keep_env(cls)
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeVllm)
        cls.engine.daemon_threads = True
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        os.environ.update(UPSTREAM=f"http://127.0.0.1:{cls.engine.server_port}", KEEPALIVE_S="0.4",
                          CLIENT_IO_S="5", PROMPT_CEILING_TOKENS="0", FLASH_PROMPT_CEILING_TOKENS="0")
        cls.mod = load_proxy("kproxy_vllm_lane")
        cls.mod._api_key = lambda: KEY
        # this proxy's own lines only: stderr is the process's, and a drain left running by
        # another module's proxy wrote its line into this one's (the full unittest run, 2026-10-14)
        cls.lines = []
        cls.mod.log = lambda msg: cls.lines.append(f"[proxy] {msg}")
        cls.log = type("Lines", (), {"getvalue": staticmethod(lambda: "\n".join(cls.lines) + "\n")})()
        cls.err = redirect_stderr(io.StringIO())
        cls.err.__enter__()
        cls.proxy = cls.mod.Server(("127.0.0.1", 0), cls.mod.H)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.port = cls.proxy.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.err.__exit__(None, None, None)
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.engine.shutdown()
        cls.engine.server_close()

    def setUp(self):
        FakeVllm.served, FakeVllm.owner, FakeVllm.metrics_fail = "kolibri-1", "vllm", False
        with FakeVllm.lock:
            FakeVllm.bodies.clear(); FakeVllm.aborts.clear(); FakeVllm.closed_at.clear()
        self.mod.invalidate_pool()

    def post(self, body, read_all=True, auth=None):
        raw = json.dumps(body).encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=20)
        s.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: p\r\nAuthorization: {auth or 'Bearer ' + KEY}\r\n"
                  f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n".encode() + raw)
        if not read_all:
            return s
        s.settimeout(20)
        got = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            got += c
        s.close()
        return got

    def test_the_pool_and_the_window_come_from_metrics_and_models(self):
        self.assertEqual(self.mod.pool_tokens(), 2014877)
        self.assertEqual(self.mod._POOL["window"], 262144)
        self.assertEqual((self.mod._SERVED["names"], self.mod._SERVED["engine"]), (("kolibri-1",), "vllm"))

    def test_a_body_past_256_kib_is_served_not_held(self):
        got = self.post({"model": "kolibri-1", "stream": True,
                         "messages": [{"role": "user", "content": "x " * 150000}]})
        self.assertTrue(got.startswith(b"HTTP/1.1 200"), got[:120])
        self.assertIn(b"[DONE]", got)

    def test_a_client_set_up_for_another_lane_reaches_kolibri(self):
        got = self.post({"model": "qwen3.8-27b", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        self.assertTrue(got.startswith(b"HTTP/1.1 200"), got[:200])
        self.assertEqual(FakeVllm.bodies[-1]["model"], "kolibri-1")
        self.assertIn("model 'qwen3.8-27b' is not what this lane serves", self.log.getvalue())

    def test_a_client_that_leaves_ends_the_generation_at_once_and_is_never_drained(self):
        s = self.post({"model": "kolibri-1", "stream": True,
                       "messages": [{"role": "user", "content": "slow essay"}]}, read_all=False)
        s.settimeout(10)
        s.recv(4096)                     # the stream is on
        t0 = time.time()
        s.close()
        end = time.time() + 5
        while time.time() < end and "slow essay" not in FakeVllm.closed_at:
            time.sleep(0.05)
        self.assertIn("slow essay", FakeVllm.closed_at, "the engine never saw its connection close")
        self.assertLess(FakeVllm.closed_at["slow essay"] - t0, 2.0)
        self.assertEqual(FakeVllm.aborts, [], "vLLM has no /abort_request")
        self.assertIn("closing the upstream: vLLM stops what it generates", self.log.getvalue())
        self.assertNotIn("drained an abandoned", self.log.getvalue())
        # and no SGLang advice in a vLLM lane's journal (it did, on the reference box)
        self.assertNotIn("SGLANG_ENABLE_REQUEST_HEADER_OVERRIDES", self.log.getvalue())

    def test_a_client_that_leaves_during_the_prefill_is_seen_by_the_engine_at_once(self):
        # close() waited for the stream's next byte, the first token after a long prefill
        # (48.5 s at 128K): the engine computed it for nobody (found in review, 2026-10-14)
        s = self.post({"model": "kolibri-1", "stream": True,
                       "messages": [{"role": "user", "content": "prefill long"}]}, read_all=False)
        s.settimeout(10)
        s.recv(4096)                     # the headers, then the proxy's keepalives
        t0 = time.time()
        s.close()
        end = time.time() + 8
        while time.time() < end and "prefill long" not in FakeVllm.closed_at:
            time.sleep(0.05)
        self.assertIn("prefill long", FakeVllm.closed_at, "the engine never saw its connection end")
        self.assertLess(FakeVllm.closed_at["prefill long"] - t0, 2.0)
        self.assertEqual(FakeVllm.aborts, [])

    def test_the_engine_a_request_went_to_decides_how_it_ends(self):
        # the lane's facts dropped while the stream ran (a pool read that failed): an abandoned
        # Kolibri stream took SGLang's way, an /abort_request answered 404, and a drain
        since = len(self.lines)
        s = self.post({"model": "kolibri-1", "stream": True,
                       "messages": [{"role": "user", "content": "slow and dropped"}]}, read_all=False)
        s.settimeout(10)
        s.recv(4096)
        self.mod.invalidate_pool()
        t0 = time.time()
        s.close()
        end = time.time() + 5
        while time.time() < end and "slow and dropped" not in FakeVllm.closed_at:
            time.sleep(0.05)
        self.assertLess(FakeVllm.closed_at.get("slow and dropped", 1e12) - t0, 2.0)
        self.assertEqual(FakeVllm.aborts, [], "vLLM has no /abort_request")
        time.sleep(0.3)
        mine = "\n".join(self.lines[since:])
        self.assertNotIn("drained an abandoned", mine)
        self.assertNotIn("SGLANG_ENABLE_REQUEST_HEADER_OVERRIDES", mine, "SGLang's advice on a vLLM request")

    def test_the_vocabulary_is_read_in_vllms_words_with_the_served_name(self):
        self.mod.pool_tokens()                       # names known, as on any prompt
        self.assertEqual(self.mod.served_vocab(), 128000)
        self.assertTrue(any(b.get("logit_bias") and b["model"] == "kolibri-1" for b in FakeVllm.bodies))

    def test_names_lost_to_one_failed_read_are_read_again_for_the_rename(self):
        # the pool cached, the names dropped by one failed read of /v1/models (served_models
        # empties them, the pool stays): the rename read the cached pool, which names nothing,
        # and vLLM answered 404 to every other name until the pool expired (found in review,
        # 2026-10-14)
        self.mod.pool_tokens()
        self.mod._SERVED.update(names=(), engine="", ts=0.0)
        got = self.post({"model": "qwen3.8-27b", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        self.assertTrue(got.startswith(b"HTTP/1.1 200"), got[:200])
        self.assertEqual(FakeVllm.bodies[-1]["model"], "kolibri-1")

    def test_the_vocabulary_probe_names_the_served_model_with_no_name_read_yet(self):
        # right after a switch, or a first request with logit_bias: no name read yet, the probe
        # named "probe", vLLM refused it with a 404 before it looked at logit_bias, and the guard
        # went 60 s without a vocabulary (found in review, 2026-10-14)
        self.mod._VOCAB.update(size=0, ts=0.0)
        self.assertEqual(self.mod._SERVED["names"], ())
        self.assertEqual(self.mod.served_vocab(), 128000)

    def test_a_failed_pool_read_on_the_way_does_not_send_a_kolibri_stream_sglangs_way(self):
        # the cached facts expire, and the next prompt's read of them fails (/metrics, here):
        # the facts dropped on the way, the request went out with no engine noted, and the
        # client that left was handled as on SGLang: an /abort_request vLLM answers 404, then a
        # drain while vLLM kept generating (found in review, 2026-10-14)
        self.mod.pool_tokens()
        self.mod._POOL["ts"] = 0.0
        FakeVllm.metrics_fail = True
        since = len(self.lines)
        s = self.post({"model": "kolibri-1", "stream": True,
                       "messages": [{"role": "user", "content": "slow after a failed read"}]}, read_all=False)
        s.settimeout(10)
        s.recv(4096)
        t0 = time.time()
        s.close()
        end = time.time() + 5
        while time.time() < end and "slow after a failed read" not in FakeVllm.closed_at:
            time.sleep(0.05)
        self.assertLess(FakeVllm.closed_at.get("slow after a failed read", 1e12) - t0, 2.0,
                        "vLLM did not see its client leave")
        self.assertEqual(FakeVllm.aborts, [], "vLLM has no /abort_request")
        time.sleep(0.3)
        self.assertNotIn("drained an abandoned", "\n".join(self.lines[since:]))

    def test_a_new_engine_that_names_itself_drops_what_was_learned_without_systemd(self):
        # a proxy that names no unit (PROXY_HOLD_UNITS unset, as run by hand) learns of a switch
        # only from the names the engine gives: kept, the 27B's vocabulary judged Kolibri's ids
        FakeVllm.served, FakeVllm.owner = "qwen3.8-27b", "sglang"
        self.mod.served_models()
        self.mod._VOCAB.update(size=248320, ts=time.time())
        FakeVllm.served, FakeVllm.owner = "kolibri-1", "vllm"
        self.mod._SERVED["ts"] = 0.0                       # the names' 600 s are over
        self.mod.served_models()
        self.assertEqual(self.mod._VOCAB["size"], 0, "served_models")
        FakeVllm.served, FakeVllm.owner = "qwen3.8-27b", "sglang"   # the 27B again
        self.mod._SERVED["ts"] = 0.0
        self.mod.served_models()
        self.mod._VOCAB.update(size=248320, ts=time.time())
        FakeVllm.served, FakeVllm.owner = "kolibri-1", "vllm"        # then Kolibri, met first
        self.mod.invalidate_pool()                                   # by the pool's read
        self.mod.pool_tokens()
        self.assertEqual(self.mod._VOCAB["size"], 0, "the pool's read of vLLM's facts")

    def test_effort_none_turns_thinking_off_on_a_qwen_lane_and_stays_none_on_kolibri(self):
        # the clients set up for Kolibri send "none" for "off", and the Qwen templates refuse
        # it with a 400: a client left on kolibri-1 broke on the next Qwen lane (found in
        # review, 2026-10-14)
        # and on the Qwen3.8 Flash-Next FP4-Local lane, a Qwen lane on vLLM whose template is the flash lane's
        for owner, served in (("sglang", "qwen3.8-flash-next"), ("vllm", "kolibri-1"), ("vllm", "qwen3.8-flash-next-fp4")):
            for how in ({"reasoning_effort": "none"}, {"chat_template_kwargs": {"reasoning_effort": "none"}}):
                with self.subTest(owner=owner, how=how):
                    FakeVllm.served, FakeVllm.owner = served, owner
                    self.mod.invalidate_pool()
                    self.mod.served_models()
                    got = self.post(dict({"model": "kolibri-1", "stream": True,
                                          "messages": [{"role": "user", "content": "hi"}]}, **how))
                    self.assertTrue(got.startswith(b"HTTP/1.1 200"), got[:200])
                    sent = FakeVllm.bodies[-1]
                    if served.startswith("qwen3.8-"):
                        self.assertNotIn("reasoning_effort", sent)
                        self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": False})
                    else:
                        self.assertEqual({k: sent[k] for k in how}, how, "Kolibri's template takes none")

    def test_other_levels_go_as_they_came_on_a_qwen_lane(self):
        FakeVllm.served, FakeVllm.owner = "qwen3.8-flash-next", "sglang"
        self.mod.served_models()
        for level in ("low", "high", "minimal"):
            with self.subTest(level=level):
                self.post({"model": "qwen3.8-flash-next", "stream": True, "reasoning_effort": level,
                           "messages": [{"role": "user", "content": "none of that"}]})
                self.assertEqual(FakeVllm.bodies[-1]["reasoning_effort"], level)
                self.assertNotIn("chat_template_kwargs", FakeVllm.bodies[-1])

    def test_a_key_sent_with_spaces_around_it_reaches_vllm(self):
        # the spaces around a header's value are not part of it (RFC 9110, 5.5): SGLang read
        # "Bearer <key> " as the key, vLLM compares the value as it comes, and the proxy forwarded
        # it untouched: the same client got a 401 on the Kolibri lane only (the production checks
        # after v1.29.0, 2026-10-14)
        for auth in (f"Bearer {KEY} ", f"Bearer {KEY}\t"):
            with self.subTest(auth=repr(auth[-1])):
                got = self.post({"model": "kolibri-1", "stream": True,
                                 "messages": [{"role": "user", "content": "hi"}]}, auth=auth)
                self.assertTrue(got.startswith(b"HTTP/1.1 200"), got[:160])
        got = self.post({"model": "kolibri-1", "messages": [{"role": "user", "content": "hi"}]},
                        auth=f"Bearer  {KEY}")
        self.assertTrue(got.startswith(b"HTTP/1.1 401"), "two spaces inside are still not the key")

    def test_vllm_is_not_asked_sglangs_info_routes_twice(self):
        # every read of the pool began with /server_info and /get_server_info, two 404s, and the
        # hold's probe asked /server_info each time (found in review, 2026-10-14)
        with FakeVllm.lock:
            FakeVllm.info_hits.clear()
        self.assertEqual(self.mod.pool_tokens(), 2014877)
        first = len(FakeVllm.info_hits)
        self.mod._POOL["ts"] = 0.0                    # the pool's 600 s are over
        self.assertEqual(self.mod.pool_tokens(), 2014877)
        self.assertTrue(self.mod.engine_answers())
        self.assertEqual((first, len(FakeVllm.info_hits)), (2, 2), FakeVllm.info_hits)
        self.mod.invalidate_pool()                    # a new engine is asked again
        self.mod.pool_tokens()
        self.assertEqual(len(FakeVllm.info_hits), 4)

    def test_an_sglang_engine_after_vllm_is_asked_its_routes_again(self):
        # a proxy that names no unit (PROXY_HOLD_UNITS unset) sees no start: Kolibri noted that
        # the engine has no SGLang route, a 27B took the port, and every read of its pool went
        # vLLM's way and found nothing, the guard down for that engine's life (review, 2026-10-14)
        self.assertEqual(self.mod.pool_tokens(), 2014877)
        self.assertIsNone(self.mod._INFO_ROUTE["path"])
        FakeVllm.served, FakeVllm.owner = "qwen3.8-27b", "sglang"
        self.mod._POOL["ts"] = 0.0
        self.assertIsNone(self.mod.pool_tokens(), "this read meets the new engine: not the vLLM engine's pool")
        self.assertEqual(self.mod._INFO_ROUTE["path"], "/server_info")
        self.assertEqual(self.mod.pool_tokens(), 863644)

    def test_on_sglang_a_foreign_name_goes_as_it_came(self):
        FakeVllm.served, FakeVllm.owner = "qwen3.8-27b", "sglang"
        self.mod.served_models()
        got = self.post({"model": "my-alias", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        self.assertTrue(got.startswith(b"HTTP/1.1 200"), got[:200])
        self.assertEqual(FakeVllm.bodies[-1]["model"], "my-alias")




class AnUpstreamShutDown(unittest.TestCase):
    """_shut_upstream ends the engine's connection, and never another one."""

    def test_a_connection_opened_meanwhile_on_the_same_number_is_left_alone(self):
        # the answer took its socket's number, then a duplicate of it was shut down: the pump
        # can close the answer in between (the stream's last chunk) and the number then goes to
        # the next socket anyone opens, which was shut down instead (found in review, 2026-10-14)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ["UPSTREAM"] = "http://127.0.0.1:9"
        mod = load_proxy(f"kproxy_shut_{time.time_ns()}")

        class Short(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *a):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"5\r\nhello\r\n0\r\n\r\n")
                self.wfile.flush()

        eng = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Short)
        eng.daemon_threads = True
        threading.Thread(target=eng.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (eng.shutdown(), eng.server_close()))
        other = socket.socket()
        other.bind(("127.0.0.1", 0))
        other.listen()
        self.addCleanup(other.close)
        h = mod.H.__new__(mod.H)
        req = urllib.request.Request(f"http://127.0.0.1:{eng.server_port}/s")
        h._upstream_req = req
        h._upstream_resp = resp = mod._UPSTREAM_OPENER.open(req, timeout=5)
        victim = {}
        real_dup = os.dup

        def the_pump_and_another_thread_first(fd):
            resp.read1(8192); resp.read1(8192)         # the last chunk: the answer closes
            v = socket.create_connection(other.getsockname())
            victim["sock"], victim["peer"] = v, other.accept()[0]
            return real_dup(fd)

        mod.os.dup = the_pump_and_another_thread_first
        try:
            if "kept_sock" in vars(getattr(req, "upstream", object())):
                resp.read1(8192); resp.read1(8192)     # the same order, for the code that keeps the socket
                v = socket.create_connection(other.getsockname())
                victim["sock"], victim["peer"] = v, other.accept()[0]
            h._shut_upstream()
        finally:
            mod.os.dup = real_dup
        self.addCleanup(victim["sock"].close)
        self.addCleanup(victim["peer"].close)
        victim["sock"].sendall(b"x")
        victim["peer"].settimeout(2)
        self.assertEqual(victim["peer"].recv(10), b"x", "another connection was shut down")


class ALaneSwitch(unittest.TestCase):
    """The proxy in front of a Kolibri unit that boots, then serves: a systemctl that reads the
    unit's state and its start's invocation id from files, and the fake engine on the port."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="vllm-switch-"))
        (self.t / "bin").mkdir()
        self.state, self.inv = self.t / "state", self.t / "inv"
        self.state.write_text("activating\n")
        self.inv.write_text("start-1\n")
        (self.t / "bin/systemctl").write_text(
            "#!/bin/sh\n"
            f'case "$*" in *InvocationID*) cat "{self.inv}"; exit 0;; esac\n'
            f'if [ "$1" = show ]; then cat "{self.state}"; exit 0; fi\n'
            "exit 3\n")
        (self.t / "bin/systemctl").chmod(0o755)
        # the engine's port is gone until start_engine, and the test's all along: no one else can take it
        self.engine_port = localports.held(self)
        saved, saved_argv = dict(os.environ), list(sys.argv)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved), setattr(sys, "argv", saved_argv)))
        os.environ.update(UPSTREAM=f"http://127.0.0.1:{self.engine_port}", KEEPALIVE_S="0.4",
                          PROXY_HOLD_UNITS="qwen38-kolibri.service", PROXY_HOLD_MAX_S="30",
                          PROXY_HOLD_POLL_S="0.2", PROMPT_CEILING_TOKENS="0", FLASH_PROMPT_CEILING_TOKENS="0",
                          PATH=f"{self.t / 'bin'}:{os.environ.get('PATH', '')}")
        self.mod = load_proxy(f"kproxy_switch_{time.time_ns()}")
        self.mod._api_key = lambda: KEY
        self.lines = []
        self.mod.log = self.lines.append
        self.proxy = self.mod.Server(("127.0.0.1", 0), self.mod.H)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.proxy.shutdown(), self.proxy.server_close()))
        self.engine = None
        FakeVllm.served, FakeVllm.owner = "kolibri-1", "vllm"
        with FakeVllm.lock:
            FakeVllm.bodies.clear(); FakeVllm.aborts.clear(); FakeVllm.closed_at.clear()

    def start_engine(self):
        self.engine = localports.serve(self.engine_port, http.server.ThreadingHTTPServer, FakeVllm)
        self.engine.daemon_threads = True
        threading.Thread(target=self.engine.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.engine.shutdown(), self.engine.server_close()))

    def started_again(self):
        """The unit's next start, as systemd says it; the proxy looks at most every 2 s."""
        self.inv.write_text("start-2\n")
        self.mod._STARTS["ts"] = -1e9

    def ask(self, model, out):
        raw = json.dumps({"model": model, "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]}).encode()
        s = socket.create_connection(("127.0.0.1", self.proxy.server_address[1]), timeout=40)
        s.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: p\r\nAuthorization: Bearer {KEY}\r\n"
                  f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n".encode() + raw)
        got = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            got += c
        s.close()
        out.append(got)

    def test_a_request_held_through_the_boot_reaches_kolibri_by_its_name(self):
        out = []
        th = threading.Thread(target=self.ask, args=("qwen3.8-27b", out), daemon=True)
        th.start()
        time.sleep(1.0)                  # held: the port is closed, the unit is activating
        self.start_engine()
        self.state.write_text("active\n")
        th.join(30)
        self.assertTrue(out, "no answer")
        self.assertIn(b"t1 ", out[0], out[0][-300:])
        self.assertEqual([b["model"] for b in FakeVllm.bodies if not b.get("logit_bias")], ["kolibri-1"])

    def test_the_first_request_after_a_switch_nobody_asked_about_is_renamed(self):
        FakeVllm.served, FakeVllm.owner = "qwen3.8-27b", "sglang"
        self.state.write_text("active\n")
        self.start_engine()
        self.mod.served_models()                       # the 27B, read and cached for 600 s
        self.assertEqual(self.mod._SERVED["engine"], "sglang")
        FakeVllm.served, FakeVllm.owner = "kolibri-1", "vllm"   # the cockpit loads Kolibri
        self.started_again()
        out = []
        self.ask("qwen3.8-27b", out)
        self.assertIn(b"t1 ", out[0], out[0][:300])
        self.assertEqual(FakeVllm.bodies[-1]["model"], "kolibri-1")

    def test_a_unit_that_started_again_drops_the_vocabulary(self):
        self.state.write_text("active\n")
        self.start_engine()
        self.mod.follow_engine_restarts()              # the first start, as the proxy first sees it
        self.mod._VOCAB.update(size=128000, ts=time.time())   # learned on Kolibri
        self.mod._rid_override_honoured = False
        self.started_again()                           # the cockpit loads the 27B
        FakeVllm.served, FakeVllm.owner = "qwen3.8-27b", "sglang"
        self.mod.served_vocab()
        self.assertTrue(any(b.get("logit_bias") for b in FakeVllm.bodies), "learned again, not kept")
        self.assertIsNone(self.mod._rid_override_honoured)


if __name__ == "__main__":
    unittest.main(verbosity=2)
