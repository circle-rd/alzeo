#!/usr/bin/env python3
"""Two boxes behind one endpoint (keepalive-proxy role 10, discussion #45, decisions 961-976).

Two real proxies, each in front of a fake engine of its own: spark1 serves the 27B, spark2 serves Flash
FP4-Local, and each names the other in its pair.json with the other's key and certificate (a pair is mutual,
decision 973), the two talking over TLS both ways on their pair ports (decision 971). A generation that
names one of this repo's lanes' models goes to the box that serves it; a lane name no box serves is refused
with what each box serves; any other name stays on the box it was sent to, as on a box alone; a peer that
switched since its list was read answers 409 and is never made to serve the wrong model; a request a peer
sends on is never sent on again; GET /v1/models lists both boxes; the peer's key never reaches a journal.
The pair port admits the paired box alone: no certificate, a certificate not pinned, a box taken out of the
pair are refused, and a box never sends to an address that does not present the pinned certificate. A box
without pair.json behaves exactly as before.

The pair's tests are four files since 2026-11-10, so that CI runs them side by side: this one,
tests/test_proxy_pair_inprocess.py, tests/test_proxy_pair_one_model.py and
tests/test_proxy_pair_one_model_inprocess.py. What they share (the two
boxes, their identities, the fake engines, PairFixture) is tests/pairbox.py.
"""
import hashlib
import http.client
import http.server
import importlib.util
import json
import os
import pathlib
import re
import shutil
import socket
import select
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import localports  # noqa: E402
from pairbox import (child_of, client_ctx, Engine, fake_engine, FakeCockpit, future_ident, GIB,
    HOP_ENDED_NDJSON, HOP_ENDED_OPENAI, ident, install_identity, KEY_1, KEY_2, PAIR_GRACE_READ_S, PAIR_TTL,
    PairFixture, peer_entry, REPO, sleeper, TEST_REV, tls_peer, _until, VLLM_DEAD_EVENT)  # noqa: E402


