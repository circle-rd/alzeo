#!/usr/bin/env python3
"""The side lanes' checkpoints in the list, and what --recommended moves (v1.36.0, 2026-10-17).

The Kolibri, image and video lanes keep the checkpoint their unit names across updates (an
operator switched to kolibri-nvfp4 or image-turbo, or installed another video variant), and
the list said nothing of them (Hasan, 2026-10-17: record them from v1.36.0). They are compared
like the text lanes' targets: read back from their units the way their installers read them,
recommended as each installer installs a new box, and, since no release installed another one
by default, a different one is always a choice. ./install.sh --recommended moves every setting
but the site's to the recommended value, choices included (Hasan, 2026-10-17: "Tout comme une
box neuve", the site's port, cache and folders kept); a value set on its command line stays.
"""
import os
import pathlib
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_conformity_classify import HOME, REC, box, classes  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import conformity  # noqa: E402
from unittest import mock  # noqa: E402

# conformity.py recommends by the box's class (the native window on a 64 GB one): these hold a
# 128 GB box's state, so the class is that one whatever the machine running them is, and an
# exported QWEN38_MEMORY_CLASS is back as it was after them (found in review, 2026-10-23).
_ENV = mock.patch.dict(os.environ, {"QWEN38_MEMORY_CLASS": "128"})


def setUpModule():
    _ENV.start()


def tearDownModule():
    _ENV.stop()


NVFP4 = "Quantinine/Kolibri-1-NVFP4A16-FP8"
TURBO = "Qwen/Qwen-Image-2.1-Turbo"


def bash(script):
    r = subprocess.run(["bash", "-c", "set -eu\n" + script], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def line(script, pattern):
    """The one line of an installer that matches pattern."""
    found = [ln for ln in (REPO / script).read_text().splitlines() if re.search(pattern, ln)]
    assert len(found) == 1, (script, pattern, found)
    return found[0]


class TheRecommendedOnesAreWhatTheInstallersInstall(unittest.TestCase):
    """Each installer's own lines, run with no variable and no unit, as on a new box."""

    def test_kolibri_installs_its_fp8_checkpoint(self):
        text = (REPO / "install-kolibri.sh").read_text()
        start = text.index('case "${KOLIBRI_VARIANT:-}" in')
        block = text[start:text.index("esac", start) + 4]
        got = bash(f'INSTALLED_MODEL=""; KOLIBRI_NVFP4_REPO={NVFP4}\ndie(){{ exit 3; }}\n{block}\necho "$VARIANT"')
        self.assertEqual(got, "fp8")
        self.assertEqual(REC["KOLIBRI_VARIANT"], got)

    def test_the_image_lane_installs_its_pinned_model(self):
        got = bash('unit_flag(){ :; }\nkept_flag(){ :; }\n' + line("install-image.sh", r'^MODEL="\$\{IMAGE_MODEL:-') + '\necho "$MODEL"')
        self.assertEqual(got, "Qwen/Qwen-Image-2.1")
        self.assertEqual(REC["IMAGE_MODEL"], got)

    def test_the_video_lane_installs_fl2va(self):
        got = bash('unit_flag(){ :; }\nkept_flag(){ :; }\n' + line("install-video.sh", r'^VARIANT="\$\{VIDEO_VARIANT:-') + '\necho "$VARIANT"')
        self.assertEqual(got, "fl2va")
        self.assertEqual(REC["VIDEO_VARIANT"], got)


class EachOneIsReadBackFromItsUnit(unittest.TestCase):
    def test_another_checkpoint_is_a_choice_no_release_made_the_default(self):
        for kw, knob, value in ((dict(kolibri=NVFP4), "KOLIBRI_VARIANT", "nvfp4"),
                                (dict(image_model=TURBO), "IMAGE_MODEL", TURBO),
                                (dict(video_variant="ref2va"), "VIDEO_VARIANT", "ref2va")):
            with self.subTest(knob=knob):
                units, cfg = box(**kw)
                self.assertEqual(conformity.observe(units, cfg)[knob], value)
                self.assertEqual(classes(units, cfg), {knob: ("choice", "never-default")})

    def test_a_box_without_the_side_lanes_says_nothing_of_them(self):
        units, cfg = box(side=False)
        seen = conformity.observe(units, cfg)
        for knob in ("KOLIBRI_VARIANT", "IMAGE_MODEL", "VIDEO_VARIANT"):
            self.assertNotIn(knob, seen)
        self.assertEqual(classes(units, cfg), {})


class RecommendedMovesEveryChoiceButTheSites(unittest.TestCase):
    def setUp(self):
        self.units, self.cfg = box(flash_target="nvidia/Qwen3.8-Flash-Next-NVFP4", kolibri=NVFP4,
                                   host="0.0.0.0", port="30100", proxy_port="30101")

    def groups(self, chose=None, recommended_run=True):
        items = conformity.plan(self.cfg, "27b", chose or {}, units_dir=self.units, home=HOME,
                                recommended_run=recommended_run)
        return {i["knob"]: (i["group"], i["why"]) for i in items}, items

    def test_a_plain_run_keeps_them_and_a_recommended_one_moves_them(self):
        plain, _ = self.groups(recommended_run=False)
        self.assertEqual(plain["target.flash"], ("yours", "never-default"))
        self.assertEqual(plain["KOLIBRI_VARIANT"], ("yours", "never-default"))
        rec, _ = self.groups()
        for knob in ("target.flash", "KOLIBRI_VARIANT", "ENGINE_BIND"):
            with self.subTest(knob=knob):
                self.assertEqual(rec[knob], ("moves", "recommended"))
        self.assertEqual(rec["PORT"], ("site", "site"))
        self.assertNotIn("PROXY_PORT", rec)  # 30101 is what is recommended beside port 30100

    def test_a_value_set_on_its_command_line_stays(self):
        rec, _ = self.groups(chose={"KOLIBRI_VARIANT": "nvfp4"})
        self.assertEqual(rec["KOLIBRI_VARIANT"], ("yours", "env"))
        self.assertEqual(rec["target.flash"], ("moves", "recommended"))

    def test_the_list_says_what_moves_and_why(self):
        _, items = self.groups()
        lines = conformity.plan_lines(items, "v1.36.0")
        self.assertIn("  moves now    flash target flash-nvda, to flash: --recommended moves it, a choice of yours or not",
                      lines)
        self.assertIn("  moves now    Kolibri-1 checkpoint nvfp4, to fp8: --recommended moves it, a choice of yours or not",
                      lines)
        self.assertTrue(any(ln.startswith("  site setting PORT 30100") for ln in lines), lines)


if __name__ == "__main__":
    unittest.main()
