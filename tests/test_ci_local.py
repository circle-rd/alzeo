#!/usr/bin/env python3
"""ci-local.sh runs the workflow's jobs the way GitHub does.

A job that needs others runs once they have ended, and only when every one of them passed:
checks, the job the run's status goes by, ran beside the others and said "Every job passed"
before any of them had ended (2026-10-28), and said it as well after one of them failed. The
directory the jobs' worktrees go in goes with the run. A fragment runs in the checkout as it
is, a full run hands its steps the checkout's test venv, and a stop stops every job (code
review, 2026-10-28). Each case runs ci-local.sh on a repository of its own, made here."""
import os
import pathlib
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "ci-local.sh"
# ci-local.sh reads the workflow with PyYAML, in the python3 it finds first on PATH
HAVE_YAML = subprocess.run(["python3", "-c", "import yaml"], capture_output=True).returncode == 0

WORKFLOW = """\
name: ci
on: push
jobs:
  slow:
    runs-on: ubuntu-24.04-arm
    steps:
      - name: The slow one
        run: sleep 0.5 && {slow}
  quick:
    runs-on: ubuntu-24.04-arm
    steps:
      - name: The quick one
        run: "true"
  checks:
    needs: [slow, quick]
    runs-on: ubuntu-24.04-arm
    steps:
      - name: Every job passed
        run: echo slow and quick passed
"""


