#!/usr/bin/env python3
"""The cockpit follows the Qwen3.8 Flash-Next FP4-Local lane on its own facts, as the third lane on vLLM with b12x.

Its engine is the community b12x vLLM image NVIDIA's model card names: the Nemotron lanes' routes,
/metrics and engine panel, an MTP head in place of a drafter, and a boot of its own, read by b12x's
loader and tuned by b12x at the first one. The fixtures are its engine's own answers, captured on the
reference box on 2026-10-20 (the card's DGX Spark recipe, the table on the internal disk, eight
requests running): its /metrics, its /v1/models, the log of a later boot and the journal of its first,
with the served name and the template's path renamed to the lane's (the capture ran the PR #55
setup, which served it as qwen3.8-flash-next). The module is imported with its environment pointed at
a throwaway box.
"""
import http.server
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]
METRICS = (HERE.parent / "fixtures" / "vllm-metrics-flash-local.txt").read_text()
MODELS = json.loads((HERE.parent / "fixtures" / "vllm-models-flash-local.json").read_text())
BOOT = (HERE.parent / "fixtures" / "flash-local-boot.log").read_text().splitlines()
FIRST = (HERE.parent / "fixtures" / "flash-local-first-boot.log").read_text().splitlines()
POOL = 949619
U = "qwen38-flash-local.service"
REPO_ID = "nvidia/Qwen3.8-Flash-Next-FP4-Local"
ENV_BEFORE = {}


def setUpModule():
    ENV_BEFORE.update(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)


class Engine(http.server.BaseHTTPRequestHandler):
    """vLLM as the cockpit meets it on the Qwen3.8 Flash-Next FP4-Local lane."""

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
            self._send(200, '{"version":"0.1.dev21830+g8244d1c44.d20261015"}')
        elif path == "/health":
            self._send(200, "")
        else:
            self._send(404, '{"detail":"Not Found"}')


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Path(tempfile.mkdtemp(prefix="cockpit-flash-local-"))
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
        spec = importlib.util.spec_from_file_location("cockpit_flash_local_under_test", DASH / "cockpit.py")
        cls.ck = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ck)
        cls.unit = cls.box / U
        cls.unit.write_text("ExecStart=/bin/bash -c 'exec /usr/bin/docker run --rm --name qwen38-flash-local \\\n"
                            "  eugr/spark-vllm-b12x@sha256:" + "0" * 64 + " \\\n"
                            f"  vllm serve {REPO_ID} --revision " + "b" * 40 + " \\\n"
                            "    --speculative-config.method mtp --speculative-config.num_speculative_tokens 4 \\\n"
                            "    --port 30000'\n")
        cls.ck.FLASH_LOCAL_UNIT_PATH = cls.unit
        # the Nemotron lanes' units, not installed here: their panel is not this lane's
        cls.ck.NEMOTRON_UNIT_PATH = cls.box / "absent-nemotron.service"
        cls.ck.NEMOTRON_SUPER_UNIT_PATH = cls.box / "absent-nemotron-super.service"
        cls.ck.ORNITH_UNIT_PATH = cls.box / "absent-ornith-122b.service"
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


class ItsEnginePanel(Base):
    def keepalive(self, env):
        real = self.real_run

        def fake(argv, **kw):
            if argv[:3] == ["systemctl", "show", "qwen38-keepalive.service"]:
                return env
            return real(argv, **kw)
        self.ck.run = fake

    def test_what_it_serves_comes_from_the_engine_and_its_own_unit(self):
        self.keepalive("Environment=PROMPT_CEILING_TOKENS=0 FLASH_PROMPT_CEILING_TOKENS=250000\n")
        out = self.ck.nemotron_engine_info()
        info = out["info"]
        self.assertEqual(info["served_model_name"], "qwen3.8-flash-next-fp4")
        self.assertEqual(info["model_path"], REPO_ID)
        self.assertEqual(info["context_length"], 262144)
        self.assertEqual(info["max_total_num_tokens"], POOL)
        self.assertEqual(info["kv_cache_dtype"], "fp8")
        self.assertEqual(info["mem_fraction_static"], 0.8)
        self.assertEqual((out["served_target"], info["quantization"]), ("flash-local", "nvfp4"))
        # an MTP head, and its draft length from the unit that started it
        self.assertEqual((info["speculative_algorithm"], info["speculative_num_draft_tokens"]), ("MTP", 4))
        # the flash lane's ceiling, which the proxy applies to it (v6.43): 0 here said none held
        self.assertEqual(out["prompt_ceiling_tokens"], 250000)

    def test_a_vllm_engine_is_told_apart_by_the_name_it_serves(self):
        self.keepalive("Environment=PROMPT_CEILING_TOKENS=0\n")
        self.assertEqual(self.ck.vllm_engine_info()["served_target"], "flash-local")
        self.assertEqual(self.ck.nemotron_unit_serving(MODELS["data"]), U)


