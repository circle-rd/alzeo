"""Every cockpit collector, against real command output and against garbage.

A collector is the only thing between a command's stdout and what a person reads
off the dashboard, so a wrong parse is a lying panel, and this repo has been bitten
by exactly that (the stage parser and the decode telemetry read an empty stdout for
a night because docker logs writes to stderr). Two things are asserted here:

  * PARSE: given output captured from the reference box (tests/fixtures/), the
    collector returns the values a person would read off that output by hand.
  * SURVIVE: given output no collector expects (empty, truncated mid-line, an
    nvidia-smi [N/A], a French locale, 2 MB of one line, NUL bytes, a stack
    trace where a table was), it returns a dict and never raises. @guard turns a
    raise into {"error": ...}, so the test also checks WHICH collectors degrade
    into an error, because an error dict is an honest answer and a wrong number
    is not.

Nothing here touches the box: cockpit.run is replaced by a table, every path
points at a temp directory, and no collector is allowed to reach the network.
"""
import http.server
import importlib.util
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]

# The environment this module sets for the cockpit it loads is handed back when it ends,
# so the next module in the same process starts from what this one found.
ENV_BEFORE = {}


def setUpModule():
    ENV_BEFORE.update(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)


FIX = HERE.parent / "fixtures"
CLOSED = "http://127.0.0.1:1"        # nothing listens there: a request fails at once


def offline(url, timeout=5.0):
    """cockpit._get_json on a box with no route out, which every collector must survive."""
    raise OSError(f"offline test: {url}")


def fixture(name: str) -> str:
    return (FIX / name).read_text()


