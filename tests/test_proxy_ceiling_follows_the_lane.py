#!/usr/bin/env python3
"""The proxy's one-prompt ceiling follows the lane that serves, not the last switch.

The flash lane's 250,000-token ceiling was the proxy unit's PROMPT_CEILING_TOKENS, set per
install and moved by switch-model.sh at switch time, with a proxy restart, while the old
lane still served: a switch to flash queued behind a 27B serving 1M refused every prompt
past 250,000 until the next boot, and the restart cut every stream in flight through
:30001 (found in review, 2026-09-24; the reference box's notes had the trap as a manual
fix since 2026-09-12). Proxy v6.25 applies FLASH_PROMPT_CEILING_TOKENS while the served
model is the flash lane's; the cockpit and oc-fit-limits.py read the same rule; a switch
leaves a proxy of that kind alone, and restarts opencode-web only when its limits change.
"""
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]


def proxy_module(prompt_ceiling, flash_ceiling):
    saved = {k: os.environ.get(k) for k in ("PROMPT_CEILING_TOKENS", "FLASH_PROMPT_CEILING_TOKENS", "UPSTREAM")}
    os.environ["PROMPT_CEILING_TOKENS"] = str(prompt_ceiling)
    os.environ["FLASH_PROMPT_CEILING_TOKENS"] = str(flash_ceiling)
    # no engine of the box's, whatever a test reaches: a read meant to be stubbed went to :30000 (2026-11-09)
    os.environ["UPSTREAM"] = "http://127.0.0.1:9"
    spec = importlib.util.spec_from_file_location(f"kp_ceil_{prompt_ceiling}_{flash_ceiling}", REPO / "keepalive-proxy.py")
    mod = importlib.util.module_from_spec(spec)
    err, sys.stderr = sys.stderr, io.StringIO()
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.stderr = err
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


def fit_module():
    spec = importlib.util.spec_from_file_location("oc_fit_ceiling", REPO / "oc-fit-limits.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CASES = [  # (PROMPT_CEILING_TOKENS, FLASH_PROMPT_CEILING_TOKENS, served, expected)
    (0, 250000, ("qwen3.8-27b",), 0),
    (0, 250000, ("qwen3.8-flash-next",), 250000),
    (0, 250000, ("qwen3.8-flash-next-fp4",), 250000),   # the Qwen3.8 Flash-Next FP4-Local lane: the flash window
    (0, 250000, (), 250000),                      # unknown: the tighter, retryable mistake
    (100000, 250000, ("qwen3.8-flash-next",), 100000),
    (100000, 250000, ("qwen3.8-27b",), 100000),
    (250000, 0, ("qwen3.8-27b",), 250000),        # a unit from before v1.18.7, as it was
    (0, 0, ("qwen3.8-flash-next",), 0),
]


