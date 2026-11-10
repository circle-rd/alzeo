#!/usr/bin/env python3
"""tests/run_single.py runs every unittest module of tests/ beside the others, in groups or in one process.

Each fact its run stands for is proven here on a planted tree: a failing test, fewer tests run than declared,
two counts from one process, a skip nobody listed, whatever quotes unittest writes its reason in. That every
module is in a group is read on the real plan. And its known limit is shown as it is: what a module does to a
module of another group is seen by the one-process pass only, which is why that pass still runs every night
and before a release."""
import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("run_single", REPO / "tests" / "run_single.py")
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)

# the planted files' tests are spelled TEST_ here and test_ on disk: the CI counts that string in this
# file to know how many tests it declares
TEST_DEF = "def " + "test_"
OK = "import unittest\n\nclass T(unittest.TestCase):\n    def TEST_a(self):\n        pass\n\n    def TEST_b(self):\n        pass\n"
LEAKS = ("import os, unittest\n\nclass T(unittest.TestCase):\n    def TEST_a(self):\n"
         "        os.environ['LEFT_BY_A_MODULE'] = '1'\n")
READS = ("import os, unittest\n\nclass T(unittest.TestCase):\n    def TEST_a(self):\n"
         "        self.assertNotIn('LEFT_BY_A_MODULE', os.environ)\n")


def skipped(reason):
    return (f"import unittest\n\nclass T(unittest.TestCase):\n    @unittest.skip({reason!r})\n"
            "    def TEST_a(self):\n        pass\n")


