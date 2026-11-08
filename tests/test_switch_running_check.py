#!/usr/bin/env python3
"""The text switch says which lane it stops, read from every lane's unit.

./switch-model.sh <text target> ends on the command that makes the switch effective: "stop <the lane
that serves> && start <the target>". It found the lane that serves among the units it names one by
one, and the Qwen3.8 Flash-Next FP4-Local lane was not among them: with that lane serving, a switch to the flash
lane printed "restart qwen38-flash.service", naming the wrong lane and hiding that the one serving
would stop (found in review, 2026-10-20). Every side lane's unit name the script declares must be
among those it checks. Read from the script as written."""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "switch-model.sh").read_text()


class TheRunningCheck(unittest.TestCase):
    def test_it_names_every_side_lanes_unit(self):
        start = TEXT.index('RUNNING=""\nsystemctl is-active --quiet "$OTHER_UNIT_NAME"')
        block = TEXT[start:TEXT.index("Switch queued", start)]
        checked = set(re.findall(r'systemctl is-active --quiet "\$(\w+)"', block))
        declared = {n for n in re.findall(r'^(\w+_UNIT_NAME)="qwen38-[\w-]+\.service"', TEXT, re.M)
                    if n not in ("SGL_UNIT_NAME", "FLASH_UNIT_NAME")}   # the text targets: OTHER_UNIT_NAME
        self.assertTrue(declared)
        self.assertEqual(declared - checked, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
