#!/usr/bin/env python3
"""The cockpit's stop goes through the proxy (v6.47, 2026-10-25): no client takes a cut answer for a whole one.

SGLang ends an aborted generation as a finished one: stop_reason end_turn on /v1/messages (the
STOP_REASON_MAP of srt/entrypoints/anthropic/serving.py has no "abort") and a last chunk with
finish_reason "abort" on the OpenAI routes (serving_chat.py, for an abort that carries no status);
read in v0.5.22 and the flash nightly on 2026-10-25. The cockpit's memory floor asked SGLang
straight, so each client got the part generated before the stop as its whole answer: Claude Code
kept a compaction summary cut in the middle of a word on the reference box (2026-10-25 07:42). The
fake engine below ends an aborted generation exactly that way, so each test here fails on a proxy
that relays the end it is given. Through the proxy now, a stopped generation is stopped upstream
(by rid on SGLang, by closing its connection on vLLM) and its client is cut, with no end to its
stream and no last chunk; new generations wait while the box is short of memory; and an OpenAI
stream the engine ends as aborted is cut, whatever aborted it. Why a cut and a refusal, measured on
the real clients: the proxy's comment "The memory floor's stop".
"""
import http.client
import http.server
import importlib.util
import json
import os
import select
import socket
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve()
SPEC = importlib.util.spec_from_file_location("kproxy_floor_stop", HERE.parents[1] / "keepalive-proxy.py")
KEY = "floor-stop-test-key"
WORDS = 60
GAP = 0.05


def _keep_env(cls):
    """Put os.environ back as this class found it (tests/test_suite_isolation.py holds it)."""
    saved = dict(os.environ)
    cls.addClassCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))