class ItsState(Base):
    def setUp(self):
        super().setUp()
        self.asked = []
        self.ck.run = lambda argv, **kw: "abc\n" if argv[:2] == ["docker", "ps"] and "name=^qwen38-flash-local$" in argv else ""

    def state(self, healthy, enter="100", prev="ready", log=()):
        self.ck.kolibri_healthy = lambda: healthy
        self.ck._nemotron_log = lambda container: (self.asked.append(container), list(log))[1]
        st, boot, running = self.ck.nemotron_engine_state(U, active="active", sub="running",
                                                          prev_state=prev, enter_key=enter)
        return st["state"], boot, running

    def test_a_boot_is_read_from_its_own_containers_log_with_its_own_reader(self):
        upto = next(i for i, ln in enumerate(BOOT) if "10/21 shards routed" in ln) + 1
        st, boot, running = self.state(False, prev="starting", log=BOOT[:upto])
        self.assertEqual(st, "loading-weights")
        self.assertIn("shard 10 of 21", boot["detail"])
        self.assertTrue(running)
        self.assertEqual(self.asked, ["qwen38-flash-local"])

    def test_its_boot_walks_the_stages_in_order_and_names_the_mtp_head(self):
        lc = self.ck.lc
        stages = list(lc.FLASH_LOCAL_STAGES)
        start = next(i for i, ln in enumerate(BOOT) if "Initializing a V1 LLM engine" in ln)
        order = [stages.index(lc.parse_flash_local_boot_log(BOOT[:k])["stage"]) for k in range(start + 1, len(BOOT) + 1)]
        self.assertEqual(order, sorted(order))
        head = [lc.parse_flash_local_boot_log(BOOT[:k + 1])["detail"] for k in range(len(BOOT))
                if "0/3 shards routed" in BOOT[k] or "3/3 shards routed" in BOOT[k]]
        self.assertTrue(head and all(d.startswith("the MTP head") for d in head), head)
        end = lc.parse_flash_local_boot_log(BOOT)
        self.assertTrue(end["fired_up"])
        self.assertEqual(end["done"], stages)

    def test_its_first_boot_says_it_tunes_its_kernels_and_a_later_one_that_it_loads_them(self):
        lc = self.ck.lc
        # its progress lines ("b12x compiling gemm...: 63/241 ready, ..."), not its notes about them
        progress = re.compile(r"b12x (compiling|autotuning) [\w.]+: \d+/\d+ ready")
        tuning = [lc.parse_flash_local_boot_log(FIRST[:k + 1])["detail"] for k in range(len(FIRST))
                  if progress.search(FIRST[k])]
        self.assertTrue(tuning and all("first boot only" in d for d in tuning), tuning[:3])
        later = [lc.parse_flash_local_boot_log(BOOT[:k + 1])["detail"] for k in range(len(BOOT)) if "b12x priming" in BOOT[k]]
        self.assertTrue(later and all(d.startswith("loading b12x's kernel choices") for d in later), later[:3])
        self.assertTrue(lc.parse_flash_local_boot_log(FIRST)["fired_up"])

    def test_the_nemotron_reader_is_not_its(self):
        # read with the Nemotron markers, its whole load read as "starting vLLM"
        upto = next(i for i, ln in enumerate(BOOT) if "10/21 shards routed" in ln) + 1
        self.assertEqual(self.ck.lc.parse_nemotron_boot_log(BOOT[:upto])["stage"], "init")
        self.assertEqual(self.state(False, prev="starting", log=BOOT[:upto])[0], "loading-weights")


