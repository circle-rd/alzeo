#!/usr/bin/env python3
"""install.sh writes down what it installed: ~/.config/qwen38/installed.json (2026-10-17).

Nothing recorded which release a box runs (the cockpit named its clone's CHANGELOG, so a git
pull with no ./install.sh read as an update done), nor which values the operator had chosen:
a value an older release installed by default and a value somebody typed looked the same in
a unit. Every run that ends well now records the release, its commit, how it ended and the
choices made on its command line; conformity.py writes it, atomically and under a lock, and
carries the age of the box from one record to the next. The run for the other text lane
(QWEN38_SECONDARY) writes nothing: PORT, HF_CACHE and the binds reach it from the run that
started it and only look explicit there. A record that cannot be written never fails a run.
"""
import fcntl
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer_wall as wall  # noqa: E402
from test_conformity_classify import box  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
LINES = TEXT.splitlines()
CONFORMITY = str(REPO / "conformity.py")
NO_ENV = ("MODEL_CHOICE", "PORT", "HF_CACHE", "CONTEXT_MODE", "PROXY_PORT", "ENGINE_BIND", "PROXY_BIND", "PLE_DIR",
          "SPEC_TOKEN_MAP_SIZE", "FLASH_REPLAYSSM_SPEC", "DRAFT2_REPO", "DRAFT2_REV", "DRAFT2_QUANT", "DRAFT2_TOKENS",
          "KOLIBRI_VARIANT", "IMAGE_MODEL", "VIDEO_VARIANT")


def git_head():
    r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else None


def record_install_func():
    """record_install and collect_choices, which gives it the choices of the command line."""
    funcs = []
    for name in ("collect_choices() {", "record_install() {"):
        start = TEXT.index(name)
        funcs.append(TEXT[start:TEXT.index("\n}\n", start) + 3])
    return "".join(funcs)


def run_record_install(how, units, cfg, repo=REPO, secondary="", lane="27b", **resolved):
    """install.sh's own record_install, run with the values a run would have resolved by then
    (explicit ones given as _ENV_<NAME>=value, like install.sh's captures)."""
    env_lines = [f'_ENV_{k}=""' for k in NO_ENV if k != "MODEL_CHOICE"] + ['_ENV_MODEL_CHOICE=""', 'FLASH_TIER_ENV=""']
    values = {"ENGINE_BIND": "127.0.0.1", "PROXY_BIND": "0.0.0.0", "PORT": "30000", "HF_CACHE": "/home/u/.cache/huggingface",
              "PROXY_PORT": "30001", "PLE_DIR": "/home/u/flashnext-ple", "CONTEXT_MODE": "1m", "FLASH_TIER": "concurrency",
              "SPEC_TOKEN_MAP_SIZE": "65536", "FLASH_REPLAYSSM_SPEC": "1", "MODEL_CHOICE": "stock",
              "DRAFT2_REPO": "maurienne-ai/Qwen3.8-27B-DFlash2-NVFP4-RTNcal", "DRAFT2_QUANT": "modelopt_fp4", "DRAFT2_TOKENS": "16"}
    for k, v in resolved.items():
        if k.startswith("_ENV_") or k == "FLASH_TIER_ENV":
            env_lines.append(f"{k}='{v}'")
        else:
            values[k] = v
    script = ("set -euo pipefail\n" + "\n".join(env_lines) + "\n"
              + "\n".join(f"{k}='{v}'" for k, v in values.items()) + "\n"
              + f"SECONDARY='{secondary}'; LANE='{lane}'; CONFIG_DIR='{cfg}'; REPO_DIR='{repo}'\n"
              + record_install_func().replace(wall.UNITS, f"{units}/")
              + f"record_install {how}\necho AFTER-RECORD\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/u"})
    return r.returncode, r.stdout + r.stderr


def written(cfg):
    p = pathlib.Path(cfg) / "installed.json"
    return json.loads(p.read_text()) if p.exists() else None


