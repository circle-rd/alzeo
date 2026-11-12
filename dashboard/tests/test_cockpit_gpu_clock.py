#!/usr/bin/env python3
"""The GPU clock cap of the Machine view (decision of 2026-11-11), from the action to the box.

The cockpit caps the GPU's clock at 2,400, 2,200 or 2,000 MHz, or lifts the cap, through sudo -n and
nvidia-smi under four exact sudoers lines, keeps the choice in its config folder and sets it again
at its start, since a reboot loses the cap. What is held here:
  * the action takes those four values and nothing else, behind the session and the CSRF token of
    every other action, and a GET changes nothing;
  * each value runs its own exact argv, the one the rendered sudoers lines allow (visudo passes them,
    and refuses the comma unescaped);
  * the choice is kept whole (one rename), none forgets it, a refusal keeps what was kept, and a file
    that names no cap the cockpit offers sets nothing;
  * the kept cap is set again at the cockpit's start, by a job of its own, before anything else runs;
  * no nvidia-smi, a refused sudo, nvidia-smi's own refusal and a call that never answers are each
    said, nothing raises, and the state never says a cap that is not set (a call that never answers
    may still apply, so the cap is said not known);
  * the event feed gives the exit code of the call the job ran, none when it did not end.
sudo and nvidia-smi are stubs on a PATH of the test's own: the real ones are never run. The cockpit is
loaded in dry run, and a test that needs its job to run for real says so and puts the dry run back.
"""
import http.client
import importlib.util
import json
import os
import shutil
import subprocess
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


API_KEY = "test-key-not-a-real-one"
CLOSED = "http://127.0.0.1:1"        # nothing listens there: a request fails at once
CAPS = ["none", "2400", "2200", "2000"]
WANT = {"none": ["-n", "/usr/bin/nvidia-smi", "-rgc"],
        "2400": ["-n", "/usr/bin/nvidia-smi", "-lgc", "0,2400"],
        "2200": ["-n", "/usr/bin/nvidia-smi", "-lgc", "0,2200"],
        "2000": ["-n", "/usr/bin/nvidia-smi", "-lgc", "0,2000"]}

# sudo as the test wants it: each call's arguments kept whole (one unit separator after each), then
# what the mode file says. The texts are nvidia-smi's on the reference box (2026-11-11, driver
# 580.178.04) for a set, with the warning it adds after it, and a reset; the refusal of an unsupported
# GPU is a stand-in for nvidia-smi's own words, passed through, with that warning after it.
WARNING = ("Warning: persistence mode is disabled on device 0000000F:01:00.0. See the Known Issues section of the "
           "nvidia-smi(1) man page for more information. Run with [--help | -h] switch to get more information.")
SUDO = r"""#!/bin/sh
here="$(dirname "$0")"
for a in "$@"; do printf '%s\037' "$a"; done >> "$here/calls"; printf '\n' >> "$here/calls"
case "$(cat "$here/mode")" in
  ok) case "$*" in *-rgc*) echo "All done." ;;
                   *) echo "GPU clocks set to \"(gpuClkMin 0, gpuClkMax ${4#0,})\" for GPU 0000000F:01:00.0"; echo "WARNING" ;; esac ;;
  refuse) echo "sudo: a password is required" >&2; exit 1 ;;
  refuse-fr) echo "sudo: un mot de passe est nécessaire" >&2; exit 1 ;;
  fail) echo "Setting locked GPU clocks is not supported for GPU 0000000F:01:00.0."; echo "WARNING"; exit 3 ;;
  silent) exit 1 ;;
  hang) exec sleep 30 ;;
esac
""".replace("WARNING", WARNING)
# nvidia-smi by its name, as collect_gpu runs it: each field the query names answers, in the order named,
# the value the test wrote for it, and [N/A] for one it wrote none for (so a field left out of the query
# is a value the cockpit does not get)
NVSMI = r"""#!/bin/sh
here="$(dirname "$0")"; q=""
for a in "$@"; do case "$a" in --query-gpu=*) q="${a#--query-gpu=}" ;; esac; done
[ -n "$q" ] || exit 0
out=""
for f in $(printf '%s' "$q" | tr ',' ' '); do
  v="$(grep -m1 "^$f=" "$here/fields" | cut -d= -f2-)"
  out="${out:+$out, }${v:-[N/A]}"
done
echo "$out"
"""