class FakeBox:
    """Answers cockpit.run() from a table keyed by a fragment of the argv."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []
        self.hits = dict.fromkeys(self.table, 0)   # which answers were ever given

    def __call__(self, argv, timeout=5.0, merge_err=False):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key, val in self.table.items():
            if key in joined:
                self.hits[key] += 1
                return val
        return ""


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-coll-"))
        (cls.tmp / "api-key").write_text("k\n")
        # The engine, the proxy, the image lane and the video lane on a closed port, and no
        # request out: with the defaults left in place this suite made 46 requests to the engine
        # serving on the reference box (/health there is a one-token generation), one to
        # the image lane and two to api.github.com (found in review, 2026-09-24), and one to
        # the video lane's port, which the offline suite's guard did not watch (found on
        # 2026-11-10). A test that needs an engine starts its own and points ENGINE_BASE at it.
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp),
                          COCKPIT_REPO_DIR=str(REPO), COCKPIT_PORT="0",
                          COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0",
                          COCKPIT_ENGINE=CLOSED, COCKPIT_PROXY=CLOSED, COCKPIT_IMAGE=CLOSED,
                          COCKPIT_VIDEO=CLOSED)
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_coll_under_test",
                                                      DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)
        cls.cp._get_json = offline
        # staticmethod: a plain function stored on a class becomes a method,
        # and would then receive the TestCase as its first argument.
        cls.real_run = staticmethod(cls.cp.run)
        cls._real_run = cls.cp.run

    @classmethod
    def tearDownClass(cls):
        cls.cp.run = cls._real_run
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self):
        self.cp.run = type(self)._real_run

    def box(self, table=None):
        fake = FakeBox(table)
        self.cp.run = fake
        return fake

    def collectors(self):
        """Every collector this module exposes, by name."""
        return {n: getattr(self.cp, n) for n in dir(self.cp)
                if n.startswith("collect_") and callable(getattr(self.cp, n))}


class TheUpdateCommand(Base):
    def test_the_update_is_the_one_command_run_from_this_checkout(self):
        # Settings gave cd <repo> && ./install.sh as the update, and install.sh fetches
        # nothing: it installed again the release the box already ran (found in review,
        # 2026-10-14). The update is the README's one command; run from the checkout, get.sh
        # updates that clone, wherever it is, and no other. Since v1.41.0 the README's command is
        # get.alzeo.ai, a redirect to get.sh on GitHub, and the cockpit gives the long form the
        # README gives next to it: a box already installed trusts no new party for its updates.
        readme = (REPO / "README.md").read_text()
        self.assertIn("\ncurl -fsSL https://get.alzeo.ai | bash\n", readme)
        one = "curl -fsSL https://raw.githubusercontent.com/hasso5703/alzeo/main/get.sh | bash"
        self.assertIn(f"`{one}`", readme)
        t = self.cp.STATE["config"]["data"]["terminal_only"]
        self.assertEqual(t["update"], f"cd {REPO} && {one}")
        # the Agent view's hint stays the installer again, which is what it asks for
        self.assertEqual(t["update_stack"], f"cd {REPO} && ./install.sh")
        # and the way to the recommended state (v1.36.0) is the same command, which hands
        # install.sh what follows -s -- (get.sh's own header says so)
        self.assertEqual(t["recommended"], f"cd {REPO} && {one} -s -- --recommended")
        self.assertIn("bash -s --", (REPO / "get.sh").read_text())


class TheRegistryReadsEveryCache(Base):
    def test_a_cache_a_unit_names_is_scanned(self):
        # install.sh takes HF_CACHE, and the registry scanned ~/.cache/huggingface alone, so
        # a box installed with another cache read an empty Library (found in review,
        # 2026-10-14). The units say where each lane's cache is.
        cache = self.tmp / "elsewhere"
        snap = cache / "hub" / "models--Aleph-Alpha--Kolibri-1" / "snapshots" / ("a" * 40)
        snap.mkdir(parents=True)
        (snap / "model.safetensors").write_bytes(b"x")
        unit = self.tmp / "qwen38-kolibri.service"
        unit.write_text(f"ExecStart=/usr/bin/docker run --rm -v {cache}:/root/.cache/huggingface img\n")
        saved = self.cp.KOLIBRI_UNIT_PATH
        self.cp.KOLIBRI_UNIT_PATH = unit
        self.box({})
        try:
            data = self.cp.registry_snapshot(max_age=0.0)
        finally:
            self.cp.KOLIBRI_UNIT_PATH = saved
            self.cp.registry_snapshot(max_age=0.0)
        self.assertIn(str(cache), data["caches"])
        got = [m for m in data["models"] if m["repo_id"] == "Aleph-Alpha/Kolibri-1" and m["cache"] == str(cache)]
        self.assertEqual(len(got), 1, data["models"])


class Parse(Base):
    def test_units_reads_systemctl_show(self):
        self.box({"systemctl show": fixture("systemctl-show.txt")})
        out = self.cp.collect_units()
        self.assertNotIn("error", out)
        for unit, got in out["units"].items():
            self.assertEqual(got["active"], "active", unit)
            self.assertEqual(got["sub"], "running", unit)
            self.assertEqual(got["enabled"], "enabled", unit)
            self.assertTrue(got["since"].startswith("Wed 2026-09-09"), got)

    def test_units_reports_a_missing_unit_as_unknown_not_as_active(self):
        self.box({})            # systemctl printed nothing at all
        out = self.cp.collect_units()
        for unit, got in out["units"].items():
            self.assertEqual(got["active"], "?", unit)

    def test_containers_reads_ps_and_stats(self):
        self.box({"docker ps --format": fixture("docker-ps.txt"),
                  "docker stats": fixture("docker-stats.txt")})
        out = self.cp.collect_containers()
        self.assertNotIn("error", out)
        self.assertIn("qwen38-flash", out["containers"])
        c = out["containers"]["qwen38-flash"]
        self.assertEqual(c["image"], "lmsysorg/sglang")
        self.assertEqual(c["cpu"], "101.61%")
        self.assertIn("5.83GiB", c["mem"])

    def test_gpu_reads_power_and_temperature(self):
        self.box({"--query-gpu=power.draw,temperature.gpu": "10.58, 46\n"})
        out = self.cp.collect_gpu()
        self.assertNotIn("error", out)
        self.assertAlmostEqual(out["power_w"], 10.58, places=2)
        self.assertAlmostEqual(out["temp_c"], 46.0, places=1)

    def test_gpu_does_not_invent_numbers_when_nvidia_smi_is_silent(self):
        self.box({})
        out = self.cp.collect_gpu()
        self.assertIsNone(out["power_w"])
        self.assertIsNone(out["temp_c"])
        self.assertEqual(out["procs"], [])

    def test_gpu_that_did_not_answer_has_no_process_list(self):
        """An empty list is a GPU with nothing on it: a query that timed out is no list, or
        the page says "No process on the GPU" of a box it could not read (2026-10-02)."""
        def silent(argv, timeout=5.0, merge_err=False):
            r = self.cp.Ran("")
            r.ok = False
            return r
        self.cp.run = silent
        out = self.cp.collect_gpu()
        self.assertEqual((out["power_w"], out["temp_c"], out["procs"]), (None, None, None), out)

    def test_feed_reads_the_keepalive_journal(self):
        self.box({"journalctl -u qwen38-keepalive.service": fixture("keepalive-journal.txt")})
        out = self.cp.collect_feed()
        self.assertNotIn("error", out)
        self.assertTrue(out["rows"], "the real journal produced no request rows")
        for r in out["rows"]:
            self.assertIn("kind", r)
            self.assertIn(r["kind"], ("ok", "gone", "fail", "live", "unknown"))

    def test_feed_keeps_a_long_request_after_its_start_left_the_window(self):
        """The badge and the table count the rows in flight. The first read goes back to the
        proxy's start; later ones read the 800 newest lines and carry what was in flight, so
        a generation whose start left the window stays counted and its end lands
        (2026-10-02: 8 in flight at the proxy and the engine, 2 in the cockpit's rows)."""
        L = "2026-10-02T23:{m:02d}:{s:02d}+02:00 gx10 python3[1]: [proxy] {rest}"
        start = L.format(m=16, s=1, rest="127.0.0.1:49398 -> POST /v1/chat/completions body=111922b")
        short = lambda m: [L.format(m=m, s=i, rest=f"127.0.0.1:{41000 + m * 60 + i} -> POST /v1/chat/completions body=9b")
                           for i in range(30)] + [L.format(m=m, s=i, rest=f"127.0.0.1:{41000 + m * 60 + i} POST /v1/chat/completions ok in 1.0s")
                                                   for i in range(30)]
        end = L.format(m=49, s=49, rest="127.0.0.1:49398 POST /v1/chat/completions ok in 2028.0s")
        calls, answers = [], {"since": "\n".join([start] + short(20)), "window": "\n".join(short(40))}

        def fake(argv, timeout=5.0, merge_err=False):
            calls.append(list(argv))
            if argv[:2] == ["systemctl", "show"]:
                return "@1790974535\n"
            return answers["since"] if "--since" in argv else answers["window"]
        self.cp.run = fake
        self.cp.FEED_CARRY.clear()
        self.cp.FEED_BOOTED[0] = False
        live = lambda out: [r["peer"] for r in out["rows"] if r["outcome"] == "in flight"]
        out = self.cp.collect_feed()
        self.assertIn("--since", calls[-1])
        self.assertEqual(calls[-1][calls[-1].index("--since") + 1], "@1790974535")
        self.assertEqual(live(out), ["127.0.0.1:49398"])
        out = self.cp.collect_feed()
        self.assertNotIn("--since", calls[-1])
        self.assertEqual(calls[-1][calls[-1].index("-n") + 1], "800")
        self.assertEqual(live(out), ["127.0.0.1:49398"], "the window no longer holds its start")
        # its end lands on the carried start (a few newer requests: it is among the 25 newest)
        answers["window"] = "\n".join(short(41)[:3] + short(41)[30:33] + [end])
        out = self.cp.collect_feed()
        self.assertEqual(live(out), [])
        self.assertIn(("127.0.0.1:49398", "ok", 2028.0), [(r["peer"], r["outcome"], r["secs"]) for r in out["rows"]])

    def test_a_first_feed_read_that_timed_out_is_tried_again(self):
        def silent(argv, timeout=5.0, merge_err=False):
            r = self.cp.Ran("" if argv[0] == "journalctl" else "@1790974535\n")
            r.ok = argv[0] != "journalctl"
            return r
        self.cp.run = silent
        self.cp.FEED_CARRY.clear()
        self.cp.FEED_BOOTED[0] = False
        self.cp.collect_feed()
        self.assertFalse(self.cp.FEED_BOOTED[0], "the read since the proxy's start is still to do")

    def test_decode_telemetry_needs_a_running_container_to_say_anything(self):
        self.box({})            # docker ps -q answers nothing: no lane is up
        out = self.cp.collect_decode_telemetry()
        self.assertIsNone(out["lane"])

    def test_decode_telemetry_reads_the_engine_log_of_the_running_lane(self):
        log = ("[2026-09-10 10:00:00] Decode batch. #running-req: 3, "
               "token usage: 0.12, accept len: 2.41\n")
        self.box({"docker ps -q": "abc123\n", "docker logs": log})
        out = self.cp.collect_decode_telemetry()
        self.assertIsNotNone(out["lane"])
        self.assertEqual(out["decode"], {"running": 3, "token_usage": 0.12,
                                         "accept_len": 2.41})

    def test_the_zombie_guard_reads_both_sides_of_the_wire(self):
        engine = ("[2026-09-10 10:00:00] Received output for rid='abc' but the state "
                  "was deleted in TokenizerManager.\n") * 3
        journal = ("[proxy] v6.14 on :30001 -> http://127.0.0.1:30000 (keepalive 10s, "
                   "max silence 3600s)\n"
                   "[proxy] aborted upstream rid=abc (client gone)\n")
        self.box({"docker ps -q": "abc123\n", "docker logs": engine,
                  "journalctl": journal})
        out = self.cp.collect_guard()
        self.assertEqual(out["zombies"]["lines"], 3)
        self.assertEqual(out["guard"]["version"], "6.14")
        self.assertEqual(out["guard"]["aborted"], 1)
        self.assertIn(out["state"], ("ok", "warn", "err", ""))

    def test_the_guard_reads_a_day_of_tool_calls_naming_the_hosts(self):
        """The proxy's role 8 (v6.48): its TOOL TARGET lines of the last 24 h, read with their time,
        for the page's banner. The day is read every 5 min, not at each 30 s tick of the guard. A read
        that failed counts the last good one again over the day, and a day read without a line
        (a journal vacuumed since) takes the banner down."""
        line = ("{at} gx10-eff9 python3[4713]: [proxy] 127.0.0.1:5 key=alice TOOL TARGET aliyuncs.com in a "
                "bash call on /v1/chat/completions, relayed as it came (docs/agents-and-network.md)\n")
        now = time.time()
        seen, answer = [], [line.format(at=now - 3600) + "1793200000.0 gx10-eff9 python3[4713]: [proxy] other\n"]

        class Journal(FakeBox):
            def __call__(self, argv, timeout=5.0, merge_err=False):
                self.calls.append(list(argv))
                if argv[0] == "journalctl" and "short-unix" in argv:
                    seen.append(list(argv))
                    return answer[0]
                return ""
        self.cp.run = Journal()
        self.cp.TOOL_TARGETS.update(at=0.0, raw="", seen={"count": 0, "last": None})
        out = self.cp.collect_guard()
        self.assertEqual(out["tool_targets"]["count"], 1)
        self.assertEqual(out["tool_targets"]["last"]["tool"], "bash")
        self.assertEqual(seen[0][seen[0].index("--since") + 1], "-24h")
        self.assertNotIn("-g", seen[0], "journalctl -g ends 1 on no match, as a failed read does")
        self.cp.collect_guard()
        self.assertEqual(len(seen), 1, "read again inside its 5 minutes")
        failed = self.cp.Ran("")                              # what run() returns for a timeout
        failed.ok = False
        answer[0] = failed
        self.cp.TOOL_TARGETS["at"] = 0.0
        self.assertEqual(self.cp.collect_guard()["tool_targets"]["count"], 1, "a failed read keeps the day")
        # the last good read counted again over the day as it stands: a line past the day goes
        self.cp.TOOL_TARGETS.update(at=0.0, raw=line.format(at=now - 90000) + line.format(at=now - 3600))
        self.assertEqual(self.cp.collect_guard()["tool_targets"]["count"], 1)
        answer[0] = ""                                         # the day read, and no line in it
        self.cp.TOOL_TARGETS["at"] = 0.0
        self.assertEqual(self.cp.collect_guard()["tool_targets"]["count"], 0)

    def test_the_guard_reads_the_long_prompts_sent_again_from_the_same_day(self):
        """The proxy's role 9 (v6.49, issue #61): its RESENT lines of the last 24 h, from the same read
        of the day as role 8's, for the page's banner. A failed read keeps the day."""
        resent = ("{at} gx10-eff9 python3[4713]: [proxy] 127.0.0.1:57102 RESENT the prompt its client left "
                  "without an answer after 621 s (2 times in a row, claude-cli/2.1.314): Claude Code gives up "
                  "on a turn after 10 min, then 5 min without streaming: claude-local, or API_TIMEOUT_MS=3600000 "
                  "with CLAUDE_STREAM_IDLE_TIMEOUT_MS and CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS at 1800000, lets it "
                  "wait (docs/clients.md)\n")
        now = time.time()
        seen, answer = [], [resent.format(at=now - 90000) + resent.format(at=now - 600)
                            + "1793200000.0 gx10-eff9 python3[4713]: [proxy] other\n"]

        class Journal(FakeBox):
            def __call__(self, argv, timeout=5.0, merge_err=False):
                self.calls.append(list(argv))
                if argv[0] == "journalctl" and "short-unix" in argv:
                    seen.append(list(argv))
                    return answer[0]
                return ""
        self.cp.run = Journal()
        self.cp.TOOL_TARGETS.update(at=0.0, raw="", raw_resent="", seen={"count": 0, "last": None},
                                    resent={"count": 0, "last": None})
        out = self.cp.collect_guard()
        self.assertEqual(out["resent"]["count"], 1, "the line of yesterday is past the day")
        self.assertEqual((out["resent"]["last"]["secs"], out["resent"]["last"]["n"], out["resent"]["last"]["who"]),
                         (621, 2, "claude-cli/2.1.314"))
        self.assertEqual(len(seen), 1, "one read of the day for both roles")
        failed = self.cp.Ran("")
        failed.ok = False
        answer[0] = failed
        self.cp.TOOL_TARGETS["at"] = 0.0
        self.assertEqual(self.cp.collect_guard()["resent"]["count"], 1, "a failed read keeps the day")

    def test_the_zombie_guard_reports_the_proxy_that_runs(self):
        """journalctl -g is given a pattern, and -n 1 answers the newest line matching it:
        the fake applies the collector's own pattern to a journal holding a banner of the
        old shape and, after it, the banner the proxy prints today (rendered from its
        source). The old pattern, "on :", only matched the first, so the page showed
        v6.20 on the reference box while v6.24 ran."""
        import re
        src = (REPO / "keepalive-proxy.py").read_text()
        tpl = re.search(r'log\(f"(v([\d.]+) on \{BIND\}:\{port\} -> [^"]*)"\)', src)
        current = tpl.group(2)
        banner = "[proxy] " + re.sub(r"\{[^}]*\}", "x", tpl.group(1).replace("{BIND}", "0.0.0.0")
                                                             .replace("{port}", "30001"))
        journal = ["[proxy] v6.20 on :30001 -> http://127.0.0.1:30000 (keepalive 10s, max silence 3600s)",
                   "[proxy] aborted upstream rid=abc (client gone)", banner]

        class Journal(FakeBox):
            def __call__(self, argv, timeout=5.0, merge_err=False):
                self.calls.append(list(argv))
                if argv[0] == "journalctl" and "-g" in argv:
                    pattern = argv[argv.index("-g") + 1]
                    hits = [ln for ln in journal if re.search(pattern, ln, re.I)]
                    return (hits[-1] + "\n") if hits else ""
                return "\n".join(journal) + "\n" if argv[0] == "journalctl" else ""
        self.cp.run = Journal()
        out = self.cp.collect_guard()
        self.assertEqual(out["guard"]["version"], current)
        self.assertIs(out["guard"]["predates_abort"], False)

    def test_the_deployed_proxy_says_its_version(self):
        # its history line is past the first 4,000 bytes the cockpit read since v1.40.7: Lanes and Settings said
        # "unknown", and the warning that the deployed proxy is not the repo's copy could never show (review,
        # 2026-11-03)
        self.box({"git": ""})
        repo = (REPO / "keepalive-proxy.py").read_bytes()
        want = "v" + re.search(rb"\nv(\d+\.\d+):", repo).group(1).decode()
        deployed = pathlib.Path(self.cp.CONFIG_DIR) / "keepalive-proxy.py"
        self.addCleanup(deployed.unlink, missing_ok=True)
        deployed.write_bytes(repo)
        out = self.cp.collect_repo()
        self.assertEqual(out["proxy"], {"version": want, "same_as_repo": True})
        deployed.write_bytes(repo.replace(b"No content logging", b"No content logged", 1))
        self.assertEqual(self.cp.collect_repo()["proxy"], {"version": want, "same_as_repo": False})
        # the history says the version where the first line lagged it (v1.5.4 to v1.5.7), and the first line where
        # there is no history
        deployed.write_bytes(b'#!/usr/bin/env python3\n"""Keepalive proxy in front of SGLang (v6.6).\n' + b"x" * 5000
                             + b"\nv6.8: the latest\n\nv6.7: one before\n")
        self.assertEqual(self.cp.collect_repo()["proxy"]["version"], "v6.8")
        deployed.write_bytes(b'#!/usr/bin/env python3\n"""Keepalive proxy in front of SGLang (v6.6).\n')
        self.assertEqual(self.cp.collect_repo()["proxy"]["version"], "v6.6")

    def test_an_untracked_file_is_not_a_modified_working_tree(self):
        """A screenshot dropped in the checkout is not the served code drifting.

        git status --porcelain prints both, and the cockpit used to call any
        non-empty output "modified (uncommitted changes)", which is the sentence
        a person reads to decide whether the box runs the repo's code. Seen on
        2026-09-17 with a downloaded .png sitting in the working tree.
        """
        self.box({"git": '?? "telechargement (2).png"\n'})
        out = self.cp.collect_repo()
        self.assertIs(out["dirty"], False)
        self.assertEqual(out["untracked"], 1)

    def test_it_asks_git_once_so_two_answers_cannot_disagree(self):
        """run() turns a timeout into "", so two calls can contradict each other.

        With one call per fact, a timeout on the first and a success on the
        second reported dirty=False with untracked=1: "clean (1 untracked
        file)" over a modified install.sh, which is the exact sentence this
        split was written to make trustworthy.
        """
        calls = []

        def flaky(argv, *a, **kw):
            calls.append(argv)
            if "status" in argv:
                # first status answers empty (the timeout shape), any later one
                # answers with real content
                return "" if len([c for c in calls if "status" in c]) == 1 \
                    else " M install.sh\n?? shot.png\n"
            return ""

        self.cp.run = flaky
        out = self.cp.collect_repo()
        self.assertEqual(len([c for c in calls if "status" in c]), 1,
                         "collect_repo must ask git status exactly once")
        # and with a single answer the two facts always agree
        self.assertIs(out["dirty"], False)
        self.assertEqual(out["untracked"], 0)

    def test_a_tracked_change_is_still_a_modified_working_tree(self):
        self.box({"git": " M install.sh\n?? note.txt\n"})
        out = self.cp.collect_repo()
        self.assertIs(out["dirty"], True)
        self.assertEqual(out["untracked"], 1)

    def test_a_clean_tree_is_clean(self):
        self.box({"git": ""})
        out = self.cp.collect_repo()
        self.assertIs(out["dirty"], False)
        self.assertEqual(out["untracked"], 0)

    def test_every_collector_answers_with_a_dict_on_a_healthy_box(self):
        self.box({"docker ps --format": fixture("docker-ps.txt"),
                  "docker stats": fixture("docker-stats.txt"),
                  "systemctl show": fixture("systemctl-show.txt"),
                  "journalctl": fixture("keepalive-journal.txt")})
        for name, fn in self.collectors().items():
            with self.subTest(collector=name):
                out = fn()
                self.assertIsInstance(out, dict, name)
        # and every captured output in the table reached a collector: one no collector
        # asks for is a fixture that tests nothing (found in review, 2026-09-24)
        self.assertEqual([k for k, n in self.cp.run.hits.items() if not n], [])


class TheOtherBoxOfAPair(Base):
    """collect_pair (decision 975): each box of pair.json read through this box's own proxy, this box's key sent
    there and nowhere else, a box that does not answer said on its row while the others still read, a name that is
    no box's never put in a URL; nothing read on a box alone."""

    def setUp(self):
        class Proxy(http.server.BaseHTTPRequestHandler):
            seen: list = []

            def log_message(self, *a):
                pass

            def do_GET(self):
                Proxy.seen.append((self.path, self.headers.get("Authorization")))
                if self.path == "/proxy/pair/spark2/state":
                    code, obj = 200, {"name": "spark2", "models": ["qwen3.8-flash-next-fp4"], "mem_total": 1 << 37}
                elif self.path in ("/proxy/pair/spark-nan/state", "/proxy/pair/spark-big/state"):
                    out = (b'{"gpu_temp_c": NaN}' if "nan" in self.path else b'{"mem_total": 1e400}')
                    self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers()
                    self.wfile.write(out)
                    return
                elif self.path == "/proxy/pair/spark3/state":
                    code, obj = 503, {"error": {"type": "engine_unavailable",
                                                "message": "keepalive-proxy: box spark3 does not answer: no answer in time"}}
                elif self.path == "/proxy/pair/spark4/state":
                    code, obj = 503, {"error": {"type": "engine_unavailable",
                                                "message": "keepalive-proxy: box spark4 does not answer: refused"},
                                      "sent": {"affinity": 2, "failover": 3, "too_long": True, "rescued": -1,
                                               "balance": 1.5, "evil": 5}}
                else:
                    code, obj = 404, {"error": {"message": "keepalive-proxy: names no box of this box's pair"}}
                out = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        self.Proxy = Proxy
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        saved = self.cp.PROXY_BASE
        self.cp.PROXY_BASE = f"http://127.0.0.1:{srv.server_address[1]}"
        self.addCleanup(setattr, self.cp, "PROXY_BASE", saved)
        self.pair = self.tmp / "pair.json"
        self.addCleanup(lambda: self.pair.unlink(missing_ok=True))

    def test_each_box_of_the_pair_is_read_through_this_boxs_proxy(self):
        self.pair.write_text(json.dumps({"name": "spark1", "peers": [
            {"name": "spark2"}, {"name": "spark3"}, {"name": "../../etc"}, {"name": 7}, "x"]}))
        out = self.cp.collect_pair()
        boxes = out["boxes"]
        self.assertEqual(sorted(boxes), ["spark2", "spark3"], "a name that is no box's is never put in a URL")
        self.assertEqual(boxes["spark2"]["state"]["models"], ["qwen3.8-flash-next-fp4"])
        self.assertEqual(boxes["spark3"]["error"], "box spark3 does not answer: no answer in time")
        self.assertEqual({auth for _, auth in self.Proxy.seen}, {"Bearer k"}, "this box's key, to its own proxy only")

    def test_what_was_shared_is_kept_while_the_other_box_does_not_answer(self):
        # the proxy's 503 says its counts (review, 2026-11-03): counts alone, of the tile's words alone
        self.pair.write_text(json.dumps({"name": "spark1", "peers": [{"name": "spark4"}]}))
        self.assertEqual(self.cp.collect_pair()["boxes"]["spark4"], {
            "error": "box spark4 does not answer: refused", "sent": {"affinity": 2, "failover": 3}})

    def test_what_a_browser_cannot_parse_never_enters_the_state(self):
        # json.dumps(STATE) wrote the NaN back, and every view of this cockpit froze on a frame JSON.parse refuses
        # (review, 2026-11-03)
        self.pair.write_text(json.dumps({"name": "spark1", "peers": [{"name": "spark-nan"}, {"name": "spark-big"}]}))
        boxes = self.cp.collect_pair()["boxes"]
        self.assertEqual(sorted(boxes), ["spark-big", "spark-nan"])
        self.assertTrue(all("error" in b and "state" not in b for b in boxes.values()), boxes)
        json.dumps(boxes, allow_nan=False)

    def test_a_box_alone_reads_nothing(self):
        self.assertEqual(self.cp.collect_pair(), {"node_id": "local", "boxes": {}})
        self.pair.write_text("{not json")
        self.assertEqual(self.cp.collect_pair(), {"node_id": "local", "boxes": {}})
        self.assertEqual(self.Proxy.seen, [])

    def test_it_has_a_tier_of_its_own(self):
        tiers = [cols for _, cols in self.cp.TIERS if "pair" in cols]
        self.assertEqual(tiers, [{"pair": self.cp.collect_pair}], "a box that went holds its read: alone in its thread")


class Survive(Base):
    """Output no collector expects. None of these may raise, and none may
    silently turn into a plausible-looking number."""

    HOSTILE = {
        "empty": "",
        "one newline": "\n",
        "spaces": "    \n   \n",
        "nvidia N/A": "[N/A], [N/A]\n",
        "not applicable words": "No running processes found\n",
        "french locale": ("               total       utilise      libre\n"
                          "Mem:             121         109           2\n"),
        "truncated mid line": "[2026-09-10 10:00:00] Decode batch. #running-req: 3, token us",
        "header only": "Name  CPU  MEM\n",
        "a stack trace": ("Traceback (most recent call last):\n"
                          '  File "x", line 1\nValueError: nope\n'),
        "json where text was": json.dumps({"unexpected": True}),
        "one very long line": "x" * 200000,
        "many lines": "line\n" * 20000,
        "nul bytes": "a\x00b\x00c\n",
        "control chars": "".join(chr(i) for i in range(1, 32)) + "\n",
        "unicode": "éèê你好\U0001f600 running-req: ∞\n",
        "negative numbers": "#running-req: -1, token usage: -0.5, accept len: -2\n",
        "huge numbers": "#running-req: 99999999999999999999, token usage: 1e400, accept len: nan\n",
        "delimiters only": "|||\n===\n:::\n,,,\n",
        "partial fields": "qwen38-flash|\n|image|\n||\n",
        "docker error": ("Error response from daemon: No such container: qwen38-flash\n"),
        "sudo refusal": "sudo: a password is required\n",
    }

    def test_no_collector_raises_on_any_hostile_output(self):
        for label, text in self.HOSTILE.items():
            for name, fn in self.collectors().items():
                with self.subTest(output=label, collector=name):
                    self.box({"": text})       # every command answers this
                    try:
                        out = fn()
                    except Exception as e:      # noqa: BLE001
                        self.fail(f"{name} raised on {label}: {type(e).__name__}: {e}")
                    self.assertIsInstance(out, dict, f"{name} on {label}")

    def test_no_hostile_output_becomes_a_plausible_value(self):
        """The half of the contract the test above cannot see, since @guard makes every
        answer a dict: no refusal counted, no watt or degree read, no unit state made up
        out of garbage. Only the type was asserted, so a kernel collector that counted
        every journal line as a driver refusal passed (found in review, 2026-09-24)."""
        for label, text in self.HOSTILE.items():
            self.assertNotIn("NV_ERR_NO_MEMORY", text)
            self.assertNotIn("ActiveState=", text)
            with self.subTest(output=label):
                self.box({"": text})
                self.cp.KERNEL_LAST["count"] = None
                k = self.cp.collect_kernel()
                self.assertEqual((k.get("nvrm_oom_1h"), k.get("nvrm_last")), (0, None), k)
                g = self.cp.collect_gpu()
                if "error" not in g:
                    self.assertEqual((g["power_w"], g["temp_c"]), (None, None), g)
                u = self.cp.collect_units()
                if "error" not in u:
                    for unit, st in u["units"].items():
                        self.assertEqual((st["active"], st["sub"], st["enabled"]), ("?", "?", "?"), unit)

    def test_the_kernel_collector_counts_driver_refusals_only(self):
        self.box({"journalctl": (
            "2026-09-24T10:00:01+0200 spark kernel: usb 1-1: new device\n"
            "2026-09-24T10:00:02+0200 spark kernel: NVRM: nvAssertFailed NV_ERR_NO_MEMORY\n"
            "2026-09-24T10:00:03+0200 spark kernel: EXT4-fs (nvme0n1p2): re-mounted\n"
            "2026-09-24T10:00:04+0200 spark kernel: NVRM: alloc NV_ERR_NO_MEMORY\n")})
        self.cp.KERNEL_LAST["count"] = None
        k = self.cp.collect_kernel()
        self.assertEqual((k["nvrm_oom_1h"], k["nvrm_last"]), (2, "2026-09-24T10:00:04+0200"))

    def test_a_collector_that_cannot_parse_says_so_instead_of_guessing(self):
        """The contract of @guard: a failure is a named error in the payload, which
        the UI renders as a stale panel, never as a fresh zero."""
        self.box({"": self.HOSTILE["a stack trace"]})
        out = self.cp.collect_decode_telemetry()
        if "error" in out:
            self.assertRegex(out["error"], r"^[A-Za-z_]+Error|^[A-Za-z]+: ")
        else:
            self.assertIsNone(out.get("decode"),
                              "a stack trace was parsed into decode telemetry")

    def test_guard_turns_a_raise_into_a_named_error(self):
        @self.cp.guard
        def boom():
            raise ValueError("measured nothing")

        out = boom()
        self.assertEqual(out, {"error": "ValueError: measured nothing"})

    def test_guard_does_not_swallow_a_keyboard_interrupt(self):
        @self.cp.guard
        def stop():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            stop()

    def test_no_collector_reaches_a_real_command(self):
        """The stub records every argv: nothing may escape it during this suite."""
        fake = self.box({"": ""})
        for fn in self.collectors().values():
            fn()
        for argv in fake.calls:
            self.assertIsInstance(argv, list)
            self.assertTrue(all(isinstance(a, str) for a in argv), argv)


class RunItself(Base):
    """cockpit.run, the one place a subprocess is spawned."""

    def test_it_never_uses_a_shell(self):
        out = self.real_run(["echo", "$HOME;id"])
        self.assertIn("$HOME;id", out, "the argument was interpreted, not passed")

    def test_a_missing_binary_is_an_empty_string_not_an_exception(self):
        self.assertEqual(self.real_run(["/nonexistent/binary-xyz"]), "")

    def test_a_timeout_is_an_empty_string_not_an_exception(self):
        self.assertEqual(self.real_run(["sleep", "5"], timeout=0.3), "")

    def test_merge_err_is_what_reads_a_docker_log(self):
        """docker logs writes the container's stderr to ITS stderr: a reader
        without merge_err sees an empty string. This cost a night once."""
        script = "import sys; sys.stderr.write('on stderr\\n')"
        self.assertEqual(self.real_run(["python3", "-c", script]), "")
        self.assertIn("on stderr",
                      self.real_run(["python3", "-c", script], merge_err=True))


class FitVerdict(Base):
    """When the banner fires, the two numbers under it must be the two that failed.

    Seen on the box 2026-09-19: the proxy was carrying the flash lane's 250,000
    token ceiling while the 27B lane served, so opencode's 548,000 context could
    not be relayed. True warning, right headline, and then it printed "730,000
    asked, 827,968 servable", which is the worst case against the pool: a pair
    that says the ask FITS. A warning whose own numbers contradict it gets read
    as a bug in the cockpit, which is how a real misconfiguration survives a
    person looking straight at it.
    """

    def test_the_ceiling_case_shows_the_context_against_the_ceiling(self):
        """The box's own numbers that day."""
        v = self.cp.fit_verdict(ctx=548_000, outp=182_000, usable=827_968, prompt_cap=250_000)
        self.assertFalse(v["ok"])
        self.assertIn("proxy", v["why"])
        self.assertEqual((v["asked"], v["limit"]), (548_000, 250_000))

    def test_the_pool_case_shows_the_worst_case_against_the_pool(self):
        """No ceiling in force, so the prompt passes and the pair is the other one."""
        v = self.cp.fit_verdict(ctx=700_000, outp=200_000, usable=827_968, prompt_cap=827_968)
        self.assertFalse(v["ok"])
        self.assertIn("pool", v["why"])
        self.assertEqual((v["asked"], v["limit"]), (900_000, 827_968))

    def test_limits_that_fit_report_ok(self):
        v = self.cp.fit_verdict(ctx=558_000, outp=186_000, usable=827_968, prompt_cap=827_968)
        self.assertTrue(v["ok"])
        self.assertEqual(v["why"], "")

    def test_the_window_case_shows_the_request_against_the_window(self):
        """The flash pair of v1.18.6: it passes the proxy and the pool, and the engine still
        refuses it, because it takes input + max_tokens against its 262,144 window and the
        prompt reaches opencode's compaction point plus one step (found in review,
        2026-09-24). The ask shown is that request, against the window."""
        v = self.cp.fit_verdict(ctx=225_000, outp=32_000, usable=519_203, prompt_cap=250_000,
                                window=262_144)
        self.assertFalse(v["ok"])
        self.assertIn("window", v["why"])
        self.assertEqual((v["asked"], v["limit"]), (225_000 + 25_000 + 32_000, 262_144))
        ok = self.cp.fit_verdict(ctx=205_000, outp=32_000, usable=519_203, prompt_cap=250_000,
                                 window=262_144)
        self.assertTrue(ok["ok"], ok)
        # the 1M lane is far from its window: nothing about it changes
        self.assertTrue(self.cp.fit_verdict(ctx=558_000, outp=186_000, usable=827_968,
                                            prompt_cap=827_968, window=1_010_000)["ok"])

    def test_a_warning_never_shows_an_ask_below_its_limit(self):
        """The invariant the screenshot broke, over the whole grid."""
        for ctx in (1, 100_000, 250_000, 548_000, 700_000, 900_000):
            for outp in (0, 32_000, 186_000, 200_000):
                for usable in (250_000, 827_968, 900_000):
                    for cap in (200_000, 250_000, usable):
                        for window in (0, 262_144, 1_010_000):
                            v = self.cp.fit_verdict(ctx=ctx, outp=outp, usable=usable,
                                                    prompt_cap=min(cap, usable), window=window)
                            with self.subTest(ctx=ctx, outp=outp, usable=usable, cap=cap, window=window):
                                if v["ok"]:
                                    self.assertLessEqual(v["asked"], v["limit"])
                                else:
                                    self.assertGreater(v["asked"], v["limit"])


class TheBootBarNeverGoesBack(Base):
    """Some boots flood the log the lifecycle reads the last 300 lines of: inductor compile
    errors during the graph capture, 450 lines in six minutes on the reference box on
    22/09. Every milestone left the window, the parse read stage None, and the lane went
    from "capturing graphs" back to "starting". Within one activation the stage stays
    the last one the log proved; a new activation starts from what its own log says."""
    MILESTONES = "\n".join([
        "[2026-09-22 19:51:23] Load weight begin. avail mem=113.93 GB",
        "[2026-09-22 19:53:22] Load weight end. elapsed=118.38 s, type=Qwen3_5ForConditionalGeneration",
        "[2026-09-22 19:53:22] Load weight begin. avail mem=92.04 GB",
        "[2026-09-22 19:53:32] Load weight end. elapsed=9.44 s, type=DFlash2DraftModel",
        "[2026-09-22 19:53:38] KV Cache is allocated. dtype: torch.float8_e4m3fn, #tokens: 914573",
        "[2026-09-22 19:53:41] Capture target verify CUDA graph begin. backend=full",
    ]) + "\n"
    FLOOD = "  torch._dynamo.utils.warn_once(msg)\n" * 300
    U = "qwen38-sglang.service"

    def setUp(self):
        self.cp.BOOT_SEEN.clear()
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": False}}

    def tick(self, logs, enter):
        self.box({
            f"systemctl show {self.U}": f"ActiveState=active\nSubState=running\n"
                                        f"ActiveEnterTimestampMonotonic={enter}\n",
            "docker ps -q -f name=^qwen38-sglang$": "c0ffee\n",
            "docker logs --tail 300 qwen38-sglang": logs,
        })
        out = self.cp.collect_lifecycle()
        return (out.get("data", out))["engines"][self.U]["state"]

    def test_a_flood_inside_one_boot_keeps_the_stage_it_had_reached(self):
        self.assertEqual(self.tick(self.MILESTONES, enter="1000"), "capturing-graphs")
        self.assertEqual(self.tick(self.FLOOD, enter="1000"), "capturing-graphs")
        self.assertEqual(self.tick(self.FLOOD, enter="1000"), "capturing-graphs")

    def test_a_new_activation_does_not_inherit_the_last_ones_stage(self):
        self.assertEqual(self.tick(self.MILESTONES, enter="1000"), "capturing-graphs")
        self.assertEqual(self.tick(self.FLOOD, enter="2000"), "starting")

    def test_evidence_always_wins_over_what_was_seen(self):
        self.assertEqual(self.tick(self.MILESTONES, enter="1000"), "capturing-graphs")
        fired = self.MILESTONES + "[2026-09-22 19:58:40] The server is fired up and ready to roll!\n"
        self.assertEqual(self.tick(fired, enter="1000"), "warming-up")

    def test_a_cockpit_started_in_the_middle_of_the_flood_reads_the_start_once(self):
        self.cp.BOOT_HEAD_READ.clear()
        reads = []
        for _ in range(3):
            fake = self.box({
                f"systemctl show {self.U}": "ActiveState=active\nSubState=running\n"
                                            "ActiveEnterTimestampMonotonic=1000\n",
                "docker ps -q -f name=^qwen38-sglang$": "c0ffee\n",
                "docker logs --tail 300 qwen38-sglang": self.FLOOD,
                "docker logs --since": self.MILESTONES})
            out = self.cp.collect_lifecycle()
            self.assertEqual(out.get("data", out)["engines"][self.U]["state"], "capturing-graphs")
            reads.append(sum(1 for c in fake.calls if c[:3] == ["docker", "logs", "--since"] and "--until" in c))
        self.assertEqual(reads, [1, 0, 0], "the start of the boot is read once per activation")

    def test_a_serving_engine_that_lost_health_is_degraded_not_starting(self):
        """Its tail is decode lines by then. Before the head read, a cockpit that had not
        watched this boot parsed stage None there, and a serving engine read "starting"."""
        self.cp.BOOT_HEAD_READ.clear()
        fired = self.MILESTONES + "[2026-09-22 19:58:40] The server is fired up and ready to roll!\n"
        decode = "[2026-09-22 20:40:00] Decode batch, #running-req: 1, #token: 5000, gen throughput (token/s): 70.1\n" * 300
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {self.U: "ready"}
        self.cp.UNHEALTHY_TICKS[self.U] = 5            # past the hysteresis: it really lost health
        self.box({f"systemctl show {self.U}": "ActiveState=active\nSubState=running\n"
                                              "ActiveEnterTimestampMonotonic=1000\n",
                  "docker ps -q -f name=^qwen38-sglang$": "c0ffee\n",
                  "docker logs --tail 300 qwen38-sglang": decode,
                  "docker logs --since": fired})
        out = self.cp.collect_lifecycle()
        self.assertEqual(out.get("data", out)["engines"][self.U]["state"], "degraded")



class TheLaneSaysWhyTheStartGuardHoldsIt(Base):
    """engine-preflight.sh writes to the unit's journal, and while it waits there is no
    container, so the container log the lifecycle reads never had its reason: a held lane
    read "starting", a refused one "keeps crashing" (issue #26)."""
    U = "qwen38-sglang.service"
    W = ("preflight: qwen38-sglang.service waits: what is left of an engine still holds 16 GiB of GPU "
         "memory: 1756018 sglang::stuck 16578 MiB\n")
    R = ("preflight: NOT starting qwen38-sglang.service: the GPU driver holds 50.9 GiB that no process "
         "accounts for, still after 60 s. Starting into memory the driver has not given back is how "
         "sglang#40948 froze a DGX Spark until a power cycle. Stop what holds it, or reboot if nothing "
         "does; systemd tries again in 15 s.\n")

    def setUp(self):
        self.cp.BOOT_SEEN.clear()
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": False}}

    def tick(self, active, sub, journal, container=""):
        fake = self.box({f"systemctl show {self.U}": f"ActiveState={active}\nSubState={sub}\n"
                                                     "ActiveEnterTimestampMonotonic=0\nInvocationID=abc123\n",
                         "docker ps -q -f name=^qwen38-sglang$": container,
                         "_SYSTEMD_INVOCATION_ID=abc123": journal})
        out = self.cp.collect_lifecycle()
        return out.get("data", out)["engines"][self.U], fake

    def test_held_while_it_waits(self):
        e, fake = self.tick("activating", "start-pre", self.W)
        self.assertEqual(e["state"], "starting")
        self.assertEqual(e["held"], "what is left of an engine still holds 16 GiB of GPU memory: "
                                    "1756018 sglang::stuck 16578 MiB")
        self.assertIsNone(e["refused"])
        reads = [c for c in fake.calls if c[:1] == ["journalctl"] and self.U in c]
        self.assertEqual(len(reads), 1)
        self.assertIn("-g", reads[0])
        self.assertIn("_SYSTEMD_INVOCATION_ID=abc123", reads[0], "this run's lines, never an older run's")

    def test_refused_between_two_attempts(self):
        e, _ = self.tick("activating", "auto-restart", self.R)
        self.assertEqual((e["state"], e["restarting"]), ("failed", True))
        self.assertEqual(e["refused"], "the GPU driver holds 50.9 GiB that no process accounts for")
        self.assertIsNone(e["held"])

    def test_a_lane_that_is_simply_starting_has_no_reason(self):
        e, _ = self.tick("activating", "start-pre", "")
        self.assertEqual(e["state"], "starting")
        self.assertIsNone(e["held"])

    def test_the_guards_lines_are_read_in_the_order_they_were_written(self):
        """journalctl 255 prints the lines -g finds newest first once -n is given too (measured on
        the reference box, 2026-10-19), and the last word read was the guard's first: a start it
        refused after a wait read as one it still held, and failed read "keeps crashing"."""
        e, fake = self.tick("activating", "auto-restart", self.W + self.R)
        self.assertEqual(e["refused"], "the GPU driver holds 50.9 GiB that no process accounts for")
        read = [c for c in fake.calls if c[:1] == ["journalctl"] and self.U in c][0]
        self.assertIn("-g", read)
        for flag in ("-n", "--lines", "-r", "--reverse"):
            self.assertNotIn(flag, read)

    def test_a_gpu_that_cannot_start_the_lane_reaches_the_page_as_such(self):
        """The flash launcher's line on a GPU outside its ATS mode, after the guard's own: the page
        is told "cannot", the last word, never the guard's "starts" (issue #54). The collector
        handed the page its fields one by one and left this one out (found on the reference box,
        2026-10-19)."""
        s = "preflight: qwen38-sglang.service starts: no GPU memory held from before, 116.6 GiB available (1 s)\n"
        c = ("preflight: qwen38-sglang.service cannot start: the GPU's addressing mode is None "
             "(nvidia-smi --query-gpu=addressing_mode) and the flash lane needs ATS\n")
        e, _ = self.tick("failed", "failed", s + c)
        self.assertEqual(e["state"], "failed")
        self.assertEqual(e["cannot"], "the GPU's addressing mode is None (nvidia-smi "
                                      "--query-gpu=addressing_mode) and the flash lane needs ATS")
        self.assertIsNone(e["refused"])

    def test_a_serving_lane_never_reads_it(self):
        _, fake = self.tick("active", "running", self.W, container="c0ffee\n")
        self.assertFalse([c for c in fake.calls if c[:1] == ["journalctl"] and self.U in c and "-g" in c])


class AServerWithoutItsSchedulerIsRestartedOnce(Base):
    """SGLang v0.5.19 can be left with its server process alive and no scheduler (a SIGQUIT
    that reaches the server while the scheduler runs; reproduced on the reference box,
    2026-10-01): no HTTP, a unit systemd calls active, and nothing restarts it."""
    U = "qwen38-sglang.service"
    FIRED = ("[2026-10-01 12:12:30] Load weight end. elapsed=119.55 s\n"
             "[2026-10-01 12:17:42] The server is fired up and ready to roll!\n")

    def setUp(self):
        self.cp.BOOT_SEEN.clear()
        self.cp.BOOT_HEAD_READ.clear()
        self.cp.ZOMBIE_SINCE.clear()
        self.cp.LAST_ZOMBIE_RESTART.clear()
        self.actions = []
        real_alive, real_start = self.cp.scheduler_alive, self.cp.start_action
        self.addCleanup(setattr, self.cp, "scheduler_alive", real_alive)
        self.addCleanup(setattr, self.cp, "start_action", real_start)
        self.cp.start_action = lambda kind, args, origin="": (self.actions.append((kind, args, origin)), (0, "ok"))[1]
        self.alive = False
        self.cp.scheduler_alive = lambda container: self.alive

    def tick(self):
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": False}}
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {self.U: "degraded"}
        self.cp.UNHEALTHY_TICKS[self.U] = 5
        self.cp.LAST_PROGRESS["ts"] = None
        self.box({f"systemctl show {self.U}": "ActiveState=active\nSubState=running\n"
                                              "ActiveEnterTimestampMonotonic=1000\nInvocationID=abc\n",
                  "docker ps -q -f name=^qwen38-sglang$": "c0ffee\n",
                  "docker logs --tail 300 qwen38-sglang": self.FIRED,
                  "docker logs --since": self.FIRED})
        out = self.cp.collect_lifecycle()
        return out.get("data", out)["engines"][self.U]

    def test_it_is_named_then_restarted_once_after_its_delay(self):
        e = self.tick()
        self.assertEqual((e["state"], e["zombie"], e["zombie_in"]), ("degraded", True, 120))
        self.assertEqual(self.actions, [])
        self.cp.ZOMBIE_SINCE[self.U] = __import__("time").time() - 121
        self.tick()
        self.assertEqual(self.actions, [("unit", {"verb": "restart", "unit": self.U}, "zombie")])
        self.tick()
        self.assertEqual(len(self.actions), 1, "one restart per half hour, not one per tick")

    def test_a_server_with_its_scheduler_is_left_alone(self):
        self.alive = True
        e = self.tick()
        self.assertFalse(e["zombie"])
        self.assertNotIn(self.U, self.cp.ZOMBIE_SINCE)
        self.alive = None                     # an unreadable process list decides nothing
        self.assertFalse(self.tick()["zombie"])
        self.assertEqual(self.actions, [])

    def test_off_it_says_so_and_does_nothing(self):
        self.cp.ZOMBIE_RESTART = False
        self.addCleanup(setattr, self.cp, "ZOMBIE_RESTART", True)
        self.cp.ZOMBIE_SINCE[self.U] = __import__("time").time() - 600
        self.assertTrue(self.tick()["zombie"])
        self.assertEqual(self.actions, [])


class AWedgedEngineTheAutohealRestartsIsARecovery(Base):
    """Decision 1049: the autoheal's restart of a wedged engine is the restart the proxy took for an order when an update
    had staged another checkpoint (the 4th review of v1.43.0, 2026-11-04). Its decision to restart is given here (the
    wedge's own tests are lifecycle.py's); what is checked is the action it asks, origin autoheal."""
    U = "qwen38-sglang.service"
    FIRED = AServerWithoutItsSchedulerIsRestartedOnce.FIRED

    def setUp(self):
        cp = self.cp
        saved = (cp.start_action, cp.lc.decide_wedge, cp.lc.wedge_plan, cp.POOL_GUARD)
        self.addCleanup(lambda: (setattr(cp, "start_action", saved[0]), setattr(cp.lc, "decide_wedge", saved[1]),
                                 setattr(cp.lc, "wedge_plan", saved[2]), setattr(cp, "POOL_GUARD", saved[3])))
        self.actions = []
        cp.start_action = lambda kind, args, origin="": (self.actions.append((kind, args, origin)), (202, {}))[1]
        cp.lc.decide_wedge = lambda **kw: True
        cp.lc.wedge_plan = lambda **kw: {"state": "wedged", "since": 0.0, "first": False, "restart": True}
        cp.POOL_GUARD = False

    def test_the_autoheal_restarts_a_wedged_engine_as_a_recovery(self):
        cp = self.cp
        with cp.STATE_LOCK:
            cp.STATE["engine_fast"] = {"data": {"healthy": True, "load": [{"num_reqs": 1}]}}
        with cp.LIFE_LOCK:
            cp.LIFE["states"] = {self.U: "ready"}
        cp.READY_SINCE[self.U] = __import__("time").time() - 3600
        self.addCleanup(cp.READY_SINCE.pop, self.U, None)
        self.box({f"systemctl show {self.U}": "ActiveState=active\nSubState=running\n"
                                              "ActiveEnterTimestampMonotonic=1000\nInvocationID=run-7\n",
                  "docker ps -q -f name=^qwen38-sglang$": "c0ffee\n",
                  "docker logs --tail 300 qwen38-sglang": self.FIRED,
                  "docker logs --since": self.FIRED})
        cp.collect_lifecycle()
        # its origin is what its job marks for the proxy (ARecoveryRestartIsMarkedByItsOwnJob)
        self.assertEqual(self.actions, [("unit", {"verb": "restart", "unit": self.U}, "autoheal")])


class TheLifecycleTickKeepsTheRunItRead(Base):
    """A recovery's job falls back on the run the lifecycle tick read when systemd does not say it at the restart (the 7th
    review of v1.43.0). A tick whose read failed erased it, where systemd slow under memory pressure, a wedged engine's
    very case, fails both (found re-reading that fix, 2026-11-04). In a dry run: no action of the tick runs."""
    U = "qwen38-sglang.service"

    def test_a_tick_whose_read_failed_keeps_the_run_read_before(self):
        cp = self.cp
        self.addCleanup(cp.LAST_RUN.pop, self.U, None)
        self.box({f"systemctl show {self.U}": "ActiveState=active\nSubState=running\nInvocationID=run-7\n"})
        cp.collect_lifecycle()
        self.assertEqual(cp.LAST_RUN.get(self.U), "run-7")
        self.box({})
        cp.collect_lifecycle()
        self.assertEqual(cp.LAST_RUN.get(self.U), "run-7", "a read that failed erased the run read before")


class ARecoveryRestartIsMarkedByItsOwnJob(Base):
    """Decisions 1049 and 1058: the job that runs a recovery restart (autoheal, zombie) marks it for the proxy just before
    its systemctl, under JOB_LOCK, and removes the mark when that fails. Marked where the action was asked, a duplicate
    refused busy erased the running restart's mark, a refused sudo left it, and two threads wrote the file at once (the
    6th review of v1.43.0, 2026-11-04). The job's command is this test's: a shell that says whether the mark was there
    when it ran, never systemctl."""
    U = "qwen38-sglang.service"

    def setUp(self):
        cp = self.cp
        self.marks = cp.CONFIG_DIR / "recovery-restarts.json"
        self.marks.unlink(missing_ok=True)
        self.addCleanup(self.marks.unlink, missing_ok=True)
        dry = cp.DRY_RUN
        cp.DRY_RUN = False                         # the job's command below is this test's own
        self.addCleanup(setattr, cp, "DRY_RUN", dry)
        self.box({f"systemctl show {self.U}": "InvocationID=run-7\n"})

    def run_job(self, origin, rc):
        cp = self.cp
        argv = ["/bin/sh", "-c", f'grep -q run-7 "{self.marks}" 2>/dev/null && exit {rc}; exit 9']
        job = cp.Job("unit", argv, 30, params={"verb": "restart", "unit": self.U}, origin=origin)
        self.assertTrue(cp.JOB_LOCK.acquire(blocking=False), "a job still holds the lock")
        cp.run_job(job)                            # it releases JOB_LOCK
        return job

    def mark(self):
        return json.loads(self.marks.read_text()).get(self.U) if self.marks.exists() else None

    def test_a_recovery_restart_is_marked_before_it_runs_and_kept_once_it_ran(self):
        job = self.run_job("autoheal", 0)
        self.assertEqual(job.rc, 0, f"no mark when its command ran (9): {job.lines}")
        self.assertEqual((self.mark() or {}).get("why"), "autoheal")

    def test_a_recovery_restart_that_failed_leaves_no_mark(self):
        job = self.run_job("zombie", 1)
        self.assertEqual(job.rc, 1, f"no mark when its command ran (9): {job.lines}")
        self.assertIsNone(self.mark(), "a restart that failed left its mark")

    def test_an_operators_restart_is_no_recovery(self):
        job = self.run_job("ui", 0)
        self.assertEqual(job.rc, 9)
        self.assertIsNone(self.mark())

    def test_a_restart_systemd_took_keeps_its_mark_when_systemctl_failed(self):
        # systemctl can fail after PID 1 took the job (sd-bus's timeout under memory pressure): its mark, removed, let
        # that restart read as an order (the 7th review of v1.43.0, 2026-11-04)
        self.box({f"systemctl show {self.U}": "InvocationID=run-7\n",
                  "systemctl list-jobs": f"12 {self.U} restart running\n"})
        job = self.run_job("autoheal", 1)
        self.assertEqual(job.rc, 1, job.lines)
        self.assertEqual((self.mark() or {}).get("invocation"), "run-7", "the mark of a restart under way was removed")

    def test_a_restart_systemd_ran_already_keeps_its_mark_when_systemctl_failed(self):
        # its job gone already, the unit's run another: that restart went, and its mark stays
        shown = []

        def answer(argv, timeout=5.0, merge_err=False):
            if argv[:2] == ["systemctl", "show"]:
                shown.append(argv)
                return "InvocationID=run-7\n" if len(shown) == 1 else "InvocationID=run-8\n"
            return ""
        self.cp.run = answer
        job = self.run_job("autoheal", 1)
        self.assertEqual(job.rc, 1, job.lines)
        self.assertEqual((self.mark() or {}).get("invocation"), "run-7", "the mark of a restart that went was removed")

    def test_a_restart_whose_systemd_does_not_answer_keeps_its_mark(self):
        # systemd silent under memory pressure, the very case of a systemctl that failed after PID 1 took its job: its
        # silence read as no restart removed the mark (the 8th review of v1.43.0, 2026-11-05)
        cp = self.cp
        for silent in ("list-jobs", "show"):
            with self.subTest(silent=silent):
                self.marks.unlink(missing_ok=True)
                shown = []

                def answer(argv, timeout=5.0, merge_err=False, errors="strict"):
                    if argv[:2] == ["systemctl", "show"] and not shown:
                        shown.append(argv)
                        return "InvocationID=run-7\n"                  # read for the mark, before the restart
                    if argv[1] == silent:
                        out = cp.Ran("")                                # a timeout: "" and not ok
                        out.ok = False
                        return out
                    return "" if argv[1] == "list-jobs" else "InvocationID=run-7\n"
                cp.run = answer
                job = self.run_job("autoheal", 1)
                self.assertEqual(job.rc, 1, job.lines)
                self.assertEqual((self.mark() or {}).get("invocation"), "run-7", f"systemd's silence ({silent}) "
                                                                                "removed the mark")

    def test_a_restart_whose_stop_is_done_keeps_its_mark(self):
        # its job reads "start" once its stop is done (systemd's job.c patches a restart job so)
        self.box({f"systemctl show {self.U}": "InvocationID=run-7\n",
                  "systemctl list-jobs": f"12 {self.U} start waiting\n"})
        job = self.run_job("zombie", 1)
        self.assertEqual(job.rc, 1, job.lines)
        self.assertEqual((self.mark() or {}).get("invocation"), "run-7", "a restart past its stop lost its mark")

    def test_a_run_systemd_does_not_say_now_is_the_ticks(self):
        # systemd slow to answer at the restart: no mark and nothing said, the proxy then reading the restart of a lane
        # with a staged checkpoint as an order (the 7th review of v1.43.0, 2026-11-04)
        self.box({f"systemctl show {self.U}": ""})
        self.cp.LAST_RUN[self.U] = "run-7"
        self.addCleanup(self.cp.LAST_RUN.pop, self.U, None)
        job = self.run_job("zombie", 0)
        self.assertEqual(job.rc, 0, f"no mark when its command ran (9): {job.lines}")
        self.assertEqual((self.mark() or {}).get("invocation"), "run-7")

    def test_a_restart_with_no_run_known_says_so(self):
        self.box({f"systemctl show {self.U}": ""})
        self.cp.LAST_RUN.pop(self.U, None)
        events = []
        saved = self.cp.add_event
        self.cp.add_event = lambda kind, text: events.append((kind, text))
        self.addCleanup(setattr, self.cp, "add_event", saved)
        self.run_job("autoheal", 0)
        self.assertTrue(any("could not be marked for the proxy" in text for _, text in events), events)

    def test_a_write_that_fails_leaves_no_temp_file(self):
        # names per thread piled up when a write or its rename failed (the 7th review of v1.43.0, 2026-11-04)
        cp = self.cp
        d = cp.CONFIG_DIR / "marks-as-a-dir"
        d.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, d, True)
        saved = cp.RECOVERY_FILE
        cp.RECOVERY_FILE = d                                                    # a rename onto a directory fails
        self.addCleanup(setattr, cp, "RECOVERY_FILE", saved)
        cp._write_recovery_marks({self.U: {"invocation": "run-7", "at": 1.0, "why": "autoheal"}})
        self.assertEqual(list(cp.CONFIG_DIR.glob("marks-as-a-dir*.tmp")), [])

    def test_only_the_job_writes_the_marks(self):
        src = (DASH / "cockpit.py").read_text()
        calls = [m.start() for m in re.finditer(r"(?<!def )\bmark_recovery\(", src)]
        start = src.index("\ndef run_job(")
        self.assertEqual(len(calls), 1, "a mark written elsewhere than by run_job")
        self.assertTrue(start < calls[0] < src.index("\ndef ", start + 1), "a mark written elsewhere than by run_job")