class OnPlantedTrees(unittest.TestCase):
    def tree(self, files):
        repo = pathlib.Path(tempfile.mkdtemp(prefix="run-single-repo-"))
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        (repo / "tests").mkdir()
        for name, text in files.items():
            (repo / "tests" / name).write_text(text.replace("def TEST_", TEST_DEF))
        return repo

    def run_it(self, repo, *args, own="", **env):
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        said = io.StringIO()
        try:
            with contextlib.redirect_stdout(said):
                rc = rs.main(["--repo", str(repo), "--own", own, *args])
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return rc, said.getvalue()

    def test_a_clean_tree_passes_in_groups_and_in_one_process(self):
        repo = self.tree({"test_a.py": OK, "test_b.py": OK, "test_long.py": OK, "helper.py": "X = 1\n"})
        rc, said = self.run_it(repo, own="tests.test_long")
        self.assertEqual(rc, 0, said)
        self.assertIn("declared 6, ran 6 (3 processes", said)
        rc, said = self.run_it(repo, "--one-process", own="tests.test_long")
        self.assertEqual(rc, 0, said)
        self.assertIn("declared 6, ran 6 (one process", said)

    def test_a_failing_test_fails_the_run_and_is_named(self):
        fails = "import unittest\n\nclass T(unittest.TestCase):\n    def TEST_a(self):\n        self.assertEqual(1, 2)\n"
        repo = self.tree({"test_a.py": OK, "test_fails.py": fails})
        for shape in ([], ["--one-process"]):
            rc, said = self.run_it(repo, *shape)
            self.assertEqual(rc, 1, said)
            self.assertIn("the unittest suites failed", said)
            self.assertRegex(said, r"(?m)^FAIL: test_a \(tests\.test_fails\.T")

    def test_a_module_that_cannot_be_imported_fails_the_run(self):
        repo = self.tree({"test_a.py": OK, "test_broken.py": "import nothing_of_that_name\n" + OK})
        rc, said = self.run_it(repo)
        self.assertEqual(rc, 1, said)
        self.assertIn("nothing_of_that_name", said)

    def test_fewer_tests_run_than_declared_fails_the_run(self):
        # the first class is no TestCase: its test is declared and never runs
        hidden = "import unittest\n\nclass Forgotten:\n    def TEST_a(self):\n        pass\n\n\n" + OK.split("\n\n", 1)[1]
        repo = self.tree({"test_a.py": OK, "test_hidden.py": hidden})
        for shape in ([], ["--one-process"]):
            rc, said = self.run_it(repo, *shape)
            self.assertEqual(rc, 1, said)
            self.assertIn("unittest ran 2 of 3 declared tests" if not shape else "unittest ran 4 of 5 declared tests", said)

    def test_a_skip_passes_only_for_a_listed_reason(self):
        listed = self.tree({"test_a.py": OK, "test_skips.py": skipped("needs a real opencode: none here")})
        rc, said = self.run_it(listed)
        self.assertEqual(rc, 0, said)
        self.assertIn("skipped for a listed reason: 1", said)
        unlisted = self.tree({"test_a.py": OK, "test_skips.py": skipped("not today")})
        rc, said = self.run_it(unlisted)
        self.assertEqual(rc, 1, said)
        self.assertIn("tests skipped for a reason nobody listed", said)
        self.assertIn("not today", said)

    def test_a_reason_with_an_apostrophe_is_read_too(self):
        # unittest writes it between double quotes: the check read single ones alone and saw none of the 85
        # reasons of tests/ that hold an apostrophe (found in review, 2026-11-10)
        repo = self.tree({"test_a.py": OK, "test_skips.py": skipped("the runner's docker is absent")})
        rc, said = self.run_it(repo)
        self.assertEqual(rc, 1, said)
        self.assertIn("tests skipped for a reason nobody listed", said)
        self.assertIn("the runner's docker is absent", said)
        listed = self.tree({"test_a.py": OK, "test_skips.py": skipped("needs the pinned bytes cached (the box's cache)")})
        rc, said = self.run_it(listed)
        self.assertEqual(rc, 0, said)
        self.assertIn("skipped for a listed reason: 1 (0 of them", said)

    def test_a_case_for_the_twin_alone_is_skipped_where_the_proxy_is_a_process_and_nowhere_else(self):
        twin_only = ("import unittest\n\nclass ABox(unittest.TestCase):\n    def TEST_a(self):\n"
                     "        self.skipTest(\"the stop is made in this test's process\")\n\n\n"
                     "class ABoxInProcess(ABox):\n    def TEST_a(self):\n        pass\n")
        repo = self.tree({"test_a.py": OK, "test_twins.py": twin_only})
        rc, said = self.run_it(repo)
        self.assertEqual(rc, 0, said)
        self.assertIn("skipped for a listed reason: 1 (1 of them cases that only the in-process twin", said)
        # a twin that skips the case too: nothing runs it any more
        both = twin_only.replace("class ABoxInProcess(ABox):\n    def TEST_a(self):\n        pass\n",
                                 "class ABoxInProcess(ABox):\n    def TEST_a(self):\n        super().test_a()\n")
        repo = self.tree({"test_a.py": OK, "test_twins.py": both})
        rc, said = self.run_it(repo)
        self.assertEqual(rc, 1, said)
        self.assertRegex(said, r"tests\.test_twins\.ABoxInProcess\.test_a\) \.\.\. skipped")
        self.assertNotRegex(said.split("nobody listed:")[1], r"test_twins\.ABox\.test_a")

    def test_two_counts_from_one_process_fail_the_run(self):
        # a test that runs the tests again says "Ran" a second time (a fork whose child goes on, say): the last
        # count alone is the declared one, and the run would pass
        twice = "import sys\nsys.stderr.write('Ran 7 tests in 0.001s\\n')\n" + OK
        repo = self.tree({"test_a.py": OK, "test_twice.py": twice})
        for shape in ([], ["--one-process"]):
            rc, said = self.run_it(repo, *shape)
            self.assertEqual(rc, 1, said)
            self.assertIn("unittest said how many tests it ran 2 times, not once", said)

    def test_the_sdk_may_be_missing_off_the_runner_only(self):
        repo = self.tree({"test_a.py": OK, "test_sdk.py": skipped("typesafe-sdk is not installed")})
        rc, said = self.run_it(repo, GITHUB_ACTIONS="")
        self.assertEqual(rc, 0, said)
        rc, said = self.run_it(repo, GITHUB_ACTIONS="true")
        self.assertEqual(rc, 1, said)
        self.assertIn("typesafe-sdk is not installed", said)

    def test_what_a_module_leaves_is_seen_by_the_modules_of_its_group(self):
        # by name: a_leaks, m_between, z_reads; dealt in turn into two groups, a_leaks and z_reads share one
        repo = self.tree({"test_a_leaks.py": LEAKS, "test_m_between.py": OK, "test_z_reads.py": READS})
        self.assertEqual(rs.groups(rs.modules(repo), own=[]),
                         [["tests.test_a_leaks", "tests.test_z_reads"], ["tests.test_m_between"]])
        rc, said = self.run_it(repo)
        self.assertEqual(rc, 1, said)
        self.assertRegex(said, r"(?m)^FAIL: test_a \(tests\.test_z_reads\.T")

    def test_what_a_module_leaves_to_another_group_is_seen_by_the_one_process_pass_alone(self):
        # the limit of a run in groups, as it is: the two modules are in two groups, and only one process shows it
        repo = self.tree({"test_a_leaks.py": LEAKS, "test_z_reads.py": READS})
        self.assertEqual(rs.groups(rs.modules(repo), own=[]), [["tests.test_a_leaks"], ["tests.test_z_reads"]])
        rc, said = self.run_it(repo)
        self.assertEqual(rc, 0, said)
        rc, said = self.run_it(repo, "--one-process")
        self.assertEqual(rc, 1, said)
        self.assertRegex(said, r"(?m)^FAIL: test_a \(tests\.test_z_reads\.T")

    def test_a_longest_module_that_is_gone_stops_the_run(self):
        repo = self.tree({"test_a.py": OK})
        rc, said = self.run_it(repo, own="tests.test_renamed")
        self.assertEqual(rc, 1, said)
        self.assertIn("are gone: name them again in OWN", said)