@unittest.skipUnless(HAVE_YAML, "ci-local.sh reads the workflow with PyYAML, which python3 lacks here")
class TheJobThatNeedsTheOthers(unittest.TestCase):
    def make_repo(self, workflow, files=()):
        """A repository with this workflow and these files, all committed, and an empty TMPDIR."""
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        repo, tmp = root / "repo", root / "tmp"
        (repo / ".github" / "workflows").mkdir(parents=True)
        tmp.mkdir()
        (repo / ".github" / "workflows" / "ci.yml").write_text(workflow)
        for rel, text in dict(files).items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text)
            if rel.endswith(".sh"):
                os.chmod(repo / rel, 0o755)
        self.git = ["git", "-C", str(repo), "-c", "user.name=ci", "-c", "user.email=ci@example.invalid",
                    "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(self.git + ["add", "."], check=True)
        subprocess.run(self.git + ["commit", "-qm", "ci"], check=True)
        return repo, tmp

    def env(self, tmp, serial=False, **extra):
        # CI_LOCAL_WORKFLOW too: a case asks for another workflow file by naming it, never by inheriting it
        env = {k: v for k, v in os.environ.items()
               if k not in ("CI_LOCAL_SERIAL", "QWEN38_TEST_PYTHON", "CI_LOCAL_WORKFLOW")}
        env.update(TMPDIR=str(tmp), **extra)
        if serial:
            env["CI_LOCAL_SERIAL"] = "1"
        return env

    def run_ci(self, slow="true", serial=False):
        repo, tmp = self.make_repo(WORKFLOW.format(slow=slow))
        r = subprocess.run(["bash", str(SCRIPT)], cwd=repo, env=self.env(tmp, serial), capture_output=True,
                           text=True, timeout=120)
        return r, tmp

    def line(self, out, name):
        """The index of the line that says how the step named name ended."""
        hits = [i for i, ln in enumerate(out.splitlines()) if name in ln]
        self.assertEqual(len(hits), 1, out)
        return hits[0]

    def test_it_runs_once_they_have_ended(self):
        for serial in (False, True):
            with self.subTest(serial=serial):
                r, _ = self.run_ci(serial=serial)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                last = self.line(r.stdout, "ok   Every job passed")
                self.assertGreater(last, self.line(r.stdout, "ok   The slow one"), r.stdout)
                self.assertGreater(last, self.line(r.stdout, "ok   The quick one"), r.stdout)
                self.assertIn("CI local: 3 ok, 0 echecs, 0 sautes", r.stdout)

    def test_it_is_not_run_when_one_of_them_failed(self):
        for serial in (False, True):
            with self.subTest(serial=serial):
                r, _ = self.run_ci(slow="false", serial=serial)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertNotIn("ok   Every job passed", r.stdout)
                self.assertIn("--   Every job passed (not run: slow did not pass)", r.stdout)
                # and said again next to the count, where a reader of the run looks
                self.assertIn("not run, a job they need did not pass: Every job passed\n\nCI local: 1 ok, "
                              "1 echecs, 0 sautes", r.stdout)

    def test_the_directory_of_the_worktrees_goes_with_the_run(self):
        r, tmp = self.run_ci()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(p.name for p in tmp.iterdir()), [], r.stdout)

    def run_with_git(self, add):
        """A full run whose git, first on PATH, runs the shell lines add on each `git worktree add`
        (REAL_GIT is the real one) and hands it, and every other call, to the real git."""
        repo, tmp = self.make_repo(WORKFLOW.format(slow="true"))
        bindir = repo.parent / "bin"
        bindir.mkdir()
        (bindir / "git").write_text(f'#!/bin/sh\nREAL_GIT="{shutil.which("git")}"\n'
                                    f'if [ "$1 $2" = "worktree add" ]; then\n{add}\nfi\nexec "$REAL_GIT" "$@"\n')
        os.chmod(bindir / "git", 0o755)
        env = self.env(tmp, PATH=str(bindir) + os.pathsep + os.environ["PATH"])
        r = subprocess.run(["bash", str(SCRIPT)], cwd=repo, env=env, capture_output=True, text=True,
                           timeout=120)
        return r, tmp

    def test_the_worktrees_are_added_one_after_the_other(self):
        # git worktree add, side by side, reads what the other adds are writing: twice on 2026-11-07 a
        # job's add failed on another's worktrees/<job>/commondir. Each add holds a mark here for 0.3 s,
        # and one that finds it taken writes itself down.
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        mark, beside = root / "adding", root / "added-beside-another"
        r, _ = self.run_with_git(f'if mkdir "{mark}" 2>/dev/null; then\n'
                                    f'  sleep 0.3; "$REAL_GIT" "$@"; rc=$?; rmdir "{mark}"; exit $rc\nfi\n'
                                    f'echo "$*" >> "{beside}"')
        self.assertFalse(beside.exists(), beside.read_text() if beside.exists() else "")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CI local: 3 ok, 0 echecs, 0 sautes", r.stdout)

    def test_a_worktree_that_cannot_be_added_is_its_jobs_failure(self):
        # the other jobs ran to their end, then the run ended in a traceback and said no count (2026-11-07)
        r, tmp = self.run_with_git('for a in "$@"; do case "$a" in\n'
                                      '  */quick) echo "fatal: no worktree for quick here" >&2; exit 128 ;;\n'
                                      'esac; done')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("  [quick] FAIL git worktree add (rc=128): none of quick's steps ran\n"
                      "       fatal: no worktree for quick here\n", r.stdout)
        self.assertIn("  [slow] ok   The slow one", r.stdout)
        self.assertIn("--   Every job passed (not run: quick did not pass)", r.stdout)
        self.assertIn("CI local: 1 ok, 1 echecs, 0 sautes", r.stdout)
        self.assertEqual(sorted(p.name for p in tmp.iterdir()), [], r.stdout)

    def test_the_job_that_needs_the_others_is_not_run_without_its_worktree(self):
        # run without one, its steps would run in this checkout: the tree a full run must leave alone
        r, tmp = self.run_with_git('for a in "$@"; do case "$a" in\n'
                                   '  */checks) echo "fatal: no worktree for checks here" >&2; exit 128 ;;\n'
                                   'esac; done')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("  [checks] FAIL git worktree add (rc=128): none of checks's steps ran\n", r.stdout)
        self.assertNotIn("ok   Every job passed", r.stdout)
        self.assertIn("CI local: 2 ok, 1 echecs, 0 sautes", r.stdout)
        self.assertEqual(sorted(p.name for p in tmp.iterdir()), [], r.stdout)

    def test_another_workflow_file_is_run_the_same_way_when_named(self):
        # the night's pass (.github/workflows/nightly.yml), which a release runs here before it is pushed
        night = ("name: nightly\non:\n  schedule:\n    - cron: '43 2 * * *'\njobs:\n  one-process:\n"
                 "    runs-on: ubuntu-24.04-arm\n    steps:\n      - name: The night's one\n"
                 "        run: 'test -z \"${CI_LOCAL_WORKFLOW:-}\"'\n")
        repo, tmp = self.make_repo(WORKFLOW.format(slow="false"), files={".github/workflows/nightly.yml": night})
        r = subprocess.run(["bash", str(SCRIPT)], cwd=repo, capture_output=True, text=True, timeout=120,
                           env=self.env(tmp, CI_LOCAL_WORKFLOW=".github/workflows/nightly.yml"))
        # its step passes only without the variable: the name of the workflow is this run's, not its steps'
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[one-process] ok   The night's one", r.stdout)
        self.assertIn("CI local: 1 ok, 0 echecs, 0 sautes", r.stdout)
        self.assertNotIn("The slow one", r.stdout, "ci.yml's jobs are not this run's")
        r = subprocess.run(["bash", str(SCRIPT)], cwd=repo, capture_output=True, text=True, timeout=120,
                           env=self.env(tmp, CI_LOCAL_WORKFLOW=""))
        self.assertEqual(r.returncode, 1, "without the name, ci.yml, whose slow job fails here")
        self.assertIn("FAIL The slow one", r.stdout)

    def test_these_cases_run_the_same_inside_the_nights_pass(self):
        # this file is a step of that pass, which runs with the variable set: each case here plants a repository
        # with a ci.yml alone, and ran its ci-local.sh on a nightly.yml that is not there (13 failures, 2026-11-10)
        old = os.environ.get("CI_LOCAL_WORKFLOW")
        os.environ["CI_LOCAL_WORKFLOW"] = ".github/workflows/nightly.yml"
        try:
            r, _ = self.run_ci()
        finally:
            if old is None:
                del os.environ["CI_LOCAL_WORKFLOW"]
            else:
                os.environ["CI_LOCAL_WORKFLOW"] = old
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CI local: 3 ok, 0 echecs, 0 sautes", r.stdout)

    def test_a_fragment_runs_in_the_checkout_as_it_is(self):
        # what one runs while editing: the worktrees of HEAD made it refuse every tree with a change
        repo, tmp = self.make_repo(WORKFLOW.format(slow="true"), {"notes.txt": "one\n"})
        (repo / "notes.txt").write_text("two\n")
        r = subprocess.run(["bash", str(SCRIPT), "The quick one"], cwd=repo, env=self.env(tmp),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("  ok   The quick one", r.stdout)
        self.assertNotIn("worktree", r.stdout)

    def test_a_full_run_hands_its_steps_the_checkouts_test_venv(self):
        # .venv-test is not in git, so not in a worktree of HEAD, where tests/testpy.sh looks for it
        workflow = ("name: ci\non: push\njobs:\n  venv:\n    runs-on: ubuntu-24.04-arm\n    steps:\n"
                    "      - name: The checkout's test venv\n        run: |\n"
                    "          p=$(./tests/testpy.sh fakepkg)\n"
                    "          case \"$p\" in */.venv-test/bin/python) ;; *) echo \"found $p\"; exit 1 ;; esac\n")
        repo, tmp = self.make_repo(workflow, {".gitignore": ".venv-test/\n",
                                              "tests/testpy.sh": (REPO / "tests" / "testpy.sh").read_text()})
        venv_python = repo / ".venv-test" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\nexit 0\n")    # it "has" any package it is asked for
        os.chmod(venv_python, 0o755)
        r = subprocess.run(["bash", str(SCRIPT)], cwd=repo, env=self.env(tmp), capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ok   The checkout's test venv", r.stdout)

    def test_a_stop_stops_every_job(self):
        # a stop through the process group killed the steps running, and each job went on to its next
        workflow = ("name: ci\non: push\njobs:\n"
                    "  long:\n    runs-on: ubuntu-24.04-arm\n    steps:\n"
                    "      - name: The long one\n        run: sleep 30\n"
                    "      - name: After the long one\n        run: sleep 2 && touch \"$MARK\"\n"
                    "  other:\n    runs-on: ubuntu-24.04-arm\n    steps:\n"
                    "      - name: The other one\n        run: sleep 30\n")
        repo, tmp = self.make_repo(workflow)
        mark = tmp.parent / "after-the-stop"
        p = subprocess.Popen(["bash", str(SCRIPT)], cwd=repo, env=self.env(tmp, MARK=str(mark)),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)

        def kill_group():
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.addCleanup(kill_group)
        self.assertIn("2 jobs side by side", p.stdout.readline())
        time.sleep(1)
        os.killpg(p.pid, signal.SIGTERM)
        t0 = time.monotonic()
        out = p.communicate(timeout=60)[0]      # to the end of python's output, bash being gone
        time.sleep(3)                           # longer than the step after the long one takes
        self.assertFalse(mark.exists(), "a step started after the stop:\n" + out)
        self.assertLess(time.monotonic() - t0, 30, out)
        self.assertIn("[long] --   The long one (stopped)", out)
        self.assertIn("[other] --   The other one (stopped)", out)
        self.assertEqual(subprocess.run(self.git + ["worktree", "list"], capture_output=True,
                                        text=True).stdout.count("\n"), 1, "a worktree was left")
        self.assertEqual(sorted(p_.name for p_ in tmp.iterdir()), [], out)


    def test_a_checkout_with_its_test_venv_is_clean_enough_to_run(self):
        # review of PR #60 (2026-10-29, decision 780): a full run looks for .venv-test and .venv and refuses a tree
        # with changes, and .gitignore had .venv-test alone: a checkout with a .venv was refused for it
        repo, tmp = self.make_repo(WORKFLOW.format(slow="true"),
                                   files={".gitignore": (REPO / ".gitignore").read_text()})
        for venv in (".venv", ".venv-test"):
            py = repo / venv / "bin" / "python"
            py.parent.mkdir(parents=True)
            py.write_text("#!/bin/sh\nexec python3 \"$@\"\n")
            py.chmod(0o755)
        r = subprocess.run(["bash", str(SCRIPT)], cwd=repo, env=self.env(tmp), capture_output=True, text=True,
                           timeout=120)
        self.assertNotIn("the tree is not clean", r.stdout, r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_stop_of_python_alone_ends_a_step_whose_bash_has_a_child(self):
        # review of PR #60 (2026-10-29, decision 780): stop() killed the step's bash by its pid alone; a child that
        # bash had started kept the pipes open and communicate() waited for it, so a SIGTERM to python (not to its
        # group) ended the run only when that child ended. Here bash has a child: "sleep 30; true" is no exec.
        workflow = ("name: ci\non: push\njobs:\n"
                    "  long:\n    runs-on: ubuntu-24.04-arm\n    steps:\n"
                    "      - name: The long one\n        run: sleep 30; true\n"
                    "  other:\n    runs-on: ubuntu-24.04-arm\n    steps:\n"
                    "      - name: The other one\n        run: sleep 30; true\n")
        repo, tmp = self.make_repo(workflow)
        p = subprocess.Popen(["bash", str(SCRIPT)], cwd=repo, env=self.env(tmp), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)

        def kill_all():
            for pid in [p.pid] + self.descendants(p.pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(kill_all)
        self.assertIn("2 jobs side by side", p.stdout.readline())
        time.sleep(1)
        python = [pid for pid in self.descendants(p.pid) if self.comm(pid) == "python3"]
        self.assertTrue(python, "no python under ci-local.sh")
        os.kill(python[0], signal.SIGTERM)
        t0 = time.monotonic()
        out = p.communicate(timeout=60)[0]
        self.assertLess(time.monotonic() - t0, 10, "the stop waited for the steps' children:\n" + out)
        self.assertIn("[long] --   The long one (stopped)", out)
        self.assertIn("[other] --   The other one (stopped)", out)

    @staticmethod
    def descendants(pid):
        """Every process under pid, from /proc (no pgrep: it would match its own command line)."""
        kids = {}
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    with open(f"/proc/{d}/stat") as f:
                        ppid = int(f.read().rsplit(")", 1)[1].split()[1])
                except (OSError, ValueError, IndexError):
                    continue
                kids.setdefault(ppid, []).append(int(d))
        out, todo = [], [pid]
        while todo:
            for k in kids.get(todo.pop(), []):
                out.append(k)
                todo.append(k)
        return out

    @staticmethod
    def comm(pid):
        try:
            with open(f"/proc/{pid}/comm") as f:
                return f.read().strip()
        except OSError:
            return ""


if __name__ == "__main__":
    unittest.main(verbosity=2)