class WhatItDoesNotHave(Base):
    def test_the_canary_stands_down_and_says_why_in_its_name(self):
        self.serving("ready")
        out = self.ck.collect_canary()
        self.assertTrue(out["skipped"], out)
        self.assertIn("the Qwen3.8 Flash-Next FP4-Local lane is checked by vLLM's own /health", out["why"])

    def test_flush_abort_and_fit_are_refused_in_its_name(self):
        self.serving("ready")
        for name in ("flush_cache", "abort_all", "fit_opencode"):
            with self.subTest(name=name):
                why = self.ck.b12x_cannot(name)
                self.assertTrue(why and why.startswith("The Qwen3.8 Flash-Next FP4-Local lane"), why)
        with self.assertRaises(RuntimeError) as e:
            self.ck.engine_abort_all()
        self.assertIn("the Qwen3.8 Flash-Next FP4-Local lane's vLLM has no route that aborts", str(e.exception))
        ok, lines, _ = self.ck.job_flush_cache(None)
        self.assertFalse(ok)
        self.assertIn("the Qwen3.8 Flash-Next FP4-Local lane's vLLM has no cache flush route", lines[0])
        # and on a Nemotron lane, the words stay the Nemotron lane's
        self.serving("ready", unit="qwen38-nemotron.service")
        self.assertTrue(self.ck.b12x_cannot("flush_cache").startswith("The Nemotron 3.5 Bolt lane"))

    def test_system_one_points_at_the_sglang_lanes(self):
        self.serving("ready")
        out = self.ck.systemone_available(max_age=0)
        self.assertFalse(out["available"])
        self.assertIn("the Qwen3.8 Flash-Next FP4-Local lane is serving", out["reason"])


class ItsUnit(Base):
    def test_the_target_is_read_from_its_unit(self):
        self.ck.UNIT_TARGET_CACHE.clear()
        self.assertEqual(self.ck.unit_target(U), {"model": REPO_ID, "target": "flash-local"})

    def test_it_is_a_switch_target_a_unit_the_page_may_drive_and_a_lane_it_prepares(self):
        self.assertIn("flash-local", self.ck.ACTIONS["switch"]["params"]["target"])
        self.assertIn(U, self.ck.SERVING_UNITS)
        self.assertIn(U, self.ck.lc.ENGINE_UNITS)
        self.assertNotIn(U, self.ck.lc.TEXT_UNITS, "the SGLang belts are not its")
        self.assertIn(U, self.ck.VLLM_UNITS)
        self.assertEqual(self.ck.TARGET_PIN["flash-local"], "FLASH_LOCAL_REV")
        self.assertEqual(self.ck.rg.PIN_MODELS["FLASH_LOCAL_REV"], REPO_ID)
        self.assertEqual(self.ck.lc.LANE_PROVIDER[U], "flash-local")
        argv = self.ck.ACTIONS["prepare"]["argv"]({"target": "flash-local"})
        self.assertEqual(argv[-1], "flash-local")

    def test_the_flash_lanes_prefix_does_not_take_it(self):
        # "flash-local".startswith("flash"): read by prefix, its Load prepared and switched the
        # SGLang flash lane
        self.assertEqual(self.ck.target_lane("flash-local"), "flash-local")
        self.assertEqual(self.ck.target_unit("flash-local"), self.ck.FLASH_LOCAL_UNIT_PATH)
        self.assertEqual(self.ck.target_lane("flash-nvda"), "flash")

    def test_it_is_prepared_once_the_image_its_unit_names_is_here(self):
        img = "eugr/spark-vllm-b12x@sha256:" + "0" * 64
        self.ck.run = lambda argv, **kw: "sha256:abc\n" if argv[:3] == ["docker", "image", "inspect"] and argv[-1] == img else ""
        self.assertTrue(self.ck.lane_prepared("flash-local"))
        self.ck.run = lambda argv, **kw: ""
        self.assertFalse(self.ck.lane_prepared("flash-local"))
        saved = self.ck.FLASH_LOCAL_UNIT_PATH
        self.ck.FLASH_LOCAL_UNIT_PATH = self.box / "absent.service"
        try:
            self.assertIsNone(self.ck.lane_prepared("flash-local"))
        finally:
            self.ck.FLASH_LOCAL_UNIT_PATH = saved


