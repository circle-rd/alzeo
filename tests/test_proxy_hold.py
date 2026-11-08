#!/usr/bin/env python3
"""A request is held while its engine comes back (keepalive-proxy v6.28, issue #26).

systemd starts a crashed engine again 15 s after it stops and the boot takes 7 to 11 min.
Every request of that window got a 503 at once, and opencode 1.18.32 retried a 503 six
times on its Retry-After, then failed the session after 151 s (measured against a fake
engine, 2026-10-01). The proxy now holds a request that finds the engine not answering
while a unit named in PROXY_HOLD_UNITS is on its way back: a streamed one gets its 200 and
the keepalives at once (opencode and Claude Code both waited 400 s on that, then took the
answer), a non-streamed one waits in silence, and a hold that fails ends a stream with an
error event, which both clients answer by sending the request again.

Each test runs the real proxy against an engine this file starts and stops on one port,
and a systemctl that reads the units' state from a file."""
import http.client
import http.server
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localports  # noqa: E402

HERE = Path(__file__).resolve()
PROXY = HERE.parents[1] / "keepalive-proxy.py"
UNITS = "qwen38-sglang.service qwen38-flash.service"


class Engine(http.server.BaseHTTPRequestHandler):
    """SGLang as the proxy sees it: its info route, a generation in both dialects, /tokenize.
    The server's `mode` is "up" or "starting" (503, as SGLang answers while it loads) for
    its generations, "vllm-dead" (a stream whose EngineCore died: vLLM's error event, then its
    clean end), or "slow400" (a 400 whose body comes 2 s after its headers, /abort_request a 404),
    `info_mode` the same for its info route; `pool` is what that route reports;
    `generations` lists what it was asked to generate."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def _empty(self, code):
        self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self):
        if self.path in ("/server_info", "/get_server_info"):
            if self.server.info_mode == "starting":
                return self._empty(503)
            return self._json(200, {"max_total_num_tokens": self.server.pool})
        if self.server.mode == "starting":
            return self._empty(503)
        self._json(200, {"data": [{"id": "m"}]})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.server.mode == "starting":
            return self._empty(503)
        if self.server.mode == "slow400":
            if self.path == "/abort_request":
                return self._empty(404)
            out = json.dumps({"object": "error", "message": "bad request", "type": "BadRequestError",
                              "code": 400}).encode()
            self.send_response(400); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.flush()
            time.sleep(2.0)                 # its headers out, its body late: the error's relay under way
            self.wfile.write(out); self.wfile.flush()
            return
        time.sleep(getattr(self.server, "delay", 0))
        if self.path == "/tokenize":
            return self._json(200, {"count": len(body) // 4})
        self.server.generations.append(self.path)
        req = json.loads(body or b"{}")
        if self.server.mode == "plain400":
            out = b"bad request, said in plain text"
            self.send_response(400); self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out); return
        if self.path == "/api/chat" and req.get("stream"):         # SGLang's Ollama route: NDJSON
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
            for ln in (b'{"model": "m", "message": {"role": "assistant", "content": "four"}, "done": false}\n',
                       b'{"model": "m", "message": {"role": "assistant", "content": ""}, "done": true}\n'):
                self.wfile.write(b"%x\r\n" % len(ln) + ln + b"\r\n"); self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
            return
        if not req.get("stream") or self.server.mode == "json-for-stream":
            return self._json(200, {"id": "x", "object": "chat.completion", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "four"}, "finish_reason": "stop"}]})
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
        def c(b):
            self.wfile.write(b"%x\r\n" % len(b) + b + b"\r\n"); self.wfile.flush()
        if self.server.mode == "vllm-dead":
            c(b'data: {"id":"g1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"fo"}}]}\n\n')
            c(b'data: {"error": {"message": "EngineCore encountered an issue. See stack trace (above) for the root '
              b'cause.", "type": "InternalServerError", "param": null, "code": 500}}\n\n')
            c(b"data: [DONE]\n\n")
        elif self.path.startswith("/v1/messages"):
            for name, data in (("message_start", {"type": "message_start", "message": {"id": "m1", "type": "message",
                                "role": "assistant", "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}}),
                               ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                "delta": {"type": "text_delta", "text": "four"}}),
                               ("message_stop", {"type": "message_stop"})):
                c(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        else:
            c(b'data: {"id":"g1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"four"},"finish_reason":null}]}\n\n')
            c(b'data: {"id":"g1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n')
            c(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()


class Box:
    """One engine port, one proxy, one systemctl state file. The proxy runs as its own
    process, or (inproc) in this one, where the coverage gate of CI can see its lines."""

    def __init__(self, test, units=UNITS, hold_max="30", state="activating", inproc=False, keepalive="1"):
        self.test = test
        self.t = Path(tempfile.mkdtemp(prefix="proxy-hold-"))
        test.addCleanup(shutil.rmtree, self.t, True)
        (self.t / "home/.config/qwen38").mkdir(parents=True)
        (self.t / "home/.config/qwen38/api-key").write_text("test-key\n")
        (self.t / "bin").mkdir()
        self.state_file, self.jobs_file = self.t / "state", self.t / "jobs"
        self.state(state)
        # systemctl show A B -p ActiveState --value prints one state per unit, and -p
        # InvocationID one id per unit (v6.35), the same ids all along: no unit starts again
        # in these tests; list-jobs prints the jobs file (none by default); is-active (the
        # image and video lanes' question) says inactive
        (self.t / "bin/systemctl").write_text(
            "#!/bin/sh\n"
            f'case "$*" in *InvocationID*) for u in {units}; do echo "inv-$u"; echo; done; exit 0;; esac\n'
            f'if [ "$1" = show ]; then for u in {units}; do cat "{self.state_file}"; done; exit 0; fi\n'
            f'if [ "$1" = list-jobs ]; then cat "{self.jobs_file}" 2>/dev/null; exit 0; fi\n'
            "exit 3\n")
        (self.t / "bin/systemctl").chmod(0o755)
        # the engine's port is gone until start_engine, and the test's all along: no one else can take it
        self.engine_port = localports.held(test)
        self.proc = None
        self.srv = None
        self.generations = []
        env = dict(os.environ, UPSTREAM=f"http://127.0.0.1:{self.engine_port}", HOME=str(self.t / "home"),
                   PATH=f"{self.t / 'bin'}:{os.environ.get('PATH', '')}", KEEPALIVE_S=keepalive,
                   PROXY_HOLD_MAX_S=hold_max, PROXY_HOLD_POLL_S="0.2")
        env.pop("PROXY_HOLD_UNITS", None)
        if units:
            env["PROXY_HOLD_UNITS"] = units
        self.inproc, self.lines = inproc, []
        test.addCleanup(self.close)
        if inproc:
            saved_env, saved_argv = dict(os.environ), list(sys.argv)
            test.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved_env)))
            test.addCleanup(setattr, sys, "argv", saved_argv)
            os.environ.clear(); os.environ.update(env)
            spec = importlib.util.spec_from_file_location(f"kp_hold_{time.time_ns()}", PROXY)
            self.mod = importlib.util.module_from_spec(spec)
            sys.argv = ["keepalive-proxy.py"]
            spec.loader.exec_module(self.mod)
            self.mod.log = lambda msg: self.lines.append(f"[proxy] {msg}")
            self.proxy = self.mod.Server(("127.0.0.1", 0), self.mod.H)
            threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
            self.port = self.proxy.server_address[1]
            return
        self.log = open(self.t / "proxy.log", "w")
        self.proc, self.port = localports.spawn(lambda port: subprocess.Popen(
            [sys.executable, str(PROXY), str(port)], env=env, stdout=subprocess.DEVNULL, stderr=self.log))

    def state(self, s):
        self.state_file.write_text(s + "\n")

    def jobs(self, *lines):
        """What `systemctl list-jobs --no-legend` prints: JOB UNIT TYPE STATE."""
        self.jobs_file.write_text("".join(ln + "\n" for ln in lines))

    def start_engine(self, mode="up", pool=200000, info_mode=None, delay=0.0):
        if self.srv is None:
            self.srv = localports.serve(self.engine_port, http.server.ThreadingHTTPServer, Engine)
            self.srv.generations = self.generations
            threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.srv.mode, self.srv.pool, self.srv.info_mode = mode, pool, info_mode or mode
        self.srv.delay = delay              # seconds before each generation is answered

    def stop_engine(self):
        if self.srv is not None:
            self.srv.shutdown(); self.srv.server_close(); self.srv = None

    def close(self):
        if self.inproc:
            self.proxy.shutdown(); self.proxy.server_close()
        else:
            if self.proc is not None:       # None when the proxy never listened: spawn said why
                self.proc.terminate()
                try: self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired: self.proc.kill()
            self.log.close()
        if self.srv is not None:
            self.srv.shutdown(); self.srv.server_close()

    def journal(self):
        if self.inproc:
            return "\n".join(self.lines)
        self.log.flush()
        return (self.t / "proxy.log").read_text()

    def journal_with(self, text, within=10):
        """The journal once it holds `text`, or as it is after `within` s. The proxy writes a
        request's outcome line after the answer's last byte, so the caller can be done before
        it is written (2 runs in 36 under load read the journal too early, 2026-10-01)."""
        deadline = time.time() + within
        while text not in self.journal() and time.time() < deadline:
            time.sleep(0.05)
        return self.journal()

    def send(self, path="/v1/chat/completions", stream=True, content="What is 2+2?", timeout=60):
        """Start a request; returns a Reply that reads the answer in the background."""
        body = json.dumps({"model": "m", "stream": stream, "max_tokens": 8,
                           "messages": [{"role": "user", "content": content}]}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json",
                                                       "Authorization": "Bearer test-key"})
        return Reply(conn)


class Reply:
    def __init__(self, conn):
        self.conn, self.status, self.events, self.raw, self.err = conn, None, [], b"", None
        self.t0, self.first_at = time.time(), None
        self.done = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            r = self.conn.getresponse()
            self.status, self.ctype, self.first_at = r.status, r.getheader("Content-Type") or "", time.time() - self.t0
            buf = b""
            while True:
                c = r.read1(65536)
                if not c:
                    break
                self.raw += c; buf += c
                while b"\n\n" in buf:
                    ev, buf = buf.split(b"\n\n", 1)
                    self.events.append(ev.decode("utf-8", "replace"))
        except Exception as e:                       # noqa: BLE001 - recorded for the assertion
            self.err = e
        finally:
            self.done.set()

    def keepalives(self):
        return [e for e in self.events if '"id":"keepalive"' in e or e.startswith("event: ping")]

    def content(self):
        return [e for e in self.events if '"id":"keepalive"' not in e and not e.startswith("event: ping")]


class AStreamedRequestWaitsForItsEngine(unittest.TestCase):
    INPROC = False

    def box(self, **kw):
        return Box(self, inproc=self.INPROC, **kw)

    def test_it_hears_from_the_proxy_at_once_and_gets_the_answer_once_the_engine_is_back(self):
        box = self.box()
        reply = box.send()
        time.sleep(3.5)
        self.assertEqual(reply.status, 200, "the 200 and the stream's headers came at once")
        self.assertLess(reply.first_at, 1.5)
        self.assertIn("text/event-stream", reply.ctype)
        self.assertGreaterEqual(len(reply.keepalives()), 2, reply.events)
        self.assertEqual(reply.content(), [])
        self.assertEqual(box.generations, [], "nothing reached an engine that is not there")
        box.start_engine()
        self.assertTrue(reply.done.wait(15))
        self.assertIsNone(reply.err)
        content = reply.content()
        self.assertTrue(any('"content":"four"' in e for e in content), content)
        self.assertEqual(content[-1], "data: [DONE]")
        self.assertEqual(box.generations, ["/v1/chat/completions"], "relayed once, when the engine answered")
        log = box.journal()
        self.assertIn("holding: the engine does not answer", log)
        self.assertIn("the engine answers again after", log)
        self.assertIn("POST /v1/chat/completions ok in", box.journal_with("POST /v1/chat/completions ok in"))

    def test_an_engine_that_answers_503_while_it_starts_is_waited_for_too(self):
        box = self.box()
        box.start_engine(mode="starting")
        reply = box.send()
        time.sleep(2)
        self.assertEqual(reply.status, 200)
        self.assertEqual(box.generations, [])
        box.start_engine(mode="up")
        self.assertTrue(reply.done.wait(15))
        self.assertTrue(any('"content":"four"' in e for e in reply.content()), reply.events)

    def test_an_ollama_stream_waits_in_silence_and_gets_its_ndjson(self):
        # the hold opened a text/event-stream for every streamed request, an Ollama one too, whose client reads a JSON
        # object a line and no SSE event (found in the 5th review of v1.43.0, 2026-11-04)
        box = self.box()
        reply = box.send(path="/api/chat")
        time.sleep(2.5)
        self.assertIsNone(reply.status, f"a stream was opened for an Ollama client: {reply.raw[:200]!r}")
        box.start_engine()
        self.assertTrue(reply.done.wait(15))
        self.assertIsNone(reply.err)
        self.assertEqual(reply.status, 200)
        self.assertIn("application/x-ndjson", reply.ctype)
        self.assertIn(b'"content": "four"', reply.raw)
        self.assertNotIn(b"keepalive", reply.raw)

    def test_an_ollama_stream_from_a_slow_engine_is_given_no_sse(self):
        # the keeper opened a text/event-stream when the engine was slow to answer, an Ollama client's too: untested, a
        # mutant without its choice stayed green (the 6th review of v1.43.0, 2026-11-04)
        box = self.box()
        box.start_engine(delay=2.5)                                             # up and slow: the keeper's case
        reply = box.send(path="/api/chat")
        time.sleep(1.8)                                                         # past KEEPALIVE_S, 1 s here
        self.assertIsNone(reply.status, f"a stream was opened for an Ollama client: {reply.raw[:200]!r}")
        self.assertTrue(reply.done.wait(15))
        self.assertIsNone(reply.err)
        self.assertIn("application/x-ndjson", reply.ctype)
        self.assertIn(b'"content": "four"', reply.raw)

    def test_the_anthropic_dialect_waits_on_pings(self):
        box = self.box()
        reply = box.send(path="/v1/messages")
        time.sleep(2.5)
        self.assertEqual(reply.status, 200)
        self.assertTrue(reply.keepalives() and all(e.startswith("event: ping") for e in reply.keepalives()), reply.events)
        box.start_engine()
        self.assertTrue(reply.done.wait(15))
        self.assertTrue(any("message_stop" in e for e in reply.content()), reply.events)


class ANonStreamedRequestWaitsInSilence(unittest.TestCase):
    INPROC = False

    def box(self, **kw):
        return Box(self, inproc=self.INPROC, **kw)

    def test_it_gets_its_answer_when_the_engine_is_back(self):
        box = self.box()
        reply = box.send(stream=False)
        time.sleep(2)
        self.assertIsNone(reply.status, "nothing is sent before the answer exists")
        box.start_engine()
        self.assertTrue(reply.done.wait(15))
        self.assertEqual(reply.status, 200)
        self.assertEqual(json.loads(reply.raw)["choices"][0]["message"]["content"], "four")


class AStopReachesWhatItShould(unittest.TestCase):
    """The stop (the memory floor, Abort all) and what a box alone reads of systemd around an engine's death."""
    INPROC = False

    def box(self, **kw):
        return Box(self, inproc=self.INPROC, **kw)

    def test_a_request_sent_again_after_the_engines_503_is_still_a_stop_target(self):
        # its first try got the engine's 503: the flag of an answer relayed whole was set on that error, the request held
        # and sent again, and its generation was then out of every stop's reach, a cut answer reaching an Anthropic
        # client as whole (v6.47's bug back; the 8th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the stop is made in this test's process")
        box = self.box()
        box.start_engine(mode="starting")                   # 503 to its generations and to its info route
        reply = box.send(stream=False)
        box.journal_with("-> POST /v1/chat/completions")
        time.sleep(1.5)                                     # its first try got the 503: it is held
        self.assertIsNone(reply.status, "the test needs the request held")
        box.start_engine(mode="up", delay=4.0)              # back: the generation sent again takes 4 s
        time.sleep(1.5)                                     # the hold sees it back and sends it (polls of 0.2 s)
        stopped, _ = box.mod.stop_generations("this test's Abort all", floor=False)
        self.assertEqual(stopped, 1, "the generation sent again after the engine's 503 was out of the stop's reach")
        self.assertTrue(reply.done.wait(15))

    def test_an_engine_error_being_relayed_is_no_stop_target(self):
        # its error's relay under way when a floor stop came: counted as stopped, it started the floor's episode, new
        # generations held back 60 s for nothing, and its client got no answer (the 9th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the stop is made in this test's process")
        box = self.box(state="active")
        box.start_engine(mode="slow400")
        reply = box.send(stream=False)
        time.sleep(1.0)                                     # the 400's headers came, its body 1 s away
        stopped, _ = box.mod.stop_generations("this test's memory floor", floor=True)
        self.assertEqual(stopped, 0, "an engine's error being relayed was stopped")
        self.assertIsNone(box.mod.floor_refusal(), "a floor that stopped nothing held new generations back")
        self.assertTrue(reply.done.wait(15))
        self.assertEqual(reply.status, 400, reply.raw[:200])
        self.assertIn(b"bad request", reply.raw)

    def test_an_engine_that_refuses_the_connection_is_no_stop_target(self):
        # no engine where it should be (refused, or reset under the memory pressure that also fires the floor): a floor
        # stop in the keeper's join counted the request as stopped, started its episode, new generations held back 60 s
        # while nothing generated, and its client was cut (the 10th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the stop is made in this test's process")
        box = self.box(state="active", hold_max="2")        # no engine: the connection is refused, held 2 s
        m = box.mod
        took = m.H._take_whole_answer
        stops = []

        def then_a_stop(h, *a, **k):
            took(h, *a, **k)
            if not stops:                                   # a floor stop lands as _open returns
                stops.append(m.stop_generations("this test's memory floor", floor=True))
        m.H._take_whole_answer = then_a_stop
        self.addCleanup(setattr, m.H, "_take_whole_answer", took)
        reply = box.send(stream=False)
        self.assertTrue(reply.done.wait(20))
        self.assertEqual(len(stops), 1, "the test needs its stop")
        self.assertEqual(stops[0][0], 0, "a request whose engine refused its connection was stopped")
        self.assertIsNone(m.floor_refusal(), "a floor that stopped nothing held new generations back")

    def test_a_box_alone_reads_no_order_at_a_dead_streams_end(self):
        # nothing to tell and nothing to undo without a pair: every stream at a vLLM death read systemd at its end, all at
        # once (the 8th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("systemd's reads are counted in this test's process")
        box = self.box(state="active")
        reads = []
        box.mod.unit_stopping_by_order = lambda: reads.append(1) or False
        box.start_engine(mode="vllm-dead")
        reply = box.send(stream=True)
        self.assertTrue(reply.done.wait(15))
        self.assertIn(b"EngineCore", reply.raw)
        self.assertEqual(reads, [], "a box alone read systemd at a dead stream's end")


