"""A request body the guards cannot read is not relayed (2026-10-17).

Every guard of this proxy reads the body as it comes: the size guard, the sampling guard, the
logprob ceiling, the tool schemas. A body sent with a Content-Encoding (gzip, deflate, br) reads
as no JSON at all to each of them, so each let it pass, and an engine or a front that decodes it
would have taken what no guard had seen (found in review, 2026-10-17). A body in any coding but
identity is refused with a 415 that names the one coding taken, before it is read.
"""
import gzip
import http.server
import importlib.util
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


def _keep_env(cls):
    """Put os.environ back as this class found it (tests/test_suite_isolation.py holds it)."""
    saved = dict(os.environ)
    cls.addClassCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))


def _no_key():
    """The serving key of this harness's proxy: none, whatever HOME holds."""
    raise FileNotFoundError("no serving key in this harness")


HERE = Path(__file__).resolve()
SPEC = importlib.util.spec_from_file_location("kproxy_encoding", HERE.parents[1] / "keepalive-proxy.py")
DOC = {"model": "m", "stream": False, "messages": [{"role": "user", "content": "hi"}],
       "top_logprobs": 100000}


class Engine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Engine.seen.append((self.path, self.headers.get("Content-Encoding")))
        out = json.dumps({"id": "chatcmpl-x", "object": "chat.completion", "choices": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class ContentEncoding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _keep_env(cls)
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        cls.engine.daemon_threads = True
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        os.environ["UPSTREAM"] = f"http://127.0.0.1:{cls.engine.server_port}"
        for k in ("QWEN38_CLIENT_KEYS_FILE", "QWEN38_UPSTREAM_API_KEY", "PROXY_HOLD_UNITS"):
            os.environ.pop(k, None)
        cls.mod = importlib.util.module_from_spec(SPEC)
        sys.argv = ["keepalive-proxy.py"]
        SPEC.loader.exec_module(cls.mod)
        cls.mod._api_key = _no_key
        cls.proxy = cls.mod.Server(("127.0.0.1", 0), cls.mod.H)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.port = cls.proxy.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.engine.shutdown()
        cls.engine.server_close()

    def setUp(self):
        Engine.seen.clear()

    def post(self, data, encoding):
        headers = {"Content-Type": "application/json"}
        if encoding is not None:
            headers["Content-Encoding"] = encoding
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions",
                                     data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_a_compressed_body_is_refused_unread(self):
        """Its top_logprobs, which the guard refuses in clear, would have gone through."""
        packed = gzip.compress(json.dumps(DOC).encode())
        for coding in ("gzip", "GZIP", "deflate", "br", "gzip, identity"):
            with self.subTest(coding=coding):
                status, headers, body = self.post(packed, coding)
                self.assertEqual(status, 415, body[:200])
                self.assertEqual(headers.get("Accept-Encoding"), "identity")
                self.assertIn("identity", json.loads(body)["error"]["message"])
        self.assertEqual(Engine.seen, [], "a body no guard could read reached the engine")

    def test_a_body_in_utf16_or_utf32_is_refused_unread(self):
        """Python's json reads JSON in UTF-16 or UTF-32, and so does the engine's (FastAPI), but no guard here saw a
        field in it: its top_logprobs went through, and took SGLang down (security review, 2026-11-03)."""
        for enc in ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le"):
            with self.subTest(encoding=enc):
                status, _, body = self.post(json.dumps(DOC).encode(enc), None)
                self.assertEqual(status, 415, body[:200])
                self.assertIn("UTF-8", json.loads(body)["error"]["message"])
        self.assertEqual(Engine.seen, [], "a body no guard could read reached the engine")
        # UTF-8, with its byte order mark too, is read and judged as before
        status, _, _ = self.post(json.dumps(DOC).encode("utf-8-sig"), None)
        self.assertEqual(status, 400, "the guard did not read a UTF-8 body with a byte order mark")

    def test_an_uncompressed_body_is_judged_and_relayed_as_before(self):
        plain = dict(DOC, top_logprobs=5)
        for coding in (None, "identity", " Identity "):
            with self.subTest(coding=coding):
                status, _, body = self.post(json.dumps(plain).encode(), coding)
                self.assertEqual(status, 200, body[:200])
        self.assertEqual(len(Engine.seen), 3)
        status, _, _ = self.post(json.dumps(DOC).encode(), "identity")     # the guard still reads it
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
