#!/usr/bin/env python3
"""Two boxes that serve one model (keepalive-proxy role 10, decisions 996 to 998): where a conversation goes and
stays, the two boxes as processes.

The pair's tests are four files since 2026-11-10, so that CI runs them side by side: this one,
tests/test_proxy_pair.py, tests/test_proxy_pair_inprocess.py and
tests/test_proxy_pair_one_model_inprocess.py. What they share (the two
boxes, their identities, the fake engines, PairFixture) is tests/pairbox.py.
"""
import http.client
import http.server
import json
import pathlib
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import localports  # noqa: E402
from pairbox import (client_ctx, Engine, fake_engine, fake_peer, HOP_ENDED_NDJSON, HOP_ENDED_OPENAI, KEY_1,
    KEY_2, LateBox, PAIR_GRACE_READ_S, PairFixture, PEER_ANSWER, server_ctx, silent_port, TEST_REV, tls_peer,
    _until, VLLM_DEAD_EVENT)  # noqa: E402


class TwoBoxesServeOneModel(PairFixture, unittest.TestCase):
    """Role 10, a model both boxes serve (decisions 996 to 998 of 2026-11-03): a conversation stays on the box that
    holds its prefix, a new one goes to the box with the fewest requests in flight, boxes far apart share even a
    conversation, and a box that refuses hands its request back to the entry before any byte of an answer. Both
    serve the 27B from their start, as two boxes that loaded it do (a proxy learns what its engine serves at a
    lane's start); spark2 lists a marker beside it, which shows in the entry's list once it read spark2's."""
    MODELS = (("qwen3.8-27b",), ("qwen3.8-27b", "marker-27b"))
    ENTRY_ENV = {"PAIR_SPREAD_COOL_S": "2"}     # a refusal's pause, short: the tests after it share again

    def setUp(self):
        super().setUp()
        if self.INPROC:
            # no pause an earlier test left: once busy() ended a test's generations with it, a pause that test caused
            # still ran as the next one began (1.07 s of it after
            # test_a_box_gone_without_a_reset_holds_a_shared_request_seconds_not_ten, review of v1.44.3)
            self.mod._PAIR_COOL.clear()
        self.until_listed("marker-27b")


    def until_listed(self, model, within=15.0, port=None, gone=False):
        """Until the entry's own list shows model (or no longer does): the union of its list and what it kept of the
        other box's."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            st, out, _ = self.call("/v1/models", method="GET", port=port)
            if st == 200 and (model in [m.get("id") for m in json.loads(out).get("data", [])]) != gone:
                return
            time.sleep(0.2)
        self.fail(f"the entry's list never {'lost' if gone else 'showed'} {model}")

    def turns(self, tag, n, size=9000, turn=1500):
        """n turns of one conversation, as an agent sends them: each body is the one before plus a turn."""
        msgs = [{"role": "system", "content": (tag + " ") * (size // (len(tag) + 1))}]
        out = []
        for i in range(n):
            msgs = msgs + [{"role": "user", "content": f"turn {i} " + "x" * turn}]
            out.append({"model": "qwen3.8-27b", "max_tokens": 8, "messages": list(msgs), "user": f"{tag}-{i}"})
            msgs = msgs + [{"role": "assistant", "content": "ok " * 100}]
        return out

    def sent(self, port=None):
        """What the entry's tile says it sent spark2, and why."""
        st, out, _ = self.call("/proxy/pair/spark2/state", method="GET", port=port)
        self.assertIn(st, (200, 503), out[:300])
        return json.loads(out).get("sent")

    @staticmethod
    def moved(before, after):
        """The counts that changed, by how much."""
        return {k: after[k] - before.get(k, 0) for k in after if after[k] != before.get(k, 0)}

    def busy(self, engine, port, key, model, n, secs):
        """n requests of secs each, sent to the box behind port by its own client: its generations in flight, until
        its engine has them all."""
        threads = [threading.Thread(target=self.call, args=("/v1/chat/completions", {
            "model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "busy"}], "x_delay": secs,
            "user": f"busy-{i}"}), kwargs={"port": port, "key": key}, daemon=True) for i in range(n)]
        for t in threads:
            t.start()

        def ended():
            # the test is over: its generations answer now, and the next test starts once they are in, as
            # it did when they ran their whole secs (no assertion reads them past the test)
            engine.release.set()
            try:
                for t in threads:
                    t.join(30)
            finally:
                engine.release.clear()
        self.addCleanup(ended)
        deadline = time.monotonic() + 10
        while sum(1 for u in engine.users if u and u.startswith("busy-")) < n:
            if time.monotonic() > deadline:
                self.fail(f"the engine has {engine.users} of {n} busy requests")
            time.sleep(0.05)
        return threads

    def test_a_conversation_stays_on_the_box_that_served_it(self):
        a, b = self.turns("conv-a", 2), self.turns("conv-b", 1)
        # spark1 runs 2, spark2 none: a new conversation goes to spark2
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 3.0)
        st, out, _ = self.call("/v1/chat/completions", a[0])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out)
        self.assert_said("spark1", "-> pair spark2 for qwen3.8-27b")
        # spark2 runs 2 and says so, spark1 none: the conversation's next turn still goes to spark2, whose cache
        # holds all of it but the new turn, and a new conversation goes to spark1
        for t in held:
            t.join(30)
        self.busy(self.E2, self.port2, KEY_2, "marker-27b", 2, 6.0)
        self.E2.served[:] = ["qwen3.8-27b", "marker-busy"]
        self.until_listed("marker-busy")        # read after its 2 went in: the entry knows it runs 2
        st, out, _ = self.call("/v1/chat/completions", a[1])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "the turn left the box that holds its conversation")
        st, out, _ = self.call("/v1/chat/completions", b[0])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a new conversation went to the busier box")

    def test_a_long_prompt_a_warming_engine_cannot_take_goes_to_the_other_box(self):
        # this box's engine just started, its pool not measured yet: a long prompt waited for it (503), where the other
        # box's is measured (review, 2026-11-03)
        if not self.INPROC:
            self.skipTest("this box's pool is dropped in this test's process")
        self.E1.mode = "nopool"
        self.mod.invalidate_pool()
        body = dict(self.chat("qwen3.8-27b"), messages=[{"role": "user", "content": "w" * 300000}])
        st, out, _ = self.call("/v1/chat/completions", body)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "a long prompt waited for this box's warming engine")
        self.assert_said("spark1", "this box's engine is warming up for qwen3.8-27b: trying spark2")

    def test_a_prompt_too_long_for_this_boxs_pool_goes_to_the_other_box(self):
        # a 64 GB box paired with a 128 GB one: what the small pool refuses, the big one serves (review, 2026-11-03)
        if not self.INPROC:
            self.skipTest("this box's pool is set in this test's process")
        self.mod._POOL.update(tokens=10000, window=9990, ts=time.time())
        self.addCleanup(self.mod._POOL.update, tokens=None, window=None, ts=0.0)
        body = dict(self.chat("qwen3.8-27b"), messages=[{"role": "user", "content": "w " * 30000}])
        st, out, _ = self.call("/v1/chat/completions", body)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "this box refused as too long what the other box's pool holds")
        self.assert_said("spark1", "the prompt is too long for this box's pool for qwen3.8-27b: trying spark2")

    def test_a_box_gone_without_a_reset_holds_a_shared_request_seconds_not_ten(self):
        # its power or its cable gone: its port answers no TLS. A request this box could serve itself waited the whole
        # connection timeout (10 s) for it (review, 2026-11-03)
        if not self.INPROC:
            self.skipTest("the kept list is set in this test's process")
        self.repoint(silent_port(self))
        self.mod._PAIR_MODELS["spark2"] = {"ts": time.monotonic(), "models": ("qwen3.8-27b",), "rows": [],
                                           "error": None, "answered": True, "known": ("qwen3.8-27b",),
                                           "known_ts": time.monotonic(), "read_s": 0.01, "running": 0,
                                           "revision": TEST_REV}         # the same checkpoint: it is shared (1018)
        self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 9.0)         # one here, none there: it goes there
        t0 = time.monotonic()
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        took = time.monotonic() - t0
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertLess(took, 6.0, f"a shared request waited {took:.1f} s for a box gone without a reset")

    def test_a_client_that_leaves_while_the_other_box_does_not_answer_still_pauses_it(self):
        # its client gave up during the connection wait to a box gone without a reset: that box was neither paused nor
        # forgotten, and each retry waited the 3 s again (review, 2026-11-03)
        if not self.INPROC:
            self.skipTest("the kept list is set in this test's process")
        self.repoint(silent_port(self))
        self.mod._PAIR_MODELS["spark2"] = {"ts": time.monotonic(), "models": ("qwen3.8-27b",), "rows": [],
                                           "error": None, "answered": True, "known": ("qwen3.8-27b",),
                                           "known_ts": time.monotonic(), "read_s": 0.01, "running": 0,
                                           "revision": TEST_REV}
        self.addCleanup(self.mod._PAIR_COOL.pop, "spark2", None)
        self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 9.0)         # one here, none there: it goes there
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=1.0)    # a client that gives up after 1 s
        try:
            c.request("POST", "/v1/chat/completions", json.dumps(self.chat("qwen3.8-27b")).encode(),
                      {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
            with self.assertRaises(OSError):
                c.getresponse()
        finally:
            c.close()
        self.assertTrue(self.until_in_journal("spark1", "its client left, and spark2 gets nothing of a model both "
                                                        "serve", within=6.0), "the box that did not answer was not paused")
        t0 = time.monotonic()
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        took = time.monotonic() - t0
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertLess(took, 2.0, f"the next request waited {took:.1f} s again for the box that does not answer")

    def test_a_refusal_served_here_is_counted_once_this_box_served_it(self):
        # the tile counted "served here" before this box tried: a request this box then refused too was said served
        # here (review, 2026-11-03)
        Peer = fake_peer(["qwen3.8-27b", "marker-count-here"], (503, {"error": {"type": "engine_unavailable",
                                                                                 "message": "away"}}))
        entry = self.spare_proxy("spark1-count", KEY_1, peers=(("spark2", tls_peer(self, Peer)),),
                                 extra={"PAIR_SPREAD_COOL_S": "0"})
        self.until_listed("marker-count-here", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)       # one here, none there: it goes there
        before = self.sent(entry)
        self.E1.mode = "busy"                                               # and this box's engine says 503 too
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertNotEqual(st, 200, out[:300])
        self.assertEqual(self.moved(before, self.sent(entry)), {}, "a request no box served was counted served here")
        self.E1.mode = "ok"
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertEqual(self.moved(before, self.sent(entry)), {"failover": 1})
        for th in held:
            th.join(30)

    def test_a_box_held_by_its_memory_floor_sends_on_what_the_other_box_serves(self):
        # the cockpit's floor stopped a generation here: new ones wait here (v6.47), but the other box serves the model:
        # it answers, where this box said 503 or 529 for the whole hold (review, 2026-11-03)
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 6.0)        # a generation the stop cuts
        st, out, _ = self.call("/proxy/stop", {"why": "test floor", "hold_s": 3})
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(json.loads(out).get("stopped"), 1, out[:300])
        before = self.sent()
        for path in ("/v1/chat/completions", "/v1/messages"):
            with self.subTest(path=path):
                st, out, _ = self.call(path, self.chat("qwen3.8-27b"))
                self.assertEqual(st, 200, out[:300])
                self.assertIn(b"from spark2", out, "this box refused for memory what the other box serves")
        # sent there at the decision, this box held by its floor being no box to keep them: the take-over passed the
        # test alone when the decision kept them here (review, 2026-11-03). Counted as what this box could not serve,
        # never "to even the load"
        self.assertEqual(self.moved(before, self.sent()), {"rescued": 2})
        self.assertFalse(self.until_in_journal("spark1", "this box is short of memory for qwen3.8-27b: trying spark2",
                                               within=0.5), "kept here, then taken over")
        for th in held:
            th.join(30)
        time.sleep(3)                                                       # the hold is over

    def test_a_claude_code_conversation_stays_on_its_box(self):
        # Claude Code writes its system prompt and its tools after the conversation: the turns share their messages
        # alone, 32 KiB here, under 30 % of a body that carries 80 KB of system and tools (review, 2026-11-03)
        system, tools = "s" * 40000, [{"name": f"t{i}", "description": "d" * 4000} for i in range(10)]
        first = [{"role": "user", "content": "claude-conv " + "c" * 36000}]
        turns = []
        for msgs in (first, first + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "more " * 1000}]):
            turns.append({"model": "qwen3.8-27b", "max_tokens": 8, "messages": msgs, "system": system, "tools": tools})
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 3.0)      # one here, none there: it goes there
        st, out, _ = self.call("/v1/messages", turns[0])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out)
        for th in held:
            th.join(30)
        self.E2.users.clear()
        self.busy(self.E2, self.port2, KEY_2, "marker-27b", 2, 6.0)         # spark2 runs 2 of its own, spark1 none
        self.E2.served[:] = ["qwen3.8-27b", "marker-cc"]
        self.until_listed("marker-cc")
        st, out, _ = self.call("/v1/messages", turns[1])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "the turn left the box that holds its conversation")

    def test_boxes_far_apart_share_even_a_conversation(self):
        a = self.turns("conv-c", 3)
        st, out, _ = self.call("/v1/chat/completions", a[0])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "two idle boxes: the entry serves it, no hop")
        # the edge, pinned (review, 2026-11-03): spark1 runs 4, spark2 none: 4 apart is not more than 4, the turn stays
        # on the box that holds its prefix
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 4, 4.0)
        st, out, _ = self.call("/v1/chat/completions", a[1])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "4 apart moved a conversation off the box that holds its prefix")
        for th in held:
            th.join(30)
        self.E1.users.clear()
        # spark1 runs 5, spark2 none: more than 4 apart and more than 1.5 times as many, the turn goes to spark2
        self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 3.0)
        st, out, _ = self.call("/v1/chat/completions", a[2])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "5 apart kept a conversation on the busier box")

    def test_a_lane_of_16_keeps_a_conversation_5_ahead(self):
        # decision 1014: a box may run half its lane's slots ahead, 8 of 16. 5 ahead moved a conversation off a box
        # with 11 places free, its prefix computed again on the other one
        if not self.INPROC:
            self.skipTest("this box's slots are set in this test's process")
        saved = self.mod.unit_slots
        self.mod.unit_slots = lambda: 16
        self.addCleanup(setattr, self.mod, "unit_slots", saved)
        a = self.turns("conv-s16", 2)
        st, out, _ = self.call("/v1/chat/completions", a[0])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "two idle boxes: the entry serves it, no hop")
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)
        st, out, _ = self.call("/v1/chat/completions", a[1])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "5 ahead on a lane of 16 moved a conversation off a box with room to spare")
        for th in held:
            th.join(30)
        self.E1.users.clear()

    def test_the_gap_is_half_the_slots_the_other_box_says(self):
        # decision 1014, the other box's side: a conversation it holds stays there 5 ahead when its lane runs 16 at
        # once, and comes here when it says nothing of its slots (the 27B's 8, a gap of 4)
        for slots in (16, None):
            with self.subTest(slots=slots):
                Peer = fake_peer(["qwen3.8-27b", f"marker-s{slots}-a"], PEER_ANSWER, slots=slots)
                entry = self.spare_proxy(f"spark1-s{slots}", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
                self.until_listed(f"marker-s{slots}-a", port=entry)
                a = self.turns(f"conv-gap-{slots}", 2)
                held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 3.0)     # one here, none there
                st, out, _ = self.call("/v1/chat/completions", a[0], port=entry)
                self.assertIn(b"from the peer", out, out[:300])
                for th in held:
                    th.join(30)
                self.E1.users.clear()
                # now it runs 5 of its own clients' generations, and this box none
                Peer.state.update(running=5, listed=["qwen3.8-27b", f"marker-s{slots}-b"])
                self.until_listed(f"marker-s{slots}-b", port=entry)
                st, out, _ = self.call("/v1/chat/completions", a[1], port=entry)
                self.assertEqual(st, 200, out[:300])
                if slots == 16:
                    self.assertIn(b"from the peer", out, "5 ahead on a lane of 16 moved the conversation off its box")
                    self.assertEqual(len(Peer.seen), 2)
                else:
                    self.assertIn(b"from spark1", out, "5 ahead on a box of unknown slots kept the conversation there")
                    self.assertEqual(len(Peer.seen), 1)

    def test_a_lane_whose_port_refuses_hands_its_request_to_the_other_box(self):
        # decision 998's first case: this box's engine refuses the connection (its lane stops or starts), and the
        # other box that serves the model answers at once. Every test passed with a refusal never handed over (review,
        # 2026-11-03)
        h = fake_engine("spark1-refuses", "qwen3.8-27b")
        port = localports.held(self)        # kept to the end: once the engine stops, no other test's server takes it
        eng = localports.serve(port, Engine, h)
        threading.Thread(target=eng.serve_forever, daemon=True).start()
        stopped = []

        def stop():
            if not stopped:
                stopped.append(1)
                eng.shutdown()
                eng.server_close()
        self.addCleanup(stop)
        Peer = fake_peer(["qwen3.8-27b", "marker-refuses"], PEER_ANSWER)
        entry = self.spare_proxy("spark1-refuses", KEY_1, peers=(("spark2", tls_peer(self, Peer)),),
                                 upstream=f"http://127.0.0.1:{port}")
        self.until_listed("marker-refuses", port=entry)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(Peer.seen, [], "two idle boxes: the entry serves it")
        stop()                                              # its lane stops: the port refuses connections
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from the peer", out, "this box's engine refused the connection, the other box was not tried")
        self.assert_said("spark1-refuses", "this box's engine does not answer for qwen3.8-27b: trying spark2")

    def test_a_request_kept_here_goes_over_when_the_floor_holds_this_box(self):
        # the floor may start after the sharing kept a request here: it goes to the other box that serves the model,
        # where it waited the whole hold here (v6.52). The floor test passed without this hand-over, its requests sent
        # there at the decision (review, 2026-11-03)
        if not self.INPROC:
            self.skipTest("this box's floor is set in this test's process")
        m = self.mod
        decided = []
        real_floor, real_spread = m.floor_refusal, m.H._pair_spread

        def spread(h, *a, **k):
            kept = real_spread(h, *a, **k)
            decided.append(1)                               # the floor starts once the sharing decided
            return kept
        m.floor_refusal = lambda *a, **k: ("the test's floor, after the decision", 3) if decided else None
        m.H._pair_spread = spread
        self.addCleanup(setattr, m, "floor_refusal", real_floor)
        self.addCleanup(setattr, m.H, "_pair_spread", real_spread)
        before = self.sent()
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "a request kept here was held by the floor while the other box serves it")
        self.assertEqual(self.moved(before, self.sent()), {"rescued": 1})

    def test_a_count_this_box_failed_is_no_engine_that_closed_on_a_request(self):
        # the size guard's own count failed (a 5xx of a stopping engine), the generation never went: the other box gets
        # a 503 it serves the request on, not the 502 of an engine that closed the connection while it had it, and
        # this box's own client is served by the other box (review, 2026-11-03)
        if not self.INPROC:
            self.skipTest("this box's pool is set in this test's process")
        self.mod._POOL.update(tokens=10000, window=9990, ts=time.time())
        self.addCleanup(self.mod._POOL.update, tokens=None, window=None, ts=0.0)
        self.E1.mode = "count_fails"
        body = dict(self.chat("qwen3.8-27b"), messages=[{"role": "user", "content": "count " * 12000}])
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", body)[:2]
        self.assertEqual(st, 503, out[:300])
        self.assertNotIn(b"upstream_closed", out, "a failed count was said to be an engine that died on the request")
        self.mod._POOL.update(tokens=10000, window=9990, ts=time.time())    # the failed count dropped the pool read
        st, out, _ = self.call("/v1/chat/completions", body)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "a request whose count failed here was not handed to the other box")

    def test_a_hop_whose_engine_dies_in_an_open_stream_gets_an_error_event(self):
        # the other box's hold opened the stream (200 and keepalives after KEEPALIVE_S), then its engine died on the
        # request: its 502 was a status line written inside the stream, which cut this box's client with no error
        # event (review, 2026-11-03)
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 16.0)       # one here, none there: it goes there
        self.E2.mode = "die"
        body = dict(self.turns("conv-held", 1)[0], stream=True, x_die_after=12.0)
        try:
            st, out, _ = self.call("/v1/chat/completions", body)
        except http.client.HTTPException as e:
            self.fail(f"the stream was cut with no error event: {e!r}")
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"closed the connection while it had the request", out, out[-400:])
        self.assertNotIn(b"HTTP/1.1 502", out)
        self.assertFalse(self.until_in_journal("spark1", "UPSTREAM CUT pair", within=1.0),
                         "the relay of the other box's stream broke")
        self.assert_said("spark1", "502 pair upstream")                       # not "ok pair" (review, 2026-11-04)
        # sent again: its engine closed on it, so that box or nowhere (decision 1019), its engine away now (503)
        self.E2.mode = "busy"
        st, out, _ = self.call("/v1/chat/completions", body)       # the same body: its engine says 503 first
        self.assertNotIn("conv-held-0", self.E1.users, "sent again, the request reached this box's engine")
        for th in held:
            th.join(30)

    def test_a_request_asked_of_this_box_goes_nowhere_else_while_its_engine_is_away(self):
        # the cockpit's smoke test asks whether THIS box's engine answers: with its engine away (a crash, a restart: the
        # names it serves unknown), the request went to the other box, whose answer said the engine was fine (review,
        # 2026-11-03)
        if not self.INPROC:
            self.skipTest("this box's engine is said away in this test's process")
        m = self.mod
        real = m.served_models

        def away(*a, **k):
            raise m.EngineUnreachable("connection refused (the test's engine away)")
        m.served_models = away
        self.addCleanup(setattr, m, "served_models", real)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), headers={"X-Alzeo-Here": "1"})
        self.assertNotIn(b"from spark2", out, "the smoke test of this box got the other box's answer")
        self.assertEqual(self.E2.users, [])

    def test_a_request_this_boxs_engine_died_on_stays_here_when_sent_again(self):
        # decision 1019: a client sends a 5xx's request again within seconds. This box's engine died on it: sent again,
        # it may take that engine down again, never the other box's (review, 2026-11-03)
        r = self.turns("conv-killer-here", 1)[0]
        self.E1.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.E1.mode = "ok"
        # 5 here, none there: far apart, its next turn would go there whatever its prefix (decision 1014: 4 of 8)
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "sent again, a request this box's engine died on went to the other box")
        self.assertNotIn("conv-killer-here-0", self.E2.users)
        for th in held:
            th.join(30)

    def test_a_request_the_other_boxs_engine_died_on_goes_there_or_nowhere_when_sent_again(self):
        # decision 1019: the other box's engine died on it (its 502); sent again while that engine is away (503), the
        # entry served it on its own engine, which it could take down too (review, 2026-11-03)
        r = self.turns("conv-killer-there", 1)[0]
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 6.0)     # one here, none there: it goes there
        self.E2.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 502, out[:300])
        self.E2.mode = "busy"                                               # its engine away: it says 503
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertNotIn("conv-killer-there-0", self.E1.users, "sent again, it went to this box's engine")
        for th in held:
            th.join(30)

    def test_a_box_serving_another_checkpoint_gets_nothing_shared(self):
        # decision 1018: switch-model.sh serves the 27B's stock, uncensored and fp8 checkpoints under one name. Shared
        # as one model, a client of the stock box got the uncensored one (review, 2026-11-03)
        for said in ("9f8e7d6c5b" * 4, None):
            with self.subTest(revision=said):
                Peer = fake_peer(["qwen3.8-27b", f"marker-rev-{said is None}"], PEER_ANSWER, revision=said)
                entry = self.spare_proxy(f"spark1-rev-{said is None}", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
                self.until_listed(f"marker-rev-{said is None}", port=entry)
                held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 3.0)     # one here, none there
                st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
                self.assertEqual(st, 200, out[:300])
                self.assertIn(b"from spark1", out, "a box serving another checkpoint, or saying none, was sent it")
                self.assertEqual(Peer.seen, [])
                for th in held:
                    th.join(30)
                self.E1.users.clear()

    def test_a_stream_the_other_box_ends_by_order_binds_nothing(self):
        # decision 1031: the other box's Load, Abort all or floor cut a stream it served: the entry bound the request to
        # it for 10 minutes, and its retries got 503s while that box was away (review, 2026-11-04)
        Base = fake_peer(["qwen3.8-27b", "marker-ended"], PEER_ANSWER)

        class EndsByOrder(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                if Base.state.get("away"):
                    return self.reply(503, {"error": {"type": "engine_unavailable", "message": "its engine is away"}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                first = (b'data: {"id":"c","object":"chat.completion.chunk","model":"qwen3.8-27b","choices":[{"index":0,'
                         b'"delta":{"content":"from the peer "}}]}\n\n')
                for part in (first, HOP_ENDED_OPENAI):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)                      # cut, not ended
        entry = self.spare_proxy("spark1-ended", KEY_1, peers=(("spark2", tls_peer(self, EndsByOrder)),))
        self.until_listed("marker-ended", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)          # one here, none there: it goes there
        r = dict(self.turns("conv-ended", 1)[0], stream=True)
        try:
            self.call("/v1/chat/completions", r, port=entry)
        except http.client.HTTPException:
            pass                                                                # a cut stream, as the client reads it
        self.assertEqual(len(Base.seen), 1)
        Base.state["away"] = True                                               # that box's engine away now
        st, out, _ = self.call("/v1/chat/completions", dict(r, stream=False), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a request the other box ended by order was bound to it")
        self.assert_said("spark1-ended", "ended it by order, nothing is bound")
        for th in held:
            th.join(30)

    def test_a_503_the_other_box_says_is_an_order_is_not_run_here_nor_bound(self):
        # decision 1031 with 1017: the other box's Abort all ended it before any answer; not run here too, nor bound
        Base = fake_peer(["qwen3.8-27b", "marker-ended-503"], PEER_ANSWER)

        class Ends503(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                out = json.dumps({"error": {"type": "engine_unavailable", "message": "keepalive-proxy: this box ended "
                                            "the request by order"}}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                if not Base.state.get("away"):
                    self.send_header("X-Alzeo-Pair-Ended", "order")
                self.end_headers()
                self.wfile.write(out)
        entry = self.spare_proxy("spark1-ended-503", KEY_1, peers=(("spark2", tls_peer(self, Ends503)),))
        self.until_listed("marker-ended-503", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)          # one here, none there: it goes there
        r = self.turns("conv-ended-503", 1)[0]
        st, out, headers = self.call("/v1/chat/completions", r, port=entry)
        self.assertEqual(st, 503, out[:300])
        self.assertNotIn("conv-ended-503-0", self.E1.users, "an Abort all of the other box was undone here")
        self.assertNotIn("X-Alzeo-Pair-Ended", headers)
        self.assertEqual(headers.get("Retry-After"), "30")                     # as every 503 of the pair
        self.assertIn(b"box spark2 ended this request by order", out, "the other box's own words, 'this box', relayed")
        Base.state["away"] = True                                               # a plain 503 now: its engine away
        st, out, _ = self.call("/v1/chat/completions", r, port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a request the other box ended by order was bound to it")
        for th in held:
            th.join(30)

    def test_an_ordered_503_whose_body_was_cut_is_still_an_order(self):
        # the body-dependent fallback came first: an ordered 503 whose body was cut ran here, undoing an Abort all there
        # (the delta's 3rd review, 2026-11-04)
        Base = fake_peer(["qwen3.8-27b", "marker-ended-cut"], PEER_ANSWER)

        class EndsCut(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                out = json.dumps({"error": {"type": "engine_unavailable", "message": "ended by order"}}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out) + 50))         # its body cut on the way
                self.send_header("X-Alzeo-Pair-Ended", "order")
                self.end_headers()
                self.wfile.write(out[:10])
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
        entry = self.spare_proxy("spark1-ended-cut", KEY_1, peers=(("spark2", tls_peer(self, EndsCut)),))
        self.until_listed("marker-ended-cut", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)          # one here, none there: it goes there
        r = self.turns("conv-ended-cut", 1)[0]
        st, out, _ = self.call("/v1/chat/completions", r, port=entry)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"ended this request by order", out)
        self.assertNotIn("conv-ended-cut-0", self.E1.users, "an Abort all of the other box was undone here")
        for th in held:
            th.join(30)

    def test_a_death_the_other_box_said_was_ordered_after_it_binds_nothing(self):
        # vLLM's death relayed first, the end by order said after: the death bound it at once (the delta's 3rd review)
        Base = fake_peer(["qwen3.8-27b", "marker-dead-then-order"], PEER_ANSWER)

        class DeadThenOrder(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                if Base.state.get("away"):
                    return self.reply(503, {"error": {"type": "engine_unavailable", "message": "its engine is away"}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                first = (b'data: {"id":"c","object":"chat.completion.chunk","model":"qwen3.8-27b","choices":[{"index":0,'
                         b'"delta":{"content":"from the peer "}}]}\n\n')
                for part in (first, b"data: " + VLLM_DEAD_EVENT + b"\n\n", HOP_ENDED_OPENAI):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        entry = self.spare_proxy("spark1-dead-order", KEY_1, peers=(("spark2", tls_peer(self, DeadThenOrder)),))
        self.until_listed("marker-dead-then-order", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)          # one here, none there: it goes there
        r = dict(self.turns("conv-dead-order", 1)[0], stream=True)
        st, out, _ = self.call("/v1/chat/completions", r, port=entry)
        self.assertEqual(st, 200, out[:300])
        Base.state["away"] = True
        st, out, _ = self.call("/v1/chat/completions", dict(r, stream=False), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a death under an order was bound to the other box")
        for th in held:
            th.join(30)

    def test_a_box_that_refuses_this_boxs_certificate_never_had_the_request(self):
        # decision 1017: under TLS 1.3 the other box refuses this box's certificate (re-paired, its entry of this box
        # replaced) at the first read, the request sent: it was said lost, bound there and that box paused, where its
        # proxy never read it (review, 2026-11-04). Its list was read before, so the entry still lists the model there
        Peer = fake_peer(["qwen3.8-27b", "marker-tls"], PEER_ANSWER)
        state = {"ctx": server_ctx("spark2", ("spark1",))}

        class Switching(Engine):
            def get_request(self):
                sock, addr = self.socket.accept()
                return state["ctx"].wrap_socket(sock, server_side=True), addr
        port = localports.held(self)
        srv = localports.serve(port, Switching, Peer)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        entry = self.spare_proxy("spark1-tls", KEY_1, peers=(("spark2", port),))
        self.until_listed("marker-tls", port=entry)
        state["ctx"] = server_ctx("spark2", ("spark9",))                     # this box's certificate refused from now
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 3.0)          # one here, none there: it goes there
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a request the other box's TLS refused was said lost")
        self.assertEqual(Peer.seen, [])
        self.assert_said("spark1-tls", "refused this box's certificate")
        for th in held:
            th.join(30)

    def test_a_client_that_leaves_while_a_box_that_took_its_request_reads_it_pauses_nothing(self):
        # the client left while its body was on its way to a box that took the connection: this request's own watch
        # cut it, and that healthy box was paused 30 s as one that does not answer (review, 2026-11-04)
        Base = fake_peer(["qwen3.8-27b", "marker-slow-read"], PEER_ANSWER)

        class Slow(Base):
            def do_POST(self):
                if Base.state.get("fast"):
                    return Base.do_POST(self)
                # it reads nothing of the body yet, past the seconds this box's watch keeps the connection once its
                # client left (decision 1112): ended sooner, its own close cut the upload, a box that does not answer
                time.sleep(14)
        entry = self.spare_proxy("spark1-slow-read", KEY_1, peers=(("spark2", tls_peer(self, Slow)),))
        self.until_listed("marker-slow-read", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 6.0)          # one here, none there: it goes there
        body = json.dumps(dict(self.chat("qwen3.8-27b"), messages=[{"role": "user", "content": "x" * (64 << 20)}]))
        s = socket.create_connection(("127.0.0.1", entry), timeout=60)
        try:
            s.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      f"Authorization: Bearer {KEY_1}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body.encode())
            time.sleep(2.5)                                                     # its body on its way, unread there
        finally:
            s.close()                                                           # the client leaves
        self.assert_said("spark1-slow-read", "CLIENT GONE during pair")
        self.assertFalse(self.until_in_journal("spark1-slow-read", "its client left", within=2.0),
                         "a box that took the connection was paused as one that does not answer")
        for th in held:
            th.join(30)
        # that box never read the conversation: it holds none of it, and the next turn is not pulled there by a prefix it
        # does not have (the conversation was moved there, the delta's review, 2026-11-04)
        Base.state["fast"] = True
        again = dict(self.chat("qwen3.8-27b"), messages=[{"role": "user", "content": "x" * (200 << 10)}])
        st, out, _ = self.call("/v1/chat/completions", again, port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "the conversation was kept on the box that never read it")

    def test_a_shared_request_carries_this_boxs_checkpoint_and_never_the_clients(self):
        # decision 1018: the checkpoint the other box must serve a shared request from comes with it, set by the entry
        # alone (review, 2026-11-04)
        Peer = fake_peer(["qwen3.8-27b", "marker-carries"], PEER_ANSWER)
        entry = self.spare_proxy("spark1-carries", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-carries", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 3.0)          # one here, none there: it goes there
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry,
                               headers={"X-Alzeo-Pair-Checkpoint": "ab" * 20})
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(len(Peer.seen), 1)
        self.assertEqual(Peer.heard, [TEST_REV], "the entry's checkpoint, never the one its client wrote")
        for th in held:
            th.join(30)

    def test_a_request_for_a_model_only_the_other_box_serves_carries_no_checkpoint(self):
        Peer = fake_peer(["qwen3.8-flash-next-fp4", "marker-alone"], PEER_ANSWER)
        entry = self.spare_proxy("spark1-alone", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-alone", port=entry)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-flash-next-fp4"), port=entry,
                               headers={"X-Alzeo-Pair-Checkpoint": "ab" * 20})
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(Peer.heard, [None], "a model this box does not serve: no checkpoint to keep")

    def test_a_tool_call_the_other_box_served_is_watched_here_too(self):
        # role 8: a call naming a watched host, in an answer this box relayed from the other one, was written in the
        # other box's journal alone, and this box's cockpit showed no banner for its own client (review, 2026-11-03)
        tool = (200, {"id": "c", "object": "chat.completion", "model": "qwen3.8-27b", "choices": [{
            "index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "t1", "type": "function", "function": {
                    "name": "bash", "arguments": json.dumps({"command": "curl https://b.oss-cn.aliyuncs.com/x"})}}]}}]})
        Peer = fake_peer(["qwen3.8-27b", "marker-tool"], tool)
        entry = self.spare_proxy("spark1-tool", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-tool", port=entry)
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 3.0)          # one here, none there: it goes there
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(len(Peer.seen), 1)
        self.assert_said("spark1-tool", "TOOL TARGET aliyuncs.com in a bash call on /v1/chat/completions")
        for th in held:
            th.join(30)
        self.E1.users.clear()                               # the next busy() waits for its own request
        # and in a stream relayed as it came
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 3.0)     # one here, none there: it goes there
        self.E2.mode = "tool"
        st, out, _ = self.call("/v1/chat/completions", dict(self.turns("conv-tool", 1)[0], stream=True))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"aliyuncs", out)
        self.assert_said("spark1", "TOOL TARGET aliyuncs.com in a bash call on /v1/chat/completions")
        for th in held:
            th.join(30)

    def test_the_other_boxs_list_reads_write_a_line_a_minute_while_this_engine_is_away(self):
        # each read of the other box (every PAIR_TTL_S while it has traffic) wrote "engine unreachable" and a 503 line:
        # some 24 a minute, the 800 lines this box's cockpit reads filled in about 33 minutes (review, 2026-11-03)
        self.E1.mode = "down"                               # this box's engine: its list answers 503 (a boot)
        for _ in range(6):
            st, out = self.tls_call(self.pport1, "/v1/models", method="GET")[:2]
            self.assertEqual(st, 503, out[:300])
        time.sleep(0.5)
        lines = self.journal_of("spark1")[self.journal_at.get("spark1", 0):]
        self.assertLessEqual(lines.count("engine unreachable"), 1, lines[-1500:])

    def test_a_request_bound_to_a_box_now_serving_another_checkpoint_goes_nowhere(self):
        # decisions 1018 and 1019 together: bound to the other box by its engine dying on it, a request went there
        # even once that box served another checkpoint of the model, the uncensored 27B for a client of the stock one
        if not self.INPROC:
            self.skipTest("this box's checkpoint is set in this test's process")
        m = self.mod
        r = self.turns("conv-bound-rev", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "spark2")
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_revision
        m.unit_revision = lambda: "ab" * 20             # this box serves another checkpoint than spark2 now
        self.addCleanup(setattr, m, "unit_revision", saved)
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"from another checkpoint", out)
        self.assertNotIn("conv-bound-rev-0", self.E2.users, "it went to the box serving another checkpoint")
        self.assertNotIn("conv-bound-rev-0", self.E1.users, "it was served here, where an engine never closed on it")

    def test_a_request_bound_to_a_box_that_serves_another_model_now_gets_an_answer(self):
        # decision 1019: bound to the other box, which said 409 (it serves another model now): the client got no answer
        # at all, the 409 written nowhere (review, 2026-11-04)
        class DropsThen409(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            state = {"drop": True}

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-27b"}, {"id": "marker-409"}],
                                  "x_alzeo_running": 0, "x_alzeo_running_yours": 0,
                                  "x_alzeo_revision": TEST_REV}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if DropsThen409.state["drop"]:                  # it had it, and went: the request is bound to it
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                out = json.dumps({"error": {"type": "model_not_served", "message": "another model here"}}).encode()
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        entry = self.spare_proxy("spark1-409", KEY_1, peers=(("spark2", tls_peer(self, DropsThen409)),))
        self.until_listed("marker-409", port=entry)
        r = self.turns("conv-bound-409", 1)[0]
        held = self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)          # one here, none there: it goes there
        st, out, _ = self.call("/v1/chat/completions", r, port=entry)
        self.assertEqual(st, 503, out[:300])                                    # lost (decision 1017), and bound
        DropsThen409.state["drop"] = False                                      # now it serves another model
        c = http.client.HTTPConnection("127.0.0.1", entry, timeout=10)
        try:
            c.request("POST", "/v1/chat/completions", json.dumps(r).encode(),
                      {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
            resp = c.getresponse()
            st, out = resp.status, resp.read()
        except OSError as e:
            self.fail(f"the client got no answer: {e!r}")
        finally:
            c.close()
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"serves another model or checkpoint now", out)
        self.assertNotIn("conv-bound-409-0", self.E1.users, "served here, where no engine closed on it")
        for th in held:
            th.join(30)

    def test_a_request_the_proxy_rewrites_is_bound_as_its_client_sends_it(self):
        # decision 1019: the binding was noted on the body this proxy rewrote (a reasoning effort it maps, a tool schema
        # it fixes), the retry looked up with the body as sent: they never matched (review, 2026-11-04)
        r = dict(self.turns("conv-killer-lean", 1)[0], reasoning_effort="lean")
        self.E1.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.E1.mode = "ok"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it would go there
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a rewritten request this box's engine died on went to the other box")
        for th in held:
            th.join(30)

    def test_an_engine_that_dies_during_its_answer_binds_the_request(self):
        # decision 1019: this box's engine died in the middle of the stream; sent again, the request went to the other
        # engine (review, 2026-11-04)
        r = dict(self.turns("conv-killer-cut", 1)[0], stream=True)
        self.E1.mode = "cut"                                                    # its stream dies after two chunks
        try:
            self.call("/v1/chat/completions", r)
        except http.client.HTTPException:
            pass                                                                # a cut stream, as the client reads it
        self.E1.mode = "ok"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it would go there
        try:
            st, out, _ = self.call("/v1/chat/completions", r)
        except http.client.HTTPException as e:
            self.fail(f"the retry broke: {e!r}")
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"spark1-", out, "a request this box's engine died on, mid-answer, went to the other box")
        self.assertNotIn("conv-killer-cut-0", self.E2.users)
        for th in held:
            th.join(30)

    def test_this_engine_away_a_model_its_lane_served_goes_to_no_other_checkpoint(self):
        # decision 1018, this box's engine away (a crash, a restart): the model its lane served went to the other box
        # whatever its checkpoint, the uncensored 27B for a client of the stock one (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("this box's engine is said away in this test's process")
        m = self.mod
        saved_lane = m._LAST_LANE["lane"]
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "ab" * 20)       # this box served another checkpoint
        self.addCleanup(m._LAST_LANE.__setitem__, "lane", saved_lane)
        real = m.served_models

        def away(*a, **k):
            raise m.EngineUnreachable("connection refused (the test's engine away)")
        m.served_models = away
        self.addCleanup(setattr, m, "served_models", real)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        # this box's own path, which holds it while its engine comes back (role 6): a 503 at once cut a restart's
        # clients off (the delta's review, 2026-11-04). The engine of these tests answers, so it serves it
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)
        self.assertEqual(self.E2.users, [], "a client of this box got the other checkpoint")
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), TEST_REV)        # the same checkpoint there: it serves
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out)

    def test_a_request_a_dead_vllm_engine_answered_stays_here_when_sent_again(self):
        # decision 1019: vLLM answers the requests in flight when its EngineCore died with a 500, relayed and bound to
        # nothing: the SDKs' retry went to the other box, which the same request could take down (review, 2026-11-04)
        r = self.turns("conv-vllm-dead", 1)[0]
        self.E1.mode = "vllm_dead"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 500, out[:300])
        self.E1.mode = "ok"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it would go there
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "sent again, a request vLLM's engine died on here went to the other box")
        self.assertNotIn("conv-vllm-dead-0", self.E2.users)
        for th in held:
            th.join(30)

    def test_a_request_a_dead_vllm_engine_ended_in_its_stream_stays_here_when_sent_again(self):
        r = dict(self.turns("conv-vllm-dead-stream", 1)[0], stream=True)
        self.E1.mode = "vllm_dead_stream"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"EngineCore encountered an issue", out)
        self.E1.mode = "ok"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it would go there
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"spark1-", out, "sent again, a request vLLM's engine died on during its stream went elsewhere")
        self.assert_said("spark1", "502 upstream")                            # not "ok" (the delta's review, 2026-11-04)
        self.assertNotIn("conv-vllm-dead-stream-0", self.E2.users)
        for th in held:
            th.join(30)

    def test_a_request_the_other_boxs_dead_vllm_engine_answered_goes_there_or_nowhere(self):
        r = self.turns("conv-vllm-dead-there", 1)[0]
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 6.0)     # one here, none there: it goes there
        self.E2.mode = "vllm_dead"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 500, out[:300])
        self.E2.mode = "busy"                                               # its engine away now: it says 503
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertNotIn("conv-vllm-dead-there-0", self.E1.users, "sent again, it went to this box's engine")
        for th in held:
            th.join(30)

    def test_an_engine_that_closes_the_connection_on_the_count_closed_it_on_the_request(self):
        # decision 1019: the size guard's count is this request's prompt; an engine that closed the connection on it
        # was said a failed count, which a hop answers 503 and the other box serves (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("this box's pool is set in this test's process")
        self.mod._POOL.update(tokens=10000, window=9990, ts=time.time())
        self.addCleanup(self.mod._POOL.update, tokens=None, window=None, ts=0.0)
        self.E1.mode = "count_dies"
        body = dict(self.chat("qwen3.8-27b"), messages=[{"role": "user", "content": "count " * 12000}],
                    user="hop-count-dies")
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", body)[:2]
        self.assertEqual(st, 502, out[:300])
        self.assertIn(b"upstream_closed", out)

    def test_a_request_bound_while_this_box_was_alone_is_this_boxs(self):
        # a box alone notes "" for itself: paired within the 10 minutes, it took that for a box that left the pair and
        # served the request nowhere (found re-reading the rule, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the binding is set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-alone", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "")
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it would go there
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a request bound while this box was alone was not served here")
        self.assertNotIn("conv-bound-alone-0", self.E2.users)
        for th in held:
            th.join(30)

    def test_a_request_bound_here_for_a_name_no_box_serves_is_this_boxs_lanes(self):
        # decision 986: a lane name no box serves is answered by this box's lane; bound here, it was refused "this box
        # serves another model now" for 10 minutes though nothing switched (the delta's review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the binding is set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = dict(self.turns("conv-bound-986", 1)[0], model="qwen3.8-flash-next-fp4")
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "")       # this box
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)

    def test_a_request_bound_to_the_other_box_on_a_box_that_reads_no_key_runs_nowhere(self):
        # decision 1019 and the key rule: bound to the other box, a box that reads no key ran it on its own engine, the
        # fallback 1019 rules out (the delta's review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the key is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m._door_key
        m._door_key = lambda: None
        self.addCleanup(setattr, m, "_door_key", saved)
        r = self.turns("conv-bound-nokey", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "spark2")
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"cannot read its own key", out)
        self.assertNotIn("conv-bound-nokey-0", self.E1.users + self.E2.users)

    def test_a_request_bound_to_a_box_of_a_pair_dissolved_since_goes_nowhere(self):
        # decision 1019: the pair dissolved, a request bound to the box that left ran on this box's engine (the delta's
        # review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the pair is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        # the pair port's keeper waits while pair.json reads as dissolved here: seeing it (it looks every 2 s), it closed
        # this box's pair port and opened another (PAIR_PORT=0), and the next tests' connections to the port they knew
        # were refused (the whole file run alone, 2026-11-05)
        m.PAIR_PORT_KEEPER._lock.acquire()
        self.addCleanup(m.PAIR_PORT_KEEPER._lock.release)
        saved = m.pair_conf
        m.pair_conf = lambda: {"name": "spark1", "peers": []}
        self.addCleanup(setattr, m, "pair_conf", saved)
        port = m.PAIR_PORT_KEEPER.srv.server_address[1]
        r = self.turns("conv-bound-dissolved", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "spark2")
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"no longer in this pair", out)
        self.assertNotIn("conv-bound-dissolved-0", self.E1.users)
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-not-bound", 1)[0])
        self.assertEqual(st, 200, out[:300])                                    # an unbound one is served as on a box alone
        time.sleep(2.5)                                                         # past a look of the keeper
        self.assertEqual(m.PAIR_PORT_KEEPER.srv.server_address[1], port, "the pair port moved under the next tests")

    def test_a_request_bound_to_a_box_that_left_the_pair_goes_nowhere(self):
        # decision 1019: bound to a box no longer in pair.json, it was served here, the fallback 1019 rules out
        # (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the binding is set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-left", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "spark7")
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"no longer in this pair", out)
        self.assertNotIn("conv-bound-left-0", self.E1.users + self.E2.users)

    def test_a_hop_this_box_stops_by_order_before_any_answer_gets_a_503_that_says_so(self):
        # decision 1031: this box's Abort all cut a request the other box sent on, with no answer: the other box took
        # it for a request it lost (1017) and bound it to this box for 10 minutes (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the Abort all runs in this test's process")
        got = {}
        hop = dict(self.chat("qwen3.8-27b"), user="hop-abort", x_delay=6.0)

        def send():
            c = http.client.HTTPSConnection("127.0.0.1", self.pport1, timeout=60, context=client_ctx("spark2", "spark1"))
            try:
                c.request("POST", "/v1/chat/completions", json.dumps(hop).encode(), {"Content-Type": "application/json"})
                r = c.getresponse()
                got.update(st=r.status, headers=dict(r.getheaders()), body=r.read())
            except Exception as e:
                got["err"] = repr(e)
            finally:
                c.close()
        th = threading.Thread(target=send, daemon=True)
        th.start()
        deadline = time.monotonic() + 10
        while "hop-abort" not in self.E1.users and time.monotonic() < deadline:
            time.sleep(0.05)
        self.mod.stop_generations("test: Abort all", floor=False)
        th.join(30)
        self.assertNotIn("err", got, "cut with no answer")
        self.assertEqual(got["st"], 503, got.get("body", b"")[:300])
        self.assertEqual(got["headers"].get("X-Alzeo-Pair-Ended"), "order")

    def test_a_hop_this_box_stops_by_order_in_its_open_stream_says_so_before_the_cut(self):
        # decision 1031, in a stream the hold opened: the other box read a cut and bound the request (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the Abort all runs in this test's process")
        hop = dict(self.chat("qwen3.8-27b"), user="hop-abort-stream", stream=True, x_delay=16.0)
        c = http.client.HTTPSConnection("127.0.0.1", self.pport1, timeout=60, context=client_ctx("spark2", "spark1"))
        self.addCleanup(c.close)
        c.request("POST", "/v1/chat/completions", json.dumps(hop).encode(), {"Content-Type": "application/json"})
        r = c.getresponse()                                                     # the hold opens it after KEEPALIVE_S
        self.assertEqual(r.status, 200)
        got = r.read1(65536)                                                    # its first keepalive
        self.mod.stop_generations("test: Abort all", floor=False)
        try:
            while True:
                b = r.read1(65536)
                if not b:
                    break
                got += b
        except (http.client.HTTPException, OSError):
            pass                                                                # cut, not ended
        self.assertIn(b'"x_alzeo_ended":"order"', got, got[-300:])

    def test_a_hop_this_box_stops_by_order_answers_in_its_clients_dialect(self):
        # the 503 of an end by order was written by hand: no Retry-After, the OpenAI shape on /v1/messages (the delta's 3rd
        # review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the Abort all runs in this test's process")
        got = {}
        hop = {"model": "qwen3.8-27b", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}],
               "metadata": {"user_id": "hop-abort-anthropic"}, "x_delay": 6.0}

        def send():
            c = http.client.HTTPSConnection("127.0.0.1", self.pport1, timeout=60, context=client_ctx("spark2", "spark1"))
            try:
                c.request("POST", "/v1/messages", json.dumps(hop).encode(), {"Content-Type": "application/json"})
                r = c.getresponse()
                got.update(st=r.status, headers=dict(r.getheaders()), body=r.read())
            except Exception as e:
                got["err"] = repr(e)
            finally:
                c.close()
        before = len(self.E1.seen)
        th = threading.Thread(target=send, daemon=True)
        th.start()
        deadline = time.monotonic() + 10
        while len(self.E1.seen) == before and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        self.mod.stop_generations("test: Abort all", floor=False)
        th.join(30)
        self.assertNotIn("err", got, "cut with no answer")
        self.assertEqual(got["st"], 503, got.get("body", b"")[:300])
        self.assertEqual(got["headers"].get("X-Alzeo-Pair-Ended"), "order")
        self.assertEqual(got["headers"].get("Retry-After"), "30")
        self.assertEqual(json.loads(got["body"]).get("type"), "error", "not the Anthropic dialect")

    def test_a_dead_vllm_engine_under_an_ordered_stop_is_said_to_be_one(self):
        # decision 1031: vLLM answers the requests in flight with its EngineDeadError while its lane stops by order, and
        # the other box bound them (the delta's 3rd review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: True
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead"
        st, out, headers = self.tls_call(self.pport1, "/v1/chat/completions",
                                         dict(self.chat("qwen3.8-27b"), user="hop-dead-ordered"))
        self.assertEqual(st, 500, out[:300])
        self.assertEqual(headers.get("X-Alzeo-Pair-Ended"), "order")

    def test_a_request_bound_here_is_refused_when_the_others_list_failed_but_named_it(self):
        # bound to this box, which serves another model now: refused only when the other box's current list named it; a
        # read that failed (its models empty, its last good list kept in "known") let this box answer with its other
        # model (the delta's 3rd review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the binding and the lists are set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-failed-read", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "")
        real = m.served_models
        m.served_models = lambda *a, **k: ["qwen3.8-flash-next-fp4"]           # this box serves another model now
        self.addCleanup(setattr, m, "served_models", real)
        with m._PAIR_LOCK:
            row = dict(m._PAIR_MODELS["spark2"])
            m._PAIR_MODELS["spark2"] = dict(row, models=(), known=("qwen3.8-27b",), error="unreachable",
                                            ts=time.monotonic())
        self.addCleanup(lambda: m._PAIR_MODELS.__setitem__("spark2", row))
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"serves another model now", out)
        self.assertNotIn("conv-bound-failed-read-0", self.E1.users)

    def test_a_hop_whose_engine_closes_as_its_lane_stops_by_order_gets_a_503_that_says_so(self):
        # decision 1031: a hop queued or in prefill here when this box's cockpit loads another lane, its engine closing
        # before any byte: a plain 503, which the other box took for an engine away, served the request itself, paused
        # this box 30 s and counted a failover (the 4th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: True
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.addCleanup(m._KILLERS.clear)
        self.E1.mode = "die"
        st, out, headers = self.tls_call(self.pport1, "/v1/chat/completions",
                                         dict(self.chat("qwen3.8-27b"), user="hop-closed-ordered"))
        self.assertEqual(st, 503, out[:300])
        self.assertEqual(headers.get("X-Alzeo-Pair-Ended"), "order", out[:300])
        self.assertEqual(len(m._KILLERS), 0, "an ordered stop bound the request")

    def test_an_ordered_death_in_a_stream_the_keeper_opened_is_said_in_it(self):
        # decision 1031: vLLM's 500 under an ordered stop, once the keeper had opened the stream: no header goes then,
        # and the other box bound the request for 10 minutes (the 4th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: True
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead"
        hop = dict(self.chat("qwen3.8-27b"), user="hop-dead-kept", stream=True, x_dead_after=m.KEEPALIVE_S + 1.5)
        st, out, _ = self.tls_call(self.pport1, "/v1/chat/completions", hop)
        self.assertEqual(st, 200, out[:300])                                    # the keeper's stream
        self.assertIn(b"EngineCore", out)
        self.assertIn(b'"x_alzeo_ended":"order"', out, out[-400:])

    def test_a_death_the_other_box_relayed_before_its_client_left_is_bound(self):
        # decision 1019: a death the other box relayed in its stream was bound at the stream's end, once an order said
        # after it would have come; a client that left first (the SDKs raise on that event) left it unbound, and its
        # resend could take this box's engine down too (the 4th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 6.0)    # this box busy: a new one goes there
        self.E2.mode = "vllm_dead_more"
        raw = json.dumps(dict(self.turns("conv-dead-left", 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"EngineCore", got, got[-300:])
        self.assertIn(b"spark2", got, "served here: the test needs the other box's stream")
        resp.close()                                                            # it raises on that event, and leaves:
        c.close()                                                               # its answer held the socket
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        deadline = time.monotonic() + 6
        while m.killer_box(fp) is None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(m.killer_box(fp), "spark2", "a death relayed before its client left was bound to nothing")
        for th in held:
            th.join(30)

    def test_a_request_bound_here_is_answered_here_once_the_other_box_said_nothing_for_long(self):
        # bound to this box, which serves another model now: the other box's last good list named the model long ago,
        # every read failing since (it is off): refused for the binding's 10 minutes, where the same request unbound got
        # this box's lane's answer (decision 986; the 4th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the binding and the lists are set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-off", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "")
        real = m.served_models
        m.served_models = lambda *a, **k: ["qwen3.8-flash-next-fp4"]           # this box serves another model now
        self.addCleanup(setattr, m, "served_models", real)
        with m._PAIR_LOCK:
            row = dict(m._PAIR_MODELS["spark2"])
            m._PAIR_MODELS["spark2"] = dict(row, models=(), known=("qwen3.8-27b",), error="unreachable",
                                            known_ts=time.monotonic() - m.PAIR_KNOWN_WAIT_S - 60, ts=time.monotonic())
        self.addCleanup(lambda: m._PAIR_MODELS.__setitem__("spark2", row))
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)

    def test_a_request_bound_here_is_answered_here_when_the_other_box_moved_to_another_model(self):
        # a 409 "moved" leaves the other box's row with no models, its last list and no error: refused 10 minutes as if
        # its read had failed, where the same request unbound got this box's lane's answer (the 5th review of v1.43.0)
        if not self.INPROC:
            self.skipTest("the binding and the lists are set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-moved", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "")
        real = m.served_models
        m.served_models = lambda *a, **k: ["qwen3.8-flash-next-fp4"]           # this box serves another model now
        self.addCleanup(setattr, m, "served_models", real)
        with m._PAIR_LOCK:
            row = dict(m._PAIR_MODELS["spark2"])
            m._PAIR_MODELS["spark2"] = dict(row, models=(), known=("qwen3.8-27b",), error=None,
                                            known_ts=time.monotonic(), ts=time.monotonic())
        self.addCleanup(lambda: m._PAIR_MODELS.__setitem__("spark2", row))
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out)

    def test_a_death_relayed_then_an_order_said_after_it_binds_nothing(self):
        # decision 1054: the other box relays its engine's death, its client leaves at once (the SDKs raise on that
        # event), and the other box says only then that it ended the request by order (its read of systemd was late):
        # bound 10 minutes at the client's leaving, where the order said otherwise (the 5th review of v1.43.0)
        self._death_then_order(between=10, silence=1.0)

    def test_a_death_relayed_then_an_order_said_after_a_silence_binds_nothing(self):
        # the same with nothing between the death and the order: no write failed, the watch saw the client gone first,
        # shut the connection within a second, and the request was bound 10 minutes in 5 runs of 5 (the 6th review of
        # v1.43.0, 2026-11-04; the test before replaced the watch, which hid it)
        self._death_then_order(between=0, silence=1.5)

    def _death_then_order(self, between, silence):
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        Base = fake_peer(["qwen3.8-27b", "marker-1054"], PEER_ANSWER)
        event = (b'data: {"id":"c","object":"chat.completion.chunk","model":"qwen3.8-27b","choices":[{"index":0,'
                 b'"delta":{"content":"from the peer "}}]}\n\n')

        class DeadThenOrder(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def chunk(part):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                try:
                    chunk(event)
                    chunk(b"data: " + VLLM_DEAD_EVENT + b"\n\n")
                    for _ in range(between):                                    # the entry writes them to no client
                        time.sleep(0.05)
                        chunk(event)
                    time.sleep(silence)
                    chunk(HOP_ENDED_OPENAI)                                     # the order, said after the death
                except OSError:
                    return
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)                      # cut, not ended
        self.repoint(tls_peer(self, DeadThenOrder))
        self.until_listed("marker-1054")
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 6.0)    # this box busy: a new one goes there
        raw = json.dumps(dict(self.turns("conv-dead-order", 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"from the peer", got, "served here: the test needs the other box's stream")
        self.assertIn(b"EngineCore", got)
        resp.close()                                                            # it raises on that event, and leaves
        c.close()
        self.assert_said("spark1", "said it ended by order: its binding is undone (decision 1054)")
        self.assertIsNone(m.killer_box(m.body_fingerprint(raw, "/v1/chat/completions")),
                          "an order said after the death was bound 10 minutes")
        for th in held:
            th.join(30)

    def test_a_death_relayed_is_bound_at_once_when_its_client_leaves_and_that_box_goes_quiet(self):
        # decision 1061: the wait for an order after the death had no bound once the death was relayed: the other box
        # quiet (no FIN), the request was bound 60 s after its client left, its retry running unbound on this box's engine
        # meanwhile (the 7th review of v1.43.0, 2026-11-04). Bound at once now; an order said in 5 s would undo it
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        Base = fake_peer(["qwen3.8-27b", "marker-quiet"], PEER_ANSWER)
        event = (b'data: {"id":"c","object":"chat.completion.chunk","model":"qwen3.8-27b","choices":[{"index":0,'
                 b'"delta":{"content":"from the peer "}}]}\n\n')

        class DeadThenQuiet(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for part in (event, b"data: " + VLLM_DEAD_EVENT + b"\n\n"):
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                        self.wfile.flush()
                    time.sleep(8.0)                                             # quiet, its connection open
                except OSError:
                    return
        self.repoint(tls_peer(self, DeadThenQuiet))
        self.until_listed("marker-quiet")
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 6.0)    # this box busy: a new one goes there
        raw = json.dumps(dict(self.turns("conv-dead-quiet", 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"from the peer", got, "served here: the test needs the other box's stream")
        resp.close()                                                            # it raises on that event, and leaves
        c.close()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        deadline = time.monotonic() + 3
        while m.killer_box(fp) is None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(m.killer_box(fp), "spark2", "its retry ran unbound while that box was quiet")
        for th in held:
            th.join(30)

    def test_a_death_here_whose_order_shows_at_the_streams_end_is_unbound(self):
        # a request of this box's own client, bound at the death by a read of systemd that saw no stop job yet, stayed
        # bound 10 minutes once the order showed at the stream's end, its retries refused (the 7th review of v1.43.0)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: self.E1.ordered_now                  # the stop job, seen after the death
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead_late_order"
        r = dict(self.turns("conv-local-late-order", 1)[0], stream=True)
        raw = json.dumps(r).encode()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        self.assertEqual(resp.status, 200)
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"EngineCore", got)
        # the premise: bound at the death, its order not seen yet; unread, the test passed whenever the death came after
        # the order (the 9th review of v1.43.0, 2026-11-05)
        self.assertEqual(m.killer_box(fp), "", "the test needs the request bound at the death")
        got += resp.read()                                                      # its clean end
        self.assertIn("conv-local-late-order-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertIsNone(m.killer_box(fp), "an order shown at the stream's end left the request bound")

    def test_a_death_here_whose_order_shows_before_a_cut_is_unbound(self):
        # the same death, its stop job seen after it, then the server killed: the binding was undone at a clean end only,
        # and the client's retries stayed bound 10 minutes (the 8th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: self.E1.ordered_now                  # the stop job, seen after the death
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead_late_order_cut"
        r = dict(self.turns("conv-local-late-order-cut", 1)[0], stream=True)
        raw = json.dumps(r).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        got, premise = b"", None
        try:
            while True:
                b = resp.read1(65536)
                if not b:
                    break
                got += b
                if premise is None and b"EngineCore" in got:
                    premise = m.killer_box(fp)  # bound at the death, its order not seen yet (the 9th review of v1.43.0)
        except (OSError, http.client.HTTPException):
            pass                                                                # cut, as its engine's end was
        c.close()
        self.assertIn(b"EngineCore", got)
        self.assertEqual(premise, "", "the test needs the request bound at the death")
        self.assertIn("conv-local-late-order-cut-0", self.E1.users, "the test needs this box's engine to have it")
        deadline = time.monotonic() + 3
        while m.killer_box(fp) is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIsNone(m.killer_box(fp), "an order shown before the stream's cut left the request bound")

    def test_a_death_here_whose_client_leaves_before_its_order_shows_is_unbound(self):
        # its client gone at the death (the SDKs raise on that event), before the stream's end: that end, where a stop job
        # seen late undoes the binding, never came, and the request stayed bound 10 minutes after an order (the 9th review
        # of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: self.E1.ordered_now                  # the stop job, seen after the death
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead_late_order"
        r = dict(self.turns("conv-local-leaves-first", 1)[0], stream=True)
        raw = json.dumps(r).encode()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.connect()
        c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # it leaves with a reset
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"EngineCore", got)
        self.assertEqual(m.killer_box(fp), "", "the test needs the request bound at the death")
        resp.close()
        c.close()                                                               # before its order shows (0.4 s on)
        time.sleep(3.5)
        self.assertIn("conv-local-leaves-first-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertIsNone(m.killer_box(fp), "an order shown after its client left left the request bound")

    def test_a_death_whose_own_read_saw_the_order_stays_unbound_at_the_cut(self):
        # its death came while its stop job ran (nothing bound), the cut was read once the job was over: the cut bound
        # it 10 minutes, as if an engine had died on it (the 9th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: self.E1.ordered_now
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead_ordered_then_cut"
        r = dict(self.turns("conv-ordered-then-cut", 1)[0], stream=True)
        raw = json.dumps(r).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        got, premise = b"", "unread"
        try:
            while True:
                b = resp.read1(65536)
                if not b:
                    break
                got += b
                if premise == "unread" and b"EngineCore" in got:
                    premise = m.killer_box(fp)  # its death read its stop job: nothing bound (the 10th review)
        except (OSError, http.client.HTTPException):
            pass                                                                # cut
        self.assertIn(b"EngineCore", got)
        self.assertIsNone(premise, "the test needs the death's own read to see the order")
        self.assertIn("conv-ordered-then-cut-0", self.E1.users, "the test needs this box's engine to have it")
        time.sleep(0.5)
        self.assertIsNone(m.killer_box(m.body_fingerprint(raw, "/v1/chat/completions")),
                          "a death its own read saw ordered was bound at the cut")

    def test_a_cut_is_bound_before_its_client_sees_it(self):
        # an engine that dies with no death event (a crash): its connection was shut, then systemd read again and the
        # request bound, a quick retry arriving unbound meanwhile, free to reach the other box (decision 1019; the 9th
        # review of v1.43.0, 2026-11-05). systemd is slow here, as under the memory pressure that ends engines
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        reads = []

        def slow():
            reads.append(1)
            time.sleep(1.0)
            return False
        m.unit_stopping_by_order = slow
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "cut"
        r = dict(self.turns("conv-cut-bound", 1)[0], stream=True)
        raw = json.dumps(r).encode()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        try:
            while resp.read1(65536):
                pass
        except (OSError, http.client.HTTPException):
            pass                                                                # cut: its client retries now
        self.assertIn("conv-cut-bound-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertEqual(m.killer_box(fp), "", "the cut reached its client before the request was bound")
        self.assertEqual(len(reads), 1, "the cut read systemd more than once")

    def _sent(self, raw):
        """raw sent to this box as its own client's request: what it got, cut or ended."""
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        try:
            c.request("POST", "/v1/chat/completions", raw,
                      {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
            try:
                return c.getresponse().read()
            except http.client.IncompleteRead as e:
                return e.partial
        finally:
            c.close()

    def test_a_retry_that_bound_nothing_keeps_the_binding_a_death_before_it_made(self):
        # a retry whose death read saw its lane's stop bound nothing, and at its ordered end undid the binding an earlier
        # real death of the same body had made, the next retry free to reach the other box (decision 1019; the 12th
        # review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        ordered = {"now": False}
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: ordered["now"]
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead_stream"
        raw = json.dumps(dict(self.turns("conv-unbound-retry", 1)[0], stream=True)).encode()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        self.assertIn(b"EngineCore", self._sent(raw))
        self.assertEqual(m.killer_box(fp), "", "the test needs the first death bound here")
        ordered["now"] = True                   # its lane's stop job: the retry's death is the order's, seen at once
        self.assertIn(b"EngineCore", self._sent(raw))
        self.assertEqual(self.E1.users.count("conv-unbound-retry-0"), 2, "the test needs the retry served here")
        self.assertEqual(m.killer_box(fp), "", "a retry that bound nothing undid the binding of a death before it")

    def test_a_try_ended_by_order_keeps_the_binding_its_first_try_made(self):
        # its first try closed by its engine (a real death: bound), the request held and sent again: the second try's
        # ordered end undid the first try's binding, the binding state kept for the whole request (decision 1019; the
        # 12th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        ordered = {"now": False}
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: ordered["now"]
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        held = m.H._hold

        def back_at_once(h, why, streamed):
            ordered["now"] = True               # the try sent again runs as its lane is stopped by order
            self.E1.mode = "vllm_dead_stream"
            return True
        m.H._hold = back_at_once
        self.addCleanup(setattr, m.H, "_hold", held)
        self.E1.mode = "die"
        raw = json.dumps(dict(self.turns("conv-two-tries", 1)[0], stream=True)).encode()
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        out = self._sent(raw)
        self.assertEqual(self.E1.users.count("conv-two-tries-0"), 2, "the test needs both tries on this box's engine")
        self.assertIn(b"EngineCore", out)
        self.assertEqual(m.killer_box(fp), "", "a try ended by order undid the binding its first try made")

    def test_a_stop_during_a_dead_streams_reads_counts_nothing_and_holds_nothing_back(self):
        # dead, a vLLM stream stayed a stop target while systemd was read at its death and at its clean end: a floor stop
        # that landed then counted it, started the floor's episode, and new generations were refused for nothing (the
        # 12th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the stop is made in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        floor = dict(m._FLOOR)                                                  # the floor's state, given back
        self.addCleanup(lambda: (m._FLOOR.clear(), m._FLOOR.update(floor)))
        self.E1.mode = "vllm_dead_more"         # its death said, its stream's end 2 s later
        for which in (1, 2):                    # the read at its death, the read at its clean end
            with self.subTest(read=which):
                m._FLOOR.clear(); m._FLOOR.update(floor)                        # no episode of the read before
                reads, stops = [], []

                def a_stop_meanwhile():
                    reads.append(1)
                    if len(reads) == which:
                        stops.append(m.stop_generations("this test's memory floor", floor=True))
                    return False
                m.unit_stopping_by_order = a_stop_meanwhile
                out = self._sent(json.dumps(dict(self.turns(f"conv-dead-read-{which}", 1)[0], stream=True)).encode())
                self.assertIn(b"EngineCore", out)
                self.assertEqual(len(stops), 1, "the test needs its stop")
                self.assertEqual(stops[0][0], 0, "a dead stream was counted as stopped")
                self.assertIsNone(m.floor_refusal(), "a floor that stopped nothing held new generations back")

    def test_a_hops_death_relayed_is_never_said_an_order_by_a_stop_after_it(self):
        # this box's engine died on a request the other box sent, the death relayed to it, and a floor stop here before
        # the stream's end counted the dead stream and cut it as stopped, telling that box the request ended by order,
        # which undid the binding its real death had made there (decision 1019; the 12th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the stop is made in this test's process")
        m = self.mod
        floor = dict(m._FLOOR)
        self.addCleanup(lambda: (m._FLOOR.clear(), m._FLOOR.update(floor)))
        self.E1.mode = "vllm_dead_more"         # its death said, then more events for 2 s before its end
        got = {}

        def hop():
            try:
                got["out"] = self.tls_call(self.pport1, "/v1/chat/completions",
                                           dict(self.chat("qwen3.8-27b"), user="hop-dead-then-stop", stream=True))[1]
            except http.client.IncompleteRead as e:
                got["out"] = e.partial
        th = threading.Thread(target=hop, daemon=True)
        th.start()
        deadline = time.monotonic() + 10
        while "hop-dead-then-stop" not in self.E1.users and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("hop-dead-then-stop", self.E1.users, "the test needs this box's engine to have it")
        time.sleep(0.6)                         # its death relayed (0.15 s in), its stream not over (2 s)
        stopped = m.stop_generations("this test's memory floor", floor=True)
        th.join(30)
        self.assertEqual(stopped[0], 0, "a dead stream was counted as stopped")
        self.assertIn(b"EngineCore", got.get("out", b""))
        self.assertNotIn(b"x_alzeo_ended", got["out"], "a real death relayed was then said an order")

    def _left_after_its_first_event(self, tag):
        """A stream this box sends to the other box (one generation here, none there), its client gone after the first
        event it got: the request's fingerprint, and the generation that kept this box busy."""
        m = self.mod
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 6.0)     # one here, none there: it goes there
        raw = json.dumps(dict(self.turns(tag, 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.connect()
        c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # it leaves with a reset
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        resp.read1(65536)                       # its first event
        resp.close()
        c.close()                               # gone: the next event's write fails, before the death comes
        return m.body_fingerprint(raw, "/v1/chat/completions"), held

    def _left_at_once(self, tag, stream=False, after=0.3, reset=True, **extra):
        """A request this box sends to the other box (one generation here, none there), its client gone `after` seconds
        after sending it, with a reset (or a plain close, reset=False): the request's fingerprint, and the generation
        that kept this box busy."""
        m = self.mod
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 6.0)     # one here, none there: it goes there
        raw = json.dumps(dict(self.turns(tag, 1)[0], stream=stream, **extra)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.connect()
        if reset:
            c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # it leaves with a reset
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        time.sleep(after)
        c.close()
        return m.body_fingerprint(raw, "/v1/chat/completions"), held

    def _bound_within(self, fp, secs):
        m = self.mod
        deadline = time.monotonic() + secs
        while m.killer_box(fp) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        return m.killer_box(fp)

    def _the_other_box_is(self, how):
        """spark2 a LateBox that does `how`, listed as serving the 27B: its class, whose `got` says what it read."""
        late = type("Late", (LateBox,), {"how": how, "got": []})
        self.repoint(tls_peer(self, late))
        self.mod._PAIR_MODELS["spark2"] = {"ts": time.monotonic(), "models": ("qwen3.8-27b",), "rows": [],
                                           "error": None, "answered": True, "known": ("qwen3.8-27b",),
                                           "known_ts": time.monotonic(), "read_s": 0.01, "running": 0,
                                           "revision": TEST_REV}
        self.addCleanup(self.mod._PAIR_COOL.pop, "spark2", None)
        return late

    def _stopped_in_its_count(self, tag, floor, hop=False, fails=False):
        """A long prompt this box keeps (the other box busier), a stop during its count (floor or not), the count
        answering too long (or failing): (status, body) its client got, None when the proxy cut it. hop: sent by the
        other box to this box's pair port."""
        m = self.mod
        m._POOL.update(tokens=10000, window=9990, ts=time.time())
        self.addCleanup(m._POOL.update, tokens=None, window=None, ts=0.0)
        key = m.UPSTREAM_API_KEY
        self.addCleanup(setattr, m, "UPSTREAM_API_KEY", key)
        m.UPSTREAM_API_KEY = KEY_1
        windows, floor_state = dict(m._STOP_WINDOWS), dict(m._FLOOR)
        self.addCleanup(lambda: (m._STOP_WINDOWS.clear(), m._STOP_WINDOWS.update(windows)))
        self.addCleanup(lambda: (m._FLOOR.clear(), m._FLOOR.update(floor_state)))
        if not hop:                             # a request the other box sent on is no box's to share
            self.busy(self.E2, self.port2, KEY_2, "marker-27b", 2, 10.0)
            self.E2.served[:] = ["qwen3.8-27b", f"marker-busy-{tag}"]
            self.until_listed(f"marker-busy-{tag}")
        self.E1.count_delay = 1.5
        if fails:
            self.E1.mode = "count_fails"
        body = dict(self.chat("qwen3.8-27b"), user=tag, messages=[{"role": "user", "content": f"{tag} " + "count " * 12000}])
        got = {}

        def send():
            try:
                got["r"] = (self.tls_call(self.pport1, "/v1/chat/completions", body)[:2] if hop
                            else self.call("/v1/chat/completions", body)[:2])
            except (OSError, http.client.HTTPException):
                got["r"] = None
        th = threading.Thread(target=send, daemon=True)
        th.start()
        self.assertTrue(_until(lambda: any(p == "/tokenize" for p, *_ in self.E1.seen), 5), "no count was made")
        self.assertEqual(m.stop_generations(f"this test's {'memory floor' if floor else 'Abort all'}", floor=floor)[0], 1,
                         "the stop did not reach it")
        th.join(30)
        return got.get("r")

    def test_a_hop_a_stop_marks_during_its_count_still_gets_its_400(self):
        # a request the other box sent on, its count too long: the size guard's 400, which the entry serves itself on,
        # became a 503 ended by order once a stop reached it during its count (the 4th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("this box's pool and stop are in this test's process")
        r = self._stopped_in_its_count("conv-hop-too-long", floor=False, hop=True)
        self.assertIsNotNone(r, "the hop's request was cut")
        st, out = r
        self.assertEqual(st, 400, out[:300])
        self.assertIn(b"context_length_exceeded", out)

    def test_a_request_the_floor_stops_during_its_count_is_refused_by_it_never_handed_on(self):
        # its count too long for this box's pool, which hands it to the other box's: the floor's stop during the count is
        # answered by the floor, with when to come back, the other box never getting it (the 4th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("this box's pool and stop are in this test's process")
        r = self._stopped_in_its_count("conv-floor-too-long", floor=True)
        self.assertIsNotNone(r, "a request the floor stopped was cut bare")
        st, out = r
        self.assertIn(st, (503, 529), out[:300])
        self.assertIn(b"memory floor", out)
        self.assertNotIn("conv-floor-too-long", self.E2.users, "a request the floor stopped went to the other box")

    def _left_then(self, how, tag, reset=True):
        """A shared request to a LateBox that does `how` once its client left (answer_now set right after): the box."""
        late = self._the_other_box_is(how)
        late.answer_now = threading.Event()
        fp, held = self._left_at_once(tag, after=0.1, reset=reset)
        late.answer_now.set()
        self.addCleanup(lambda: [th.join(30) for th in held])
        self.assertTrue(_until(lambda: f"{tag}-0" in late.got, 5), "the test needs the other box to have it")
        return late, fp

    def test_a_box_that_closes_on_a_request_as_its_client_leaves_is_read_with_its_client_gone(self):
        # its close came within the second its client left, before the watch's first look: read as one with its client
        # there (the 6th review of v1.44.0: no test held the immediate look of that branch)
        if not self.INPROC:
            self.skipTest("the journal is read in this test's process")
        self._left_then("close_soon", "conv-entry-close-soon")
        self.assertTrue(self.until_in_journal("spark1", "its client gone, spark2 had the request and closed the "
                                                        "connection with no answer", within=3.0),
                        "a close that came as its client left was read with its client there")

    def test_a_refusal_as_its_client_leaves_moves_no_conversation_there(self):
        # a 409 that came once its client had left read as served: the conversation's next turn went to the box that
        # refused it (the 6th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the affinities are read in this test's process")
        m = self.mod
        before = len(m._AFFINITY.get("spark2", ()))
        self._left_then("moved_soon", "conv-entry-moved-soon")
        self.assertTrue(self.until_in_journal("spark1", "CLIENT GONE during pair", within=3.0))
        time.sleep(0.3)
        self.assertEqual(len(m._AFFINITY.get("spark2", ())), before, "a refusal after its client left moved its turns there")
        # forgotten there, never paused: that box answered (the 7th review of v1.44.0)
        self.assertLessEqual(m._PAIR_COOL.get("spark2", 0), time.monotonic(), "a box that answered was paused")

    def test_an_error_as_its_client_leaves_keeps_the_conversation_there(self):
        # an error that is no refusal (a 500 that is no engine's death) once its client had left: the conversation's
        # prefix forgotten on the box that holds it in its cache, its retry placed by balance alone and prefilled again
        # elsewhere (the 7th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the affinities are read in this test's process")
        m = self.mod
        before = len(m._AFFINITY.get("spark2", ()))
        self._left_then("error_soon", "conv-entry-error-soon")
        self.assertTrue(self.until_in_journal("spark1", "CLIENT GONE during pair", within=3.0))
        time.sleep(0.3)
        self.assertGreater(len(m._AFFINITY.get("spark2", ())), before, "an error after its client left moved its turns away")

    def test_an_answer_cut_as_its_client_leaves_is_read_with_its_client_gone(self):
        # the head of an answer not streamed, then its cut, within the second its client left: read with its client
        # there, a 502 written to nobody and an upstream cut said (the 7th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the journal is read in this test's process")
        cuts = self.journal("spark1").count("UPSTREAM CUT pair")
        self._left_then("cut_soon", "conv-entry-cut-soon")
        self.assertTrue(self.until_in_journal("spark1", "CLIENT GONE during pair", within=3.0),
                        "an answer cut as its client left was read with its client there")
        self.assertEqual(self.journal("spark1").count("UPSTREAM CUT pair"), cuts)

    def test_a_whole_answer_as_its_client_leaves_goes_to_nobody(self):
        # a whole answer that came within the second its client left, before the watch's first look: written to a
        # client gone, said served or with no outcome (the 6th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the journal is read in this test's process")
        gone = self.journal("spark1").count("CLIENT GONE during pair")
        oks = self.journal("spark1").count("ok pair")
        self._left_then("answer_soon", "conv-entry-answer-soon", reset=False)
        self.assertTrue(_until(lambda: self.journal("spark1").count("CLIENT GONE during pair") > gone, 5),
                        "a whole answer that came as its client left was not said gone")
        self.assertEqual(self.journal("spark1").count("ok pair"), oks, "an answer written to nobody was said served")

    def test_a_box_that_refuses_a_shared_request_as_its_client_leaves_serves_it_nowhere(self):
        # its 503 came within the second its client left, before the watch's tick: read as a refusal with its client
        # there, the request was served here for nobody (the 5th review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the pause is read in this test's process")
        m = self.mod
        self._left_then("away_soon", "conv-entry-left-away-soon")   # its refusal once its client left
        self.assertTrue(_until(lambda: m._PAIR_COOL.get("spark2", 0) > time.monotonic(), 6),
                        "a box that refused a shared request as its client left was not paused")
        # its client's leaving read with the refusal: the request served nowhere, where the path of a client still there
        # had this box serve it, cut a moment later as its client was found gone
        self.assertTrue(self.until_in_journal("spark1", "its client left, and spark2 gets nothing", within=3.0),
                        "the refusal was read as one with its client there")
        self.assertFalse(self.until_in_journal("spark1", "this box serves it", within=0.5),
                         "a request whose client left was served here")

    def test_a_box_away_that_refuses_a_shared_request_after_its_client_left_is_paused(self):
        # its 503 came once its client had left: said served, the box was neither paused nor its conversation
        # forgotten there, its retry refused again (the 3rd review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the pause is read in this test's process")
        m = self.mod
        late = self._the_other_box_is("away")
        fp, held = self._left_at_once("conv-entry-left-away")
        self.assertTrue(_until(lambda: "conv-entry-left-away-0" in late.got, 5), "the test needs the other box to have it")
        self.assertTrue(_until(lambda: m._PAIR_COOL.get("spark2", 0) > time.monotonic(), 6),
                        "a box that refused a shared request after its client left was not paused")
        self.assertIsNone(m.killer_box(fp), "a refusal was bound")
        # its client gone first, its path pinned: with the watch late, the refusal took the path of a client still
        # there, which pauses too and serves it here for nobody (the 4th review of v1.44.0)
        self.assertTrue(self.until_in_journal("spark1", "its client left, and spark2 gets nothing", within=3.0))
        self.assertNotIn("conv-entry-left-away-0", self.E1.users, "a request whose client left was served here")
        for th in held:
            th.join(30)

    def test_a_request_a_stop_marks_during_its_count_is_never_handed_on_as_too_long(self):
        # its count said too long for this box's pool, which hands it to the other box's (v6.52): a request a stop
        # marked during its count went there and was served (the 3rd review of v1.44.0)
        if not self.INPROC:
            self.skipTest("this box's pool and stop are in this test's process")
        m = self.mod
        m._POOL.update(tokens=10000, window=9990, ts=time.time())
        self.addCleanup(m._POOL.update, tokens=None, window=None, ts=0.0)
        key = m.UPSTREAM_API_KEY
        self.addCleanup(setattr, m, "UPSTREAM_API_KEY", key)
        m.UPSTREAM_API_KEY = KEY_1
        windows = dict(m._STOP_WINDOWS)
        self.addCleanup(lambda: (m._STOP_WINDOWS.clear(), m._STOP_WINDOWS.update(windows)))
        held = self.busy(self.E2, self.port2, KEY_2, "marker-27b", 2, 10.0)
        self.E2.served[:] = ["qwen3.8-27b", "marker-busy-long"]
        self.until_listed("marker-busy-long")
        self.E1.count_delay = 1.5               # its count answers, too long, 1.5 s on
        body = dict(self.chat("qwen3.8-27b"), user="conv-stopped-too-long",
                    messages=[{"role": "user", "content": "stopped too long " + "count " * 12000}])
        t = threading.Thread(target=lambda: self._quiet_call(body), daemon=True)
        t.start()
        self.assertTrue(_until(lambda: any(p == "/tokenize" for p, *_ in self.E1.seen), 5), "no count was made")
        self.assertEqual(m.stop_generations("this test's Abort all", floor=False)[0], 1, "the stop did not reach it")
        t.join(30)
        self.assertNotIn("conv-stopped-too-long", self.E2.users, "a request a stop marked was handed to the other box")
        self.assertTrue(self.until_in_journal("spark1", "stopped by the cockpit", within=3.0),
                        "a request a stop marked during its count was not cut as stopped")
        for th in held:
            th.join(30)

    def test_a_request_the_floor_stops_during_its_count_is_refused_by_the_floor(self):
        # the floor's stop reached it during its count, which then failed: refused by the floor, with when to come
        # back, never a bare cut (no test drove the floor's branch, the 3rd review of v1.44.0)
        if not self.INPROC:
            self.skipTest("this box's pool and stop are in this test's process")
        r = self._stopped_in_its_count("conv-floor-in-count", floor=True, fails=True)
        self.assertIsNotNone(r, "a request the floor stopped was cut bare")
        st, out = r
        self.assertIn(st, (503, 529), out[:300])
        self.assertIn(b"memory floor", out)
        self.assertNotIn("conv-floor-in-count", self.E2.users, "a request the floor stopped reached the other box")

    def _quiet_call(self, body):
        """(status, body) of a call, or (None, b"") when the proxy cut it."""
        try:
            st, out, _ = self.call("/v1/chat/completions", body)
            return st, out
        except (OSError, http.client.HTTPException):
            return None, b""

    def test_a_box_that_closes_on_a_shared_request_after_its_client_left_is_paused(self):
        # bound as its client left, but said "served": the box that closed on it with no answer was neither paused nor
        # its conversation forgotten there, where the same close with its client present pauses it (the 2nd review of
        # v1.44.0)
        if not self.INPROC:
            self.skipTest("the pause is read in this test's process")
        m = self.mod
        late = self._the_other_box_is("close")
        fp, held = self._left_at_once("conv-entry-left-pause")
        self.assertEqual(self._bound_within(fp, 10), "spark2", "the test needs the close bound")
        # its client gone first, its path pinned: with the watch late, the same close took the path of a client still
        # there, which binds and pauses too (the 3rd review of v1.44.0)
        self.assertTrue(self.until_in_journal("spark1", "its client gone, spark2 had the request and closed the "
                                                        "connection with no answer", within=3.0))
        self.assertTrue(_until(lambda: m._PAIR_COOL.get("spark2", 0) > time.monotonic(), 3),
                        "a box that closed on a shared request after its client left was not paused")
        self.assertIn("conv-entry-left-pause-0", late.got)
        for th in held:
            th.join(30)

    def test_an_ollama_stream_that_ends_whole_as_its_client_leaves_binds_nothing(self):
        # its last line had no line end, written once the stream had ended whole; its write failed, its client gone,
        # and the read after it, on the connection urllib had closed, read as a cut: bound 10 minutes (the 2nd review
        # of v1.44.0)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.E2.mode = "ndjson_unended_end"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 6.0)     # one here, none there: it goes there
        raw = json.dumps({"model": "qwen3.8-27b", "stream": True, "user": "conv-ollama-end-0",
                          "messages": self.turns("conv-ollama-end", 1)[0]["messages"]}).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.connect()
        c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        c.request("POST", "/api/chat", raw, {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        resp.read1(65536)                       # its first line
        resp.close()
        c.close()                               # gone with a reset, before its last line
        time.sleep(PAIR_GRACE_READ_S)
        self.assertIn("conv-ollama-end-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertIsNone(m.killer_box(m.body_fingerprint(raw, "/api/chat")),
                          "a stream that ended whole was bound when its last line could not be written")
        for th in held:
            th.join(30)

    def test_an_answer_that_comes_after_its_client_left_goes_to_nobody(self):
        # an answer not streamed whose body came while its client left, within the seconds the watch keeps the
        # connection: written to a client gone, it was said served, "ok pair" (the 2nd review of v1.44.0)
        if not self.INPROC:
            self.skipTest("the journal is read in this test's process")
        gone = self.journal("spark1").count("CLIENT GONE during pair")
        oks = self.journal("spark1").count("ok pair")
        self.E2.mode = "slow_body"              # its head at once, its body's second half 1.5 s on
        # a plain close: the first write after it goes through, and "ok pair" was said (a reset made it fail, the 3rd
        # review of v1.44.0)
        fp, held = self._left_at_once("conv-entry-left-late-200", reset=False)
        self.assertTrue(_until(lambda: "conv-entry-left-late-200-0" in self.E2.users, 5))
        self.assertTrue(_until(lambda: self.journal("spark1").count("CLIENT GONE during pair") > gone, 8),
                        "an answer that came after its client left was not said gone")
        self.assertEqual(self.journal("spark1").count("ok pair"), oks, "an answer written to nobody was said served")
        for th in held:
            th.join(30)

    def test_a_request_a_stop_marks_during_its_count_is_cut_never_held_or_handed_on(self):
        # the size guard's count under way when the cockpit's stop came, the count failed (its engine stopping): the
        # stop's mark lasted through the hold that followed, or the other box took the request over, a stop's request
        # served all the same (the 2nd review of v1.44.0)
        if not self.INPROC:
            self.skipTest("this box's pool and stop are in this test's process")
        self._stopped_in_its_count("conv-stopped-in-count", floor=False, fails=True)
        self.assertNotIn("conv-stopped-in-count", self.E2.users, "a request a stop marked reached the other box")
        self.assertTrue(self.until_in_journal("spark1", "stopped by the cockpit", within=3.0),
                        "a request a stop marked during its count was not cut as stopped")

    def test_a_death_the_other_box_answers_after_this_boxs_client_left_is_bound(self):
        # an answer not streamed, its client gone: the other box's engine died on it a second later, which that box
        # answered 502 within the seconds the watch keeps the connection; read after its client left, it bound nothing,
        # and its client's retry ran on this box's engine too (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        self.E2.mode = "die"
        fp, held = self._left_at_once("conv-entry-left-502", x_die_after=1.5)
        bound = self._bound_within(fp, 10)
        self.assertIn("conv-entry-left-502-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertEqual(bound, "spark2", "a death the other box answered after this box's client left was left unbound")
        for th in held:
            th.join(30)

    def test_a_stream_the_other_box_begins_after_this_boxs_client_left_is_read_for_a_death(self):
        # its client gone before the other box's stream began: the entry left at its head, unread, the death that
        # stream carried a second later (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        self.E2.mode = "vllm_dead_after_1s"
        fp, held = self._left_at_once("conv-entry-left-late", stream=True, x_delay=1.6)
        bound = self._bound_within(fp, 10)
        self.assertIn("conv-entry-left-late-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertEqual(bound, "spark2", "a death in a stream begun after this box's client left was left unbound")
        for th in held:
            th.join(30)

    def test_a_stream_whose_head_this_box_could_not_write_is_read_for_a_death(self):
        # its client reset before the watch saw it go: the stream's head failed to reach it, and the entry left there,
        # unread, the death the stream carried a second later (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        self.E2.mode = "vllm_dead_after_1s"
        fp, held = self._left_at_once("conv-entry-left-head", stream=True, after=0.05, x_delay=0.5)
        bound = self._bound_within(fp, 10)
        self.assertIn("conv-entry-left-head-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertEqual(bound, "spark2", "a death in a stream whose head failed to reach its client was left unbound")
        for th in held:
            th.join(30)

    def test_a_box_that_closes_on_a_request_after_its_client_left_is_bound(self):
        # its client gone, the other box closed the connection on the request with no answer a second later (its power,
        # a restart of its proxy): bound as when its client stays (decision 1017), its retry otherwise free to run on
        # this box's engine (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        late = self._the_other_box_is("close")
        fp, held = self._left_at_once("conv-entry-left-drop")
        _until(lambda: "conv-entry-left-drop-0" in late.got, 5)
        self.assertIn("conv-entry-left-drop-0", late.got, "the test needs the other box to have it")
        self.assertEqual(self._bound_within(fp, 10), "spark2",
                         "a box that closed on the request after its client left was left unbound")
        for th in held:
            th.join(30)

    def test_an_answer_cut_after_its_client_left_is_bound(self):
        # an answer not streamed whose head came, its client gone, then cut by the other box a second later: a death, as
        # when its client stays (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        late = self._the_other_box_is("cut")
        fp, held = self._left_at_once("conv-entry-left-cut")
        _until(lambda: "conv-entry-left-cut-0" in late.got, 5)
        self.assertIn("conv-entry-left-cut-0", late.got, "the test needs the other box to have it")
        self.assertEqual(self._bound_within(fp, 10), "spark2", "an answer cut after its client left was left unbound")
        for th in held:
            th.join(30)

    def _the_watch_ends_it(self, how):
        # the other box silent past the seconds the watch keeps the connection, then ended by the watch: that end is this
        # box's, no death (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        late = self._the_other_box_is(how)
        fp, held = self._left_at_once(f"conv-entry-left-{how}")
        time.sleep(PAIR_GRACE_READ_S + 2)
        self.assertIn(f"conv-entry-left-{how}-0", late.got, "the test needs the other box to have it")
        self.assertIsNone(self.mod.killer_box(fp), "this box ending the connection was read as a death")
        for th in held:
            th.join(30)

    def test_the_watchs_own_end_of_a_silent_box_binds_nothing(self):
        self._the_watch_ends_it("silent")

    def test_the_watchs_own_end_of_an_answer_gone_silent_binds_nothing(self):
        self._the_watch_ends_it("cut_silent")

    def test_a_death_the_other_box_sends_after_this_boxs_client_left_is_bound(self):
        # the other box's engine died on the request just after this box's client left, the death unrelayed: the watch
        # shut the connection at once, the death went unread, and the request unbound, its retry free to run on this
        # box's engine too (decision 1019; the 14th review of v1.43.0, decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.E2.mode = "vllm_dead_after_1s"
        fp, held = self._left_after_its_first_event("conv-entry-left-dead")
        deadline = time.monotonic() + 10
        while m.killer_box(fp) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("conv-entry-left-dead-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertEqual(m.killer_box(fp), "spark2", "a death sent after this box's client left was left unbound")
        for th in held:
            th.join(30)

    def test_a_stream_that_ends_after_this_boxs_client_left_binds_nothing(self):
        # read on after its client left (decision 1112), a stream that ends as streams do binds nothing
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.E2.mode = "ok"
        fp, held = self._left_after_its_first_event("conv-entry-left-ok")
        time.sleep(PAIR_GRACE_READ_S)
        self.assertIn("conv-entry-left-ok-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertIsNone(m.killer_box(fp), "a stream that ended as streams do was bound")
        for th in held:
            th.join(30)

    def test_this_boxs_own_end_of_the_connection_is_no_death_of_the_other_boxs_engine(self):
        # its client gone, seen first by the watch, which ends the connection once its grace is over: the read under way
        # then fails, which read as the other box's engine cutting its stream, and bound the request (decision 1112)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.E2.mode = "slow_10s"
        fp, held = self._left_after_its_first_event("conv-entry-left-slow")
        time.sleep(PAIR_GRACE_READ_S + 3)
        self.assertIn("conv-entry-left-slow-0", self.E2.users, "the test needs the other box's engine to have it")
        self.assertIsNone(m.killer_box(fp), "this box ending the connection was read as the other box's engine's death")
        for th in held:
            th.join(30)

    def test_a_cut_during_a_floor_stop_is_bound_and_no_stop_counts_it(self):
        # the cockpit's floor stop landed during the systemd read of an engine's cut: it counted the stream, cut by its
        # engine, as stopped, started the floor's episode for nothing, and the stream was left unbound, its retry free to
        # reach the other box's engine (decision 1019; the 11th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the stop is made in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        stops = []

        def a_stop_meanwhile():
            if not stops:
                stops.append(m.stop_generations("this test's memory floor", floor=True))
            return False
        m.unit_stopping_by_order = a_stop_meanwhile
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        floor = dict(m._FLOOR)                                                  # the floor's state, given back
        self.addCleanup(lambda: (m._FLOOR.clear(), m._FLOOR.update(floor)))
        self.E1.mode = "ndjson_cut"
        body = {"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "an ollama stream"}], "stream": True,
                "user": "conv-cut-floor-0"}
        raw = json.dumps(body).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/api/chat", raw, {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        try:
            while resp.read1(65536):
                pass
        except (OSError, http.client.HTTPException):
            pass                                                                # cut
        self.assertIn("conv-cut-floor-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertEqual(len(stops), 1, "the test needs its stop")
        self.assertEqual(stops[0][0], 0, "a stream its engine had cut was counted as stopped")
        self.assertEqual(m.killer_box(m.body_fingerprint(raw, "/api/chat")), "", "a cut during a stop was left unbound")

    def test_a_cut_under_an_order_binds_nothing_and_tells_the_hops_box(self):
        # the two ordered outcomes of an engine's cut with no death before it had no test (the 11th review of v1.43.0,
        # 2026-11-05): this box's own client's request stays unbound (decisions 1030 and 1041), and a request the other
        # box sent is said ended by order in its stream (decision 1031)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: True
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "cut"
        r = dict(self.turns("conv-cut-ordered", 1)[0], stream=True)
        try:
            self.call("/v1/chat/completions", r)
        except http.client.IncompleteRead:
            pass                                                                # cut, as its engine's end was
        self.assertIn("conv-cut-ordered-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertIsNone(m.killer_box(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions")),
                          "a cut under an order bound this box's own request")
        try:
            st, out, _ = self.tls_call(self.pport1, "/v1/chat/completions",
                                       dict(self.chat("qwen3.8-27b"), user="hop-cut-ordered", stream=True))
        except http.client.IncompleteRead as e:                                 # cut, its order said before
            out = e.partial
        self.assertIn(b'"x_alzeo_ended":"order"', out, out[-400:])

    def test_an_ndjson_cut_is_bound_before_its_client_sees_it(self):
        # the 9th review's fix reached SSE streams alone: an Ollama stream, or a non-streamed answer, was cut to its client
        # before its request was bound, and its retry went to the other box (decision 1019; the 10th review of v1.43.0,
        # 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        saved = m.unit_stopping_by_order
        reads = []

        def slow():
            reads.append(1)
            time.sleep(1.0)
            return False
        m.unit_stopping_by_order = slow
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "ndjson_cut"
        body = {"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "an ollama stream"}], "stream": True,
                "user": "conv-nd-cut-0"}
        raw = json.dumps(body).encode()
        fp = m.body_fingerprint(raw, "/api/chat")
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/api/chat", raw, {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        try:
            while resp.read1(65536):
                pass
        except (OSError, http.client.HTTPException):
            pass                                                                # cut: its client retries now
        self.assertIn("conv-nd-cut-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertEqual(m.killer_box(fp), "", "an Ollama stream's cut reached its client before the binding")
        self.assertEqual(len(reads), 1, "the cut read systemd more than once")

    def _ndjson_peer(self, marker, script):
        """The other box serving the 27B, whose /api/chat streams NDJSON as script(chunk, handler) writes it."""
        Base = fake_peer(["qwen3.8-27b", marker], PEER_ANSWER)

        class Peer(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def chunk(part):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                try:
                    script(chunk)
                except OSError:
                    return
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)                      # cut, not ended
        self.repoint(tls_peer(self, Peer))
        self.until_listed(marker)

    NDJSON_LINE = (b'{"model": "qwen3.8-27b", "message": {"role": "assistant", "content": "from the peer "}, '
                   b'"done": false}\n')
    NDJSON_DEATH = (b'{"error": {"message": "EngineCore encountered an issue. See stack trace (above) for the root '
                    b'cause."}}\n')

    def test_a_death_relayed_is_bound_as_it_is_relayed(self):
        # bound when the watch saw its client gone, a retry within that watch's second went unbound, free to take this
        # box's engine down too (the 8th review of v1.43.0, 2026-11-05): bound as the death is relayed, its client still
        # there
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        Base = fake_peer(["qwen3.8-27b", "marker-bound-at-death"], PEER_ANSWER)
        event = (b'data: {"id":"c","object":"chat.completion.chunk","model":"qwen3.8-27b","choices":[{"index":0,'
                 b'"delta":{"content":"from the peer "}}]}\n\n')

        class DeadThenQuiet(Base):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                Base.seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for part in (event, b"data: " + VLLM_DEAD_EVENT + b"\n\n"):
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                        self.wfile.flush()
                    time.sleep(4.0)                                             # quiet, its connection open
                except OSError:
                    return
        self.repoint(tls_peer(self, DeadThenQuiet))
        self.until_listed("marker-bound-at-death")
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 6.0)    # this box busy: a new one goes there
        raw = json.dumps(dict(self.turns("conv-bound-at-death", 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/v1/chat/completions", raw,
                  {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"from the peer", got, "served here: the test needs the other box's stream")
        fp = m.body_fingerprint(raw, "/v1/chat/completions")
        self.assertEqual(m.killer_box(fp), "spark2", "a death relayed was not bound while its client stayed")
        resp.close()
        for th in held:
            th.join(30)

    def test_an_order_line_split_where_its_client_leaves_is_read_whole(self):
        # the other box's NDJSON: a line and the first half of its order line in one read, the write of that line
        # failing (its client gone at the death): the half held back was not read with the rest, and the request stayed
        # bound 10 minutes (the 8th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the binding is read in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        order = HOP_ENDED_NDJSON

        def script(chunk):
            chunk(self.NDJSON_LINE)
            chunk(self.NDJSON_DEATH)
            time.sleep(1.0)                                                     # its client reads the death and leaves
            chunk(self.NDJSON_LINE + order[:10])
            time.sleep(0.3)
            chunk(order[10:])
            time.sleep(1.0)
        self._ndjson_peer("marker-nd-split", script)
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 6.0)    # this box busy: a new one goes there
        raw = json.dumps(dict(self.turns("conv-nd-split", 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        c.connect()
        c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # it leaves with a reset
        c.request("POST", "/api/chat", raw, {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        while b"EngineCore" not in got:
            b = resp.read1(65536)
            if not b:
                break
            got += b
        self.assertIn(b"from the peer", got, "served here: the test needs the other box's stream")
        resp.close()
        c.close()
        fp = m.body_fingerprint(raw, "/api/chat")
        # past the order's second half (1.3 s after the death) and its read: read before, a binding not made yet passed
        # for one undone (the 9th review of v1.43.0, 2026-11-05)
        time.sleep(2.5)
        self.assertIsNone(m.killer_box(fp), "an order line split where its client left was not read whole")
        for th in held:
            th.join(30)

    def test_the_line_end_before_an_order_line_stays_at_the_entry(self):
        # after a line over 64 KiB relayed unended, the other box puts a line end before its order line: relayed to the
        # client, it ended half a line as if whole (the 8th review of v1.43.0, 2026-11-05)
        if not self.INPROC:
            self.skipTest("the entry's relay is read in this test's process")
        long_line = b'{"model": "qwen3.8-27b", "message": {"role": "assistant", "content": "' + b"x" * 70000

        def script(chunk):
            chunk(long_line)                                                    # over 64 KiB, unended
            time.sleep(0.3)
            chunk(b"\n" + HOP_ENDED_NDJSON)
            time.sleep(0.5)
        self._ndjson_peer("marker-nd-sep", script)
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 6.0)    # this box busy: a new one goes there
        raw = json.dumps(dict(self.turns("conv-nd-sep", 1)[0], stream=True)).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port1, timeout=30)
        self.addCleanup(c.close)
        c.request("POST", "/api/chat", raw, {"Content-Type": "application/json", "Authorization": f"Bearer {KEY_1}"})
        resp = c.getresponse()
        got = b""
        try:
            while True:
                b = resp.read1(65536)
                if not b:
                    break
                got += b
        except (OSError, http.client.HTTPException):
            pass                                                                # cut, as the other box's was
        self.assertTrue(got.startswith(long_line), got[:120])
        self.assertNotIn(b"x_alzeo_ended", got)
        self.assertFalse(got.endswith(b"\n"), "the line end before the order line reached the client")
        for th in held:
            th.join(30)

    def test_a_stop_job_seen_after_a_vllm_death_is_said_before_the_streams_end(self):
        # vLLM ends a dead stream cleanly ([DONE]); its lane's stop job, not seen yet at the death, went unsaid, and the
        # other box bound the request 10 minutes (the 6th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = m.unit_stopping_by_order
        m.unit_stopping_by_order = lambda: self.E1.ordered_now                  # the stop job, seen after the death
        self.addCleanup(setattr, m, "unit_stopping_by_order", saved)
        self.E1.mode = "vllm_dead_late_order"
        st, out, _ = self.tls_call(self.pport1, "/v1/chat/completions",
                                   dict(self.chat("qwen3.8-27b"), user="hop-late-order", stream=True))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"EngineCore", out)
        self.assertIn(b'"x_alzeo_ended":"order"', out, out[-400:])
        self.assertLess(out.index(b"EngineCore"), out.index(b'"x_alzeo_ended":"order"'))

    def test_a_box_that_reads_no_key_holds_its_own_lane_from_the_same_checkpoint_too(self):
        # its engine away, the other box on the same checkpoint: refused at once ("cannot read its own key"), where the
        # other checkpoint went on to this box's own path (the 4th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the key is read in this test's process")
        m = self.mod
        saved = (m._door_key, m.served_models, m.unit_texts, m.unit_jobs, m._LAST_LANE["lane"])

        def restore():
            m._door_key, m.served_models, m.unit_texts, m.unit_jobs = saved[:4]
            m._LAST_LANE["lane"] = saved[4]
        self.addCleanup(restore)

        def away(*a, **k):
            raise m.EngineUnreachable("connection refused (the test's engine away)")
        m._door_key, m.served_models, m.unit_texts, m.unit_jobs = (lambda: None), away, (lambda: None), (lambda: [])
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), TEST_REV)         # its own lane, the other box's checkpoint
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertNotIn(b"cannot read its own key", out, "a request of its own lane was turned away")
        self.assertEqual(st, 200, out[:300])                                    # its own path; this test's engine answers
        self.assertIn(b"from spark1", out)

    def test_a_recovery_restart_to_a_staged_checkpoint_binds_what_its_engine_closed_on(self):
        # decision 1049: an update staged another checkpoint, and the cockpit restarts a wedged engine; 1041 took that
        # restart for the Load of the staged checkpoint, an order, and the request that wedged the engine went unbound
        # (the 4th review of v1.43.0, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = (m.unit_jobs, m.unit_start_texts, m.recovery_runs)

        def restore():
            m.unit_jobs, m.unit_start_texts, m.recovery_runs = saved
        self.addCleanup(restore)
        self.addCleanup(m._KILLERS.clear)
        m.unit_jobs = lambda: [("qwen38-sglang.service", "restart")]
        m.unit_start_texts = lambda unit: [f"docker run x --served-model-name qwen3.8-27b --revision {'ab' * 20}"]
        m.recovery_runs = lambda: {"qwen38-sglang.service": True}             # the cockpit's mark (decision 1049)
        r = self.turns("conv-recovery", 1)[0]
        self.E1.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertNotEqual(st, 200, out[:300])
        self.assertIn("conv-recovery-0", self.E1.users, "the test needs this box's engine to have it")
        self.assertEqual(m.killer_box(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions")), "",
                         "the cockpit's recovery was taken for an order, and bound nothing")

    def test_a_box_that_reads_no_key_holds_what_its_own_engine_will_serve(self):
        # its engine away, the other box on another checkpoint: the request goes on to this box's own path; the key's
        # refusal came first and turned it away (the delta's 3rd review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the key is read in this test's process")
        m = self.mod
        saved = (m._door_key, m.served_models, m.unit_texts, m.unit_jobs, m._LAST_LANE["lane"])

        def restore():
            m._door_key, m.served_models, m.unit_texts, m.unit_jobs = saved[:4]
            m._LAST_LANE["lane"] = saved[4]
        self.addCleanup(restore)

        def away(*a, **k):
            raise m.EngineUnreachable("connection refused (the test's engine away)")
        m._door_key, m.served_models, m.unit_texts, m.unit_jobs = (lambda: None), away, (lambda: None), (lambda: [])
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "ab" * 20)       # its own lane, another checkpoint
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertNotIn(b"cannot read its own key", out, "a request of its own lane was turned away")
        self.assertEqual(st, 200, out[:300])                                    # its own path; this test's engine answers
        self.assertIn(b"from spark1", out)

    def test_a_restart_binds_what_its_engine_closed_on(self):
        # decision 1030: only a stop is an order. The cockpit restarts an engine a request may have wedged (autoheal,
        # zombie): counted as an order, that request was bound to nothing and its retry went to the other box, where it
        # could wedge the other engine too (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = (m.unit_jobs, m.unit_start_texts)
        m.unit_jobs = lambda: [("qwen38-sglang.service", "restart")]          # the cockpit's restart of a wedged engine
        # to the checkpoint it serves: this proxy names no unit, and the comparison of decision 1041 was never reached
        # (the 4th review of v1.43.0, 2026-11-04)
        m.unit_start_texts = lambda unit: [f"docker run x --served-model-name qwen3.8-27b --revision {TEST_REV}"]
        self.addCleanup(lambda: (setattr(m, "unit_jobs", saved[0]), setattr(m, "unit_start_texts", saved[1])))
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-restart", 1)[0]
        self.E1.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertNotEqual(st, 200, out[:300])
        hop = dict(self.chat("qwen3.8-27b"), user="hop-restart")
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", hop)[:2]
        self.assertEqual(st, 502, out[:300])                                    # the 502 of an engine that closed on it
        self.E1.mode = "ok"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it would go there, unbound
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a request its engine closed on under a restart went to the other box")
        for th in held:
            th.join(30)

    def test_this_engine_starting_its_lane_goes_to_no_other_checkpoint(self):
        # decision 1018: an update restarts this proxy and the engine together, and the 27B loads for minutes. This
        # proxy, new, had never seen its engine answer, and sent the 27B's clients to the other box whatever its
        # checkpoint (review, 2026-11-04): the command of the engine starting names its model and checkpoint
        if not self.INPROC:
            self.skipTest("this box's engine is said away in this test's process")
        m = self.mod
        saved = (m.served_models, m.unit_texts, m._LAST_LANE["lane"])

        def restore():
            m.served_models, m.unit_texts = saved[:2]
            m._LAST_LANE["lane"] = saved[2]
        self.addCleanup(restore)

        def away(*a, **k):
            raise m.EngineUnreachable("connection refused (the test's engine starting)")
        m.served_models = away
        m._LAST_LANE["lane"] = (frozenset(), None)                             # a proxy that never saw it answer
        starting = ("/usr/bin/docker run --rm --name qwen38-sglang img python3 -m sglang.launch_server --model-path "
                    "/m --served-model-name qwen3.8-27b --revision {} --max-running-requests 8")
        m.unit_texts = lambda: [starting.format("ab" * 20)]                   # its engine loads another checkpoint
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])                                    # this box's own path (role 6's hold)
        self.assertIn(b"from spark1", out)
        self.assertEqual(self.E2.users, [], "a client of this box got the other checkpoint")
        m.unit_texts = lambda: [starting.format(TEST_REV)]                    # the same checkpoint there: it serves
        getattr(m, "forget_unit_reads", lambda: None)()                         # lane_away keeps its read a second
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out)
        m.unit_texts = lambda: [starting.replace("qwen3.8-27b", "qwen3.8-flash-next-fp4").format("ab" * 20)]
        getattr(m, "forget_unit_reads", lambda: None)()
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])                                    # its lane serves another model: there
        self.assertIn(b"from spark2", out)

    def test_another_lane_saying_no_checkpoint_is_noted_with_none(self):
        # a lane whose command names no revision (a custom model) was never noted: while its engine was away its model's
        # requests went to the other box whatever its checkpoint (the delta's 3rd review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the lane seen last is read in this test's process")
        m = self.mod
        saved = (m.unit_revision, m._LAST_LANE["lane"])

        def restore():
            m.unit_revision = saved[0]
            m._LAST_LANE["lane"] = saved[1]
        self.addCleanup(restore)
        here = frozenset(m.served_models())
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-flash-next-fp4"}), "ab" * 20)    # another lane before
        m.unit_revision = lambda: None
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(m._LAST_LANE["lane"], (here, None))

    def test_the_lane_seen_last_keeps_its_checkpoint_while_the_unit_says_none(self):
        # its unit with no process to read for a moment, the lane seen last was written with no checkpoint, and once its
        # engine was away every request of its model got "another checkpoint" (the delta's review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the lane seen last is read in this test's process")
        m = self.mod
        saved = (m.unit_revision, m._LAST_LANE["lane"])

        def restore():
            m.unit_revision = saved[0]
            m._LAST_LANE["lane"] = saved[1]
        self.addCleanup(restore)
        here = frozenset(m.served_models())                                     # the lane this box serves
        m._LAST_LANE["lane"] = (here, TEST_REV)
        m.unit_revision = lambda: None
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(m._LAST_LANE["lane"], (here, TEST_REV))

    def test_a_box_that_serves_no_text_lane_now_sends_the_others_model_there(self):
        # decision 1018 holds for a model this box's lane serves: a box switched to images (or stopped) kept the 27B it
        # served last as its own, and refused its clients the other box's 27B of another checkpoint (found re-reading
        # the rule, 2026-11-04). A crash on its way back keeps it
        if not self.INPROC:
            self.skipTest("systemd's answer is set in this test's process")
        m = self.mod
        saved = (m.served_models, m.unit_texts, m.unit_jobs, m.HOLD_UNITS, m._LAST_LANE["lane"],
                 m._UNIT_FACTS.get("states"))

        def restore():
            m.served_models, m.unit_texts, m.unit_jobs, m.HOLD_UNITS = saved[:4]
            m._LAST_LANE["lane"] = saved[4]
            m._UNIT_FACTS["states"] = saved[5]
        self.addCleanup(restore)
        m.unit_jobs = lambda: []

        def away(*a, **k):
            raise m.EngineUnreachable("connection refused (the test's text lane stopped)")
        m.served_models = away
        m.HOLD_UNITS = ("qwen38-sglang.service",)
        m.unit_texts = lambda: None                                             # no text unit active
        m._LAST_LANE["lane"] = (frozenset({"qwen3.8-27b"}), "ab" * 20)       # the 27B it served before, another one
        m._UNIT_FACTS["states"] = ("inactive",)                                 # images now, or no lane
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "a box that serves no text lane kept the other box's model from its client")
        m._UNIT_FACTS["states"] = ("deactivating",)                             # a crash, its restart not due yet
        getattr(m, "forget_unit_reads", lambda: None)()                         # lane_away keeps its read a second
        before = len(self.E2.users)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"))
        self.assertEqual(st, 200, out[:300])                                    # this box's own path (role 6's hold)
        self.assertIn(b"from spark1", out)
        self.assertEqual(len(self.E2.users), before, "a client of this box got the other checkpoint")

    def test_a_request_sent_on_for_another_checkpoint_than_this_boxs_is_refused(self):
        # decision 1018, the receiving side: the other box's row of this one, read minutes ago, said the checkpoint this
        # box served before a switch under the same name, and the hop said nothing this box could check (review,
        # 2026-11-04). The entry's checkpoint comes with the request now, and another one gets a 409
        hop = dict(self.chat("qwen3.8-27b"), user="hop-checkpoint")
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", hop,
                                headers={"X-Alzeo-Pair-Checkpoint": "ab" * 20})[:2]
        self.assertEqual(st, 409, out[:300])
        self.assertIn(b"model_not_served", out)
        self.assertIn(b"another checkpoint", out)
        self.assertNotIn("hop-checkpoint", self.E1.users, "this box served another checkpoint's request")
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", hop, headers={"X-Alzeo-Pair-Checkpoint": TEST_REV})[:2]
        self.assertEqual(st, 200, out[:300])
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", dict(hop, user="hop-no-checkpoint"))[:2]
        self.assertEqual(st, 200, out[:300])                                    # a hop that carries none: no check

    def test_an_ordered_stop_binds_no_request(self):
        # decision 1019 binds a request that may have killed an engine: a stop ordered to the lane's unit (a Load, an
        # update) closed every request in flight and bound each one for 10 minutes, and its hop got the 502 no box takes
        # back (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the unit's job is said in this test's process")
        m = self.mod
        saved = m.unit_jobs
        m.unit_jobs = lambda: [("qwen38-sglang.service", "stop")]             # a Load of another lane stops this one
        self.addCleanup(setattr, m, "unit_jobs", saved)
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-ordered", 1)[0]
        self.E1.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertNotEqual(st, 200, out[:300])
        hop = dict(self.chat("qwen3.8-27b"), user="hop-ordered")
        st, out = self.tls_call(self.pport1, "/v1/chat/completions", hop)[:2]
        self.assertEqual(st, 503, out[:300])                                    # not the 502 of an engine that died
        self.E1.mode = "ok"
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 5, 4.0)     # far apart: it goes there, unbound
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "a request an ordered stop closed was bound to this box")
        for th in held:
            th.join(30)

    def test_a_request_bound_to_a_box_whose_list_is_unknown_is_tried_there(self):
        # its list read failed (it restarted): the bound branch said "another checkpoint" to each retry for 10 minutes
        # (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("the kept list is set in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-unknown", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "spark2")
        with m._PAIR_LOCK:
            m._PAIR_MODELS["spark2"]["revision"] = None                        # read while it restarted
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark2", out, "a box whose list is unknown was said to serve another checkpoint")

    def test_a_request_bound_here_goes_nowhere_once_this_box_serves_another_model(self):
        # bound to this box, which then switched lanes: its new lane answered a request for the other model, which
        # docs/pair.md says never happens (review, 2026-11-04)
        if not self.INPROC:
            self.skipTest("this box's model is said in this test's process")
        m = self.mod
        self.addCleanup(m._KILLERS.clear)
        r = self.turns("conv-bound-here-other", 1)[0]
        m.killer_note(m.body_fingerprint(json.dumps(r).encode(), "/v1/chat/completions"), "")       # this box
        real = m.served_models
        m.served_models = lambda *a, **k: ["qwen3.8-flash-next-fp4"]           # this box serves another model now
        self.addCleanup(setattr, m, "served_models", real)
        st, out, _ = self.call("/v1/chat/completions", r)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"serves another model now", out)
        self.assertNotIn("conv-bound-here-other-0", self.E1.users + self.E2.users)

    def test_a_burst_of_new_conversations_is_shared(self):
        self.E1.delay = self.E2.delay = 1.0
        out = []
        threads = [threading.Thread(target=lambda i=i: out.append(self.call(
            "/v1/chat/completions", self.turns(f"burst-{i}", 1)[0])[1])) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        here, there = sum(b"from spark1" in o for o in out), sum(b"from spark2" in o for o in out)
        self.assertEqual(here + there, 8, out)
        self.assertGreaterEqual(min(here, there), 3, f"{here} here, {there} on spark2: the burst went to one box")

    def test_a_box_that_refuses_hands_its_share_back(self):
        self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 3.0)
        self.E2.mode = "busy"                   # spark2's engine answers 503: spark2 relays it
        self.addCleanup(time.sleep, 2.5)        # its pause (ENTRY_ENV) over before the next test
        before = self.sent()
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-d", 1)[0])
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "the entry answered the peer's 503 instead of serving it")
        self.assert_said("spark1", "spark2 does not answer for qwen3.8-27b: this box serves it")
        # served here, never "sent there" too (the tile counted both, review of 2026-11-03)
        self.assertEqual(self.moved(before, self.sent()), {"failover": 1})

    def test_a_request_the_other_box_drops_runs_nowhere_else(self):
        # decision 1017: the other box had it whole and closed the connection with no answer (an Abort all there, its
        # floor, a restart, its power). It was run here again: an abort there undone, and a request that took that box
        # down sent to this one (review, 2026-11-03)
        class Drops(http.server.BaseHTTPRequestHandler):
            """A box that lists the 27B and goes as a request arrives (its power, its cable)."""
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"object": "list", "data": [{"id": "qwen3.8-27b"}, {"id": "marker-drop"}],
                                  "x_alzeo_running": 0, "x_alzeo_running_yours": 0,
                                  "x_alzeo_revision": TEST_REV}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True

        entry = self.spare_proxy("spark1-drop", KEY_1, peers=(("spark2", tls_peer(self, Drops)),))
        self.until_listed("marker-drop", port=entry)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 3.0)     # one here, none there: the turn goes there
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-e", 1)[0], port=entry)
        self.assertEqual(st, 503, out[:300])
        self.assertIn(b"had this request and closed the connection", out)
        self.assertNotIn("conv-e-0", self.E1.users, "a request the other box had was run on this box's engine too")
        self.assert_said("spark1-drop", "spark2 closed the connection on a request of qwen3.8-27b: its client is told "
                                        "503, and spark2 gets nothing of a model both serve")
        # sent again, as a client does after a 5xx: that box or nowhere, though it is paused (decision 1019)
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-e", 1)[0], port=entry)
        self.assertEqual(st, 503, out[:300])
        self.assertNotIn("conv-e-0", self.E1.users, "sent again, the request reached this box's engine")

    def test_this_box_hands_a_request_its_engine_refuses_to_the_other(self):
        self.E1.mode = "busy"                   # spark1's engine answers 503 to generations: a lane stopping or starting
        before = self.sent()
        for stream in (False, True):
            with self.subTest(stream=stream):
                body = dict(self.turns(f"conv-g-{stream}", 1)[0], stream=stream)
                st, out, _ = self.call("/v1/chat/completions", body)
                self.assertEqual(st, 200, out[:300])
                self.assertIn(b"spark2", out, "the entry held or refused a request the other box serves too")
                if stream:
                    self.assertIn(b"data: [DONE]", out)
        self.assert_said("spark1", "this box's engine does not answer for qwen3.8-27b: trying spark2")
        # counted in delta: an earlier test's rescues never stand in for these two (review, 2026-11-03)
        self.assertEqual(self.moved(before, self.sent()), {"rescued": 2})

    def test_a_request_this_boxs_engine_died_on_goes_to_no_other_engine(self):
        # the request may be what took the engine down: sent on, it could take the other box's down too (security
        # review of v1.43.0, 2026-11-03). Decision 998 hands over a refused connection or a 502, 503 or 504 only
        self.E1.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-m", 1)[0])
        self.assertEqual(st, 503, out[:300])
        self.assertEqual(self.E2.users, [], "the request that the engine died on went to the other box's engine")

    def test_the_other_box_whose_engine_died_on_a_request_says_502_and_it_goes_no_further(self):
        self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 3.0)         # one here, none there: it goes there
        self.E2.mode = "die"
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-n", 1)[0])
        self.assertEqual(st, 502, out[:300])
        self.assertEqual([u for u in self.E1.users if u and not u.startswith("busy-")], [],
                         "the request that the other box's engine died on was served here after it")
        self.assert_said("spark2", "502 engine closed a hop")

    def test_a_request_asked_of_this_box_is_never_shared_nor_handed_over(self):
        # the cockpit's smoke test asks whether THIS box's engine answers (X-Alzeo-Here: 1)
        here = {"X-Alzeo-Here": "1"}
        self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 3.0)         # one here, none there
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), headers=here)
        self.assertIn(b"from spark1", out, "a request asked of this box was shared")
        self.E1.mode = "busy"                                               # this box's engine says 503
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), headers=here)
        self.assertEqual(st, 503, "a request asked of this box was handed to the other one")
        self.assertEqual(self.E2.users, [], self.E2.users)

    def test_a_box_that_does_not_say_what_it_runs_gets_nothing_shared(self):
        # a box of v1.42.0 lists no x_alzeo_running: its load is unknown, and its own clients may fill it
        Peer = fake_peer(["qwen3.8-27b", "marker-old"], PEER_ANSWER, running=None)
        entry = self.spare_proxy("spark1-old", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-old", port=entry)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertIn(b"from spark1", out, "a box that says nothing of its load was sent a request")
        self.assertEqual(Peer.seen, [])

    def test_the_tile_says_what_this_box_sent_the_other(self):
        self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 3.0)     # one here, none there
        before = self.sent()
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-f", 1)[0])
        self.assertIn(b"from spark2", out)
        sent = self.sent()
        self.assertEqual(set(sent), {"affinity", "balance", "failover", "too_long", "rescued"}, sent)
        self.assertEqual(self.moved(before, sent), {"balance": 1})

    def test_the_tile_still_says_what_was_shared_when_the_other_box_does_not_answer(self):
        # what this box served while the other box did not answer mattered most then, and the 503 said none of it
        # (review, 2026-11-03)
        self.busy(self.E1, self.port1, KEY_1, "local-only", 1, 3.0)         # one here, none there: it goes there
        before = self.sent()
        st, out, _ = self.call("/v1/chat/completions", self.turns("conv-q", 1)[0])
        self.assertIn(b"from spark2", out)
        self.repoint(localports.held(self))                                 # spark2 gone: nothing listens there
        st, out, _ = self.call("/proxy/pair/spark2/state", method="GET")
        self.assertEqual(st, 503, out[:300])
        self.assertEqual(self.moved(before, json.loads(out).get("sent") or {}), {"balance": 1}, out[:300])

    def test_a_box_that_said_529_gets_nothing_shared_for_a_while(self):
        # its cockpit hit the memory floor: 529 on /v1/messages, the floor's word in the Anthropic dialect
        Peer = fake_peer(["qwen3.8-27b", "marker-529"], (529, {"type": "error", "error": {
            "type": "overloaded_error", "message": "the memory floor"}}))
        entry = self.spare_proxy("spark1-529", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-529", port=entry)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 6.0)          # one here, none there
        msg = {"model": "qwen3.8-27b", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
        for _ in range(2):
            st, out, _ = self.call("/v1/messages", msg, port=entry)
            self.assertEqual(st, 200, out[:300])
            self.assertIn(b"from spark1", out, "the other box's 529 went to the client while this box idled")
        self.assertEqual(len(Peer.seen), 1, "the box that said 529 got the next request too")
        self.assert_said("spark1-529", "spark2 does not answer for qwen3.8-27b: this box serves it, and spark2 gets nothing")

    def test_a_prompt_too_long_for_the_other_box_is_served_here_and_the_next_ones_still_go_there(self):
        Peer = fake_peer(["qwen3.8-27b", "marker-long"], (400, {"error": {
            "type": "context_too_long", "code": "context_length_exceeded", "message": "too long for this lane"}}))
        entry = self.spare_proxy("spark1-long", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-long", port=entry)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 6.0)
        before = self.sent(entry)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "the other box's pool refused it and the client got that refusal")
        self.assert_said("spark1-long", "the prompt is too long for spark2's pool: this box serves it")
        # counted apart: a small pool says nothing of the other box's health (review, 2026-11-03)
        self.assertEqual(self.moved(before, self.sent(entry)), {"too_long": 1})
        Peer.state["post"] = PEER_ANSWER
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertIn(b"from the peer", out, "one prompt too long for it kept the other box out")
        self.assertEqual(self.moved(before, self.sent(entry)), {"too_long": 1, "balance": 1})

    def test_a_refusal_cut_on_the_way_is_still_served_here(self):
        # its status said it all: a 503 whose body was cut is a refusal, served here, not a 502 (review, 2026-11-03)
        Peer = fake_peer(["qwen3.8-27b", "marker-cut"], (503, {"error": {"type": "engine_unavailable", "message": "x" * 200}}))
        Peer.state["cut"] = True
        entry = self.spare_proxy("spark1-cut", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-cut", port=entry)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 4.0)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from spark1", out, "a cut refusal reached the client as a 502")

    def test_a_box_that_switched_is_not_sent_the_next_request(self):
        Peer = fake_peer(["qwen3.8-27b", "marker-moved"], (409, {"error": {
            "type": "model_not_served", "message": "this box (spark2) serves ornith-1.5-122b, not qwen3.8-27b"}}))
        entry = self.spare_proxy("spark1-moved", KEY_1, peers=(("spark2", tls_peer(self, Peer)),))
        self.until_listed("marker-moved", port=entry)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 6.0)
        for _ in range(3):
            st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
            self.assertEqual(st, 200, out[:300])
            self.assertIn(b"from spark1", out)
        self.assertEqual(len(Peer.seen), 1, "each request went to the box that switched, for another 409")

    def test_a_list_read_long_ago_still_shares(self):
        Peer = fake_peer(["qwen3.8-27b", "marker-pause"], PEER_ANSWER)
        entry = self.spare_proxy("spark1-pause", KEY_1, peers=(("spark2", tls_peer(self, Peer)),),
                                 extra={"PAIR_TTL_S": "1"})
        self.until_listed("marker-pause", port=entry)
        time.sleep(4)                       # after a pause: the list was read more than 3 reads' time ago
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 6.0)
        st, out, _ = self.call("/v1/chat/completions", self.chat("qwen3.8-27b"), port=entry)
        self.assertEqual(st, 200, out[:300])
        self.assertIn(b"from the peer", out, "a burst after a pause stayed here, before the list's read")

    def test_a_conversation_served_here_while_the_other_box_was_away_stays_here(self):
        Peer = fake_peer(["qwen3.8-27b", "marker-away"], PEER_ANSWER)
        entry = self.spare_proxy("spark1-away", KEY_1, peers=(("spark2", tls_peer(self, Peer)),),
                                 extra={"PAIR_TTL_S": "1"})
        self.until_listed("marker-away", port=entry)
        # turns of 3,000 bytes after 12,000: the first is 15 KB, the second 18 KB, its 16 KiB block the first's not
        a = self.turns("conv-h", 3, size=12000, turn=3000)
        self.busy(self.E1, entry, KEY_1, "local-only", 1, 25.0)         # one here the whole test, none there
        st, out, _ = self.call("/v1/chat/completions", a[0], port=entry)
        self.assertIn(b"from the peer", out, "a new conversation, to the box with none in flight")
        Peer.state["listed"] = None                                     # the other box goes (its list fails)
        self.until_listed("marker-away", port=entry, gone=True)
        st, out, _ = self.call("/v1/chat/completions", a[1], port=entry)
        self.assertIn(b"from spark1", out, "served here while the other box was away")
        Peer.state["listed"] = ["qwen3.8-27b", "marker-back"]           # back, its prefix cache empty
        self.until_listed("marker-back", port=entry)
        st, out, _ = self.call("/v1/chat/completions", a[2], port=entry)
        self.assertIn(b"from spark1", out, "the conversation went back to the box that lost its cache")

    def test_a_token_count_stays_here_and_moves_no_conversation(self):
        # Claude Code counts a conversation's tokens with its whole body: the count generates nothing and fills no
        # prefix cache, so it is counted here (the same model, the same tokenizer), however far apart the boxes are.
        # Its last check, a turn served here after the count, could not fail: the fingerprints start with the route
        # (review, 2026-11-03). Its engine here answering the count is what says it stayed
        a = self.turns("conv-k", 2)
        st, out, _ = self.call("/v1/chat/completions", a[0])
        self.assertIn(b"from spark1", out, "two idle boxes: the entry serves the first turn")
        held = self.busy(self.E1, self.port1, KEY_1, "local-only", 6, 3.0)     # 6 here, none there: far apart
        count = {"model": "qwen3.8-27b", "messages": a[1]["messages"]}
        st, out, _ = self.call("/v1/messages/count_tokens", count)
        self.assertEqual(st, 200, out[:300])
        self.assertNotIn("/v1/messages/count_tokens", [x[0] for x in self.E2.seen], "the count went to the other box")
        self.assertIn("/v1/messages/count_tokens", [x[0] for x in self.E1.seen], "this box's engine did not count it")
        for t in held:
            t.join(30)

    def test_a_box_tells_the_other_how_many_it_runs_and_how_many_are_its(self):
        said = lambda: {k: v for k, v in json.loads(self.tls_call(self.pport1, "/v1/models", method="GET")[1]).items()
                        if k.startswith("x_alzeo_running")}
        listed = json.loads(self.tls_call(self.pport1, "/v1/models", method="GET")[1])
        self.assertEqual(listed.get("x_alzeo_revision"), TEST_REV, "the checkpoint this box serves")
        self.assertEqual(said(), {"x_alzeo_running": 0, "x_alzeo_running_yours": 0})
        self.busy(self.E1, self.port1, KEY_1, "local-only", 2, 3.0)        # two of its own clients' generations
        # and one the other box sent (on the pair port, its certificate)
        hop = threading.Thread(target=self.tls_call, args=(self.pport1, "/v1/chat/completions",
                                                            dict(self.chat("qwen3.8-27b"), x_delay=3.0, user="hop-1")),
                               daemon=True)
        hop.start()
        self.addCleanup(hop.join, 30)
        deadline = time.monotonic() + 10
        while "hop-1" not in self.E1.users and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(said(), {"x_alzeo_running": 3, "x_alzeo_running_yours": 1})
        st, out, _ = self.call("/v1/models", method="GET")
        self.assertEqual(st, 200, out[:300])
        self.assertNotIn(b"x_alzeo_running", out, "a client of the box sees what the other box reads")


if __name__ == "__main__":
    unittest.main()
