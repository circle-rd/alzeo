#!/usr/bin/env python3
"""The cockpit follows the Kolibri lane on its own facts, as it does the image and the video lanes.

The lane runs vLLM 0.29.0, which has none of the routes the SGLang belts read (/v1/loads,
/server_info, /flush_cache, /abort_request: 404, measured 2026-10-14), refuses the canary's model
name (404 "The model `canary` does not exist."), and answers /health without generating anything.
The fixtures are that engine's own answers, captured on the reference box: its /metrics and its
/v1/models. The module is imported with its environment pointed at a throwaway box.
"""
import http.server
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]
METRICS = (HERE.parent / "fixtures" / "vllm-metrics-kolibri.txt").read_text()
MODELS = json.loads((HERE.parent / "fixtures" / "vllm-models-kolibri.json").read_text())
ENV_BEFORE = {}


def setUpModule():
    ENV_BEFORE.update(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)


class Engine(http.server.BaseHTTPRequestHandler):
    """vLLM as the cockpit meets it."""
    models = MODELS
    metrics = METRICS

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        out = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/metrics":
            self._send(200, Engine.metrics, "text/plain")
        elif path == "/v1/models":
            self._send(200, json.dumps(Engine.models))
        elif path == "/version":
            self._send(200, '{"version":"0.29.0"}')
        elif path == "/health":
            self._send(200, "")
        else:
            self._send(404, '{"detail":"Not Found"}')


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Path(tempfile.mkdtemp(prefix="cockpit-kolibri-"))
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        cls.engine.daemon_threads = True
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.box), COCKPIT_REPO_DIR=str(REPO),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0",
                          COCKPIT_ENGINE=f"http://127.0.0.1:{cls.engine.server_port}",
                          # never the box's own proxy: a stop goes there first since v1.40.3
                          COCKPIT_PROXY="http://127.0.0.1:1")
        (cls.box / "api-key").write_text("test-key\n")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_kolibri_under_test", DASH / "cockpit.py")
        cls.ck = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ck)

    @classmethod
    def tearDownClass(cls):
        cls.engine.shutdown()
        cls.engine.server_close()
        shutil.rmtree(cls.box, ignore_errors=True)

    def serving(self, state="ready"):
        with self.ck.LIFE_LOCK:
            self.ck.LIFE["states"] = {"qwen38-kolibri.service": state} if state else {}


class ItsMetrics(Base):
    def test_the_engines_own_page_gives_load_and_cache(self):
        f = self.ck.vllm_metrics_facts(METRICS)
        self.assertEqual((f["running"], f["waiting"], f["usage"]), (0, 0, 0.0))
        self.assertEqual(f["pool"], 1949253)

    def test_running_waiting_and_usage_are_read(self):
        text = (METRICS.replace('vllm:num_requests_running{engine="0",model_name="kolibri-1"} 0.0',
                                'vllm:num_requests_running{engine="0",model_name="kolibri-1"} 3.0')
                .replace('vllm:num_requests_waiting{engine="0",model_name="kolibri-1"} 0.0',
                         'vllm:num_requests_waiting{engine="0",model_name="kolibri-1"} 2.0')
                .replace('vllm:kv_cache_usage_perc{engine="0",model_name="kolibri-1"} 0.0',
                         'vllm:kv_cache_usage_perc{engine="0",model_name="kolibri-1"} 0.25'))
        f = self.ck.vllm_metrics_facts(text)
        self.assertEqual((f["running"], f["waiting"], f["usage"]), (3, 2, 0.25))

    def test_a_page_that_is_not_vllms_reads_as_nothing(self):
        self.assertEqual(self.ck.vllm_metrics_facts("sglang:num_running_reqs 4\n"),
                         {"running": 0, "waiting": 0, "usage": 0.0, "pool": 0, "spec_accepted": 0.0, "spec_drafts": 0.0,
                          "gen": None, "queries": None, "finished": None, "preempted": None})
        self.assertEqual(self.ck.vllm_metrics_facts('vllm:num_requests_running{engine="0"} NaNx\n')["running"], 0)
        # "1e" passes the gauge's pattern and not float(): the case the try is for ("NaNx" never
        # reached it, review of 2026-10-14)
        self.assertEqual(self.ck.vllm_metrics_facts('vllm:num_requests_running{engine="0"} 1e\n')["running"], 0)

    def test_the_load_takes_the_shape_the_page_reads(self):
        rows = self.ck.kolibri_load()
        self.assertEqual(rows, [{"dp_rank": 0, "num_reqs": 0, "num_waiting_reqs": 0, "num_tokens": 1949253,
                                 "num_used_tokens": 0, "num_pending_tokens": 1949253,
                                 "gen_tokens": 5075.0, "prefix_queries": 4551.0, "finished": 9.0,
                                 "preemptions": 0.0}])

    def test_the_two_counters_of_a_prompt_being_computed_are_read(self):
        # the tokens generated and the tokens of every prompt admitted (lc.prompt_progress, issue #61),
        # summed over the engine's labels, and None on a page that has neither
        f = self.ck.vllm_metrics_facts(METRICS)
        self.assertEqual((f["gen"], f["queries"]), (5075.0, 4551.0))
        two = ('vllm:generation_tokens_total{engine="0"} 3.0\nvllm:generation_tokens_total{engine="1"} 4.0\n'
               'vllm:prefix_cache_queries_total{engine="0"} 10.0\n')
        self.assertEqual((self.ck.vllm_metrics_facts(two)["gen"], self.ck.vllm_metrics_facts(two)["queries"]), (7.0, 10.0))
        # and the requests finished, every reason summed (an abort is a finish too), and preempted, which
        # say whether a jump of the prompts admitted is one request's
        self.assertEqual((f["finished"], f["preempted"]), (9.0, 0.0))
        ends = ('vllm:request_success_total{engine="0",finished_reason="stop"} 2.0\n'
                'vllm:request_success_total{engine="0",finished_reason="abort"} 3.0\n'
                'vllm:num_preemptions_total{engine="0"} 1.0\nvllm:num_preemptions_total{engine="1"} 4.0\n')
        self.assertEqual((self.ck.vllm_metrics_facts(ends)["finished"], self.ck.vllm_metrics_facts(ends)["preempted"]), (5.0, 5.0))

    def test_health_is_vllms_own(self):
        self.assertTrue(self.ck.kolibri_healthy())
        port = self.ck.ENGINE_BASE
        try:
            self.ck.ENGINE_BASE = "http://127.0.0.1:9"
            self.assertFalse(self.ck.kolibri_healthy())
        finally:
            self.ck.ENGINE_BASE = port