class ItStartsOnlyWithItsTableCopiesThere(Base):
    """A Start with no switch mounts the copies of its n-gram table its unit names: one gone, docker
    refuses the start and the unit loops (ple-table.py makes them at the Load's switch)."""

    def test_a_missing_copy_holds_the_start_back(self):
        hub = self.box / "hf" / "hub"
        copies = self.box / "copies"
        copies.mkdir()
        sha = "c" * 64
        unit = self.box / "startable.service"
        unit.write_text(f"ExecStart=/bin/bash -c 'exec /usr/bin/docker run -v {self.box}/hf:/root/.cache/huggingface "
                        f"-v {copies}/{sha}:/root/.cache/huggingface/hub/models--nvidia--Qwen3.8-Flash-Next-FP4-Local/blobs/{sha}:ro "
                        f"eugr/spark-vllm-b12x@sha256:{'0' * 64} vllm serve {REPO_ID} --revision {'b' * 40} --port 30000'\n")
        d = hub / "models--nvidia--Qwen3.8-Flash-Next-FP4-Local" / "snapshots" / ("b" * 40)
        d.mkdir(parents=True)
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_text("w")
        saved = self.ck.FLASH_LOCAL_UNIT_PATH
        self.ck.FLASH_LOCAL_UNIT_PATH = unit
        self.ck.UNIT_TARGET_CACHE.clear()
        self.ck.run = lambda argv, **kw: "sha256:abc\n"
        try:
            self.assertFalse(self.ck.lane_startable("flash-local"), "its table's copy is missing")
            # an empty copy is no copy, as the unit's own check says (flash-local-files.sh): the page
            # offered a Start the unit then refused (found in the series' second review, 2026-10-20)
            (copies / sha).write_text("")
            self.ck.UNIT_TARGET_CACHE.clear()
            self.assertFalse(self.ck.lane_startable("flash-local"), "its table's copy is empty")
            # the copy's original, in the cache: a copy has its size, as the unit's check and
            # ple-table.py read it, and one cut short is no copy (found in review, 2026-10-20)
            orig = self.box / "hf" / "hub" / "models--nvidia--Qwen3.8-Flash-Next-FP4-Local" / "blobs" / sha
            orig.parent.mkdir(parents=True, exist_ok=True)
            orig.write_text("rows")
            (copies / sha).write_text("rows")
            self.ck.UNIT_TARGET_CACHE.clear()
            self.assertTrue(self.ck.lane_startable("flash-local"))
            (copies / sha).write_text("row")
            self.ck.UNIT_TARGET_CACHE.clear()
            self.assertFalse(self.ck.lane_startable("flash-local"), "its table's copy is cut short")
            (copies / sha).write_text("rows")
            self.ck.UNIT_TARGET_CACHE.clear()
            self.assertTrue(self.ck.lane_startable("flash-local"))
            # the unit's check of its files gone, it refuses every start: its Load must run the
            # switch, whose stage puts the check back (found in review, 2026-10-20)
            check = self.box / "cfg-check" / "flash-local-files.sh"
            unit.write_text(unit.read_text().replace("ExecStart=/bin/bash -c '", f"ExecStart=/bin/bash -c 'check={check}; ", 1))
            self.ck.UNIT_TARGET_CACHE.clear()
            self.assertFalse(self.ck.lane_startable("flash-local"), "its check of its files is gone")
            check.parent.mkdir()
            check.write_text("exit 0\n")
            self.ck.UNIT_TARGET_CACHE.clear()
            self.assertTrue(self.ck.lane_startable("flash-local"))
        finally:
            self.ck.FLASH_LOCAL_UNIT_PATH = saved


class ItStartsOnlyWithItsTemplateThere(Base):
    """Its unit mounts its chat template on its own (--mount, not the whole directory): the check that
    a lane does not start on an empty template read the directory's -v mount only, and passed it by
    (found in review, 2026-10-20)."""

    def test_an_empty_template_holds_the_start_back(self):
        cfg = self.box / "cfg"
        cfg.mkdir(exist_ok=True)
        tmpl = cfg / "chat-template-flash-local.jinja"
        unit = self.box / "template.service"
        unit.write_text(f"ExecStart=/bin/bash -c 'exec /usr/bin/docker run -v {self.box}/hf2:/root/.cache/huggingface "
                        f"--mount type=bind,source={tmpl},target=/out/chat-template-flash-local.jinja,readonly "
                        f"eugr/spark-vllm-b12x@sha256:{'0' * 64} vllm serve {REPO_ID} --revision {'b' * 40} "
                        f"--chat-template /out/chat-template-flash-local.jinja --port 30000'\n")
        d = self.box / "hf2" / "hub" / "models--nvidia--Qwen3.8-Flash-Next-FP4-Local" / "snapshots" / ("b" * 40)
        d.mkdir(parents=True)
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_text("w")
        saved = self.ck.FLASH_LOCAL_UNIT_PATH
        self.ck.FLASH_LOCAL_UNIT_PATH = unit
        self.ck.run = lambda argv, **kw: "sha256:abc\n"
        try:
            for body, want in (("", False), ("{{ messages }}", True)):
                with self.subTest(template=body):
                    tmpl.write_text(body)
                    self.ck.UNIT_TARGET_CACHE.clear()
                    self.assertIs(self.ck.lane_startable("flash-local"), want)
        finally:
            self.ck.FLASH_LOCAL_UNIT_PATH = saved


if __name__ == "__main__":
    unittest.main()
