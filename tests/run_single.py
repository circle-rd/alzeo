#!/usr/bin/env python3
"""Every unittest module of tests/ beside the others: a few processes side by side, or one.

What a module does to the modules after it in the same process (a variable of the environment, the
directory, sys.path, a thread, a signal handler, a patched function left in place) shows only when
they share a process. CI ran every module of tests/ in ONE process for that: 1,808 s of a 30-minute
run on 2026-11-10, half of it the pair's tests, and the longest job of the run.

This runs the same modules in groups, each group one process, the groups side by side:

- each file of OWN, the longest ones, is a group of its own;
- the others, in the order of their names, are dealt in turn into REST groups.

Every module is in one group and one only, by construction. What a module does to a module of ANOTHER
group is not seen by a run in groups: `--one-process` runs them all in one process, as CI did, and
that pass is run every night (.github/workflows/nightly.yml) and before a release.

Whatever the shape, a run fails when a group fails, when the tests that ran are not as many as the
files declare, and when a test was skipped for a reason ALLOWED does not name.

Usage: run_single.py [--jobs N] [--one-process] [--repo DIR --own M1,M2]   (exit 1 on any failure;
--repo and --own point it at another tree, for its own tests)
"""
import argparse
import os
import pathlib
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

REPO = pathlib.Path(__file__).resolve().parents[1]

# The longest modules, each a process of its own, the longest first (seconds measured on the
# reference box on 2026-11-10: 497, 164, 132, 99; no other module takes more than 166).
OWN = ["tests.test_proxy_pair_one_model_inprocess", "tests.test_proxy_pair_one_model", "tests.test_proxy_pair",
       "tests.test_proxy_pair_inprocess"]
REST = 2

# spelled in two parts: the CI counts that string to know how many tests a file declares
TEST_DEF = "def " + "test_"

# "Ran" counts a skipped test as run: a skip decorator on a whole class stayed green, and so did
# the SDK round trip when its install failed (found in review, 2026-09-24). A skip passes only for
# a reason this list names: what a runner cannot have (the reference box, its cache, its units,
# opencode). The SDK is installed on the runner, so its absence is a reason only off it.
ALLOWED = ["needs the reference class of machine", "needs the pinned bytes cached", "no 27B unit installed here",
           "is not the enabled lane here", "no Qwen3.8 tokenizer.json", "needs a real opencode"]
OFF_THE_RUNNER = ["typesafe-sdk is not installed"]


def modules(repo=REPO):
    """Every unittest module of tests/, as python -m unittest names it from the repo."""
    return ["tests." + p.stem for p in sorted((repo / "tests").glob("test_*.py")) if TEST_DEF in p.read_text()]


def groups(mods, own=OWN, rest=REST):
    """The groups, the longest first: each module of own alone, the others dealt in turn into rest groups."""
    missing = [m for m in own if m not in mods]
    if missing:
        raise SystemExit(f"{missing}, named as the longest, are gone: name them again in OWN")
    others = [m for m in mods if m not in own]
    dealt = [others[i::rest] for i in range(rest)]
    return [[m] for m in own] + [g for g in dealt if g]


def declared(mods, repo=REPO):
    return sum((repo / (m.replace(".", "/") + ".py")).read_text().count(TEST_DEF) for m in mods)


def run_group(mods, repo=REPO):
    """One process for these modules: what it ran, what it skipped and why, how it ended."""
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-m", "unittest", "-v", *mods], cwd=repo, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    out = r.stdout + r.stderr
    ran = [int(n) for n in re.findall(r"^Ran (\d+) tests? in ", out, re.M)]
    return {"mods": mods, "rc": r.returncode, "out": out, "seconds": time.monotonic() - t0,
            "ran": ran[-1] if ran else 0, "declared": declared(mods, repo),
            "skips": [ln for ln in out.splitlines() if "... skipped '" in ln]}


def label(g):
    return g["mods"][0] if len(g["mods"]) == 1 else f"{g['mods'][0]} and {len(g['mods']) - 1} more"


def main(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--jobs", type=int, default=min(os.cpu_count() or 4, 4))
    ap.add_argument("--one-process", action="store_true")
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--own", default=",".join(OWN))
    a = ap.parse_args(argv)
    repo = pathlib.Path(a.repo).resolve()
    mods = modules(repo)
    try:
        plan = [mods] if a.one_process else groups(mods, own=[m for m in a.own.split(",") if m])
    except SystemExit as e:
        print(e)
        return 1
    print(f"unittest suites: {' '.join(mods)}")
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        done = list(pool.map(lambda g: run_group(g, repo), plan))
    bad = 0
    for g in done:
        if g["rc"] != 0:
            # Under bash -e a failing run used to end the step having printed nothing but the
            # list above (2026-09-23): say what failed.
            lines = [ln for ln in g["out"].splitlines() if "ResourceWarning" not in ln]
            print("\n".join(lines[-80:]))
            print(f"the unittest suites failed ({label(g)}):")
            print("\n".join(ln for ln in g["out"].splitlines() if re.match(r"^(FAIL|ERROR):", ln)))
            bad += 1
        elif g["ran"] != g["declared"]:
            print(f"unittest ran {g['ran']} of {g['declared']} declared tests ({label(g)})")
            bad += 1
    ran, want = sum(g["ran"] for g in done), declared(mods, repo)
    shape = "one process" if a.one_process else f"{len(plan)} processes, {a.jobs} at a time"
    print(f"declared {want}, ran {ran} ({shape}, {time.monotonic() - t0:.0f} s)")
    for g in sorted(done, key=lambda g: -g["seconds"]):
        print(f"  {g['seconds']:6.0f} s  {g['ran']:5d} tests  {label(g)}")
    if ran != want and not bad:
        print(f"unittest ran {ran} of {want} declared tests")
        bad += 1
    allowed = ALLOWED + ([] if os.environ.get("GITHUB_ACTIONS") else OFF_THE_RUNNER)
    skips = [ln for g in done for ln in g["skips"]]
    unlisted = [ln for ln in skips if not any(reason in ln for reason in allowed)]
    print(f"skipped for a listed reason: {len(skips) - len(unlisted)}")
    if unlisted:
        print("tests skipped for a reason nobody listed:")
        print("\n".join(unlisted))
        bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
