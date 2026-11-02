#!/usr/bin/env python3
"""No test file has the same name in tests/ and in dashboard/tests/ (2026-11-02).

Neither directory is a package (no __init__.py), so "tests" is a namespace package, and Python reads its
path again whenever sys.path changes. tests/test_proxy_resent.py puts dashboard/ first on sys.path when it
is imported, and the "No test file in tests/ is left unrun" step imports every module of tests/ in one
process: from then on, tests.<name> is dashboard/tests/<name> whenever both directories hold that name.
On 2026-11-02 the Qwen4 27B lane's tests/test_qwen4_lane.py ran the cockpit's 12 tests that way and none
of its own 19 (2449 tests declared, 2442 run), and tests/test_ornith_lane.py had the same twin, spared
only because it sorts before test_proxy_resent.py. A twin is refused here instead of being found by luck.
"""
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]


class NoTestFileHasATwin(unittest.TestCase):
    def test_no_name_is_in_both_test_directories(self):
        root = {p.name for p in (REPO / "tests").glob("test_*.py")}
        cockpit = {p.name for p in (REPO / "dashboard" / "tests").glob("test_*.py")}
        self.assertTrue(root and cockpit, "both test directories hold test files")
        self.assertEqual(sorted(root & cockpit), [],
                         "rename one of each pair: in one process tests.<name> may import the cockpit's file")


if __name__ == "__main__":
    unittest.main()