class AnAnswerIsRelayedTheMomentItComes(unittest.TestCase):
    """The keepers watched the caller in a select() of 0.5 s and the relay waited for their next tick: a
    non-streamed answer took 503 ms through the proxy for 35 ms at the engine, and a stream whose engine took
    over a second lost up to 0.5 s more (measured on the reference box, v1.41.0, 2026-11-03)."""
    INPROC = False

    def box(self, **kw):
        return Box(self, inproc=self.INPROC, **kw)

    def test_a_non_streamed_answer_is_relayed_at_once(self):
        box = self.box()
        box.start_engine()
        took = []
        for _ in range(3):
            reply = box.send(stream=False)
            self.assertTrue(reply.done.wait(15))
            self.assertEqual((reply.status, json.loads(reply.raw)["choices"][0]["message"]["content"]), (200, "four"))
            took.append(reply.first_at)
        self.assertLess(min(took), 0.25, took)

    def test_a_stream_whose_engine_took_over_a_second_is_relayed_at_once(self):
        # no keepalive is due in the meantime (5 s), so the headers the client gets are the engine's
        box = self.box(keepalive="5")
        box.start_engine(delay=1.05)
        took = []
        for _ in range(3):
            reply = box.send(stream=True)
            self.assertTrue(reply.done.wait(15))
            self.assertEqual(reply.status, 200)
            self.assertTrue(any('"four"' in e for e in reply.content()), reply.events)
            took.append(reply.first_at - 1.05)
        self.assertLess(min(took), 0.25, took)