class TheProxyReadsTheServedLane(unittest.TestCase):
    def test_every_case(self):
        for prompt, flash, served, want in CASES:
            with self.subTest(prompt=prompt, flash=flash, served=served):
                m = proxy_module(prompt, flash)
                m.served_models = lambda s=served: s
                self.assertEqual(m.lane_ceiling(), want)
                limit = m.prompt_limit(900000)
                self.assertEqual(limit, min(int(900000 * (1 - m.OVERSIZE_MARGIN_FRAC)), want or 10 ** 9))

    def test_the_fp4_local_lane_at_1m_has_no_flash_ceiling(self):
        # FLASH_LOCAL_CONTEXT=1m (v1.45.2): its engine's window is past 262,144, and the flash ceiling refused every prompt
        # past 250,000 there (code review of v1.45.2); at its native window it keeps the flash ceiling
        for window, want in ((1048576, 0), (262144, 250000), (0, 250000)):
            with self.subTest(window=window):
                m = proxy_module(0, 250000)
                m.served_models = lambda: ("qwen3.8-flash-next-fp4",)
                m._SERVED["window"] = window
                self.assertEqual(m.lane_ceiling(), want)
        m = proxy_module(0, 250000)                      # the SGLang flash lane keeps it, whatever its window
        m.served_models = lambda: ("qwen3.8-flash-next",)
        m._SERVED["window"] = 1048576
        self.assertEqual(m.lane_ceiling(), 250000)

    def test_the_window_is_read_with_the_names_and_nothing_more(self):
        # read on /v1/models with the names, as the cockpit reads it: the pool's read, which the ceiling waited on, could
        # fail or say no pool while the engine served 1M, and ran again at every check (second review of v1.45.2)
        import json as _json
        m = proxy_module(0, 250000)

        class Answer:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body
        asked = []

        def urlopen(req, timeout=None):
            asked.append(req.full_url)
            return Answer(_json.dumps({"data": [{"id": "qwen3.8-flash-next-fp4", "owned_by": "vllm",
                                                 "max_model_len": 1048576}]}).encode())
        from unittest import mock
        m._api_key = lambda: "k"
        m.follow_engine_restarts = lambda: None
        m.pool_tokens = lambda: self.fail("the ceiling reads no pool")
        with mock.patch.object(m.urllib.request, "urlopen", urlopen):
            self.assertEqual(m.lane_ceiling(), 0)
            self.assertEqual(m._SERVED["window"], 1048576)
            self.assertEqual(asked, ["http://127.0.0.1:9/v1/models"])
            self.assertEqual(m.lane_ceiling(), 0)        # cached with the names: no second read
        self.assertEqual(len(asked), 1)

    def test_one_reader_of_the_window_for_the_names_and_the_pool(self):
        # an alias listed first with no window: the names' reader looked at the first row only, the pool's at the first
        # that says one, and the lane at 1M fell back under the flash ceiling (third review of v1.45.2)
        m = proxy_module(0, 250000)
        rows = [{"id": "alias"}, {"id": "qwen3.8-flash-next-fp4", "max_model_len": 1048576}]
        self.assertEqual(m.served_window(rows), 1048576)
        self.assertEqual(m.vllm_pool_facts("", rows)[1], 1048576)
        self.assertEqual(m.served_window([{"id": "x", "max_model_len": "1048576"}, "junk"]), 0)
        self.assertEqual(m.served_window(None), 0)

    def test_an_unreachable_engine_counts_as_unknown(self):
        m = proxy_module(0, 250000)

        def gone():
            raise m.EngineUnreachable("refused")
        m.served_models = gone
        self.assertEqual(m.lane_ceiling(), 250000)


class TheFitToolAndTheCockpitAgree(unittest.TestCase):
    def test_the_fit_tool_reads_the_same_rule(self):
        fit = fit_module()
        for prompt, flash, served, want in CASES:
            env = f"Environment=UPSTREAM=http://127.0.0.1:30000 PROMPT_CEILING_TOKENS={prompt} FLASH_PROMPT_CEILING_TOKENS={flash}"
            with self.subTest(prompt=prompt, flash=flash, served=served):
                self.assertEqual(fit.ceiling_from_env(env, served[0] if served else None), want)
        # and at the FP4-Local lane's 1M window, as the proxy decides (v1.45.2)
        env = "Environment=UPSTREAM=http://127.0.0.1:30000 PROMPT_CEILING_TOKENS=0 FLASH_PROMPT_CEILING_TOKENS=250000"
        self.assertEqual(fit.ceiling_from_env(env, "qwen3.8-flash-next-fp4", 1048576), 0)
        self.assertEqual(fit.ceiling_from_env(env, "qwen3.8-flash-next-fp4", 262144), 250000)
        self.assertEqual(fit.ceiling_from_env(env, "qwen3.8-flash-next", 1048576), 250000)

    def test_the_cockpit_asks_the_fit_tool(self):
        cockpit = (REPO / "dashboard/cockpit.py").read_text()
        block = cockpit[cockpit.index("def collect_engine_info():"):cockpit.index("CANARY: dict =")]
        # one reading of it for the SGLang lanes' panel and the vLLM lanes' (proxy_prompt_ceiling,
        # since the Qwen3.8 Flash-Next FP4-Local lane, which the proxy holds to the flash lane's ceiling)
        # with the engine's window: the FP4-Local lane at 1M has no flash ceiling (v1.45.2)
        self.assertIn('proxy_prompt_ceiling(slim.get("served_model_name"), int(slim.get("context_length") or 0))', block)
        helper = cockpit[cockpit.index("def proxy_prompt_ceiling(served"):]
        helper = helper[:helper.index("\n\n\n")]
        self.assertIn('_oc_fit().ceiling_from_env(env or "", served, window)', helper)
        self.assertEqual(cockpit.count("ceiling_from_env("), 1, "one reading of the ceiling")


