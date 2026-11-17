#!/usr/bin/env python3
"""The embedding side lane behind the proxy: /v1/embeddings goes to its own upstream.

The text engine serves no embedding route, so before this lane the proxy relayed the
route to it and got a 404. Here a fake embedding server stands behind EMBED_UPSTREAM,
and a fake text engine behind UPSTREAM: the route must reach the first, never the
second, with the same key the box serves, and a lane that does not answer must say so
as a 503 of its own rather than a text engine's refusal.

The proxy is imported fresh per scenario, as tests/test_proxy_client_keys.py does,
because its upstreams are read at import time.
"""
import http.server
import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class TextEngine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or "0")
        TextEngine.hits.append((self.path, self.headers.get("Authorization"), self.rfile.read(n)))
        body = b'{"id":"chatcmpl-test","choices":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class EmbedEngine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits = []
    status = 200

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(n)
        EmbedEngine.hits.append((self.path, self.headers.get("Authorization"), body))
        if EmbedEngine.status != 200:
            out = b'{"error":"refused by the embedding lane"}'
            self.send_response(EmbedEngine.status)
        else:
            out = json.dumps({"object": "list", "data": [
                {"object": "embedding", "index": 0, "embedding": [0.5] * 1024}],
                "model": "pplx-embed-v1-0.6b"}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


_fresh = [0]


def fresh_proxy(upstream_port, embed_port, keys_path, upstream_key):
    _fresh[0] += 1
    spec = importlib.util.spec_from_file_location("kproxy_embed_%d" % _fresh[0], REPO / "keepalive-proxy.py")
    mod = importlib.util.module_from_spec(spec)
    names = ("UPSTREAM", "EMBED_UPSTREAM", "QWEN38_CLIENT_KEYS_FILE", "QWEN38_UPSTREAM_API_KEY", "EMBED_API_KEY")
    saved = [os.environ.get(k) for k in names]
    os.environ["UPSTREAM"] = "http://127.0.0.1:%d" % upstream_port
    os.environ["EMBED_UPSTREAM"] = "http://127.0.0.1:%d" % embed_port if embed_port else ""
    os.environ["QWEN38_CLIENT_KEYS_FILE"] = keys_path
    os.environ["QWEN38_UPSTREAM_API_KEY"] = upstream_key
    os.environ.pop("EMBED_API_KEY", None)
    try:
        spec.loader.exec_module(mod)
    finally:
        for key, value in zip(names, saved):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    mod._api_key = lambda: (_ for _ in ()).throw(FileNotFoundError("no serving key in this harness"))
    return mod


def post(port, path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=body or b'{"input":"hello"}',
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        finally:
            e.close()


class EmbeddingRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TextEngine)
        cls.embed = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EmbedEngine)
        for srv in (cls.text, cls.embed):
            threading.Thread(target=srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.text, cls.embed):
            srv.shutdown()
            srv.server_close()

    def setUp(self):
        TextEngine.hits.clear()
        EmbedEngine.hits.clear()
        EmbedEngine.status = 200
        fd, self.keys = tempfile.mkstemp(suffix=".json", text=True)
        with os.fdopen(fd, "w") as f:
            f.write('{"tok-alice":"alice"}')
        self.addCleanup(os.unlink, self.keys)

    def live(self, embed_port=None):
        mod = fresh_proxy(self.text.server_address[1],
                          self.embed.server_address[1] if embed_port is None else embed_port,
                          self.keys, "engine-K")
        srv = mod.Server(("127.0.0.1", 0), mod.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", srv.server_address[1]), 0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        return srv.server_address[1]

    def test_the_openai_route_reaches_the_embedding_lane_and_not_the_text_engine(self):
        port = self.live()
        for path in ("/v1/embeddings", "/v1/embedding"):
            with self.subTest(path=path):
                TextEngine.hits.clear()
                EmbedEngine.hits.clear()
                status, body = post(port, path, "tok-alice", b'{"model":"pplx-embed-v1-0.6b","input":"hello"}')
                self.assertEqual(status, 200, body)
                self.assertEqual(len(json.loads(body)["data"][0]["embedding"]), 1024)
                self.assertEqual([h[0] for h in EmbedEngine.hits], ["/v1/embeddings"])
                self.assertEqual(EmbedEngine.hits[0][1], "Bearer engine-K")
                self.assertEqual(EmbedEngine.hits[0][2], b'{"model":"pplx-embed-v1-0.6b","input":"hello"}')
                self.assertEqual(TextEngine.hits, [])

    def test_a_client_without_the_key_never_reaches_the_embedding_lane(self):
        port = self.live()
        for token in (None, "wrong"):
            with self.subTest(token=token):
                status, _ = post(port, "/v1/embeddings", token)
                self.assertEqual(status, 401)
        self.assertEqual(EmbedEngine.hits, [])

    def test_the_text_route_still_reaches_the_text_engine(self):
        port = self.live()
        status, _ = post(port, "/v1/chat/completions", "tok-alice", b'{"model":"m","messages":[]}')
        self.assertEqual(status, 200)
        self.assertEqual([h[0] for h in TextEngine.hits], ["/v1/chat/completions"])
        self.assertEqual(EmbedEngine.hits, [])

    def test_an_embedding_lane_that_does_not_answer_is_a_503_of_its_own(self):
        port = self.live(embed_port=1)
        status, body = post(port, "/v1/embeddings", "tok-alice")
        self.assertEqual(status, 503, body)
        self.assertIn("embedding lane", json.loads(body)["error"]["message"])
        self.assertEqual(TextEngine.hits, [])

    def test_the_lane_refusal_is_relayed_as_it_answers(self):
        EmbedEngine.status = 422
        port = self.live()
        status, body = post(port, "/v1/embeddings", "tok-alice")
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(body), {"error": "refused by the embedding lane"})


if __name__ == "__main__":
    unittest.main()
