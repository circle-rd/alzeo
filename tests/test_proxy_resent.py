#!/usr/bin/env python3
"""The proxy says when a long prompt comes again after its client left it without an answer (role 9,
v6.49, issue #61).

On the Ornith lane a prompt of 494,089 tokens the prefix cache could not serve took 19 min before its
first token, and Claude Code with its own default timeouts left it after 620 s, twice, then sent it
without streaming and left after 300 s, twice: the engine lost part of the prompt each time, and the
answer came at the fifth attempt, after 35 min (the reference box, 2026-10-30). The journal said
CLIENT GONE, then the same body again, and nothing else.

A real proxy, imported fresh with short thresholds, in front of an engine that answers nothing. Its
line is read back by the cockpit's parser, as the banner reads it.
"""
import http.server
import importlib.util
import io
import json
import os
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "dashboard"))
import lifecycle as lc  # noqa: E402

CLAUDE = "claude-cli/2.1.314 (external, cli)"


def _no_key():
    """No serving key in this harness (see tests/test_proxy_relay_integrity.py)."""
    raise FileNotFoundError("no serving key in this harness")


class Computing(http.server.BaseHTTPRequestHandler):
    """An engine computing a prompt: it reads the request and answers nothing until released."""
    protocol_version = "HTTP/1.1"
    release = threading.Event()

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.release.wait(30)

    def do_GET(self):
        body = b'{"max_total_num_tokens": 900000}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def body(text="a" * 3000, stream=True, path="/v1/messages"):
    if path == "/v1/messages":
        j = {"model": "m", "max_tokens": 32000, "system": "You are a coding agent.",
             "messages": [{"role": "user", "content": text}]}
    else:
        j = {"model": "m", "max_tokens": 300, "messages": [{"role": "user", "content": text}]}
    j["stream"] = stream             # as Claude Code sends it: the key stays, true then false
    return json.dumps(j).encode()


