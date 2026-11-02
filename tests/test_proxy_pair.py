#!/usr/bin/env python3
"""Two boxes behind one endpoint (keepalive-proxy role 10, v6.50, discussion #45, decisions 961-966).

Two real proxies, each in front of a fake engine of its own: the entry box (spark1) serves the 27B, its
peer (spark2) serves Flash FP4-Local, and spark1's pair.json names spark2 with spark2's key. A
generation that names one of this repo's lanes' models goes to the box that serves it; a lane name no
box serves is refused with what each box serves; any other name stays on the entry box as on a box
alone; a peer that switched since its list was read answers 409 and is never made to serve the wrong
model; a request a peer sends on is never sent on again; GET /v1/models lists both boxes; the peer's
key never reaches a journal. And a box without pair.json behaves exactly as before.
"""
import http.client
import http.server
import importlib.util
import json
import os
import pathlib
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import localports  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
KEY_1, KEY_2 = "k-entry-111", "k-peer-222"
PAIR_TTL = 2                        # PAIR_TTL_S of the entry: its lists of the peer are read again after it


def fake_engine(name, model):
    """A fake SGLang engine that serves `model`, writes down every POST it gets (path, model, the
    key it was given) and answers a generation whole or as an SSE stream."""
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        served = [model]
        seen: list = []
        delay = 0.0
        mode = "ok"                 # "down": /v1/models answers 503 (a boot) ; "cut": a stream dies mid-way

        def log_message(self, *a):
            pass

        def reply(self, obj, code=200):
            out = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            if self.path.startswith("/v1/models") and Handler.mode == "down":
                return self.reply({"error": "loading"}, 503)
            if self.path.startswith("/v1/models"):
                return self.reply({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "sglang",
                                                               "max_model_len": 262144} for m in Handler.served]})
            if self.path.startswith(("/server_info", "/get_server_info")):
                return self.reply({"max_total_num_tokens": 900000})
            return self.reply({})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            Handler.seen.append((self.path, body.get("model"), self.headers.get("Authorization"),
                                 self.headers.get("X-Alzeo-Pair-Hop")))
            time.sleep(Handler.delay)
            if self.path.startswith("/v1/messages"):
                return self.reply({"id": "m", "type": "message", "role": "assistant", "model": Handler.served[0],
                                   "content": [{"type": "text", "text": f"from {name}"}]})
            if not body.get("stream"):
                return self.reply({"id": "c", "object": "chat.completion", "model": Handler.served[0],
                                   "choices": [{"index": 0, "message": {"role": "assistant", "content": f"from {name}"},
                                                "finish_reason": "stop"}]})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(3):
                ev = json.dumps({"id": "c", "object": "chat.completion.chunk", "model": Handler.served[0],
                                 "choices": [{"index": 0, "delta": {"content": f"{name}-{i} "}}]})
                part = f"data: {ev}\n\n".encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part)); self.wfile.flush()
                time.sleep(0.05)
                if Handler.mode == "cut" and i == 1:
                    self.connection.shutdown(socket.SHUT_RDWR)      # the engine dies mid-stream
                    self.close_connection = True
                    return
            part = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(part), part)); self.wfile.flush()

    return Handler


