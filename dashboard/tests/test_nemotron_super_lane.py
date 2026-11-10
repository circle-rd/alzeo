#!/usr/bin/env python3
"""The cockpit follows the Nemotron Super lane on its own facts, as it does the Nemotron lane.

The two lanes run one vLLM image (Bolt's nightly 77154ca6 with b12x), so the same routes, the same
/metrics and the same boot lines; each has its own unit, its own container and its own model. The
fixtures are the Super VL engine's own answers, captured on the reference box on 2026-10-16 with
the lane's settings (four requests at a time, the 1M window, one request running): its /metrics,
its /v1/models and the log of one boot. The module is imported with its environment pointed at a
throwaway box.
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
METRICS = (HERE.parent / "fixtures" / "vllm-metrics-nemotron-super.txt").read_text()
MODELS = json.loads((HERE.parent / "fixtures" / "vllm-models-nemotron-super.json").read_text())
BOOT = (HERE.parent / "fixtures" / "nemotron-super-boot.log").read_text().splitlines()
POOL = 1382265
U = "qwen38-nemotron-super.service"
BOLT = "qwen38-nemotron.service"
ENV_BEFORE = {}


def setUpModule():
    ENV_BEFORE.update(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)


class Engine(http.server.BaseHTTPRequestHandler):
    """vLLM as the cockpit meets it on the Nemotron Super lane."""

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
            self._send(200, METRICS, "text/plain")
        elif path == "/v1/models":
            self._send(200, json.dumps(MODELS))
        elif path == "/version":
            self._send(200, '{"version":"0.32.0rc1.dev100+g77154ca61"}')
        elif path == "/health":
            self._send(200, "")
        else:
            self._send(404, '{"detail":"Not Found"}')


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Path(tempfile.mkdtemp(prefix="cockpit-nemotron-super-"))
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
        spec = importlib.util.spec_from_file_location("cockpit_nemotron_super_under_test", DASH / "cockpit.py")
        cls.ck = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ck)
        # the two units the box would have: each lane's draft length is read from its own
        cls.unit = cls.box / U
        cls.unit.write_text("ExecStart=... nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4 --revision " + "8" * 40
                            + " \\\n    --speculative-config.num_speculative_tokens 7 \\\n")
        cls.bolt = cls.box / BOLT
        cls.bolt.write_text("ExecStart=... nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB --revision " + "c" * 40
                            + " \\\n    --speculative-config.num_speculative_tokens 5 \\\n")
        cls.ck.NEMOTRON_SUPER_UNIT_PATH = cls.unit
        cls.ck.NEMOTRON_UNIT_PATH = cls.bolt

    @classmethod
    def tearDownClass(cls):
        cls.engine.shutdown()
        cls.engine.server_close()
        shutil.rmtree(cls.box, ignore_errors=True)

    def serving(self, state="ready", unit=U):
        with self.ck.LIFE_LOCK:
            self.ck.LIFE["states"] = {unit: state} if state else {}


class ItsEnginePanel(Base):
    def test_what_it_serves_comes_from_the_engine_and_its_own_unit(self):
        out = self.ck.nemotron_engine_info()
        info = out["info"]
        self.assertEqual(info["served_model_name"], "nemotron-3.5-super-vl")
        self.assertEqual(info["model_path"], "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4")
        self.assertEqual(info["context_length"], 1048576)
        self.assertEqual(info["max_total_num_tokens"], POOL)
        self.assertEqual(info["kv_cache_dtype"], "fp8_e4m3")
        self.assertEqual(info["mem_fraction_static"], 0.75)
        self.assertEqual((out["served_target"], info["quantization"]), ("nemotron-super", "nvfp4"))
        # its draft length is its unit's, not Bolt's (5 there, 7 here)
        self.assertEqual((info["speculative_algorithm"], info["speculative_num_draft_tokens"]), ("LiLiCorr (DFlash)", 7))

    def test_a_vllm_engine_is_told_apart_by_the_name_it_serves(self):
        self.assertEqual(self.ck.vllm_engine_info()["served_target"], "nemotron-super")

    def test_a_vllm_engine_is_asked_its_models_once(self):
        # vllm_engine_info read /v1/models to choose the lane, and the lane's panel read it again: two
        # requests to the engine, and two waits when it is slow (found in review, 2026-10-16)
        real, asked = self.ck.http_json, []
        def counting(url, timeout=10):
            asked.append(url)
            return real(url, timeout=timeout)
        self.ck.http_json = counting
        try:
            self.assertEqual(self.ck.vllm_engine_info()["served_target"], "nemotron-super")
        finally:
            self.ck.http_json = real
        self.assertEqual(sum(u.endswith("/v1/models") for u in asked), 1, asked)

    def test_a_name_of_neither_nemotron_lane_is_neither_lanes(self):
        # the lane comes from the one table of the two (NEMOTRON_LANES), by the exact name the engine
        # serves: a name starting "nemotron-" was taken for Bolt's, its draft length read from Bolt's
        # unit (found in review, 2026-10-16)
        real = self.ck.http_json
        self.ck.http_json = lambda url, timeout=10: (
            {"data": [{"id": "nemotron-4-other", "root": "nvidia/other", "max_model_len": 4096}]}
            if url.endswith("/v1/models") else real(url, timeout=timeout))
        try:
            out = self.ck.nemotron_engine_info()
        finally:
            self.ck.http_json = real
        self.assertIsNone(out["served_target"])
        self.assertIsNone(out["info"]["quantization"])
        self.assertIsNone(out["info"]["speculative_num_draft_tokens"])
        self.assertEqual(self.ck.nemotron_unit_serving([{"id": "nemotron-3.5-bolt"}]), "qwen38-nemotron.service")
        self.assertIsNone(self.ck.nemotron_unit_serving([{"id": "nemotron-3.5"}]))

    def test_the_collectors_ask_it_while_it_serves(self):
        self.serving("ready")
        try:
            self.assertTrue(self.ck.nemotron_serving())
            self.assertEqual(self.ck.collect_engine_info()["info"]["served_model_name"], "nemotron-3.5-super-vl")
            fast = self.ck.collect_engine_fast()
            self.assertEqual(fast["load"][0]["num_tokens"], POOL)
            self.assertIn("accept_len", fast["load"][0])
        finally:
            self.serving(None)


class ItsState(Base):
    def setUp(self):
        self.ck.UNHEALTHY_TICKS.pop(U, None)
        self.ck.NEMOTRON_READY_ENTER.pop(U, None)
        self.saved = (self.ck.run, self.ck.kolibri_healthy, self.ck._nemotron_log)
        self.asked = []
        self.ck.run = lambda argv, **kw: "abc\n" if argv[:2] == ["docker", "ps"] and "name=^qwen38-nemotron-super$" in argv else ""

    def tearDown(self):
        self.ck.run, self.ck.kolibri_healthy, self.ck._nemotron_log = self.saved

    def state(self, healthy, enter="100", prev="ready", log=()):
        self.ck.kolibri_healthy = lambda: healthy
        self.ck._nemotron_log = lambda container: (self.asked.append(container), list(log))[1]
        st, boot, running = self.ck.nemotron_engine_state(U, active="active", sub="running",
                                                          prev_state=prev, enter_key=enter)
        return st["state"], boot, running

    def test_a_boot_is_read_from_its_own_containers_log(self):
        upto = next(i for i, ln in enumerate(BOOT) if "Loading weights took" in ln)
        st, boot, running = self.state(False, prev="starting", log=BOOT[:upto])
        self.assertEqual(st, "loading-weights")
        self.assertIn("shard", boot["detail"])
        self.assertTrue(running)
        self.assertEqual(self.asked, ["qwen38-nemotron-super"])

    def test_its_drafters_load_comes_after_the_models_and_never_goes_back(self):
        start = next(i for i, ln in enumerate(BOOT) if "Initializing a V1 LLM engine" in ln)
        stages = list(self.ck.lc.NEMOTRON_STAGES)
        order = [stages.index(self.ck.lc.parse_nemotron_boot_log(BOOT[:k])["stage"]) for k in range(start + 1, len(BOOT) + 1)]
        self.assertEqual(order, sorted(order))
        model_done = next(i for i, ln in enumerate(BOOT) if "Loading weights took" in ln)
        drafter = [self.ck.lc.parse_nemotron_boot_log(BOOT[:k + 1])["detail"] for k in range(model_done + 1, len(BOOT))
                   if "Loading safetensors checkpoint shards" in BOOT[k]]
        self.assertTrue(drafter and all(d.startswith("the drafter") for d in drafter), drafter)

    def test_its_whole_boot_reads_as_its_warmup_until_it_answers(self):
        self.assertEqual(self.state(False, prev="capturing-graphs", log=BOOT)[0], "warming-up")

    def test_the_bolt_lanes_container_is_not_its(self):
        self.ck.run = lambda argv, **kw: "abc\n" if argv[:2] == ["docker", "ps"] and "name=^qwen38-nemotron$" in argv else ""
        self.assertFalse(self.state(False, prev="stopped")[2], "the Bolt container is not this lane's")


class WhatItDoesNotHave(Base):
    def test_the_canary_stands_down_and_says_why(self):
        self.serving("ready")
        try:
            out = self.ck.collect_canary()
        finally:
            self.serving(None)
        self.assertTrue(out["skipped"])
        self.assertIn("the Nemotron 3.5 Super VL lane is checked by vLLM's own /health", out["why"])

    def test_flush_abort_and_fit_are_refused_by_the_server_with_why(self):
        def act(name):
            assert self.ck.JOB_LOCK.acquire(blocking=False)
            try:
                return self.ck.start_action(name, {})
            finally:
                self.ck.JOB_LOCK.release()
        self.serving("ready")
        try:
            out = {name: act(name) for name in ("flush_cache", "abort_all", "fit_opencode")}
        finally:
            self.serving(None)
        for name in out:
            code, body = out[name]
            self.assertEqual((code, body.get("error")), (409, "blocked"), name)
            # in its own name: one sentence said "the Nemotron lane" for Bolt and for it (found 2026-11-10)
            self.assertTrue(" ".join(body.get("reasons") or []).startswith("The Nemotron 3.5 Super VL lane"), name)

    def test_a_flush_and_an_abort_say_it_has_none_in_its_name(self):
        self.serving("ready")
        try:
            ok, lines, data = self.ck.job_flush_cache(None)
            with self.assertRaises(RuntimeError) as e:
                self.ck.engine_abort_all()
        finally:
            self.serving(None)
        self.assertEqual((ok, data["reason"]), (False, "unsupported"))
        self.assertIn("the Nemotron 3.5 Super VL lane's vLLM has no cache flush route", lines[0])
        self.assertIn("the Nemotron 3.5 Super VL lane's vLLM has no route that aborts", str(e.exception))

    def test_system_one_points_at_the_qwen_lanes(self):
        self.serving("ready")
        try:
            out = self.ck.systemone_available()
        finally:
            self.serving(None)
        self.assertFalse(out["available"])
        self.assertIn("the Nemotron 3.5 Super VL lane is serving", out["reason"])

    def test_its_served_name_is_its_own(self):
        with self.ck.STATE_LOCK:
            self.ck.STATE.pop("engine_info", None)
        for unit, name in ((U, "nemotron-3.5-super-vl"), (BOLT, "nemotron-3.5-bolt")):
            self.serving("ready", unit)
            try:
                self.assertEqual(self.ck.served_model_name(), name)
            finally:
                self.serving(None)


class ItsUnit(Base):
    def test_the_target_is_read_from_its_unit(self):
        self.assertEqual(self.ck.unit_target(U), {"model": "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4",
                                                  "target": "nemotron-super"})
        self.assertEqual(self.ck.unit_target(BOLT)["target"], "nemotron")

    def test_it_is_a_switch_target_a_unit_the_page_may_drive_and_a_lane_it_prepares(self):
        self.assertIn(U, self.ck.SERVING_UNITS)
        self.assertEqual(self.ck.target_lane("nemotron-super"), "nemotron-super")
        self.assertEqual(self.ck.target_lane("nemotron"), "nemotron")
        self.assertEqual(self.ck.target_unit("nemotron-super"), self.unit)
        self.assertEqual(self.ck.TARGET_PIN["nemotron-super"], "NEMOTRON_SUPER_REV")
        self.assertEqual(self.ck.TARGET_DRAFT_PIN["nemotron-super"], "NEMOTRON_SUPER_DRAFT_REV")
        for pin in ("NEMOTRON_SUPER_REV", "NEMOTRON_SUPER_DRAFT_REV"):
            self.assertIn(pin, self.ck.rg.PIN_MODELS)
        self.assertEqual(self.ck.ACTIONS["prepare"]["argv"]({"target": "nemotron-super"})[-2:],
                         [str(REPO / "prepare-lane.sh"), "nemotron-super"])
        self.assertIn("qwen38-nemotron-super", self.ck.CONTAINERS)
        self.assertNotIn("qwen38-nemotron-super", self.ck.SGL_CONTAINERS)
        self.assertIn(U, self.ck.JOURNAL_UNITS)
        self.assertEqual(self.ck.UNIT2CONT[U], "qwen38-nemotron-super")
        self.assertEqual(self.ck.UNIT_OF_LANE["nemotron-super"], U)
        self.assertIn(U, self.ck.lc.ENGINE_UNITS)
        self.assertNotIn(U, self.ck.lc.TEXT_UNITS)
        self.assertEqual(self.ck.lc.LANE_PROVIDER[U], "nemotron-super")

    def test_it_is_prepared_once_the_bolt_lanes_image_its_unit_names_is_built(self):
        unit = self.box / "prepared.service"
        unit.write_text("ExecStart=/bin/bash -c 'exec /usr/bin/docker run qwen38-nemotron:vllm77154ca6-b12x1.5.1-1ee85ca80e0f \\\n")
        saved_path, saved_run = self.ck.NEMOTRON_SUPER_UNIT_PATH, self.ck.run
        asked = []
        self.ck.NEMOTRON_SUPER_UNIT_PATH = unit
        try:
            self.ck.run = lambda argv, **kw: (asked.append(argv), "sha256:abc\n")[1]
            self.assertTrue(self.ck.lane_prepared("nemotron-super"))
            self.assertIn("qwen38-nemotron:vllm77154ca6-b12x1.5.1-1ee85ca80e0f", asked[-1])
            self.ck.run = lambda argv, **kw: ""
            self.assertFalse(self.ck.lane_prepared("nemotron-super"))
            self.ck.NEMOTRON_SUPER_UNIT_PATH = self.box / "absent.service"
            self.assertIsNone(self.ck.lane_prepared("nemotron-super"))
        finally:
            self.ck.NEMOTRON_SUPER_UNIT_PATH, self.ck.run = saved_path, saved_run


class ItStartsOnlyWithItsDrafterWhole(Base):
    """A Start with no switch loads the checkpoint and its drafter from the cache, offline: the lane
    is startable only with the two whole (found on the reference box, 2026-10-16)."""

    def test_the_drafter_is_counted(self):
        hub = self.box / "hf" / "hub"
        unit = self.box / "startable.service"
        unit.write_text(f"ExecStart=/bin/bash -c 'exec /usr/bin/docker run -v {self.box}/hf:/root/.cache/huggingface "
                        "qwen38-nemotron:t nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4 --revision " + "8" * 40
                        + " --speculative-config.model nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4-LiLiCorr"
                        " --speculative-config.revision " + "7" * 40 + " --port 30000'\n")

        def snap(repo, rev):
            d = hub / ("models--" + repo.replace("/", "--")) / "snapshots" / rev
            d.mkdir(parents=True, exist_ok=True)
            (d / "config.json").write_text("{}")
            (d / "model.safetensors").write_text("w")
        saved_path, saved_run = self.ck.NEMOTRON_SUPER_UNIT_PATH, self.ck.run
        self.ck.NEMOTRON_SUPER_UNIT_PATH = unit
        self.ck.run = lambda argv, **kw: "sha256:abc\n"
        try:
            snap("nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4", "8" * 40)
            self.assertFalse(self.ck.lane_startable("nemotron-super"), "its drafter is missing")
            snap("nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4-LiLiCorr", "7" * 40)
            self.assertTrue(self.ck.lane_startable("nemotron-super"))
        finally:
            self.ck.NEMOTRON_SUPER_UNIT_PATH, self.ck.run = saved_path, saved_run


if __name__ == "__main__":
    unittest.main()
