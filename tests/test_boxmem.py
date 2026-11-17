#!/usr/bin/env python3
"""A box's memory class comes from its MemTotal, its name from its DMI.

The 64 GB DGX Spark ships from 2026-10-23, from NVIDIA's partners only, and install.sh refused it
("This config needs a ~121 GB unified-memory machine"). boxmem.py says which class a box is of,
which lanes fit it, and the name its maker sells it under, for install.sh, the lane installers and
the cockpit alike. These hold its thresholds to the boxes they were set from: the reference box,
a 128 GB box on the 7.0 kernel without kho=off (forum t/383222), and a 64 GB budget."""
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("boxmem", REPO / "boxmem.py")
boxmem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boxmem)

KIB = 1024
GIB_KB = 1024 * 1024          # one GiB, in /proc/meminfo's kB (KiB)
REFERENCE_KB = 127533104      # the reference box and the second GX10, 121.6 GiB (2026-10-23)
KERNEL_70_KB = 117723484      # a 128 GB box on 7.0.0-1019 without kho=off, 112.3 GiB (t/383222)
BUDGET_64_KB = int(57.6 * GIB_KB)   # the reference box with 64 GiB held aside


class Base(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="boxmem-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.d)]))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("QWEN38_")}
        # the in-process checks read os.environ: none of an exported QWEN38_* reaches them, and
        # all of it is back after each test (a pop lost it for the tests after, found in review,
        # 2026-10-23)
        patch = mock.patch.dict(os.environ, clear=True, values=self.env)
        patch.start()
        self.addCleanup(patch.stop)

    def meminfo(self, kb):
        p = self.d / f"meminfo-{kb}"      # one file per size: a test that reads two keeps both
        p.write_text(f"MemTotal:       {kb} kB\nMemFree:        1000 kB\nMemAvailable:   1000 kB\n")
        return str(p)

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, "-I", str(REPO / "boxmem.py"), *args],
                              capture_output=True, text=True, env={**self.env, **(env or {})})


class MemClass(Base):
    def test_the_128_gb_boxes(self):
        for kb in (REFERENCE_KB, KERNEL_70_KB, boxmem.MIN_128_GIB * GIB_KB):
            with self.subTest(kb=kb):
                self.assertEqual(boxmem.mem_class(self.meminfo(kb)), "128")

    def test_the_64_gb_budget_and_its_bounds(self):
        for kb in (BUDGET_64_KB, boxmem.MIN_64_GIB * GIB_KB, boxmem.MIN_128_GIB * GIB_KB - 1):
            with self.subTest(kb=kb):
                self.assertEqual(boxmem.mem_class(self.meminfo(kb)), "64")

    def test_below_the_64_gb_class_nothing(self):
        self.assertIsNone(boxmem.mem_class(self.meminfo(boxmem.MIN_64_GIB * GIB_KB - 1)))
        empty = self.d / "empty"
        empty.write_text("MemFree: 1 kB\n")
        self.assertIsNone(boxmem.mem_class(str(empty)))

    def test_forced_for_the_proofs_and_only_to_a_class(self):
        os.environ["QWEN38_MEMORY_CLASS"] = "64"
        self.assertEqual(boxmem.mem_class(self.meminfo(REFERENCE_KB)), "64")
        os.environ["QWEN38_MEMORY_CLASS"] = "96"
        with self.assertRaises(ValueError):
            boxmem.mem_class(self.meminfo(REFERENCE_KB))

    def test_the_command_line(self):
        env = {"QWEN38_MEMINFO": self.meminfo(BUDGET_64_KB)}
        self.assertEqual(self.cli("class", env=env).stdout.strip(), "64")
        self.assertEqual(self.cli("gib", env=env).stdout.strip(), "57.6")
        small = {"QWEN38_MEMINFO": self.meminfo(16 * GIB_KB)}
        self.assertEqual(self.cli("class", env=small).stdout.strip(), "none")
        ref = {"QWEN38_MEMINFO": self.meminfo(REFERENCE_KB)}
        self.assertEqual(self.cli("gib", env=ref).stdout.strip(), "121.6")


