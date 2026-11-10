#!/usr/bin/env python3
"""A run of the installer that ends whole says once, in its last line, that a star helps (v1.46.0).

Hasan, 2026-11-09: the project is found by what its users say of it, and nothing in it said so. The installer now
prints one line at the end of an install and of every update, and that is all: no prompt, no reminder, nothing the
run waits for. The run started again for another lane (QWEN38_SECONDARY) is not the end of the run and says nothing.
The function runs here as install.sh has it, and each of the four endings of a run is read in the script itself.
"""
import pathlib
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
LINES = TEXT.splitlines()
URL = "https://github.com/hasso5703/alzeo"


def star_line(secondary=""):
    start = TEXT.index("star_line() {")
    func = TEXT[start:TEXT.index("\n}\n", start) + 3]
    return subprocess.run(["bash", "-c", f"set -euo pipefail\nSECONDARY='{secondary}'\n{func}star_line\n"],
                          capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})


class TheLastLineOfARun(unittest.TestCase):
    def test_one_line_that_names_the_repository(self):
        r = star_line()
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(len(r.stdout.splitlines()), 1, r.stdout)
        self.assertTrue(r.stdout.rstrip("\n").endswith(URL), r.stdout)
        self.assertIn("a star helps", r.stdout)

    def test_the_run_for_another_lane_says_nothing(self):
        r = star_line(secondary="flash")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_it_is_called_four_times_and_nowhere_else(self):
        """Not before the exit of --plan, of --help or of the run for another lane: only where a run was recorded."""
        calls = [i for i, ln in enumerate(LINES) if ln.strip() == "star_line"]
        self.assertEqual(len(calls), 4)
        for i in calls:
            self.assertTrue(LINES[i - 1].strip().startswith("record_install "), LINES[i - 1])
            self.assertEqual(LINES[i + 1].strip(), "exit 0")

    def test_every_ending_of_a_run_says_it_last(self):
        for how in ("no-service", "side-boot", "no-start", "full"):
            with self.subTest(how=how):
                calls = [i for i, ln in enumerate(LINES) if ln.strip() == f"record_install {how}"]
                self.assertEqual(len(calls), 1, f"record_install {how} is called once")
                self.assertEqual([ln.strip() for ln in LINES[calls[0] + 1:calls[0] + 3]], ["star_line", "exit 0"])

    def test_no_ending_is_left_out(self):
        ends = {ln.strip().split()[1] for ln in LINES if ln.strip().startswith("record_install ")}
        self.assertEqual(ends, {"no-service", "side-boot", "no-start", "full"},
                         "a new way for a run to end whole says the line too")

    def test_it_asks_nothing_and_is_said_nowhere_else(self):
        start = TEXT.index("star_line() {")
        func = TEXT[start:TEXT.index("\n}\n", start) + 3]
        self.assertNotRegex(func, r"\bread\b|\bsleep\b|curl|xdg-open", "one echo, nothing the run waits for")
        self.assertEqual(TEXT.count("a star helps"), 1)


if __name__ == "__main__":
    unittest.main()
