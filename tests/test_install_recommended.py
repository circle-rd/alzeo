#!/usr/bin/env python3
"""./install.sh --recommended: what a new box gets, the site's settings kept (v1.36.0, 2026-10-17).

A run keeps the operator's choices from one update to the next: each lane's target, the drafter,
the context mode, the binds, the flash tier, the side lanes' checkpoints, and opencode and the
cockpit an earlier --no-opencode or --no-cockpit left out (a lane left out is not kept: its
--no-<lane> counts for its run only, Hasan, 2026-10-22). Hasan asked for one command that brings a box to the
recommended state, "tout comme une box neuve", the site's port, cache and folders kept
(2026-10-17). --recommended keeps none of those choices: each setting the list compares, but the
site's, takes the value a new box gets, and what was left out comes back. --plan --recommended
lists what it moves and changes nothing, and a value set on the same command line still wins.

Each case runs install.sh itself up to step 1 (installer_wall) on a box that holds a choice
everywhere, and the same box without --recommended is the witness that every branch it skips is
taken on a plain run.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer_wall as wall  # noqa: E402
import test_conformity_classify as tcc  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
YOUNG_BOX = "2026-10-01T10:00:00+02:00"    # after every change OLD_DEFAULTS lists: all choices
NVFP4 = "Quantinine/Kolibri-1-NVFP4A16-FP8"
TURBO = "Qwen/Qwen-Image-2.1-Turbo"
LEFT_OUT = ("image.off", "opencode.off", "cockpit.off")
# the choices the box below holds, as a plain run says it keeps them
KEPT = ("Keeping the installed drafter:", "Keeping the installed context mode: native",
        "Keeping the installed engine bind: 0.0.0.0", "Keeping the installed proxy bind: 127.0.0.1",
        "Keeping the opencode integration off (your earlier --no-opencode)",
        "Keeping the cockpit off (your earlier --no-cockpit)")


class Base(unittest.TestCase):
    def setUp(self):
        self.home_was = tcc.HOME

    def tearDown(self):
        tcc.HOME = self.home_was

    def run_install(self, *args, enabled=(), env=None, **box):
        """(exit code, output, config dir) of install.sh run up to step 1 on a box holding a
        choice in every setting the list compares, its site on port 30100."""
        home = wall._tmp("recommended-home-")
        tcc.HOME = str(home)
        kw = dict(context="native", host="0.0.0.0", proxy_bind="127.0.0.1", port="30100", proxy_port="30101",
                  draft=tcc.ZLAB, flash_target="nvidia/Qwen3.8-Flash-Next-NVFP4", kolibri=NVFP4,
                  image_model=TURBO, video_variant="ref2va", key_at=YOUNG_BOX)
        kw.update(box)
        units, cfg = tcc.box(**kw)
        for name in LEFT_OUT:
            (pathlib.Path(cfg) / name).write_text("left out\n")
        shutil.copytree(cfg, pathlib.Path(home) / ".config/qwen38")
        fenv, record = wall.fenced_env(home=str(home), enabled=enabled, **(env or {}))
        r = subprocess.run([wall.walled(units=units), *args], capture_output=True, text=True,
                           env=fenv, cwd=wall.cwd(), timeout=180)
        self.record = record
        return r.returncode, r.stdout + r.stderr, pathlib.Path(home) / ".config/qwen38"


class ARecommendedRunKeepsNoChoice(Base):
    def test_a_plain_run_keeps_every_choice_of_this_box(self):
        rc, out, cfg = self.run_install()
        self.assertIn(wall.WALL, out, out[-800:])
        for line in KEPT:
            with self.subTest(line=line):
                self.assertIn(line, out)
        for name in ("opencode.off", "cockpit.off"):
            self.assertTrue((cfg / name).exists(), name)
        # but a lane: an older release's marker is said once and dropped
        self.assertIn("Your earlier --no-image is no longer remembered (left out)", out)
        self.assertFalse((cfg / "image.off").exists())

    def test_recommended_keeps_none_but_the_sites_and_brings_back_what_was_left_out(self):
        rc, out, cfg = self.run_install("--recommended")
        self.assertIn(wall.WALL, out, out[-800:])
        for line in KEPT:
            with self.subTest(line=line):
                self.assertNotIn(line, out)
        self.assertIn("Keeping the installed port: :30100", out, "the site's port stays")
        self.assertIn("Your earlier --no-image is no longer remembered (left out)", out)
        self.assertIn("--recommended: the opencode integration comes back (your earlier --no-opencode), as on a new box.", out)
        self.assertIn("--recommended: the cockpit comes back (your earlier --no-cockpit), as on a new box.", out)
        self.assertFalse((cfg / "image.off").exists(), "the lane's marker is gone, as on a plain run")
        self.assertFalse((cfg / "cockpit.off").exists())
        # the list before step 1 names each choice it moves, the side lanes' checkpoints among them
        at = out.index("recommends:")
        for name in ("27B drafter z-lab/Qwen3.8-27B-DFlash2 (unquant, D=8), to maurienne-ai",
                     "CONTEXT_MODE native, to 1m", "ENGINE_BIND 0.0.0.0, to 127.0.0.1",
                     "PROXY_BIND 127.0.0.1, to 0.0.0.0", "flash target flash-nvda, to flash",
                     "Kolibri checkpoint nvfp4, to fp8", f"image model {TURBO}, to Qwen/Qwen-Image-2.1",
                     "video variant ref2va, to fl2va"):
            with self.subTest(moves=name):
                self.assertRegex(out[at:], r"moves now +" + re.escape(name) + r".*: --recommended moves it")
        self.assertRegex(out[at:], r"site setting PORT 30100")

    def test_a_value_set_on_the_same_command_line_wins(self):
        rc, out, cfg = self.run_install("--recommended", env={"CONTEXT_MODE": "native"})
        self.assertIn(wall.WALL, out, out[-800:])
        self.assertRegex(out, r"stays yours +CONTEXT_MODE native \(recommended: 1m\), set on this command line")
        self.assertNotRegex(out, r"moves now +CONTEXT_MODE")

    def test_plan_lists_what_it_moves_and_changes_nothing(self):
        rc, out, cfg = self.run_install("--plan", "--recommended")
        self.assertEqual(rc, 0, out[-800:])
        self.assertNotIn(wall.WALL, out, "it stopped before step 1")
        self.assertIn("--recommended moves it", out)
        self.assertIn("--plan: nothing was changed. The same command without --plan applies it.", out)
        for name in LEFT_OUT:
            self.assertTrue((cfg / name).exists(), f"{name} stays under --plan")
        self.assertFalse((cfg / "installed.json").exists())

    def test_a_side_lane_booting_gives_the_boot_back_to_the_27b_lane(self):
        rc, out, cfg = self.run_install("--recommended", enabled=("qwen38-nemotron.service",))
        self.assertIn("--recommended on a box booting the Nemotron 3.5 Bolt lane: the 27B lane becomes the boot lane "
                      "again, as on a new box.", out)
        self.assertNotIn("The Nemotron 3.5 Bolt lane is this box's boot lane", out)

    def test_qwen38_recommended_takes_0_or_1(self):
        rc, out, cfg = self.run_install(env={"QWEN38_RECOMMENDED": "yes"})
        self.assertNotEqual(rc, 0)
        self.assertIn("QWEN38_RECOMMENDED takes 0 or 1 (got: yes)", out)


class TheOtherRunsTakeIt(Base):
    def test_the_flash_lanes_run_takes_its_default_target(self):
        for rec, line in (("1", "--recommended: the flash lane takes its default target, flash (RadixArk/"),
                          ("0", "Keeping the installed target model: flash-nvda (nvidia/Qwen3.8-Flash-Next-NVFP4)")):
            with self.subTest(QWEN38_RECOMMENDED=rec):
                rc, out, cfg = self.run_install(env={"QWEN38_SECONDARY": "flash", "QWEN38_RECOMMENDED": rec})
                self.assertIn(wall.WALL, out, out[-800:])
                self.assertIn(line, out)

    def test_the_main_run_hands_it_to_the_other_text_lanes_run(self):
        call = TEXT[TEXT.index('if env -u MODEL_CHOICE -u MODEL_REV'):]
        self.assertIn('QWEN38_SECONDARY="$SECOND_LANE" QWEN38_RECOMMENDED="$RECOMMENDED"', call[:400])

    def test_the_side_lanes_installers_take_their_default_checkpoint(self):
        def bash(script, rec):
            r = subprocess.run(["bash", "-c", "set -eu\n" + script], capture_output=True, text=True, timeout=30,
                               env={"PATH": "/usr/bin:/bin", "QWEN38_RECOMMENDED": rec})
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip()

        def lines(script, *patterns):
            text = (REPO / script).read_text().splitlines()
            return "\n".join(next(ln for ln in text if re.search(p, ln)) for p in patterns)

        image = lines("install-image.sh", r"^kept_flag\(\)", r'^MODEL="\$\{IMAGE_MODEL:-')
        video = lines("install-video.sh", r"^kept_flag\(\)", r'^VARIANT="\$\{VIDEO_VARIANT:-')
        kol = (REPO / "install-kolibri.sh").read_text()
        start = kol.index("# ./install.sh --recommended installs the default checkpoint")
        kblock = kol[start:kol.index("esac", start) + 4]
        for rec, want_image, want_video, want_kol in (("1", "Qwen/Qwen-Image-2.1", "fl2va", "fp8"),
                                                      ("0", TURBO, "ref2va", "nvfp4")):
            with self.subTest(QWEN38_RECOMMENDED=rec):
                self.assertEqual(bash(f'unit_flag(){{ echo {TURBO}; }}\n{image}\necho "$MODEL"', rec), want_image)
                self.assertEqual(bash(f'unit_flag(){{ echo ref2va; }}\n{video}\necho "$VARIANT"', rec), want_video)
                self.assertEqual(bash(f'KOLIBRI_NVFP4_REPO={NVFP4}; INSTALLED_MODEL={NVFP4}\ndie(){{ exit 3; }}\n'
                                      f'{kblock}\necho "$VARIANT"', rec), want_kol)


if __name__ == "__main__":
    unittest.main()
