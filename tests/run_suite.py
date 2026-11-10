#!/usr/bin/env python3
"""Every test file of the repo, run once, on its own, as a human runs it, several at a time.

The offline step ran each file on its own, then tests/test_suite_isolation.py ran each unittest
module again in a process of its own to see whether it left an environment variable changed for
the files after it, and that file itself ran twice, in the offline step and in the single-process
pass: the suite ran about four times in a CI run (counted on 2026-10-24). This runs each file once
and takes every fact from that one run:

- it passes, and a unittest file runs as many tests as it declares when invoked directly (a file
  without a __main__ block exits 0 having run nothing);
- it leaves no environment variable changed on its way out (sitecustomize.py below compares the
  process's environment at its start and at its exit), the fact the files after it in one process
  would inherit;
- it contacts no serving port (the same hook records a connect() to one, with the test line).

Files that take a port number out of a socket they close (getsockname()[1]) run one at a time once
the others are over: a file running beside them could be handed the same port meanwhile. No test file
does since tests/localports.py, which keeps a port the test's beside the other jobs as well
(test_localports.py fails on a file that does): the planted files of this script's own test still do. The
witness HOME, the listeners and what is left running are the CI step's, around this script.

Usage: run_suite.py [--jobs N] [--report FILE] [--repo DIR --first F1,F2]   (exit 1 on any failure;
--repo and --first point it at another tree, for its own tests)
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

REPO = pathlib.Path(__file__).resolve().parents[1]

# The longest files start first, so that they do not end alone at the tail of the run.
FIRST = ["tests/test_proxy_pair_one_model_inprocess.py", "tests/test_proxy_hold.py",
         "tests/test_proxy_pair_one_model.py", "tests/test_proxy_pair.py", "tests/test_text_lane.py",
         "tests/test_proxy_pair_inprocess.py", "tests/test_pair_tool.py", "tests/test_check_pins.py"]

# spelled in two parts: the CI counts that string to know how many tests a file declares
TEST_DEF = "def " + "test_"

# Loaded by every python the suite starts (PYTHONPATH). It writes the environment changes of the
# test file's own process only, the one started with the file under test as its script.
SITECUSTOMIZE = r'''
import atexit, json, os, sys, traceback
_START = dict(os.environ)
_PORTS = {int(p) for p in os.environ.get("QWEN38_SERVING_PORTS", "").split(",") if p}

def _contact(event, args):
    if event != "socket.connect":
        return
    addr = args[1]
    if isinstance(addr, tuple) and len(addr) >= 2 and addr[1] in _PORTS:
        where = [f"{os.path.basename(f.filename)}:{f.lineno}" for f in traceback.extract_stack()
                 if "/tests/" in f.filename]
        with open(os.environ["QWEN38_CONTACT_LOG"], "a") as fh:
            fh.write(f"{addr[0]}:{addr[1]} from {' '.join(where[-2:]) or sys.argv[0]}\n")

if _PORTS and os.environ.get("QWEN38_CONTACT_LOG"):
    sys.addaudithook(_contact)

# Decided now, from the command line as the runner gave it: a test can replace sys.argv (the proxy's
# tests load it as keepalive-proxy.py) or change directory before its process exits.
_MINE = bool(_START.get("QWEN38_ENV_FOR")) and sys.argv[:1] == [_START.get("QWEN38_ENV_FOR")]

def _env_at_exit():
    report = _START.get("QWEN38_ENV_REPORT")
    if not (_MINE and report):
        return
    now = dict(os.environ)
    changed = sorted(k for k in set(_START) | set(now) if _START.get(k) != now.get(k))
    with open(report, "w") as fh:
        json.dump(changed, fh)

atexit.register(_env_at_exit)
'''


def files(repo=REPO):
    """Every test file: dashboard/tests/test_*.py and tests/test_*.py, as the repo has them."""
    return sorted(str(p.relative_to(repo)) for p in [*(repo / "dashboard" / "tests").glob("test_*.py"),
                                                      *(repo / "tests").glob("test_*.py")])


def plan(all_files, repo=REPO, first=FIRST):
    """(together, alone): the longest named first, then the others, and the files that must run alone."""
    alone = [f for f in all_files if "getsockname()[1]" in (repo / f).read_text()]
    missing = [f for f in first if f not in all_files]
    if missing:
        raise SystemExit(f"{missing}, named as the longest, are gone: name them again in FIRST")
    together = [f for f in first if f not in alone] + [f for f in all_files if f not in first and f not in alone]
    return together, alone


def run_one(f, probe, repo=REPO, timeout=1800):
    """One file on its own, in the directory a human runs it from: its verdict as a dict."""
    path = repo / f
    cwd, rel = (repo / "dashboard", "tests/" + path.name) if f.startswith("dashboard/") else (repo, f)
    env_report = probe / ("env-" + f.replace("/", "_") + ".json")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(probe), os.environ.get("PYTHONPATH")])),
           "QWEN38_ENV_FOR": rel, "QWEN38_ENV_REPORT": str(env_report)}
    t0 = time.monotonic()
    try:
        r = subprocess.run([sys.executable, rel], cwd=cwd, env=env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=timeout)
        rc, out = r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        # what it printed so far is bytes whatever text= says, and unittest names the test it is
        # in on stderr, which this dropped (code review, 2026-10-28)
        part = b"".join(x for x in (e.stdout, e.stderr) if x).decode(errors="replace")
        rc, out = None, f"timed out after {timeout} s\n{part[-4000:]}"
    verdict = {"file": f, "seconds": round(time.monotonic() - t0, 1), "rc": rc, "problems": []}
    if rc != 0:
        verdict["problems"].append("failed, the end of its output:\n" + "\n".join(out.splitlines()[-60:]))
        return verdict
    text = path.read_text()
    if TEST_DEF in text:
        declared = text.count(TEST_DEF)
        ran = max([int(w.split()[1]) for w in out.splitlines() if w.startswith("Ran ") and w.split()[1].isdigit()],
                  default=0)
        if ran != declared:
            verdict["problems"].append(f"ran {ran} of its {declared} declared tests when invoked directly")
    try:
        changed = json.loads(env_report.read_text())
    except (OSError, ValueError):
        changed = None
    if changed is None:
        verdict["problems"].append("its process never reported its environment at exit")
    elif changed:
        verdict["problems"].append(f"left these environment variables changed: {', '.join(changed)}")
    return verdict


def main(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--jobs", type=int, default=min(os.cpu_count() or 4, 4))
    ap.add_argument("--report")
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--first", default=",".join(FIRST))
    a = ap.parse_args(argv)
    repo = pathlib.Path(a.repo).resolve()
    try:
        together, alone = plan(files(repo), repo=repo, first=[f for f in a.first.split(",") if f])
    except SystemExit as e:
        print(e)
        return 1
    with tempfile.TemporaryDirectory(prefix="run-suite-") as d:
        probe = pathlib.Path(d)
        (probe / "sitecustomize.py").write_text(SITECUSTOMIZE)
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=a.jobs) as pool:
            verdicts = list(pool.map(lambda f: run_one(f, probe, repo=repo), together))
        t1 = time.monotonic()
        verdicts += [run_one(f, probe, repo=repo) for f in alone]
        t2 = time.monotonic()
    bad = [v for v in verdicts if v["problems"]]
    for v in bad:
        for p in v["problems"]:
            print(f"{v['file']} {p}")
    longest = sorted(verdicts, key=lambda v: -v["seconds"])[:3]
    print(f"every test file ran on its own: {len(together)} of them {a.jobs} at a time in {t1 - t0:.0f} s, "
          f"then {len(alone)} one at a time in {t2 - t1:.0f} s; {len(bad)} with a problem")
    print("  the longest: " + ", ".join(f"{v['file']} {v['seconds']:.0f} s" for v in longest))
    if a.report:
        pathlib.Path(a.report).write_text(json.dumps(verdicts, indent=1))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
