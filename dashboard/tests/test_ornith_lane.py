#!/usr/bin/env python3
"""The cockpit follows the Ornith 1.5 122B lane (v1.38) on its own facts, as the fourth lane on vLLM
with b12x.

Its engine is the Qwen3.8 Flash-Next FP4-Local lane's image, read by the same b12x loader, with an MTP head: the
family's routes, its boot read as FP4-Local's is, and its name in each sentence. The boot fixture is
its engine's own log, captured on the reference box on 2026-10-21 (vLLM b12x, MTP 2, eight requests at
a time), up to its startup line. The module is imported with its environment pointed at a throwaway box.
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
BOOT = (HERE.parent / "fixtures" / "ornith-boot.log").read_text().splitlines()
U = "qwen38-ornith-122b.service"
REPO_ID = "ornith-ai/Ornith-1.5-122B-NVFP4"
MODELS = {"object": "list", "data": [{"id": "ornith-1.5-122b", "object": "model", "owned_by": "vllm", "root": REPO_ID,
                                      "max_model_len": 1048576}]}
ENV_BEFORE = {}


def setUpModule():
    ENV_BEFORE.update(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)


class Engine(http.server.BaseHTTPRequestHandler):
    """vLLM as the cockpit meets it on the Ornith lane: its model list, its health."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        body, code = ({"/v1/models": json.dumps(MODELS), "/health": ""}.get(path), 200)
        if body is None:
            body, code = '{"detail":"Not Found"}', 404
        out = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Path(tempfile.mkdtemp(prefix="cockpit-ornith-"))
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
        spec = importlib.util.spec_from_file_location("cockpit_ornith_under_test", DASH / "cockpit.py")
        cls.ck = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ck)
        cls.unit = cls.box / U
        cls.unit.write_text("ExecStart=/bin/bash -c 'exec /usr/bin/docker run --rm --name qwen38-ornith-122b \\\n"
                            "  eugr/spark-vllm-b12x@sha256:" + "0" * 64 + " \\\n"
                            f"  vllm serve {REPO_ID} --revision " + "d" * 40 + " \\\n"
                            "    --speculative-config.method mtp --speculative-config.num_speculative_tokens 2 \\\n"
                            "    --port 30000'\n")
        cls.ck.ORNITH_UNIT_PATH = cls.unit
        # the other lanes of the family, not installed here
        cls.ck.NEMOTRON_UNIT_PATH = cls.box / "absent-nemotron.service"
        cls.ck.NEMOTRON_SUPER_UNIT_PATH = cls.box / "absent-nemotron-super.service"
        cls.ck.FLASH_LOCAL_UNIT_PATH = cls.box / "absent-flash-local.service"
        cls.real_run = cls.ck.run

    @classmethod
    def tearDownClass(cls):
        cls.engine.shutdown()
        cls.engine.server_close()
        shutil.rmtree(cls.box, ignore_errors=True)

    def setUp(self):
        self.ck.run = self.real_run

    def serving(self, state="ready", unit=U):
        with self.ck.LIFE_LOCK:
            self.ck.LIFE["states"] = {unit: state} if state else {}


class ItsBoot(Base):
    def setUp(self):
        super().setUp()
        self.asked = []
        self.ck.run = lambda argv, **kw: "abc\n" if argv[:2] == ["docker", "ps"] and "name=^qwen38-ornith-122b$" in argv else ""

    def state(self, healthy, enter="100", prev="ready", log=()):
        self.ck.kolibri_healthy = lambda: healthy
        self.ck._nemotron_log = lambda container: (self.asked.append(container), list(log))[1]
        st, boot, running = self.ck.nemotron_engine_state(U, active="active", sub="running",
                                                          prev_state=prev, enter_key=enter)
        return st["state"], boot, running

    def test_a_boot_is_read_from_its_own_containers_log_with_the_b12x_loaders_reader(self):
        upto = next(i for i, ln in enumerate(BOOT) if "8/8 shards routed, 72.56 GB" in ln) + 1
        st, boot, running = self.state(False, prev="starting", log=BOOT[:upto])
        self.assertEqual(st, "loading-weights")
        self.assertIn("shard 8 of 8 (72.56 GB)", boot["detail"])
        self.assertTrue(running)
        self.assertEqual(self.asked, ["qwen38-ornith-122b"])
        # read with the Nemotron markers, its whole load read as "starting vLLM"
        self.assertEqual(self.ck.lc.parse_nemotron_boot_log(BOOT[:upto])["stage"], "init")

    def test_its_boot_walks_the_stages_in_order_and_names_the_mtp_head(self):
        lc = self.ck.lc
        stages = list(lc.FLASH_LOCAL_STAGES)
        start = next(i for i, ln in enumerate(BOOT) if "Initializing a V1 LLM engine" in ln)
        order = [stages.index(lc.parse_flash_local_boot_log(BOOT[:k])["stage"]) for k in range(start + 1, len(BOOT) + 1)]
        self.assertEqual(order, sorted(order))
        head = [lc.parse_flash_local_boot_log(BOOT[:k + 1])["detail"] for k in range(len(BOOT))
                if "7.00 GB selected" in BOOT[k]]
        self.assertTrue(head and all(d.startswith("the MTP head, shard 8 of 8") for d in head), head)
        later = [lc.parse_flash_local_boot_log(BOOT[:k + 1])["detail"] for k in range(len(BOOT)) if "b12x priming" in BOOT[k]]
        self.assertTrue(later and all(d.startswith("loading b12x's kernel choices") for d in later), later[:3])
        end = lc.parse_flash_local_boot_log(BOOT)
        self.assertTrue(end["fired_up"])
        self.assertEqual(end["done"], stages)