class ACrashWhileServingIsNoCrashLoop(Base):
    """A serving engine that crashes waits 15 s for its next attempt, exactly like a boot that
    keeps dying, and the page said "keeps crashing, it dies during startup" for both (seen
    2026-10-01 after one crash of the serving flash lane). The lane tells them apart now."""
    U = "qwen38-sglang.service"

    def tick(self, active, sub, prev):
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {self.U: prev}
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": False}}
        self.box({f"systemctl show {self.U}": f"ActiveState={active}\nSubState={sub}\n"
                                              "ActiveEnterTimestampMonotonic=0\nInvocationID=abc\nNRestarts=1\n",
                  "docker ps -q -f name=^qwen38-sglang$": ""})
        out = self.cp.collect_lifecycle()
        return out.get("data", out)["engines"][self.U]

    def test_down_while_serving_then_down_at_boot(self):
        self.cp.CRASHED_SERVING.clear()
        e = self.tick("activating", "auto-restart", "ready")
        self.assertEqual((e["state"], e["restarting"], e["crashed_serving"]), ("failed", True, True))
        e = self.tick("activating", "auto-restart", "failed")       # the rest of the same 15 s
        self.assertTrue(e["crashed_serving"])
        e = self.tick("activating", "start", "failed")              # the next attempt boots
        self.assertEqual((e["state"], e["crashed_serving"]), ("starting", False))
        e = self.tick("activating", "auto-restart", "starting")     # and dies at boot: a loop
        self.assertEqual((e["state"], e["restarting"], e["crashed_serving"]), ("failed", True, False))

    def test_a_degraded_or_wedged_engine_that_goes_down_was_serving_too(self):
        for prev in ("degraded", "wedged"):
            with self.subTest(prev=prev):
                self.cp.CRASHED_SERVING.clear()
                self.assertTrue(self.tick("activating", "auto-restart", prev)["crashed_serving"])