class EveryRunThatEndsWellRecordsIt(unittest.TestCase):
    def test_each_successful_end_of_a_run_records_how_it_ended(self):
        """Six lines end a run with exit 0 past the flags (--help ends on its own line): four
        end the run that serves, each right after record_install with its path and the run's
        last line (star_line, v1.46.0), one ends the run for the other text lane, and one --plan,
        before step 1: neither records anything."""
        ends = [i for i, ln in enumerate(LINES) if ln.strip() == "exit 0"]
        self.assertEqual(len(ends), 6, "a new way for install.sh to end: decide what it records")
        self.assertEqual([i for i in ends if LINES[i - 1].strip().startswith("record_install ")], [],
                         "a record is followed by the run's last line, then the exit")
        for i in ends:
            if LINES[i - 1].strip() == "star_line":
                self.assertTrue(LINES[i - 2].strip().startswith("record_install "), "star_line only ends a recorded run")
        before = [LINES[i - 2].strip() if LINES[i - 1].strip() == "star_line" else LINES[i - 1].strip() for i in ends]
        self.assertEqual(sorted(b for b in before if b.startswith("record_install ")),
                         ["record_install full", "record_install no-service", "record_install no-start",
                          "record_install side-boot"])
        secondary = TEXT.index('if [ -n "$SECONDARY" ]; then\n  # Installed, not enabled and not started')
        own_exit = TEXT.index("  exit 0\n", secondary)
        self.assertNotIn("record_install", TEXT[secondary:own_exit])
        plan = TEXT.index('if [ "$PLAN_ONLY" -eq 1 ]; then\n  echo\n  echo "--plan: nothing was changed.')
        plan_exit = TEXT.index("  exit 0\n", plan)
        self.assertNotIn("record_install", TEXT[plan:plan_exit])
        self.assertLess(plan_exit, TEXT.index('step "1/10 Preflight checks"'), "--plan ends before step 1")
        self.assertEqual(sum(1 for b in before if not b.startswith("record_install ")), 2)

    def test_the_paths_are_the_right_ones(self):
        for marker, how in (('if [ "$NO_SERVICE" -eq 1 ]; then\n  apply_context_configs', "no-service"),
                            ('step "Done: the text lane is up to date; the $SERVING_LANE lane', "side-boot"),
                            ('if [ "$NO_START" -eq 1 ]; then\n  step "Done (service installed', "no-start"),
                            ('lane_line "Super VL   "', "full")):
            at = TEXT.index(marker)
            self.assertEqual([ln.strip() for ln in TEXT[at:TEXT.index("exit 0\n", at)].rstrip().splitlines()[-2:]],
                             [f"record_install {how}", "star_line"], how)