class ARequestInFlightHoldsItsSocketsAlone(unittest.TestCase):
    """A first fix of the relay's wait gave each request in flight a pipe of its own, two descriptors more: the other
    box of a pair could then exhaust the proxy's 1024, and select() refused any past 1024 (review, 2026-11-03). Its
    keepers now poll(): a request in flight holds its client's socket and its engine's, nothing else."""
    INPROC = False

    def test_eight_requests_in_flight_hold_no_more_than_their_sockets(self):
        box = Box(self, inproc=self.INPROC)
        box.start_engine(delay=2.0)
        where = f"/proc/{os.getpid() if self.INPROC else box.proc.pid}/fd"
        before = len(os.listdir(where))
        replies = [box.send(stream=False) for _ in range(8)]
        time.sleep(1.0)                     # all of them in flight, their engine still working
        during = len(os.listdir(where))
        for r in replies:
            self.assertTrue(r.done.wait(15))
            self.assertEqual(r.status, 200)
        # in this process: the test's client socket and the fake engine's too, two per request more
        self.assertLessEqual((during - before) / 8, 4.5 if self.INPROC else 2.5, (before, during))


class WhatIsNotWaitedFor(unittest.TestCase):
    INPROC = False

    def box(self, **kw):
        return Box(self, inproc=self.INPROC, **kw)

    """The v6.27 answer, at once, whenever no engine is on its way back."""

    def assert_503_at_once(self, box, path="/v1/chat/completions", stream=True):
        reply = box.send(path=path, stream=stream)
        self.assertTrue(reply.done.wait(5))
        self.assertEqual(reply.status, 503)
        self.assertLess(reply.first_at, 3)
        return json.loads(reply.raw)

    def test_a_lane_stopped_on_purpose(self):
        for state in ("inactive", "failed", "deactivating"):
            with self.subTest(state=state):
                box = self.box(state=state)
                self.assertEqual(self.assert_503_at_once(box)["error"]["type"], "engine_unavailable")

    def test_a_proxy_that_names_no_unit(self):
        box = self.box(units="")
        self.assertEqual(self.assert_503_at_once(box)["error"]["type"], "engine_unavailable")
        if not self.INPROC:                 # the startup line is the process's own
            self.assertIn("holds nothing", box.journal())

    def test_an_engine_that_answers_and_still_fails_is_here_not_coming(self):
        """Its info route answers and its generations 503: waiting would only replay the
        failure, so the answer is the one it always was."""
        box = self.box()
        box.start_engine(mode="starting", info_mode="up")
        self.assertEqual(self.assert_503_at_once(box)["error"]["type"], "engine_unavailable")
        self.assertNotIn("holding:", box.journal())