class FakeEngine(http.server.BaseHTTPRequestHandler):
    """SGLang (or vLLM, owner "vllm") as far as a stopped generation goes: an abort ends the
    generation the way SGLang ends it, as a finished one, and a reader that goes away is what
    stops vLLM."""
    protocol_version = "HTTP/1.1"
    owner = "sglang"
    prefill = 0.0                # silence before the first byte (a stream's headers, a whole answer)
    self_abort_after = None      # OpenAI streams: the engine itself ends them as aborted after this many words
    lock = threading.Lock()
    live = {}                    # rid -> Event, set by an abort
    aborted = []                 # rids aborted by rid
    abort_all = []               # one entry per abort_all
    abort_all_at = []            # when each abort_all came (time.monotonic())
    rid_abort_delay = 0.0        # an engine the memory floor finds slow: seconds before an abort by rid answers
    abort_404_delay = 0.0        # vLLM under the same pressure: seconds before its 404 to /abort_request
    abort_all_fails = False      # an SGLang that answers abort_all with a 503
    split_lines = False          # Ollama streams: each line written in two pieces, a pause between them
    slow_body = 0.0              # Ollama, not streamed: the seconds between the two halves of its finished answer
    closed = []                  # rids whose reader went away mid-generation
    closed_at = {}               # rid -> when its reader went away during the prefill (time.monotonic())
    finished = []                # rids written to their end
    seen = []                    # (path, rid, stream)

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _head(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()

    def _chunk(self, data):
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _end(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/v1/models":
            return self._json(200, {"object": "list", "data": [{"id": "m", "object": "model", "owned_by": self.owner}]})
        if path in ("/server_info", "/get_server_info"):
            return self._json(200, {"max_total_num_tokens": 500000, "context_length": 262144})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path = self.path.split("?")[0]
        if path == "/abort_request":
            if self.owner == "vllm":
                time.sleep(self.abort_404_delay)
                return self._json(404, {"detail": "Not Found"})
            j = json.loads(body or b"{}")
            if j.get("abort_all") and self.abort_all_fails:
                return self._json(503, {"detail": "busy"})
            if not j.get("abort_all") and self.rid_abort_delay:
                time.sleep(self.rid_abort_delay)
            with self.lock:
                if j.get("abort_all"):
                    FakeEngine.abort_all.append(1)
                    FakeEngine.abort_all_at.append(time.monotonic())
                    for ev in FakeEngine.live.values():
                        ev.set()
                elif j.get("rid") in FakeEngine.live:
                    FakeEngine.aborted.append(j["rid"])
                    FakeEngine.live[j["rid"]].set()
            return self._json(200, {})
        if path == "/v1/messages/count_tokens":
            return self._json(200, {"input_tokens": 3})
        if path in ("/tokenize", "/v1/tokenize"):
            return self._json(200, {"count": 3, "tokens": [1, 2, 3], "max_model_len": 262144})
        req = json.loads(body or b"{}")
        rid = self.headers.get("x-override-rid") or uuid.uuid4().hex
        stream = bool(req.get("stream"))
        ev = threading.Event()
        with self.lock:
            FakeEngine.live[rid] = ev
            FakeEngine.seen.append((path, rid, stream))
        try:
            if self.prefill and self._prefill_until_closed(rid, ev):
                return
            if path == "/v1/messages":
                self._anthropic(rid, ev, stream)
            elif path in ("/api/chat", "/api/generate"):
                self._ollama(rid, ev, stream)
            else:
                self._openai(rid, ev, stream)
        except (BrokenPipeError, ConnectionResetError):
            with self.lock:
                FakeEngine.closed.append(rid)
        finally:
            with self.lock:
                FakeEngine.live.pop(rid, None)

    def _prefill_until_closed(self, rid, ev):
        """The prefill's silence, watching the reader: True when it went away meanwhile (vLLM stops a
        request whose connection closes), with when it did."""
        end = time.monotonic() + self.prefill
        while time.monotonic() < end and not ev.is_set():
            ready, _, _ = select.select([self.connection], [], [], 0.02)
            if ready:
                try:
                    gone = self.connection.recv(1, socket.MSG_PEEK) == b""
                except OSError:
                    gone = True
                if gone:
                    with self.lock:
                        FakeEngine.closed.append(rid)
                        FakeEngine.closed_at[rid] = time.monotonic()
                    return True
        return False

    def _words(self, ev, limit=WORDS):
        for i in range(limit):
            if ev.is_set():
                return
            yield f"w{i + 1:02d}"

    def _anthropic(self, rid, ev, stream):
        if not stream:
            sent = []
            for w in self._words(ev):
                sent.append(w)
                time.sleep(GAP)
            self._json(200, {"id": f"msg_{rid[:24]}", "type": "message", "role": "assistant", "model": "m",
                             "content": [{"type": "text", "text": " ".join(sent)}], "stop_reason": "end_turn",
                             "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": len(sent)}})
            return

        def ev_(kind, obj):
            self._chunk(f"event: {kind}\ndata: {json.dumps(obj)}\n\n".encode())
        self._head()
        ev_("message_start", {"type": "message_start", "message": {
            "id": f"msg_{rid[:24]}", "type": "message", "role": "assistant", "model": "m", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 0}}})
        ev_("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        n = 0
        for w in self._words(ev):
            ev_("content_block_delta", {"type": "content_block_delta", "index": 0,
                                        "delta": {"type": "text_delta", "text": w + " "}})
            n += 1
            time.sleep(GAP)
        # aborted or not, SGLang's end: STOP_REASON_MAP has no "abort", which falls back on end_turn
        ev_("content_block_stop", {"type": "content_block_stop", "index": 0})
        ev_("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                              "usage": {"output_tokens": n}})
        ev_("message_stop", {"type": "message_stop"})
        self._end()
        with self.lock:
            FakeEngine.finished.append(rid)

    def _openai(self, rid, ev, stream):
        def chunk(delta, finish=None):
            obj = {"id": rid, "object": "chat.completion.chunk", "created": 0, "model": "m",
                   "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}]}
            self._chunk(b"data: " + json.dumps(obj).encode() + b"\n\n")
        if not stream:
            sent = list(self._words(ev))
            raw = json.dumps({"id": rid, "object": "chat.completion", "model": "m", "choices": [{
                "index": 0, "message": {"role": "assistant", "content": " ".join(sent)},
                "finish_reason": "abort" if ev.is_set() else "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            half = len(raw) // 2 if self.slow_body else len(raw)
            self.wfile.write(raw[:half]); self.wfile.flush()
            time.sleep(self.slow_body)
            self.wfile.write(raw[half:]); self.wfile.flush()
            return
        self._head()
        chunk({"role": "assistant", "content": ""})
        limit = self.self_abort_after if self.self_abort_after is not None else WORDS
        for w in self._words(ev, limit):
            chunk({"content": w + " "})
            time.sleep(GAP)
        aborted = ev.is_set() or self.self_abort_after is not None
        chunk({}, "abort" if aborted else "stop")      # SGLang's last chunk for an abort without a status
        self._chunk(b"data: [DONE]\n\n")
        self._end()
        with self.lock:
            FakeEngine.finished.append(rid)

    def _ollama(self, rid, ev, stream):
        """SGLang's Ollama routes (python/sglang/srt/entrypoints/ollama/serving.py, v0.5.22): NDJSON, and a last line
        done_reason "stop" whatever ended the generation, an abort included."""
        if not stream:
            sent = list(self._words(ev))
            raw = json.dumps({"model": "m", "message": {"role": "assistant", "content": " ".join(sent)}, "done": True,
                              "done_reason": "stop"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            half = len(raw) // 2 if self.slow_body else len(raw)
            self.wfile.write(raw[:half]); self.wfile.flush()
            time.sleep(self.slow_body)
            self.wfile.write(raw[half:]); self.wfile.flush()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        for w in self._words(ev):
            line = json.dumps({"model": "m", "message": {"role": "assistant", "content": w + " "},
                               "done": False}).encode() + b"\n"
            if self.split_lines:        # its word in the first piece, the line's end in the second
                self._chunk(line[:-10])
                time.sleep(0.5)
                self._chunk(line[-10:])
            else:
                self._chunk(line)
            time.sleep(GAP)
        self._chunk(json.dumps({"model": "m", "message": {"role": "assistant", "content": ""}, "done": True,
                                "done_reason": "stop"}).encode() + b"\n")
        self._end()
        with self.lock:
            FakeEngine.finished.append(rid)


class FloorStop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _keep_env(cls)
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeEngine)
        cls.engine.daemon_threads = True
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        os.environ["UPSTREAM"] = f"http://127.0.0.1:{cls.engine.server_port}"
        os.environ["KEEPALIVE_S"] = "0.4"
        os.environ["CLIENT_IO_S"] = "5"
        os.environ.pop("PROXY_HOLD_UNITS", None)
        cls.mod = importlib.util.module_from_spec(SPEC)
        sys.argv = ["keepalive-proxy.py"]
        SPEC.loader.exec_module(cls.mod)
        cls.mod._api_key = lambda: KEY
        cls.proxy = cls.mod.Server(("127.0.0.1", 0), cls.mod.H)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.port = cls.proxy.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.engine.shutdown()

    def setUp(self):
        with FakeEngine.lock:
            for name in ("aborted", "abort_all", "abort_all_at", "closed", "finished", "seen"):
                getattr(FakeEngine, name).clear()
            FakeEngine.live.clear()
            FakeEngine.closed_at.clear()
        FakeEngine.owner, FakeEngine.prefill, FakeEngine.self_abort_after = "sglang", 0.0, None
        FakeEngine.split_lines, FakeEngine.slow_body = False, 0.0
        FakeEngine.rid_abort_delay = FakeEngine.abort_404_delay = 0.0
        FakeEngine.abort_all_fails = False
        self.addCleanup(self._join_stop_threads)
        mod = self.mod
        mod._rid_override_honoured = True
        mod._SERVED.update(names=(), engine="", ts=0.0)
        getattr(mod, "_STOP_WINDOWS", {}).clear()     # no stop under way left by the test before (v1.44.0)
        # The box's own memory never decides here: the floor is off unless a test sets it. Read with
        # getattr, so that on a proxy without the stop each test fails on what it checks, not here.
        if hasattr(mod, "_FLOOR"):
            mod._FLOOR.update(at=None, until=None, why="", floor_gib=0.0, episode=False)
            with mod._GEN_LOCK:
                mod._GENERATIONS.clear()
        for name in ("STOP_PEERS", "mem_available_gib", "FLOOR_EPISODE_S", "MEM_FLOOR_GIB", "FLOOR_HOLD_S"):
            if hasattr(mod, name):
                self.addCleanup(setattr, mod, name, getattr(mod, name))

    @staticmethod
    def _join_stop_threads():
        """A stop's aborts by rid run in threads of their own: none may reach the next test."""
        for th in threading.enumerate():
            if th.name == "stop-waiting":
                th.join(timeout=15)

    # ---- the client side, raw, to see every byte and how the connection ends ---------------
    def _post(self, path, body, key=KEY):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=15)
        raw = json.dumps(body).encode()
        head = (f"POST {path} HTTP/1.1\r\nHost: p\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(raw)}\r\n")
        if key:
            head += f"Authorization: Bearer {key}\r\n"
        s.sendall((head + "\r\n").encode() + raw)
        return s

    @staticmethod
    def _read(s, until=None, timeout=15):
        """The bytes until the proxy ends the connection, or until `until` is among them."""
        s.settimeout(timeout)
        got = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                c = s.recv(65536)
            except (socket.timeout, ConnectionResetError):
                break
            if not c:
                break
            got += c
            if until is not None and until in got:
                break
        return got

    def _stop(self, body=None, key=KEY, method="POST"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        raw = json.dumps({"why": "test floor"} if body is None else body).encode() if method == "POST" else None
        hdrs = {"Content-Type": "application/json"}
        if key:
            hdrs["Authorization"] = f"Bearer {key}"
        conn.request(method, "/proxy/stop", body=raw, headers=hdrs)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        try:
            return r.status, json.loads(data), r
        except ValueError:
            return r.status, None, r

    def _wait(self, pred, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            with FakeEngine.lock:
                if pred():
                    return True
            time.sleep(0.02)
        return False

    @staticmethod
    def _dechunk(raw):
        """The body of a chunked answer as read raw, its chunks joined, up to where it was cut."""
        rest, out = raw.split(b"\r\n\r\n", 1)[1], b""
        while b"\r\n" in rest:
            size, rest = rest.split(b"\r\n", 1)
            n = int(size, 16)
            if n == 0:
                break
            out, rest = out + rest[:n], rest[n + 2:]
        return out

    def _stream_then_stop(self, path, body, stop_body=None):
        s = self._post(path, {**body, "stream": True})
        head = self._read(s, until=b"w03")
        self.assertIn(b"w03", head, head[-300:])
        status, out, _ = self._stop(stop_body)
        rest = self._read(s)
        s.close()
        return head + rest, status, out

    # ---- the stop ---------------------------------------------------------------------------
    def test_a_stopped_anthropic_stream_is_cut_never_ended(self):
        got, status, out = self._stream_then_stop("/v1/messages", {"model": "m", "max_tokens": 100,
                                                                   "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["stopped"], 1)
        self.assertTrue(out["abort_all"])
        # no end of the message, no end_turn, no last chunk: the client reads an incomplete answer
        self.assertNotIn(b"message_stop", got)
        self.assertNotIn(b"end_turn", got)
        self.assertFalse(got.endswith(b"0\r\n\r\n"), got[-80:])
        self.assertNotIn(b"w%02d" % WORDS, got)
        # and the generation was stopped upstream, by its rid, before the close
        self.assertTrue(self._wait(lambda: FakeEngine.aborted))

    def test_a_stopped_openai_stream_is_cut_never_ended(self):
        got, status, out = self._stream_then_stop("/v1/chat/completions",
                                                  {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["stopped"], 1)
        self.assertNotIn(b'"abort"', got)
        self.assertNotIn(b"[DONE]", got)
        self.assertFalse(got.endswith(b"0\r\n\r\n"), got[-80:])
        self.assertTrue(self._wait(lambda: FakeEngine.aborted))

    def test_an_openai_stream_the_engine_aborts_is_cut(self):
        """Whatever aborted it (here the engine itself): finish_reason "abort" is not relayed."""
        FakeEngine.self_abort_after = 4
        s = self._post("/v1/chat/completions", {"model": "m", "stream": True,
                                                 "messages": [{"role": "user", "content": "hi"}]})
        got = self._read(s)
        s.close()
        self.assertIn(b"w04", got)
        self.assertNotIn(b'"abort"', got)
        self.assertNotIn(b"[DONE]", got)
        self.assertFalse(got.endswith(b"0\r\n\r\n"), got[-80:])

    def test_an_ollama_stream_arrives_as_it_comes(self):
        # the relay read every answer that is not SSE by 64 KiB, and an Ollama client got its NDJSON stream whole at its
        # end (the 4th review of v1.43.0, 2026-11-04)
        s = self._post("/api/chat", {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        t0 = time.monotonic()
        head = self._read(s, until=b"w02")
        took = time.monotonic() - t0
        rest = self._read(s)
        s.close()
        self.assertIn(b"w02", head)
        self.assertLess(took, WORDS * GAP / 2, f"its 2nd line came after {took:.2f} s of a {WORDS * GAP:.1f} s stream")
        self.assertIn(b'"done": true', head + rest)

    def test_a_stopped_ollama_stream_is_cut_never_ended(self):
        # v6.47 cut the SSE streams a stop ended. SGLang ends an aborted Ollama stream with a last line, done_reason
        # "stop", and the relay of what is not SSE gave it to its client as the end of a whole answer (the 4th review of
        # v1.43.0, 2026-11-04)
        got, status, out = self._stream_then_stop("/api/chat", {"model": "m",
                                                                "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["stopped"], 1)
        self.assertTrue(out["abort_all"])
        self.assertNotIn(b'"done": true', got)
        self.assertFalse(got.endswith(b"0\r\n\r\n"), got[-80:])
        self.assertNotIn(b"w%02d" % WORDS, got)

    def test_a_stopped_ollama_stream_never_leaves_half_a_line(self):
        # the relay cut a stopped Ollama stream where it was, half a line out; on a hop, the order line then joined that
        # half, and the other box read neither (the 5th review of v1.43.0, 2026-11-04)
        FakeEngine.split_lines = True
        got, status, out = self._stream_then_stop("/api/chat", {"model": "m",
                                                                "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(out["stopped"], 1)
        payload = self._dechunk(got)
        self.assertIn(b"w03", payload)
        self.assertTrue(payload.endswith(b"\n"), payload[-120:])
        for line in payload.splitlines():
            json.loads(line)

    def test_a_finished_answer_that_is_not_streamed_goes_whole_through_a_stop(self):
        # every answer a stop found under way was cut, one the engine had finished too, and not streamed: its client
        # asked it again of a box short of memory, and the other box of a pair bound it (the 5th review of v1.43.0)
        FakeEngine.slow_body = 1.0
        s = self._post("/api/chat", {"model": "m", "stream": False, "messages": [{"role": "user", "content": "hi"}]})
        head = self._read(s, until=b'"model"')
        self.assertIn(b'"model"', head)
        status, out, _ = self._stop()
        self.assertEqual((status, out["stopped"]), (200, 0), out)              # an answer relayed whole: no target
        rest = self._read(s)
        s.close()
        got = head + rest
        self.assertTrue(got.endswith(b"0\r\n\r\n"), got[-80:])
        self.assertIs(json.loads(self._dechunk(got))["done"], True)

    def test_a_finished_answer_is_no_stop_target_on_vllm_and_holds_nothing_back(self):
        # stopped, vLLM's answer being relayed had its upstream shut: cut, taken for its engine's death (bound 10 minutes
        # on a hop), and a floor stop that cut nothing held new generations back (the 6th review of v1.43.0, 2026-11-04)
        FakeEngine.owner, FakeEngine.slow_body = "vllm", 1.0
        s = self._post("/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        head = self._read(s, until=b'"object"')
        self.assertIn(b'"object"', head)
        status, out, _ = self._stop()
        self.assertEqual((status, out["stopped"]), (200, 0), out)
        self.assertEqual(out.get("finishing"), 1, "the answer left to go out was not said")   # the 7th review
        rest = self._read(s)
        s.close()
        got = head + rest
        self.assertTrue(got.endswith(b"0\r\n\r\n"), got[-80:])
        self.assertEqual(json.loads(self._dechunk(got))["object"], "chat.completion")
        self.assertFalse(self.mod._FLOOR["episode"], "a stop that cut nothing held new generations back")

    def test_streams_that_finish_end_as_before(self):
        for path in ("/v1/messages", "/v1/chat/completions"):
            with self.subTest(path=path):
                s = self._post(path, {"model": "m", "max_tokens": 100, "stream": True,
                                      "messages": [{"role": "user", "content": "hi"}]})
                got = self._read(s)
                s.close()
                self.assertIn(b"w%02d" % WORDS, got)
                self.assertTrue(got.endswith(b"0\r\n\r\n"), got[-80:])
                self.assertIn(b"message_stop" if path == "/v1/messages" else b"[DONE]", got)
                self.assertNotIn(b'"abort"', got)

    def test_on_vllm_the_stop_closes_the_generation(self):
        """vLLM has no abort route: closing its connection is the abort (measured 2026-10-14)."""
        FakeEngine.owner = "vllm"
        got, status, out = self._stream_then_stop("/v1/chat/completions",
                                                  {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200, out)
        self.assertEqual((out["stopped"], out["abort_all"]), (1, False))
        self.assertNotIn(b"[DONE]", got)
        self.assertFalse(got.endswith(b"0\r\n\r\n"), got[-80:])
        self.assertTrue(self._wait(lambda: FakeEngine.closed), "the engine kept writing for nobody")
        self.assertEqual((FakeEngine.aborted, FakeEngine.abort_all), ([], []))

    def test_a_stream_stopped_before_the_engine_answers_is_cut(self):
        """Queued or prefilling: the proxy has opened the stream with its keepalives (v6.31)."""
        FakeEngine.prefill = 4.0
        s = self._post("/v1/messages", {"model": "m", "max_tokens": 100, "stream": True,
                                        "messages": [{"role": "user", "content": "hi"}]})
        head = self._read(s, until=b"ping")
        self.assertIn(b"ping", head)
        status, out, _ = self._stop()
        got = head + self._read(s)
        s.close()
        self.assertEqual((status, out["stopped"]), (200, 1))
        self.assertNotIn(b"message_stop", got)
        self.assertNotIn(b"w01", got)
        self.assertFalse(got.endswith(b"0\r\n\r\n"), got[-80:])
        self.assertTrue(self._wait(lambda: FakeEngine.aborted or FakeEngine.abort_all))

    def test_a_non_streamed_answer_stopped_while_it_waits_never_arrives(self):
        FakeEngine.prefill = 3.0
        s = self._post("/v1/messages", {"model": "m", "max_tokens": 100,
                                        "messages": [{"role": "user", "content": "hi"}]})
        self.assertTrue(self._wait(lambda: FakeEngine.seen))
        time.sleep(0.3)
        status, out, _ = self._stop()
        got = self._read(s)
        s.close()
        self.assertEqual((status, out["stopped"]), (200, 1))
        self.assertNotIn(b"end_turn", got)
        self.assertNotIn(b"HTTP/1.1 200", got)

    # ---- what waits after a stop, and while memory is short ----------------------------------
    def test_after_the_floors_stop_new_generations_wait_then_go(self):
        # the wait follows a real cut (revue 627, 26/10): a stop that finds nothing to cut holds
        # nothing back, so this generation is streaming when the floor's stop comes
        got, status, out = self._stream_then_stop(
            "/v1/messages", {"model": "m", "max_tokens": 100,
                             "messages": [{"role": "user", "content": "hi"}]},
            {"why": "test floor", "hold_s": 1.5})
        self.assertEqual((status, out["stopped"]), (200, 1))
        with FakeEngine.lock:
            FakeEngine.seen.clear()
        for path, code in (("/v1/messages", 529), ("/v1/chat/completions", 503)):
            with self.subTest(path=path):
                s = self._post(path, {"model": "m", "max_tokens": 10, "stream": True,
                                      "messages": [{"role": "user", "content": "hi"}]})
                got = self._read(s)
                s.close()
                head, _, body = got.partition(b"\r\n\r\n")
                self.assertTrue(head.startswith(f"HTTP/1.1 {code}".encode()), head[:60])
                self.assertIn(b"Retry-After:", head)
                err = json.loads(body)["error"]
                self.assertEqual(err["type"], "overloaded_error")
                self.assertIn("short of memory", err["message"])
        # counting is no generation: it goes through
        s = self._post("/v1/messages/count_tokens", {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        self.assertIn(b"input_tokens", self._read(s))
        s.close()
        self.assertEqual([p for p, _, _ in FakeEngine.seen], [])     # nothing reached the engine
        time.sleep(1.6)
        s = self._post("/v1/chat/completions", {"model": "m", "stream": True,
                                                 "messages": [{"role": "user", "content": "hi"}]})
        got = self._read(s)
        s.close()
        self.assertIn(b"[DONE]", got)

    def _message(self):
        s = self._post("/v1/messages", {"model": "m", "max_tokens": 10, "stream": True,
                                        "messages": [{"role": "user", "content": "hi"}]})
        got = self._read(s)
        s.close()
        return got

    def test_memory_alone_refuses_nothing(self):
        # an idle box short of memory for other programs (a VS Code server and a browser beside the
        # flash, 3.0 GiB on 2026-10-25) had every request refused, where v1.40.2 served it and the
        # cockpit's floor acts only while generations run (code review, 2026-10-26)
        self.mod._FLOOR["floor_gib"] = 3.0
        self.mod.mem_available_gib = lambda path="/proc/meminfo": 1.2
        got = self._message()
        self.assertIn(b"message_stop", got)
        self.assertNotIn(b"memory floor", got)

    def test_after_a_floor_stop_memory_under_the_floor_refuses_until_it_is_back(self):
        mod = self.mod
        _, status, out = self._stream_then_stop(
            "/v1/messages", {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
            {"why": "test floor", "hold_s": 0, "floor_gib": 3.0})
        self.assertEqual((status, out["stopped"]), (200, 1))
        mod.mem_available_gib = lambda path="/proc/meminfo": 1.2
        got = self._message()
        self.assertTrue(got.startswith(b"HTTP/1.1 529"), got[:60])
        self.assertIn(b"MemAvailable is 1.2 GiB, under the 3 GiB memory floor", got)
        mod.mem_available_gib = lambda path="/proc/meminfo": 9.0
        self.assertIn(b"message_stop", self._message())
        # memory back: the floor's episode is over, a later dip refuses nothing until the next stop
        mod.mem_available_gib = lambda path="/proc/meminfo": 1.2
        self.assertIn(b"message_stop", self._message())

    def test_a_floor_the_box_never_gets_back_over_ends_with_its_episode(self):
        # a stop's floor_gib stayed until the proxy restarted: {"floor_gib": 99} that cut a generation
        # answered 529 to every request on a box with 25 GiB free, a day later (code review, 2026-10-26)
        mod = self.mod
        mod.FLOOR_EPISODE_S = 1.0
        mod.mem_available_gib = lambda path="/proc/meminfo": 25.0
        _, status, out = self._stream_then_stop(
            "/v1/messages", {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
            {"why": "test floor", "hold_s": 0, "floor_gib": 99})
        self.assertEqual((status, out["stopped"]), (200, 1))
        got = self._message()
        self.assertTrue(got.startswith(b"HTTP/1.1 529"), got[:60])
        self.assertIn(b"under the 99 GiB memory floor", got)
        time.sleep(1.1)
        self.assertIn(b"message_stop", self._message())

    def test_a_proxy_floor_of_0_keeps_the_refusal_for_memory_off(self):
        mod = self.mod
        mod.MEM_FLOOR_GIB = 0.0
        mod.mem_available_gib = lambda path="/proc/meminfo": 1.2
        _, status, out = self._stream_then_stop(
            "/v1/messages", {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
            {"why": "test floor", "hold_s": 0, "floor_gib": 3.0})
        self.assertEqual((status, out["stopped"]), (200, 1))
        self.assertIn(b"message_stop", self._message())

    def test_abort_all_goes_whatever_lane_the_proxy_last_saw(self):
        # a switch from a vLLM lane keeps _SERVED saying vllm until a request goes through: the floor's
        # stop sent no abort_all, and a generation sent to SGLang straight (bench.sh, needle.sh, an
        # eval) went on to the livelock (code review, 2026-10-26)
        self.mod._SERVED.update(names=("m",), engine="vllm", ts=time.monotonic())
        status, out, _ = self._stop({"why": "test floor", "hold_s": 0})
        self.assertEqual((status, out["stopped"]), (200, 0))
        self.assertTrue(out["abort_all"])
        self.assertEqual(len(FakeEngine.abort_all), 1)
        # vLLM has no such route: its 404 says so, and nothing else is claimed
        FakeEngine.owner = "vllm"
        status, out, _ = self._stop({"why": "test floor", "hold_s": 0})
        self.assertEqual((status, out["abort_all"]), (200, False))

    def test_an_operators_stop_holds_nothing_back(self):
        got, status, out = self._stream_then_stop("/v1/messages", {"model": "m", "max_tokens": 100,
                                                                   "messages": [{"role": "user", "content": "hi"}]},
                                                  {"kind": "operator"})
        self.assertEqual((status, out["stopped"]), (200, 1))
        self.assertNotIn(b"message_stop", got)
        s = self._post("/v1/messages", {"model": "m", "max_tokens": 10, "stream": True,
                                        "messages": [{"role": "user", "content": "hi"}]})
        got = self._read(s)
        s.close()
        self.assertIn(b"message_stop", got)

    def test_a_generation_that_goes_out_while_the_cockpits_abort_all_may_come_is_cut(self):
        # SGLang refused the proxy's abort_all: the cockpit sends its own within 8 s, which ends every generation SGLang
        # runs, and SGLang ends an aborted Anthropic stream as end_turn. One that goes out then is that stop's, cut, never
        # ended as whole (the 15th review of v1.43.0); read as it goes out, after the size guard, since the review of
        # v1.44.0
        FakeEngine.abort_all_fails = True
        status, out, _ = self._stop({"why": "test operator", "kind": "operator"})
        self.assertEqual((status, out["stopped"], out["abort_all"]), (200, 0, False), out)
        FakeEngine.seen.clear()
        s = self._post("/v1/messages", {"model": "m", "max_tokens": 10, "stream": True,
                                        "messages": [{"role": "user", "content": "hi"}]})
        got = self._read(s)
        s.close()
        self.assertNotIn(b"message_stop", got, "a generation that went out in a stop's window was ended as whole")
        self.assertEqual([p for p, _, _ in FakeEngine.seen if p == "/v1/messages"], [],
                         "a generation that went out in a stop's window reached the engine")

    def test_a_stop_that_cut_nothing_holds_nothing_back(self):
        # the wait belongs to a cut (revue 627, 26/10): generations running outside this proxy
        # make a floor stop find nothing, and that was enough to blackout every new generation
        # for the hold's 60 s on an idle box
        status, out, _ = self._stop({"why": "test floor", "hold_s": 60})
        self.assertEqual((status, out["stopped"]), (200, 0))
        self.assertIsNone(self.mod._FLOOR["until"])
        s = self._post("/v1/messages", {"model": "m", "max_tokens": 10, "stream": True,
                                        "messages": [{"role": "user", "content": "hi"}]})
        got = self._read(s)
        s.close()
        self.assertIn(b"message_stop", got)
        self.assertNotIn(b"overloaded_error", got)

    def test_a_stop_cannot_blackout_the_proxy_with_a_silly_number(self):
        # Infinity in a stop body used to end the box for the session (revue 627, 26/10):
        # _FLOOR["until"]=inf makes floor_refusal raise OverflowError in every new generation
        # and _handle has no except, so the connection dies with no status at all. It takes the
        # engine key, so no client can send it, but a cockpit bug must not blind every lane.
        def disarm():   # the sane 60 s hold one stop armed must not block the next stop's stream
            self.mod._FLOOR.update(at=None, until=None, why="", floor_gib=0.0)
        for bad in (float("inf"), float("nan")):
            with self.subTest(bad=bad):
                disarm()
                _, status, out = self._stream_then_stop(
                    "/v1/messages", {"model": "m", "max_tokens": 100,
                                     "messages": [{"role": "user", "content": "hi"}]},
                    {"why": "test floor", "hold_s": bad})
                self.assertEqual((status, out["stopped"]), (200, 1))
                s = self._post("/v1/messages", {"model": "m", "max_tokens": 10, "stream": True,
                                                "messages": [{"role": "user", "content": "hi"}]})
                got = self._read(s)
                s.close()
                head = got.partition(b"\r\n\r\n")[0]
                self.assertTrue(head.startswith(b"HTTP/1.1 529"), got[:80])
                ra = [l for l in head.split(b"\r\n") if l.lower().startswith(b"retry-after")][0]
                self.assertLessEqual(int(ra.split(b":")[1]), 60)    # the default hold, not forever
        # a finite silly value is capped, not believed: the box has 128 GiB and a stop is no siege
        disarm()
        _, status, out = self._stream_then_stop(
            "/v1/messages", {"model": "m", "max_tokens": 100,
                             "messages": [{"role": "user", "content": "hi"}]},
            {"why": "test floor", "hold_s": 1e9, "floor_gib": 1e9})
        self.assertEqual(status, 200)
        self.assertLessEqual(self.mod._FLOOR["floor_gib"], 100.0)
        s = self._post("/v1/messages", {"model": "m", "max_tokens": 10, "stream": True,
                                        "messages": [{"role": "user", "content": "hi"}]})
        got = self._read(s)
        s.close()
        head = got.partition(b"\r\n\r\n")[0]
        self.assertTrue(head.startswith(b"HTTP/1.1 529"), got[:80])
        ra = [l for l in head.split(b"\r\n") if l.lower().startswith(b"retry-after")][0]
        self.assertLessEqual(int(ra.split(b":")[1]), 3600)

    def test_abort_all_goes_first_when_the_engine_is_slow_to_abort_by_rid(self):
        # at the memory floor the engine is slow: each abort by rid can wait ABORT_TIMEOUT_S, and abort_all
        # went only after all of them, up to the cockpit's 8 s fallback late (verification, 2026-10-26)
        FakeEngine.prefill, FakeEngine.rid_abort_delay = 8.0, 1.5
        socks = [self._post("/v1/messages", {"model": "m", "max_tokens": 10, "stream": True,
                                             "messages": [{"role": "user", "content": f"hi {i}"}]})
                 for i in range(2)]
        self.assertTrue(self._wait(lambda: len(FakeEngine.live) == 2))
        t0 = time.monotonic()
        status, out, _ = self._stop({"why": "test floor", "hold_s": 0})
        self.assertEqual((status, out["stopped"]), (200, 2), out)
        self.assertTrue(out["abort_all"])
        with FakeEngine.lock:
            first = FakeEngine.abort_all_at[0]
        self.assertLess(first - t0, 1.0, "abort_all waited for the aborts by rid")
        for s in socks:
            got = self._read(s, timeout=10)
            s.close()
            self.assertNotIn(b"message_stop", got)       # marked before the engine heard: cut, not ended

    def test_a_number_too_big_for_a_float_reads_as_not_given(self):
        # float() of a 310-digit integer raised OverflowError before the finite check, and the stop's
        # handler died with no answer (verification, 2026-10-26). Beside a generation, so that the stop
        # writes the floor and its pause: the defaults, never the ceilings (code review, 2026-10-26).
        t0 = time.monotonic()
        _, status, out = self._stream_then_stop(
            "/v1/messages", {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
            {"why": "test floor", "hold_s": 10 ** 400, "floor_gib": 10 ** 400})
        self.assertEqual((status, out["stopped"]), (200, 1), out)
        self.assertEqual(self.mod._FLOOR["floor_gib"], self.mod.MEM_FLOOR_GIB)
        self.assertLess(self.mod._FLOOR["until"] - t0, self.mod.FLOOR_HOLD_S + 15)

    def test_on_vllm_a_waiting_request_closes_before_abort_alls_answer(self):
        # closing is vLLM's abort, with no round trip: a request still in its prefill waited for
        # abort_all's 404 first, 2 s here, up to ABORT_TIMEOUT_S under memory pressure (code review,
        # 2026-10-26)
        FakeEngine.owner, FakeEngine.prefill, FakeEngine.abort_404_delay = "vllm", 8.0, 2.0
        s = self._post("/v1/messages", {"model": "m", "max_tokens": 10, "stream": False,
                                        "messages": [{"role": "user", "content": "hi"}]})
        self.assertTrue(self._wait(lambda: len(FakeEngine.live) == 1))
        rid = next(iter(FakeEngine.live))
        t0 = time.monotonic()
        status, out, _ = self._stop({"why": "test floor", "hold_s": 0})
        self.assertEqual((status, out["stopped"], out["abort_all"]), (200, 1, False), out)
        self.assertTrue(self._wait(lambda: rid in FakeEngine.closed_at, timeout=5))
        self.assertLess(FakeEngine.closed_at[rid] - t0, 1.0, "the prefill waited for abort_all's 404")
        s.close()

    def test_the_cockpit_has_its_answer_before_the_aborts_by_rid(self):
        # each abort by rid of a request still waiting can take ABORT_TIMEOUT_S on a slow engine, and the
        # answer waited for all of them: past the cockpit's 8 s, it took the stop for one the proxy had not
        # made, and the last request kept its prefill as long (code reviews, 2026-10-26). They go when
        # abort_all did not, each in its own thread.
        FakeEngine.prefill, FakeEngine.rid_abort_delay, FakeEngine.abort_all_fails = 8.0, 3.0, True
        socks = [self._post("/v1/messages", {"model": "m", "max_tokens": 10, "stream": True,
                                             "messages": [{"role": "user", "content": f"hi {i}"}]})
                 for i in range(2)]
        self.assertTrue(self._wait(lambda: len(FakeEngine.live) == 2))
        t0 = time.monotonic()
        status, out, _ = self._stop({"why": "test floor", "hold_s": 0})
        took = time.monotonic() - t0
        self.assertEqual((status, out["stopped"], out["abort_all"]), (200, 2, False), out)
        self.assertLess(took, 2.0, "the answer waited for the aborts by rid")
        self.assertTrue(self._wait(lambda: len(FakeEngine.aborted) == 2, timeout=5.0))
        self.assertLess(time.monotonic() - t0, 4.5, "the aborts by rid went one after the other")
        for s in socks:
            got = self._read(s, timeout=15)
            s.close()
            self.assertNotIn(b"message_stop", got)

    def _slow_routing(self, seconds):
        """The request's way to the engine takes SECONDS before its connection opens: a stop that lands
        then finds no socket to close (code review, 2026-10-26)."""
        H, orig = self.mod.H, self.mod.H._open

        def slow_open(handler, body):
            time.sleep(seconds)
            return orig(handler, body)
        H._open = slow_open
        self.addCleanup(setattr, H, "_open", orig)

    def test_a_vllm_request_stopped_while_routed_is_closed_once_it_opens(self):
        # the close came before the connection and found nothing, and the request then generated whole,
        # its client cut only at the end (code review, 2026-10-26)
        FakeEngine.owner, FakeEngine.prefill = "vllm", 6.0
        self._slow_routing(0.8)
        s = self._post("/v1/messages", {"model": "m", "max_tokens": 10, "stream": False,
                                        "messages": [{"role": "user", "content": "hi"}]})
        self.assertTrue(self._wait(lambda: len(self.mod._GENERATIONS) == 1, timeout=5))
        t0 = time.monotonic()
        status, out, _ = self._stop({"why": "test floor", "hold_s": 0})
        self.assertEqual((status, out["stopped"]), (200, 1), out)
        self.assertTrue(self._wait(lambda: len(FakeEngine.closed_at) == 1, timeout=5))
        self.assertLess(next(iter(FakeEngine.closed_at.values())) - t0, 2.5, "the engine generated for nobody")
        got = self._read(s, timeout=10)
        s.close()
        self.assertNotIn(b"end_turn", got)

    def test_an_sglang_request_stopped_while_routed_is_ended_once_it_opens(self):
        # abort_all went before it reached the engine, and its abort by rid too, which stopped nothing and
        # marked it aborted: its prefill ran whole, and its relay skipped the abort it sends at the end
        # (code review, 2026-10-26)
        FakeEngine.prefill = 6.0
        self._slow_routing(0.8)
        s = self._post("/v1/messages", {"model": "m", "max_tokens": 10, "stream": True,
                                        "messages": [{"role": "user", "content": "hi"}]})
        self.assertTrue(self._wait(lambda: len(self.mod._GENERATIONS) == 1, timeout=5))
        t0 = time.monotonic()
        status, out, _ = self._stop({"why": "test floor", "hold_s": 0})
        self.assertEqual((status, out["stopped"], out["abort_all"]), (200, 1, True), out)
        self.assertTrue(self._wait(lambda: len(FakeEngine.closed_at) == 1, timeout=5))
        self.assertLess(next(iter(FakeEngine.closed_at.values())) - t0, 2.5, "the prefill ran whole")
        got = self._read(s, timeout=10)
        s.close()
        self.assertNotIn(b"message_stop", got)

    def test_a_stop_meanwhile_keeps_its_episode(self):
        # floor_refusal read the floor, read MemAvailable, then ended the episode without the lock: a
        # stop that started another in between lost it, and after its pause a generation went through
        # under the floor (code review, 2026-10-26)
        mod = self.mod
        now = time.monotonic()
        mod._FLOOR.update(at=now - 10, until=None, why="before", floor_gib=3.0, episode=True)

        def meanwhile(path="/proc/meminfo"):
            mod._FLOOR.update(at=time.monotonic(), until=None, why="a new stop", episode=True)
            return 3.4
        mod.mem_available_gib = meanwhile
        self.assertIsNone(mod.floor_refusal())
        mod.mem_available_gib = lambda path="/proc/meminfo": 1.2
        got = mod.floor_refusal()
        self.assertIsNotNone(got, "the new stop's episode was lost")
        self.assertIn("under the 3 GiB memory floor", got[0])

    def test_a_proxy_pause_of_0_keeps_the_pause_off(self):
        # PROXY_FLOOR_HOLD_S=0 said "off" and the cockpit's hold_s of 60 won anyway, where
        # PROXY_MEM_FLOOR_GIB=0 wins over its floor_gib (code review, 2026-10-26)
        self.mod.FLOOR_HOLD_S, self.mod.MEM_FLOOR_GIB = 0.0, 0.0
        _, status, out = self._stream_then_stop(
            "/v1/messages", {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
            {"why": "test floor", "hold_s": 60, "floor_gib": 3.0})
        self.assertEqual((status, out["stopped"]), (200, 1))
        self.assertIn(b"message_stop", self._message())

    def test_a_chunked_stop_is_refused_never_read_as_the_floors(self):
        # a chunked body has no Content-Length: it was never read, and an operator's stop read as the
        # floor's, which holds new generations back (verification, 2026-10-26)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=15)
        raw = json.dumps({"kind": "operator", "why": "test"}).encode()
        s.sendall((f"POST /proxy/stop HTTP/1.1\r\nHost: p\r\nContent-Type: application/json\r\n"
                   f"Authorization: Bearer {KEY}\r\nTransfer-Encoding: chunked\r\n\r\n").encode()
                  + f"{len(raw):x}\r\n".encode() + raw + b"\r\n0\r\n\r\n")
        got = self._read(s)
        s.close()
        self.assertTrue(got.startswith(b"HTTP/1.1 411"), got[:120])
        self.assertIsNone(self.mod._FLOOR["until"])

    # ---- whose route it is ------------------------------------------------------------------
    def test_the_stop_is_the_operators_alone(self):
        # a generation runs through every refused stop below, and ends whole
        s = self._post("/v1/messages", {"model": "m", "max_tokens": 100, "stream": True,
                                        "messages": [{"role": "user", "content": "hi"}]})
        self.assertIn(b"w03", self._read(s, until=b"w03"))
        self.assertEqual(self._stop(key=None)[0], 401)
        self.assertEqual(self._stop(key="a client key")[0], 401)
        self.assertEqual(self._stop(method="GET")[0], 405)
        self.assertEqual(self._stop("x" * 5000)[0], 413)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("POST", "/proxy/stop", body=b"[1, 2]", headers={"Content-Type": "application/json",
                                                                    "Authorization": f"Bearer {KEY}"})
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()
        self.mod.STOP_PEERS = ()          # as if it came from the network
        self.assertEqual(self._stop()[0], 403)
        # none of these stopped anything nor held anything back
        got = self._read(s)
        s.close()
        self.assertIn(b"message_stop", got)
        self.assertEqual((FakeEngine.abort_all, FakeEngine.aborted), ([], []))
        self.assertIsNone(getattr(self.mod, "_FLOOR", {}).get("at", "no stop in this proxy"))


if __name__ == "__main__":
    unittest.main()