class WhatItWrites(unittest.TestCase):
    def test_a_full_run_writes_its_release_commit_and_path(self):
        units, cfg = box(key_at="2026-09-30T10:00:00+02:00")
        rc, out = run_record_install("full", units, cfg)
        self.assertEqual(rc, 0, out)
        self.assertIn("AFTER-RECORD", out)
        got = written(cfg)
        first = re.search(r"^## (v\d+\.\d+\.\d+)", (REPO / "CHANGELOG.md").read_text(), re.M).group(1)
        self.assertEqual((got["schema"], got["release"], got["path"], got["lane"]), (1, first, "full", "27b"))
        self.assertEqual(got["commit"], git_head())
        self.assertEqual((got["box_since"], got["box_since_from"]), ("2026-09-30T08:00:00+00:00", "api-key"))
        self.assertEqual(got["choices"], {})
        self.assertIn("installed.json", out)

    def test_the_run_for_the_other_text_lane_writes_nothing(self):
        units, cfg = box()
        rc, out = run_record_install("full", units, cfg, secondary="flash", _ENV_PORT="30000", PORT="30000")
        self.assertEqual(rc, 0, out)
        self.assertIn("AFTER-RECORD", out)
        self.assertIsNone(written(cfg))

    def test_a_choice_on_the_command_line_is_recorded_and_a_one_run_override_is_not(self):
        units, cfg = box(host="0.0.0.0", tier="throughput")
        rc, out = run_record_install("full", units, cfg, _ENV_ENGINE_BIND="0.0.0.0", ENGINE_BIND="0.0.0.0",
                                     FLASH_TIER_ENV="throughput", FLASH_TIER="throughput", _ENV_MODEL_REV="abc")
        self.assertEqual(rc, 0, out)
        got = written(cfg)["choices"]
        self.assertEqual({k: (v["value"], v["how"]) for k, v in got.items()},
                         {"ENGINE_BIND": ("0.0.0.0", "env"), "FLASH_TIER": ("throughput", "env")})

    def test_the_lane_that_serves_names_the_target_it_was_told(self):
        units, cfg = box(target27="Qwen/Qwen3.8-27B-FP8")
        run_record_install("full", units, cfg, _ENV_MODEL_CHOICE="fp8", MODEL_CHOICE="fp8")
        self.assertEqual(written(cfg)["choices"]["target.27b"]["how"], "env")

    def test_a_no_service_run_records_no_choice(self):
        """run.sh reads the environment at every start: a choice there lives in the shell."""
        units, cfg = box(host="0.0.0.0")
        run_record_install("no-service", units, cfg, _ENV_ENGINE_BIND="0.0.0.0", ENGINE_BIND="0.0.0.0")
        got = written(cfg)
        self.assertEqual(got["path"], "no-service")
        self.assertNotEqual(got["choices"].get("ENGINE_BIND", {}).get("how"), "env")

    def test_a_record_that_cannot_be_written_never_fails_the_run(self):
        units, cfg = box()
        broken = wall._tmp("broken-repo-")
        (broken / "conformity.py").write_text("raise SystemExit('boom')\n")
        rc, out = run_record_install("full", units, cfg, repo=broken)
        self.assertEqual(rc, 0, out)
        self.assertIn("AFTER-RECORD", out)
        self.assertIn("NOTE: could not record this install", out)


class TheFileItself(unittest.TestCase):
    def record(self, cfg, units, *extra, umask=None):
        cmd = [sys.executable, CONFORMITY, "record", "--config-dir", cfg, "--units", units, "--lane", "27b",
               "--path", "full", *extra]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                preexec_fn=(lambda: os.umask(umask)) if umask is not None else None)

    def test_it_is_written_whole_and_world_readable_whatever_the_umask(self):
        units, cfg = box()
        p = self.record(cfg, units, umask=0o077)
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(stat.S_IMODE((pathlib.Path(cfg) / "installed.json").stat().st_mode), 0o644)
        self.assertEqual(sorted(f.name for f in pathlib.Path(cfg).iterdir()),
                         ["installed.json", "installed.json.lock", "launch-flash.sh"])

    def test_a_writer_waits_for_the_one_holding_the_lock(self):
        """The cockpit's Load (switch-model.sh) and an update in a terminal can both write it."""
        units, cfg = box()
        lock = open(pathlib.Path(cfg) / "installed.json.lock", "a")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            p = self.record(cfg, units)
            time.sleep(1.5)
            self.assertIsNone(p.poll(), "it wrote while another writer held the lock")
            self.assertIsNone(written(cfg))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out)
        self.assertIsNotNone(written(cfg))

    def test_the_age_of_the_box_is_carried_from_one_record_to_the_next(self):
        units, cfg = box(key_at="2026-09-05T11:00:00+02:00")
        self.record(cfg, units).communicate(timeout=60)
        t = time.time()
        os.utime(pathlib.Path(cfg) / "api-key", (t, t))          # a key restored from a backup
        self.record(cfg, units).communicate(timeout=60)
        got = written(cfg)
        self.assertEqual((got["box_since"], got["box_since_from"]), ("2026-09-05T09:00:00+00:00", "record"))

    def test_an_unreadable_file_is_written_again_whole_and_said(self):
        units, cfg = box(record='{"schema": 1, "choi')
        p = self.record(cfg, units)
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("is unreadable", out)
        self.assertEqual(written(cfg)["schema"], 1)

    def test_a_setting_it_does_not_know_is_refused_by_name(self):
        units, cfg = box()
        p = self.record(cfg, units, "--chose", "ENGINEBIND=0.0.0.0")
        out, _ = p.communicate(timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("ENGINEBIND", out)
        self.assertIsNone(written(cfg))


if __name__ == "__main__":
    unittest.main(verbosity=2)