class AWedgedEngineThatLosesHealthIsDegraded(Base):
    """A wedge is only reachable from ready, so when that engine also stops answering it has
    long been serving: "degraded". The check that keeps a fresh boot in warming-up did not
    know the state, and drew a boot bar with an overdue warning over it (found in review,
    2026-09-24)."""
    U = "qwen38-sglang.service"

    def test_it_reads_degraded_not_warming_up(self):
        self.cp.BOOT_HEAD_READ.clear()
        self.cp.BOOT_SEEN.clear()
        fired = ("[2026-09-22 19:53:22] Load weight end. elapsed=118.38 s\n"
                 "[2026-09-22 19:58:40] The server is fired up and ready to roll!\n")
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": False}}
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {self.U: "wedged"}
        self.cp.UNHEALTHY_TICKS[self.U] = 5
        self.cp.LAST_PROGRESS["ts"] = None
        self.box({f"systemctl show {self.U}": "ActiveState=active\nSubState=running\n"
                                              "ActiveEnterTimestampMonotonic=1000\n",
                  "docker ps -q -f name=^qwen38-sglang$": "c0ffee\n",
                  "docker logs --tail 300 qwen38-sglang": fired,
                  "docker logs --since": fired})
        out = self.cp.collect_lifecycle()
        self.assertEqual(out.get("data", out)["engines"][self.U]["state"], "degraded")


