#!/usr/bin/env python3
"""The 27B lane starts SGLang without torch.compile (decision 1102, v1.44.0).

Measured on the reference box on 2026-11-05, the official 27B checkpoint and its DFlash2 draft behind the same SGLang
image (v0.5.22), the lane's flags otherwise as they ship, the engine alone on the box, one after the other within the
hour (the GPU at 52 to 89 °C, 80 °C median, both times): with --enable-torch-compile --torch-compile-max-bs 4, the
official benchmark (8,192 tokens in, 1,024 out, three runs each, medians) gave 42.9 tok/s at one request and 77.0 at
four; without it, 44.0 and 80.6, GSM8K 0.992 against 0.988 (250 questions), bench.sh's spreads overlapping (65.2 and
68.5 against 71.0 and 67.4), and the engine was ready in 208 s against 371. With it, SGLang v0.5.23 decoded ten times
slower at one request (3.6 against 37.3 tok/s, the same image without it as fast as v0.5.22), so the lane could not
move to it. None of the 27B's launches (its three units and run.sh) passes either flag now; the 64 GB unit follows
without a measure of its own, as no 64 GB box was at hand.
"""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
LAUNCHES = ("qwen38-sglang.service.template", "qwen38-sglang-1m.service.template",
            "qwen38-sglang-64.service.template", "run.sh")
FLAGS = re.compile(r"(?<![\w-])--(enable-torch-compile|torch-compile-max-bs)(?![\w-])")


def code(name):
    """The file without its comment lines: a flag named in prose is not passed."""
    return "\n".join(ln for ln in (REPO / name).read_text().splitlines() if not ln.lstrip().startswith("#"))


class The27BCompilesNothing(unittest.TestCase):
    def test_no_27b_launch_passes_torch_compile(self):
        for name in LAUNCHES:
            with self.subTest(name=name):
                self.assertEqual(FLAGS.findall(code(name)), [], f"{name} still starts SGLang with torch.compile")

    def test_each_launch_still_starts_sglang(self):
        # the test reads the files it means to: each one launches SGLang with the 27B's own flags
        for name in LAUNCHES:
            with self.subTest(name=name):
                self.assertIn("--num-continuous-decode-steps 2", code(name))


if __name__ == "__main__":
    unittest.main()