class ALongPromptSentAgainIsSaid(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Computing)
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        env = {"UPSTREAM": f"http://127.0.0.1:{cls.engine.server_address[1]}", "KEEPALIVE_S": "0.2",
               "RESEND_MIN_BYTES": "2000", "RESEND_MIN_S": "0.6", "RESEND_WINDOW_S": "3"}
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        spec = importlib.util.spec_from_file_location("kproxy_resent", REPO / "keepalive-proxy.py")
        cls.mod = importlib.util.module_from_spec(spec)
        err, sys.stderr = sys.stderr, io.StringIO()
        try:
            spec.loader.exec_module(cls.mod)
            cls.mod._api_key = _no_key
        finally:
            sys.stderr = err
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        cls.lines = []
        cls.mod.log = cls.lines.append
        cls.srv = cls.mod.Server(("127.0.0.1", 0), cls.mod.H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        Computing.release.set()
        cls.srv.shutdown(); cls.srv.server_close()
        cls.engine.shutdown(); cls.engine.server_close()

    def setUp(self):
        self.mod._GAVE_UP.clear()
        del self.lines[:]

    def leave_after(self, seconds, data, path="/v1/messages", ua=CLAUDE):
        """Send a request, wait, leave; return once the proxy has written how it ended."""
        n = sum("CLIENT GONE" in ln for ln in self.lines)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  f"Authorization: Bearer k\r\nUser-Agent: {ua}\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
        time.sleep(seconds)
        s.close()
        deadline = time.time() + 5
        while sum("CLIENT GONE" in ln for ln in self.lines) == n and time.time() < deadline:
            time.sleep(0.05)
        self.assertGreater(sum("CLIENT GONE" in ln for ln in self.lines), n, self.lines[-3:])

    def resent(self):
        return [ln for ln in self.lines if " RESENT " in ln]

    def test_claude_codes_retries_are_said_with_what_widens_its_timeout(self):
        # its own pattern: streamed, then the same prompt without streaming, each left unanswered
        self.leave_after(1.0, body(stream=True))
        self.assertEqual(self.resent(), [])
        self.leave_after(1.0, body(stream=False))
        self.assertEqual(len(self.resent()), 1, self.lines)
        line = self.resent()[0]
        self.assertRegex(line, r"RESENT the prompt its client left without an answer after 1 s "
                               r"\(1 time in a row, claude-cli/2\.1\.314\): Claude Code gives up on a turn")
        self.assertIn("claude-local, or API_TIMEOUT_MS=3600000", line)
        self.leave_after(1.0, body(stream=False))
        self.assertIn("(2 times in a row, claude-cli/2.1.314)", self.resent()[-1])
        # the cockpit's parser reads the line as journalctl -o short-unix writes it
        seen = lc.parse_resent(f"1730310000.123456 gx10-eff9 python3[5003]: [proxy] {self.resent()[-1]}")
        self.assertEqual(seen["count"], 1)
        self.assertEqual((seen["last"]["secs"], seen["last"]["n"], seen["last"]["who"]), (1, 2, "claude-cli/2.1.314"))
        self.assertTrue(seen["last"]["advice"].startswith("Claude Code gives up on a turn after 10 min"))

    def test_another_client_is_told_to_widen_its_own_timeout(self):
        self.leave_after(1.0, body(stream=False, path="/v1/chat/completions"), path="/v1/chat/completions",
                         ua="OpenAI/Python 3.26.1")
        self.leave_after(1.0, body(stream=False, path="/v1/chat/completions"), path="/v1/chat/completions",
                         ua="OpenAI/Python 3.26.1")
        self.assertEqual(len(self.resent()), 1, self.lines)
        self.assertIn("(1 time in a row, OpenAI/Python): its client's timeout is shorter than this prompt",
                      self.resent()[0])

    def test_nothing_is_said_of_another_prompt_a_short_one_a_quick_leave_or_a_late_return(self):
        self.leave_after(1.0, body("a" * 3000))
        self.leave_after(1.0, body("b" * 3000))
        self.assertEqual(self.resent(), [], "another prompt")
        self.mod._GAVE_UP.clear()
        self.leave_after(1.0, body("c" * 100))
        self.leave_after(1.0, body("c" * 100))
        self.assertEqual(self.resent(), [], "under RESEND_MIN_BYTES it computes in seconds")
        self.mod._GAVE_UP.clear()
        self.leave_after(0.1, body("d" * 3000))
        self.leave_after(1.0, body("d" * 3000))
        self.assertEqual(self.resent(), [], "left before RESEND_MIN_S: no timeout of a client")
        self.mod._GAVE_UP.clear()
        self.leave_after(1.0, body("e" * 3000))
        time.sleep(3.5)
        self.leave_after(1.0, body("e" * 3000))
        self.assertEqual(self.resent(), [], "past RESEND_WINDOW_S")


class OnlyAClientThatLeftBeforeAnyAnswerCounts(unittest.TestCase):
    """A non-streamed answer the engine had finished, whose client left while it was relayed, and a request
    held while the lane started, which the engine never had, were counted as prompts left without an
    answer: the banner then blamed the client's timeout and said the engine computed it again (code
    review, 2026-10-31). Every way the proxy ends a request on a client that left is said here."""

    @classmethod
    def setUpClass(cls):
        saved = os.environ.get("RESEND_MIN_BYTES")
        os.environ["RESEND_MIN_BYTES"] = "10"
        spec = importlib.util.spec_from_file_location("kproxy_resent_outcomes", REPO / "keepalive-proxy.py")
        cls.mod = importlib.util.module_from_spec(spec)
        err, sys.stderr = sys.stderr, io.StringIO()
        try:
            spec.loader.exec_module(cls.mod)
        finally:
            sys.stderr = err
            if saved is None:
                os.environ.pop("RESEND_MIN_BYTES", None)
            else:
                os.environ["RESEND_MIN_BYTES"] = saved

    def gave_up(self, outcome, first=None):
        self.mod._GAVE_UP.clear()
        handler = type("H", (), {})()
        handler._prompt_fp, handler._first, handler._t0 = "f" * 16, first, time.time() - 600
        self.mod.H._note_gave_up(handler, outcome)
        return bool(self.mod._GAVE_UP)

    def test_every_way_a_client_goes_is_counted_or_not_on_purpose(self):
        import re
        written = set(re.findall(r'"(CLIENT GONE [^"{]*)"', (REPO / "keepalive-proxy.py").read_text()))
        # v6.50: "CLIENT GONE during pair" is a client that left a request this box sent to its peer
        # (role 10). Not counted here: the request went on before its prompt was fingerprinted, and the
        # peer's own proxy, which computed the prompt, counts its caller leaving it
        self.assertEqual(written, {"CLIENT GONE before its body", "CLIENT GONE during hold", "CLIENT GONE during keepalive",
                                   "CLIENT GONE before the engine answered", "CLIENT GONE during non-sse wait",
                                   "CLIENT GONE mid-systemone", "CLIENT GONE on write", "CLIENT GONE during pair"},
                         "a new way: say here whether it is a client that left before any answer")
        counted = {o for o in written if self.gave_up(o)}
        self.assertEqual(counted, {"CLIENT GONE during keepalive", "CLIENT GONE before the engine answered",
                                   "CLIENT GONE during non-sse wait"})

    def test_a_client_that_had_its_first_event_did_not_leave_without_an_answer(self):
        self.assertTrue(self.gave_up("CLIENT GONE during keepalive"))
        self.assertFalse(self.gave_up("CLIENT GONE during keepalive", first=12.5))


class ThePromptsFingerprint(unittest.TestCase):
    """What a retry changes (the stream, the answer's length) is no other prompt; what it asks is."""

    @classmethod
    def setUpClass(cls):
        saved = os.environ.get("RESEND_MIN_BYTES")
        os.environ["RESEND_MIN_BYTES"] = "10"
        spec = importlib.util.spec_from_file_location("kproxy_resent_fp", REPO / "keepalive-proxy.py")
        cls.mod = importlib.util.module_from_spec(spec)
        err, sys.stderr = sys.stderr, io.StringIO()
        try:
            spec.loader.exec_module(cls.mod)
        finally:
            sys.stderr = err
            if saved is None:
                os.environ.pop("RESEND_MIN_BYTES", None)
            else:
                os.environ["RESEND_MIN_BYTES"] = saved

    def test_the_stream_and_the_answers_length_do_not_count_and_the_prompt_does(self):
        fp = self.mod.prompt_fingerprint
        a = {"model": "m", "system": "s", "messages": [{"role": "user", "content": "hello there"}], "stream": True,
             "max_tokens": 32000}
        b = dict(a, stream=False, max_tokens=21333)
        self.assertEqual(fp(json.dumps(a).encode(), "/v1/messages"), fp(json.dumps(b).encode(), "/v1/messages"))
        c = dict(a, messages=[{"role": "user", "content": "hello there!"}])
        self.assertNotEqual(fp(json.dumps(a).encode(), "/v1/messages"), fp(json.dumps(c).encode(), "/v1/messages"))
        self.assertNotEqual(fp(json.dumps(a).encode(), "/v1/messages"),
                            fp(json.dumps(a).encode(), "/v1/chat/completions"))
        self.assertIsNone(fp(json.dumps(a).encode(), "/v1/models"), "a route that generates nothing")
        self.assertIsNone(fp(b'{"a":1}', "/v1/messages"), "under RESEND_MIN_BYTES")
        # read on the raw bytes, never parsed: a body that is no JSON still gets one (an engine refuses
        # it at once, so no client of it waits RESEND_MIN_S to leave)
        self.assertEqual(len(fp(b"{not json at all}", "/v1/messages")), 16)
        # spaces around the colon, as another client writes them, and a stream_options of another value
        d = json.dumps(dict(a, stream_options={"include_usage": True})).replace('"stream": true', '"stream" :true')
        e = json.dumps(dict(b, stream_options={"include_usage": False}))
        self.assertEqual(fp(d.encode(), "/v1/messages"), fp(e.encode(), "/v1/messages"))


if __name__ == "__main__":
    unittest.main()