def load_cockpit(config_dir: Path):
    """Import cockpit.py with its environment pointed at a throwaway box."""
    os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(config_dir), COCKPIT_REPO_DIR=str(REPO),
                      COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0",
                      COCKPIT_ENGINE=CLOSED, COCKPIT_PROXY=CLOSED, COCKPIT_IMAGE=CLOSED, COCKPIT_VIDEO=CLOSED,
                      HOME=str(config_dir))
    sys.path.insert(0, str(DASH))
    spec = importlib.util.spec_from_file_location("cockpit_gpu_clock_under_test", DASH / "cockpit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-gpu-clock-"))
        (cls.tmp / "api-key").write_text(API_KEY + "\n")
        cls.cp = load_cockpit(cls.tmp)
        cls.nvidia_smi_as_loaded = cls.cp.NVIDIA_SMI

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.bin = Path(tempfile.mkdtemp(prefix="cockpit-gpu-bin-"))
        self.addCleanup(shutil.rmtree, self.bin, True)
        for name, body in (("sudo", SUDO), ("nvidia-smi", NVSMI)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.mode("ok")
        self.reads(clock="2402")
        path = os.environ["PATH"]
        self.addCleanup(os.environ.__setitem__, "PATH", path)
        os.environ["PATH"] = f"{self.bin}:{path}"
        # the stub is the nvidia-smi this machine has; the argv still names the real path, which sudo is handed
        for name in ("NVIDIA_SMI", "GPU_CLOCK_FILE", "GPU_CLOCK_TIMEOUT_S", "DRY_RUN"):
            self.addCleanup(setattr, self.cp, name, getattr(self.cp, name))
        self.cp.NVIDIA_SMI = str(self.bin / "nvidia-smi")
        self.cp.GPU_CLOCK_FILE.unlink(missing_ok=True)
        with self.cp.GPU_CLOCK_LOCK:
            self.cp.GPU_CLOCK.update({"set": None, "at": None, "origin": None, "tried": None, "error": None, "said": None})

    def mode(self, m):
        (self.bin / "mode").write_text(m)

    def reads(self, clock):
        """What the GPU reads: 10.58 W, 46 °C, and this graphics clock."""
        (self.bin / "fields").write_text(f"power.draw=10.58\ntemperature.gpu=46\nclocks.current.graphics={clock}\n")

    def calls(self):
        log = self.bin / "calls"
        return [ln.split("\x1f")[:-1] for ln in log.read_text().splitlines()] if log.exists() else []

    def job(self, cap, origin="ui"):
        """The job's function, run here and now, as run_job runs it outside a dry run."""
        j = self.cp.Job("gpu_clock", None, 60, params={"cap": cap}, fn=self.cp.job_gpu_clock, origin=origin)
        ok, lines, result = self.cp.job_gpu_clock(j)
        return j, ok, lines, result

    def kept(self):
        f = self.cp.GPU_CLOCK_FILE
        return json.loads(f.read_text()) if f.exists() else None

    def wait_for_the_job_lock(self):
        for _ in range(400):
            if not self.cp.JOB_LOCK.locked():
                return
            time.sleep(0.02)
        self.fail("the job never released the job lock")


class TheCapsAreAClosedSet(Base):
    BAD = ["2400 -i 0", "0,2400", " 2400", "2400 ", "2 400", "2400\n", "2,400", "99999", 99999, 2400, 2400.0,
           "", None, "None", "NONE", "-rgc", "-lgc", "2400;id", "$(id)", "2500", "3003", "0", ["2400"],
           {"cap": "2400"}, True, False]

    def tearDown(self):
        self.wait_for_the_job_lock()
        self.cp.JOB_CURRENT["id"] = None

    def test_the_action_takes_the_four_caps_and_nothing_else(self):
        self.assertEqual(self.cp.ACTIONS["gpu_clock"]["params"], {"cap": CAPS})
        self.assertEqual(sorted(self.cp.GPU_CLOCK_ARGV), sorted(CAPS))
        for bad in self.BAD:
            with self.subTest(cap=bad):
                code, out = self.cp.start_action("gpu_clock", {"cap": bad})
                self.assertEqual((code, out.get("error")), (400, "invalid cap"), out)
        for params in ({}, {"value": "2400"}, ["2400"], "2400", None):
            with self.subTest(params=params):
                code, _ = self.cp.start_action("gpu_clock", params)
                self.assertEqual(code, 400)
        self.assertEqual(self.calls(), [], "a refused value reached sudo")

    def test_a_cap_of_the_set_is_started_as_a_job(self):
        code, out = self.cp.start_action("gpu_clock", {"cap": "2200"})
        self.assertEqual(code, 202, out)
        self.assertTrue(out.get("dry_run"), "these tests would run sudo")


class TheRouteIsGuardedAsEveryOtherAction(Base):
    """The cap rides /api/action: no session is a 401, a POST without the CSRF token of the session is
    a 403, a value outside the set a 400, and a GET of the route changes nothing."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = cls.cp.Server(("127.0.0.1", 0), cls.cp.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        super().tearDownClass()

    def tearDown(self):
        self.wait_for_the_job_lock()
        self.cp.JOB_CURRENT["id"] = None
        self.cp.LOGIN_FAILS.clear()

    def req(self, method, path, body=None, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = cookie
        try:
            c.request(method, path, json.dumps(body).encode() if body is not None else None, h)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def test_session_token_and_value_are_each_checked(self):
        body = {"name": "gpu_clock", "params": {"cap": "2400"}}
        st, _, _ = self.req("POST", "/api/action", body)
        self.assertEqual(st, 401)
        st, hdrs, _ = self.req("POST", "/api/login", {"key": API_KEY})
        self.assertEqual(st, 200)
        cookie = hdrs["Set-Cookie"].split(";")[0]
        st, _, _ = self.req("POST", "/api/action", body, cookie)
        self.assertEqual(st, 403, "no CSRF token")
        st, _, data = self.req("POST", "/api/csrf", {}, cookie)
        token = json.loads(data)["token"]
        st, _, _ = self.req("GET", "/api/action?name=gpu_clock&cap=2400", None, cookie)
        self.assertEqual(st, 404, "a GET reaches no action")
        st, _, data = self.req("POST", "/api/action", {**body, "params": {"cap": "0,2400"}, "csrf": token}, cookie)
        self.assertEqual((st, json.loads(data)["error"]), (400, "invalid cap"))
        st, _, data = self.req("POST", "/api/action", {**body, "csrf": token}, cookie)
        self.assertEqual(st, 202, data)
        self.assertEqual(self.calls(), [], "a dry run ran sudo")


class EachCapRunsItsExactArgv(Base):
    def test_each_cap_hands_sudo_its_own_arguments_whole(self):
        for cap in CAPS:
            with self.subTest(cap=cap):
                before = len(self.calls())
                j, ok, lines, _ = self.job(cap)
                self.assertTrue(ok, lines)
                self.assertEqual(self.calls()[before:], [WANT[cap]], "one call, its arguments as the sudoers line has them")
                self.assertEqual(j.argv, ["sudo"] + WANT[cap], "the job's log names the command it ran")

    def test_the_argv_names_nvidia_smi_where_the_sudoers_lines_do(self):
        self.assertEqual(self.nvidia_smi_as_loaded, "/usr/bin/nvidia-smi")
        for cap, argv in self.cp.GPU_CLOCK_ARGV.items():
            self.assertEqual(argv[:3], ["sudo", "-n", "/usr/bin/nvidia-smi"], cap)

    def test_the_audit_log_has_the_exact_argv(self):
        self.job("2000")
        lines = [json.loads(ln) for ln in (self.tmp / "cockpit-audit.log").read_text().splitlines()]
        last = [e for e in lines if e.get("kind") == "gpu_clock"][-1]
        self.assertEqual((last["argv"], last["ok"], last["cap"]), (["sudo"] + WANT["2000"], True, "2000"))

    def test_nvidia_smi_s_words_and_the_outcome_are_in_the_job_s_log(self):
        _, ok, lines, result = self.job("2400")
        self.assertTrue(ok)
        self.assertEqual(lines[:2], ['GPU clocks set to "(gpuClkMin 0, gpuClkMax 2400)" for GPU 0000000F:01:00.0', WARNING])
        self.assertEqual(lines[-1], "GPU clock capped at 2,400 MHz")
        self.assertEqual(result, {"ok": True, "cap": "2400", "error": None, "said": "GPU clock capped at 2,400 MHz",
                                  "rc": 0})


class TheChoiceIsKept(Base):
    def test_a_cap_is_kept_whole_and_none_forgets_it(self):
        self.job("2400")
        self.assertEqual(self.kept(), {"cap": "2400"})
        self.assertEqual(self.cp.gpu_clock_kept(), ("2400", None))
        self.assertEqual(sorted(p.name for p in self.tmp.glob("cockpit-gpu-clock*")), ["cockpit-gpu-clock.json"],
                         "the temp file of the rename is left")
        self.job("2200")
        self.assertEqual(self.kept(), {"cap": "2200"})
        self.job("none")
        self.assertIsNone(self.kept(), "none keeps a file")
        self.assertEqual(self.cp.gpu_clock_kept(), ("none", None))

    def test_a_refused_change_keeps_what_was_kept(self):
        self.job("2400")
        self.mode("refuse")
        _, ok, _, result = self.job("2000")
        self.assertFalse(ok)
        self.assertEqual(self.kept(), {"cap": "2400"})
        st = self.cp.gpu_clock_state(None)
        self.assertEqual((st["kept"], st["set"], st["tried"], st["error"]), ("2400", "2400", "2000", "sudo"))
        # and a refused lift keeps the cap and its file
        _, ok, _, _ = self.job("none")
        self.assertFalse(ok)
        self.assertEqual(self.kept(), {"cap": "2400"})

    def test_a_file_that_names_no_offered_cap_sets_nothing(self):
        for text in ('{"cap": "2500"}', '{"cap": 2400}', '{"cap": "0,2400"}', '["2400"]', "2400", "", "{",
                     b'{"cap": "\xff2400"}', "[" * 100000 + "]" * 100000):
            with self.subTest(text=text[:20]):
                if isinstance(text, bytes):
                    self.cp.GPU_CLOCK_FILE.write_bytes(text)
                else:
                    self.cp.GPU_CLOCK_FILE.write_text(text)
                cap, bad = self.cp.gpu_clock_kept()
                self.assertEqual(cap, "none")
                self.assertTrue(bad)
                self.assertIsNone(self.cp.gpu_clock_at_start())
                self.assertEqual(self.cp.gpu_clock_state(None)["kept_bad"], bad)
        self.assertEqual(self.calls(), [])

    def test_a_folder_in_place_of_the_file_sets_nothing_and_says_so(self):
        # read as a file, a folder raised out of gpu_clock_at_start, which main() calls outside any try: the
        # cockpit would not start, and systemd would restart it every 3 s (a mutant of the review of v1.47.0)
        self.cp.GPU_CLOCK_FILE.mkdir()
        self.addCleanup(self.cp.GPU_CLOCK_FILE.rmdir)
        cap, bad = self.cp.gpu_clock_kept()
        self.assertEqual(cap, "none")
        self.assertTrue(bad.startswith("it cannot be read ("), bad)
        self.assertIsNone(self.cp.gpu_clock_at_start())
        self.assertEqual(self.cp.gpu_clock_state(None)["kept_bad"], bad)
        self.assertEqual(self.calls(), [])

    def test_a_cap_set_and_not_kept_says_both(self):
        self.cp.GPU_CLOCK_FILE = self.tmp / "no-such-folder" / "cockpit-gpu-clock.json"
        _, ok, _, result = self.job("2400")
        self.assertFalse(ok, "the cap would be lost at the next reboot with nothing to say so")
        self.assertEqual(result["error"], "keep")
        self.assertIn("GPU clock capped at 2,400 MHz, but the cockpit could not keep it for its next start", result["said"])
        st = self.cp.gpu_clock_state(None)
        self.assertEqual((st["set"], st["kept"]), ("2400", "none"), "what holds is what was set")


    def test_a_cap_lifted_and_not_forgotten_says_the_next_start_sets_it_again(self):
        # the file cannot go (a folder with something in it stands in its place: unlink fails, as root too)
        self.cp.GPU_CLOCK_FILE.mkdir()
        (self.cp.GPU_CLOCK_FILE / "x").write_text("x")
        self.addCleanup(shutil.rmtree, self.cp.GPU_CLOCK_FILE, True)
        _, ok, _, result = self.job("none")
        self.assertFalse(ok)
        self.assertEqual(result["error"], "keep")
        self.assertIn("GPU clock cap lifted: the GPU runs at its own clocks, but the cockpit could not forget the cap "
                      "it kept", result["said"])
        self.assertIn("its next start sets that cap again", result["said"])
        self.assertNotIn("nothing sets it again", result["said"])
        self.assertEqual(self.calls(), [WANT["none"]], "the cap was lifted all the same")


class TheKeptCapIsSetAgainAtTheStart(Base):
    def run_at_start(self):
        """gpu_clock_at_start as main() calls it, its job run for real (not in a dry run)."""
        self.cp.DRY_RUN = False
        before = set(self.cp.JOBS)
        code = self.cp.gpu_clock_at_start()
        self.wait_for_the_job_lock()
        self.cp.JOB_CURRENT["id"] = None
        new = [self.cp.JOBS[i] for i in set(self.cp.JOBS) - before]
        # run_job says the job ended just after it frees the lock
        for _ in range(200):
            if all(j.status != "running" for j in new):
                break
            time.sleep(0.01)
        return code, new

    def test_a_kept_cap_is_set_again_by_a_job_of_its_own(self):
        self.cp.GPU_CLOCK_FILE.write_text('{"cap": "2200"}\n')
        code, jobs = self.run_at_start()
        self.assertEqual(code, 202)
        self.assertEqual(self.calls(), [WANT["2200"]])
        self.assertEqual([(j.action, j.params, j.origin, j.status) for j in jobs],
                         [("gpu_clock", {"cap": "2200"}, "boot", "done")])
        st = self.cp.gpu_clock_state(None)
        self.assertEqual((st["kept"], st["set"], st["origin"], st["error"]), ("2200", "2200", "boot", None))

    def test_nothing_kept_nothing_run(self):
        code, jobs = self.run_at_start()
        self.assertIsNone(code)
        self.assertEqual((jobs, self.calls()), ([], []), "the factory clocks need no call")

    def test_a_failure_at_the_start_never_reads_as_a_cap_that_holds(self):
        self.cp.GPU_CLOCK_FILE.write_text('{"cap": "2400"}\n')
        self.mode("refuse")
        code, jobs = self.run_at_start()
        self.assertEqual((code, [j.status for j in jobs]), (202, ["failed"]))
        st = self.cp.gpu_clock_state(None)
        self.assertEqual((st["kept"], st["set"], st["error"]), ("2400", None, "sudo"))

    def test_main_sets_it_again_before_anything_else_can_run(self):
        src = (DASH / "cockpit.py").read_text()
        body = src[src.index("\ndef main():"):src.index('\nif __name__ == "__main__":')]
        self.assertIn("gpu_clock_at_start()", body)
        self.assertLess(body.index("gpu_clock_at_start()"), body.index("start_samplers()"),
                        "the autoheal's job could take the lock first")
        self.assertLess(body.index("gpu_clock_at_start()"), body.index("Server(("))


class EachFailureIsSaid(Base):
    def assert_said(self, error, said, calls):
        st = self.cp.collect_gpu()
        self.assertNotIn("error", st, "the GPU panel went dark")
        self.assertEqual((st["clock"]["error"], st["clock"]["said"]), (error, said))
        self.assertEqual(len(self.calls()), calls)

    def test_no_nvidia_smi(self):
        self.cp.NVIDIA_SMI = str(self.bin / "absent")
        _, ok, _, result = self.job("2400")
        self.assertFalse(ok)
        self.assertEqual(result["error"], "absent")
        self.assertFalse(self.cp.gpu_clock_state(None)["nvidia_smi"])
        self.assert_said("absent", f"this machine has no {self.cp.NVIDIA_SMI}: the cockpit cannot cap its GPU clock", 0)
        self.assertIsNone(self.kept())

    def test_no_nvidia_smi_none_forgets_a_kept_cap_and_runs_nothing(self):
        # a cap kept, then nvidia-smi gone: each start tried it again in vain, and None could not forget it
        # (review of v1.47.0)
        self.cp.GPU_CLOCK_FILE.write_text('{"cap": "2400"}\n')
        self.cp.NVIDIA_SMI = str(self.bin / "absent")
        j, ok, lines, result = self.job("none")
        self.assertTrue(ok, lines)
        self.assertEqual((result["error"], result["said"]),
                         (None, f"this machine has no {self.cp.NVIDIA_SMI}, so nothing was lifted: the cockpit "
                                "forgot the cap it kept"))
        self.assertIsNone(self.kept(), "each start would try the kept cap again")
        self.assertEqual(self.calls(), [], "a call was made")
        self.assertIsNone(j.argv, "the job's log would name a command it did not run")

    def test_a_refused_sudo_names_the_installer_to_run_again(self):
        for mode in ("refuse", "refuse-fr"):            # sudo speaks the box's language
            with self.subTest(mode=mode):
                self.mode(mode)
                _, ok, lines, result = self.job("2400")
                self.assertFalse(ok)
                self.assertEqual(result["error"], "sudo")
                self.assertTrue(lines[0].startswith("sudo: "), lines)
                # the installer that always writes the sudoers file: ./install.sh does not when it leaves the
                # cockpit out (--no-cockpit, --no-service, --no-start), and a cockpit running there said to run it
                self.assertIn(f"(cd {REPO} && ./dashboard/install-dashboard.sh)", result["said"])
                self.assertIsNone(self.kept())
        self.assert_said("sudo", result["said"], 2)

    def test_nvidia_smi_s_own_refusal_is_quoted(self):
        # its reason, not the warning nvidia-smi writes after it, which would have stood for it
        self.mode("fail")
        _, ok, lines, result = self.job("2200")
        self.assertFalse(ok)
        self.assertIn(WARNING, lines)
        said = "nvidia-smi refused: Setting locked GPU clocks is not supported for GPU 0000000F:01:00.0."
        self.assertEqual((result["error"], result["said"]), ("failed", said))
        self.assert_said("failed", said, 1)

    def test_a_call_that_never_answers_is_said_after_its_timeout(self):
        # nvidia-smi runs as root: the cockpit ends its sudo at the deadline, and the nvidia-smi it started may
        # still set or lift the cap. So neither the cap tried nor the one before is said to hold
        self.job("2400")
        self.cp.GPU_CLOCK_TIMEOUT_S = 1
        self.mode("hang")
        t0 = time.time()
        _, ok, _, result = self.job("2000")
        self.assertLess(time.time() - t0, 10)
        self.assertFalse(ok)
        self.assertEqual((result["error"], result["said"]),
                         ("timeout", "nvidia-smi did not answer within 1 s and may still run, so the cap in effect "
                                     "is not known"))
        st = self.cp.gpu_clock_state(None)
        self.assertEqual((st["set"], st["kept"], st["tried"], st["error"]), ("2400", "2400", "2000", "timeout"),
                         "the cap lit stays the one before, and the cap kept for the next start too")

    def test_a_call_that_fails_at_once_and_says_nothing_is_not_called_slow(self):
        self.mode("silent")
        _, ok, _, result = self.job("2000")
        self.assertFalse(ok)
        self.assertEqual((result["error"], result["said"]), ("failed", "sudo or nvidia-smi failed and said nothing"))

    def test_a_call_that_ended_late_is_not_said_to_run_still(self):
        # an exit code is an end: only a call without one (the deadline) may still set the cap. Read by the time
        # alone, a silent failure near the deadline was said "not known, may still run"
        cp = self.cp

        class TheDeadlinePassesInTheCall:
            now = 0.0

            def monotonic(self):
                self.now += cp.GPU_CLOCK_TIMEOUT_S
                return self.now

            def __getattr__(self, name):
                return getattr(time, name)
        self.addCleanup(setattr, cp, "time", cp.time)
        cp.time = TheDeadlinePassesInTheCall()
        self.mode("silent")
        _, ok, _, result = self.job("2000")
        self.assertFalse(ok)
        self.assertEqual((result["error"], result["said"], result["rc"]),
                         ("failed", "sudo or nvidia-smi failed and said nothing", 1))

    def test_nothing_at_the_start_keeps_the_cockpit_from_starting(self):
        self.cp.GPU_CLOCK_FILE.write_text('{"cap": "2400"}\n')
        saved = self.cp.start_action
        self.addCleanup(setattr, self.cp, "start_action", saved)

        def broken(*a, **k):
            raise RuntimeError("a gate that raised")
        self.cp.start_action = broken
        self.assertEqual(self.cp.gpu_clock_at_start(), 500)
        with self.cp.EVENTS_LOCK:
            last = self.cp.EVENTS[-1]
        self.assertEqual(last["kind"], "error")
        self.assertIn("the GPU clock cap kept by the cockpit (2,400 MHz) could not be set again at its start: "
                      "RuntimeError: a gate that raised", last["msg"])


class TheEventFeedGivesTheCallsOwnExitCode(Base):
    """The job names nvidia-smi's command in its log, and the event feed, the dock and the Logs view give its
    exit code. That code was the python job's flag, 1 for any failure: "failed (exit code 1)" of an nvidia-smi
    that ended 3, or never ended (review of v1.47.0). It is the call's own now, and none when it did not end."""

    def run_job(self, cap):
        """The job as start_action runs it, for real (not in a dry run): (the job, the feed's line for it)."""
        self.cp.DRY_RUN = False
        code, out = self.cp.start_action("gpu_clock", {"cap": cap})
        self.assertEqual(code, 202, out)
        job = self.cp.JOBS[out["job"]]
        for _ in range(500):                  # run_job says the job ended once its event is written
            if job.status != "running":
                break
            time.sleep(0.02)
        self.wait_for_the_job_lock()
        self.cp.JOB_CURRENT["id"] = None
        with self.cp.EVENTS_LOCK:
            said = [e["msg"] for e in self.cp.EVENTS if e["kind"] == "job"][-1]
        return job, said

    def test_nvidia_smi_s_refusal_gives_its_own_code(self):
        self.mode("fail")                     # nvidia-smi ends 3
        job, said = self.run_job("2200")
        self.assertEqual((job.status, job.rc), ("failed", 3))
        self.assertEqual(said, "cap the GPU clock at 2,200 MHz: failed (exit code 3)")

    def test_a_call_that_did_not_end_gives_none(self):
        self.cp.GPU_CLOCK_TIMEOUT_S = 1
        self.mode("hang")
        job, said = self.run_job("2000")
        self.assertEqual((job.status, job.rc), ("failed", None))
        self.assertEqual(said, "cap the GPU clock at 2,000 MHz: failed")

    def test_a_cap_set_ends_with_its_code(self):
        job, said = self.run_job("2400")
        self.assertEqual((job.status, job.rc), ("done", 0))
        self.assertEqual(said, "cap the GPU clock at 2,400 MHz: done")


class TheClockIsReadWithThePowerAndTheTemperature(Base):
    def test_one_query_reads_the_three(self):
        st = self.cp.collect_gpu()
        self.assertEqual((st["power_w"], st["temp_c"], st["clock"]["mhz"]), (10.58, 46.0, 2402))

    def test_a_clock_nvidia_smi_cannot_read_is_no_reading(self):
        for clock in ("[N/A]", "[Not Supported]", "", "1e400", "9999999", "-5", "2,402"):
            with self.subTest(clock=clock):
                self.reads(clock)
                st = self.cp.collect_gpu()
                self.assertEqual((st["power_w"], st["temp_c"], st["clock"]["mhz"]), (10.58, 46.0, None))

    def test_the_state_says_what_is_kept_and_what_was_set(self):
        st = self.cp.collect_gpu()["clock"]
        self.assertEqual((st["nvidia_smi"], st["kept"], st["set"], st["error"]), (True, "none", None, None))
        self.job("2200")
        st = self.cp.collect_gpu()["clock"]
        self.assertEqual((st["kept"], st["set"], st["origin"], st["error"]), ("2200", "2200", "ui", None))
        self.assertIsInstance(st["at"], float)

    def test_the_job_publishes_the_state_it_leaves(self):
        self.job("2000")
        with self.cp.STATE_LOCK:
            st = self.cp.STATE["gpu"]["data"]["clock"]
        self.assertEqual((st["kept"], st["set"]), ("2000", "2000"), "the page reads it before the job is seen to end")

    def test_the_page_is_handed_each_cap_s_command(self):
        self.assertEqual(self.cp.STATE["config"]["data"]["gpu_clock_argv"], self.cp.GPU_CLOCK_ARGV)


class TheSudoersLinesAreTheCalls(Base):
    """The template, rendered by install-dashboard.sh's own sed line, allows each cap's argv as the
    cockpit runs it and nothing more, and visudo passes it: a comma in a sudoers argument separates
    commands unless it is escaped, which visudo refuses ("0,2400" read as two commands)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.argv = cls.cp.GPU_CLOCK_ARGV
        text = (DASH / "install-dashboard.sh").read_text().splitlines()
        sed = next(ln for ln in text if "sudoers-cockpit.template" in ln and ln.startswith("sed "))
        cls.rendered = cls.tmp / "sudoers"
        r = subprocess.run(["bash", "-c", f'HERE="{DASH}"; TMP_SUDO="{cls.rendered}"\n{sed}\n'], capture_output=True,
                           text=True, timeout=10, env={"PATH": "/usr/bin:/bin", "HOME": str(cls.tmp / "home")})
        assert r.returncode == 0, r.stderr
        cls.user = subprocess.run(["id", "-un"], capture_output=True, text=True, timeout=10).stdout.strip()

    def visudo(self, path):
        exe = shutil.which("visudo") or "/usr/sbin/visudo"
        self.assertTrue(os.access(exe, os.X_OK), "visudo is part of sudo, which every box and runner of this repo has")
        return subprocess.run([exe, "-c", "-f", str(path)], capture_output=True, text=True, timeout=30).returncode

    def test_each_cap_s_argv_is_a_line_and_no_other_line_names_nvidia_smi(self):
        lines = [ln for ln in self.rendered.read_text().splitlines() if "nvidia-smi" in ln and not ln.startswith("#")]
        head = f"{self.user} ALL=(root) NOPASSWD: "
        for ln in lines:
            self.assertTrue(ln.startswith(head), ln)
        allowed = sorted(ln[len(head):].replace("\\,", ",").split() for ln in lines)
        self.assertEqual(allowed, sorted(argv[2:] for argv in self.argv.values()))
        self.assertEqual(len(lines), 4)

    def test_visudo_passes_the_render_and_refuses_the_comma_unescaped(self):
        self.assertEqual(self.visudo(self.rendered), 0, "the installer would refuse to install the allowlist")
        bare = self.tmp / "sudoers-bare-comma"
        bare.write_text(self.rendered.read_text().replace("0\\,2400", "0,2400"))
        self.assertNotEqual(self.visudo(bare), 0, "this check would pass a comma that splits the line")


if __name__ == "__main__":
    unittest.main(verbosity=2)