class Engine(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class TwoBoxesBehindOneEndpoint(unittest.TestCase):
    # The entry runs as a process of its own here, and in this test's process in the subclass at the end,
    # where the coverage gate of CI sees its lines (as test_proxy_hold.py does for role 6)
    INPROC = False

    @classmethod
    def setUpClass(cls):
        cls.E1, cls.E2 = fake_engine("spark1", "qwen3.8-27b"), fake_engine("spark2", "qwen3.8-flash-next-fp4")
        cls.engines = []
        for h in (cls.E1, cls.E2):
            e = Engine(("127.0.0.1", 0), h)
            threading.Thread(target=e.serve_forever, daemon=True).start()
            cls.engines.append(e)
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="kp-pair-"))
        cls.procs = []
        cls.home2 = cls.home(cls.tmp / "spark2", KEY_2, {"name": "spark2", "peers": []})
        cls.port2 = cls.start_proxy(cls.home2, cls.engines[1], "spark2")
        cls.home1 = cls.home(cls.tmp / "spark1", KEY_1, {"name": "spark1", "peers": [
            {"name": "spark2", "proxy": f"http://127.0.0.1:{cls.port2}", "key_file": "pair/spark2.key"}]})
        (cls.home1 / ".config/qwen38/pair").mkdir()
        (cls.home1 / ".config/qwen38/pair/spark2.key").write_text(KEY_2 + "\n")
        if cls.INPROC:
            cls.port1 = cls.start_inproc(cls.home1, cls.engines[0])
        else:
            cls.port1 = cls.start_proxy(cls.home1, cls.engines[0], "spark1", PAIR_TTL_S=str(PAIR_TTL))

    @classmethod
    def start_inproc(cls, home, engine):
        cls.saved_env = dict(os.environ)
        os.environ.update({"HOME": str(home), "PROXY_BIND": "127.0.0.1", "PAIR_TTL_S": str(PAIR_TTL),
                           "UPSTREAM": f"http://127.0.0.1:{engine.server_address[1]}",
                           "PROMPT_CEILING_TOKENS": "0", "FLASH_PROMPT_CEILING_TOKENS": "0"})
        for k in ("QWEN38_CLIENT_KEYS_FILE", "QWEN38_UPSTREAM_API_KEY", "PROXY_HOLD_UNITS", "QWEN38_PAIR_FILE",
                  "http_proxy", "HTTP_PROXY"):
            os.environ.pop(k, None)
        spec = importlib.util.spec_from_file_location(f"kp_pair_{time.time_ns()}", REPO / "keepalive-proxy.py")
        cls.mod = importlib.util.module_from_spec(spec)
        saved_argv, sys.argv = sys.argv, ["keepalive-proxy.py"]
        try:
            spec.loader.exec_module(cls.mod)
        finally:
            sys.argv = saved_argv
        cls.lines = []
        cls.mod.log = lambda msg: cls.lines.append(f"[proxy] {msg}")
        cls.mod.pair_conf()
        cls.inproc = cls.mod.Server(("127.0.0.1", 0), cls.mod.H)
        threading.Thread(target=cls.inproc.serve_forever, daemon=True).start()
        return cls.inproc.server_address[1]

    @staticmethod
    def home(path, key, pair):
        cfg = path / ".config" / "qwen38"
        cfg.mkdir(parents=True)
        (cfg / "api-key").write_text(key + "\n")
        if pair is not None:
            (cfg / "pair.json").write_text(json.dumps(pair))
        return path

    @classmethod
    def start_proxy(cls, home, engine, label, **extra):
        env = {"PATH": os.environ["PATH"], "HOME": str(home), "PROXY_BIND": "127.0.0.1",
               "UPSTREAM": f"http://127.0.0.1:{engine.server_address[1]}",
               "PROMPT_CEILING_TOKENS": "0", "FLASH_PROMPT_CEILING_TOKENS": "0", **extra}
        with open(cls.tmp / f"{label}.journal", "ab") as journal:     # the child keeps its own copy
            proc, port = localports.spawn(lambda port: subprocess.Popen(
                [sys.executable, str(REPO / "keepalive-proxy.py"), str(port)], env=env,
                stdout=subprocess.DEVNULL, stderr=journal))
        cls.procs.append(proc)
        return port

    @classmethod
    def tearDownClass(cls):
        if cls.INPROC:
            cls.inproc.shutdown(); cls.inproc.server_close()
            os.environ.clear(); os.environ.update(cls.saved_env)
        for p in cls.procs:
            if p.poll() is None:
                p.terminate()
                p.wait(10)
        for e in cls.engines:
            e.shutdown(); e.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.E1.seen.clear(); self.E2.seen.clear()
        self.E1.served[:] = ["qwen3.8-27b"]; self.E2.served[:] = ["qwen3.8-flash-next-fp4"]
        self.E1.delay = self.E2.delay = 0.0
        self.E1.mode = self.E2.mode = "ok"

    def call(self, path, body=None, port=None, key=KEY_1, headers=None, method="POST"):
        c = http.client.HTTPConnection("127.0.0.1", port or self.port1, timeout=60)
        try:
            h = {"Content-Type": "application/json", "Authorization": f"Bearer {key}", **(headers or {})}
            c.request(method, path, json.dumps(body).encode() if body is not None else None, h)
            r = c.getresponse()
            return r.status, r.read(), dict(r.getheaders())
        finally:
            c.close()

    def chat(self, model, **kw):
        return {"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}], **kw}

    def journal(self, label):
        if label == "spark1" and self.INPROC:
            time.sleep(0.2)                 # the outcome line is written after the answer's last byte
            return "\n".join(self.lines)
        return (self.tmp / f"{label}.journal").read_text(errors="replace")

    # ---- routing --------------------------------------------------------------------------------
    def test_a_lane_this_box_serves_stays_here(self):
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertEqual([s[1] for s in self.E1.seen], ["qwen3.8-27b"])
        self.assertEqual(self.E2.seen, [])

    def test_a_lane_the_peer_serves_goes_there_with_the_peers_key(self):
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out)
        self.assertEqual(self.E1.seen, [])
        self.assertEqual(len(self.E2.seen), 1)
        path, model, auth, hop = self.E2.seen[0]
        self.assertEqual((path, model, auth), ("/v1/chat/completions", "qwen3.8-flash-next-fp4", f"Bearer {KEY_2}"))
        self.assertIsNone(hop, "the hop header stops at the peer's proxy, never reaches its engine")

    def test_a_stream_from_the_peer_arrives_whole_and_in_order(self):
        st, out, hdrs = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4", stream=True))
        self.assertEqual(st, 200, out[:300])
        self.assertIn("text/event-stream", hdrs.get("Content-Type", ""))
        text = out.decode()
        self.assertLess(text.index("spark2-0"), text.index("spark2-1"))
        self.assertLess(text.index("spark2-1"), text.index("spark2-2"))
        self.assertTrue(text.rstrip().endswith("data: [DONE]"), text[-200:])

    def test_the_anthropic_dialect_goes_to_the_peer_too(self):
        st, out, _ = self.call("/v1/messages", self.chat("qwen3.8-flash-next-fp4"))
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(json.loads(out)["content"][0]["text"], "from spark2")

    def test_any_other_name_stays_on_this_box_as_on_a_box_alone(self):
        for name in ("claude-haiku-4-5", "gpt-4o", ""):
            st, out, _ = self.call("/v1/chat/completions", self.chat(name))
            self.assertEqual(st, 200, out[:300])
        self.assertEqual(len(self.E1.seen), 3)
        self.assertEqual(self.E2.seen, [])

    # ---- refusals -------------------------------------------------------------------------------
    def test_a_lane_no_box_serves_is_refused_with_what_each_box_serves(self):
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"))
        self.assertEqual(st, 404, out[:300])
        err = json.loads(out)["error"]
        self.assertEqual(err["type"], "model_not_found")
        self.assertEqual(err["served"], {"spark1": ["qwen3.8-27b"], "spark2": ["qwen3.8-flash-next-fp4"]})
        self.assertEqual(self.E1.seen + self.E2.seen, [], "no engine is asked for a model it does not serve")

    def test_the_anthropic_refusal_is_in_its_own_shape(self):
        st, out, _ = self.call("/v1/messages", self.chat("ornith-1.5-122b"))
        self.assertEqual(st, 404, out[:300])
        j = json.loads(out)
        self.assertEqual((j["type"], j["error"]["type"]), ("error", "model_not_found"))

    def test_a_peer_that_switched_since_its_list_was_read_never_serves_the_wrong_model(self):
        st, _, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertEqual(st, 200)
        self.E2.served[:] = ["ornith-1.5-122b"]      # the peer switched; the entry's list is up to 5 s old
        self.E2.seen.clear()
        # the peer's own proxy keeps its engine's names 600 s: a real switch restarts it, here its
        # cache is the stale side, so the peer is given a fresh proxy that reads the new name
        port2b = self.start_proxy(self.home2, self.engines[1], "spark2b")
        pair = json.loads((self.home1 / ".config/qwen38/pair.json").read_text())
        pair["peers"][0]["proxy"] = f"http://127.0.0.1:{port2b}"
        try:
            (self.home1 / ".config/qwen38/pair.json").write_text(json.dumps(pair))
            time.sleep(2.5)                               # the entry looks at pair.json every 2 s
            st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
            self.assertEqual(st, 404, out[:300])
            self.assertEqual(self.E2.seen, [], "the peer's engine is never handed the old model's request")
            st, out, _ = self.call("/v1/chat/completions", self.chat("ornith-1.5-122b"))
            self.assertEqual(st, 200, out[:300])
        finally:
            pair["peers"][0]["proxy"] = f"http://127.0.0.1:{self.port2}"
            (self.home1 / ".config/qwen38/pair.json").write_text(json.dumps(pair))
            time.sleep(2.5)

    def test_a_request_a_peer_sent_on_is_served_or_refused_never_sent_on(self):
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"),
                               headers={"X-Alzeo-Pair-Hop": "spark9"})
        self.assertEqual(st, 409, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "model_not_served")
        self.assertEqual(self.E1.seen + self.E2.seen, [])

    def test_a_peer_that_does_not_answer_is_said_at_once(self):
        pair = json.loads((self.home1 / ".config/qwen38/pair.json").read_text())
        dead = localports.held(self)                     # bound, never listening: refused, as a box that is gone
        pair["peers"][0]["proxy"] = f"http://127.0.0.1:{dead}"
        try:
            (self.home1 / ".config/qwen38/pair.json").write_text(json.dumps(pair))
            time.sleep(2.5)
            t0 = time.monotonic()
            st, out, _hdrs = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
            self.assertLess(time.monotonic() - t0, 15)
            self.assertEqual(st, 503, out[:300])        # the box that may serve it does not answer: try again
            self.assertEqual(_hdrs.get("Retry-After"), "30", "every 503 of this proxy says when to try again")
            err = json.loads(out)["error"]
            self.assertEqual(err["type"], "engine_unavailable")
            self.assertIn("does not answer", err["served"]["spark2"])
        finally:
            pair["peers"][0]["proxy"] = f"http://127.0.0.1:{self.port2}"
            (self.home1 / ".config/qwen38/pair.json").write_text(json.dumps(pair))
            time.sleep(2.5)

    # ---- the list, the key, a box alone ---------------------------------------------------------
    def models_once_known(self, port=None):
        """GET /v1/models until the peer's list, read in the background, is in it (3 s at most)."""
        deadline = time.monotonic() + 3
        while True:
            st, out, _ = self.call("/v1/models", port=port, method="GET")
            rows = {r["id"]: r.get("box") for r in json.loads(out)["data"]} if st == 200 else {}
            if len(rows) > 1 or time.monotonic() > deadline:
                return st, rows
            time.sleep(0.2)

    def test_the_models_list_names_both_boxes(self):
        st, rows = self.models_once_known()
        self.assertEqual(st, 200)
        self.assertEqual(rows, {"qwen3.8-27b": "spark1", "qwen3.8-flash-next-fp4": "spark2"})

    def test_the_models_list_never_waits_for_a_peer(self):
        class Frozen(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                time.sleep(20)
        port = localports.held(self)
        frozen = localports.serve(port, Engine, Frozen)
        threading.Thread(target=frozen.serve_forever, daemon=True).start()
        self.addCleanup(frozen.server_close); self.addCleanup(frozen.shutdown)
        entry = self.spare_proxy("frozen-list", KEY_1, self.entry_pair(port))
        for _ in range(3):
            t0 = time.monotonic()
            st, out, _ = self.call("/v1/models", port=entry, method="GET")
            self.assertLess(time.monotonic() - t0, 2, "needle.sh and bench-matrix.sh give it 5 s")
            self.assertEqual(st, 200)

    def test_the_peers_key_reaches_no_journal(self):
        self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        for label in ("spark1", "spark2"):
            self.assertNotIn(KEY_2, self.journal(label))
        self.assertIn("-> pair spark2 for qwen3.8-flash-next-fp4", self.journal("spark1"))

    def test_a_box_without_pair_json_is_what_it_was(self):
        alone = self.home(self.tmp / "alone", KEY_1, None)
        port = self.start_proxy(alone, self.engines[0], "alone")
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=port)
        self.assertEqual(st, 200, out[:300])           # any name reaches the lane that serves, as before
        st, out, _ = self.call("/v1/models", port=port, method="GET")
        self.assertEqual([r.get("box") for r in json.loads(out)["data"]], [None])

    def test_a_pair_json_that_does_not_parse_leaves_the_box_alone(self):
        broken = self.home(self.tmp / "broken", KEY_1, None)
        (broken / ".config/qwen38/pair.json").write_text('{"peers": [{"name": "x"}]}')
        port = self.start_proxy(broken, self.engines[0], "broken")
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=port)
        self.assertEqual(st, 200, out[:300])
        self.assertRegex(self.journal("broken"), r"pair: .*pair\.json is not used: peer 0 needs")


    # ---- what the review of 2026-11-02 found ----------------------------------------------------
    def test_a_peer_whose_engine_restarts_gets_a_503_to_try_again_not_a_404(self):
        self.E2.mode = "down"
        try:
            time.sleep(PAIR_TTL + 0.5)                   # the entry's list of the peer is read again
            st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
            self.assertEqual(st, 503, out[:300])
            self.assertEqual(json.loads(out)["error"]["type"], "engine_unavailable")
            self.assertEqual(self.E2.seen, [])
        finally:
            self.E2.mode = "ok"
            time.sleep(PAIR_TTL + 0.5)

    def test_a_stream_the_peer_cuts_reaches_the_client_cut_never_ended(self):
        self.E2.mode = "cut"
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=60)
        try:
            c.request("POST", "/v1/chat/completions", json.dumps(self.chat("qwen3.8-flash-next-fp4", stream=True)).encode(),
                      {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            with self.assertRaises(http.client.IncompleteRead):   # no closing chunk: the client sees a cut
                r.read()
        finally:
            c.close()
        self.assertIn("UPSTREAM CUT pair", self.journal("spark1"))

    def test_a_peer_without_pair_json_refuses_a_request_sent_on_for_another_model(self):
        bare = self.home(self.tmp / "bare-peer", KEY_2, None)            # the peer of a one-entry pair
        port = self.start_proxy(bare, self.engines[0], "bare-peer")     # its engine serves the 27B
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port, key=KEY_2,
                               headers={"X-Alzeo-Pair-Hop": "spark1"})
        self.assertEqual(st, 409, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "model_not_served")
        self.assertEqual(self.E1.seen, [], "the 27B is never asked a request for Flash")
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port, key=KEY_2)
        self.assertEqual(st, 200, "a client of its own, with no hop, is served as on a box alone")

    def test_a_request_sent_on_to_a_box_whose_engine_restarts_is_told_to_try_again_not_held(self):
        # a peer whose engine does not answer while its unit starts again (a switch): its own clients are
        # held for whatever lane comes back (role 6), but a request an entry sent on for one model is told
        # at once to try again, since the lane that comes back may serve another
        t = self.tmp / "restarting"
        (t / "bin").mkdir(parents=True)
        (t / "bin/systemctl").write_text(
            "#!/bin/sh\n"
            'case "$*" in *InvocationID*) echo inv-1; echo; exit 0;; esac\n'
            'if [ "$1" = show ]; then echo activating; exit 0; fi\n'
            'if [ "$1" = list-jobs ]; then exit 0; fi\n'
            "exit 3\n")
        (t / "bin/systemctl").chmod(0o755)
        home = self.home(t / "home", KEY_2, None)
        gone = localports.held(self)                      # its engine: nothing listens there
        env = {"PATH": f"{t / 'bin'}:{os.environ['PATH']}", "HOME": str(home), "PROXY_BIND": "127.0.0.1",
               "UPSTREAM": f"http://127.0.0.1:{gone}", "PROMPT_CEILING_TOKENS": "0", "FLASH_PROMPT_CEILING_TOKENS": "0",
               "PROXY_HOLD_UNITS": "qwen38-x.service", "PROXY_HOLD_MAX_S": "20", "PROXY_HOLD_POLL_S": "0.2"}
        with open(self.tmp / "restarting.journal", "ab") as journal:
            proc, port = localports.spawn(lambda port: subprocess.Popen(
                [sys.executable, str(REPO / "keepalive-proxy.py"), str(port)], env=env,
                stdout=subprocess.DEVNULL, stderr=journal))
        self.procs.append(proc)
        t0 = time.monotonic()
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=port, key=KEY_2,
                               headers={"X-Alzeo-Pair-Hop": "spark1"})
        self.assertLess(time.monotonic() - t0, 5, "not held up to PROXY_HOLD_MAX_S")
        self.assertEqual(st, 503, out[:300])
        self.assertIn("try again", json.loads(out)["error"]["message"])

    def test_a_peer_that_takes_the_connection_and_never_answers_holds_nobody(self):
        class Frozen(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                time.sleep(40)                      # a proxy that hangs: the connection is taken, no answer

            do_POST = do_GET
        port = localports.held(self)
        frozen = localports.serve(port, Engine, Frozen)
        threading.Thread(target=frozen.serve_forever, daemon=True).start()
        self.addCleanup(frozen.server_close)
        self.addCleanup(frozen.shutdown)
        pair = json.loads((self.home1 / ".config/qwen38/pair.json").read_text())
        pair["peers"][0]["proxy"] = f"http://127.0.0.1:{port}"
        try:
            (self.home1 / ".config/qwen38/pair.json").write_text(json.dumps(pair))
            time.sleep(2.5)
            t0 = time.monotonic()
            st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
            self.assertLess(time.monotonic() - t0, 20, "two reads of its list, 5 s each, then the answer")
            self.assertEqual(st, 503, out[:300])
            t0 = time.monotonic()
            st, out, _ = self.call("/v1/models", method="GET")
            self.assertLess(time.monotonic() - t0, 12)
            self.assertEqual([r["id"] for r in json.loads(out)["data"]], ["qwen3.8-27b"])
        finally:
            pair["peers"][0]["proxy"] = f"http://127.0.0.1:{self.port2}"
            (self.home1 / ".config/qwen38/pair.json").write_text(json.dumps(pair))
            time.sleep(2.5)

    def test_two_boxes_that_name_each_other_ask_each_other_once(self):
        a = self.home(self.tmp / "sym-a", KEY_1, None)
        b = self.home(self.tmp / "sym-b", KEY_2, None)
        pa = self.start_proxy(a, self.engines[0], "sym-a")
        pb = self.start_proxy(b, self.engines[1], "sym-b")
        for home, me, peer, port, key in ((a, "spark1", "spark2", pb, KEY_2), (b, "spark2", "spark1", pa, KEY_1)):
            (home / ".config/qwen38/pair").mkdir()
            (home / ".config/qwen38/pair" / f"{peer}.key").write_text(key + "\n")
            (home / ".config/qwen38/pair.json").write_text(json.dumps({"name": me, "peers": [
                {"name": peer, "proxy": f"http://127.0.0.1:{port}", "key_file": f"pair/{peer}.key"}]}))
        time.sleep(2.5)
        st, rows = self.models_once_known(port=pa)
        self.assertEqual(st, 200)
        self.assertEqual(rows, {"qwen3.8-27b": "spark1", "qwen3.8-flash-next-fp4": "spark2"})
        count = lambda label: self.journal(label).count("GET /v1/models ok get")
        before = (count("sym-a"), count("sym-b"))
        time.sleep(2)                       # a loop would go on asking with nobody calling
        self.assertEqual((count("sym-a"), count("sym-b")), before, "no GET goes on once the client stopped")
        self.assertLessEqual(before[1], before[0], "each GET of the client reads the peer's own list once at most")
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=pa)
        self.assertEqual(st, 404, out[:300])
        self.assertEqual(json.loads(out)["error"]["served"],
                         {"spark1": ["qwen3.8-27b"], "spark2": ["qwen3.8-flash-next-fp4"]},
                         "each box says what it serves itself, not the union it shows its clients")


    # ---- what the code review of 2026-11-02 found -------------------------------------------------
    def spare_proxy(self, label, key, pair, upstream=None, extra=None, keyfile=True):
        home = self.home(self.tmp / label, key, pair)
        if not keyfile:
            (home / ".config/qwen38/api-key").unlink()
        if pair and pair.get("peers"):
            (home / ".config/qwen38/pair").mkdir()
            (home / ".config/qwen38/pair/spark2.key").write_text(KEY_2 + "\n")
        env = {"PATH": os.environ["PATH"], "HOME": str(home), "PROXY_BIND": "127.0.0.1",
               "UPSTREAM": upstream or f"http://127.0.0.1:{self.engines[0].server_address[1]}",
               "PROMPT_CEILING_TOKENS": "0", "FLASH_PROMPT_CEILING_TOKENS": "0", **(extra or {})}
        with open(self.tmp / f"{label}.journal", "ab") as journal:
            proc, port = localports.spawn(lambda port: subprocess.Popen(
                [sys.executable, str(REPO / "keepalive-proxy.py"), str(port)], env=env,
                stdout=subprocess.DEVNULL, stderr=journal))
        self.procs.append(proc)
        return port

    def entry_pair(self, peer_port):
        return {"name": "spark1", "peers": [{"name": "spark2", "proxy": f"http://127.0.0.1:{peer_port}",
                                             "key_file": "pair/spark2.key"}]}

    def test_a_box_that_cannot_read_its_own_key_sends_nothing_on(self):
        port = self.spare_proxy("no-key", KEY_1, self.entry_pair(self.port2), keyfile=False)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port, key="anything")
        self.assertEqual(st, 503, out[:300])
        self.assertEqual(self.E2.seen, [], "the peer is never sent a request this box could not check")

    def test_a_pair_json_with_a_bad_port_or_name_leaves_the_box_alone(self):
        for label, pair in (("bad-port", {"name": "spark1", "peers": [{"name": "spark2", "proxy": "http://127.0.0.1:3000l",
                                                                        "key_file": "pair/spark2.key"}]}),
                            ("bad-name", {"name": "Spark \u2460 bureau", "peers": []})):
            port = self.spare_proxy(label, KEY_1, pair)
            st, out, _ = self.call("/v1/models", port=port, method="GET")
            self.assertEqual(st, 200, out[:300])
            self.assertEqual([r.get("box") for r in json.loads(out)["data"]], [None], "a box alone")
            self.assertIn("pair.json is not used", self.journal(label))

    def test_an_entry_whose_engine_does_not_answer_says_so_on_v1_models(self):
        port = self.spare_proxy("entry-down", KEY_1, self.entry_pair(self.port2),
                                upstream=f"http://127.0.0.1:{localports.held(self)}")
        st, out, _ = self.call("/v1/models", port=port, method="GET")
        self.assertEqual(st, 503, "as a box alone: no peer's model stands in for this box's engine")

    def test_no_http_proxy_of_the_environment_gets_the_peers_key(self):
        dead = localports.held(self)
        port = self.spare_proxy("with-proxy-env", KEY_1, self.entry_pair(self.port2),
                                extra={"http_proxy": f"http://127.0.0.1:{dead}", "HTTP_PROXY": f"http://127.0.0.1:{dead}"})
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 200, out[:300])           # through the dead proxy it would have failed
        self.assertIn(b"from spark2", out)

    def test_a_peer_that_answers_409_has_every_list_read_again(self):
        class Switched(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            models = ["qwen3.8-flash-next-fp4"]
            posts: list = []

            def log_message(self, *a):
                pass

            def reply(self, obj, code=200):
                out = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_GET(self):
                self.reply({"object": "list", "data": [{"id": m, "object": "model"} for m in Switched.models]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                Switched.posts.append(body.get("model"))
                if body.get("model") not in Switched.models:
                    return self.reply({"error": {"type": "model_not_served", "message": "another model here"}}, 409)
                self.reply({"id": "c", "choices": [{"message": {"content": "from the switched peer"}}]})
        held = localports.held(self)
        fake = localports.serve(held, Engine, Switched)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        self.addCleanup(fake.server_close); self.addCleanup(fake.shutdown)
        port = self.spare_proxy("switch-409", KEY_1, self.entry_pair(held), extra={"PAIR_TTL_S": "300"})
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 200, out[:300])          # its list is read and kept 300 s
        Switched.models = ["ornith-1.5-122b"]         # the peer switches; the entry's list is the old one
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 404, out[:300])          # its 409 had every list read again: served nowhere now
        self.assertEqual(json.loads(out)["error"]["served"]["spark2"], ["ornith-1.5-122b"])
        self.assertEqual(Switched.posts, ["qwen3.8-flash-next-fp4", "qwen3.8-flash-next-fp4"])
        self.assertIn("serves another model now; the lists are read again", self.journal("switch-409"))
        st, out, _ = self.call("/v1/chat/completions", self.chat("ornith-1.5-122b"), port=port)
        self.assertEqual(st, 200, out[:300])

    def test_a_silent_peer_stream_reaches_the_client_cut(self):
        class Silent(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
                part = b'data: {"choices": [{"delta": {"content": "x"}}]}\n\n'
                self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part)); self.wfile.flush()
                time.sleep(30)                      # the box went: no byte, no FIN
        held = localports.held(self)
        fake = localports.serve(held, Engine, Silent)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        self.addCleanup(fake.server_close); self.addCleanup(fake.shutdown)
        port = self.spare_proxy("silent", KEY_1, self.entry_pair(held), extra={"PAIR_STREAM_SILENCE_S": "2"})
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            t0 = time.monotonic()
            c.request("POST", "/v1/chat/completions", json.dumps(self.chat("qwen3.8-flash-next-fp4", stream=True)).encode(),
                      {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
            r = c.getresponse()
            with self.assertRaises(http.client.IncompleteRead):
                r.read()
            self.assertLess(time.monotonic() - t0, 10)
        finally:
            c.close()

    def test_a_request_held_through_a_restart_is_never_answered_by_another_lane(self):
        t = self.tmp / "held"
        (t / "bin").mkdir(parents=True)
        state = t / "state"
        state.write_text("activating\n")
        (t / "bin/systemctl").write_text(
            "#!/bin/sh\n"
            'case "$*" in *InvocationID*) echo inv-1; echo; exit 0;; esac\n'
            f'if [ "$1" = show ]; then cat "{state}"; exit 0; fi\n'
            'if [ "$1" = list-jobs ]; then exit 0; fi\n'
            "exit 3\n")
        (t / "bin/systemctl").chmod(0o755)
        engine_port = localports.held(self)
        port = self.spare_proxy("held-box", KEY_2, {"name": "spark2", "peers": []},
                                upstream=f"http://127.0.0.1:{engine_port}",
                                extra={"PATH": f"{t / 'bin'}:{os.environ['PATH']}", "PROXY_HOLD_UNITS": "qwen38-x.service",
                                       "PROXY_HOLD_MAX_S": "30", "PROXY_HOLD_POLL_S": "0.2"})
        Ornith = fake_engine("ornith-box", "ornith-1.5-122b")
        def come_back():
            time.sleep(1.5)
            e = localports.serve(engine_port, Engine, Ornith)    # the lane that comes back serves Ornith
            self.addCleanup(e.server_close); self.addCleanup(e.shutdown)
            e.serve_forever()
        threading.Thread(target=come_back, daemon=True).start()
        st, out, hdrs = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port, key=KEY_2)
        self.assertEqual(st, 503, out[:300])             # to try again: sent again, it goes where Flash is served
        self.assertEqual(hdrs.get("Retry-After"), "30")
        self.assertIn("came back serving ornith-1.5-122b", json.loads(out)["error"]["message"])
        self.assertEqual(Ornith.seen, [], "Ornith is never asked a request for Flash")


class TwoBoxesBehindOneEndpointInProcess(TwoBoxesBehindOneEndpoint):
    INPROC = True


class ThePairsPieces(unittest.TestCase):
    """The pair's functions one by one, in this process: what pair.json may say, how a peer's list is read and
    kept, the hop header, the union of /v1/models."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="kp-pair-pieces-"))
        (cls.tmp / ".config/qwen38").mkdir(parents=True)
        cls.pair_file = cls.tmp / ".config/qwen38/pair.json"
        saved = dict(os.environ)
        os.environ.update({"HOME": str(cls.tmp), "QWEN38_PAIR_FILE": str(cls.pair_file), "PAIR_TTL_S": "30"})
        try:
            spec = importlib.util.spec_from_file_location(f"kp_pair_pieces_{time.time_ns()}", REPO / "keepalive-proxy.py")
            cls.mod = importlib.util.module_from_spec(spec)
            saved_argv, sys.argv = sys.argv, ["keepalive-proxy.py"]
            try:
                spec.loader.exec_module(cls.mod)
            finally:
                sys.argv = saved_argv
        finally:
            os.environ.clear(); os.environ.update(saved)
        cls.lines = []
        cls.mod.log = lambda msg: cls.lines.append(msg)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def parse(self, conf):
        return self.mod._pair_parse(json.dumps(conf))

    def peer(self, name="spark2", proxy="http://127.0.0.1:30001"):
        return {"name": name, "proxy": proxy, "key_file": "pair/k"}

    def test_what_pair_json_may_say(self):
        ok = self.parse({"name": "Spark-1", "peers": [self.peer(), self.peer("b", "http://[fe80::1]:30001"),
                                                       self.peer("c", "HTTP://spark2.local:30001/")]})
        self.assertEqual([p["proxy"] for p in ok["peers"]],
                         ["http://127.0.0.1:30001", "http://[fe80::1]:30001", "http://spark2.local:30001"])
        self.assertTrue(ok["peers"][0]["key_file"].endswith("/.config/qwen38/pair/k"))
        self.assertTrue(self.mod.PAIR_NAME.fullmatch(self.parse({"peers": []})["name"]), "the host's name, made safe")

    def test_what_pair_json_may_not_say(self):
        for conf in ([], {"peers": {}}, {"peers": [{"name": "x"}]}, {"peers": [self.peer("two words")]},
                     {"peers": [self.peer(proxy="https://127.0.0.1:30001")]}, {"peers": [self.peer(proxy="http://127.0.0.1:3000l")]},
                     {"peers": [self.peer(proxy="http://127.0.0.1")]}, {"peers": [self.peer(proxy="http://127.0.0.1:30001/v1")]},
                     {"peers": [self.peer(proxy="http://u:p@127.0.0.1:30001")]}, {"peers": [self.peer(proxy="http://127.0.0.1:30001?a")]},
                     {"name": "", "peers": []}, {"name": "Spark \u2460", "peers": []}, {"name": "x" * 41, "peers": []},
                     {"peers": [self.peer("a"), self.peer("a")]}, {"name": "a", "peers": [self.peer("a")]}):
            with self.subTest(conf=conf), self.assertRaises(ValueError):
                self.parse(conf)

    def test_pair_json_is_read_again_when_it_changes_and_a_bad_one_leaves_the_box_alone(self):
        self.mod._PAIR.update(seen=0.0, stamp=None)
        self.pair_file.write_text(json.dumps({"name": "one", "peers": []}))
        self.assertEqual(self.mod.pair_conf()["name"], "one")
        self.assertEqual(self.mod.pair_conf()["name"], "one", "kept between two looks")
        self.pair_file.write_text("{not json")
        self.mod._PAIR["seen"] = 0.0
        self.assertIsNone(self.mod.pair_conf())
        self.assertTrue(any("is not used" in ln for ln in self.lines))
        self.pair_file.unlink()
        self.mod._PAIR["seen"] = 0.0
        self.assertIsNone(self.mod.pair_conf())

    def test_the_hop_header_and_the_model(self):
        hop = self.mod.pair_hop
        self.assertIsNone(hop({}))
        self.assertIsNone(hop({"X-Alzeo-Pair-Hop": "  "}))
        self.assertEqual(hop({"X-Alzeo-Pair-Hop": "spark1"}), "spark1")
        self.assertEqual(hop({"X-Alzeo-Pair-Hop": "spark1\nGET /fake"}), "a peer")
        bm = self.mod.body_model
        self.assertEqual(bm(b'{"model": "kolibri-1"}'), "kolibri-1")
        for body in (b"not json", b"[1]", b'{"model": 3}', b"{}"):
            self.assertIsNone(bm(body))

    def test_a_peers_list_is_read_kept_and_read_again(self):
        class Lists(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            answer, gets = (200, {"data": [{"id": "kolibri-1"}, {"id": 3}, "x"]}), []

            def log_message(self, *a):
                pass

            def do_GET(self):
                Lists.gets.append(self.headers.get("X-Alzeo-Pair-Hop"))
                code, obj = Lists.answer
                out = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        port = localports.held(self)
        srv = localports.serve(port, Engine, Lists)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        key = self.tmp / "peer.key"
        key.write_text("k-peer\n")
        peer = {"name": "lists", "proxy": f"http://127.0.0.1:{port}", "key_file": str(key)}
        pm = self.mod.peer_models
        self.assertEqual(pm(peer), ("kolibri-1",))
        self.assertEqual(pm(peer), ("kolibri-1",), "kept")
        self.assertEqual(len(Lists.gets), 1)
        self.assertTrue(Lists.gets[0], "a peer's list is asked as a hop: its own, never its union")
        t = time.monotonic()
        self.assertEqual(pm(peer, fresh=True, since=t - 60), ("kolibri-1",), "read after `since`: kept")
        self.assertEqual(len(Lists.gets), 1)
        Lists.answer = (200, [1, 2])
        self.assertEqual(pm(peer, fresh=True), ())
        self.assertIn("not a list", self.mod._PAIR_MODELS["lists"]["error"])
        Lists.answer = (503, {"error": "loading"})
        self.assertEqual(pm(peer, fresh=True), ())
        self.assertEqual(self.mod._PAIR_MODELS["lists"]["error"], "HTTP 503 on /v1/models")
        gone = {"name": "gone", "proxy": f"http://127.0.0.1:{localports.held(self)}", "key_file": str(key)}
        self.assertEqual(pm(gone), ())
        self.assertTrue(self.mod._PAIR_MODELS["gone"]["error"])
        nokey = {"name": "nokey", "proxy": f"http://127.0.0.1:{port}", "key_file": str(self.tmp / "missing.key")}
        self.assertEqual(pm(nokey), ())

    def test_the_union_and_the_served_map(self):
        self.mod._PAIR.update(seen=0.0, stamp=None)
        self.pair_file.write_text(json.dumps({"name": "spark1", "peers": [self.peer()]}))
        conf = self.mod.pair_conf()
        self.mod._PAIR_MODELS["spark2"] = {"ts": time.monotonic(), "models": ("kolibri-1",), "error": None,
                                           "rows": [{"id": "kolibri-1", "object": "model"}, {"id": "qwen3.8-27b"}]}
        out = json.loads(self.mod.pair_models_body(b'{"object": "list", "data": [{"id": "qwen3.8-27b"}]}'))
        self.assertEqual([(r["id"], r["box"]) for r in out["data"]], [("qwen3.8-27b", "spark1"), ("kolibri-1", "spark2")])
        self.assertIsNone(self.mod.pair_models_body(b"not json"))
        self.assertIsNone(self.mod.pair_models_body(b'{"data": "x"}'))
        self.assertEqual(self.mod.pair_served_map(conf, None), {"spark1": "does not answer", "spark2": ["kolibri-1"]})
        self.mod._PAIR_MODELS["spark2"].update(models=(), error="timed out")
        self.assertEqual(self.mod.pair_served_map(conf, ("qwen3.8-27b",))["spark2"], "does not answer (timed out)")
        self.pair_file.unlink()
        self.mod._PAIR["seen"] = 0.0
        self.assertIsNone(self.mod.pair_models_body(b'{"data": []}'), "a box alone: its list as it came")


class TheLaneNamesAreThoseTheUnitsServe(unittest.TestCase):
    def test_lane_names_match_every_served_model_name(self):
        src = (REPO / "keepalive-proxy.py").read_text()
        block = re.search(r"LANE_NAMES = frozenset\(\{(.*?)\}\)", src, re.S).group(1)
        names = set(re.findall(r'"([^"]+)"', block))
        served = set()
        for t in REPO.glob("*.template"):
            served |= set(re.findall(r"--served-model-name[ =]([A-Za-z0-9._/-]+)", t.read_text()))
        self.assertEqual(names, served, "a lane's served name and the pair's list of lane names agree")


if __name__ == "__main__":
    unittest.main()