class ItsEnginePanel(Base):
    def test_what_it_serves_comes_from_the_engine(self):
        out = self.ck.kolibri_engine_info()
        info = out["info"]
        self.assertEqual(info["served_model_name"], "kolibri-1")
        self.assertEqual(info["context_length"], 262144)
        self.assertEqual(info["max_total_num_tokens"], 1949253)
        self.assertEqual(info["kv_cache_dtype"], "fp8")
        self.assertEqual(info["mem_fraction_static"], 0.8)
        self.assertEqual(info["version"], "vLLM 0.29.0")
        self.assertEqual((out["served_target"], info["quantization"]), ("kolibri", "fp8"))
        self.assertEqual(out["prompt_ceiling_tokens"], 0)

    def test_the_nvfp4_checkpoint_is_told_apart(self):
        Engine.models = json.loads(json.dumps(MODELS).replace("Aleph-Alpha/Kolibri-1", "Quantinine/Kolibri-1-NVFP4A16-FP8"))
        try:
            out = self.ck.kolibri_engine_info()
        finally:
            Engine.models = MODELS
        self.assertEqual((out["served_target"], out["info"]["quantization"]), ("kolibri-nvfp4", "nvfp4"))

    def test_the_fast_and_the_info_collectors_ask_it_while_it_serves(self):
        self.serving("ready")
        try:
            self.assertEqual(self.ck.collect_engine_info()["info"]["served_model_name"], "kolibri-1")
            fast = self.ck.collect_engine_fast()
            self.assertEqual(fast["load"][0]["num_tokens"], 1949253)
            self.assertTrue(fast["healthy"])
        finally:
            self.serving(None)


class ItsState(Base):
    U = "qwen38-kolibri.service"

    def setUp(self):
        self.ck.UNHEALTHY_TICKS.pop(self.U, None)
        self.ck.KOLIBRI_READY_ENTER.pop(self.U, None)
        self.ck.run = lambda argv, **kw: "abc\n" if argv[:2] == ["docker", "ps"] else ""

    def state(self, healthy, enter="100", prev="ready", log=()):
        self.ck.kolibri_healthy = lambda: healthy
        self.ck._kolibri_log = lambda: list(log)
        st, boot, running = self.ck.kolibri_engine_state(self.U, active="active", sub="running",
                                                         prev_state=prev, enter_key=enter)
        return st["state"], boot, running

    def test_one_missed_probe_keeps_it_ready_three_make_it_degraded(self):
        self.assertEqual(self.state(True)[0], "ready")
        self.assertEqual(self.state(False)[0], "ready")
        self.assertEqual(self.state(False)[0], "ready")
        self.assertEqual(self.state(False)[0], "degraded")

    def test_a_restart_is_a_new_life_at_once(self):
        self.assertEqual(self.state(True, enter="100")[0], "ready")
        self.assertEqual(self.state(False, enter="200", prev="ready")[0], "starting")

    def test_a_boot_is_read_from_its_own_log(self):
        log = (HERE.parent / "fixtures" / "kolibri-boot.log").read_text().splitlines()[:40]
        st, boot, running = self.state(False, prev="starting", log=log)
        self.assertEqual(st, "loading-weights")
        self.assertIn("shard", boot["detail"])
        self.assertTrue(running)

    def test_a_degraded_reading_before_it_ever_served_is_its_warmup(self):
        """Fired up in its log, not answering yet: the first probe after the API is up."""
        log = (HERE.parent / "fixtures" / "kolibri-boot.log").read_text().splitlines()
        self.assertEqual(self.state(False, prev="capturing-graphs", log=log)[0], "warming-up")

    def test_no_container_is_not_running(self):
        self.ck.run = lambda argv, **kw: ""
        self.assertFalse(self.state(False, prev="stopped")[2])