class ACockpitRestartFacingAServingEngine(Base):
    """Every cockpit restart logged "warming-up -> ready" for the serving lane two seconds
    later (five times in the event log of 2026-09-29/30). The lifecycle tier's first pass
    ran before the first health sample existed, read "no sample" as "unhealthy", and
    turned the "degraded" that gave into "warming-up", having no previous state. And an
    engine that really was not answering when the cockpit came up read "warming up" for as
    long as it lasted: a boot state, which holds every switch back."""
    U = "qwen38-flash.service"
    FIRED = ("[2026-09-30 15:16:03] KV Cache is allocated. dtype: torch.bfloat16, #tokens: 491136\n"
             "[2026-09-30 15:16:35] The server is fired up and ready to roll!\n")
    NOW = 100_000.0

    def setUp(self):
        self.cp.BOOT_HEAD_READ.clear()
        self.cp.BOOT_SEEN.clear()
        self.cp.UNHEALTHY_TICKS.clear()
        self.cp.LAST_PROGRESS["ts"] = None
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"], self.cp.LIFE["witnessed"], self.cp.LIFE["enter"] = {}, {}, {}
        (self.tmp / self.cp.HISTORY_FILE_NAME).unlink(missing_ok=True)
        self.saved_mono = self.cp.monotonic_now
        self.cp.monotonic_now = lambda: self.NOW

    def tearDown(self):
        self.cp.monotonic_now = self.saved_mono
        (self.tmp / self.cp.HISTORY_FILE_NAME).unlink(missing_ok=True)
        super().tearDown()

    def tick(self, healthy, age_s):
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": healthy}}
        enter = int((self.NOW - age_s) * 1e6)
        self.box({f"systemctl show {self.U}": "ActiveState=active\nSubState=running\n"
                                              f"ActiveEnterTimestampMonotonic={enter}\n",
                  "docker ps -q -f name=^qwen38-flash$": "c0ffee\n",
                  "docker logs --tail 300 qwen38-flash": self.FIRED,
                  "docker logs --since": self.FIRED})
        out = self.cp.collect_lifecycle()
        return out.get("data", out)

    def test_the_other_tiers_start_once_health_has_been_sampled(self):
        import threading
        import time
        seen = []
        with self.cp.STATE_LOCK:
            self.cp.STATE.pop("engine_fast", None)

        def spawn(period, cols):
            if "engine_fast" in cols:
                def first_sample():
                    time.sleep(0.3)
                    with self.cp.STATE_LOCK:
                        self.cp.STATE["engine_fast"] = {"data": {"healthy": True}}
                threading.Thread(target=first_sample, daemon=True).start()
                return
            with self.cp.STATE_LOCK:
                seen.append((tuple(cols), "engine_fast" in self.cp.STATE))
        self.cp.start_samplers(spawn=spawn, wait_s=5)
        rest = [t for t in self.cp.TIERS if "engine_fast" not in t[1]]
        deadline = time.time() + 6
        while len(seen) < len(rest) and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(seen), len(rest), seen)
        self.assertTrue(all(had for _, had in seen), seen)
        self.assertIn((("lifecycle",), True), seen)

    def test_a_health_tier_that_never_answers_does_not_hold_the_others_back(self):
        import time
        started = []
        with self.cp.STATE_LOCK:
            self.cp.STATE.pop("engine_fast", None)
        self.cp.start_samplers(spawn=lambda period, cols: started.append(tuple(cols)), wait_s=0.2)
        deadline = time.time() + 3
        while len(started) < len(self.cp.TIERS) and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(started), len(self.cp.TIERS), started)

    def test_an_engine_fired_up_hours_ago_and_silent_is_degraded(self):
        d = self.tick(healthy=False, age_s=3 * 3600)
        self.assertEqual(d["engines"][self.U]["state"], "degraded")
        self.assertNotIn("switch", d["blocked"], "a lane that has served is no boot in progress")

    def test_an_activation_younger_than_a_boot_still_reads_warming_up(self):
        d = self.tick(healthy=False, age_s=60)
        self.assertEqual(d["engines"][self.U]["state"], "warming-up")
        self.assertIn("switch", d["blocked"], "a warm-up holds the switch back, which is why the line matters")

    def test_a_boot_this_cockpit_watched_keeps_its_warm_up(self):
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["witnessed"][self.U] = True
        d = self.tick(healthy=False, age_s=3 * 3600)
        self.assertEqual(d["engines"][self.U]["state"], "warming-up")

    def test_the_line_is_the_boxs_own_median_boot_plus_the_grace(self):
        (self.tmp / self.cp.HISTORY_FILE_NAME).write_text(json.dumps({self.U: [600.0, 610.0, 620.0]}))
        edge = 610.0 + self.cp.WARMUP_GRACE_S
        self.assertEqual(self.tick(healthy=False, age_s=edge - 5)["engines"][self.U]["state"], "warming-up")
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {}
        self.assertEqual(self.tick(healthy=False, age_s=edge + 5)["engines"][self.U]["state"], "degraded")

    def test_an_engine_that_answers_is_ready_at_the_first_pass(self):
        d = self.tick(healthy=True, age_s=3 * 3600)
        self.assertEqual(d["engines"][self.U]["state"], "ready")



