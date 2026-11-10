#!/usr/bin/env bash
# Run this repo's CI shell steps locally, exactly as GitHub does (bash -e), before
# pushing. A step is reported as SKIPPED when it calls a tool missing on this box, or
# when a package its runner-only `pip install` line provides is missing here.
# Usage: ./ci-local.sh [step-name-substring]
# A full run takes the workflow's jobs side by side, as on GitHub, each in a checkout of its
# own: a detached worktree of HEAD, so the tree has to be clean (what runs is HEAD^{tree}).
# Within a job the steps run in order. A job that needs others (checks) runs once they
# have ended, and only when every one of them passed, as on GitHub. A fragment, and any
# run with CI_LOCAL_SERIAL=1, runs every step it takes in this checkout as it is, one after
# the other.
# CI_LOCAL_WORKFLOW names another workflow file than .github/workflows/ci.yml, run the same way:
# CI_LOCAL_WORKFLOW=.github/workflows/nightly.yml ./ci-local.sh is the night's pass, every unittest
# module of tests/ in one process, which a release runs here before it is pushed.
# Exit status: 1 when a step failed, 2 when a full run finds changes nobody committed, 3
# when nothing ran (a filter that matched no step, or only skipped ones), 0 otherwise.
set -u
FILTER="${1:-}"
python3 -u - "$FILTER" <<'PY'
import os, re, signal, subprocess, sys, yaml, shutil, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
flt = sys.argv[1]
# A fragment is what one runs while editing, so it runs in this checkout as it is: worktrees of
# HEAD made it refuse every tree with a change in it (code review, 2026-10-28).
serial = os.environ.get("CI_LOCAL_SERIAL") == "1" or bool(flt)
say = threading.Lock()
# Each step's wall time, said on its line and summed up at the end: where a run's hour goes
# is the first thing to know before making it shorter (2026-10-24).
took, t_run = [], time.monotonic()
d = yaml.safe_load(open(os.environ.get("CI_LOCAL_WORKFLOW") or ".github/workflows/ci.yml"))
ok = fail = skip = 0
# A run stopped through its process group gets SIGTERM, which ended python without its
# finally blocks: the step's throwaway HOME, with the keys the cockpit tests write, and
# the stubs stayed in TMPDIR (found 2026-10-07). As an exception, it runs them. A stop
# also stops the jobs: their threads start no further step, and the steps they run are
# killed, as subprocess.run killed the one step of a serial run. The exception alone
# waited in the pool's shutdown while each job went on to its next steps (code review,
# 2026-10-28). Ctrl-C is the same stop.
stopping, running = threading.Event(), set()