class TwoBoxesBehindOneEndpoint(PairFixture, unittest.TestCase):
    """Two boxes that serve two models: each request goes to the box that serves its model."""

    def test_a_box_serving_its_own_model_writes_no_line_on_the_other(self):
        # each request of a model this box serves reads the other box's list again every PAIR_TTL_S (v6.51): those
        # reads must not push the other box's real requests out of the lines its cockpit reads (review of v1.43.0)
        listed = lambda: self.journal_of("spark2").count("GET /v1/models ok get")
        before = listed()
        for _ in range(4):
            st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
            self.assertEqual(st, 200, out[:300])
            time.sleep(1.5)                 # past PAIR_TTL (2 s) twice: the list is read again
        self.assertEqual(listed(), before, "the other box's list reads wrote lines in its journal")

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

    def test_the_ollama_and_sagemaker_routes_go_to_the_box_that_serves_too(self):
        # they stayed on this box, whose model answered under the other box's name (review, 2026-11-03)
        for route in ("/api/chat", "/api/generate", "/invocations"):
            with self.subTest(route=route):
                st, out, _ = self.call(route, self.chat("qwen3.8-flash-next-fp4"))
                self.assertEqual(st, 200, out[:300])
                self.assertIn(b"from spark2", out)
                self.assertEqual(self.E2.seen[-1][:2], (route, "qwen3.8-flash-next-fp4"))
        self.assertEqual(self.E1.seen, [])

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

    def test_either_box_is_an_entry(self):
        # decision 973: a pair is mutual, and a client of spark2 that asks for the 27B gets it from spark1
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=self.port2, key=KEY_2)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertEqual([s[1:3] for s in self.E1.seen], [("qwen3.8-27b", f"Bearer {KEY_1}")])
        self.assertEqual(self.E2.seen, [])
        st, rows = self.models_once_known(port=self.port2, key=KEY_2)
        self.assertEqual(rows, {"qwen3.8-flash-next-fp4": "spark2", "qwen3.8-27b": "spark1"})

    # ---- a lane no box serves ---------------------------------------------------------------------
    def test_a_lane_no_box_serves_is_answered_here_as_on_a_box_alone(self):
        # decision 986 (2026-11-03): Claude Code, pi and Hermes as installed name lanes that are often not loaded,
        # and a box alone answers any name with its lane; the 404 of 961 made them fail once the boxes were paired
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertEqual([m for _, m, _, _ in self.E1.seen], ["kolibri-1"])
        self.assertEqual(self.E2.seen, [], "never sent to the other box")
        self.assertTrue(self.until_in_journal("spark1", "no box of the pair serves kolibri-1 now"))

    def test_the_anthropic_dialect_of_a_lane_no_box_serves_is_answered_here_too(self):
        st, out, _ = self.call("/v1/messages", self.chat("ornith-1.5-122b"))
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(json.loads(out)["type"], "message")
        self.assertIn(b"from spark1", out)

    def test_a_peer_that_switched_since_its_list_was_read_never_serves_the_wrong_model(self):
        st, _, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertEqual(st, 200)
        self.E2.served[:] = ["ornith-1.5-122b"]      # the peer switched; the entry's list is up to 5 s old
        self.E2.seen.clear()
        # the peer's own proxy keeps its engine's names 600 s: a real switch restarts it, here its cache is the
        # stale side, so the peer is given a fresh proxy that reads the new name, with spark2's certificate
        home2b = self.home(self.tmp / "spark2b", KEY_2, None, who="spark2")
        self.start_proxy(home2b, self.engines[1], "spark2b")
        cfg2b = home2b / ".config/qwen38"
        self.write_pair(cfg2b, "spark2b", {"name": "spark2", "peers": [peer_entry(cfg2b, "spark1", self.pport1)]})
        self.repoint(self.pair_port("spark2b"))
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertEqual(st, 200, out[:300])           # served by no box now: this box's lane answers it (986)
        self.assertIn(b"from spark1", out)
        self.assertEqual(self.E2.seen, [], "the peer's engine is never handed the old model's request")
        st, out, _ = self.call("/v1/chat/completions", self.chat("ornith-1.5-122b"))
        self.assertEqual(st, 200, out[:300])

    def test_a_hop_header_on_the_client_port_is_no_hop(self):
        # a hop is a request that came in on the pair port, from the box of its certificate: the header that
        # said it before let any key holder have a request refused and the journal name a box that sent nothing
        # (found in review, 2026-11-03)
        said = self.journal("spark1").count("sent on by spark9")
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"),
                               headers={"X-Alzeo-Pair-Hop": "spark9"})
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "sent on to the box that serves it, as any request")
        self.assertEqual(self.journal("spark1").count("sent on by spark9"), said)

    def test_a_peer_that_does_not_answer_is_said_at_once(self):
        self.repoint(localports.held(self))        # bound, never listening: refused, as a box that is gone
        t0 = time.monotonic()
        st, out, _hdrs = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(st, 503, out[:300])        # the box that may serve it does not answer: try again
        self.assertEqual(_hdrs.get("Retry-After"), "30", "every 503 of this proxy says when to try again")
        err = json.loads(out)["error"]
        self.assertEqual(err["type"], "engine_unavailable")
        self.assertIn("does not answer", err["served"]["spark2"])
        self.assertIn("nothing listens on its pair port", err["served"]["spark2"])

    # ---- the list, the key, a box alone ---------------------------------------------------------
    def models_once_known(self, port=None, key=KEY_1):
        """GET /v1/models until the peer's list, read in the background, is in it (3 s at most)."""
        deadline = time.monotonic() + 3
        while True:
            st, out, _ = self.call("/v1/models", port=port, key=key, method="GET")
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
        entry = self.spare_proxy("frozen-list", KEY_1, peers=[("spark2", tls_peer(self, Frozen))])
        for _ in range(3):
            t0 = time.monotonic()
            st, out, _ = self.call("/v1/models", port=entry, method="GET")
            self.assertLess(time.monotonic() - t0, 2, "needle.sh and bench-matrix.sh give it 5 s")
            self.assertEqual(st, 200)

    def test_no_secret_reaches_a_journal(self):
        self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=self.port2, key=KEY_2)
        for label in ("spark1", "spark2"):
            text = self.journal(label)
            for secret in (KEY_1, KEY_2, "PRIVATE KEY", (ident("spark1")[2] / "key.pem").read_text().splitlines()[1],
                           (ident("spark2")[2] / "key.pem").read_text().splitlines()[1]):
                self.assertNotIn(secret, text, label)
        self.assertIn("-> pair spark2 for qwen3.8-flash-next-fp4", self.journal("spark1"))
        self.assertIn("-> pair spark1 for qwen3.8-27b", self.journal("spark2"))

    def test_no_key_or_cookie_crosses_the_pair(self):
        # the certificate is the pair's only credential (decision 978): the client's key never goes to the other
        # box, nor a cookie either way (cookies know no port: this box's cockpit cookie would have gone)
        class Recording(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            seen: list = []

            def log_message(self, *a):
                pass

            def reply(self, obj):
                out = json.dumps(obj).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "session=from-the-other-box")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_GET(self):
                Recording.seen.append(("GET", {k.lower(): v for k, v in self.headers.items()}))
                self.reply({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Recording.seen.append(("POST", {k.lower(): v for k, v in self.headers.items()}))
                self.reply({"id": "c", "choices": [{"message": {"content": "recorded"}}]})
        entry = self.spare_proxy("recorded", KEY_1, peers=[("spark2", tls_peer(self, Recording))])
        st, out, hdrs = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=entry,
                                  headers={"Cookie": "qwen38_session=this-box-cockpit", "X-Api-Key": KEY_1,
                                           "Proxy-Authorization": "Basic eA=="})
        self.assertEqual(st, 200, out[:300])
        self.assertNotIn("set-cookie", {k.lower() for k in hdrs}, "no cookie of the other box set here")
        self.assertEqual([m for m, _ in Recording.seen].count("POST"), 1)
        for _, h in Recording.seen:
            for name in ("authorization", "x-api-key", "cookie", "proxy-authorization", "x-alzeo-pair-hop"):
                self.assertNotIn(name, h)
            self.assertNotIn(KEY_1, json.dumps(h))

    def test_the_pair_port_takes_the_pairs_routes_only(self):
        for method, path in (("GET", "/server_info"), ("GET", "/get_server_info"), ("POST", "/v1/systemone"),
                             ("POST", "/generate"), ("PUT", "/v1/chat/completions"), ("GET", "/health"),
                             ("POST", "/proxy/stop"), ("GET", "/metrics")):
            with self.subTest(route=f"{method} {path}"):
                st, out, _ = self.tls_call(self.pport1, path, {} if method != "GET" else None, method=method)
                self.assertEqual(st, 403, out[:300])
                self.assertIn("takes the generation routes and GET /v1/models only", json.loads(out)["error"]["message"])
        self.assertEqual(self.E1.seen, [], "none reached the engine")
        st, out, _ = self.tls_call(self.pport1, "/v1/messages/count_tokens", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])

    def test_a_box_without_pair_json_is_what_it_was(self):
        alone = self.home(self.tmp / "alone", KEY_1, None)
        port = self.start_proxy(alone, self.engines[0], "alone")
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=port)
        self.assertEqual(st, 200, out[:300])           # any name reaches the lane that serves, as before
        st, out, _ = self.call("/v1/models", port=port, method="GET")
        self.assertEqual([r.get("box") for r in json.loads(out)["data"]], [None])
        self.assertNotIn("pair: port", self.journal("alone"), "no pair port on a box alone")

    def test_a_pair_json_without_a_peer_is_a_box_alone(self):
        # `pair.py name` alone writes one: a lane name this box does not serve was a 404 (found in review, 2026-11-02)
        named = self.home(self.tmp / "named-only", KEY_1, {"name": "spark1", "peers": []})
        port = self.start_proxy(named, self.engines[0], "named-only")
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=port)
        self.assertEqual(st, 200, out[:300])
        self.assertNotIn("pair: port", self.journal("named-only"))

    def test_a_pair_json_that_does_not_parse_leaves_the_box_alone(self):
        broken = self.home(self.tmp / "broken", KEY_1, None)
        (broken / ".config/qwen38/pair.json").write_text('{"peers": [{"name": "x"}]}')
        port = self.start_proxy(broken, self.engines[0], "broken")
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=port)
        self.assertEqual(st, 200, out[:300])
        self.assertRegex(self.journal("broken"), r"pair: .*pair\.json is not used: peer 0 needs")

    # ---- the pair port: TLS both ways (decision 971) --------------------------------------------
    def test_a_request_on_the_pair_port_is_a_hop_from_the_box_of_its_certificate(self):
        # no X-Alzeo-Pair-Hop: the certificate says spark2 sent it, so it is served here or refused, never sent on
        st, out, _ = self.tls_call(self.pport1, "/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertEqual(st, 409, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "model_not_served")
        self.assertEqual(self.E1.seen + self.E2.seen, [], "never sent back to spark2, never served by the 27B")
        st, out, _ = self.tls_call(self.pport1, "/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        st, out, _ = self.tls_call(self.pport1, "/v1/models", method="GET")
        self.assertEqual([r["id"] for r in json.loads(out)["data"]], ["qwen3.8-27b"], "its own list, not the union")
        self.assert_said("spark1", "sent on by spark2")

    def test_the_other_boxs_rows_and_summary_are_rebuilt_from_their_fields(self):
        # nested 9,990 deep, a row parsed and then made every GET /v1/models here drop when written back, and a
        # summary froze every view of the cockpit (review, 2026-11-03)
        deep = "[" * 9990 + "]" * 9990

        class Deep(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/pair/cockpit/state"):
                    out = ('{"name": "spark2", "models": ["qwen3.8-flash-next-fp4"], "job": {"action": ' + deep
                           + '}, "extra": 1}').encode()
                else:
                    out = ('{"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4", "owned_by": "sglang", '
                           '"nested": ' + deep + '}]}').encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        port = self.spare_proxy("deep", KEY_1, peers=[("spark2", tls_peer(self, Deep))])
        st, rows = self.models_once_known(port=port)
        self.assertEqual((st, rows.get("qwen3.8-flash-next-fp4")), (200, "spark2"))
        st, out, _ = self.call("/v1/models", port=port, method="GET")
        row = [r for r in json.loads(out)["data"] if r["id"] == "qwen3.8-flash-next-fp4"][0]
        self.assertEqual(set(row), {"id", "owned_by", "box"}, "its scalar fields alone")
        st, out, _ = self.call("/proxy/pair/spark2/state", port=port, method="GET")
        self.assertEqual(st, 200, out[:200])
        s = json.loads(out)
        self.assertEqual((s["models"], s["job"]["action"], "extra" in s), (["qwen3.8-flash-next-fp4"], None, False))

    def test_an_ndjson_stream_from_the_other_box_arrives_as_it_comes(self):
        # the Ollama routes stream NDJSON, and the entry read every answer but SSE whole (review, 2026-11-03)
        class Ollama(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
                for line, pause in ((b'{"message": {"content": "one"}, "done": false}\n', 2.0),
                                    (b'{"message": {"content": "two"}, "done": true}\n', 0.0)):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line)); self.wfile.flush()
                    time.sleep(pause)
                self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
        port = self.spare_proxy("ndjson", KEY_1, peers=[("spark2", tls_peer(self, Ollama))])
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.addCleanup(c.close)
        t0 = time.monotonic()
        c.request("POST", "/api/chat", json.dumps(self.chat("qwen3.8-flash-next-fp4", stream=True)).encode(),
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        r = c.getresponse()
        first = r.readline()
        self.assertIn(b'"one"', first)
        self.assertLess(time.monotonic() - t0, 1.5, "its first line before its end")
        self.assertIn(b'"two"', r.read())

    def test_an_ndjson_stream_from_the_other_box_waits_out_a_long_prefill(self):
        # NDJSON carries no keepalive: the entry bounded its silence as an SSE stream's, and the long prefill of an Ollama
        # stream on the other box was cut and bound 10 minutes (the 5th review of v1.43.0, 2026-11-04)
        class SlowOllama(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
                time.sleep(3.0)                                                 # its prefill, past the 2 s set below
                for line in (b'{"message": {"content": "one"}, "done": false}\n',
                             b'{"message": {"content": "two"}, "done": true}\n'):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line)); self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
        port = self.spare_proxy("ndjson-prefill", KEY_1, peers=[("spark2", tls_peer(self, SlowOllama))],
                                extra={"PAIR_STREAM_SILENCE_S": "2"})
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/api/chat", json.dumps(self.chat("qwen3.8-flash-next-fp4", stream=True)).encode(),
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        r = c.getresponse()
        try:
            body = r.read()
        except http.client.IncompleteRead as e:
            body = e.partial
        self.assertIn(b'"two"', body, "cut during its prefill")

    def test_the_other_boxs_end_by_order_line_never_reaches_an_ollama_client(self):
        # decision 1031's line on the Ollama routes is the entry's to read: relayed, ollama-python's ChatResponse(**part)
        # failed on a line with no message (the 4th review of v1.43.0, 2026-11-04)
        class EndsByOrder(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
                for line in (b'{"message": {"content": "one"}, "done": false}\n', HOP_ENDED_NDJSON):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line)); self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)                      # cut, not ended
        port = self.spare_proxy("ndjson-ended", KEY_1, peers=[("spark2", tls_peer(self, EndsByOrder))])
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/api/chat", json.dumps(self.chat("qwen3.8-flash-next-fp4", stream=True)).encode(),
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        r = c.getresponse()
        got = b""
        try:
            while True:
                b = r.read1(65536)
                if not b:
                    break
                got += b
        except (http.client.HTTPException, OSError):
            pass                                                                # cut, not ended
        self.assertIn(b'"one"', got)
        self.assertNotIn(b"x_alzeo_ended", got, "the other box's line reached the Ollama client")
        self.assert_said("ndjson-ended", "ended it by order, nothing is bound")

    def test_the_other_box_never_names_the_engines_requests_here(self):
        # SGLang aborts every request whose rid starts with one it aborts: the other box naming rids "0" to "f",
        # in a body or in the header, and leaving, ended every generation here (review, 2026-11-03)
        for path in ("/v1/completions", "/v1/chat/completions", "/invocations"):
            with self.subTest(path=path):
                st, out, _ = self.tls_call(self.pport1, path, dict(self.chat("qwen3.8-27b"), rid="0"),
                                           headers={"X-Override-Rid": "0"})
                self.assertEqual(st, 200, out[:300])
                rid, header = self.E1.rids[-1]
                self.assertIsNone(rid, "no rid of the other box's in the body")
                self.assertNotEqual(header, "0", "nor in the header")
                if path == "/v1/chat/completions":
                    self.assertRegex(header or "", r"^[0-9a-f]{32}$", "this box names it")
        # a body in UTF-16, whose bytes show none of its keys, is refused unread (security review, 2026-11-03)
        st, out, _ = self.tls_call(self.pport1, "/v1/completions",
                                   json.dumps(dict(self.chat("qwen3.8-27b"), rid="0")).encode("utf-16"))
        self.assertEqual(st, 415, out[:300])
        # a key written with escapes, which the bytes do not show, and /v1/responses' request_id (security review,
        # 2026-11-03)
        for path, raw, key in (("/v1/completions", b'{"model": "qwen3.8-27b", "prompt": "hi", "r\\u0069d": "0"}', "rid"),
                               ("/v1/responses", b'{"model": "qwen3.8-27b", "input": "hi", "request_id": "0"}',
                                "request_id")):
            with self.subTest(path=path, body=raw):
                st, out, _ = self.tls_call(self.pport1, path, raw)
                self.assertEqual(st, 200, out[:300])
                self.assertNotIn(key, self.E1.bodies[-1], f"the other box's id reached the engine: {self.E1.bodies[-1]}")

    def test_the_cockpits_stop_is_never_taken_from_the_pair_port(self):
        # the other box's connection may come from loopback (a tunnel's end, these tests): POST /proxy/stop is
        # this machine's operator's, never a pair's
        st, out, _ = self.tls_call(self.pport1, "/proxy/stop", {"why": "a pair's stop", "kind": "operator"})
        self.assertEqual(st, 403, out[:300])
        self.assert_said("spark1", "403 pair route refused")

    def test_the_pair_port_refuses_a_box_without_a_pinned_certificate(self):
        for me in (None, "rogue"):
            with self.subTest(me=me), self.assertRaises((ssl.SSLError, ConnectionResetError, BrokenPipeError,
                                                         http.client.RemoteDisconnected)):
                self.tls_call(self.pport1, "/v1/chat/completions", self.chat("qwen3.8-27b"), me=me)
        self.assertEqual(self.E1.seen, [], "not a byte of a request read")
        self.assertRegex(self.journal("spark1"), r"pair port: TLS handshake from 127\.0\.0\.1 failed \(")

    def test_a_certificate_that_chains_to_a_pinned_one_is_not_it(self):
        # the chain ends at spark2's certificate, but the certificate is another: the pin itself is checked
        with self.assertRaises((ssl.SSLError, ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected)):
            self.tls_call(self.pport1, "/v1/chat/completions", self.chat("qwen3.8-27b"), me=child_of("spark2"))
        self.assertEqual(self.E1.seen, [])
        self.assertIn("REFUSED: its certificate is no peer's of this box's pair", self.journal("spark1"))

    def test_an_entry_never_sends_to_a_box_without_the_pinned_certificate(self):
        class Rogue(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            seen: list = []

            def log_message(self, *a):
                pass

            def do_GET(self):
                Rogue.seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

            do_POST = do_GET
        entry = self.spare_proxy("rogue-peer", KEY_1, peers=[("spark2", tls_peer(self, Rogue, me="rogue"))])
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=entry)
        self.assertEqual(st, 503, out[:300])
        self.assertIn("another certificate than the one pinned", json.loads(out)["error"]["served"]["spark2"])
        self.assertEqual(Rogue.seen, [], "neither a request nor the key reaches it")

    def test_a_box_taken_out_of_the_pair_is_refused_at_its_next_connection(self):
        # every answer of this proxy ends its connection (Connection: close), so a box taken out of the pair meets
        # the handshake again at its next request; the check of a connection still open is in ThePairsPieces
        st, _, _ = self.tls_call(self.pport1, "/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200)
        cfg1 = self.home1 / ".config/qwen38"
        kept = json.loads((cfg1 / "pair.json").read_text())
        # spark2 out, another box in its place: spark1 stays paired, its pair port open
        self.write_pair(cfg1, "spark1", {"name": "spark1", "peers": [peer_entry(cfg1, "spark3", 9, who="rogue")]})
        self.addCleanup(self.write_pair, cfg1, "spark1", kept)
        with self.assertRaises((ssl.SSLError, ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected)):
            self.tls_call(self.pport1, "/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(len(self.E1.seen), 1, "the second request never reached the engine")

    def test_the_pair_port_opens_with_a_pair_and_closes_without(self):
        home = self.home(self.tmp / "opens", KEY_1, None, who="spark1")
        self.start_proxy(home, self.engines[0], "opens")            # alone: no pair port
        proc = self.procs[-1]
        self.assertNotIn("pair: port", self.journal("opens"))
        cfg = home / ".config/qwen38"
        self.write_pair(cfg, "opens", {"name": "spark1", "peers": [peer_entry(cfg, "spark2", self.pport2)]})
        port = self.pair_port("opens", within=5)
        self.assertIn(port, localports._ports_of(proc.pid))
        st, out, _ = self.tls_call(port, "/v1/models", method="GET")  # spark2's certificate gets past
        self.assertEqual(st, 200, out[:300])
        (cfg / "pair.json").unlink()
        self.assertTrue(self.until_in_journal("opens", f"pair: port {port} closed: this box has no pair now", 8))
        # what it listens on, read from /proc: no connection to a port number another program may hold now
        self.assertNotIn(port, localports._ports_of(proc.pid))

    def test_a_certificate_that_is_not_the_one_pinned_leaves_the_box_alone(self):
        for label, said in (("pin-off", "is not the certificate pair.json pins"),
                            ("no-identity", "has no certificate of its own")):
            with self.subTest(label=label):
                home = self.home(self.tmp / label, KEY_1, None, who="spark1")
                cfg = home / ".config/qwen38"
                e = peer_entry(cfg, "spark2", self.pport2)
                if label == "pin-off":
                    e["sha256"] = ident("rogue")[1]
                else:
                    shutil.rmtree(cfg / "pair/identity")
                (cfg / "pair.json").write_text(json.dumps({"name": "spark1", "peers": [e]}))
                port = self.start_proxy(home, self.engines[0], label)  # a pair it does not take: one port
                st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=port)
                self.assertEqual(st, 200, out[:300])                # a box alone: any name reaches its lane
                self.assertIn("pair.json is not used: ", self.journal(label))
                self.assertIn(said, self.journal(label))
                self.assertEqual(self.E2.seen, [])

    def test_an_entrys_client_that_leaves_ends_the_request_on_the_other_box(self):
        # through TLS the other box sees the entry's connection end (no peek was made through TLS before: its
        # engine generated to the end for nobody), the answer not streamed or streamed
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.E2.delay = 8.0 + PAIR_GRACE_READ_S
                gone = self.journal("spark2").count("CLIENT GONE")
                c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=60)
                t0 = time.monotonic()
                c.request("POST", "/v1/chat/completions",
                          json.dumps(self.chat("qwen3.8-flash-next-fp4", stream=stream)).encode(),
                          {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
                time.sleep(1.5)
                c.close()                           # the client leaves before the answer
                # once the seconds the entry reads on for a death are over (decision 1112)
                deadline = time.monotonic() + 5.5 + PAIR_GRACE_READ_S
                while self.journal("spark2").count("CLIENT GONE") <= gone and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertGreater(self.journal("spark2").count("CLIENT GONE"), gone, self.journal("spark2")[-600:])
                self.assertLess(time.monotonic() - t0, 7.5 + PAIR_GRACE_READ_S, "seen before its engine answered")

    def test_an_answer_past_the_cap_is_refused(self):
        # an answer not streamed was read whole: a box gone wrong could fill this one's memory (review, 2026-11-03)
        class Huge(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, out):
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_GET(self):
                self.reply(json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode())

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.reply(b'{"pad": "' + b"x" * 20000 + b'"}')
        port = self.spare_proxy("huge", KEY_1, peers=[("spark2", tls_peer(self, Huge))], extra={"MAX_BODY_BYTES": "8192"})
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 502, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "upstream_too_large")
        self.assert_said("huge", "502 pair answer too large")

    def test_an_error_answer_is_relayed_whole_up_to_the_cap_and_refused_past_it(self):
        # an error answer over 1 MiB went out cut to 1 MiB as if whole: a validation error that echoes a long
        # prompt does that (review, 2026-11-03)
        class BigError(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, code, out):
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_GET(self):
                self.reply(200, json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode())

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.reply(400, json.dumps({"error": {"message": "x" * 1_300_000}}).encode())
        port = self.spare_proxy("big-error", KEY_1, peers=[("spark2", tls_peer(self, BigError))])
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 400, out[:300])
        self.assertEqual(len(json.loads(out)["error"]["message"]), 1_300_000, "the whole answer")
        port = self.spare_proxy("big-error-capped", KEY_1, peers=[("spark2", tls_peer(self, BigError))],
                                extra={"MAX_BODY_BYTES": "8192"})
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 502, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "upstream_too_large")

    def test_an_error_answer_the_other_box_cuts_is_said_cut(self):
        # its error answer cut on the way escaped the relay before (review, 2026-11-03)
        class CutError(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(500); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "400"); self.end_headers()
                self.wfile.write(b'{"error": "half'); self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
        port = self.spare_proxy("cut-error", KEY_1, peers=[("spark2", tls_peer(self, CutError))])
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 502, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "upstream_cut")
        self.assert_said("cut-error", "UPSTREAM CUT pair")

    def test_an_answer_the_other_box_cuts_is_said_cut(self):
        # a 200 whose body ended before its Content-Length went to the client whole, half a document as a
        # complete one: http.client's read(n) says nothing of a body cut short (found by the tests, 2026-11-03)
        class CutAnswer(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "400"); self.end_headers()
                self.wfile.write(b'{"choices": [{"message": {"content": "half'); self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
        port = self.spare_proxy("cut-answer", KEY_1, peers=[("spark2", tls_peer(self, CutAnswer))])
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 502, out[:300])
        self.assertEqual(json.loads(out)["error"]["type"], "upstream_cut")
        self.assert_said("cut-answer", "UPSTREAM CUT pair")

    def test_a_certificate_whose_dates_this_clock_does_not_hold_still_pairs(self):
        # a new certificate is valid from the second it is made: two boxes whose clocks disagree could not pair
        # (review, 2026-11-03); the pin is the certificate itself, its dates add nothing
        class Lists(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            do_POST = do_GET
        later = future_ident()
        port = self.spare_proxy("clock", KEY_1, pair=lambda cfg: {"name": "spark1", "peers": [
            peer_entry(cfg, "spark2", tls_peer(self, Lists, me=later), who=later)]})
        st, out, _ = self.call("/v1/models", port=port, method="GET")
        deadline = time.monotonic() + 3
        while b"qwen3.8-flash-next-fp4" not in out and time.monotonic() < deadline:
            time.sleep(0.2)
            st, out, _ = self.call("/v1/models", port=port, method="GET")
        self.assertIn(b"qwen3.8-flash-next-fp4", out, "the other box's list read: its certificate taken")

    def test_the_tile_reads_the_other_box_through_the_pair(self):
        # decision 975: this box's cockpit asks its own proxy; the other box's proxy sums its own cockpit up, logged
        # in with its own key, never relayed whole
        FakeCockpit.logins.clear()
        time.sleep(2.2)                     # past the summary another test may have had kept (PAIR_SUMMARY_KEEP_S)
        FakeCockpit.states.clear()
        lines = (self.journal("spark1").count("pair/spark2/state"), self.journal("spark2").count("/pair/cockpit/state"))
        for _ in range(2):
            st, out, _ = self.call("/proxy/pair/spark2/state", method="GET")
            self.assertEqual(st, 200, out[:300])
        # a read comes every 5 s on both boxes: its lines pushed the real requests out of the 800 the cockpit reads
        # in 34 minutes (review, 2026-11-03)
        time.sleep(0.3)
        self.assertEqual((self.journal("spark1").count("pair/spark2/state"),
                          self.journal("spark2").count("/pair/cockpit/state")), lines, "a read of the tile writes no line")
        s = json.loads(out)
        self.assertEqual(s["name"], "spark2")
        self.assertEqual(s["models"], ["qwen3.8-flash-next-fp4"])
        self.assertEqual((s["mem_total"], s["mem_available"], s["cpu_pct"]), (121 * GIB, 100 * GIB, 12.5))
        self.assertEqual((s["gpu_temp_c"], s["gpu_power_w"], s["version"]), (51.0, 40.5, "1.41.0"))
        self.assertEqual(s["job"], {"action": "switch", "status": "running", "elapsed": 12.0})
        for kept_home in ("feed", "repo_dir", "argv", "secret log", "10.0.0.9", "disks"):
            self.assertNotIn(kept_home, out.decode(), "its feed, paths and logs stay on the other box")
        self.assertEqual(FakeCockpit.logins, [KEY_2], "logged in once, with that box's own key, its session kept")
        # the other box could ask for it as fast as it liked, each time a read of this cockpit (review, 2026-11-03)
        self.assertEqual(len(FakeCockpit.states), 1, "two reads within PAIR_SUMMARY_KEEP_S: one read of the cockpit")

    def test_the_tiles_route_is_this_machines_alone(self):
        st, out, _ = self.tls_call(self.pport1, "/proxy/pair/spark2/state", method="GET")
        self.assertEqual(st, 403, "never from the other box: " + out[:200].decode(errors="replace"))
        st, out, _ = self.call("/proxy/pair/spark2/state", method="GET", key="not-this-box")
        self.assertEqual(st, 401, out[:300])
        st, out, _ = self.call("/proxy/pair/spark9/state", method="GET")
        self.assertEqual(st, 404, out[:300])
        self.assertIn("names no box of this box's pair", json.loads(out)["error"]["message"])
        st, out, _ = self.tls_call(self.pport1, "/pair/cockpit/state", method="GET")
        self.assertEqual(st, 200, "the summary itself is the other box's to read")

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

    def test_a_box_never_read_holds_no_lane_past_its_first_read(self):
        # the entry restarted while the other box stays off (a power cut): every lane it does not load got a 503
        # for as long as that box was off (review, 2026-11-03); past its first read, it holds nothing up (986)
        port = self.spare_proxy("first-read", KEY_1, peers=[("spark2", localports.held(self))],
                                extra={"PAIR_FIRST_READ_S": "1"})
        time.sleep(1.5)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)

    def test_a_box_that_went_off_holds_its_lanes_for_a_while_then_none(self):
        class Going(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            up = True

            def log_message(self, *a):
                pass

            def reply(self, code, obj):
                out = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_GET(self):
                if not Going.up:
                    return self.reply(503, {"error": "its lane is loading"})
                self.reply(200, {"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.reply(200, {"id": "c", "choices": [{"message": {"content": "from going"}}]})
        port = self.spare_proxy("known-wait", KEY_1, peers=[("spark2", tls_peer(self, Going))],
                                extra={"PAIR_KNOWN_WAIT_S": "3", "PAIR_TTL_S": "0.5"})
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertIn(b"from going", out)
        Going.up = False
        time.sleep(1)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 503, out[:300])           # it served it at its last good read: try again
        time.sleep(3.5)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 200, out[:300])           # off for longer than a switch: this box's lane answers it
        self.assertIn(b"from spark1", out)

    def test_a_box_that_lists_a_model_and_refuses_it_twice_gets_a_503_never_this_boxs_answer(self):
        # docs/pair.md promises that a box never answers the other box's model with its own: a 409 twice from a
        # box whose list says it serves it fell through to this box's lane (review, 2026-11-03)
        class Lagging(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            posts: list = []

            def log_message(self, *a):
                pass

            def reply(self, code, obj):
                out = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

            def do_GET(self):
                self.reply(200, {"object": "list", "data": [{"id": "qwen3.8-flash-next-fp4"}]})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Lagging.posts.append(1)
                self.reply(409, {"error": {"type": "model_not_served", "message": "another model here now"}})
        port = self.spare_proxy("twice-409", KEY_1, peers=[("spark2", tls_peer(self, Lagging))])
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 503, out[:300])
        self.assertIn("refused it twice", json.loads(out)["error"]["message"])
        self.assertNotIn(b"from spark1", out)
        self.assertEqual(len(Lagging.posts), 2)

    def test_a_lane_the_box_that_does_not_answer_never_served_is_answered_here(self):
        # decision 986 holds while the other box is off: only a model it served at its last good read waits
        # for it (503 above); a client set up for a lane loaded nowhere is answered here, as on a box alone
        self.E2.mode = "down"
        try:
            time.sleep(PAIR_TTL + 0.5)
            st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"))
            self.assertEqual(st, 200, out[:300])
            self.assertIn(b"from spark1", out)
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
        self.assert_said("spark1", "UPSTREAM CUT pair")

    def test_a_request_sent_on_to_a_box_whose_engine_restarts_is_told_to_try_again_not_held(self):
        # a box whose engine does not answer while its unit starts again (a switch): its own clients are held for
        # whatever lane comes back (role 6), but a request the other box sent on for one model is told at once to
        # try again, since the lane that comes back may serve another
        t = self.tmp / "restarting"
        (t / "bin").mkdir(parents=True)
        (t / "bin/systemctl").write_text(
            "#!/bin/sh\n"
            'case "$*" in *InvocationID*) echo inv-1; echo; exit 0;; esac\n'
            'if [ "$1" = show ]; then echo activating; exit 0; fi\n'
            'if [ "$1" = list-jobs ]; then exit 0; fi\n'
            "exit 3\n")
        (t / "bin/systemctl").chmod(0o755)
        home = self.home(t / "home", KEY_2, None, who="spark2")
        gone = localports.held(self)                      # its engine: nothing listens there
        env = {"PATH": f"{t / 'bin'}:{os.environ['PATH']}", "HOME": str(home), "PROXY_BIND": "127.0.0.1",
               "PAIR_PORT": "0", "UPSTREAM": f"http://127.0.0.1:{gone}", "PROMPT_CEILING_TOKENS": "0",
               "FLASH_PROMPT_CEILING_TOKENS": "0", "PROXY_HOLD_UNITS": "qwen38-x.service", "PROXY_HOLD_MAX_S": "20",
               "PROXY_HOLD_POLL_S": "0.2"}
        with open(self.tmp / "restarting.journal", "ab") as journal:
            proc, _port = localports.spawn(lambda port: subprocess.Popen(
                [sys.executable, str(REPO / "keepalive-proxy.py"), str(port)], env=env,
                stdout=subprocess.DEVNULL, stderr=journal))
        self.procs.append(proc)
        cfg = home / ".config/qwen38"
        self.write_pair(cfg, "restarting", {"name": "spark2", "peers": [peer_entry(cfg, "spark1", 9)]})
        t0 = time.monotonic()
        st, out, _ = self.tls_call(self.pair_port("restarting"), "/v1/chat/completions", self.chat("qwen3.8-27b"),
                                   me="spark1", server="spark2")
        self.assertLess(time.monotonic() - t0, 12, "not held up to PROXY_HOLD_MAX_S")
        self.assertEqual(st, 503, out[:300])
        self.assertIn("try again", json.loads(out)["error"]["message"])

    def test_a_peer_that_takes_the_connection_and_never_answers_holds_nobody(self):
        class Frozen(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                time.sleep(40)                      # a proxy that hangs: the connection is taken, no answer

            do_POST = do_GET
        self.repoint(tls_peer(self, Frozen))
        t0 = time.monotonic()
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"))
        self.assertLess(time.monotonic() - t0, 20, "two reads of its list, 5 s each, then the answer")
        self.assertEqual(st, 503, out[:300])
        t0 = time.monotonic()
        st, out, _ = self.call("/v1/models", method="GET")
        self.assertLess(time.monotonic() - t0, 12)
        self.assertEqual([r["id"] for r in json.loads(out)["data"]], ["qwen3.8-27b"])

    def test_two_boxes_that_name_each_other_ask_each_other_once(self):
        a = self.home(self.tmp / "sym-a", KEY_1, None, who="sym-a")
        b = self.home(self.tmp / "sym-b", KEY_2, None, who="sym-b")
        # an engine of its own each, whose list reads say what each box asked: the other box's reads write no line
        # since v1.43.0, so the journals counted nothing for sym-b, and a loop would have gone unseen (review,
        # 2026-11-03)
        ha, hb = fake_engine("sym-a", "qwen3.8-27b"), fake_engine("sym-b", "qwen3.8-flash-next-fp4")
        engines = [Engine(("127.0.0.1", 0), h) for h in (ha, hb)]
        for e in engines:
            threading.Thread(target=e.serve_forever, daemon=True).start()
            self.addCleanup(e.server_close)
            self.addCleanup(e.shutdown)
        pa = self.start_proxy(a, engines[0], "sym-a")
        self.start_proxy(b, engines[1], "sym-b")
        self.pair_up(a, "sym-a", b, "sym-b")
        st, rows = self.models_once_known(port=pa)
        self.assertEqual(st, 200)
        self.assertEqual(rows, {"qwen3.8-27b": "sym-a", "qwen3.8-flash-next-fp4": "sym-b"})
        reads = {"sym-a": ha, "sym-b": hb}
        count = lambda label: sum(1 for p in list(reads[label].gets) if p.startswith("/v1/models"))
        # each box reads the other's list once when the pair loads, and under load that read can land after the
        # client's last GET (the single-process CI step, 2026-11-03): the counts settle first; a loop never would
        before, still_since, deadline = None, None, time.monotonic() + 15
        while time.monotonic() < deadline:
            now = (count("sym-a"), count("sym-b"))
            if now != before:
                before, still_since = now, time.monotonic()
            elif time.monotonic() - still_since >= 1.5:
                break
            time.sleep(0.25)
        time.sleep(2)                       # a loop would go on asking with nobody calling
        self.assertEqual((count("sym-a"), count("sym-b")), before, "no GET goes on once the client stopped")
        self.assertLessEqual(before[1], before[0], "each GET of the client reads the peer's own list once at most")
        st, out, _ = self.call("/v1/chat/completions", self.chat("kolibri-1"), port=pa)
        self.assertEqual(st, 200, out[:300])           # served by no box: sym-a's own lane answers it (986)
        st, out, _ = self.tls_call(self.pair_port("sym-b"), "/v1/models", me="sym-a", server="sym-b", method="GET")
        self.assertEqual([r["id"] for r in json.loads(out)["data"]], ["qwen3.8-flash-next-fp4"],
                         "a box asked over the pair says what it serves itself, not the union it shows its clients")

    # ---- what the code review of 2026-11-02 found -------------------------------------------------
    def test_a_box_that_cannot_read_its_own_key_sends_nothing_on(self):
        port = self.spare_proxy("no-key", KEY_1, peers=[("spark2", self.pport2)], keyfile=False)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port, key="anything")
        self.assertEqual(st, 503, out[:300])
        self.assertEqual(self.E2.seen, [], "the peer is never sent a request this box could not check")

    def test_a_pair_json_with_a_bad_port_or_name_leaves_the_box_alone(self):
        for label, pair in (("bad-port", lambda cfg: {"name": "spark1", "peers": [
                                dict(peer_entry(cfg, "spark2", 1), proxy="https://127.0.0.1:3000l")]}),
                            ("http-peer", lambda cfg: {"name": "spark1", "peers": [
                                dict(peer_entry(cfg, "spark2", 1), proxy=f"http://127.0.0.1:{self.port2}")]}),
                            ("bad-name", lambda cfg: {"name": "Spark \u2460 bureau", "peers": []})):
            with self.subTest(label=label):
                port = self.spare_proxy(label, KEY_1, pair=pair)
                st, out, _ = self.call("/v1/models", port=port, method="GET")
                self.assertEqual(st, 200, out[:300])
                self.assertEqual([r.get("box") for r in json.loads(out)["data"]], [None], "a box alone")
                self.assertIn("pair.json is not used", self.journal(label))
        self.assertIn("proxy is https://host:port", self.journal("http-peer"), "never in clear between two boxes")

    def test_an_entry_whose_engine_does_not_answer_says_so_on_v1_models(self):
        port = self.spare_proxy("entry-down", KEY_1, peers=[("spark2", self.pport2)],
                                upstream=f"http://127.0.0.1:{localports.held(self)}")
        st, out, _ = self.call("/v1/models", port=port, method="GET")
        self.assertEqual(st, 503, "as a box alone: no peer's model stands in for this box's engine")

    def test_no_proxy_of_the_environment_gets_the_peers_key(self):
        dead = f"http://127.0.0.1:{localports.held(self)}"
        port = self.spare_proxy("with-proxy-env", KEY_1, peers=[("spark2", self.pport2)],
                                extra={"http_proxy": dead, "HTTP_PROXY": dead, "https_proxy": dead, "HTTPS_PROXY": dead})
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
        port = self.spare_proxy("switch-409", KEY_1, peers=[("spark2", tls_peer(self, Switched))],
                                extra={"PAIR_TTL_S": "300"})
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 200, out[:300])          # its list is read and kept 300 s
        Switched.models = ["ornith-1.5-122b"]         # the peer switches; the entry's list is the old one
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=port)
        self.assertEqual(st, 200, out[:300])          # its 409 had every list read again: served by no box now,
        self.assertIn(b"from spark1", out)            # so this box's lane answers it, as on a box alone (986)
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
        port = self.spare_proxy("silent", KEY_1, peers=[("spark2", tls_peer(self, Silent))],
                                extra={"PAIR_STREAM_SILENCE_S": "2"})
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
        # a box of a pair (its peer gone): a pair.json without a peer is a box alone now
        port = self.spare_proxy("held-box", KEY_2, peers=[("spark1", localports.held(self))], name="spark2",
                                who="spark2", upstream=f"http://127.0.0.1:{engine_port}",
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


class ThePairsPieces(unittest.TestCase):
    """The pair's functions one by one, in this process: what pair.json may say, the TLS made of it, how a
    peer's list is read and kept, the hop header, the union of /v1/models, the pair port and its keeper."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="kp-pair-pieces-"))
        cls.cfg = cls.tmp / ".config/qwen38"
        cls.cfg.mkdir(parents=True)
        install_identity(cls.cfg, "spark1")
        cls.pair_file = cls.cfg / "pair.json"
        saved = dict(os.environ)
        os.environ.update({"HOME": str(cls.tmp), "QWEN38_PAIR_FILE": str(cls.pair_file), "PAIR_TTL_S": "30",
                           "PAIR_PORT": "0", "PROXY_BIND": "127.0.0.1"})
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

    def setUp(self):
        self.pair_file.unlink(missing_ok=True)
        self.mod._PAIR.update(seen=0.0, stamp=None, conf=None)

    def parse(self, conf):
        return self.mod._pair_parse(json.dumps(conf))

    def peer(self, name="spark2", proxy="https://127.0.0.1:30002", who="spark2"):
        return {"name": name, "proxy": proxy, "cert_file": f"pair/{name}.crt", "sha256": ident(who)[1]}

    def conf_now(self, conf):
        """conf written as pair.json and read by the proxy at once."""
        self.pair_file.write_text(json.dumps(conf))
        self.mod._PAIR["seen"] = 0.0
        return self.mod.pair_conf()

    def _relayed_behind_a_frozen_client(self, n, fp=None, meanwhile=None, cut=True, tail=(), freeze=0.0,
                                        path="/v1/chat/completions", hop=None, frozen_at=50, until=None,
                                        need_freeze=True, eof_delay=0.0, settle=None, abort=None, real_conn=False,
                                        pace=0.0,
                                        event=b'data: {"choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'):
        """A stream of n events relayed by a bare handler, then the event of tail if any, then its engine's cut (or its
        end, cut=False). Its client is frozen once, at its 51st write, until the pump has read all of it and the proxy
        took the stream out of every stop's reach, or 2 s on, and freeze seconds more: the pump never waits for the
        client since v1.44.0, so the whole stream is read during the freeze, however long. meanwhile(h) runs during it,
        and a BrokenPipeError it raises is the client leaving. frozen_at and until: another write to freeze at, and
        what the freeze waits for instead (until(h)). eof_delay: seconds between the last event and the stream's end,
        so that only what the pump reads in the event itself takes the stream out of reach; settle: the freeze waits
        for the last event read and that many seconds more, not for the stream out of reach, which an end delayed less
        than its 10 s would bring anyway. A binding's deferred reads of systemd are counted in
        self.later, not run: run, they outlived the test and took the next one's systemd answers (the 15th review of
        v1.43.0). What the relay ended with; what the client got is in self.relayed. The relay gives up past 20 s of
        silence: a test whose end never comes fails, never hangs for the hour of MAX_SILENCE_S. tail: records read one
        by one after the n events. abort: the upstream's abort (True: told); self.order says when each abort returned
        and each close of the upstream came. real_conn: the client's connection a real socket, whose writes fail once
        its TCP side is shut; self.finish_at: how many writes had gone when its stream was ended."""
        m = self.mod
        got, writes, read_all, frozen, errors = [], [], threading.Event(), [], []
        self.relayed, self.later, self.order, self.finish_at = writes, [], [], []
        if real_conn:
            mine, peer = socket.socketpair()
            self.addCleanup(mine.close)
            self.addCleanup(peer.close)
            peer.setblocking(False)

        def shut():
            # its TCP side shut (shutdown from any thread): its peer reads its end
            try:
                return real_conn and peer.recv(1) == b""
            except BlockingIOError:
                return False
        silent = m.MAX_SILENCE_S
        m.MAX_SILENCE_S = 20
        self.addCleanup(setattr, m, "MAX_SILENCE_S", silent)

        class Resp:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self):
                self.n = 0

            def read1(self, size):
                self.n += 1
                if self.n <= n:
                    if pace:
                        time.sleep(pace)            # an engine that sends at its pace (pace: seconds an event)
                    return event
                if self.n <= n + len(tail):
                    if self.n == n + len(tail):
                        read_all.set()
                    return tail[self.n - n - 1]
                read_all.set()
                time.sleep(eof_delay)
                if not cut:
                    return b""                                          # the engine's stream ends
                raise http.client.IncompleteRead(b"", 10)              # the engine dies: its stream cut

            def close(self):
                order.append("close")

        order = self.order

        class Conn:
            def shutdown(self, how):
                got.append("shutdown")

        def chunk(b):
            if len(writes) == frozen_at and not frozen:
                # the effect waited for, not a time: the pump may parse what it read later than read1 returned it
                ready = (lambda: until(h)) if until else (lambda: read_all.is_set() and h not in m._GENERATIONS)
                if settle is not None:
                    read_all.wait(10)
                    time.sleep(settle)
                    ready = read_all.is_set
                deadline = time.monotonic() + 10
                while not ready() and time.monotonic() < deadline:
                    time.sleep(0.01)
                frozen.append(bool(ready()))       # what the test waited for came while its client read nothing
                if meanwhile:
                    try:
                        meanwhile(h)
                    except BrokenPipeError:
                        raise                       # the client leaves
                    except Exception as e:          # noqa: BLE001 - said after the relay, not taken for a client gone
                        errors.append(e)
                time.sleep(freeze)
            if shut():
                raise BrokenPipeError("its connection was shut")
            writes.append(b)
        h = object.__new__(m.H)
        self.h = h
        h.path, h._t0, h._first, h._last, h._bytes = path, time.time(), None, None, 0
        h._stop_why, h._pair_box, h._killer_fp, h._killer_bound, h._killer_before = None, hop, fp, False, None
        h._peer, h._held_sse, h.connection = "test", True, (mine if real_conn else Conn())  # the hold opened it

        def told(why):
            order.append("abort")
            return True
        h._abort_upstream = abort or told
        h._note_rid = lambda v: None
        h._scan_corruption = lambda v: False
        h._watch_tools = lambda v, a: None
        h._unbind_if_ordered_later = lambda: self.later.append(1)
        h._chunk = chunk
        h._finish = lambda: (got.append("finish"), self.finish_at.append(len(writes)))
        h._done = lambda outcome, quiet=False: (got.append(outcome), m._gen_discard(h))
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)               # as _forward adds it
        self.addCleanup(m._gen_discard, h)
        h._relay_inner(Resp())
        if errors:
            raise errors[0]
        if need_freeze:
            self.assertEqual(frozen, [True], "the test needs its client frozen once, the whole stream read meanwhile")
        return got

    def _a_stop(self, reached):
        """What stop_generations marks, its engine's abort aside: the handler if it is a stop target. reached gets how
        many it marked."""
        m = self.mod

        def stop(h):
            with m._GEN_LOCK:
                targets = [x for x in m._GENERATIONS if x is h and not getattr(x, "_relaying_whole", False)]
                for x in targets:
                    x._mark_stopped("this test's memory floor", True)
            reached.append(len(targets))
        return stop

    def _bindings_read_here(self, ordered=False):
        """No binding of another test, and systemd's answer: a stop of the lane when ordered["now"]."""
        m = self.mod
        m._KILLERS.clear()
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        said = {"now": ordered, "reads": 0}

        def read():
            said["reads"] += 1
            return said["now"]
        m.unit_stopping_by_order = read
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        return said

    def test_an_engines_cut_behind_a_client_that_reads_nothing_is_never_a_whole_answer(self):
        # the pump put the cut once, with 5 s to wait: the queue full that long behind a client that read nothing, it was
        # dropped, the end that followed went in, and the client's stream was closed as whole on a cut answer (the 12th
        # review of v1.43.0, 2026-11-05)
        got = self._relayed_behind_a_frozen_client(300, freeze=0.5)
        self.assertNotIn("finish", got, "a cut answer was ended as whole")
        self.assertEqual(got[-1], "UPSTREAM CUT", got)

    def test_an_engines_cut_waiting_behind_a_slow_client_is_no_stop_target(self):
        # cut by its engine while its relay wrote the events before the cut to a slow client: a stop then counted it and
        # cut it as stopped, and the cut was left unbound, its retry free to reach the other box (decision 1019; the 12th
        # review of v1.43.0, 2026-11-05)
        m = self.mod
        self._bindings_read_here()
        reached = []
        got = self._relayed_behind_a_frozen_client(300, fp="c" * 32, meanwhile=self._a_stop(reached))
        self.assertEqual(reached, [0], "an engine's cut waiting behind a slow client was a stop target")
        self.assertEqual(got[-1], "UPSTREAM CUT", got)
        self.assertEqual(m.killer_box("c" * 32), "", "an engine's cut was left unbound")
        self.assertEqual(self.later, [1], "a stop job that shows late would not undo the binding")

    def test_a_stream_ended_while_its_relay_writes_to_a_slow_client_is_no_stop_target(self):
        # its engine's stream over, its last events still on their way to a slow client: a stop counted it, started the
        # floor's episode for nothing, and cut the client of an answer whole at its engine (the 12th review of v1.43.0,
        # 2026-11-05)
        reached = []
        got = self._relayed_behind_a_frozen_client(300, meanwhile=self._a_stop(reached), cut=False)
        self.assertEqual(reached, [0], "a stream its engine had ended was a stop target")
        self.assertEqual(got[-2:], ["finish", "ok"], got)

    def test_a_death_read_behind_a_client_that_reads_nothing_is_no_stop_target_and_is_bound(self):
        # vLLM's death read by the pump behind a client that read nothing: out of a stop's reach only once the relay took
        # it, the stop counted the dead stream, cut its client as stopped and left the death unbound (decision 1019; the
        # 13th review of v1.43.0, 2026-11-05)
        m = self.mod
        self._bindings_read_here()
        reached = []
        got = self._relayed_behind_a_frozen_client(300, fp="d" * 32, meanwhile=self._a_stop(reached), cut=False,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(reached, [0], "a death the pump had read was a stop target")
        self.assertEqual(got[-1], "502 upstream", got)
        self.assertEqual(m.killer_box("d" * 32), "", "the death was left unbound")

    def test_a_death_read_far_behind_a_client_that_reads_nothing_is_read_at_once(self):
        # the same death 5,000 events into the stream: the pump waited to put the 1,076th behind a full queue of 1,024
        # and read nothing more, the death included, until the client took some; a stop then counted the dead stream and
        # cut it, the death never bound (the 14th review of v1.43.0, 2026-11-05)
        m = self.mod
        self._bindings_read_here()
        reached = []
        got = self._relayed_behind_a_frozen_client(5000, fp="g" * 32, meanwhile=self._a_stop(reached), cut=False,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(reached, [0], "a death 5,000 events in was a stop target")
        self.assertEqual(got[-1], "502 upstream", got)
        self.assertEqual(m.killer_box("g" * 32), "", "a death 5,000 events in was left unbound")

    def test_a_death_whose_client_leaves_before_its_relay_gets_there_is_bound(self):
        # vLLM's death read, its client gone before the relay reached it: bound only by the relay, it was left unbound,
        # the client's retry free to take the other box's engine down too (decision 1019; the 13th review of v1.43.0)
        m = self.mod
        self._bindings_read_here()

        def gone(h):
            raise BrokenPipeError("the client left")
        got = self._relayed_behind_a_frozen_client(300, fp="h" * 32, meanwhile=gone, cut=False,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(got[-1], "CLIENT GONE on write", got)
        self.assertEqual(m.killer_box("h" * 32), "", "a death its client left before was left unbound")

    def test_an_order_read_at_the_death_binds_nothing_however_late_its_relay_gets_there(self):
        # the lane's stop job ran as vLLM's engine died, and was over when the relay reached the death behind a slow
        # client: systemd read then said no order, and an ordered end was bound 10 minutes (decisions 1030 and 1041; the
        # 14th review of v1.43.0, 2026-11-05)
        m = self.mod
        ordered = self._bindings_read_here(ordered=True)

        def the_job_over(h):
            ordered["now"] = False
        # frozen once systemd was read at the death: frozen once the handler left the stop's reach, a step before that
        # read, the job could be over before it (the review of v1.44.0)
        got = self._relayed_behind_a_frozen_client(300, fp="i" * 32, meanwhile=the_job_over, cut=False, eof_delay=1.0,
                                                   until=lambda h: ordered["reads"] >= 1,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(got[-1], "502 upstream", got)
        self.assertIsNone(m.killer_box("i" * 32), "a death by order was bound once its job was over")

    def test_a_real_death_stays_bound_when_a_stop_job_starts_after_its_stream(self):
        # vLLM's engine died with no stop job, one started once its stream was over, before the relay reached the death
        # behind a slow client: systemd read then took the real death for the order's (the 14th review of v1.43.0,
        # 2026-11-05). Read as the death comes and at the stream's end now; a stop job within 3 s of the binding still
        # undoes it, by the deferred reads it starts (decision 1061)
        m = self.mod
        ordered = self._bindings_read_here(ordered=False)

        def a_job_starts_once_the_stream_is_over(h):
            # the pump read the stream's end (systemd read at the death and at the end): waited for, not 0.3 s (the
            # review of v1.44.0)
            deadline = time.monotonic() + 10
            while ordered["reads"] < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            ordered["now"] = True
        got = self._relayed_behind_a_frozen_client(300, fp="j" * 32, meanwhile=a_job_starts_once_the_stream_is_over,
                                                   cut=False, tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(got[-1], "502 upstream", got)
        self.assertEqual(m.killer_box("j" * 32), "", "a real death was unbound by a stop job that started after it")
        self.assertEqual(self.later, [1], "the deferred reads of systemd were not started at the binding")

    def test_a_client_too_slow_is_cut_and_its_generation_stopped(self):
        # a client that reads nothing: the queue held 1,024 events and the pump waited, its engine generating on; now
        # the spool holds up to SPOOL_MAX_BYTES, past which the generation stops and the client is cut (decision 1108)
        m = self.mod
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        got = self._relayed_behind_a_frozen_client(300, cut=False, frozen_at=0, until=lambda h: h._relay_q.over,
                                                   need_freeze=False)       # cut before its first write, or after it
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertNotIn("finish", got, "a client too slow got its stream ended as whole")
        # its generation stopped, once: a too_slow that stopped nothing passed (the review of v1.44.0)
        deadline = time.monotonic() + 5
        while "abort" not in self.order and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.order.count("abort"), 1, self.order)

    def test_an_end_read_behind_a_client_that_reads_nothing_is_no_stop_target(self):
        # the pump read [DONE] and waited to put it in a queue full behind a client that read nothing: a stop counted an
        # answer whole at its engine, and cut its client, who generated all of it again (the 13th review of v1.43.0,
        # 2026-11-05)
        reached = []
        got = self._relayed_behind_a_frozen_client(300, meanwhile=self._a_stop(reached), cut=False, eof_delay=3.0, settle=0.5,
                                                   tail=(b"data: [DONE]\n\n",))
        self.assertEqual(reached, [0], "an answer whose end the pump had read was a stop target")
        self.assertEqual(got[-2:], ["finish", "ok"], got)
        self.assertEqual(self.relayed[-1], b"data: [DONE]\n\n", "the end did not reach the client")

    def test_an_anthropic_end_read_behind_a_client_that_reads_nothing_is_no_stop_target(self):
        # message_stop, the end of the Anthropic dialect Claude Code reads, read by the pump behind a client that read
        # nothing: no test reached its branch (the 14th review of v1.43.0, 2026-11-05)
        reached = []
        got = self._relayed_behind_a_frozen_client(
            300, meanwhile=self._a_stop(reached), cut=False, eof_delay=3.0, settle=0.5, path="/v1/messages",
            event=b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                  b'"delta":{"type":"text_delta","text":"x"}}\n\n',
            tail=(b'event: message_stop\ndata: {"type":"message_stop"}\n\n',))
        self.assertEqual(reached, [0], "an Anthropic answer whose end the pump had read was a stop target")
        self.assertEqual(got[-2:], ["finish", "ok"], got)
        self.assertEqual(self.relayed[-1], b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
                         "the end did not reach the client")

    def test_a_responses_end_read_behind_a_client_that_reads_nothing_is_no_stop_target(self):
        # /v1/responses ends with response.completed and no [DONE] (SGLang 0.5.22, vLLM 0.29): its end was never read
        # as one, and a stop cut an answer whole at its engine behind a slow client (the 14th review of v1.43.0)
        reached = []
        got = self._relayed_behind_a_frozen_client(
            300, meanwhile=self._a_stop(reached), cut=False, eof_delay=3.0, settle=0.5, path="/v1/responses",
            event=b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"x"}\n\n',
            tail=(b'event: response.completed\ndata: {"type":"response.completed"}\n\n',))
        self.assertEqual(reached, [0], "a Responses answer whose end the pump had read was a stop target")
        self.assertEqual(got[-2:], ["finish", "ok"], got)

    def test_vllms_death_on_its_anthropic_route_is_recognised_when_its_engine_is_gone(self):
        # vLLM 0.29's Anthropic route says its engine's death as the chat event it could not read, an error event
        # without "EngineCore": never recognised, never bound (the 14th and 15th reviews of v1.43.0, 2026-11-05)
        m = self.mod
        self._bindings_read_here()
        death = (b'event: error\ndata: {"type":"error","error":{"type":"internal_error","message":"1 validation error '
                 b'for ChatCompletionStreamResponse\\nid\\n  Field required [type=missing, input_value={\'error\': '
                 b'{\'message\': \'En...ode\': 500}}, input_type=dict]"}}\n\n')
        anthropic = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,' \
                    b'"delta":{"type":"text_delta","text":"x"}}\n\n'
        answers = m.engine_answers
        self.addCleanup(setattr, m, "engine_answers", answers)
        asked = []
        for gone in (True, False):
            with self.subTest(engine_gone=gone):
                m._KILLERS.clear()
                m.engine_answers = lambda path=None: (asked.append(path), not gone)[1]
                fp = ("k" if gone else "l") * 32
                got = self._relayed_behind_a_frozen_client(300, fp=fp, cut=False, path="/v1/messages", event=anthropic,
                                                           tail=(death,))
                self.assertEqual(got[-1], "502 upstream" if gone else "ok", got)
                self.assertEqual(m.killer_box(fp), "" if gone else None)
        # vLLM's /health: on the info route a pool read had not set yet, its 404 said the engine answered (the review)
        self.assertEqual(asked, ["/health", "/health"])

    def test_the_stop_says_a_stream_whose_end_was_read_is_finishing(self):
        # an answer whole at its engine, still going out to a slow client: out of the stop's reach, it was neither
        # stopped nor finishing, and the cockpit said no generation ran (the 13th to 15th reviews of v1.43.0)
        m = self.mod
        seen = []
        self._relayed_behind_a_frozen_client(300, meanwhile=lambda h: seen.append(h in m._FINISHING), cut=False,
                                             tail=(b"data: [DONE]\n\n",))
        self.assertEqual(seen, [True], "a stream whose end was read was not said finishing")
        self.assertEqual(len(m._FINISHING), 0, "a stream done was still said finishing")

    def test_a_floor_that_lands_during_an_abort_all_holds_new_generations_back(self):
        # an Abort all, then the floor while its generation still runs: the floor reaches it again and holds new
        # generations back, SGLang answering abort_all before its scheduler frees anything. As v1.43.0 has it: the 15th
        # review of v1.43.0 called it a double count, v1.44.0's three reviews disagreed, and the box's behaviour is
        # kept until it is decided
        m = self.mod
        upstream = m.UPSTREAM
        m.UPSTREAM = "http://127.0.0.1:9"       # no engine: the stop's abort_all is refused at once, nothing aborted
        self.addCleanup(setattr, m, "UPSTREAM", upstream)
        self._a_stops_state_given_back()
        h = object.__new__(m.H)
        h._stop_why, h._relay_q, h._peer = None, None, "test"
        h._upstream_sock = lambda: None
        h._stop_waiting = lambda: None
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)
        self.addCleanup(m._gen_discard, h)
        self.assertEqual(m.stop_generations("this test's Abort all", floor=False)[0], 1)
        self.assertIsNone(m.floor_refusal(), "an operator's stop held new generations back")
        self.assertEqual(m.stop_generations("this test's memory floor", floor=True)[0], 1,
                         "the floor did not reach a generation still running")
        self.assertIsNotNone(m.floor_refusal(), "a floor that landed during an Abort all held nothing back")

    def test_a_stop_ends_a_running_vllm_relays_upstream_at_once(self):
        # on vLLM a stop ended a running relay's upstream only at its next event, after a write to a slow client
        # returned, up to CLIENT_IO_S, the generation going on meanwhile (the 15th review of v1.43.0, 2026-11-05)
        m = self.mod
        upstream = m.UPSTREAM
        m.UPSTREAM = "http://127.0.0.1:9"
        self.addCleanup(setattr, m, "UPSTREAM", upstream)
        self._a_stops_state_given_back()
        shut = []
        h = object.__new__(m.H)
        h._stop_why, h._peer, h._upstream_engine = None, "test", "vllm"
        h._relay_q = m._Spool(0)
        h._upstream_sock = lambda: object()
        h._stop_waiting = lambda: None
        h._shut_upstream = lambda: shut.append(1)
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)
        self.addCleanup(m._gen_discard, h)
        m.stop_generations("this test's Abort all", floor=False)
        self.assertEqual(shut, [1], "a running vLLM relay's upstream was not ended by the stop")

    def test_an_ollama_streams_last_line_read_is_no_stop_target(self):
        # its last line ("done": true) on its way to a slow client: a stop counted it, and cut a whole answer once its
        # write returned (the 14th and 15th reviews of v1.43.0, 2026-11-05)
        m = self.mod
        reached, got, writes = [], [], []
        stop = self._a_stop(reached)
        last = b'{"model":"m","message":{"role":"assistant","content":""},"done":true,"done_reason":"stop"}\n'

        class Resp:
            status = 200
            headers = {"Content-Type": "application/x-ndjson"}

            def __init__(self):
                self.lines = [b'{"model":"m","message":{"role":"assistant","content":"x"},"done":false}\n'] * 3 + [last]

            def read1(self, size):
                return self.lines.pop(0) if self.lines else b""

            def close(self):
                pass

        def chunk(b):
            if b == last:
                stop(h)                         # the stop lands while the last line is written
            writes.append(b)
        h = object.__new__(m.H)
        h.path, h._t0, h._first, h._last, h._bytes = "/api/chat", time.time(), None, None, 0
        h.command = "POST"
        h._stop_why, h._pair_box, h._peer, h._held_sse = None, None, "test", False
        h._out_ndjson, h._answer_body = True, bytearray()
        h._begin = lambda status, headers: None
        h._chunk = chunk
        h._finish = lambda: got.append("finish")
        h._done = lambda outcome, quiet=False: (got.append(outcome), m._gen_discard(h))
        h.connection = types.SimpleNamespace(shutdown=lambda how: got.append("shutdown"))
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)
        self.addCleanup(m._gen_discard, h)
        h._relay_inner(Resp())
        self.assertEqual(reached, [0], "an Ollama stream whose last line was read was a stop target")
        self.assertEqual(got[-2:], ["finish", "ok non-sse"], got)

    def _a_stops_state_given_back(self):
        """What a stop changes (the floor, the stops' windows) given back at cleanup, and the key its abort_all
        carries this test's: ThePairsPieces gives HOME back after its import, and this machine's real key file was read
        and sent to the test's own server (the review of v1.44.0)."""
        m = self.mod
        floor, key = dict(m._FLOOR), m.UPSTREAM_API_KEY
        self.addCleanup(lambda: (m._FLOOR.clear(), m._FLOOR.update(floor)))
        for name in ("_STOP_WINDOWS", "_STOP_WINDOW"):      # read with getattr: a proxy before v1.44.0's review has one
            if hasattr(m, name):
                kept = dict(getattr(m, name))
                self.addCleanup(lambda name=name, kept=kept: (getattr(m, name).clear(), getattr(m, name).update(kept)))
        self.addCleanup(setattr, m, "UPSTREAM_API_KEY", key)
        m.UPSTREAM_API_KEY = "k-test-stop"      # never this machine's real key file

    def _abort_all_server(self, answer):
        """An engine whose abort_all answers what answer(n) says for its nth: (status, seconds it takes first)."""
        m = self.mod
        seen = []

        class Aborts(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append(self.headers.get("Authorization"))
                status, wait = answer(len(seen))
                time.sleep(wait)
                self.send_response(status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Aborts)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        m.UPSTREAM = f"http://127.0.0.1:{srv.server_address[1]}"
        return seen

    def _a_stop_made(self, how):
        """stop_generations as the cockpit's Abort all makes it, against an engine whose abort_all answers 200
        ("goes"), 503 ("fails"), or none listening ("refused", at once): the state it changes given back."""
        m = self.mod
        upstream = m.UPSTREAM
        self.addCleanup(setattr, m, "UPSTREAM", upstream)
        self._a_stops_state_given_back()
        self.aborts_seen = []
        if how == "refused":
            m.UPSTREAM = "http://127.0.0.1:9"   # no engine: abort_all refused at once, nothing aborted
        else:
            self.aborts_seen = self._abort_all_server(lambda n: (200 if how == "goes" else 503, 0.0))
        return m.stop_generations("this test's Abort all", floor=False)

    def _a_generation_now(self):
        """A generation registered, then going out, as _forward makes them."""
        m = self.mod
        h = object.__new__(m.H)
        h._stop_why, h._relay_q, h._peer = None, None, "test"
        h._now_a_generation()
        getattr(h, "_marked_if_a_stop_is_under_way", lambda: None)()   # before v1.44.0's review, marked as it registered
        self.addCleanup(m._gen_discard, h)
        return h

    def test_a_stops_abort_all_carries_the_tests_key_never_this_machines(self):
        # ThePairsPieces gives HOME back after its import: this machine's real key file was read and sent (the review)
        self._a_stop_made("goes")
        self.assertEqual(self.aborts_seen, ["Bearer k-test-stop"])

    def test_a_hop_too_slow_is_told_the_order_however_long_its_box_reads_nothing(self):
        # the other box read nothing a while (its own client paused): cut at SPOOL_CUT_WAIT_S, the order this box decided
        # could not be written, and that box read a bare cut, bound 10 minutes (decisions 1019 and 1031, the 2nd review
        # of v1.44.0)
        m = self.mod
        cap, wait = m.SPOOL_MAX_BYTES, m.SPOOL_CUT_WAIT_S
        m.SPOOL_MAX_BYTES, m.SPOOL_CUT_WAIT_S = 4096, 0.3
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        self.addCleanup(setattr, m, "SPOOL_CUT_WAIT_S", wait)
        got = self._relayed_behind_a_frozen_client(300, hop="spark1", cut=False, frozen_at=0, freeze=1.0,
                                                   until=lambda h: h._relay_q.over, need_freeze=False, real_conn=True)
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertIn(b"x_alzeo_ended", b"".join(self.relayed), "the other box was not told the order")

    def test_a_hop_too_slow_after_a_death_by_order_says_the_order(self):
        # the pump read its engine's death while its lane's stop ran: the order put before the death, still in the
        # spool when it went over, was never said, and the other box bound an ordered end (the 2nd review of v1.44.0)
        m = self.mod
        self._bindings_read_here(ordered=True)
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        more = b'data: {"choices":[{"index":0,"delta":{"content":"' + b"y" * 5000 + b'"}}]}\n\n'
        got = self._relayed_behind_a_frozen_client(1, hop="spark1", cut=False, frozen_at=0,
                                                   until=lambda h: h._relay_q.over, need_freeze=False,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n", more))
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertIn(b"x_alzeo_ended", b"".join(self.relayed), "an order put before a death by order was lost")

    def test_a_stop_during_the_death_probe_never_takes_the_death(self):
        # vLLM's Anthropic death is probed on /health, up to 10 s: a stop during the probe marked the dying stream, and
        # its death reached its client as a stop, unbound; out of every stop's reach before the probe, nothing in the
        # pump proved it (the 2nd review of v1.44.0)
        m = self.mod
        self._bindings_read_here()
        answers = m.engine_answers
        self.addCleanup(setattr, m, "engine_answers", answers)
        reached = []

        def a_stop_during_the_probe(path=None):
            with m._GEN_LOCK:
                targets = [x for x in m._GENERATIONS if not getattr(x, "_stop_why", None)]
                for x in targets:
                    x._mark_stopped("this test's memory floor", True)
            reached.append(len(targets))
            return False                        # its engine gone
        m.engine_answers = a_stop_during_the_probe
        death = (b'event: error\ndata: {"type":"error","error":{"type":"internal_error","message":"1 validation error '
                 b'for ChatCompletionStreamResponse"}}\n\n')
        got = self._relayed_behind_a_frozen_client(
            300, fp="q" * 32, cut=False, path="/v1/messages", tail=(death,),
            event=b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                  b'"delta":{"type":"text_delta","text":"x"}}\n\n')
        self.assertEqual(reached, [0], "a stop during the probe reached the dying stream")
        self.assertEqual(got[-1], "502 upstream", got)
        self.assertEqual(m.killer_box("q" * 32), "", "a death probed during a stop was not bound")

    def test_a_failed_responses_stream_is_never_said_finishing(self):
        # response.failed, then its engine's end: read at its end as no whole answer, then said finishing at its EOF
        # (the 2nd review of v1.44.0)
        m = self.mod
        seen = []
        self._relayed_behind_a_frozen_client(
            300, meanwhile=lambda h: seen.append(h in m._FINISHING), cut=False, settle=0.5, path="/v1/responses",
            event=b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"x"}\n\n',
            tail=(b'event: response.failed\ndata: {"type":"response.failed"}\n\n',))
        self.assertEqual(seen, [False], "a failed stream was said finishing at its end")

    def test_a_hops_stream_its_engine_aborted_says_the_order(self):
        # an abort this proxy did not make (an abort_all the cockpit sent straight, SGLang's own): cut bare, the other
        # box read its engine's death and bound it 10 minutes (the 2nd review of v1.44.0)
        self._bindings_read_here()
        aborted = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"abort"}]}\n\n'
        got = self._relayed_behind_a_frozen_client(10, hop="spark1", cut=False, frozen_at=-1, need_freeze=False,
                                                   tail=(aborted,))
        self.assertEqual(got[-1], "UPSTREAM ABORTED", got)
        self.assertIn(b"x_alzeo_ended", b"".join(self.relayed), "the other box was not told the abort binds nothing")

    def test_a_stopped_stream_whose_engine_knows_no_rid_is_drained(self):
        # a stream a stop marked before its relay began, its abort refused for want of a rid: closed, its engine
        # generated on for nobody (sglang#35255); drained, as drop_upstream does (the 2nd review of v1.44.0)
        m = self.mod
        done, drained, order = [], [], []

        class Resp:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def close(self):
                order.append("close")
        h = object.__new__(m.H)
        h.path, h._peer, h._stop_why = "/v1/chat/completions", "test", "this test's Abort all"
        h._abort_upstream = lambda why: False
        h._drain_detached = lambda resp: drained.append(1)
        h._cut_stopped = lambda: done.append("cut")
        h._relay_inner(Resp())
        self.assertEqual(done, ["cut"])
        self.assertEqual((drained, order), ([1], []), "a stopped stream whose engine knows no rid was closed, not drained")

    def test_a_client_too_slow_in_a_keepalive_write_is_cut_too_slow(self):
        # its relay in a keepalive's write when the spool went over: said gone, not too slow, and drop_upstream ran a
        # second time, its drain begun again (the 2nd review of v1.44.0)
        m = self.mod
        cap, wait, ka = m.SPOOL_MAX_BYTES, m.SPOOL_CUT_WAIT_S, m.KEEPALIVE_S
        m.SPOOL_MAX_BYTES, m.SPOOL_CUT_WAIT_S, m.KEEPALIVE_S = 4096, 0.3, 0.2
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        self.addCleanup(setattr, m, "SPOOL_CUT_WAIT_S", wait)
        self.addCleanup(setattr, m, "KEEPALIVE_S", ka)
        got, in_ka = [], threading.Event()
        mine, peer = socket.socketpair()
        self.addCleanup(mine.close)
        self.addCleanup(peer.close)

        class Resp:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def read1(self, size):
                in_ka.wait(10)                  # quiet until its relay is in a keepalive's write
                return b'data: {"choices":[{"index":0,"delta":{"content":"' + b"z" * 200 + b'"}}]}\n\n'

            def close(self):
                pass

        def chunk(b):
            if b == m.KA_OPENAI:
                in_ka.set()
                if not select.select([peer], [], [], 8)[0] or peer.recv(1) != b"":
                    raise AssertionError("never unblocked")
                raise BrokenPipeError("shut")
        h = object.__new__(m.H)
        h.path, h._t0, h._first, h._last, h._bytes = "/v1/chat/completions", time.time(), None, None, 0
        h._stop_why, h._pair_box, h._killer_fp, h._killer_bound, h._killer_before = None, None, None, False, None
        h._peer, h._held_sse, h.connection = "test", True, mine
        h._abort_upstream = lambda why: True
        h._note_rid = lambda v: None
        h._scan_corruption = lambda v: False
        h._watch_tools = lambda v, a: None
        h._chunk = chunk
        h._finish = lambda: got.append("finish")
        h._done = lambda outcome, quiet=False: (got.append(outcome), m._gen_discard(h))
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)
        self.addCleanup(m._gen_discard, h)
        h._relay_inner(Resp())
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)

    def test_a_stops_window_stays_open_while_its_abort_all_answers(self):
        # its window was ABORT_TIMEOUT_S + 2 s from its start, the abort_all's timeout bounding the connection and each
        # read apart: a slow answer outlived it, and a generation that went out then was ended by it unmarked (the 2nd
        # review of v1.44.0)
        m = self.mod
        upstream, timeout = m.UPSTREAM, m.ABORT_TIMEOUT_S
        self.addCleanup(setattr, m, "UPSTREAM", upstream)
        self.addCleanup(setattr, m, "ABORT_TIMEOUT_S", timeout)
        self._a_stops_state_given_back()
        m.ABORT_TIMEOUT_S = 1.0                 # a window of 3 s before the 2nd review, each read 1 s at most

        class Drip(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                out = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nX-Pad: " + b"x" * 20 + b"\r\n\r\n{}"
                for i in range(len(out)):       # a byte every 0.1 s: each read well within its 1 s, about 7 s in all
                    self.wfile.write(out[i:i + 1])
                    self.wfile.flush()
                    time.sleep(0.1)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Drip)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        m.UPSTREAM = f"http://127.0.0.1:{srv.server_address[1]}"
        t = threading.Thread(target=m.stop_generations, args=("this test's Abort all",), kwargs={"floor": False})
        t.start()
        self.addCleanup(t.join, 15)             # ended before the windows are given back
        time.sleep(4.0)
        self.assertTrue(t.is_alive(), "the test needs the abort_all still answering")
        self.assertEqual(self._a_generation_now()._stop_why, "this test's Abort all",
                         "a generation that went out while abort_all still answered was not the stop's")
        t.join(10)

    def test_a_stop_that_raises_still_closes_its_window(self):
        # open until its aborts are answered (the 2nd review of v1.44.0): a stop that raised before saying its end left
        # it open for good, every new generation cut until a restart
        m = self.mod
        self._a_stops_state_given_back()
        engines = m._stop_engines
        self.addCleanup(setattr, m, "_stop_engines", engines)

        def fails(targets):
            raise RuntimeError("can't start new thread")
        m._stop_engines = fails
        with self.assertRaises(RuntimeError):
            m.stop_generations("this test's Abort all", floor=False)
        with m._GEN_LOCK:
            w = m._stop_window_now()
        self.assertIsNotNone(w, "the window closed at once, the cockpit's own abort_all still to come")
        self.assertLessEqual(w["until"], time.monotonic() + m.COCKPIT_ABORT_GRACE_S, "a stop that raised left its window open")

    def test_a_drained_answers_spool_is_freed(self):
        # its client gone, its engine knowing no rid of it: the answer drained to its end, every event queued before kept
        # alive the drain long, up to DRAIN_MAX_S (the 4th review of v1.44.0)
        hs = []

        def leaves(h):
            hs.append(h)
            raise BrokenPipeError("the client left")
        got = self._relayed_behind_a_frozen_client(300, cut=False, meanwhile=leaves, abort=lambda why: False)
        self.assertEqual(got[-1], "CLIENT GONE on write", got)
        self.assertEqual(hs[0]._relay_q.bytes, 0, "what waited for a client gone stayed through the drain")

    def test_a_stop_wakes_an_idle_relay_with_no_keepalive(self):
        # its relay idle, waiting for the engine's next event, when a stop lands: woken with nothing to read, it took
        # the wake for a keepalive's silence and wrote one to the stopped client before cutting it (the 6th review);
        # the wake marker that keeps it from that was held by no test (the 7th review of v1.44.0)
        m = self.mod
        reached = []

        def a_stop_once_idle():
            _until(lambda: getattr(self, "h", None) is not None and len(getattr(self, "relayed", ())) >= 20, 10)
            time.sleep(0.3)                     # the relay waits for the engine's next event
            self._a_stop(reached)(self.h)
        self.h = None
        threading.Thread(target=a_stop_once_idle, daemon=True).start()
        self._relayed_behind_a_frozen_client(20, cut=False, eof_delay=3.0, frozen_at=-1, need_freeze=False)
        self.assertEqual(reached, [1], "the test needs the stop to reach its idle relay")
        self.assertNotIn(m.KA_OPENAI, self.relayed, "a stopped client got a keepalive before its cut")

    def test_a_stop_frees_its_frozen_relays_spool(self):
        # a stopped relay frozen in a write kept what waited in its spool, never written once stopped, up to
        # SPOOL_MAX_BYTES and CLIENT_IO_S long, on the box whose memory the floor's stop defends; cleared alone, it
        # filled again while the engine still sent (the 5th and 6th reviews of v1.44.0)
        reached, held = [], []

        def a_stop_while_frozen(h):
            held.append(h._relay_q.bytes)
            self._a_stop(reached)(h)
            held.append(h._relay_q.bytes)
            time.sleep(0.5)                     # the engine still sending
            held.append(h._relay_q.bytes)
        self._relayed_behind_a_frozen_client(3000, cut=False, pace=0.002, until=lambda h: h._relay_q.bytes > 10000,
                                             meanwhile=a_stop_while_frozen)
        self.assertEqual(reached, [1], "the test needs the stop to reach the running generation")
        self.assertGreater(held[0], 10000, "the test needs its spool full when the stop came")
        self.assertEqual(held[1:], [0, 0], "a stop left what waited, or let what came after fill it again")

    def test_a_client_too_slows_spool_is_freed_before_its_abort(self):
        # freed before the abort's round trip, up to ABORT_TIMEOUT_S on an engine the floor finds slow (the 4th and 5th
        # reviews of v1.44.0)
        m = self.mod
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        at_abort = []

        def abort(why):
            at_abort.append(self.h._relay_q.bytes)
            self.order.append("abort")
            return True
        got = self._relayed_behind_a_frozen_client(300, cut=False, frozen_at=0, need_freeze=False, abort=abort,
                                                   until=lambda h: h._relay_q.over)
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertEqual(at_abort, [0], "the spool was freed only after the abort's round trip")

    def test_a_take_over_of_a_request_a_stop_marked_answers_it_here(self):
        # the stop's mark read under the lock the stop marks under, where the hand-off is decided: read before it, a
        # stop in between had the other box serve its request (the 4th and 5th reviews of v1.44.0)
        m = self.mod
        calls, cut = [], []
        h = object.__new__(m.H)
        h._peer, h._stop_why, h._hold_until, h._held_sse = "test", "this test's Abort all", None, False
        h._pair_alt = ([{"name": "spark2"}], "qwen3.8-27b", {"name": "spark1"}, (), [], None)
        h._forward_peer = lambda *a, **k: calls.append(1)
        h._floor_refused = lambda: False
        h._cut_stopped = lambda: cut.append(1)
        self.assertTrue(h._pair_take_over(b"{}"), "a stopped request's take-over said nothing answered it")
        self.assertEqual((calls, cut), ([], [1]), "a request a stop marked was handed to the other box")

    def test_a_client_too_slows_spool_is_freed_at_once(self):
        # what waited in the spool of a client too slow is never written: it stayed SPOOL_MAX_BYTES and more behind a
        # hop that reads again only within CLIENT_IO_S, or a drain of up to DRAIN_MAX_S (the 3rd review of v1.44.0)
        m = self.mod
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        held = []
        got = self._relayed_behind_a_frozen_client(300, hop="spark1", cut=False, frozen_at=0, need_freeze=False,
                                                   until=lambda h: h._relay_q.over and h._relay_q.bytes == 0,
                                                   meanwhile=lambda h: held.append(h._relay_q.bytes), real_conn=True)
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertEqual(held, [0], "the spool of a client too slow was kept")

    def test_a_stop_whose_engine_refuses_its_abort_all_keeps_no_window(self):
        # no engine listening: the cockpit's own abort_all reaches none either, and the window open 8 s cut the requests
        # that came then, where they waited for the engine and were served (the review of v1.44.0)
        _, sent = self._a_stop_made("refused")
        self.assertFalse(sent, "the test needs the proxy's abort_all refused")
        self.assertIsNone(self._a_generation_now()._stop_why, "a generation after a stop no engine heard was stopped")

    def test_two_stops_at_once_keep_each_its_window(self):
        # the floor's stop and an operator's Abort all at once: the first to end closed the window of the other, still
        # open for the cockpit's own abort_all (the review of v1.44.0)
        m = self.mod
        upstream = m.UPSTREAM
        self.addCleanup(setattr, m, "UPSTREAM", upstream)
        self._a_stops_state_given_back()
        first_in, second_in = threading.Event(), threading.Event()

        def answer(n):
            # the first abort_all answers once the second came and was refused: its stop ends last, its window closed
            if n == 1:
                first_in.set()
                second_in.wait(5)
                return 200, 0.5
            second_in.set()
            return 503, 0.0
        self._abort_all_server(answer)
        first = threading.Thread(target=m.stop_generations, args=("this test's Abort all",), kwargs={"floor": False})
        first.start()
        self.assertTrue(first_in.wait(5), "the first stop's abort_all never came")
        m.stop_generations("this test's memory floor", floor=True)     # refused 503: its window open 8 s more
        first.join(10)
        self.assertFalse(first.is_alive(), "the first stop never ended")
        self.assertEqual(self._a_generation_now()._stop_why, "this test's memory floor",
                         "a stop that ended closed the window of one still open")

    def test_a_generation_that_registers_in_a_stops_window_is_marked_as_it_goes_out(self):
        # marked as it registered, before the size guard: one the guard then held (its engine warming up, its count
        # unanswered) kept the mark through its hold, up to 20 minutes, and was cut after it (the review of v1.44.0)
        m = self.mod
        self._a_stop_made("fails")
        h = object.__new__(m.H)
        h._stop_why, h._relay_q, h._peer = None, None, "test"
        h._now_a_generation()
        self.addCleanup(m._gen_discard, h)
        self.assertIsNone(h._stop_why, "a generation was marked as it registered, before the size guard")
        h._marked_if_a_stop_is_under_way()
        self.assertEqual(h._stop_why, "this test's Abort all", "a generation going out in the stop's window was not marked")

    def test_a_vllm_client_that_leaves_is_no_death_of_its_engine(self):
        # vLLM: its client gone, the proxy shuts the upstream (closing is vLLM's abort), and the pump read the read that
        # fails then as its engine cutting the stream, before stop was set: bound 10 minutes (decision 1019; the review
        # of v1.44.0, 18 of 60 on loopback)
        m = self.mod
        self._bindings_read_here()
        fp, shut, got, writes = "v" * 32, threading.Event(), [], []

        class Resp:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self):
                self.n = 0

            def read1(self, size):
                self.n += 1
                if self.n <= 3:
                    return b'data: {"choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'
                shut.wait(10)
                raise http.client.IncompleteRead(b"", 10)      # the socket this proxy shut

            def close(self):
                pass

        class Conn:
            def shutdown(self, how):
                pass

        def chunk(b):
            if writes:
                raise BrokenPipeError("the client left")
            writes.append(b)

        def shut_upstream():
            shut.set()
            time.sleep(0.3)                 # the pump reads the shut before its caller sets stop, as a slow switch has it
        h = object.__new__(m.H)
        h.path, h._t0, h._first, h._last, h._bytes = "/v1/chat/completions", time.time(), None, None, 0
        h._stop_why, h._pair_box, h._killer_fp, h._killer_bound, h._killer_before = None, None, fp, False, None
        h._peer, h._held_sse, h.connection, h._upstream_engine = "test", True, Conn(), "vllm"
        h._shut_upstream = shut_upstream
        h._note_rid = lambda v: None
        h._scan_corruption = lambda v: False
        h._watch_tools = lambda v, a: None
        h._unbind_if_ordered_later = lambda: None
        h._chunk = chunk
        h._finish = lambda: got.append("finish")
        h._done = lambda outcome, quiet=False: (got.append(outcome), m._gen_discard(h))
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)
        self.addCleanup(m._gen_discard, h)
        h._relay_inner(Resp())
        time.sleep(0.5)                     # the pump's end
        self.assertEqual(got[-1], "CLIENT GONE on write", got)
        self.assertIsNone(m.killer_box(fp), "its client gone, the proxy's own close bound the request")

    def test_a_hops_vllm_anthropic_death_is_said_so_the_entry_reads_it(self):
        # vLLM's death on /v1/messages, said as a validation error with no EngineCore: the box that sent the request on
        # reads a death in EngineCore or in this proxy's own words, and neither box bound it (the review of v1.44.0)
        m = self.mod
        self._bindings_read_here()
        answers = m.engine_answers
        self.addCleanup(setattr, m, "engine_answers", answers)
        m.engine_answers = lambda path=None: False
        death = (b'event: error\ndata: {"type":"error","error":{"type":"internal_error","message":"1 validation error '
                 b'for ChatCompletionStreamResponse\\nid\\n  Field required [type=missing]"}}\n\n')
        got = self._relayed_behind_a_frozen_client(
            300, hop="spark1", cut=False, path="/v1/messages",
            event=b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                  b'"delta":{"type":"text_delta","text":"x"}}\n\n', tail=(death,))
        self.assertEqual(got[-1], "502 upstream", got)
        said = b"".join(self.relayed)
        self.assertIn(m.HOP_CLOSED_B, said, "the other box was not told its engine's death in words it reads")
        self.assertNotIn(b"ChatCompletionStreamResponse", said)

    def test_a_client_too_slow_has_its_generation_aborted_before_the_upstream_closes(self):
        # the relay closed the upstream as soon as the spool was over, while the pump's abort was on its way: the
        # close first, an engine without sglang#35255 dropped the state the abort needed (the review of v1.44.0)
        m = self.mod
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)

        def slow_abort(why):
            time.sleep(0.5)                 # an abort by rid, on its way
            self.order.append("abort")
            return True
        got = self._relayed_behind_a_frozen_client(300, cut=False, frozen_at=0, until=lambda h: h._relay_q.over,
                                                   need_freeze=False, abort=slow_abort)
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        deadline = time.monotonic() + 5
        while "abort" not in self.order and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.order[:1], ["abort"], f"the upstream closed before its abort: {self.order}")

    def test_a_client_too_slow_whose_engine_knows_no_rid_has_its_answer_drained(self):
        # no rid the engine knows: the upstream was shut with no abort, the engine generating on for nobody where it
        # does not see a close (sglang#35255); drained instead, as drop_upstream does (the review of v1.44.0)
        m = self.mod
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        shut = []
        saved = m.H._shut_upstream
        self.addCleanup(setattr, m.H, "_shut_upstream", saved)
        m.H._shut_upstream = lambda h: shut.append(1)
        n0 = len(self.lines)
        got = self._relayed_behind_a_frozen_client(300, cut=False, frozen_at=0, until=lambda h: h._relay_q.over,
                                                   need_freeze=False, abort=lambda why: False)
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        deadline = time.monotonic() + 5
        while "close" not in self.order and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(shut, [], "the upstream was shut with no abort")
        self.assertEqual(self.order, ["close"], f"the upstream closed before its answer was drained: {self.order}")
        self.assertTrue(any("drained an abandoned" in ln for ln in self.lines[n0:]), "the answer was not drained")

    def test_a_hop_too_slow_after_its_engines_death_says_no_order(self):
        # the pump read the death, then the spool went over: the box that sent the request on was told an order, and
        # undid the binding of a real death (the review of v1.44.0)
        m = self.mod
        self._bindings_read_here()
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        more = b'data: {"choices":[{"index":0,"delta":{"content":"' + b"y" * 5000 + b'"}}]}\n\n'
        got = self._relayed_behind_a_frozen_client(1, hop="spark1", cut=False, frozen_at=0,
                                                   until=lambda h: h._relay_q.over, need_freeze=False,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n", more))
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertNotIn(b"x_alzeo_ended", b"".join(self.relayed), "an order was said after its engine's death")

    def test_an_end_over_the_spools_bound_stops_nothing(self):
        # its end the event that went over the bound: a generation whole at its engine was aborted and its client cut,
        # who generated all of it again (the review of v1.44.0)
        m = self.mod
        cap = m.SPOOL_MAX_BYTES
        m.SPOOL_MAX_BYTES = 4096
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        end = b'event: message_stop\ndata: {"type":"message_stop","pad":"' + b"p" * 1200 + b'"}\n\n'
        event = (b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                 b'"delta":{"type":"text_delta","text":"x"}}\n\n')
        n = 3000 // len(event)
        got = self._relayed_behind_a_frozen_client(n, cut=False, path="/v1/messages", event=event, tail=(end,),
                                                   frozen_at=0, settle=0.3, need_freeze=False)
        self.assertEqual(got[-2:], ["finish", "ok"], got)
        self.assertNotIn("abort", self.order, "a generation whole at its engine was aborted")

    def test_a_dead_stream_is_never_said_finishing(self):
        # vLLM's death, then its [DONE]: the stop said a finished answer was still going out whole (the review of
        # v1.44.0)
        m = self.mod
        self._bindings_read_here()
        seen = []
        self._relayed_behind_a_frozen_client(300, meanwhile=lambda h: seen.append(h in m._FINISHING), cut=False,
                                             tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n", b"data: [DONE]\n\n"))
        self.assertEqual(seen, [False], "a dead stream was said finishing")

    def test_a_decision_that_fails_in_the_pump_still_ends_the_stream(self):
        # _engine_end raising in the pump: its 'e' never went, and the relay waited MAX_SILENCE_S, then ended the
        # stream as whole (the review of v1.44.0)
        m = self.mod
        h_engine_end = m.H._engine_end
        self.addCleanup(setattr, m.H, "_engine_end", h_engine_end)

        def fails(self, put, died=False):
            raise RuntimeError("can't start new thread")
        m.H._engine_end = fails
        t0 = time.monotonic()
        got = self._relayed_behind_a_frozen_client(60, fp="w" * 32, need_freeze=False, frozen_at=-1)
        self.assertEqual(got[-1], "UPSTREAM CUT", got)
        self.assertLess(time.monotonic() - t0, 10, "the relay waited for an end the pump never put")

    def test_a_death_whose_probe_is_slow_is_out_of_every_stops_reach_first(self):
        # the probe of vLLM's Anthropic death takes up to 10 s: a stop during it marked the stream, and the death
        # reached its client as a stop, unbound (the review of v1.44.0)
        m = self.mod
        calls = []
        answers = m.engine_answers
        self.addCleanup(setattr, m, "engine_answers", answers)
        m.engine_answers = lambda path=None: calls.append("probe") or False
        death = (b'event: error\ndata: {"type":"error","error":{"type":"internal_error","message":"1 validation error '
                 b'for ChatCompletionStreamResponse"}}\n\n')
        self.assertTrue(m.engine_died_in(death, lambda: calls.append("out of reach")))
        self.assertEqual(calls, ["out of reach", "probe"])

    def test_a_frozen_client_is_cut_too_slow_within_seconds(self):
        # a client that reads nothing at all: its relay stayed in its write CLIENT_IO_S (900 s), the spool with it,
        # and was said gone, never too slow (the review of v1.44.0)
        m = self.mod
        cap, wait = m.SPOOL_MAX_BYTES, m.SPOOL_CUT_WAIT_S
        m.SPOOL_MAX_BYTES, m.SPOOL_CUT_WAIT_S = 4096, 0.5
        self.addCleanup(setattr, m, "SPOOL_MAX_BYTES", cap)
        self.addCleanup(setattr, m, "SPOOL_CUT_WAIT_S", wait)
        got, in_write = [], threading.Event()
        mine, peer = socket.socketpair()
        self.addCleanup(mine.close)
        self.addCleanup(peer.close)

        class Resp:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self):
                self.n = 0

            def read1(self, size):
                self.n += 1
                if self.n > 1:
                    in_write.wait(10)       # its relay in its first write, the spool going over behind it
                return b'data: {"choices":[{"index":0,"delta":{"content":"' + b"z" * 200 + b'"}}]}\n\n'

            def close(self):
                pass

        def chunk(b):
            in_write.set()
            # a client that reads nothing: its write returns once its socket's TCP side is shut, its peer reading its end
            if not select.select([peer], [], [], 8)[0] or peer.recv(1) != b"":
                raise AssertionError("never unblocked")
            raise BrokenPipeError("shut")
        h = object.__new__(m.H)
        h.path, h._t0, h._first, h._last, h._bytes = "/v1/chat/completions", time.time(), None, None, 0
        h._stop_why, h._pair_box, h._killer_fp, h._killer_bound, h._killer_before = None, None, None, False, None
        h._peer, h._held_sse, h.connection = "test", True, mine
        h._abort_upstream = lambda why: True
        h._note_rid = lambda v: None
        h._scan_corruption = lambda v: False
        h._watch_tools = lambda v, a: None
        h._chunk = chunk
        h._finish = lambda: got.append("finish")
        h._done = lambda outcome, quiet=False: (got.append(outcome), m._gen_discard(h))
        with m._GEN_LOCK:
            m._GENERATIONS.add(h)
        self.addCleanup(m._gen_discard, h)
        t0 = time.monotonic()
        h._relay_inner(Resp())
        self.assertEqual(got[-1], "CLIENT TOO SLOW", got)
        self.assertLess(time.monotonic() - t0, 5, "a frozen client kept its relay")

    def test_a_hops_order_read_at_its_death_goes_before_the_death_behind_a_slow_client(self):
        # a hop: its lane's stop read at its engine's death, the order put before the death, written so behind a
        # client that read nothing meanwhile (decisions 1031 and 1061); no test drove a hop behind a slow client (the
        # review of v1.44.0)
        self._bindings_read_here(ordered=True)
        got = self._relayed_behind_a_frozen_client(300, hop="spark1", cut=False,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(got[-1], "502 upstream", got)
        said = b"".join(self.relayed)
        self.assertIn(b"x_alzeo_ended", said, "the other box was not told the order")
        self.assertLess(said.index(b"x_alzeo_ended"), said.index(b"EngineCore"), "the order came after the death")

    def test_a_hops_order_seen_at_its_streams_end_is_said_before_its_end(self):
        # a hop: no stop job at its engine's death, one at its stream's end: said before the end, behind a client that
        # read nothing meanwhile (decision 1061, _engine_end_late)
        ordered = self._bindings_read_here(ordered=False)
        m = self.mod
        saved = m.unit_stopping_by_order

        def read():
            ordered["reads"] += 1
            return ordered["reads"] >= 2        # the read at the death says none, the one at the end says one
        m.unit_stopping_by_order = read
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        got = self._relayed_behind_a_frozen_client(300, hop="spark1", cut=False, eof_delay=0.5,
                                                   tail=(b"data: " + VLLM_DEAD_EVENT + b"\n\n",))
        self.assertEqual(got[-1], "502 upstream", got)
        orders = [i for i, w in enumerate(self.relayed) if b"x_alzeo_ended" in w]
        self.assertTrue(orders, "the order seen at the stream's end was not said")
        self.assertLess(orders[0], self.finish_at[0], "the order was said after the stream's end")

    def test_a_generation_that_starts_while_a_stop_is_under_way_is_that_stops(self):
        # abort_all ends every generation SGLang runs, one that started after the stop's list was made included, and the
        # cockpit sends its own, straight to the engine, when this proxy's did not go: unmarked, SGLang ended an
        # Anthropic stream as end_turn, whole to its client (the 15th review of v1.43.0, 2026-11-05)
        _, sent = self._a_stop_made("fails")
        self.assertFalse(sent, "the test needs the proxy's abort_all not to go")
        self.assertEqual(self._a_generation_now()._stop_why, "this test's Abort all",
                         "a generation that started while the cockpit's own abort_all could still come was not marked")

    def test_a_generation_that_starts_after_a_stop_whose_abort_all_went_is_not_its(self):
        _, sent = self._a_stop_made("goes")
        self.assertTrue(sent, "the test needs the proxy's abort_all to go")
        self.assertIsNone(self._a_generation_now()._stop_why, "a generation after a stop that was over was stopped")

    def test_what_pair_json_may_say(self):
        colons = ":".join(ident("spark3")[1][i:i + 2] for i in range(0, 64, 2)).upper()
        ok = self.parse({"name": "Spark-1", "peers": [
            self.peer(), self.peer("b", "https://[fe80::1]:30002", who="rogue"),
            dict(self.peer("c", "HTTPS://spark2.local:30002/"), sha256=colons)]})
        self.assertEqual([p["proxy"] for p in ok["peers"]],
                         ["https://127.0.0.1:30002", "https://[fe80::1]:30002", "https://spark2.local:30002"])
        self.assertEqual(ok["peers"][2]["sha256"], ident("spark3")[1], "openssl's form, with colons, is taken")
        self.assertTrue(ok["peers"][0]["cert_file"].endswith("/.config/qwen38/pair/spark2.crt"))
        self.assertNotIn("key_file", ok["peers"][0], "the certificate is the pair's only credential")
        self.assertTrue(self.mod.PAIR_NAME.fullmatch(self.parse({"peers": []})["name"]), "the host's name, made safe")

    def test_what_pair_json_may_not_say(self):
        p = self.peer
        for conf in ([], {"peers": {}}, {"peers": [{"name": "x"}]}, {"peers": [p("two words")]},
                     {"peers": [p(proxy="http://127.0.0.1:30002")]}, {"peers": [p(proxy="https://127.0.0.1:3000l")]},
                     {"peers": [p(proxy="https://127.0.0.1")]}, {"peers": [p(proxy="https://127.0.0.1:0")]},
                     {"peers": [p(proxy="https://127.0.0.1:30002/v1")]}, {"peers": [p(proxy="https://u:p@127.0.0.1:30002")]},
                     {"peers": [p(proxy="https://127.0.0.1:30002?a")]},
                     {"peers": [dict(p(), cert_file="")]}, {"peers": [{k: v for k, v in p().items() if k != "sha256"}]},
                     {"peers": [dict(p(), sha256="xyz")]}, {"peers": [dict(p(), sha256=ident("spark2")[1][:63])]},
                     {"name": "", "peers": []}, {"name": "Spark \u2460", "peers": []}, {"name": "x" * 41, "peers": []},
                     {"peers": [p("a"), p("a", who="rogue")]}, {"name": "a", "peers": [p("a")]},
                     {"peers": [p("a"), p("b")]}):
            with self.subTest(conf=conf), self.assertRaises(ValueError):
                self.parse(conf)

    def test_pair_json_is_read_again_when_it_changes_and_a_bad_one_leaves_the_box_alone(self):
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

    def test_a_box_paired_again_under_a_name_inherits_nothing_of_the_sharing(self):
        # the conversations sent to a box, its pause, its tile's counts belong to that certificate: another box paired
        # under the same name got them for 10 min, each conversation computed again there (review, 2026-11-03)
        m = self.mod
        port = localports.held(self)
        def facts(name):
            return (name in m._AFFINITY, name in m._PAIR_COOL, name in m._PAIR_SPREAD)
        def note(name):
            m.affinity_note(name, [(4096, "fp-" + name)], time.monotonic())
            m._PAIR_COOL[name] = time.monotonic() + 30
            m.pair_spread_count(name, "balance")
        self.addCleanup(lambda: [d.clear() for d in (m._AFFINITY, m._PAIR_COOL, m._PAIR_SPREAD)])
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", port),
                                                   peer_entry(self.cfg, "spark3", port)]})
        note("spark2"); note("spark3")
        # spark2 paired again, another box under its name; spark3 the same box, read again
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", port, who="rogue"),
                                                   peer_entry(self.cfg, "spark3", port)]})
        self.assertEqual(facts("spark2"), (False, False, False), "another box got the old one's facts")
        self.assertEqual(facts("spark3"), (True, True, True), "the same box lost its facts")
        self.conf_now({"name": "spark1", "peers": []})          # and a box that left the pair
        self.assertEqual(facts("spark3"), (False, False, False))

    def test_the_tls_of_a_pair_is_checked_whole(self):
        good = peer_entry(self.cfg, "spark2", localports.held(self))
        conf = self.conf_now({"name": "spark1", "peers": [good]})
        self.assertEqual(conf["tls"]["pins"], {ident("spark2")[1]: "spark2"})
        self.assertEqual(conf["tls"]["mine"], ident("spark1")[1])
        self.assertTrue(conf["tls"]["server"].verify_flags & 0x200000, "dates not checked: the pin is the certificate")
        self.assertTrue(conf["peers"][0]["opener"] and conf["peers"][0]["list_opener"])
        self.assertIn("over TLS, this box's certificate sha256 " + ident("spark1")[1][:16], self.lines[-1])
        (self.cfg / "pair/two.crt").write_text(ident("spark2")[0] + ident("rogue")[0])
        (self.cfg / "pair/none.crt").write_text("not a certificate\n")
        for why, e in (("is not the certificate pair.json pins", dict(good, sha256=ident("rogue")[1])),
                       ("cannot be read", dict(good, cert_file="pair/missing.crt")),
                       ("its certificate is this box's own", peer_entry(self.cfg, "me", localports.held(self), who="spark1")),
                       ("holds 2 certificates, not one", dict(good, cert_file="pair/two.crt")),
                       ("holds 0 certificates, not one", dict(good, cert_file="pair/none.crt"))):
            with self.subTest(why=why):
                self.assertIsNone(self.conf_now({"name": "spark1", "peers": [e]}), "a pair it does not take: a box alone")
                self.assertIn(why, self.lines[-1])
        aside = self.tmp / "identity-aside"
        shutil.move(self.cfg / "pair/identity", aside)
        try:
            self.assertIsNone(self.conf_now({"name": "spark1", "peers": [good]}))
            self.assertIn("this box has no certificate of its own", self.lines[-1])
        finally:
            shutil.move(aside, self.cfg / "pair/identity")

    def test_why_a_peer_does_not_answer(self):
        why = self.mod._pair_why
        self.assertIn("another certificate than the one pinned", why(urllib.error.URLError(ssl.SSLCertVerificationError("x"))))
        self.assertIn("refuses this box's certificate",
                      why(urllib.error.URLError(ssl.SSLError(1, "[SSL: TLSV1_ALERT_UNKNOWN_CA] tlsv1 alert unknown ca"))))
        self.assertIn("nothing listens on its pair port", why(urllib.error.URLError(ConnectionRefusedError(111, "refused"))))
        self.assertEqual(why(socket.timeout("timed out")), "no answer in time")
        self.assertEqual(why(ValueError("its /v1/models is not a list of models")), "its /v1/models is not a list of models")
        self.assertEqual(why(ValueError()), "ValueError")
        self.assertEqual(why(ValueError("HTTP/1.1 200\r\n\x1b[2Jforged")), "HTTP/1.1 200???[2Jforged",
                         "what the other box wrote never holds a control character")

    def test_the_model_a_body_names(self):
        bm = self.mod.body_model
        self.assertEqual(bm(b'{"model": "kolibri-1"}'), "kolibri-1")
        for body in (b"not json", b"[1]", b'{"model": 3}', b"{}"):
            self.assertIsNone(bm(body))

    def test_each_peers_list_is_read_as_soon_as_the_pair_is(self):
        # the first GET /v1/models after a start or a pairing listed this box's models alone (a pair simulated on
        # the reference box, 2026-11-03)
        class One(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"data": [{"id": "kolibri-1"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        self.mod._PAIR_MODELS.clear()
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "early", tls_peer(self, One), who="spark2")]})
        deadline = time.monotonic() + 8
        while (self.mod._PAIR_MODELS.get("early") or {}).get("models") != ("kolibri-1",) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual((self.mod._PAIR_MODELS.get("early") or {}).get("models"), ("kolibri-1",), "read with no request")

    def test_a_box_that_did_not_answer_is_read_again_in_the_background_once(self):
        # a box off without a reset took each read's whole timeout, and every request for a lane loaded nowhere
        # waited 5 s for it (review, 2026-11-03); the first fix of it handed the read back to the background from
        # the background, a thread after a thread: one read at a time, never waited for
        class Frozen(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                time.sleep(30)
        conf = self.mod._pair_parse(json.dumps({"name": "spark1", "peers": [
            peer_entry(self.cfg, "frozen", tls_peer(self, Frozen), who="spark2")]}))
        self.mod._pair_tls(conf)
        peer = conf["peers"][0]
        self.mod._PAIR_MODELS.pop("frozen", None)
        self.assertEqual(self.mod.peer_models(peer), ())          # its first read: its whole timeout, once
        self.assertTrue(self.mod._PAIR_MODELS["frozen"]["error"])
        self.mod._PAIR_MODELS["frozen"]["ts"] -= 60                # stale: read again, in the background
        t0 = time.monotonic()
        for _ in range(20):
            self.assertEqual(self.mod.peer_models(peer, fresh=True, since=time.monotonic()), ())
        self.assertLess(time.monotonic() - t0, 1.0, "no request waits for a box that did not answer")
        time.sleep(0.3)
        self.assertLessEqual(sum(t.name == "pair-list-frozen" for t in threading.enumerate()), 1,
                             "one read of it in the background at a time")

    def test_a_peers_list_is_read_kept_and_read_again(self):
        class Lists(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            answer, auths = (200, {"data": [{"id": "kolibri-1"}, {"id": 3}, "x", {"id": "forged\n[proxy] line"},
                                            {"id": "\x1b[31mred"}]}), []

            def log_message(self, *a):
                pass

            def do_GET(self):
                Lists.auths.append(self.headers.get("Authorization"))
                code, obj = Lists.answer
                out = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        port = tls_peer(self, Lists)
        conf = self.mod._pair_parse(json.dumps({"name": "spark1", "peers": [
            peer_entry(self.cfg, "lists", port, who="spark2"),
            peer_entry(self.cfg, "gone", localports.held(self), who="rogue"),
            peer_entry(self.cfg, "othercert", port, who="spark3")]}))
        self.mod._pair_tls(conf)
        peer, gone, other = conf["peers"]
        pm = self.mod.peer_models
        self.assertEqual(pm(peer), ("kolibri-1",), "an id that is no printable word never kept: a journal line")
        self.assertEqual(pm(peer), ("kolibri-1",), "kept")
        self.assertEqual(Lists.auths, [None], "read once, with no key: the certificate is the pair's")
        t = time.monotonic()
        self.assertEqual(pm(peer, fresh=True, since=t - 60), ("kolibri-1",), "read after `since`: kept")
        self.assertEqual(len(Lists.auths), 1)
        Lists.answer = (200, [1, 2])
        self.assertEqual(pm(peer, fresh=True), ())
        self.assertIn("not a list", self.mod._PAIR_MODELS["lists"]["error"])
        Lists.answer = (503, {"error": "loading"})
        self.assertEqual(pm(peer, fresh=True), ())
        self.assertEqual(self.mod._PAIR_MODELS["lists"]["error"], "HTTP 503 on /v1/models")
        self.assertEqual(pm(gone), ())
        self.assertIn("nothing listens on its pair port", self.mod._PAIR_MODELS["gone"]["error"])
        self.assertEqual(pm(other), (), "the box at that address shows another certificate than the pinned one")
        self.assertIn("another certificate than the one pinned", self.mod._PAIR_MODELS["othercert"]["error"])

    def test_the_union_and_the_served_map(self):
        conf = self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", localports.held(self))]})
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

    def test_the_pair_port_opens_with_a_pair_and_closes_without_one(self):
        keeper = self.mod.PairPort()
        self.addCleanup(keeper.stop)
        keeper.check()
        self.assertIsNone(keeper.srv, "a box alone opens no port")
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", localports.held(self))]})
        keeper.check()
        self.assertIsNotNone(keeper.srv)
        opened = keeper.srv
        port = keeper.srv.server_address[1]
        self.assertIn(f"pair: port 127.0.0.1:{port} open over TLS, to the certificates of spark2 only", self.lines)
        keeper.check()
        self.assertEqual(keeper.srv.server_address[1], port, "opened once")
        self.pair_file.unlink()
        self.mod._PAIR["seen"] = 0.0
        keeper.check()
        self.assertIsNone(keeper.srv)
        self.assertIn(f"pair: port {port} closed: this box has no pair now", self.lines)
        self.assertEqual(opened.socket.fileno(), -1, "its socket closed (never a connection to a number now free)")

    def test_a_pair_port_that_cannot_be_opened_is_said_once(self):
        keeper = self.mod.PairPort()
        self.addCleanup(keeper.stop)
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", localports.held(self))]})
        saved, self.mod.PAIR_PORT = self.mod.PAIR_PORT, localports.held(self)     # bound by another: EADDRINUSE
        try:
            keeper.check()
            keeper.check()
        finally:
            self.mod.PAIR_PORT = saved
        self.assertIsNone(keeper.srv)
        self.assertEqual(sum("cannot be opened" in ln for ln in self.lines), 1, "said once, tried again every 2 s")
        keeper.check()
        self.assertIsNotNone(keeper.srv, "opened once it can be")

    def test_the_pair_port_admits_nobody_without_a_pair(self):
        srv = self.mod.PairServer(("127.0.0.1", 0), self.mod.H)
        self.addCleanup(srv.server_close)
        cli = socket.create_connection(srv.server_address, timeout=5)
        self.addCleanup(cli.close)
        with self.assertRaisesRegex(OSError, "admits nobody"):
            srv.get_request()
        self.assertEqual(cli.recv(1), b"", "closed at once, before any handshake")

    def test_a_pair_connection_is_the_networks_even_from_loopback(self):
        # the header clock of the network (CLIENT_HEADER_S), not loopback's CLIENT_IO_S: a slow box on a
        # tunnel's end holds a thread seconds, not 15 minutes
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", localports.held(self))]})
        srv = self.mod.PairServer(("127.0.0.1", 0), self.mod.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        saved = self.mod.CLIENT_HEADER_S
        self.mod.CLIENT_HEADER_S = 1.0
        self.addCleanup(setattr, self.mod, "CLIENT_HEADER_S", saved)
        tls = client_ctx("spark2", "spark1").wrap_socket(socket.create_connection(srv.server_address, timeout=10))
        self.addCleanup(tls.close)
        tls.sendall(b"GET /v1/models HTTP/1.1\r\n")             # and nothing more
        t0 = time.monotonic()
        try:
            got = tls.recv(1)
        except (ConnectionResetError, ssl.SSLError):
            got = b""
        except socket.timeout:
            self.fail("the pair port waited past the header clock: it took a loopback address for this machine's")
        self.assertEqual(got, b"")
        self.assertLess(time.monotonic() - t0, 6)
        self.assertTrue(self.mod.PairServer.from_network and not self.mod.Server.from_network)

    def test_a_request_of_the_pair_is_admitted_by_this_boxs_own_key(self):
        # the other box holds no key of this one (decision 978): its certificate admits it on the pair port, and
        # this box's own key admits it to the engine, whatever key it sent (none, or another)
        class Fake:
            pass
        f = Fake()
        f._pair_box, f.headers, f.command, f.path = "spark2", {"Authorization": "Bearer not-this-box"}, "POST", "/v1/messages"
        saved = self.mod.UPSTREAM_API_KEY
        self.mod.UPSTREAM_API_KEY = "k-own-key"          # never this machine's real key file
        self.addCleanup(setattr, self.mod, "UPSTREAM_API_KEY", saved)
        self.assertEqual(self.mod._upstream_auth(f), "Bearer k-own-key")
        self.assertFalse(self.mod.H._door_shut(f, "/v1/messages"), "no key asked of the pair: its certificate")
        f._pair_box = None
        self.assertEqual(self.mod._upstream_auth(f), "Bearer not-this-box", "a client of the client port: its own")

    def test_a_summary_without_its_cockpit_still_says_what_this_box_serves(self):
        # the other box's cockpit stopped or never installed (--no-cockpit): the whole tile read "HTTP 503"
        # (review, 2026-11-03)
        saved = os.environ.get("QWEN38_COCKPIT_URL")
        os.environ["QWEN38_COCKPIT_URL"] = f"http://127.0.0.1:{localports.held(self)}"
        self.addCleanup(lambda: os.environ.pop("QWEN38_COCKPIT_URL") if saved is None
                        else os.environ.__setitem__("QWEN38_COCKPIT_URL", saved))
        served = self.mod.served_models
        self.mod.served_models = lambda: ("qwen3.8-27b",)
        self.addCleanup(setattr, self.mod, "served_models", served)
        s = self.mod.cockpit_summary()
        self.assertEqual(s["models"], ["qwen3.8-27b"])
        self.assertTrue(isinstance(s["cockpit"], str) and s["cockpit"], s)
        self.assertIsNone(s["job"])

    def test_what_a_browser_cannot_parse_is_not_taken_from_the_other_box(self):
        # NaN, Infinity and 1e400 parse in Python and not in a browser: written back into GET /v1/models or the tile,
        # they broke the answer for its client (review, 2026-11-03)
        for bad in ('{"data": [{"id": "x", "created": NaN}]}', '{"a": Infinity}', '{"a": -1e400}'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.mod._json_finite(bad)
        self.assertEqual(self.mod._json_finite('{"a": 1.5, "b": [1, 2e3]}'), {"a": 1.5, "b": [1, 2000.0]})

    def test_a_request_the_other_box_sent_on_is_never_held(self):
        # held through a whole boot, it got a 409 inside the held stream, relayed by its entry as an answer
        # (review, 2026-11-03): answered at once, the entry reads the lists again or says 503
        saved = (self.mod.engine_coming, self.mod.engine_answers)
        self.mod.engine_coming, self.mod.engine_answers = (lambda: True), (lambda: False)   # a lane on its way back
        self.addCleanup(lambda: (setattr(self.mod, "engine_coming", saved[0]), setattr(self.mod, "engine_answers", saved[1])))
        hop = types.SimpleNamespace(_pair_hop=lambda: "spark2", _hold_until=None)
        self.assertFalse(self.mod.H._hold(hop, "the engine does not answer", True))
        self.assertIsNone(hop._hold_until, "no hold begun")

    def test_a_connection_between_the_boxes_probes_a_box_that_went(self):
        # a box that loses power sends no FIN: a request in flight to it waited an hour (review, 2026-11-03)
        lsn = socket.socket()
        lsn.bind(("127.0.0.1", 0)); lsn.listen(1)
        self.addCleanup(lsn.close)
        c = socket.create_connection(lsn.getsockname())
        self.addCleanup(c.close)
        s, _ = lsn.accept()
        self.addCleanup(s.close)
        self.mod._pair_keepalive(c)
        got = (c.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), c.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE),
               c.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL), c.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT),
               c.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT))
        # no TCP_USER_TIMEOUT: it also cut a live stream whose client stopped reading for 25 s (review, 2026-11-03)
        self.assertEqual(got, (1, 10, 5, 3, 0))
        # both ends of a pair: the connection a box opens to its peer, and the one its pair port accepts
        src = (REPO / "keepalive-proxy.py").read_text()
        self.assertIn("        super().connect()\n        _pair_keepalive(self.sock)\n", src)
        self.assertIn("        _pair_keepalive(conn)", src)

    def test_a_line_said_once_a_minute_counts_the_others(self):
        lines = []
        saved = self.mod.log
        self.mod.log = lines.append
        self.addCleanup(setattr, self.mod, "log", saved)
        say = self.mod._say_once_a_minute
        say("test-kind", "first")
        say("test-kind", "second")
        say("test-kind", "third")
        self.assertEqual(lines, ["first"])
        t, quiet = self.mod._SAID["test-kind"]
        self.mod._SAID["test-kind"] = (t - 61, quiet)
        say("test-kind", "fourth")
        self.assertEqual(lines, ["first", "fourth (2 more such since the last line)"])

    def test_the_summary_of_this_box_logs_in_once_and_again_after_a_restart(self):
        srv = Engine(("127.0.0.1", 0), FakeCockpit)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        saved = (self.mod.UPSTREAM_API_KEY, self.mod.served_models, self.mod.HOLD_UNITS)
        url = os.environ.get("QWEN38_COCKPIT_URL")
        def back():
            self.mod.UPSTREAM_API_KEY, self.mod.served_models, self.mod.HOLD_UNITS = saved
            os.environ.pop("QWEN38_COCKPIT_URL", None)
            if url is not None:
                os.environ["QWEN38_COCKPIT_URL"] = url
            self.mod._COCKPIT["cookie"] = None
        self.addCleanup(back)
        self.mod.UPSTREAM_API_KEY = KEY_1                # never this machine's real key file
        self.mod.served_models = lambda: ("qwen3.8-27b",)  # never this machine's engine
        self.mod.HOLD_UNITS = ("qwen38-flash-local.service", "qwen38-sglang.service")
        self.mod._COCKPIT["cookie"] = None
        os.environ["QWEN38_COCKPIT_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
        FakeCockpit.logins.clear()
        first = self.mod.cockpit_summary()
        self.assertEqual((first["models"], first["lanes"]), (["qwen3.8-27b"], {"qwen38-flash-local.service": "active"}))
        self.mod.cockpit_summary()
        self.assertEqual(FakeCockpit.logins, [KEY_1], "one login, its session kept")
        FakeCockpit.sessions.clear()                     # the cockpit restarted: its sessions went with it
        self.mod.cockpit_summary()
        self.assertEqual(FakeCockpit.logins, [KEY_1, KEY_1], "logged in again, once")
        os.environ["QWEN38_COCKPIT_URL"] = f"http://127.0.0.1:{localports.held(self)}"
        self.mod._COCKPIT["cookie"] = None
        gone = self.mod.cockpit_summary()                # its cockpit gone: what this box serves, and why no more
        self.assertEqual(gone["models"], ["qwen3.8-27b"])
        self.assertTrue(gone["cockpit"], gone)
        self.assertEqual(len(FakeCockpit.logins), 2)

    def test_the_cockpit_is_found_where_its_unit_binds_it(self):
        unit = self.tmp / "dashboard.service"
        saved, url = self.mod.COCKPIT_UNIT_FILE, os.environ.pop("QWEN38_COCKPIT_URL", None)
        def back():
            self.mod.COCKPIT_UNIT_FILE = saved
            if url is not None:
                os.environ["QWEN38_COCKPIT_URL"] = url
        self.addCleanup(back)
        self.mod.COCKPIT_UNIT_FILE = str(unit)
        for text, base in (("", "http://127.0.0.1:30090"),
                           ("Environment=COCKPIT_BIND=0.0.0.0\nEnvironment=COCKPIT_PORT=30091\n", "http://127.0.0.1:30091"),
                           ("Environment=COCKPIT_BIND=192.0.2.10\n", "http://192.0.2.10:30090"),   # RFC 5737
                           ('Environment="COCKPIT_BIND=fd7a::1"\n', "http://[fd7a::1]:30090")):
            with self.subTest(unit=text):
                unit.write_text(text)
                self.assertEqual(self.mod._cockpit_base(), base)

    def test_a_connection_refused_in_setup_reads_nothing_and_a_box_taken_out_is_refused(self):
        class Fake:
            pass
        refused = Fake()
        refused._refused, refused.close_connection = True, False
        self.mod.H.handle_one_request(refused)            # it has no rfile: a read would raise here
        self.assertTrue(refused.close_connection)
        self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark3", localports.held(self), who="rogue")]})
        out = Fake()
        out._refused, out.close_connection = False, False
        out._pair_box, out._pair_pin, out.client_address = "spark2", ident("spark2")[1], ("127.0.0.1", 5)
        self.mod.H.handle_one_request(out)
        self.assertTrue(out.close_connection)
        self.assertIn("pair port: spark2 (127.0.0.1) REFUSED: no peer of this box's pair now", self.lines)
        self.assertEqual(self.mod._pair_box_of(ident("rogue")[1]), "spark3")
        self.assertIsNone(self.mod._pair_box_of(ident("spark2")[1]))
        self.assertIsNone(self.mod._pair_box_of(None))


    # Role 10, a model both boxes serve (decisions 996 to 998): the pieces of the decision

    def conversation(self, turns, max_tokens=8, stream=False):
        msgs = [{"role": "system", "content": "a system prompt " * 600}]
        for i in range(turns):
            msgs += [{"role": "user", "content": f"turn {i} " + "y" * 3000}, {"role": "assistant", "content": "ok"}]
        return json.dumps({"model": "qwen3.8-27b", "max_tokens": max_tokens, "stream": stream,
                           "messages": msgs[:-1]}).encode()

    def test_the_blocks_of_a_turn_start_the_blocks_of_the_next(self):
        m, route = self.mod, "/v1/chat/completions"
        one, size1 = m.prefix_blocks(self.conversation(1), route)
        two, size2 = m.prefix_blocks(self.conversation(3), route)
        self.assertEqual([n for n, _ in one], [4096, 8192])
        self.assertEqual([n for n, _ in two], [4096, 8192, 16384])
        self.assertLess(size1, 16384)
        self.assertEqual(two[:2], one, "the next turn does not start with the blocks of this one")
        # what a client changes between two turns, as role 9 reads it, changes no block
        self.assertEqual(m.prefix_blocks(self.conversation(3, max_tokens=32000, stream=True), route)[0], two)
        # each block is the fingerprint of the body's first bytes so read, the route first: one pass, no copy
        body = self.conversation(3)
        canon = m.RESEND_SKIP_RE.sub(lambda x: x.group(1), body)
        self.assertEqual(size2, len(canon))
        for n, fp in two:
            self.assertEqual(fp, hashlib.sha256(route.encode() + canon[:n]).hexdigest()[:16])
        self.assertEqual(m.prefix_blocks(b'{"model": "qwen3.8-27b"}', route), ([], 0), "a short body has no block")
        self.assertNotEqual(m.prefix_blocks(body, "/v1/completions")[0], two, "another route, other blocks")

    def test_a_box_says_what_it_runs_as_a_count_or_not_at_all(self):
        run = self.mod.pair_running
        said = lambda total, yours: {"x_alzeo_running": total, "x_alzeo_running_yours": yours}
        self.assertEqual(run(said(3, 0)), 3)
        self.assertEqual(run(said(0, 0)), 0)
        self.assertEqual(run(said(3, 1)), 2, "its own clients' generations, this box's apart")
        self.assertIsNone(run(said(3, 4)), "more of this box's than it runs")
        self.assertIsNone(run({"x_alzeo_running": 3}), "a list that says not how many are this box's")
        for bad in (True, -1, 3.0, "3", None, [], {"n": 1}, 10 ** 9):
            self.assertIsNone(run(said(bad, 0)), bad)
            self.assertIsNone(run(said(3, bad)), bad)
        self.assertIsNone(run({}))
        self.assertIsNone(run(["x_alzeo_running"]))

    def test_a_box_says_its_slots_as_a_count_or_not_at_all(self):
        slots = self.mod.pair_slots
        self.assertEqual(slots({"x_alzeo_slots": 16}), 16)
        self.assertEqual(slots({"x_alzeo_slots": 1}), 1)
        for bad in (0, -1, True, False, 8.0, "8", None, [], {"n": 8}, self.mod.PAIR_SLOTS_MAX + 1):
            self.assertIsNone(slots({"x_alzeo_slots": bad}), bad)
        self.assertIsNone(slots({}))
        self.assertIsNone(slots(["x_alzeo_slots"]))

    def test_the_gap_a_box_may_run_ahead_is_half_its_lanes_slots(self):
        # decision 1014: the 64 of SGLang's router brought to each lane
        gap = self.mod.pair_spread_gap
        for slots, want in ((8, 4), (16, 8), (24, 12), (4, 2), (5, 2), (3, 1), (2, 1), (1, 1)):
            self.assertEqual(gap(slots), want, slots)
        for unknown in (None, 0, -8, True, 8.0, "16", self.mod.PAIR_SLOTS_MAX + 1):
            self.assertEqual(gap(unknown), 4, f"{unknown!r}: the 27B's 8, as before v1.43.0")

    def test_the_slots_of_a_lane_are_read_from_its_engines_command(self):
        slots_in = self.mod.slots_in
        self.assertEqual(slots_in(["argv[]=docker run x --max-running-requests 8 --mem-fraction-static 0.76 ;"]), 8)
        self.assertEqual(slots_in(["vllm serve m --max-num-seqs=16 --port 30000"]), 16)
        self.assertEqual(slots_in(["/home/u/launch.sh", "exec vllm serve m \\\n  --max-num-seqs 24 \\\n"]), 24)
        self.assertEqual(slots_in(["--max-num-seqs 8", "x --max-num-seqs=8"]), 8, "said twice alike")
        self.assertIsNone(slots_in(["--max-num-seqs 8", "--max-num-seqs 16"]), "two values: not known")
        self.assertIsNone(slots_in(["--max-running-requests ${N}", "--max-num-seqs-per-step 9", "--max-num-seqs 8.5"]))
        self.assertIsNone(slots_in(["xx--max-num-seqs 8"]), "the end of another flag")
        self.assertIsNone(slots_in([""]))
        self.assertIsNone(slots_in([]))

    def test_this_box_reads_its_slots_from_its_active_unit_and_the_script_it_runs(self):
        m = self.mod
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        script = tmp / "launch-lane.sh"
        script.write_text("#!/bin/bash\n# --max-num-seqs 99, a comment, counts not\n"
                          "exec vllm serve m --served-model-name m-test --max-num-seqs 16\n")
        # the active unit's main process, as it was started: a script not yet at its exec
        engine = sleeper(self, str(script))
        shown = ("ActiveState=inactive\nMainPID=0\n\n"
                 f"MainPID={engine.pid}\nActiveState=active\n")
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, shown, "")
        saved = (m.HOLD_UNITS, m.subprocess)
        m.HOLD_UNITS = ("qwen38-a.service", "qwen38-b.service")
        m.subprocess = types.SimpleNamespace(run=run)
        m._UNIT_FACTS.update(ts=-1e9, texts=None)

        def restore():
            m.HOLD_UNITS, m.subprocess = saved
            m._UNIT_FACTS.update(ts=-1e9, texts=None)
        self.addCleanup(restore)
        self.assertEqual(m.unit_slots(), 16, "the active unit's script, its comment left out")
        self.assertEqual(m.unit_slots(), 16)
        self.assertEqual(len(calls), 1, "read again 10 s on at most")
        self.assertEqual(calls[0][:2], ["systemctl", "show"])
        m.HOLD_UNITS = ()
        m._UNIT_FACTS.update(ts=-1e9, texts=None)
        self.assertIsNone(m.unit_slots(), "no unit named: a proxy run by hand or by a test")

    def test_this_box_says_the_checkpoint_its_engine_runs_not_the_one_its_unit_names_next(self):
        # switch-model.sh rewrites the unit and reloads systemd without a restart: the box said the new checkpoint while
        # its engine served the old one until its next start (review, 2026-11-04)
        m = self.mod
        engine = sleeper(self, "--served-model-name", "qwen3.8-27b", "--revision", "ab" * 20,
                         "--max-running-requests", "8")
        shown = (f"ActiveState=active\nMainPID={engine.pid}\nExecStart={{ path=/usr/bin/docker ; argv[]=docker run x "
                 f"--served-model-name qwen3.8-27b --revision {'cd' * 20} --max-running-requests 8 ; }}\n")

        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, shown, "")
        saved = (m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT)
        m.HOLD_UNITS = ("qwen38-sglang.service",)
        m.subprocess = types.SimpleNamespace(run=run)
        m.PAIR_CHECKPOINT = None                                                # a box whose units say it
        m._UNIT_FACTS.update(ts=-1e9, texts=None)

        def restore():
            m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT = saved
            m._UNIT_FACTS.update(ts=-1e9, texts=None)
        self.addCleanup(restore)
        self.assertEqual(m.unit_revision(), "ab" * 20, "the unit's next start, not what its engine serves")
        self.assertEqual(m.lane_away(), (frozenset({"qwen3.8-27b"}), "ab" * 20))
        engine.kill()
        engine.wait()
        m._UNIT_FACTS.update(ts=-1e9, texts=None)
        # its process gone: unsure, read again a second on (never kept 10 s), and no checkpoint: what its unit starts is
        # its next start, which an update may have changed (the 4th review of v1.43.0, 2026-11-04)
        self.assertIsNone(m.unit_revision())
        self.assertLess(m._UNIT_FACTS["ts"], time.monotonic() - 8)

    def _systemd_says(self, shown):
        m = self.mod
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, shown["out"], "")
        saved = (m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT, m._LAST_LANE["lane"])
        m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT = ("qwen38-sglang.service",), types.SimpleNamespace(run=run), None
        self._forget()

        def restore():
            m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT = saved[:3]
            m._LAST_LANE["lane"] = saved[3]
            m._UNIT_FACTS.update(states=())
            self._forget()
        self.addCleanup(restore)
        return calls

    def _forget(self):
        """What systemd said, forgotten: read again at the next ask (lane_away keeps it a second, the 4th review)."""
        m = self.mod
        getattr(m, "forget_unit_reads", lambda: None)()
        m._UNIT_FACTS.update(ts=-1e9, texts=None)
        m._HOLD_SEEN["ts"] = -1e9

    def test_an_engine_whose_command_reads_empty_is_read_again_not_kept(self):
        # its process between fork and exec, or a zombie: an empty command was kept 10 s, the box saying no checkpoint
        # and no slots for that long after a start (the delta's review, 2026-11-04)
        m = self.mod
        p = subprocess.Popen(["true"])                                          # a zombie: its command line is empty
        self.addCleanup(p.wait)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with open(f"/proc/{p.pid}/stat") as f:
                if f.read().split(")")[-1].split()[0] == "Z":
                    break
            time.sleep(0.01)
        calls = self._systemd_says({"out": f"ActiveState=active\nMainPID={p.pid}\n"})
        self.assertIsNone(m.unit_texts())
        time.sleep(1.1)                                                         # an unsure read is kept a second
        self.assertIsNone(m.unit_texts())
        self.assertEqual(len(calls), 2, "an empty command was kept 10 s")

    def _systemd_answers(self, jobs, shown, starts, run=None, changed=None, left=None):
        """systemctl stubbed: list-jobs says jobs, show -p ExecStart --value says starts, show -p InvocationID says run
        (the unit's run, decision 1049), changed (its last state change, unix time; now by default) and left (when it
        left active, unix time; not said by default), any other show says shown."""
        m = self.mod

        def answer(argv, **kw):
            if argv[1] == "list-jobs":
                out = jobs["out"]
            elif "--value" in argv and "ExecStart" in argv:
                out = starts["out"]
            elif "InvocationID" in argv:
                at = (changed or {}).get("at", time.time())
                out = f"InvocationID={(run or {'out': ''})['out'].strip()}\nStateChangeTimestamp=@{int(at)}\n"
                out += f"ActiveExitTimestamp=@{int(left['at'])}\n" if left else "ActiveExitTimestamp=\n"
            else:
                out = shown["out"]
            return subprocess.CompletedProcess(argv, 0, out, "")
        saved = (m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT, m._LAST_LANE["lane"])
        m.HOLD_UNITS, m.subprocess = ("qwen38-sglang.service",), types.SimpleNamespace(run=answer)
        m.PAIR_CHECKPOINT = None
        self._forget()

        def restore():
            m.HOLD_UNITS, m.subprocess, m.PAIR_CHECKPOINT = saved[:3]
            m._LAST_LANE["lane"] = saved[3]
            m._UNIT_FACTS.update(states=())
            self._forget()
        self.addCleanup(restore)

    def test_a_lane_stopped_by_order_is_no_lane_here_and_a_restart_brings_back_what_its_unit_starts(self):
        # the delta's 3rd review: a unit stopping by order (a Load of another lane) kept the lane seen last, and its
        # model's requests were held through the next lane's whole boot, then refused
        m = self.mod
        jobs = {"out": "7 qwen38-sglang.service stop running\n"}
        shown = {"out": "ActiveState=deactivating\nMainPID=0\n"}
        starts = {"out": f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                         f"{'ab' * 20} ; }}"}
        self._systemd_answers(jobs, shown, starts)
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self.assertEqual(m.lane_away(), (frozenset(), None), "a lane stopped by order kept its model as this box's")
        jobs["out"] = "8 qwen38-sglang.service restart running\n"            # a restart: what it brings back
        self._forget()                                                          # lane_away keeps its read a second
        self.assertEqual(m.lane_away(), (frozenset({"qwen3.8-27b"}), "ab" * 20))

    def test_a_restart_to_another_checkpoint_is_an_order_and_to_the_same_one_is_not(self):
        # decision 1041: the cockpit's Load of another checkpoint of the same lane is a restart; a recovery or an update
        # restarts on the same one (decision 1030)
        m = self.mod
        jobs = {"out": "8 qwen38-sglang.service restart running\n"}
        starts = {"out": f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                         f"{'ab' * 20} ; }}"}
        self._systemd_answers(jobs, {"out": ""}, starts)
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)       # it served another checkpoint
        self.assertTrue(m.unit_stopping_by_order(), "the Load of another checkpoint bound what it ended")
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "ab" * 20)       # the same one: a recovery
        self.assertFalse(m.unit_stopping_by_order())
        jobs["out"] = "9 qwen38-sglang.service stop running\n"
        self.assertTrue(m.unit_stopping_by_order())
        jobs["out"] = ""
        self.assertFalse(m.unit_stopping_by_order())

    def test_a_restart_the_cockpit_makes_to_recover_its_engine_is_no_order_whatever_it_loads(self):
        # decision 1049: the cockpit marks its restarts of a wedged or zombie engine with the run they restart. A restart
        # to another checkpoint than the one served is an order (1041) without that mark, and a mark of another run (a
        # Load since) is none (the 4th review of v1.43.0, 2026-11-04)
        m = self.mod
        jobs = {"out": "8 qwen38-sglang.service restart running\n"}
        starts = {"out": f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                         f"{'ab' * 20} ; }}"}
        run = {"out": "run-1\n"}
        self._systemd_answers(jobs, {"out": ""}, starts, run)
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)       # it served another checkpoint
        marks = pathlib.Path(tempfile.mkdtemp(prefix="kp-recovery-")) / "recovery-restarts.json"
        self.addCleanup(shutil.rmtree, marks.parent, True)
        saved = getattr(m, "RECOVERY_FILE", None)
        m.RECOVERY_FILE = str(marks)
        self.addCleanup(setattr, m, "RECOVERY_FILE", saved)
        self.assertTrue(m.unit_stopping_by_order(), "no mark: the Load of another checkpoint, an order (1041)")
        marks.write_text(json.dumps({"qwen38-sglang.service": {"invocation": "run-1", "at": time.time(),
                                                                "why": "autoheal"}}))
        self.assertFalse(m.unit_stopping_by_order(), "the cockpit's recovery was taken for an order")
        run["out"] = "run-2\n"                                                  # a later run: the mark is another's
        self.assertTrue(m.unit_stopping_by_order())
        marks.write_text("[1, 2]")                                              # a mark that cannot be read
        run["out"] = "run-1\n"
        self.assertTrue(m.unit_stopping_by_order())

    def _marked(self, at, run="run-1"):
        """RECOVERY_FILE a file of this test's, saying the cockpit recovers the 27B's run `run`, marked at `at`."""
        m = self.mod
        marks = pathlib.Path(tempfile.mkdtemp(prefix="kp-recovery-")) / "recovery-restarts.json"
        self.addCleanup(shutil.rmtree, marks.parent, True)
        saved = m.RECOVERY_FILE
        m.RECOVERY_FILE = str(marks)
        self.addCleanup(setattr, m, "RECOVERY_FILE", saved)
        marks.write_text(json.dumps({"qwen38-sglang.service": {"invocation": run, "at": at, "why": "autoheal"}}))
        return marks

    def test_a_recovery_mark_left_behind_is_no_recovery(self):
        # a mark the cockpit left on a run whose restart was refused made a later Load of that run a recovery, which
        # binds what it ends (the 5th review of v1.43.0, 2026-11-04): no mark older than RECOVERY_MARK_S counts
        m = self.mod
        jobs = {"out": "8 qwen38-sglang.service restart running\n"}
        starts = {"out": f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                         f"{'ab' * 20} ; }}"}
        self._systemd_answers(jobs, {"out": ""}, starts, {"out": "run-1\n"})
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self._marked(time.time() - 700)
        self.assertTrue(m.unit_stopping_by_order(), "a stale mark made the Load of another checkpoint a recovery")

    def test_the_run_is_read_before_the_jobs(self):
        # read after the jobs, the run could be the restart's next one, its stop half over: the cockpit's recovery then
        # read as an order, and the request that wedged the engine went unbound (the 5th review of v1.43.0, 2026-11-04)
        m = self.mod
        asked = []

        def answer(argv, **kw):
            asked.append(argv[1] if argv[1] == "list-jobs" else "show " + argv[-2])
            if argv[1] == "list-jobs":
                out = "8 qwen38-sglang.service restart running\n"
            elif "ExecStart" in argv:
                out = f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision {'ab' * 20} ; }}"
            elif "InvocationID" in argv:                                        # its next run, once the jobs are read
                out = (f"InvocationID={'run-2' if 'list-jobs' in asked else 'run-1'}\n"
                       f"StateChangeTimestamp=@{int(time.time())}\n")
            else:
                out = ""
            return subprocess.CompletedProcess(argv, 0, out, "")
        self._systemd_answers({"out": ""}, {"out": ""}, {"out": ""})
        m.subprocess = types.SimpleNamespace(run=answer)
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self._marked(time.time())
        self.assertFalse(m.unit_stopping_by_order(), f"the cockpit's recovery read as an order ({asked})")

    def test_a_stop_is_an_order_whatever_the_marks(self):
        # decision 1059: the operator's stop was marked as a recovery (1055), and the Load journey's own stop of a stuck
        # engine with it, its requests bound 10 minutes and refused (the 6th review of v1.43.0, 2026-11-04): withdrawn
        m = self.mod
        jobs = {"out": "9 qwen38-sglang.service stop running\n"}
        self._systemd_answers(jobs, {"out": "ActiveState=deactivating\nMainPID=0\n"}, {"out": ""}, {"out": "run-1\n"})
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self._marked(time.time())
        self.assertTrue(m.unit_stopping_by_order(), "a marked stop read as a recovery")
        self.assertEqual(m.lane_away(), (frozenset(), None))

    def test_a_refusal_for_another_checkpoint_keeps_the_name(self):
        # the other box's 409 for another checkpoint of the model dropped the name from its row like a 409 for another
        # model, and peer_may_serve then said no box served it (the 6th review of v1.43.0, 2026-11-04)
        m = self.mod
        same = json.dumps({"error": {"type": "model_not_served", "message": "x",
                                     "served": {"spark2": ["qwen3.8-27b"]}}}).encode()
        other = json.dumps({"type": "error", "error": {"type": "model_not_served", "message": "x",
                                                       "served": {"spark2": ["qwen3.8-flash-next-fp4"]}}}).encode()
        self.assertTrue(m.refusal_serves(same, "qwen3.8-27b"))
        self.assertFalse(m.refusal_serves(other, "qwen3.8-27b"))
        self.assertFalse(m.refusal_serves(b"not json", "qwen3.8-27b"))
        h = object.__new__(m.H)
        h._peer = "test"
        with m._PAIR_LOCK:
            saved = dict(m._PAIR_MODELS)
        self.addCleanup(lambda: (m._PAIR_MODELS.clear(), m._PAIR_MODELS.update(saved)))
        # its revision dropped too, which the 409 proves stale: kept, the next requests went there for a 409 each (the
        # 7th review of v1.43.0, 2026-11-04)
        for res, models, revision in (("moved checkpoint", ("qwen3.8-27b", "marker"), None),
                                      ("moved", ("marker",), "ab" * 20)):
            with self.subTest(res=res):
                m._PAIR_MODELS["spark2"] = {"models": ("qwen3.8-27b", "marker"), "ts": 5.0, "revision": "ab" * 20}
                h._pair_refused("spark2", "qwen3.8-27b", res, "read again")
                row = m._PAIR_MODELS["spark2"]
                self.assertEqual((row["models"], row["revision"], row["ts"]), (models, revision, 0.0))

    def test_a_recovery_mark_counts_for_the_restart_it_was_written_for(self):
        # a mark the cockpit left behind (stopped between the mark and the restart) counted for the next restart of that
        # run within 10 minutes, the Load of another checkpoint then binding what it ended (the 7th review of v1.43.0,
        # 2026-11-04): the unit's state must change after the mark, within RECOVERY_START_S
        m = self.mod
        jobs = {"out": "8 qwen38-sglang.service restart running\n"}
        starts = {"out": f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                         f"{'ab' * 20} ; }}"}
        changed = {"at": time.time()}
        self._systemd_answers(jobs, {"out": ""}, starts, {"out": "run-1\n"}, changed)
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self._marked(time.time() - 300)                                         # fresh, written for another restart
        self.assertTrue(m.unit_stopping_by_order(), "a mark left behind counted for a later restart")
        self._marked(time.time() - 1)                                           # written for this one
        self.assertFalse(m.unit_stopping_by_order())

    def test_a_recovery_mark_outlives_a_stop_slower_than_its_window(self):
        # systemd stamps every step of a stop (its unit_notify): read alone, the last state change moved past
        # RECOVERY_START_S while a slow stop went on, and the cockpit's recovery then read as an order. The moment the
        # unit left active is set once (the 8th review of v1.43.0, 2026-11-05)
        m = self.mod
        jobs = {"out": "8 qwen38-sglang.service restart running\n"}
        starts = {"out": f"{{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                         f"{'ab' * 20} ; }}"}
        now = time.time()
        self._systemd_answers(jobs, {"out": ""}, starts, {"out": "run-1\n"}, {"at": now}, {"at": now - 199})
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self._marked(now - 200)                                                 # its stop began a second after it
        self.assertFalse(m.unit_stopping_by_order(), "a slow stop outlived the recovery's mark")
        self.assertEqual(m.unix_stamp("@1730757600"), 1730757600.0)
        self.assertIsNone(m.unix_stamp(""))
        self.assertIsNone(m.unix_stamp("n/a"))

    def test_undoing_a_requests_binding_gives_back_the_one_before(self):
        # an order that ended a retry erased the binding an earlier real death of the same body had made, and the next
        # retry could take this box's engine down too (decision 1019; the 8th review of v1.43.0, 2026-11-05)
        m = self.mod
        self.addCleanup(m._KILLERS.clear)

        def request(fp):
            h = object.__new__(m.H)
            h._killer_fp, h._killer_bound, h._killer_before = fp, False, None
            return h
        m.killer_note("f" * 16, "spark2")                                       # an earlier death of that body there
        h = request("f" * 16)
        h._bind_killer("spark2")                                                # this request's own, at its death
        h._bind_killer("spark2")                                                # and again at its stream's end
        h._unbind_killer("spark2")                                              # the order said after it
        self.assertEqual(m.killer_box("f" * 16), "spark2", "an order erased the binding of an earlier death")
        h = request("e" * 16)
        h._bind_killer("")
        h._unbind_killer("")
        self.assertIsNone(m.killer_box("e" * 16), "nothing was bound before it: nothing is left")
        m.killer_note("d" * 16, "spark2")                                       # bound to the other box before
        h = request("d" * 16)
        h._bind_killer("")                                                      # this box's engine died on it since
        h._unbind_killer("")
        self.assertEqual(m.killer_box("d" * 16), "spark2")

    def test_an_answer_relayed_whole_leaves_the_stops_reach_at_once(self):
        # set at its relay, the keeper's join left a window where a stop cut a finished answer, and on vLLM shut its
        # upstream mid-body (the 7th review of v1.43.0, 2026-11-04): set as the engine gives it, under the stop's lock
        m = self.mod

        class Answer:
            def __init__(self, ctype):
                self.headers = {"Content-Type": ctype}

        def taken(ctype, stopped=None):
            h = object.__new__(m.H)
            h._stop_why = stopped
            h._take_whole_answer(Answer(ctype))
            return getattr(h, "_relaying_whole", False)
        self.assertTrue(taken("application/json"))
        self.assertFalse(taken("text/event-stream"))
        self.assertFalse(taken("application/x-ndjson"))
        self.assertFalse(taken("application/json", "a floor stop"), "a stopped answer was taken out of its reach")

    def test_a_death_in_ndjson_is_a_whole_lines_top_level_error(self):
        # matched anywhere in the bytes, an Ollama tool call's arguments, a raw object, could carry the words (the 7th
        # review of v1.43.0, 2026-11-04)
        m = self.mod
        dead = b'{"error": {"message": "EngineCore encountered an issue. See stack trace (above) for the root cause."}}\n'
        args = (b'{"message": {"tool_calls": [{"function": {"arguments": {"error": {"message": "EngineCore '
                b'encountered an issue."}}}}]}, "done": false}\n')
        self.assertTrue(m.ndjson_death(b'{"done": false}\n' + dead))
        self.assertFalse(m.ndjson_death(args))
        self.assertFalse(m.ndjson_death(b'"error" EngineCore encountered an issue.\n'))

    def test_an_order_split_across_two_reads_is_read_whole(self):
        # its first half in the last read, its second in the next: the wait for an order never matched it, and the request
        # stayed bound (the 7th review of v1.43.0, 2026-11-04)
        m = self.mod

        class Stream:
            def __init__(self, parts):
                self.parts = list(parts)

            def read1(self, n):
                return self.parts.pop(0) if self.parts else b""
        h = object.__new__(m.H)
        h._peer = "test"
        half = len(HOP_ENDED_OPENAI) // 2
        self.assertEqual(h._end_after(Stream([HOP_ENDED_OPENAI[half:]]), None, HOP_ENDED_OPENAI[:half]), (False, True))
        self.assertEqual(h._end_after(Stream([b'data: {"choices": []}\n\n']), None, b""), (False, False))

    def test_a_death_split_where_its_client_leaves_is_read_whole(self):
        # read after its client left (decision 1112), a death whose first half came in the last read relayed: its SSE
        # event, and an NDJSON line held back there
        m = self.mod

        class Stream:
            def __init__(self, parts):
                self.parts = list(parts)

            def read1(self, n):
                return self.parts.pop(0) if self.parts else b""
        h = object.__new__(m.H)
        h._peer = "test"
        event = b'data: {"error": {"message": "keepalive-proxy: ' + m.HOP_CLOSED.encode() + b'"}}\n\n'
        line = json.dumps({"error": {"message": "keepalive-proxy: " + m.HOP_CLOSED}}).encode() + b"\n"
        for sse, whole, before in ((True, event, b'data: {"choices": []}\n\n'), (False, line, b'{"done": false}\n')):
            with self.subTest(sse=sse):
                for cut in (1, len(whole) // 2, len(whole) - 2):
                    self.assertEqual(h._end_after(Stream([whole[cut:]]), None, before + whole[:cut], sse), (True, False),
                                     f"a death split at byte {cut} went unread")
                self.assertEqual(h._end_after(Stream([b'{"done": true}\n' if not sse else b"data: [DONE]\n\n"]), None,
                                              before, sse), (False, False))

    def test_the_ndjson_order_line_starts_a_line_of_its_own(self):
        # after a line over 64 KiB relayed unended, the order line glued to it went unread at the entry, which bound the
        # request and gave its client a broken line (the 7th review of v1.43.0, 2026-11-04)
        import io
        m = self.mod
        h = object.__new__(m.H)
        h._pair_box, h.path, h.wfile = "spark2", "/api/chat", io.BytesIO()
        h._held_sse, h._out_sse, h._out_ndjson, h._out_begun, h._nd_open = False, False, True, True, True
        h._say_ended_by_order()
        line = b"\n" + m.HOP_ENDED_NDJSON
        self.assertEqual(h.wfile.getvalue(), b"%x\r\n%s\r\n" % (len(line), line))

    def test_a_peer_never_read_may_serve_for_the_first_read_only_in_both_rules(self):
        # the never-read rule lived in peer_may_serve alone: _pair_served_nowhere's waited_for read a missing row as {}
        # (the 7th review of v1.43.0, 2026-11-04)
        m = self.mod
        saved = m._PAIR["loaded"]
        self.addCleanup(m._PAIR.__setitem__, "loaded", saved)
        m._PAIR["loaded"] = time.monotonic()
        self.assertTrue(m.peer_row_may_serve(None, "qwen3.8-27b"))
        m._PAIR["loaded"] = time.monotonic() - m.PAIR_FIRST_READ_S - 1
        self.assertFalse(m.peer_row_may_serve(None, "qwen3.8-27b"))

    def test_an_engine_away_reads_systemd_once_a_second_whatever_the_requests(self):
        # lane_away read systemd afresh for each request while this box's engine was away: two or three systemctl a
        # request, for hours on a box serving images (the 4th review of v1.43.0, 2026-11-04)
        m = self.mod
        calls = self._systemd_says({"out": "ActiveState=inactive\nMainPID=0\n"})
        for _ in range(20):
            self.assertEqual(m.lane_away(), (frozenset(), None))               # images now, or no lane
        self.assertLessEqual(len(calls), 3, [c[1] for c in calls])

    def test_a_read_of_systemd_that_failed_decides_nothing(self):
        # a systemctl that failed stored no state, which lane_away read as no text lane: the lane's model went to the
        # other box whatever its checkpoint (decision 1018), and the failure was kept 10 s (the 4th review of v1.43.0)
        m = self.mod
        self._systemd_says({"out": ""})

        def times_out(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 3)

        def refused(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus")
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        for fails in (times_out, refused):
            with self.subTest(fails=fails.__name__):
                m.subprocess = types.SimpleNamespace(run=fails)
                self._forget()
                self.assertEqual(m.lane_away(), (frozenset({"qwen3.8-27b"}), "cd" * 20))
                self.assertLess(m._UNIT_FACTS["ts"], time.monotonic() - 8, "a read that failed was kept 10 s")

    def test_a_main_process_gone_or_a_zombie_names_no_checkpoint(self):
        # its unit's ExecStart is its NEXT start: read for a main process gone or a zombie (a crash, before systemd reaps
        # it), this box said the checkpoint an update or switch-model.sh had staged for the one it served (the 4th review
        # of v1.43.0, 2026-11-04)
        m = self.mod
        p = subprocess.Popen(["true"])                                          # a zombie: not reaped yet
        self.addCleanup(p.wait)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with open(f"/proc/{p.pid}/stat") as f:
                if f.read().split(")")[-1].split()[0] == "Z":
                    break
            time.sleep(0.01)
        starts = (f"ExecStart={{ path=/usr/bin/docker ; argv[]=docker run x --served-model-name qwen3.8-27b --revision "
                  f"{'ab' * 20} ; }}\n")
        shown = {"out": f"ActiveState=active\nMainPID={p.pid}\n{starts}"}
        self._systemd_says(shown)
        self.assertIsNone(m.unit_texts(), "a zombie said what its unit starts next")
        shown["out"] = f"ActiveState=active\nMainPID=4194305\n{starts}"        # over any pid_max: gone
        self._forget()
        self.assertIsNone(m.unit_texts(), "a process gone said what its unit starts next")
        shown["out"] = f"ActiveState=activating\nMainPID=0\n{starts}"           # a start under way: what it starts
        self._forget()
        self.assertEqual(m.unit_revision(), "ab" * 20)

    def test_a_script_that_is_not_utf8_is_read_all_the_same(self):
        # a launcher edited by hand in Latin-1 raised UnicodeDecodeError out of unit_stopping_by_order and lane_away under
        # a restart job, and the request's handler died with no answer (the 4th review of v1.43.0, 2026-11-04)
        m = self.mod
        d = pathlib.Path(tempfile.mkdtemp(prefix="kp-latin1-"))
        self.addCleanup(shutil.rmtree, d, True)
        script = d / "launch.sh"
        script.write_bytes(b"#!/bin/bash\n# caf\xe9\nexec docker run x --served-model-name qwen3.8-27b\n")
        self.assertEqual(m.names_in(m.with_scripts(f"/bin/bash {script}")), frozenset({"qwen3.8-27b"}))

    def test_a_command_that_names_no_model_yet_reads_what_the_unit_starts(self):
        # systemd 255 runs systemd-executor as the main process before the exec: a command naming no model, kept 10 s,
        # said no checkpoint and no slots (the delta's 3rd review, 2026-11-04)
        m = self.mod
        executor = sleeper(self, "systemd-executor", "--deserialize", "3")
        shown = {"out": f"ActiveState=active\nMainPID={executor.pid}\nExecStart={{ path=/usr/bin/docker ; argv[]=docker "
                        f"run x --served-model-name qwen3.8-27b --revision {'ab' * 20} --max-running-requests 8 ; }}\n"}
        self._systemd_answers({"out": ""}, shown, {"out": ""})
        self.assertEqual(m.unit_revision(), "ab" * 20)
        self.assertEqual(m.unit_slots(), 8)
        self.assertLess(m._UNIT_FACTS["ts"], time.monotonic() - 8, "an unsure read was kept 10 s")

    def test_the_bindings_that_ended_leave_on_any_lookup(self):
        # after one binding, a box alone hashed every body for good: only a note purged the ones that ended
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        m._KILLERS["ended"] = ("b", time.monotonic() - 1)
        self.assertIsNone(m.killer_box("another"))
        self.assertEqual(len(m._KILLERS), 0, "a binding that ended stayed")

    def test_this_box_notes_itself_as_the_empty_name(self):
        # its pair name noted for itself read as a box that left the pair once pair.json was gone (the delta's 3rd review)
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = (m.unit_stopping_by_order, m.pair_conf)
        m.unit_stopping_by_order, m.pair_conf = (lambda: False), (lambda: {"name": "spark1", "peers": []})
        self.addCleanup(lambda: (setattr(m, "unit_stopping_by_order", saved[0]), setattr(m, "pair_conf", saved[1])))
        h = object.__new__(m.H)
        h._killer_fp, h._stop_why = "fp-own", None
        h._killed_here()
        self.assertEqual(m._KILLERS["fp-own"][0], "")

    def test_a_unit_starting_says_what_it_starts_and_one_stopping_keeps_the_lane_seen_last(self):
        # the delta's review, 2026-11-04: an "activating" unit (ExecStartPre, the wait before a restart) was skipped, and
        # a proxy restarted with its engine sent its 27B to the other box whatever its checkpoint; a unit stopping after
        # a crash, its restart not due yet, read as no lane at all
        m = self.mod
        shown = {"out": "ActiveState=activating\nMainPID=0\nExecStart={ path=/usr/bin/docker ; argv[]=/usr/bin/docker run "
                        f"x --served-model-name qwen3.8-27b --revision {'ab' * 20} ; ignore_errors=no ; }}\n"}
        self._systemd_says(shown)
        self.assertEqual(m.lane_away(), (frozenset({"qwen3.8-27b"}), "ab" * 20))
        shown["out"] = "ActiveState=deactivating\nMainPID=0\n"                 # stopping, no job: a crash
        self._forget()
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "cd" * 20)
        self.assertEqual(m.lane_away(), (frozenset({"qwen3.8-27b"}), "cd" * 20))
        shown["out"] = "ActiveState=inactive\nMainPID=0\n"                     # no text lane: images, or none
        self._forget()
        self.assertEqual(m.lane_away(), (frozenset(), None))

    def test_a_dead_engines_answer_relayed_in_a_stream_the_keeper_opened_is_known(self):
        # _answer prefixes "keepalive-proxy: " to the message it relays in a stream it opened: the entry missed vLLM's
        # death there (the delta's review, 2026-11-04)
        m = self.mod
        said = "EngineCore encountered an issue. See stack trace (above) for the root cause."
        for event in (m.sse_error_openai(said, "InternalServerError"), m.sse_error(said)):
            self.assertTrue(m.ENGINE_DIED_RE.search(event), event[:120])

    def test_the_names_a_command_serves_are_read_from_it(self):
        names_in = self.mod.names_in
        self.assertEqual(names_in(["docker run x --served-model-name qwen3.8-27b --revision ab"]),
                         frozenset({"qwen3.8-27b"}))
        self.assertEqual(names_in(["exec vllm serve m --served-model-name=ornith-1.5-122b \\\n --port 1"]),
                         frozenset({"ornith-1.5-122b"}))
        self.assertEqual(names_in(["x --served-model-name-extra y", "xx--served-model-name z"]), frozenset())
        self.assertEqual(names_in([]), frozenset())

    def test_this_box_says_its_slots_in_the_list_it_gives_the_other(self):
        m = self.mod
        data = json.dumps({"object": "list", "data": [{"id": "qwen3.8-27b", "object": "model"}]}).encode()
        saved = m.unit_slots
        self.addCleanup(setattr, m, "unit_slots", saved)
        m.unit_slots = lambda: 16
        self.assertEqual(json.loads(m.running_said(data, "spark2"))["x_alzeo_slots"], 16)
        m.unit_slots = lambda: None
        self.assertNotIn("x_alzeo_slots", json.loads(m.running_said(data, "spark2")), "slots not known, none said")

    def test_the_gap_is_that_of_the_box_the_turn_would_go_to(self):
        m = self.mod
        conf = {"name": "me", "peers": []}
        saved = m.unit_slots
        self.addCleanup(setattr, m, "unit_slots", saved)
        m.unit_slots = lambda: 24
        self.assertEqual(m.pair_slots_of("me", conf), 24, "this box: its unit")
        with m._PAIR_LOCK:
            m._PAIR_MODELS["peer-s"] = {"slots": 16}
        self.addCleanup(m._PAIR_MODELS.pop, "peer-s", None)
        self.assertEqual(m.pair_slots_of("peer-s", conf), 16, "the other box: what its list said")
        self.assertIsNone(m.pair_slots_of("gone", conf))

    def test_the_take_over_learns_from_a_refusal_as_the_sharing_does(self):
        # a box that said 503 to a take-over gets no shared request for a while, and one that serves another model now
        # is read again (review, 2026-11-03): no test failed with the take-over learning nothing
        m = self.mod

        def cleanup():
            m._PAIR_COOL.pop("p-cool", None)
            with m._PAIR_LOCK:
                m._PAIR_MODELS.pop("p-moved", None)
        self.addCleanup(cleanup)
        for peer, res in (("p-cool", "fallback"), ("p-moved", "moved")):
            h = object.__new__(m.H)
            h._peer, h._hold_until, h._held_sse = "test", None, False
            h._forward_peer = lambda *a, res=res, **k: res
            h._pair_alt = ([{"name": peer}], "qwen3.8-27b", {"name": "me", "peers": []}, None, [], TEST_REV)
            with m._PAIR_LOCK:
                m._PAIR_MODELS[peer] = {"models": ("qwen3.8-27b",), "ts": time.monotonic()}
            self.assertFalse(h._pair_take_over(b"{}"), res)
        self.assertGreater(m._PAIR_COOL.get("p-cool", 0), time.monotonic(), "a box that said 503 got no pause")
        self.assertNotIn("qwen3.8-27b", m._PAIR_MODELS["p-moved"]["models"], "a box that serves another model kept it")

    def test_a_box_gone_from_the_pair_file_keeps_none_of_its_facts(self):
        # pair.py remove on a box without a name deletes pair.json: the box paired next under the same name got the
        # conversations, the pause and the tile's counts of the one before; a pair.json this box cannot use, the same
        # (review, 2026-11-03)
        m = self.mod
        port = localports.held(self)
        self.addCleanup(lambda: [d.clear() for d in (m._AFFINITY, m._PAIR_COOL, m._PAIR_SPREAD)])
        for gone in ("removed", "unreadable"):
            with self.subTest(gone=gone):
                self.conf_now({"name": "spark1", "peers": [peer_entry(self.cfg, "spark2", port)]})
                m.affinity_note("spark2", [(4096, "fp-spark2")], time.monotonic())
                m._PAIR_COOL["spark2"] = time.monotonic() + 30
                m.pair_spread_count("spark2", "balance")
                if gone == "removed":
                    self.pair_file.unlink()
                else:
                    self.pair_file.write_text("{ not json")
                m._PAIR["seen"] = 0.0
                self.assertIsNone(m.pair_conf())
                facts = ("spark2" in m._AFFINITY, "spark2" in m._PAIR_COOL, "spark2" in m._PAIR_SPREAD)
                self.assertEqual(facts, (False, False, False), f"pair.json {gone}: what was learned of spark2 stayed")

    def test_the_sharing_timings_keep_their_defaults_when_set_wrong(self):
        # PAIR_SHARE_CONNECT_S: 0 made the connect non-blocking (every shared send failed, then a pause), -1, nan or
        # inf raised an error nothing caught, as PAIR_CONNECT_S before its own guard. PAIR_SPREAD_COOL_S: nan paused a
        # box for ever (review, 2026-11-03)
        for name, said, want in (("PAIR_SHARE_CONNECT_S", "0", 3.0), ("PAIR_SHARE_CONNECT_S", "-1", 3.0),
                                 ("PAIR_SHARE_CONNECT_S", "nan", 3.0), ("PAIR_SHARE_CONNECT_S", "inf", 3.0),
                                 ("PAIR_SPREAD_COOL_S", "nan", 30.0), ("PAIR_SPREAD_COOL_S", "inf", 30.0),
                                 ("PAIR_SPREAD_COOL_S", "-5", 30.0), ("PAIR_SPREAD_COOL_S", "0", 0.0),
                                 ("PAIR_CONNECT_S", "inf", 10.0), ("PAIR_CONNECT_S", "nan", 10.0),
                                 # an empty value stopped the proxy at its start, -1 cut every stream (the review of
                                 # v1.44.0)
                                 ("SPOOL_MAX_BYTES", "", 32 << 20), ("SPOOL_MAX_BYTES", "nan", 32 << 20),
                                 ("SPOOL_MAX_BYTES", "inf", 32 << 20), ("SPOOL_MAX_BYTES", "-1", 32 << 20),
                                 ("SPOOL_MAX_BYTES", "x", 32 << 20), ("SPOOL_MAX_BYTES", "0", 0),
                                 ("SPOOL_MAX_BYTES", "0.5", 1 << 19)):
            saved = dict(os.environ)
            env = "PROXY_SPOOL_MAX_MIB" if name == "SPOOL_MAX_BYTES" else name
            os.environ.update({"HOME": str(self.tmp), "QWEN38_PAIR_FILE": str(self.pair_file), "PAIR_PORT": "0",
                               "PROXY_BIND": "127.0.0.1", env: said})
            try:
                spec = importlib.util.spec_from_file_location(f"kp_pair_share_{time.time_ns()}",
                                                              REPO / "keepalive-proxy.py")
                mod = importlib.util.module_from_spec(spec)
                saved_argv, sys.argv = sys.argv, ["keepalive-proxy.py"]
                try:
                    spec.loader.exec_module(mod)
                finally:
                    sys.argv = saved_argv
            finally:
                os.environ.clear()
                os.environ.update(saved)
            self.assertEqual(getattr(mod, name), want, f"{name}={said}")

    def test_the_end_by_order_is_said_in_events_every_client_takes(self):
        m = self.mod
        for event in (m.HOP_ENDED_ANTHROPIC, m.HOP_ENDED_OPENAI):
            self.assertTrue(m.ended_by_order_in(b"data: {}\n\n" + event))
            data = [ln for ln in event.split(b"\n") if ln.startswith(b"data: ")]
            self.assertEqual(len(data), 1)
            json.loads(data[0][6:])                                             # one JSON event, not a comment
            self.assertFalse(event.startswith(b":"), "a comment kills the Claude-family parsers (role 1)")
        self.assertEqual(m.HOP_ENDED_OPENAI, HOP_ENDED_OPENAI)
        quoted = ("data: " + json.dumps({"choices": [{"delta": {"content": '"x_alzeo_ended": "order"'}}]})).encode()
        self.assertFalse(m.ended_by_order_in(quoted), "model output quoting it")
        self.assertTrue(m.ended_by_order_in(b'{"model": "m", "done": false}\n' + m.HOP_ENDED_NDJSON))
        # an Ollama route's tool call carries its arguments as an object: one set a field matched anywhere (the delta's
        # 3rd review, 2026-11-04)
        call = json.dumps({"model": "m", "message": {"role": "assistant", "tool_calls": [{"function": {
            "name": "f", "arguments": {"x_alzeo_ended": "order"}}}]}, "done": False}).encode() + b"\n"
        self.assertFalse(m.ended_by_order_in(call), "a tool call's arguments said an end by order")
        self.assertFalse(m.ended_by_order_in(b'{"x_alzeo_ended": "order"} and more\n'), "not a line of its own")

    def test_an_ollama_stream_this_box_ends_by_order_says_so_on_a_line_of_its_own(self):
        # the Ollama routes' NDJSON streams said nothing before an ordered cut, and the other box bound them (the delta's
        # 3rd review, 2026-11-04)
        import io
        m = self.mod
        h = object.__new__(m.H)
        h._pair_box, h.path, h.wfile = "spark2", "/api/chat", io.BytesIO()
        h._held_sse, h._out_sse, h._out_ndjson, h._out_begun = False, False, True, True
        h._say_ended_by_order()
        h._say_ended_by_order()                                                 # said once
        self.assertEqual(h.wfile.getvalue(), b"%x\r\n%s\r\n" % (len(m.HOP_ENDED_NDJSON), m.HOP_ENDED_NDJSON))

    def test_only_a_stop_job_is_an_order_and_a_restart_is_on_its_way_back(self):
        m = self.mod
        listed = {"out": ""}

        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, listed["out"], "")
        saved = (m.HOLD_UNITS, m.subprocess)
        m.HOLD_UNITS, m.subprocess = ("qwen38-sglang.service", "qwen38-flash.service"), types.SimpleNamespace(run=run)
        self.addCleanup(lambda: (setattr(m, "HOLD_UNITS", saved[0]), setattr(m, "subprocess", saved[1])))
        for jobs, order in (("12 qwen38-sglang.service stop running\n", True),
                            ("13 qwen38-sglang.service restart running\n", False),
                            ("14 qwen38-flash.service start waiting\n", False),
                            ("15 qwen38-other.service stop running\n", False),      # not a lane's unit
                            ("", False)):
            listed["out"] = jobs
            self.assertEqual(m.unit_stopping_by_order(), order, jobs)
        listed["out"] = "13 qwen38-sglang.service restart running\n"
        self.assertEqual(m.unit_jobs(), [("qwen38-sglang.service", "restart")])

    def test_the_marker_of_an_engine_closing_on_a_hop_is_its_error_event_only(self):
        m = self.mod
        for event in (m.sse_error_openai(m.HOP_CLOSED, "upstream_closed"), m.sse_error(m.HOP_CLOSED)):
            self.assertIn(m.HOP_CLOSED_B, event)
        quoted = json.dumps({"choices": [{"delta": {"content": '"message": "keepalive-proxy: ' + m.HOP_CLOSED + '"'}}]})
        self.assertNotIn(m.HOP_CLOSED_B, ("data: " + quoted).encode(), "model output quoting it bound a request")
        self.assertNotIn(m.HOP_CLOSED_B, m.HOP_CLOSED.encode(), "the bare sentence, in any answer")

    def test_the_bindings_that_ended_leave_as_others_come(self):
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        m._KILLERS["old-ended"] = ("b", time.monotonic() - 1)
        m.killer_note("new-one", "b")
        self.assertNotIn("old-ended", m._KILLERS)
        self.assertIn("new-one", m._KILLERS)

    def test_the_checkpoint_of_a_lane_is_read_from_its_engines_command(self):
        m = self.mod
        rev, other = "ab" * 20, "cd" * 20
        self.assertEqual(m.revision_in([f"--model-path R/Qwen --revision {rev} --max-running-requests 8"]), rev)
        self.assertEqual(m.revision_in([f"vllm serve n/F --revision={rev}",
                                        f"--speculative-draft-model-revision {other} --tokenizer-revision {other}"]), rev,
                         "a draft model's or a tokenizer's revision is another flag")
        self.assertIsNone(m.revision_in([f"--revision {rev}", f"--revision {other}"]), "two checkpoints: not known")
        self.assertIsNone(m.revision_in(["--revision main", "--revision " + rev[:39], "--revision " + rev + "0"]))
        self.assertIsNone(m.revision_in([]))
        self.assertEqual(m.pair_revision({"x_alzeo_revision": rev}), rev)
        for bad in (rev.upper(), rev[:39], rev + "0", "main", 7, None, [rev]):
            self.assertIsNone(m.pair_revision({"x_alzeo_revision": bad}), bad)
        self.assertIsNone(m.pair_revision([rev]))

    def test_this_box_reads_its_checkpoint_from_its_engines_command_and_forgets_it_with_its_engine(self):
        m = self.mod
        rev = "ef" * 20
        engines = []
        for r in (rev, "01" * 20):                                              # this engine, then another one
            engines.append(sleeper(self, "docker", "run", "x", "--served-model-name", "qwen3.8-27b", "--revision", r))
        shown = f"ActiveState=active\nMainPID={engines[0].pid}\n"
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, shown, "")
        saved = (m.HOLD_UNITS, m.subprocess)
        m.HOLD_UNITS, m.subprocess = ("qwen38-a.service",), types.SimpleNamespace(run=run)
        m._UNIT_FACTS.update(ts=-1e9, texts=None)

        def restore():
            m.HOLD_UNITS, m.subprocess = saved
            m._UNIT_FACTS.update(ts=-1e9, texts=None)
        self.addCleanup(restore)
        self.assertEqual(m.unit_revision(), rev)
        self.assertEqual(m.unit_slots(), None, "no slots in that command")
        self.assertEqual(len(calls), 1, "one read for the revision and the slots")
        shown = f"ActiveState=active\nMainPID={engines[1].pid}\n"
        self.assertEqual(m.unit_revision(), rev, "kept 10 s")
        m.forget_engine_rules()                             # another engine serves: its unit is read again
        self.assertEqual(m.unit_revision(), "01" * 20)
        m.HOLD_UNITS = ()
        self.assertIsNone(m.unit_revision(), "no unit and no PAIR_CHECKPOINT: nothing said, nothing shared")

    def test_the_load_a_box_says_counts_system_one_and_what_it_holds(self):
        # a box busy with System One's branches, or with requests held while its engine comes back, said it ran none,
        # and the other box sent it more (review, 2026-11-03)
        m = self.mod
        conf = {"name": "me", "peers": []}
        data = json.dumps({"object": "list", "data": [{"id": "qwen3.8-27b"}]}).encode()
        seen = []
        saved = (m.systemone_call, m.systemone_read, m.SYSTEMONE_THINK_TOKENS)

        def call(*a, **k):
            seen.append((m.busy_here(), m.pair_load("me", conf), json.loads(m.running_said(data, "b"))["x_alzeo_running"]))
            return {}

        def restore():
            m.systemone_call, m.systemone_read, m.SYSTEMONE_THINK_TOKENS = saved
        self.addCleanup(restore)
        m.systemone_call, m.systemone_read, m.SYSTEMONE_THINK_TOKENS = call, lambda r, labels: ([1.0], 1.0, 1, 1, None), 0
        plan = {"branches": [(None, "q", ["a", "b"], 5, None)], "prefix": "p", "warm_first": False}
        m.systemone_run("Bearer x", "qwen3.8-27b", plan)
        self.assertEqual(seen, [(1, 1, 1)], "a branch at the engine is load, said and weighed")
        self.assertEqual(m.busy_here(), 0, "and no more once it ended")
        held = object()
        with m._GEN_LOCK:
            m._HELD.add(held)
        self.addCleanup(m._HELD.discard, held)
        self.assertEqual((m.busy_here(), m.pair_load("me", conf)), (1, 1), "a request held is load too")
        self.assertEqual(json.loads(m.running_said(data, "b"))["x_alzeo_running"], 1)

    def test_a_request_an_engine_closed_on_is_kept_with_its_box_for_a_while(self):
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        big = b'{"model": "qwen3.8-27b", "stream": true, "messages": [{"role": "user", "content": "' + b"k" * 300000 + b'"}]}'
        self.assertEqual(m.prompt_fingerprint(big, "/v1/chat/completions"), m.body_fingerprint(big, "/v1/chat/completions"),
                         "role 9's fingerprint changed")
        self.assertIsNone(m.prompt_fingerprint(b'{"model": "x"}', "/v1/chat/completions"), "role 9's size floor")
        r = b'{"model": "qwen3.8-27b", "stream": true, "max_tokens": 9, "messages": [{"role": "user", "content": "k"}]}'
        again = r.replace(b'"stream": true', b'"stream": false').replace(b'"max_tokens": 9', b'"max_tokens": 50')
        fp = lambda body, route="/v1/chat/completions": m.body_fingerprint(body, route)
        self.assertIsNone(m.killer_box(fp(r)))
        m.killer_note(fp(r), "spark2")
        self.assertEqual(m.killer_box(fp(again)), "spark2", "a retry changes stream and max_tokens")
        self.assertIsNone(m.killer_box(fp(r, "/v1/messages")), "another route")
        self.assertIsNone(m.killer_box(None), "a request routing never saw")
        m._KILLERS[fp(r)] = ("spark2", time.monotonic() - 1)
        self.assertIsNone(m.killer_box(fp(r)), "kept no longer")
        for i in range(m.PAIR_KILLER_MAX + 5):
            m.killer_note(fp(b'{"n": %d}' % i), "b")
        self.assertEqual(len(m._KILLERS), m.PAIR_KILLER_MAX, "bounded, the oldest dropped first")

    def test_the_blocks_kept_for_a_box_are_bounded_and_age_out(self):
        m = self.mod
        m._AFFINITY.clear()
        self.addCleanup(m._AFFINITY.clear)
        blocks = [(4096 << i, f"fp{i:02d}") for i in range(4)]
        m.affinity_note("b", blocks, 100.0)
        self.assertEqual(m.affinity_of("b", blocks, 100.0), 4096 << 3, "the longest block known wins")
        self.assertEqual(m.affinity_of("b", blocks[:2], 100.0), 8192)
        self.assertEqual(m.affinity_of("c", blocks, 100.0), 0, "another box knows none")
        self.assertEqual(m.affinity_of("b", blocks, 100.0 + m.PAIR_AFFINITY_KEEP_S), 0, "kept no longer")
        m.affinity_forget("b", blocks[2:])
        self.assertEqual(m.affinity_of("b", blocks, 100.0), 8192, "the blocks forgotten are gone")
        many = [(4096, f"x{i}") for i in range(m.PAIR_AFFINITY_MAX + 10)]
        m.affinity_note("b", many, 200.0)
        self.assertEqual(len(m._AFFINITY["b"]), m.PAIR_AFFINITY_MAX)
        self.assertNotIn("x0", m._AFFINITY["b"], "the oldest go first")
        self.assertIn(f"x{m.PAIR_AFFINITY_MAX + 9}", m._AFFINITY["b"])

    def test_a_box_counts_what_it_sent_the_other_even_past_a_read(self):
        m, conf = self.mod, {"name": "me", "peers": []}
        m._PAIR_OUT.pop("p", None)
        m._PAIR_MODELS["p"] = {"ts": time.monotonic(), "models": (), "running": 5}
        self.addCleanup(m._PAIR_MODELS.pop, "p", None)
        self.assertEqual(m.pair_load("p", conf), 5, "what it runs for its own clients")
        m.pair_sent("p", 1); m.pair_sent("p", 1)
        self.assertEqual(m.pair_load("p", conf), 7, "and this box's requests there")
        # a read whatever its moment: its own clients' count, ours apart, so ours in transit are not lost
        m._PAIR_MODELS["p"]["running"] = 3
        self.assertEqual(m.pair_load("p", conf), 5)
        m.pair_sent("p", -1); m.pair_sent("p", -1)
        self.assertEqual(m.pair_load("p", conf), 3, "the ones that ended, counted once")
        # this box: its generations and what the sharing kept here, each counted once
        a, b = object(), object()
        with m._GEN_LOCK:
            m._GENERATIONS.add(a); m._PAIR_HERE.update((a, b))
        self.addCleanup(lambda: (m._GENERATIONS.discard(a), m._PAIR_HERE.difference_update((a, b))))
        self.assertEqual(m.pair_load("me", conf), 2)

    def test_a_request_whose_answer_began_is_never_taken_over(self):
        # a hold that holds it, or opened its stream with keepalives: bytes may have gone out, and the other box would
        # start an answer over them (decision 998: never a request whose answer began; review, 2026-11-03)
        m = self.mod
        h = object.__new__(m.H)
        h._peer, tried = "test", []
        h._forward_peer = lambda *a, **k: tried.append(a)
        for hold_until, held_sse in ((time.monotonic() + 30, False), (None, True)):
            h._hold_until, h._held_sse = hold_until, held_sse
            h._pair_alt = ([{"name": "p"}], "qwen3.8-27b", {"name": "me", "peers": []}, None, [], TEST_REV)
            self.assertFalse(h._pair_take_over(b"{}"))
        self.assertEqual(tried, [], "a request whose answer began was sent to the other box")

    def test_a_request_no_box_took_over_stays_this_boxs(self):
        # the other box in its pause after a refusal: the request stays here, reached by a stop of this box and in its
        # count, where the take-over had dropped it from both for good (review, 2026-11-03)
        m = self.mod
        h = object.__new__(m.H)
        h._peer, h._hold_until, h._held_sse = "test", None, False
        alt = ([{"name": "p"}], "qwen3.8-27b", {"name": "me", "peers": []}, None, [], TEST_REV)
        m._PAIR_COOL["p"] = time.monotonic() + 60
        self.addCleanup(m._PAIR_COOL.pop, "p", None)
        with m._GEN_LOCK:
            m._GENERATIONS.add(h); m._PAIR_HERE.add(h)
        self.addCleanup(lambda: (m._GENERATIONS.discard(h), m._PAIR_HERE.discard(h)))
        h._pair_alt = alt
        self.assertFalse(h._pair_take_over(b"{}", "this box is short of memory"))
        self.assertIn(h, m._GENERATIONS, "a stop of this box no longer reached it")
        self.assertIn(h, m._PAIR_HERE, "this box's load no longer counted it")
        # held, it was out of the generations: it stays out
        with m._GEN_LOCK:
            m._GENERATIONS.discard(h)
        h._pair_alt = alt
        self.assertFalse(h._pair_take_over(b"{}"))
        self.assertNotIn(h, m._GENERATIONS, "a held request is in flight nowhere")


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