class WhatItDoesNotHave(Base):
    def test_the_canary_stands_down_and_says_why_in_its_name(self):
        self.serving("ready")
        out = self.ck.collect_canary()
        self.assertTrue(out["skipped"], out)
        self.assertIn("the Ornith 1.5 122B lane is checked by vLLM's own /health", out["why"])

    def test_flush_abort_and_fit_are_refused_in_its_name(self):
        self.serving("ready")
        for name in ("flush_cache", "abort_all", "fit_opencode"):
            with self.subTest(name=name):
                why = self.ck.b12x_cannot(name)
                self.assertTrue(why and why.startswith("The Ornith 1.5 122B lane"), why)
        with self.assertRaises(RuntimeError) as e:
            self.ck.engine_abort_all()
        self.assertIn("the Ornith 1.5 122B lane's vLLM has no route that aborts", str(e.exception))
        ok, lines, _ = self.ck.job_flush_cache(None)
        self.assertFalse(ok)
        self.assertIn("the Ornith 1.5 122B lane's vLLM has no cache flush route", lines[0])
        # the Qwen3.8 Flash-Next FP4-Local lane keeps its own words
        self.serving("ready", unit="qwen38-flash-local.service")
        self.assertTrue(self.ck.b12x_cannot("flush_cache").startswith("The Qwen3.8 Flash-Next FP4-Local lane"))

    def test_system_one_points_at_the_sglang_lanes_in_its_name(self):
        self.serving("ready")
        out = self.ck.systemone_available(max_age=0)
        self.assertFalse(out["available"])
        self.assertIn("the Ornith 1.5 122B lane is serving", out["reason"])
        self.assertNotIn("Nemotron", out["reason"])


class ItsUnit(Base):
    def test_the_target_is_read_from_its_unit(self):
        self.ck.UNIT_TARGET_CACHE.clear()
        self.assertEqual(self.ck.unit_target(U), {"model": REPO_ID, "target": "ornith-122b"})

    def test_its_engine_is_told_apart_by_the_name_it_serves(self):
        self.assertEqual(self.ck.nemotron_unit_serving(MODELS["data"]), U)
        self.assertEqual(self.ck.B12X_SPEC[U], "MTP")
        self.assertEqual(self.ck.B12X_LANE_NAME[U], "the Ornith 1.5 122B lane")

    def test_it_is_a_switch_target_a_unit_the_page_may_drive_and_a_lane_it_prepares(self):
        self.assertIn("ornith-122b", self.ck.ACTIONS["switch"]["params"]["target"])
        self.assertIn(U, self.ck.SERVING_UNITS)
        self.assertIn(U, self.ck.JOURNAL_UNITS)
        self.assertIn("qwen38-ornith-122b", self.ck.CONTAINERS)
        self.assertIn(U, self.ck.lc.ENGINE_UNITS)
        self.assertNotIn(U, self.ck.lc.TEXT_UNITS, "the SGLang belts are not its")
        self.assertIn(U, self.ck.VLLM_UNITS)
        self.assertEqual(self.ck.TARGET_PIN["ornith-122b"], "ORNITH_122B_REV")
        self.assertEqual(self.ck.rg.PIN_MODELS["ORNITH_122B_REV"], REPO_ID)
        self.assertIn("install-ornith-122b.sh", self.ck.rg.PIN_FILES)
        self.assertEqual(self.ck.lc.LANE_PROVIDER[U], "ornith-122b")
        self.assertEqual(self.ck.target_lane("ornith-122b"), "ornith-122b")
        self.assertEqual(self.ck.target_unit("ornith-122b"), self.ck.ORNITH_UNIT_PATH)
        self.assertEqual(self.ck.ACTIONS["prepare"]["argv"]({"target": "ornith-122b"})[-1], "ornith-122b")

    def test_it_is_prepared_once_the_image_its_unit_names_is_here(self):
        img = "eugr/spark-vllm-b12x@sha256:" + "0" * 64
        self.ck.run = lambda argv, **kw: "sha256:abc\n" if argv[:3] == ["docker", "image", "inspect"] and argv[-1] == img else ""
        self.assertTrue(self.ck.lane_prepared("ornith-122b"))
        self.ck.run = lambda argv, **kw: ""
        self.assertFalse(self.ck.lane_prepared("ornith-122b"))
        saved = self.ck.ORNITH_UNIT_PATH
        self.ck.ORNITH_UNIT_PATH = self.box / "absent.service"
        try:
            self.assertIsNone(self.ck.lane_prepared("ornith-122b"))
        finally:
            self.ck.ORNITH_UNIT_PATH = saved

    def test_its_journal_goes_in_the_diagnostics_bundle(self):
        src = (DASH / "cockpit.py").read_text()
        self.assertIn('(tdp / "journal-ornith-122b.txt").write_text(run(["journalctl", "-u", ORNITH_UNIT', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