class ASwitchLeavesThatProxyAlone(unittest.TestCase):
    TEXT = (REPO / "switch-model.sh").read_text()

    def run_block(self, unit_text):
        start = self.TEXT.index('KA_UNIT="/etc/systemd/system/qwen38-keepalive.service"')
        block = self.TEXT[start:self.TEXT.index("\nfi\n", start) + 4]
        d = pathlib.Path(tempfile.mkdtemp(prefix="sw-ceil-"))
        (d / "keepalive.service").write_text(unit_text)
        (d / "sudo").write_text(f"#!/bin/sh\necho \"sudo $*\" >> {d}/calls\n")
        (d / "sudo").chmod(0o755)
        script = ('set -euo pipefail\ndie(){ echo "DIE: $*"; exit 1; }\nTARGET_LANE=flash\n'
                  f'STAGE_CEIL="{d}/stage"\n' + block.replace("/etc/systemd/system/qwen38-keepalive.service", f"{d}/keepalive.service"))
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{d}:/usr/bin:/bin"})
        calls = (d / "calls").read_text() if (d / "calls").exists() else ""
        return r.stdout + r.stderr, calls

    def test_a_unit_that_follows_the_lane_is_not_touched(self):
        out, calls = self.run_block("[Service]\nEnvironment=PROMPT_CEILING_TOKENS=0\nEnvironment=FLASH_PROMPT_CEILING_TOKENS=250000\n")
        self.assertEqual(calls, "", "the switch restarted a proxy that needed nothing")
        self.assertIn("follows the lane that serves", out)

    def test_an_older_unit_is_moved_the_old_way(self):
        out, calls = self.run_block("[Service]\nEnvironment=PROMPT_CEILING_TOKENS=0\n")
        self.assertIn("systemctl restart qwen38-keepalive.service", calls)

    def test_opencode_web_is_restarted_only_for_new_limits(self):
        text = self.TEXT
        i = text.index('sudo systemctl restart opencode-web.service \\\n      && echo "opencode-web.service restarted')
        guard = text[text.rfind("\n  if ", 0, i):i]
        self.assertIn('"$(oc_configs_sum)" != "$SW_OC_SUM_BEFORE"', guard)
        self.assertLess(text.index('SW_OC_SUM_BEFORE="$(oc_configs_sum)"'), text.index("if read -r SW_CTX SW_OUT _SW_LABEL"))


class TheInstallerWritesBoth(unittest.TestCase):
    def test_the_unit_carries_the_flash_ceiling_whatever_the_lane(self):
        tpl = (REPO / "qwen38-keepalive.service.template").read_text()
        self.assertIn("Environment=FLASH_PROMPT_CEILING_TOKENS=__FLASH_PROMPT_CEILING__", tpl)
        install = (REPO / "install.sh").read_text()
        self.assertIn('FLASH_PROMPT_CEILING="${FLASH_PROMPT_CEILING_TOKENS:-250000}"', install)
        self.assertIn('PROMPT_CEILING="${PROMPT_CEILING_TOKENS:-0}"', install)


if __name__ == "__main__":
    unittest.main(verbosity=2)