class AHoldEnds(unittest.TestCase):
    INPROC = False

    def box(self, **kw):
        return Box(self, inproc=self.INPROC, **kw)

    def test_a_stream_held_too_long_ends_with_an_error_event(self):
        box = self.box(hold_max="3")
        reply = box.send()
        self.assertTrue(reply.done.wait(15))
        self.assertEqual(reply.status, 200)
        last = json.loads(reply.content()[-1][len("data: "):])
        self.assertEqual(last["error"]["type"], "engine_unavailable")
        self.assertIn("not answering", last["error"]["message"])
        self.assertNotIn("data: [DONE]", reply.content())
        self.assertIn("held 3s and the engine is still not back", box.journal())
        self.assertIn("503 engine unreachable", box.journal_with("503 engine unreachable"))

    def test_an_anthropic_stream_ends_with_its_own_error_event(self):
        box = self.box(hold_max="3")
        reply = box.send(path="/v1/messages")
        self.assertTrue(reply.done.wait(15))
        last = reply.content()[-1]
        self.assertTrue(last.startswith("event: error\ndata: "), last)
        self.assertEqual(json.loads(last.split("data: ", 1)[1])["error"]["type"], "api_error")

    def test_a_non_streamed_request_held_too_long_gets_the_503(self):
        box = self.box(hold_max="3")
        reply = box.send(stream=False)
        self.assertTrue(reply.done.wait(15))
        self.assertEqual(reply.status, 503)
        self.assertGreaterEqual(reply.first_at, 2.5)

    def test_a_lane_stopped_while_a_request_waits_ends_the_wait(self):
        box = self.box()
        reply = box.send()
        time.sleep(1.5)
        box.state("inactive")
        self.assertTrue(reply.done.wait(15))
        self.assertIn("no longer on its way back", box.journal())

    def test_a_stop_ends_the_wait_while_the_unit_is_still_stopping(self):
        box = self.box()
        reply = box.send()
        time.sleep(1.5)
        box.state("deactivating")
        box.jobs("124 qwen38-flash.service stop running")
        self.assertTrue(reply.done.wait(15))
        self.assertIn("no longer on its way back", box.journal())

    def test_a_restart_keeps_the_wait_through_its_stop(self):
        """systemctl restart runs its stop half as "deactivating" under a restart job: the
        cockpit's restart of a zombie ended both clients' waits there (2026-10-01)."""
        box = self.box()
        reply = box.send()
        time.sleep(1.5)
        box.state("deactivating")
        box.jobs("6104 qwen38-flash.service restart running")
        time.sleep(4.5)                                 # past the 2 s systemd answers are kept
        box.state("inactive")                           # the instant between its two halves
        time.sleep(3)
        box.state("activating")
        box.jobs()
        self.assertIsNone(reply.err)
        self.assertFalse(reply.done.is_set(), reply.events)
        box.start_engine()
        self.assertTrue(reply.done.wait(15))
        self.assertTrue(any('"content":"four"' in e for e in reply.content()), reply.events)
        self.assertNotIn("no longer on its way back", box.journal())

    def test_a_caller_that_leaves_ends_the_wait_and_reaches_no_engine(self):
        for stream in (True, False):
            with self.subTest(stream=stream):
                box = self.box()
                body = json.dumps({"model": "m", "stream": stream, "messages": [{"role": "user", "content": "hi"}]}).encode()
                sk = socket.create_connection(("127.0.0.1", box.port), timeout=10)
                sk.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                           b"Authorization: Bearer test-key\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
                if stream:
                    try:
                        first = sk.recv(4096)
                    except TimeoutError:
                        # On a GitHub runner (v1.36.1's first CI run, 2026-10-18) the 200 did not come
                        # in 10 s; never on the reference box (40 runs of this class and the whole suite
                        # in one process on 4 cores): the next one says what the proxy did. This file had
                        # failed once before on a runner (2026-10-03), without saying which test or why.
                        self.fail(f"no answer in 10 s (proxy on {box.port}, engine on {box.engine_port}); "
                                  f"the proxy's journal:\n{box.journal()[-4000:]}")
                    self.assertIn(b"200", first, "the held stream's 200 came first")
                time.sleep(1.5)
                sk.close()                                   # the caller gives up
                self.assertIn("CLIENT GONE during hold", box.journal_with("CLIENT GONE during hold"))
                box.start_engine()
                time.sleep(1.5)
                self.assertEqual(box.generations, [], "nobody was left to answer")

    def test_an_engine_half_back_is_retried_a_few_times_then_answered(self):
        """Its info route answers again and its generations still 503 (a boot not quite done):
        the request is sent again a bounded number of times, each after a pause, and then
        the stream says why. It spun until PROXY_HOLD_MAX_S, with no pause at all, before."""
        box = self.box()
        reply = box.send()
        time.sleep(1.2)
        box.start_engine(mode="starting", info_mode="up")
        t0 = time.time()
        self.assertTrue(reply.done.wait(20))
        self.assertLess(time.time() - t0, 10, "bounded")
        rounds = box.journal().count("the engine answers again after")
        self.assertGreaterEqual(rounds, 2)
        self.assertLessEqual(rounds, 11)
        last = json.loads(reply.content()[-1][len("data: "):])
        self.assertEqual(last["error"]["type"], "engine_unavailable")
        self.assertIn("as it does while starting or shutting down", last["error"]["message"])

    def test_an_engine_half_back_that_finishes_its_boot_serves_the_request(self):
        box = self.box()
        reply = box.send()
        time.sleep(1.2)
        box.start_engine(mode="starting", info_mode="up")
        time.sleep(0.6)
        box.srv.mode = "up"
        self.assertTrue(reply.done.wait(20))
        self.assertTrue(any('"content":"four"' in e for e in reply.content()), reply.events)

    def test_an_engine_that_comes_back_with_a_plain_text_error_is_quoted_in_the_stream(self):
        box = self.box()
        reply = box.send()
        time.sleep(1.5)
        box.start_engine(mode="plain400")
        self.assertTrue(reply.done.wait(15))
        last = json.loads(reply.content()[-1][len("data: "):])
        self.assertIn("bad request, said in plain text", last["error"]["message"])
        self.assertIn("400 upstream", box.journal_with("400 upstream"))

    def test_an_engine_that_answers_a_held_stream_without_one_is_said_so(self):
        box = self.box()
        reply = box.send()
        time.sleep(1.5)
        box.start_engine(mode="json-for-stream")
        self.assertTrue(reply.done.wait(15))
        last = json.loads(reply.content()[-1][len("data: "):])
        self.assertIn("answered this streamed request without a stream", last["error"]["message"])
        self.assertIn("UPSTREAM CUT non-sse", box.journal_with("UPSTREAM CUT non-sse"))

    def test_an_engine_lost_while_it_counted_a_long_prompt_is_waited_for(self):
        """The pool is cached from the engine that served the last long prompt; the next one
        reaches its tokenizer and finds nobody: that is a restart, not a size refusal."""
        box = self.box()
        box.start_engine(pool=100000)
        big = "word " * 60000                                 # ~300 KB: counted by the engine
        first = box.send(content=big)
        self.assertTrue(first.done.wait(15))
        self.assertEqual(first.status, 200)
        box.stop_engine()
        reply = box.send(content=big)
        time.sleep(2)
        self.assertEqual(reply.status, 200)
        box.start_engine(pool=100000)
        self.assertTrue(reply.done.wait(20))
        self.assertTrue(any('"content":"four"' in e for e in reply.content()), reply.events)
        self.assertIn("holding: the engine does not answer", box.journal())

    def test_a_prompt_too_long_for_the_lane_that_came_back_is_told_in_the_stream(self):
        """The prompt fitted the lane that went away and not the one that came back: the
        stream the proxy opened carries the overflow, with the code clients recognise."""
        box = self.box()
        reply = box.send(content="word " * 60000)            # ~300 KB, past the size guard's door
        time.sleep(2)
        box.start_engine(pool=20000)                          # the engine counts ~75,000 tokens
        self.assertTrue(reply.done.wait(20))
        last = json.loads(reply.content()[-1][len("data: "):])
        self.assertEqual((last["error"]["type"], last["error"]["code"]), ("context_too_long", "context_length_exceeded"))
        self.assertIn("the prompt is too long", last["error"]["message"])
        self.assertEqual(box.generations, [])


