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
import json
import os
import pathlib
import re
import shutil
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


def fake_engine(name, model):
    """A fake SGLang engine that serves `model`, writes down every POST it gets (path, model, the
    key it was given) and answers a generation whole or as an SSE stream."""
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        served = [model]
        seen: list = []
        delay = 0.0

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
            part = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(part), part)); self.wfile.flush()

    return Handler


class Engine(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class TwoBoxesBehindOneEndpoint(unittest.TestCase):
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
        cls.port1 = cls.start_proxy(cls.home1, cls.engines[0], "spark1")

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
            st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
            self.assertLess(time.monotonic() - t0, 15)
            self.assertEqual(st, 404, out[:300])        # its list cannot be read: no box serves the name
            self.assertIn("does not answer", json.loads(out)["error"]["served"]["spark2"])
        finally:
            pair["peers"][0]["proxy"] = f"http://127.0.0.1:{self.port2}"
            (self.home1 / ".config/qwen38/pair.json").write_text(json.dumps(pair))
            time.sleep(2.5)

    # ---- the list, the key, a box alone ---------------------------------------------------------
    def test_the_models_list_names_both_boxes(self):
        st, out, _ = self.call("/v1/models", method="GET")
        self.assertEqual(st, 200, out[:300])
        rows = {r["id"]: r.get("box") for r in json.loads(out)["data"]}
        self.assertEqual(rows, {"qwen3.8-27b": "spark1", "qwen3.8-flash-next-fp4": "spark2"})

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