class WhatItDoesNotHave(Base):
    def test_the_canary_stands_down_and_says_why(self):
        self.serving("ready")
        try:
            out = self.ck.collect_canary()
        finally:
            self.serving(None)
        self.assertTrue(out["skipped"])
        self.assertIn("the Kolibri-1 lane is checked by vLLM's own /health", out["why"])

    def test_a_flush_says_it_has_none(self):
        self.serving("ready")
        try:
            ok, lines, data = self.ck.job_flush_cache(None)
        finally:
            self.serving(None)
        self.assertFalse(ok)
        self.assertEqual(data["reason"], "unsupported")
        self.assertIn("the Kolibri-1 lane's vLLM has no cache flush route", lines[0])

    def test_abort_all_says_it_has_none(self):
        self.serving("loading-weights")
        try:
            with self.assertRaises(RuntimeError) as e:
                self.ck.engine_abort_all()
        finally:
            self.serving(None)
        self.assertIn("the Kolibri-1 lane's vLLM has no route that aborts", str(e.exception))

    def test_the_memory_floor_never_claims_an_abort_it_could_not_make(self):
        # the belt counted an abort before trying it, and on this lane, which has no abort route,
        # the page said "every generation was aborted" while nothing stopped (review, 2026-10-14)
        ck, saved = self.ck, {}
        for name in ("DRY_RUN", "kolibri_load", "mem_available_gib", "add_event", "audit"):
            saved[name] = getattr(ck, name)
        events = []
        ck.DRY_RUN = False
        ck.kolibri_load = lambda timeout=3: [{"num_reqs": 2, "num_waiting_reqs": 0}]
        ck.mem_available_gib = lambda: 0.5
        ck.add_event = lambda kind, text: events.append((kind, text))
        ck.audit = lambda rec: None
        ck.MEM_FLOOR.update(last_abort=None, aborts=0, last_try=None)
        self.serving("ready")
        try:
            ck.collect_engine_fast()
            first = dict(ck.MEM_FLOOR)
            ck.collect_engine_fast()                 # within the cooldown: not tried again
        finally:
            self.serving(None)
            for name, value in saved.items():
                setattr(ck, name, value)
        self.assertEqual((first["aborts"], first["last_abort"]), (0, None))
        self.assertIsNotNone(first["last_try"])
        tried = [e for e in events if e[0] == "mem_floor"]
        self.assertEqual(len(tried), 1, events)
        self.assertIn("abort failed", tried[0][1])
        self.assertIn("no route that aborts", tried[0][1])

    def test_system_one_points_at_the_qwen_lanes(self):
        self.serving("ready")
        try:
            out = self.ck.systemone_available()
        finally:
            self.serving(None)
        self.assertFalse(out["available"])
        self.assertIn("the Kolibri-1 lane is serving", out["reason"])

    def test_its_served_name_is_its_own(self):
        with self.ck.STATE_LOCK:
            self.ck.STATE.pop("engine_info", None)
        self.serving("ready")
        try:
            self.assertEqual(self.ck.served_model_name(), "kolibri-1")
        finally:
            self.serving(None)


