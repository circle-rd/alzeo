#!/usr/bin/env python3
"""A pin that moves takes its qwen38-pinned tag with it.

install.sh tags each digest it pulls, so that a docker image prune cannot delete a lane's
serving image. The tag of a pin a later release replaced was never removed, and kept the
retired image out of every prune: 30 to 39 GB per bumped pin (found in review,
2026-09-24). This runs install.sh's own function against a stub docker."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
LANES = (REPO / "lanes.sh").read_text()


def func(text, name):
    start = text.index(name + "() {")
    return text[start:text.index("\n}\n", start) + 3]


# unpin_stale reads a lane's tags with pin_tags_of, both in lanes.sh, which install.sh and the lanes'
# own installers source
FUNC = func(LANES, "pin_tags_of") + func(LANES, "unpin_stale")
UNTAG = func(LANES, "pin_tags_of") + func(LANES, "pin_untag")
# tags as pin_tag names them: the lane, a dash, the digest's first 12 hex digits
NEW, OLD, FL, FLL = "27b-0123456789ab", "27b-ba9876543210", "flash-aaaaaaaaaaaa", "flash-local-03a956544e9f"

DOCKER = r'''#!/bin/bash
D="$(dirname "$0")"
case "$1 $2" in
  "image inspect") grep -m1 "^$3 " "$D/refs" | cut -d' ' -f2 || exit 1; grep -q "^$3 " "$D/refs" ;;
  "images --no-trunc") cat "$D/tags" ;;
  "ps -aq") id="${4#ancestor=}"; grep -qx "$id" "$D/used" && echo c0ffee; exit 0 ;;
  "rmi "*) echo "$2" >> "$D/removed" ;;
  *) exit 2 ;;
esac
'''


def run(tags, used=(), current="lmsysorg/sglang@sha256:new", lane="27b", script_of=None):
    d = pathlib.Path(tempfile.mkdtemp(prefix="pin-tags-"))
    try:
        (d / "docker").write_text(DOCKER)
        (d / "docker").chmod(0o755)
        (d / "refs").write_text("lmsysorg/sglang@sha256:new sha256:NEW\n")
        (d / "tags").write_text("".join(f"{t} {i}\n" for t, i in tags))
        (d / "used").write_text("".join(f"{u}\n" for u in used))
        script = script_of or f"set -euo pipefail\n{FUNC}\nunpin_stale {lane} '{current}' || true\necho END\n"
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{d}:/usr/bin:/bin", "LANES_UNIT_DIR": str(d / "units")})
        removed = (d / "removed").read_text().split() if (d / "removed").exists() else []
        return r.stdout + r.stderr, removed
    finally:
        # each run left its folder in /tmp: 30 after a day of CI on the reference box (2026-10-12)
        shutil.rmtree(d, ignore_errors=True)


class RetiredPinsAreUntagged(unittest.TestCase):
    def test_the_install_calls_it_for_both_lanes(self):
        self.assertIn('unpin_stale "$LANE" "$PULLED_IMAGE" || true', TEXT)
        self.assertIn('unpin_stale 27b "$IMAGE" || true', TEXT)
        self.assertIn('unpin_stale flash "$FLASH_IMAGE" || true', TEXT)

    def test_a_retired_pin_of_the_lane_goes(self):
        out, removed = run([(f"qwen38-pinned:{NEW}", "sha256:NEW"), (f"qwen38-pinned:{OLD}", "sha256:OLD")])
        self.assertEqual(removed, [f"qwen38-pinned:{OLD}"], out)
        self.assertIn("END", out)

    def test_the_other_lane_and_images_in_use_stay(self):
        _, removed = run([(f"qwen38-pinned:{NEW}", "sha256:NEW"), (f"qwen38-pinned:{FL}", "sha256:FL"),
                          (f"qwen38-pinned:{OLD}", "sha256:OLD")], used=["sha256:OLD"])
        self.assertEqual(removed, [])

    def test_a_lane_whose_name_starts_with_this_ones_keeps_its_tag(self):
        # the flash lane's pins move; the Qwen3.8 Flash-Next FP4-Local lane's tag begins with flash- too, and
        # a glob on "flash-" untagged it, its 18 GB image then deleted by the next prune
        out, removed = run([(f"qwen38-pinned:{FL}", "sha256:NEW"), (f"qwen38-pinned:{FLL}", "sha256:FLL"),
                            ("qwen38-pinned:flash-old", "sha256:ODD")], lane="flash")
        self.assertEqual(removed, [], out)

    def test_nothing_is_touched_when_the_current_pin_is_absent(self):
        _, removed = run([(f"qwen38-pinned:{OLD}", "sha256:OLD")], current="lmsysorg/sglang@sha256:missing")
        # the stub knows only the new ref, so an absent current pin cannot be inspected
        self.assertEqual(removed, [])


class TheUninstallUntagsItsOwnTagOnly(unittest.TestCase):
    def test_pin_untag_takes_the_lanes_tags_and_no_other(self):
        script = f"set -euo pipefail\n{UNTAG}\npin_untag flash-local\npin_untag flash\necho END\n"
        tags = [(f"qwen38-pinned:{FLL}", "sha256:FLL"), (f"qwen38-pinned:{FL}", "sha256:FL"),
                (f"qwen38-pinned:{NEW}", "sha256:NEW")]
        out, removed = run(tags, script_of=script)
        # flash-local's own tag, then flash's own: the 27B's stays, and neither call took the other's
        self.assertEqual(removed, [f"qwen38-pinned:{FLL}", f"qwen38-pinned:{FL}"], out)
        self.assertIn("END", out)

    def test_the_flash_local_install_takes_its_retired_pins_with_it(self):
        # it tagged its image at every run and never untagged one it no longer pins: each bump of
        # the pin would have left 18 GB no prune reclaims (found in review, 2026-10-20)
        text = (REPO / "install-flash-local.sh").read_text()
        tag = text.index('pin_tag flash-local "$FLASH_LOCAL_IMAGE"')
        self.assertIn('unpin_stale flash-local "$FLASH_LOCAL_IMAGE" || true', text[tag:tag + 200])
        self.assertNotIn("unpin_stale() {", TEXT, "one definition, in lanes.sh")

    def test_the_flash_local_uninstall_calls_it_and_prunes_no_shared_repository(self):
        text = (REPO / "install-flash-local.sh").read_text()
        self.assertIn("pin_untag flash-local", text)
        # pruning qwen38-pinned (the repository every lane's tag lives in) untags every pin
        self.assertNotIn("vlane_uninstall_runtime qwen38-pinned", text)

if __name__ == "__main__":
    unittest.main(verbosity=2)