def kill_step(p):
    """The step's bash and everything it started. Each step is its own process group: killing bash
    by its pid alone left its children holding the pipes that communicate() reads to their end, so
    a stop sent to python waited for them (review of PR #60, 2026-10-29). Its own group also means a
    stop of this run's group reaches a step only through python, which kills the steps' groups here."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop(signum, _frame):
    stopping.set()
    for p in list(running):
        kill_step(p)
    sys.exit(128 + signum)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
# A tool is missing when a step CALLS it, not when its name appears in the step's text.
# The test was a substring one: without gh, 13 of 61 steps were skipped for containing
# "through", "high" or "github", the sudoers gate among them, and docker is named by two
# steps that never run it (found in review, 2026-09-24). Each missing tool gets a stub,
# first on PATH, that records the call and fails the way a missing command does.
stubs = tempfile.mkdtemp(prefix="ci-local-stubs-")
for tool in ("shellcheck", "docker", "gh"):
    if not shutil.which(tool):
        with open(os.path.join(stubs, tool), "w") as f:
            f.write(f'#!/bin/sh\necho {tool} >> "$CI_LOCAL_CALLED"\n'
                    f'echo "ci-local: {tool} is not installed here" >&2\nexit 127\n')
        os.chmod(os.path.join(stubs, tool), 0o755)
PIP = re.compile(r"pip install\s+'([A-Za-z0-9_.-]+)==")
# The test venvs a checkout keeps (.venv-test, .venv) are not in git, so not in a worktree of
# HEAD, where tests/testpy.sh and property_checks.py look for them next to the tree they run
# from: a full run names this checkout's to its steps, as QWEN38_TEST_PYTHON, unless that is
# set already. The coverage floors failed there, the probe here having found the venv (code
# review, 2026-10-28).
test_python = {}
if not serial and not os.environ.get("QWEN38_TEST_PYTHON"):
    for venv in (".venv-test", ".venv"):
        if os.access(os.path.join(venv, "bin", "python"), os.X_OK):
            test_python = {"QWEN38_TEST_PYTHON": os.path.abspath(os.path.join(venv, "bin", "python"))}
            break


def have(pkg, env):
    """Whether this box has what a runner-only pip line installs, found the way the steps
    find it (tests/testpy.sh, under the step's own HOME)."""
    if pkg == "ruff":
        return shutil.which("ruff") is not None
    return subprocess.run(["./tests/testpy.sh", pkg.replace("-", "_")], env=env,
                          capture_output=True).returncode == 0


def step_name(st):
    return st.get("name") or st["run"].splitlines()[0][:60]


def run_job(job, steps, cwd):
    """The job's steps in order, in cwd; each line said as it ends. (ok, fail, skip)"""
    ok = fail = skip = 0
    tag = f"[{job}] " if not serial else ""
    for st in steps:
        run = st.get("run")
        if stopping.is_set():
            break
        if not run:
            continue
        name = step_name(st)
        if flt and flt not in name:
            continue
        # Run every step under a throwaway HOME. A GitHub runner has no
        # ~/.config/qwen38, and a test that quietly read the developer's own API
        # key passed here and failed there; local has to be the harder of the two.
        home = tempfile.mkdtemp(prefix="ci-local-home-")
        called = os.path.join(home, ".ci-local-called")
        # and a TMPDIR inside it, removed with it: the suite's own temporary directories
        # (and its scripts' mktemp) stayed in /tmp after every run, 761 of them on the
        # reference box on 2026-10-03, some with mtimes in 2027 that read as files changed
        # by whatever ran next
        tmp = os.path.join(home, "tmp")
        os.mkdir(tmp)
        # CI_LOCAL_WORKFLOW names the workflow to this run and is no part of a step's environment, as a
        # runner has no such variable: tests/test_ci_local.py, a step of the night's pass, handed it to the
        # ci-local.sh of each repository it plants, which looked there for a nightly.yml they do not have
        # (13 failures, found in review on 2026-11-10 before that pass had run once)
        env = {**{k: v for k, v in os.environ.items() if k != "CI_LOCAL_WORKFLOW"}, **test_python,
               "HOME": home, "TMPDIR": tmp, "CI_LOCAL_CALLED": called,
               "PATH": stubs + os.pathsep + os.environ.get("PATH", "")}
        r, tools, dt = None, [], 0.0
        try:
            # The runner installs its pinned packages; here they have to be there
            # already. The line is dropped, and what it installs is looked for: the
            # old test looked for ruff alone, and without ruff it skipped all six
            # pip steps, coverage and hypothesis included. A line ending in
            # `|| echo ...` is one the step runs without, and says so.
            need = []
            for ln in (ln for ln in run.splitlines() if "pip install" in ln):
                m = PIP.search(ln)
                if m and "|| echo" not in ln and not have(m.group(1), env):
                    need.append(m.group(1))
            if need:
                with say:
                    print(f"  {tag}SKIP {name} (missing: {', '.join(need)})")
                skip += 1
                continue
            script = "\n".join(ln for ln in run.splitlines() if "pip install" not in ln)
            t0 = time.monotonic()
            with subprocess.Popen(["bash", "-e", "-c", script], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, cwd=cwd, env=env,
                                  start_new_session=True) as p:
                running.add(p)
                if stopping.is_set():
                    kill_step(p)
                try:
                    out, err = p.communicate()
                except BaseException:
                    kill_step(p)
                    raise
                finally:
                    running.discard(p)
            r = subprocess.CompletedProcess(p.args, p.returncode, out, err)
            dt = time.monotonic() - t0
            tools = sorted(set(open(called).read().split())) if os.path.exists(called) else []
        finally:
            shutil.rmtree(home, ignore_errors=True)
        with say:
            took.append((dt, f"{tag}{name}"))
            if stopping.is_set():
                print(f"  {tag}--   {name} (stopped)")
            elif tools:
                print(f"  {tag}SKIP {name} (missing: {', '.join(tools)})"); skip += 1
            elif r.returncode == 0:
                print(f"  {tag}ok   {name} ({dt:.0f} s)"); ok += 1
            else:
                print(f"  {tag}FAIL {name} (rc={r.returncode}, {dt:.0f} s)")
                print("       " + (r.stdout + r.stderr).strip().replace("\n", "\n       ")[-600:]); fail += 1
    return ok, fail, skip


# (name, steps, needs), in the workflow's order. A job that needs others runs once they have
# ended, and only when every one of them passed, as on GitHub: checks, which needs them all,
# ran beside them and said "Every job passed" before any of them had ended (found 2026-10-28).
jobs = []
for n, j in d["jobs"].items():
    needs = j.get("needs") or []
    jobs.append((n, j.get("steps", []), [needs] if isinstance(needs, str) else list(needs)))
first = [jb for jb in jobs if not jb[2]]
after = [jb for jb in jobs if jb[2]]
results, trees, per_job, base, held = {}, [], {}, None, []


def passed(n):
    """Whether job n ran a step and passed them all: a skipped step is no pass."""
    o, f_, s_ = results.get(n, (0, 0, 0))
    return o > 0 and not f_ and not s_


def ready(n, steps, needs):
    """Whether every job n needs passed. If not, each of n's steps the filter keeps is said
    not run, and why."""
    bad = [x for x in needs if not passed(x)]
    tag = f"[{n}] " if not serial else ""
    for st in steps if bad else []:
        if st.get("run") and not (flt and flt not in step_name(st)):
            with say:
                print(f"  {tag}--   {step_name(st)} (not run: {', '.join(bad)} did not pass)")
            held.append(st)
    return not bad


try:
    if serial:
        for n, steps, needs in first + after:
            if not needs or ready(n, steps, needs):
                results[n] = run_job(n, steps, os.getcwd())
    else:
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout
        if dirty.strip():
            print("the tree is not clean: the jobs run in checkouts of HEAD, so commit first, or run "
                  "with CI_LOCAL_SERIAL=1 to test this checkout as it is:\n" + dirty)
            sys.exit(2)
        base = tempfile.mkdtemp(prefix="ci-local-jobs-")
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD^{tree}"], capture_output=True, text=True).stdout.strip()
        print(f"tree {head}: {len(first)} jobs side by side, each in a worktree of HEAD"
              + (f", then {', '.join(n for n, _, _ in after)}" if after else ""))

        # Each job's worktree is added here, one after the other, before any job starts: git worktree add,
        # side by side, reads what the other adds are writing. Twice on 2026-11-07 a job's add failed on
        # another's worktrees/<job>/commondir: that job never ran, and once the others were over the run
        # ended in a traceback, with no count. An add takes some 0.06 s. One that fails is its job's
        # failure, said and counted, and the other jobs run.
        def add(n):
            wt = os.path.join(base, n)
            trees.append(wt)
            a = subprocess.run(["git", "worktree", "add", "-q", "--detach", wt, "HEAD"], capture_output=True,
                               text=True)
            if a.returncode == 0:
                return wt
            with say:
                print(f"  [{n}] FAIL git worktree add (rc={a.returncode}): none of {n}'s steps ran")
                print("       " + (a.stdout + a.stderr).strip().replace("\n", "\n       ")[-600:])
            results[n] = (0, 1, 0)
            return None

        def one(n, steps, wt):
            t0 = time.monotonic()
            results[n] = run_job(n, steps, wt)
            per_job[n] = time.monotonic() - t0
        wts = {n: add(n) for n, _, _ in first}
        with ThreadPoolExecutor(max_workers=max(1, len(first))) as pool:
            list(pool.map(lambda jb: one(*jb), [(n, steps, wts[n]) for n, steps, _ in first if wts[n]]))
        for n, steps, needs in after:
            if ready(n, steps, needs) and (wt := add(n)):
                one(n, steps, wt)
finally:
    shutil.rmtree(stubs, ignore_errors=True)
    for wt in trees:
        subprocess.run(["git", "worktree", "remove", "--force", wt], capture_output=True)
    if base:
        # the directory the worktrees were in: it stayed in TMPDIR after every run (2026-10-28)
        shutil.rmtree(base, ignore_errors=True)
    if trees:
        subprocess.run(["git", "worktree", "prune"], capture_output=True)
for o, f_, s_ in results.values():
    ok += o; fail += f_; skip += s_
if per_job:
    print(f"\njobs, of {time.monotonic() - t_run:.0f} s in all (the longest sets the run):")
    for n, dt in sorted(per_job.items(), key=lambda x: -x[1]):
        print(f"  {dt:6.0f} s  {n}")
if took:
    print(f"\nslowest steps, of {time.monotonic() - t_run:.0f} s in all:")
    for dt, name in sorted(took, reverse=True)[:10]:
        print(f"  {dt:6.0f} s  {name}")
# a step held back is said again here, next to the count: its job is the one a run's status goes
# by, and its line was one among hundreds (code review, 2026-10-28)
if held:
    print(f"\nnot run, a job they need did not pass: {', '.join(step_name(st) for st in held)}")
print(f"\nCI local: {ok} ok, {fail} echecs, {skip} sautes")
if fail:
    sys.exit(1)
if not ok:
    print("nothing ran: " + ("every step it matched was skipped" if skip else
                             "every step it matched waits on a job that did not pass" if held else
                             f"no step matches '{flt}'"))
    sys.exit(3)
PY