class TheRealPlan(unittest.TestCase):
    def test_every_module_is_in_one_group_and_one_only(self):
        mods = rs.modules()
        plan = rs.groups(mods)
        dealt = [m for g in plan for m in g]
        self.assertEqual(sorted(dealt), sorted(mods), "every module, once")
        self.assertEqual(len(dealt), len(set(dealt)))
        self.assertGreater(len(mods), 150, "the modules of tests/ are found")
        self.assertEqual([g for g in plan if g[0] in rs.OWN], [[m] for m in rs.OWN], "each of the longest is a process of its own")
        self.assertEqual(len(plan), len(rs.OWN) + rs.REST)

    def test_the_groups_start_in_the_order_of_their_length(self):
        # the longest module, then the dealt groups, then the shorter modules of the longest: with four at a time,
        # dealt groups that started last waited for a place and set the length of the run (589 s for 497, 2026-11-10)
        self.assertEqual(rs.groups(["a", "b", "c", "long", "shorter"], own=["long", "shorter"], rest=2),
                         [["long"], ["a", "c"], ["b"], ["shorter"]])
        plan = rs.groups(rs.modules())
        self.assertEqual(plan[0], [rs.OWN[0]])
        self.assertEqual([len(g) > 1 for g in plan], [False] + [True] * rs.REST + [False] * (len(rs.OWN) - 1))

    def test_a_module_is_a_file_that_declares_a_test(self):
        mods = rs.modules()
        self.assertIn("tests.test_run_single", mods)
        self.assertNotIn("tests.run_single", mods)
        self.assertNotIn("tests.pairbox", mods)
        for m in mods:
            self.assertIn(TEST_DEF, (REPO / (m.replace(".", "/") + ".py")).read_text(), m)

    def test_ci_runs_the_groups_and_the_night_runs_one_process(self):
        ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
        night = (REPO / ".github" / "workflows" / "nightly.yml").read_text()
        self.assertIn("python3 tests/run_single.py\n", ci, "the single job runs the groups")
        self.assertNotIn("--one-process", ci.split("\n  single:\n", 1)[1].split("\n  coverage:\n", 1)[0])
        self.assertIn("python3 tests/run_single.py --one-process\n", night, "the night runs them all in one process")
        self.assertRegex(night, r"(?m)^  schedule:\n(?: *#.*\n)* +- cron: ", "and it is scheduled")


if __name__ == "__main__":
    unittest.main()