class ABootLandsUnderTheCheckpointItBooted(Base):
    """A boot is learned per checkpoint (v1.30.0), and the checkpoint is the one the activation
    started on: the unit's file names the NEXT start's, and one rewritten while the lane boots
    (an update, or a switch whose restart has not come yet) put the boot in that series (found
    in review, 2026-10-14)."""
    U = "qwen38-flash.service"
    MILESTONES = ("[2026-10-14 15:00:01] Load weight begin. avail mem=113.93 GB\n"
                  "[2026-10-14 15:02:00] Load weight end. elapsed=118.38 s, type=Qwen3_8NextForCausalLM\n"
                  "[2026-10-14 15:02:06] KV Cache is allocated. dtype: torch.bfloat16, #tokens: 491136\n"
                  "[2026-10-14 15:02:09] Capture cuda graph begin. This can take up to several minutes.\n")
    FIRED = MILESTONES + "[2026-10-14 15:03:00] The server is fired up and ready to roll!\n"
    NOW = 100_000.0

    def setUp(self):
        self.cp.BOOT_HEAD_READ.clear()
        self.cp.BOOT_SEEN.clear()
        self.cp.UNHEALTHY_TICKS.clear()
        self.cp.LAST_PROGRESS["ts"] = None
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"], self.cp.LIFE["witnessed"], self.cp.LIFE["enter"] = {}, {}, {}
            self.cp.LIFE.pop("boot_target", None)
        (self.tmp / self.cp.HISTORY_FILE_NAME).unlink(missing_ok=True)
        self.addCleanup((self.tmp / self.cp.HISTORY_FILE_NAME).unlink, missing_ok=True)
        self.addCleanup(setattr, self.cp, "monotonic_now", self.cp.monotonic_now)
        self.cp.monotonic_now = lambda: self.NOW
        self.addCleanup(setattr, self.cp, "unit_target", self.cp.unit_target)
        self.target = "flash"
        self.cp.unit_target = lambda unit: ({"model": "m", "target": self.target} if unit == self.U
                                            else {"model": None, "target": None})

    def tick(self, active, sub, enter_s, logs, healthy, now):
        """One pass of the lifecycle tier at monotonic time `now`, the unit's activation at
        `enter_s`."""
        self.NOW = now
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": healthy}}
        self.box({f"systemctl show {self.U}": f"ActiveState={active}\nSubState={sub}\n"
                                              f"ActiveEnterTimestampMonotonic={int(enter_s * 1e6)}\n",
                  "docker ps -q -f name=^qwen38-flash$": "c0ffee\n",
                  "docker logs --tail 300 qwen38-flash": logs,
                  "docker logs --since": logs})
        out = self.cp.collect_lifecycle()
        return out.get("data", out)["engines"][self.U]["state"]

    def boots(self):
        path = self.tmp / self.cp.HISTORY_FILE_NAME
        history = json.loads(path.read_text()) if path.exists() else {}
        return {k: v for k, v in history.items() if ":boot:" in k}

    def restart_and_boot(self, rewrite_during_boot):
        old, new = 89_200.0, 100_010.0                 # the serving activation, then the new one
        self.assertEqual(self.tick("active", "running", old, self.FIRED, True, now=100_000.0), "ready")
        self.target = "flash-nvda"                     # ./switch-model.sh flash-nvda rewrites the unit
        self.tick("deactivating", "stop-sigterm", old, self.FIRED, False, now=100_005.0)   # and restarts it
        self.assertIn(self.tick("active", "running", new, self.MILESTONES, False, now=100_210.0),
                      self.cp.lc.TRANSITIONAL)
        if rewrite_during_boot:
            self.target = "flash"                      # the unit rewritten again while it boots
        self.assertEqual(self.tick("active", "running", new, self.FIRED, True, now=100_270.0), "ready")
        return self.boots()

    def test_a_unit_rewritten_during_the_boot_does_not_move_it(self):
        self.assertEqual(self.restart_and_boot(True), {f"{self.U}:boot:flash-nvda": [260.0]})

    def test_a_boot_with_its_unit_unchanged_lands_under_it(self):
        self.assertEqual(self.restart_and_boot(False), {f"{self.U}:boot:flash-nvda": [260.0]})

class AStopTimeoutIsNotACrash(Base):
    """A unit systemd killed because it did not stop in time ends "failed" with
    Result=timeout: that is how a Stop during a generation looked on 2026-09-23, and the
    page said "failed, read its journal" about a lane nothing had gone wrong with. The
    lifecycle carries systemd's own word for how the run ended, and the page reads it."""
    U = "qwen38-sglang.service"

    def test_the_result_travels_with_the_engine(self):
        self.box({f"systemctl show {self.U}": "ActiveState=failed\nSubState=failed\nResult=timeout\n"
                                              "ActiveEnterTimestampMonotonic=1000\n"})
        out = self.cp.collect_lifecycle()
        e = out.get("data", out)["engines"][self.U]
        self.assertEqual((e["state"], e["result"]), ("failed", "timeout"))

    def test_the_page_tells_a_stop_timeout_from_a_crash(self):
        js = (REPO / "dashboard" / "static" / "js" / "base.js").read_text()
        self.assertIn("e.state === 'failed' && e.result === 'timeout'", js)
        self.assertIn("was killed while stopping.", js)


class TheCockpitsOwnProbeIsNotAClient(Base):
    """GET /health runs a one-token generation in these builds (input_ids=[0],
    max_new_tokens=1, SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION on), and the cockpit sends
    it every 30 s while the engine is idle. Its prefill line counted as client activity:
    the canary, which waits for 60 s without any, never ran once (0 in the audit log of
    the reference box, 2026-09-24, 334 probe lines in 3 h), and the pool guard read the
    probe's 0.00 over the last real usage. The lines are the live ones, verbatim."""
    PROBE = ("[2026-09-24 09:44:53] Prefill batch, #new-seq: 1, #new-token: 1, #cached-token: 0, "
             "full token usage: 0.00, mamba usage: 0.00, #running-req: 0, #queue-req: 0, "
             "#pending-token: 0, cuda graph: False, input throughput (token/s): 0.03\n")
    REAL = ("[2026-09-24 09:45:10] Prefill batch, #new-seq: 1, #new-token: 8192, #cached-token: 40960, "
            "full token usage: 0.71, mamba usage: 0.12, #running-req: 0, #queue-req: 0, "
            "#pending-token: 0, cuda graph: False, input throughput (token/s): 5210.40\n")

    def setUp(self):
        self.cp.LAST_PROGRESS["ts"] = None
        self.cp.LAST_USAGE.update(value=0.93, mamba=0.2, ts=1.0)

    def read(self, logs):
        self.box({"docker ps -q -f name=^qwen38-sglang$": "c0ffee\n",
                  "docker logs --since 30s qwen38-sglang": logs})
        self.cp.collect_decode_telemetry()

    def test_probe_lines_are_neither_activity_nor_a_pool_reading(self):
        self.read(self.PROBE * 3)
        self.assertIsNone(self.cp.LAST_PROGRESS["ts"])
        self.assertEqual(self.cp.LAST_USAGE["value"], 0.93, "the last real reading is kept")

    def test_a_client_line_still_is(self):
        self.read(self.PROBE + self.REAL)
        self.assertIsNotNone(self.cp.LAST_PROGRESS["ts"])
        self.assertEqual(self.cp.LAST_USAGE["value"], 0.71)

    def test_the_canary_runs_on_an_engine_that_only_answered_probes(self):
        self.read(self.PROBE * 3)
        saved = (self.cp.DRY_RUN, self.cp.ENGINE_BASE, dict(self.cp.CANARY))
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {"qwen38-sglang.service": "ready"}
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"load": [{"num_reqs": 0, "num_waiting_reqs": 0}]}}
        self.cp.DRY_RUN, self.cp.ENGINE_BASE = False, "http://127.0.0.1:9"   # closed: it fails fast
        try:
            out = self.cp.collect_canary()
        finally:
            self.cp.DRY_RUN, self.cp.ENGINE_BASE = saved[0], saved[1]
            self.cp.CANARY.clear(); self.cp.CANARY.update(saved[2])
        self.assertFalse(out.get("skipped"), "it was attempted, not skipped as 'engine busy'")

    def canary_with(self, load):
        """The canary against a closed port (a probe sent fails fast), the fast collector's
        last read saying idle, and the engine asked again answering `load` (a callable)."""
        saved = (self.cp.DRY_RUN, self.cp.ENGINE_BASE, self.cp.engine_load, dict(self.cp.CANARY))
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {"qwen38-sglang.service": "ready"}
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"load": [{"num_reqs": 0, "num_waiting_reqs": 0}]}}
        self.cp.DRY_RUN, self.cp.ENGINE_BASE, self.cp.engine_load = False, "http://127.0.0.1:9", load
        self.cp.CANARY.update(fails=0, last_err="")
        try:
            return self.cp.collect_canary(), dict(self.cp.CANARY)
        finally:
            self.cp.DRY_RUN, self.cp.ENGINE_BASE, self.cp.engine_load = saved[0], saved[1], saved[2]
            self.cp.CANARY.clear(); self.cp.CANARY.update(saved[3])

    def test_a_client_that_started_since_the_last_load_read_keeps_the_canary_out(self):
        # 2026-10-11 00:23: the last read said idle, a client's 80,000-token prompt had just
        # started, and the probe queued behind it until it timed out, a failure the wedge
        # rule counts; the engine asked again right before the probe says it is busy
        out, canary = self.canary_with(lambda timeout=3.0: [{"num_reqs": 1, "num_waiting_reqs": 0}])
        self.assertTrue(out.get("skipped"), out)
        self.assertEqual(out.get("why"), "requests are running")
        self.assertEqual(canary["fails"], 0, "no probe was sent, so none failed")

    def test_a_load_read_that_fails_leaves_the_probe_to_judge_the_engine(self):
        def down(timeout=3.0):
            raise OSError("connection refused")
        out, canary = self.canary_with(down)
        self.assertFalse(out.get("skipped"), "an engine that will not answer is the probe's to judge")
        self.assertEqual(canary["fails"], 1, "the probe went to the closed port and failed there")