class TheTwoQuestionsItAsks(unittest.TestCase):
    """engine_coming() and engine_answers(), on the edges a box can put them on."""

    def load(self, **env):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.update(env)
        spec = importlib.util.spec_from_file_location(f"kp_q_{time.time_ns()}", PROXY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_no_systemctl_means_no_engine_coming(self):
        empty = Path(tempfile.mkdtemp(prefix="no-systemctl-"))
        self.addCleanup(shutil.rmtree, empty, True)
        mod = self.load(PROXY_HOLD_UNITS=UNITS, PATH=str(empty))
        self.assertFalse(mod.engine_coming())

    def test_a_hold_of_zero_seconds_waits_for_nothing(self):
        mod = self.load(PROXY_HOLD_UNITS=UNITS, PROXY_HOLD_MAX_S="0")
        self.assertFalse(mod.engine_coming())

    def test_an_engine_answers_without_the_key_and_with_a_401(self):
        home = Path(tempfile.mkdtemp(prefix="no-key-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        class Refusing(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(self.server.code); self.send_header("Content-Length", "0"); self.end_headers()
        # held: once this engine is closed, nobody else can be listening on its port
        port = localports.held(self)
        srv = localports.serve(port, http.server.ThreadingHTTPServer, Refusing)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        mod = self.load(HOME=str(home), UPSTREAM=f"http://127.0.0.1:{port}")
        for code, answers in ((401, True), (404, True), (500, False), (503, False)):
            srv.code = code
            self.assertEqual(mod.engine_answers(), answers, code)
        srv.shutdown(); srv.server_close()
        self.assertFalse(mod.engine_answers(), "nobody listening")

    def test_a_caller_seen_through_tls_or_a_closed_socket(self):
        """Under TLS the socket under TLS is peeked at (the pair port, 2026-11-02): a caller still there is not
        gone, bytes it sent are no goodbye, and the end of its side is one, as on a plain socket. No peek was
        made through TLS before: a peer never saw an entry's client leave."""
        import ssl
        mod = self.load()
        class Fake:
            pass
        f = Fake()
        here, there = socket.socketpair()
        self.addCleanup(there.close)
        f.connection = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).wrap_socket(here, do_handshake_on_connect=False,
                                                                             server_hostname="x")
        self.addCleanup(f.connection.close)
        self.assertFalse(mod.H._client_left(f), "a caller still there, silent")
        there.sendall(b"\x15\x03\x03")                 # the start of a TLS record: bytes, not a goodbye
        self.assertFalse(mod.H._client_left(f))
        socket.socket.recv(f.connection, 3)
        there.close()
        self.assertTrue(mod.H._client_left(f), "a caller that ended its side is gone, under TLS too")
        f.connection = socket.socket(); f.connection.close()
        self.assertTrue(mod.H._client_left(f), "a socket that is gone is a caller gone")


class WhereItIsSwitchedOn(unittest.TestCase):
    def test_the_installed_proxy_names_every_text_lane(self):
        """In a unit file, an unquoted value with a space is two assignments. Every lane
        that serves text behind this proxy is named: the 27B, the flash, Kolibri, the two Nemotron lanes and
        the Qwen3.8 Flash-Next FP4-Local lane."""
        tpl = (HERE.parents[1] / "qwen38-keepalive.service.template").read_text()
        self.assertIn(f'Environment="PROXY_HOLD_UNITS={UNITS} qwen38-kolibri.service qwen38-nemotron.service '
                      f'qwen38-nemotron-super.service qwen38-flash-local.service qwen38-ornith-122b.service"\n', tpl)

    def test_its_startup_line_says_what_it_holds(self):
        box = Box(self)
        held = "holds requests up to 30s while qwen38-sglang.service or qwen38-flash.service comes back"
        self.assertIn(held, box.journal_with(held))



# The same cases with the proxy in this process: what CI's coverage floor measures. Each is
# declared again rather than only inherited: CI counts the tests a file declares against the
# tests that ran, and an inherited case runs without being declared.
class AStreamedRequestWaitsForItsEngineInProcess(AStreamedRequestWaitsForItsEngine):
    INPROC = True

    def test_it_hears_from_the_proxy_at_once_and_gets_the_answer_once_the_engine_is_back(self):
        super().test_it_hears_from_the_proxy_at_once_and_gets_the_answer_once_the_engine_is_back()

    def test_an_engine_that_answers_503_while_it_starts_is_waited_for_too(self):
        super().test_an_engine_that_answers_503_while_it_starts_is_waited_for_too()

    def test_the_anthropic_dialect_waits_on_pings(self):
        super().test_the_anthropic_dialect_waits_on_pings()

    def test_an_ollama_stream_waits_in_silence_and_gets_its_ndjson(self):
        super().test_an_ollama_stream_waits_in_silence_and_gets_its_ndjson()

    def test_an_ollama_stream_from_a_slow_engine_is_given_no_sse(self):
        super().test_an_ollama_stream_from_a_slow_engine_is_given_no_sse()


class ANonStreamedRequestWaitsInSilenceInProcess(ANonStreamedRequestWaitsInSilence):
    INPROC = True

    def test_it_gets_its_answer_when_the_engine_is_back(self):
        super().test_it_gets_its_answer_when_the_engine_is_back()


class AStopReachesWhatItShouldInProcess(AStopReachesWhatItShould):
    INPROC = True

    def test_a_request_sent_again_after_the_engines_503_is_still_a_stop_target(self):
        super().test_a_request_sent_again_after_the_engines_503_is_still_a_stop_target()

    def test_a_box_alone_reads_no_order_at_a_dead_streams_end(self):
        super().test_a_box_alone_reads_no_order_at_a_dead_streams_end()

    def test_an_engine_error_being_relayed_is_no_stop_target(self):
        super().test_an_engine_error_being_relayed_is_no_stop_target()

    def test_an_engine_that_refuses_the_connection_is_no_stop_target(self):
        super().test_an_engine_that_refuses_the_connection_is_no_stop_target()


class AnAnswerIsRelayedTheMomentItComesInProcess(AnAnswerIsRelayedTheMomentItComes):
    INPROC = True

    def test_a_non_streamed_answer_is_relayed_at_once(self):
        super().test_a_non_streamed_answer_is_relayed_at_once()

    def test_a_stream_whose_engine_took_over_a_second_is_relayed_at_once(self):
        super().test_a_stream_whose_engine_took_over_a_second_is_relayed_at_once()


class ARequestInFlightHoldsItsSocketsAloneInProcess(ARequestInFlightHoldsItsSocketsAlone):
    INPROC = True

    def test_eight_requests_in_flight_hold_no_more_than_their_sockets(self):
        super().test_eight_requests_in_flight_hold_no_more_than_their_sockets()


class WhatIsNotWaitedForInProcess(WhatIsNotWaitedFor):
    INPROC = True

    def test_a_lane_stopped_on_purpose(self):
        super().test_a_lane_stopped_on_purpose()

    def test_a_proxy_that_names_no_unit(self):
        super().test_a_proxy_that_names_no_unit()

    def test_an_engine_that_answers_and_still_fails_is_here_not_coming(self):
        super().test_an_engine_that_answers_and_still_fails_is_here_not_coming()


class AHoldEndsInProcess(AHoldEnds):
    INPROC = True

    def test_a_stream_held_too_long_ends_with_an_error_event(self):
        super().test_a_stream_held_too_long_ends_with_an_error_event()

    def test_an_anthropic_stream_ends_with_its_own_error_event(self):
        super().test_an_anthropic_stream_ends_with_its_own_error_event()

    def test_a_non_streamed_request_held_too_long_gets_the_503(self):
        super().test_a_non_streamed_request_held_too_long_gets_the_503()

    def test_a_lane_stopped_while_a_request_waits_ends_the_wait(self):
        super().test_a_lane_stopped_while_a_request_waits_ends_the_wait()

    def test_a_stop_ends_the_wait_while_the_unit_is_still_stopping(self):
        super().test_a_stop_ends_the_wait_while_the_unit_is_still_stopping()

    def test_a_restart_keeps_the_wait_through_its_stop(self):
        super().test_a_restart_keeps_the_wait_through_its_stop()

    def test_a_caller_that_leaves_ends_the_wait_and_reaches_no_engine(self):
        super().test_a_caller_that_leaves_ends_the_wait_and_reaches_no_engine()

    def test_an_engine_half_back_is_retried_a_few_times_then_answered(self):
        super().test_an_engine_half_back_is_retried_a_few_times_then_answered()

    def test_an_engine_half_back_that_finishes_its_boot_serves_the_request(self):
        super().test_an_engine_half_back_that_finishes_its_boot_serves_the_request()

    def test_an_engine_that_comes_back_with_a_plain_text_error_is_quoted_in_the_stream(self):
        super().test_an_engine_that_comes_back_with_a_plain_text_error_is_quoted_in_the_stream()

    def test_an_engine_that_answers_a_held_stream_without_one_is_said_so(self):
        super().test_an_engine_that_answers_a_held_stream_without_one_is_said_so()

    def test_an_engine_lost_while_it_counted_a_long_prompt_is_waited_for(self):
        super().test_an_engine_lost_while_it_counted_a_long_prompt_is_waited_for()

    def test_a_prompt_too_long_for_the_lane_that_came_back_is_told_in_the_stream(self):
        super().test_a_prompt_too_long_for_the_lane_that_came_back_is_told_in_the_stream()


class EveryCaseHasItsInProcessTwin(unittest.TestCase):
    def test_each_twin_declares_every_case_of_its_class(self):
        for twin in (AStreamedRequestWaitsForItsEngineInProcess, ANonStreamedRequestWaitsInSilenceInProcess,
                     AnAnswerIsRelayedTheMomentItComesInProcess, ARequestInFlightHoldsItsSocketsAloneInProcess,
                     WhatIsNotWaitedForInProcess, AHoldEndsInProcess, AStopReachesWhatItShouldInProcess):
            base = twin.__bases__[0]
            cases = {n for n in vars(base) if n.startswith("test_")}
            self.assertTrue(cases, base.__name__)
            self.assertEqual({n for n in vars(twin) if n.startswith("test_")}, cases, twin.__name__)


if __name__ == "__main__":
    unittest.main()