class Fits(Base):
    def test_every_lane_fits_the_128_gb_class(self):
        for lane in boxmem.LANES:
            with self.subTest(lane=lane):
                self.assertTrue(boxmem.fits(lane, "128"))

    def test_the_64_gb_class_takes_the_27b_bolt_and_the_cpu_lanes_only(self):
        got = {lane for lane in boxmem.LANES if boxmem.fits(lane, "64")}
        self.assertEqual(got, {"27b", "nemotron", "speech", "embedding"})

    def test_no_lane_fits_a_box_without_a_class(self):
        self.assertFalse(any(boxmem.fits(lane, None) for lane in boxmem.LANES))

    def test_a_name_that_is_no_lane_is_an_error_not_a_no(self):
        with self.assertRaises(ValueError):
            boxmem.fits("27B", "128")

    def test_every_target_belongs_to_a_lane(self):
        self.assertTrue(set(boxmem.TARGETS.values()) <= set(boxmem.LANES))
        switch = (REPO / "switch-model.sh").read_text()
        listed = re.search(r'case "\$CHOICE" in ([a-z0-9|-]+)\) ;;', switch).group(1).split("|")
        self.assertEqual(set(listed), set(boxmem.TARGETS), "a target switch-model.sh takes is not in TARGETS")

    def test_a_64_gb_box_takes_the_27b_in_nvfp4_only(self):
        got = {t for t in boxmem.TARGETS if boxmem.target_fits(t, "64")}
        self.assertEqual(got, {"stock", "uncensored", "arete", "nemotron"})
        self.assertTrue(all(boxmem.target_fits(t, "128") for t in boxmem.TARGETS))
        with self.assertRaises(ValueError):
            boxmem.target_fits("27b", "128")

    def test_the_command_line_exits_by_the_answer(self):
        env = {"QWEN38_MEMINFO": self.meminfo(BUDGET_64_KB)}
        self.assertEqual(self.cli("fits", "27b", env=env).returncode, 0)
        self.assertEqual(self.cli("fits", "flash-local", env=env).returncode, 1)
        self.assertEqual(self.cli("fits", "flash-local", env={"QWEN38_MEMINFO": self.meminfo(REFERENCE_KB)}).returncode, 0)
        self.assertEqual(self.cli("fits", "not-a-lane", env=env).returncode, 2)
        self.assertEqual(self.cli("fits-target", "stock", env=env).returncode, 0)
        self.assertEqual(self.cli("fits-target", "fp8", env=env).returncode, 1)
        self.assertEqual(self.cli("fits-target", "fp8", "--class", "128", env=env).returncode, 0)
        self.assertEqual(self.cli("fits", "nemotron", "--class", "none", env=env).returncode, 1)


class Machine(Base):
    def dmi(self, **keys):
        d = self.d / "dmi"
        d.mkdir(exist_ok=True)
        for k, v in keys.items():
            (d / k).write_text(v + "\n")
        return str(d)

    def test_the_gx10_by_the_name_asus_sells_it_under(self):
        d = self.dmi(sys_vendor="ASUSTeK COMPUTER INC.", product_name="GX10", product_family="DGX Spark")
        self.assertEqual(boxmem.machine(d), {"vendor": "ASUSTeK COMPUTER INC.", "product": "GX10",
                                             "family": "DGX Spark", "name": "ASUS Ascent GX10"})

    def test_a_box_not_read_here_is_named_by_its_dmi_as_it_is(self):
        d = self.dmi(sys_vendor="Example Maker", product_name="Box One")
        self.assertEqual(boxmem.machine(d)["name"], "Example Maker Box One")

    def test_no_dmi_no_name(self):
        self.assertIsNone(boxmem.machine(str(self.d / "absent"))["name"])
        r = self.cli("machine", env={"QWEN38_DMI_DIR": str(self.d / "absent")})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "unknown"))


if __name__ == "__main__":
    unittest.main()