class AnOrphanContainerHoldsTheBox(Base):
    """The same stop timeout with the container still up: systemd killed the docker client,
    the daemon kept the container and its pool. The lifecycle said "failed", which no gate
    counts as busy, so Start on the other lane was allowed next to a live ~100 GB engine.
    The orphan banner only covered a "stopped" unit."""
    U = "qwen38-sglang.service"

    def test_it_is_an_orphan_the_gate_counts_and_the_banner_names(self):
        self.box({f"systemctl show {self.U}": "ActiveState=failed\nSubState=failed\nResult=timeout\n"
                                              "ActiveEnterTimestampMonotonic=1000\n",
                  "docker ps -q -f name=^qwen38-sglang$": "c0ffee\n"})
        out = self.cp.collect_lifecycle()
        d = out.get("data", out)
        self.assertEqual(d["engines"][self.U]["state"], "orphan")
        self.assertEqual([o["unit"] for o in d["orphans"]], [self.U])
        blocked = d["blocked"].get("unit:start:qwen38-flash.service") or []
        self.assertTrue(blocked and "outside systemd" in blocked[0], blocked)
        self.assertNotIn(f"unit:start:{self.U}", d["blocked"],
                         "its own unit may start: ExecStartPre removes the orphan first")


class TheEngineIsAskedItsCurrentRoutes(Base):
    """SGLang logs a deprecation warning for every call of /get_load and /get_server_info,
    on the 27B image and the flash image alike, and says both will go. This cockpit asked
    /get_load every second: 597 warnings in 10 minutes of the 27B's journal (2026-09-23).
    It asks /v1/loads?include=core and /server_info, keeps handing the rest of the cockpit
    and the page the /get_load shape, and falls back only on an engine that answers 404."""

    def engine(self, routes):
        import http.server
        import threading
        seen = []
        load = {"loads": [{"dp_rank": 0, "num_running_reqs": 2, "num_waiting_reqs": 1,
                           "num_total_tokens": 500, "num_used_tokens": 400}]}
        legacy = [{"dp_rank": 0, "num_reqs": 7, "num_waiting_reqs": 0, "num_tokens": 9,
                   "num_pending_tokens": 0, "ts_tic": 1.0}]
        answers = {"/v1/loads?include=core": load, "/get_load": legacy,
                   "/server_info": {"max_total_num_tokens": 900000},
                   "/get_server_info": {"max_total_num_tokens": 800000}}

        class Engine(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.path)
                if self.path in routes:
                    out = json.dumps(answers[self.path]).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(out)))
                    self.end_headers(); self.wfile.write(out); return
                self.send_response(404); self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        saved = self.cp.ENGINE_BASE
        self.cp.ENGINE_BASE = f"http://127.0.0.1:{srv.server_address[1]}"
        self.addCleanup(setattr, self.cp, "ENGINE_BASE", saved)
        self.cp.ENGINE_ROUTES.update(self.cp.CURRENT_ROUTES)
        self.addCleanup(self.cp.ENGINE_ROUTES.update, self.cp.CURRENT_ROUTES)
        return seen

    def test_a_current_engine_gives_the_old_shape_from_the_new_route(self):
        seen = self.engine({"/v1/loads?include=core", "/server_info"})
        self.assertEqual(self.cp.engine_load(), [{"dp_rank": 0, "num_reqs": 3, "num_waiting_reqs": 1,
                                                  "num_tokens": 500, "num_used_tokens": 400,
                                                  "num_pending_tokens": 100}])
        self.assertEqual(self.cp.engine_server_info(timeout=3)["max_total_num_tokens"], 900000)
        self.assertEqual(seen, ["/v1/loads?include=core", "/server_info"], "a deprecated route was asked")

    def test_an_engine_without_the_new_routes_still_answers(self):
        seen = self.engine({"/get_load", "/get_server_info"})
        self.assertEqual(self.cp.engine_load()[0]["num_reqs"], 7)
        self.assertEqual(self.cp.engine_server_info(timeout=3)["max_total_num_tokens"], 800000)
        self.cp.engine_load()
        self.assertEqual(seen, ["/v1/loads?include=core", "/get_load", "/server_info", "/get_server_info",
                                "/get_load"], "the fallback is remembered for that engine")

    def test_the_poll_reads_liveness_from_the_new_route(self):
        self.engine({"/v1/loads?include=core", "/server_info"})
        out = self.cp.collect_engine_fast()
        self.assertTrue(out["healthy"], out)
        self.assertEqual(out["load"][0]["num_reqs"], 3)

    def test_a_port_with_no_engine_is_asked_the_new_route_again(self):
        self.engine({"/get_load"})
        self.cp.engine_load()
        self.assertEqual(self.cp.ENGINE_ROUTES["load"], "/get_load")
        self.cp.ENGINE_BASE = "http://127.0.0.1:1"          # the lane switched, nothing listens
        with self.assertRaises(Exception):
            self.cp.engine_load()
        self.assertEqual(self.cp.ENGINE_ROUTES["load"], "/v1/loads?include=core")


class AnEngineThatServedLosesHealthAsDegraded(Base):
    """The cockpit reads an engine's log only while its health is down, so in a boot it
    watched, the last stage it saw was the one before "fired up". When the engine lost health
    later with no milestone left in the last 300 lines (a crash's tracebacks), that stage came
    back: the lane read "capturing graphs", a boot, and the zombie restart, which follows
    "degraded" only, never came (the reference box, 2026-10-01)."""
    U = "qwen38-flash.service"
    # the flash lane's own lines, from its journal of 2026-10-01
    BOOTING = ("[2026-10-01 19:23:17] Load weight begin. avail mem=109.83 GB\n"
               "[2026-10-01 19:30:58] Load weight end. elapsed=461.30 s, type=Qwen4ExpForConditionalGeneration, "
               "quant=modelopt_fp4, quant_algo=NVFP4, avail mem=30.04 GB\n"
               "[2026-10-01 19:32:30] KV Cache is allocated. dtype: torch.bfloat16, #tokens: 482176, K size: 5.52 GB\n"
               "[2026-10-01 19:33:06] Capture target verify CUDA graph begin. backend=full, num_tokens_per_req=4, "
               "bs=[1, 2, 3, 4, 5, 6, 7, 8], avail mem=12.92 GB\n")
    CRASH = ("Traceback (most recent call last):\n"
             '  File "/usr/lib/python3.12/asyncio/locks.py", line 212, in wait\n'
             "    await fut\n"
             "asyncio.exceptions.CancelledError\n") * 70

    def setUp(self):
        for d in (self.cp.BOOT_SEEN, self.cp.BOOT_HEAD_READ, self.cp.ZOMBIE_SINCE):
            d.clear()
        real_alive = self.cp.scheduler_alive
        self.addCleanup(setattr, self.cp, "scheduler_alive", real_alive)
        self.cp.scheduler_alive = lambda container: True     # a scheduler: no zombie here

    def tick(self, healthy, tail, prev):
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": healthy}}
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {self.U: prev}
        self.cp.UNHEALTHY_TICKS[self.U] = 0 if healthy else 5
        self.cp.LAST_PROGRESS["ts"] = None
        self.box({f"systemctl show {self.U}": "ActiveState=active\nSubState=running\n"
                                              "ActiveEnterTimestampMonotonic=1000\nInvocationID=abc\n",
                  "docker ps -q -f name=^qwen38-flash$": "c0ffee\n",
                  "docker logs --tail 300 qwen38-flash": tail,
                  "docker logs --since": tail})
        out = self.cp.collect_lifecycle()
        return out.get("data", out)["engines"][self.U]["state"]

    def test_a_boot_it_watched_then_served_reads_degraded_once_health_goes(self):
        self.assertEqual(self.tick(False, self.BOOTING, "loading-weights"), "capturing-graphs")
        self.assertEqual(self.tick(True, "", "capturing-graphs"), "ready")
        self.assertEqual(self.tick(False, self.CRASH, "ready"), "degraded")

    def test_a_boot_that_never_served_still_reads_its_stage(self):
        self.assertEqual(self.tick(False, self.BOOTING, "loading-weights"), "capturing-graphs")
        self.assertEqual(self.tick(False, self.CRASH, "capturing-graphs"), "capturing-graphs")


class TheSchedulerProbeAsksDockerTheWayItAnswers(Base):
    """scheduler_alive() against a docker that answers as the daemon does. `docker top`
    refuses a field list without the PID ("Couldn't find PID field in ps output"): asked for
    `comm` alone, the probe never answered on the reference box, so no zombie was ever
    decided there. The tests above stand a lambda in for the probe and could not see it
    (found by the end-to-end test of a real zombie, 2026-10-01)."""

    DOCKER = """#!/bin/sh
[ "$1" = top ] || exit 2
c="$2"; shift 2
[ "$1" = -eo ] || { echo "unexpected: $*" >&2; exit 2; }
case ",$2," in
  *,pid,*) ;;
  *) echo "Error response from daemon: Couldn't find PID field in ps output" >&2; exit 1 ;;
esac
case "$c" in
  serving) printf 'PID                 COMMAND\n2218964             python3\n2220142             sglang::schedul\n' ;;
  zombie) printf 'PID                 COMMAND\n2218964             python3\n' ;;
  *) echo "Error response from daemon: No such container: $c" >&2; exit 1 ;;
esac
"""

    def setUp(self):
        bin_dir = Path(tempfile.mkdtemp(prefix="fake-docker-"))
        self.addCleanup(shutil.rmtree, bin_dir, True)
        (bin_dir / "docker").write_text(self.DOCKER)
        (bin_dir / "docker").chmod(0o755)
        path = os.environ["PATH"]
        self.addCleanup(os.environ.__setitem__, "PATH", path)
        os.environ["PATH"] = f"{bin_dir}:{path}"

    def test_a_scheduler_listed_is_alive_and_one_gone_is_not(self):
        self.assertIs(self.cp.scheduler_alive("serving"), True)
        self.assertIs(self.cp.scheduler_alive("zombie"), False)

    def test_a_container_it_cannot_read_decides_nothing(self):
        self.assertIsNone(self.cp.scheduler_alive("gone"))