class BeforeTheLifecycleSaysItServes(Base):
    """A cockpit started while Kolibri served read the engine SGLang's way until its lifecycle's
    first reading: the engine panel showed the 404 for 30 s, the diagnostics bundle held only that
    404, and the route table kept SGLang's deprecated routes until a connection failed (found in
    review, 2026-10-14). The engine's own answer says it is vLLM."""

    def setUp(self):
        self.serving(None)
        self.ck.ENGINE_ROUTES.update(self.ck.CURRENT_ROUTES)

    def test_the_info_collector_reads_vllms_facts(self):
        out = self.ck.collect_engine_info()
        self.assertEqual(out.get("info", {}).get("served_model_name"), "kolibri-1", out)
        self.assertEqual(self.ck.ENGINE_ROUTES["info"], self.ck.CURRENT_ROUTES["info"])

    def test_the_fast_collector_reads_its_load(self):
        fast = self.ck.collect_engine_fast()
        self.assertEqual((fast.get("load") or [{}])[0].get("num_tokens"), 1949253, fast)
        self.assertEqual(self.ck.ENGINE_ROUTES["load"], self.ck.CURRENT_ROUTES["load"])

    def test_the_diagnostics_bundle_holds_its_facts(self):
        import tarfile
        home = Path(tempfile.mkdtemp(prefix="kol-bundle-home-"))
        saved_home, saved_run = os.environ.get("HOME"), self.ck.run
        os.environ["HOME"] = str(home)
        self.ck.run = lambda argv, timeout=5.0, merge_err=False: ""    # no journal of this box
        try:
            job = self.ck.Job("diag_bundle", None, 60, fn=self.ck.job_diag_bundle)
            ok, lines, result = self.ck.job_diag_bundle(job)
        finally:
            self.ck.run = saved_run
            os.environ["HOME"] = saved_home
        self.assertTrue(ok, lines)
        with tarfile.open(result["path"], "r:gz") as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("server-info.json"))
            info = json.loads(tar.extractfile(member).read())
        shutil.rmtree(home, ignore_errors=True)
        self.assertEqual(info.get("info", {}).get("served_model_name"), "kolibri-1", info)


class TheServerRefusesWhatItsVllmHasNoRouteFor(Base):
    """The page refused them with why; another caller of the action API got the engine's 404,
    or the fit tool's "no engine to read" (found in review, 2026-10-14)."""

    def act(self, name):
        # an action already running: what passes this gate meets "busy", and nothing starts
        assert self.ck.JOB_LOCK.acquire(blocking=False)
        try:
            return self.ck.start_action(name, {})
        finally:
            self.ck.JOB_LOCK.release()

    def test_flush_abort_and_fit_are_refused_with_why(self):
        self.serving("ready")
        try:
            out = {name: self.act(name) for name in ("flush_cache", "abort_all", "fit_opencode")}
        finally:
            self.serving(None)
        for name, why in (("flush_cache", "no cache flush route"), ("abort_all", "no route that aborts"),
                          ("fit_opencode", "nothing to fit")):
            code, body = out[name]
            self.assertEqual((code, body.get("error")), (409, "blocked"), name)
            self.assertIn(why, " ".join(body.get("reasons") or []), name)
            self.assertTrue(" ".join(body.get("reasons") or []).startswith("The Kolibri-1 lane"), name)

    def test_on_a_qwen_lane_the_gate_lets_them_through(self):
        code, body = self.act("flush_cache")
        self.assertEqual((code, body.get("error")), (409, "busy"))

class ItsUnit(Base):
    def test_the_target_is_read_from_the_unit(self):
        unit = self.box / "qwen38-kolibri.service"
        saved = self.ck.KOLIBRI_UNIT_PATH
        self.ck.KOLIBRI_UNIT_PATH = unit
        try:
            unit.write_text("ExecStart=... Aleph-Alpha/Kolibri-1 --revision " + "a" * 40 + " \\\n")
            self.assertEqual(self.ck.unit_target("qwen38-kolibri.service"), {"model": "Aleph-Alpha/Kolibri-1", "target": "kolibri"})
            unit.write_text("ExecStart=... Quantinine/Kolibri-1-NVFP4A16-FP8 --revision " + "b" * 40 + " \\\n")
            self.assertEqual(self.ck.unit_target("qwen38-kolibri.service")["target"], "kolibri-nvfp4")
            unit.write_text("ExecStart=nothing\n")
            self.assertEqual(self.ck.unit_target("qwen38-kolibri.service"), {"model": None, "target": None})
            unit.unlink()
            self.assertEqual(self.ck.unit_target("qwen38-kolibri.service"), {"model": None, "target": None})
        finally:
            self.ck.KOLIBRI_UNIT_PATH = saved

    def test_it_is_a_switch_target_and_a_unit_the_page_may_drive(self):
        self.assertIn("qwen38-kolibri.service", self.ck.SERVING_UNITS)
        self.assertEqual(self.ck.ACTIONS["switch"]["params"]["target"][-5:-3], ["kolibri", "kolibri-nvfp4"])
        self.assertIn("qwen38-kolibri", self.ck.CONTAINERS)
        self.assertNotIn("qwen38-kolibri", self.ck.SGL_CONTAINERS)
        self.assertIn("qwen38-kolibri.service", self.ck.JOURNAL_UNITS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