class TheLastFiveMinutesAreTheEnginesOwnCounters(Base):
    """The Traffic view's last 5 minutes read SGLang's /metrics, where each request is counted
    once and a speed holds no idle time (engine_metrics.py). The collector follows the text
    engine that serves, starts again with a new one, never turns a missing read into an
    error, and takes the cockpit's own canary out: SGLang counts it like any request."""

    FLASH = "qwen38-flash.service"

    def setUp(self):
        import http.server
        import threading
        self.count = {"requests": 0}
        count = self.count

        def page():
            n = count["requests"]
            return (f'sglang:num_requests_total{{model_name="m",is_streaming="false"}} {n}\n'
                    f'sglang:prompt_tokens_total{{model_name="m",is_streaming="false"}} {14 * n}\n'
                    f'sglang:generation_tokens_total{{model_name="m",is_streaming="false"}} {2 * n}\n'
                    f'sglang:time_to_first_token_seconds_count{{model_name="m"}} {n}\n'
                    f'sglang:time_to_first_token_seconds_sum{{model_name="m"}} {0.05 * n}\n'
                    f'sglang:inter_token_latency_seconds_count{{model_name="m"}} {n}\n'
                    f'sglang:inter_token_latency_seconds_sum{{model_name="m"}} {0.02 * n}\n'
                    f'sglang:realtime_tokens_total{{model_name="m",mode="prefill_compute"}} {14 * n}\n'
                    f'sglang:realtime_tokens_total{{model_name="m",mode="prefill_cache"}} 999999\n'
                    f'sglang:per_stage_req_latency_seconds_sum{{model_name="m",stage="prefill_forward"}} {0.007 * n}\n'
                    f'sglang:queue_time_seconds_sum{{model_name="m"}} {0.05 * n}\n'
                    f'sglang:queue_time_seconds_count{{model_name="m"}} {n}\n'
                    f'sglang:scheduler_idle_seconds_total{{model_name="m"}} {2 * n}\n'
                    f'sglang:num_aborted_requests_total{{model_name="m"}} 0\n'
                    f'sglang:spec_verify_calls_total{{model_name="unified"}} {n}\n'
                    f'sglang:num_running_reqs{{model_name="m"}} 2.0\n'
                    f'sglang:full_token_usage{{model_name="m"}} 0.5\n')

        class Engine(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, out):
                self.send_response(code)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self):
                self.reply(200, page().encode()) if self.path == "/metrics" else self.reply(404, b"")

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                count["requests"] += 1          # SGLang counts the canary like any request
                self.reply(200, b'{"choices":[{"message":{"content":"OK"}}],'
                                b'"usage":{"prompt_tokens":14,"completion_tokens":2}}')

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.base = f"http://127.0.0.1:{srv.server_address[1]}"
        saved = (self.cp.ENGINE_BASE, self.cp.DRY_RUN, self.cp.METRICS, dict(self.cp.CANARY),
                 dict(self.cp.LAST_PROGRESS))
        with self.cp.LIFE_LOCK:
            saved_life = {k: dict(v) for k, v in self.cp.LIFE.items()}
            self.cp.LIFE["states"] = {self.FLASH: "ready"}
            self.cp.LIFE["enter"] = {self.FLASH: "1000"}
        with self.cp.STATE_LOCK:
            saved_fast = self.cp.STATE.get("engine_fast")
            self.cp.STATE["engine_fast"] = {"data": {"load": [{"num_reqs": 0, "num_waiting_reqs": 0}]}}
        self.cp.ENGINE_BASE, self.cp.DRY_RUN = self.base, False
        self.cp.METRICS = self.cp.em.Window(300.0)
        self.cp.LAST_PROGRESS["ts"] = None

        def restore():
            self.cp.ENGINE_BASE, self.cp.DRY_RUN, self.cp.METRICS = saved[0], saved[1], saved[2]
            self.cp.CANARY.clear(); self.cp.CANARY.update(saved[3])
            self.cp.LAST_PROGRESS.clear(); self.cp.LAST_PROGRESS.update(saved[4])
            with self.cp.LIFE_LOCK:
                self.cp.LIFE.clear(); self.cp.LIFE.update(saved_life)
            with self.cp.STATE_LOCK:
                self.cp.STATE["engine_fast"] = saved_fast
        self.addCleanup(restore)

    def client(self):
        import urllib.request
        urllib.request.urlopen(urllib.request.Request(self.base + "/v1/chat/completions", b"{}"), timeout=5).read()

    def test_no_text_engine_no_window(self):
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {self.FLASH: "stopped"}
        self.assertEqual(self.cp.collect_engine_metrics(), {"node_id": "local", "lane": None})

    def test_a_serving_engine_is_read(self):
        out = self.cp.collect_engine_metrics()
        self.assertEqual((out["lane"], out["read"], out["requests"], out["watched_s"]), (self.FLASH, True, 0, 0.0))

    def test_the_canary_is_taken_out_and_a_client_is_not(self):
        self.cp.collect_engine_metrics()
        canary = self.cp.collect_canary()
        self.assertFalse(canary["skipped"], canary)
        self.assertEqual(self.count["requests"], 1, "the canary reached the engine")
        out = self.cp.collect_engine_metrics()
        self.assertEqual((out["requests"], out["prompt_tokens"], out["canaries_out"], out["approximate"]),
                         (0, 0, 1, False), out)
        self.assertIsNone(out["ttft_s"])
        self.assertEqual((out["prefill_tps"], out["queue_s"], out["acc_len"]), (None, None, None),
                         "the canary's prefill, queue and verification are its own too")
        self.client()
        out = self.cp.collect_engine_metrics()
        self.assertEqual((out["requests"], out["prompt_tokens"], out["generated_tokens"]), (1, 14, 2), out)
        self.assertEqual((out["prefill_tps"], out["queue_s"], out["acc_len"]), (2000.0, 0.05, 2.0), out)
        self.assertEqual((out["running_max"], out["pool_max"], out["aborted"]), (2, 0.5, 0), out)

    def test_a_canary_whose_engine_did_not_answer_makes_it_approximate(self):
        self.cp.collect_engine_metrics()
        self.cp.ENGINE_BASE = "http://127.0.0.1:9"          # closed: the canary fails at once
        self.cp.collect_canary()
        self.cp.ENGINE_BASE = self.base
        self.assertTrue(self.cp.collect_engine_metrics()["approximate"])

    def test_a_missing_read_is_a_gap_not_an_error(self):
        self.cp.collect_engine_metrics()
        self.client()
        self.cp.collect_engine_metrics()
        self.cp.ENGINE_BASE = "http://127.0.0.1:9"
        out = self.cp.collect_engine_metrics()
        self.assertNotIn("error", out)
        self.assertEqual((out["read"], out["requests"]), (False, 1), "the last numbers, said as such")

    def test_a_new_engine_starts_the_window_again(self):
        self.cp.collect_engine_metrics()
        self.client()
        self.assertEqual(self.cp.collect_engine_metrics()["requests"], 1)
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["enter"] = {self.FLASH: "2000"}     # the same unit, started again
        out = self.cp.collect_engine_metrics()
        self.assertEqual((out["requests"], out["watched_s"]), (0, 0.0))

    def test_a_dry_run_cockpit_reads_but_never_generates(self):
        self.cp.DRY_RUN = True
        self.cp.collect_engine_metrics()
        self.assertTrue(self.cp.collect_canary()["skipped"])
        self.assertEqual(self.count["requests"], 0)


class TheVllmLanesCountersAreReadToo(Base):
    """The Traffic view reads the vLLM lanes' /metrics as well (engine_metrics.py names both
    engines' counters). With a vLLM lane serving, the collector asked for no page at all: its
    key was taken among the SGLang lanes alone, and the view said nothing of a lane that served
    (found in review, 2026-10-20). vLLM counts a request's tokens per iteration and its
    inter-token samples per step, and no canary runs on its lanes."""

    UNIT = "qwen38-flash-local.service"

    def setUp(self):
        import http.server
        import threading
        self.count = {"requests": 0, "reads": 0}
        count = self.count

        def page():
            n = count["requests"]
            # a request: 14 prompt tokens, 4 of them from the cache, and 9 generated, the
            # first at 0.05 s and the other 8 over two steps of 0.08 s: (9 - 1) / 0.16 = 50 tok/s
            return (f'vllm:request_success_total{{engine="0",finished_reason="stop",model_name="m"}} {n}\n'
                    f'vllm:prompt_tokens_total{{engine="0",model_name="m"}} {14 * n}\n'
                    f'vllm:prompt_tokens_cached_total{{engine="0",model_name="m"}} {4 * n}\n'
                    f'vllm:generation_tokens_total{{engine="0",model_name="m"}} {9 * n}\n'
                    f'vllm:time_to_first_token_seconds_count{{engine="0",model_name="m"}} {n}\n'
                    f'vllm:time_to_first_token_seconds_sum{{engine="0",model_name="m"}} {0.05 * n}\n'
                    f'vllm:inter_token_latency_seconds_count{{engine="0",model_name="m"}} {2 * n}\n'
                    f'vllm:inter_token_latency_seconds_sum{{engine="0",model_name="m"}} {0.16 * n}\n'
                    f'vllm:num_requests_running{{engine="0",model_name="m"}} 1.0\n'
                    f'vllm:num_requests_waiting{{engine="0",model_name="m"}} 0.0\n'
                    f'vllm:kv_cache_usage_perc{{engine="0",model_name="m"}} 0.25\n')

        class Engine(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                out = page().encode() if self.path == "/metrics" else b""
                count["reads"] += self.path == "/metrics"
                self.send_response(200 if out else 404)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        saved = (self.cp.ENGINE_BASE, self.cp.METRICS)
        with self.cp.LIFE_LOCK:
            saved_life = {k: dict(v) for k, v in self.cp.LIFE.items()}
            self.cp.LIFE["states"] = {self.UNIT: "ready"}
            self.cp.LIFE["enter"] = {self.UNIT: "1000"}
        self.cp.ENGINE_BASE = f"http://127.0.0.1:{srv.server_address[1]}"
        self.cp.METRICS = self.cp.em.Window(300.0)

        def restore():
            self.cp.ENGINE_BASE, self.cp.METRICS = saved
            with self.cp.LIFE_LOCK:
                self.cp.LIFE.clear(); self.cp.LIFE.update(saved_life)
        self.addCleanup(restore)

    def test_each_vllm_lane_that_serves_is_read(self):
        for unit in ("qwen38-flash-local.service", "qwen38-kolibri.service", "qwen38-nemotron.service",
                     "qwen38-nemotron-super.service"):
            with self.subTest(unit=unit):
                with self.cp.LIFE_LOCK:
                    self.cp.LIFE["states"] = {unit: "ready"}
                reads = self.count["reads"]
                out = self.cp.collect_engine_metrics()
                self.assertEqual((out["lane"], out.get("read")), (unit, True), out)
                self.assertEqual(self.count["reads"], reads + 1, "the collector asked the engine for its page")

    def test_a_request_is_counted_with_vllms_decode_speed(self):
        self.cp.collect_engine_metrics()
        self.count["requests"] += 1
        out = self.cp.collect_engine_metrics()
        self.assertEqual((out["lane"], out.get("requests"), out.get("prompt_tokens"), out.get("cached_tokens"),
                          out.get("generated_tokens")), (self.UNIT, 1, 14, 4, 9), out)
        self.assertAlmostEqual(out["decode_tps"], 50.0)
        self.assertEqual((out["running_max"], out["pool_max"], out["canaries_out"]), (1, 0.25, 0), out)
        self.assertIsNone(out["aborted"], "vLLM counts no client that left: not counted, which is not none")


class WhereAClientSendsItsRequests(Base):
    """Hasan (2026-10-18): the address to give a client, plain and in sight. collect_api reads the keepalive unit as
    systemd runs it, whose environment holds keys: four fields leave it, nothing else."""

    SHOW = ("ExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /home/u/.config/qwen38/keepalive-proxy.py 30001 ; "
            "ignore_errors=no ; start_time=[n/a] }\n"
            "Environment=UPSTREAM=http://127.0.0.1:30000 PROXY_BIND=0.0.0.0 QWEN38_UPSTREAM_API_KEY=sekret-123 "
            "QWEN38_TLS_CERT=/etc/ssl/box.pem QWEN38_CLIENT_KEYS_FILE=/home/u/.config/qwen38/client-keys\n")

    IP = ("2: enP7s7    inet 192.168.1.20/24 brd 192.168.1.255 scope global dynamic enP7s7\\       valid_lft forever\n"
          "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever\n"
          "4: br-b5ac2d8eaf0e    inet 172.19.0.1/16 scope global br-b5ac2d8eaf0e\\       valid_lft forever\n"
          "5: tailscale0    inet 100.101.102.103/32 scope global tailscale0\\       valid_lft forever\n")

    def test_the_boxs_addresses_the_tailnets_first_and_no_container_bridge(self):
        # the page opened on the box (127.0.0.1) has no other address to offer another machine (captures, 2026-11-09)
        self.box({"show qwen38-keepalive.service": self.SHOW, "addr show scope global": self.IP})
        self.assertEqual(self.cp.collect_api()["addresses"], [{"iface": "tailscale0", "addr": "100.101.102.103"},
                                                              {"iface": "enP7s7", "addr": "192.168.1.20"}])

    def test_its_port_its_interface_its_tls_and_its_client_keys(self):
        self.box({"show qwen38-keepalive.service": self.SHOW})
        d = self.cp.collect_api()
        self.assertEqual({k: d[k] for k in ("known", "port", "bind", "tls", "client_keys")},
                         {"known": True, "port": 30001, "bind": "0.0.0.0", "tls": True, "client_keys": True})
        self.assertTrue(d["key_file"].endswith("api-key"))
        self.assertNotIn("sekret", json.dumps(d))            # the unit's keys never reach the page

    def test_the_environment_files_for_three_names_and_one_not_readable(self):
        # the docs put the proxy's secrets in an EnvironmentFile, which systemctl show does not unfold (review of v1.45.1)
        f = self.tmp / "proxy.env"
        f.write_text("QWEN38_UPSTREAM_API_KEY=sekret-456\nexport QWEN38_TLS_CERT='/etc/ssl/box.pem'\n"
                     "QWEN38_CLIENT_KEYS_FILE=/home/u/keys # client keys\n")
        show = ("ExecStart={ argv[]=/usr/bin/python3 /x/keepalive-proxy.py 30001 ; }\nEnvironment=PROXY_BIND=0.0.0.0\n"
                f"EnvironmentFiles={f} (ignore_errors=no)\n")
        self.box({"show qwen38-keepalive.service": show})
        d = self.cp.collect_api()
        self.assertEqual((d["tls"], d["client_keys"], d["env_unread"]), (True, True, False))
        self.assertNotIn("sekret", json.dumps(d))
        self.box({"show qwen38-keepalive.service": show.replace(str(f), "/nonexistent/proxy.env")})
        self.assertTrue(self.cp.collect_api()["env_unread"])
        self.box({"show qwen38-keepalive.service": show.replace(str(f), "/nonexistent/proxy.env").replace("=no)", "=yes)")})
        self.assertFalse(self.cp.collect_api()["env_unread"], "an optional file that is not there sets nothing")

    def test_a_proxy_on_its_defaults_and_a_unit_not_read(self):
        self.box({"show qwen38-keepalive.service": "ExecStart={ argv[]=/usr/bin/python3 /x/keepalive-proxy.py 30011 ; }\nEnvironment=\n"})
        d = self.cp.collect_api()
        self.assertEqual((d["port"], d["bind"], d["tls"], d["client_keys"]), (30011, "0.0.0.0", False, False))
        self.box({})
        d = self.cp.collect_api()
        self.assertEqual((d["known"], d["port"], d["bind"]), (False, 30001, "0.0.0.0"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
