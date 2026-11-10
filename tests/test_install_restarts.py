#!/usr/bin/env python3
"""A run of ./install.sh that changes nothing restarts nothing.

v1.18.5 kept the engine when nothing it reads changed, and everything around it went on
restarting at every run: the proxy (every request in flight through it cut), opencode-web
up to three times (the Agent tab's turn ended) and the cockpit twice. On the reference box
on 2026-09-23: 49 starts of opencode-web and 36 of the cockpit. And on a 1m box step 7
wrote the table's bounds over the pair the end-of-install fit had set, which the fit then
wrote back, a backup each time: 79 in ~/.config/opencode by 2026-09-24 (found in review).

Each service is now restarted when it is not running, when this run changed what it runs,
or when it started before the last change of a file it reads; and those files are written
only when their content changes, so their date means something.
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import re
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = (REPO / "install.sh").read_text()
# install.sh's is lanes.sh's since v1.30, which it sources (the lanes' installers share it)
SCRIPTS = {name: (REPO / name).read_text() for name in
           ("lanes.sh", "dashboard/install-dashboard.sh", "dashboard/install-agent.sh")}


def helper(text):
    if text is SCRIPTS["lanes.sh"]:
        return text + "\n"                  # the library whole, as install.sh sources it
    start = text.index("stale_since(){")
    return text[start:text.index("\n}\n", start) + 3]


def stale(text, active="active", started=None, files=()):
    d = pathlib.Path(tempfile.mkdtemp(prefix="stale-"))
    (d / "systemctl").write_text(
        "#!/bin/sh\ncase \"$*\" in *ActiveState*) echo \"$FAKE_ACTIVE\";; "
        "*ExecMainStartTimestamp*) [ -n \"$FAKE_STARTED\" ] && echo \"@$FAKE_STARTED\";; esac\n")
    (d / "systemctl").chmod(0o755)
    paths = []
    for i, age in enumerate(files):
        f = d / f"f{i}"
        f.write_text("x")
        os.utime(f, (time.time() - age, time.time() - age))
        paths.append(str(f))
    script = helper(text) + "if stale_since unit.service " + " ".join(paths) + "; then echo RESTART; else echo KEEP; fi\n"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{d}:/usr/bin:/bin", "FAKE_ACTIVE": active,
                            "FAKE_STARTED": "" if started is None else str(int(started))})
    return r.stdout.strip()


class StaleSince(unittest.TestCase):
    def test_every_copy_says_the_same(self):
        now = time.time()
        for name, text in SCRIPTS.items():
            with self.subTest(name):
                self.assertEqual(stale(text, active="inactive"), "RESTART")
                self.assertEqual(stale(text, started=now - 100, files=(500, 300)), "KEEP")
                self.assertEqual(stale(text, started=now - 100, files=(500, 10)), "RESTART")
                self.assertEqual(stale(text, started=None, files=(500,)), "RESTART")

    def test_install_sh_has_the_librarys_before_it_asks(self):
        self.assertNotIn("stale_since(){", INSTALL, "one copy: lanes.sh's")
        self.assertLess(INSTALL.index('. "$REPO_DIR/lanes.sh"'), INSTALL.index('stale_since "$KEEPALIVE_UNIT"'))


class EachRestartHasAReason(unittest.TestCase):
    def test_opencode_web_after_step_7_only_when_a_config_changed(self):
        i = INSTALL.index('sudo systemctl restart opencode-web.service \\\n      && echo "opencode-web.service restarted')
        guard = INSTALL[INSTALL.rfind("\n  if ", 0, i):i]
        self.assertIn('"$(oc_configs_sum)" != "$OC_SUM_BEFORE"', guard)
        self.assertLess(INSTALL.index('OC_SUM_BEFORE="$(oc_configs_sum)"'), INSTALL.index("python3 - <<'PYEOF' || die \"could not write the opencode provider config\""))

    def test_the_proxy_only_for_a_new_engine_new_code_or_a_new_unit(self):
        for m in re.finditer(r'sudo systemctl restart "\$KEEPALIVE_UNIT"', INSTALL):
            guard = INSTALL[INSTALL.rfind("if ", 0, m.start()):m.start()]
            self.assertIn("stale_since \"$KEEPALIVE_UNIT\"", guard, INSTALL[m.start() - 200:m.start()])
            self.assertIn('"$KA_CHANGED" -eq 1', guard)
        for write in ('install -m 755 "$REPO_DIR/keepalive-proxy.py" "$CONFIG_DIR/keepalive-proxy.py"; KA_CHANGED=1',
                      'sudo install -m 644 "$TMP_KA" "/etc/systemd/system/$KEEPALIVE_UNIT"; KA_CHANGED=1'):
            self.assertIn(write, INSTALL)

    def test_the_cockpit_and_opencode_web_installers(self):
        for name, marker in (("dashboard/install-dashboard.sh", 'DASH_CHANGED'),
                             ("dashboard/install-agent.sh", 'AGENT_CHANGED')):
            text = SCRIPTS[name]
            i = text.index('sudo systemctl try-restart "$UNIT"')
            guard = text[text.rfind("\nif ", 0, i):i]
            self.assertIn(f'"${marker}" -eq 1', guard, name)
            self.assertIn("stale_since", guard, name)


class ABoxBootingAnotherLaneGetsTheAgentTab(unittest.TestCase):
    """install-agent.sh restarts opencode-web on a newer opencode and points it at the one on
    the PATH, and it ran at step 10 only: a box whose boot lane is not a text one takes another
    branch, which reinstalled the cockpit alone, and it kept the opencode it started with until
    its next boot (the reference box on the FP4-Local lane, 2026-10-22, where a new pin's reason
    was the Bun under opencode). That branch runs it now, after the cockpit, as step 10 does."""
    GUARD = '[ "$OPENCODE" -eq 1 ] && command -v opencode >/dev/null 2>&1; then\n'
    AGENT = '"$REPO_DIR/dashboard/install-agent.sh" \\\n'
    TEXT = SCRIPTS["dashboard/install-agent.sh"]

    def test_its_branch_runs_the_agent_installer_after_the_cockpit_as_step_10_does(self):
        start = INSTALL.index("# Updated, not switched to.")
        seg = INSTALL[start:INSTALL.index("# --no-smoke always", start)]
        self.assertIn('if "$REPO_DIR/dashboard/install-dashboard.sh"; then', seg,
                      "the Agent tab only once the cockpit that relays it is installed")
        self.assertLess(seg.index('"$REPO_DIR/dashboard/install-dashboard.sh"'), seg.index(self.AGENT))
        self.assertIn(self.GUARD + "        " + self.AGENT, seg)
        self.assertIn('|| echo "NOTE: the Agent view did not install', seg, "a failure is a note, as at step 10")
        step10 = INSTALL[INSTALL.index('step "10/10 Alzeo cockpit'):]
        self.assertIn(self.GUARD + "          " + self.AGENT, step10, "the same condition as step 10's")

    def restarted(self, binary_after_start, served="1.19.4", on_disk="1.19.4"):
        """The script's own guard, run as written: the opencode-web unit, the opencode binary (its
        date after or before the server's start), the version the server answers with (None: it
        does not answer) and the one `opencode --version` gives."""
        t = pathlib.Path(tempfile.mkdtemp(prefix="agent-bin-"))
        self.addCleanup(shutil.rmtree, t, True)
        bin_, home = t / "bin", t / "home"
        bin_.mkdir(); (home / ".config" / "qwen38").mkdir(parents=True); (home / ".config" / "opencode").mkdir()
        start = 1_800_000_000
        oc = t / "opencode"
        for f in (oc, t / "opencode-web.env", home / ".config/qwen38/opencode.json", home / ".config/opencode/opencode.json"):
            f.write_text("x\n")
            os.utime(f, (start - 100, start - 100))
        if binary_after_start:
            os.utime(oc, (start + 100, start + 100))
        log = t / "calls"
        (bin_ / "systemctl").write_text(
            f'#!/bin/sh\ncase "$*" in *ActiveState*) echo active ;; *ExecMainStartTimestamp*) echo @{start} ;; esac\n')
        (bin_ / "sudo").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n')
        for f in ("systemctl", "sudo"):
            (bin_ / f).chmod(0o755)
        func = self.TEXT[self.TEXT.index("stale_since(){"):self.TEXT.index("\n}\n", self.TEXT.index("stale_since(){")) + 3]
        guard = self.TEXT[self.TEXT.index('SERVED_VERSION="$(health'):]
        guard = guard[:guard.index("\nfi\n") + 4]
        answer = "" if served is None else '{"healthy":true,"version":"%s"}' % served
        script = (f'set -euo pipefail\nUNIT=opencode-web.service; AGENT_CHANGED=0\n'
                  f'OPENCODE_BIN="{oc}"; ENV_FILE="{t}/opencode-web.env"; OC_VERSION="{on_disk}"\n'
                  f"health(){{ printf '%s' '{answer}'; }}\n" + func + "\n" + guard)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(home)})
        self.assertEqual(r.returncode, 0, r.stderr)
        return log.exists() and "try-restart opencode-web.service" in log.read_text()

    def test_a_newer_opencode_restarts_opencode_web(self):
        self.assertTrue(self.restarted(binary_after_start=True))

    def test_the_same_opencode_does_not(self):
        self.assertFalse(self.restarted(binary_after_start=False))
        self.assertFalse(self.restarted(binary_after_start=False, served="v1.19.4"), "a v in front is the same")

    def test_an_opencode_updated_with_an_older_date_restarts_it_by_its_version(self):
        """npm and Homebrew keep the date their archive gives the binary: a server started after
        that date ran the old opencode on, its binary older on disk than its start (found in
        review, 2026-10-22). The version it answers with tells."""
        self.assertTrue(self.restarted(binary_after_start=False, served="1.18.35", on_disk="1.19.4"))

    def test_a_server_that_does_not_answer_is_judged_by_dates_as_before(self):
        self.assertFalse(self.restarted(binary_after_start=False, served=None))
        self.assertTrue(self.restarted(binary_after_start=True, served=None))


class ARaisedLimitLeavesTheAgentTabRunning(unittest.TestCase):
    """The installer's fit leaves opencode-web alone when it only raises opencode's limits (v1.45.1), and
    install-agent.sh, which runs after it, restarted the server all the same: the two configs the fit had just
    rewritten were newer than the server's start (the reference box, update to v1.45.3, 2026-11-09: limits raised
    at 21:34:06, opencode-web restarted at 21:34:09). The fit now lists what it only raised in opencode-web.kept,
    by file, by content and for the server that runs, and the script's guard, run here as written, skips those."""
    WHICH = ("artifact", "user")        # the repo's own config, and the one under ~/.config/opencode
    TEXT = SCRIPTS["dashboard/install-agent.sh"]
    START = 1_800_000_000

    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="agent-kept-"))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.home = self.t / "home"
        (self.t / "bin").mkdir()
        (self.home / ".config/qwen38").mkdir(parents=True)
        (self.home / ".config/opencode").mkdir()
        self.configs = [self.home / ".config/qwen38/opencode.json", self.home / ".config/opencode/opencode.json"]
        self.kept = self.home / ".config/qwen38/opencode-web.kept"
        self.oc = self.t / "opencode"
        for f in (self.oc, self.t / "opencode-web.env", *self.configs):
            self.write(f, f"read at the start: {f}\n", -100)
        self.log = self.t / "calls"
        (self.t / "bin/systemctl").write_text(
            f'#!/bin/sh\ncase "$*" in *ActiveState*) echo active ;; *ExecMainStartTimestamp*) echo @{self.START} ;; esac\n')
        (self.t / "bin/sudo").write_text(f'#!/bin/sh\necho "$*" >> "{self.log}"\n')
        for f in ("systemctl", "sudo"):
            (self.t / "bin" / f).chmod(0o755)

    def write(self, f, text, after_start):
        f.write_text(text)
        os.utime(f, (self.START + after_start, self.START + after_start))

    def sha(self, f):
        return hashlib.sha256(f.read_bytes()).hexdigest()

    def restarted(self):
        func = self.TEXT[self.TEXT.index("stale_since(){"):self.TEXT.index("\n}\n", self.TEXT.index("stale_since(){")) + 3]
        guard = self.TEXT[self.TEXT.index('SERVED_VERSION="$(health'):]
        guard = guard[:guard.index("\nfi\n") + 4]
        script = (f'set -euo pipefail\nUNIT=opencode-web.service; AGENT_CHANGED=0\n'
                  f'OPENCODE_BIN="{self.oc}"; ENV_FILE="{self.t}/opencode-web.env"; OC_VERSION="1.19.4"\n'
                  "health(){ printf '%s' '{\"healthy\":true,\"version\":\"1.19.4\"}'; }\n" + func + "\n" + guard)
        self.log.unlink(missing_ok=True)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{self.t}/bin:/usr/bin:/bin", "HOME": str(self.home)})
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.log.exists() and "try-restart opencode-web.service" in self.log.read_text()

    def raise_both(self, start=None):
        """What the fit leaves after it only raised the limits: both files rewritten, each listed for the server."""
        for f in self.configs:
            self.write(f, f"limits raised: {f}\n", 3)
        self.kept.write_text("".join(f"{w} {self.sha(f)} {self.START if start is None else start}\n"
                                     for w, f in zip(self.WHICH, self.configs)))

    def test_nothing_newer_than_the_server_keeps_it(self):
        self.assertFalse(self.restarted())

    def test_a_config_changed_since_its_start_restarts_it_as_before(self):
        self.write(self.configs[0], "edited by hand\n", 60)
        self.assertTrue(self.restarted())

    def test_configs_the_fit_only_raised_do_not(self):
        self.raise_both()
        self.assertFalse(self.restarted(), "the Agent tab's turn ended at every update that raised its limits")

    def test_listed_for_another_server_restarts_it(self):
        self.raise_both(start=self.START - 3600)
        self.assertTrue(self.restarted())

    def test_a_listed_config_changed_again_restarts_it(self):
        self.raise_both()
        self.write(self.configs[1], "edited by hand after the fit\n", 60)
        self.assertTrue(self.restarted())

    def test_one_config_listed_and_the_other_not_restarts_it(self):
        self.raise_both()
        self.kept.write_text(f"artifact {self.sha(self.configs[0])} {self.START}\n")
        self.assertTrue(self.restarted())

    def test_the_same_bytes_in_the_other_file_are_not_listed_for_it(self):
        """The installer creates the user's config as a copy of the artifact, so the two are often the same bytes: a
        line for one named no file and vouched for the other (found in review, 2026-11-10)."""
        for f in self.configs:
            self.write(f, "the same bytes in both\n", 3)
        self.kept.write_text(f"artifact {self.sha(self.configs[0])} {self.START}\n")
        self.assertEqual(self.sha(self.configs[0]), self.sha(self.configs[1]))
        self.assertTrue(self.restarted())

    def test_the_other_reasons_still_count(self):
        self.raise_both()
        os.utime(self.oc, (self.START + 100, self.START + 100))
        self.assertTrue(self.restarted(), "a newer opencode binary")

    def test_a_server_whose_start_is_unknown_is_judged_by_dates(self):
        self.raise_both(start="")
        self.assertTrue(self.restarted())

    def fit(self):
        spec = importlib.util.spec_from_file_location("oc_fit_limits_kept", REPO / "oc-fit-limits.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m.CONFIG_DIR = self.home / ".config/qwen38"
        return m

    def before(self):
        """What the fit notes of each config before it writes: which, its path, its date, its sha256."""
        return [(w, f, int(f.stat().st_mtime), self.sha(f)) for w, f in zip(self.WHICH, self.configs)]

    def test_what_the_fit_writes_is_what_the_script_reads(self):
        """The two halves of the contract, each as shipped: oc-fit-limits.py's own record, read by the script's
        own guard."""
        m = self.fit()
        before = self.before()
        for f in self.configs:
            self.write(f, f"limits raised: {f}\n", 3)
        m.keep_agent(before, str(self.START))
        self.assertFalse(self.restarted())
        # a second raise before any restart: the files are newer than the server, and listed for it
        before = self.before()
        for f in self.configs:
            self.write(f, f"limits raised again: {f}\n", 600)
        m.keep_agent(before, str(self.START))
        self.assertFalse(self.restarted())

    def test_the_fit_does_not_vouch_for_a_change_it_did_not_make(self):
        """A config edited since the server started, then raised by the fit: the server never read the edit, so
        the fit lists nothing for that file and the script restarts the server."""
        m = self.fit()
        self.write(self.configs[0], "edited by hand\n", 60)
        before = self.before()
        for f in self.configs:
            self.write(f, f"limits raised: {f}\n", 120)
        m.keep_agent(before, str(self.START))
        self.assertEqual(self.kept.read_text().split(), ["user", self.sha(self.configs[1]), str(self.START)])
        self.assertTrue(self.restarted())

    def test_the_fit_does_not_vouch_for_a_copy_of_the_artifact_made_since_the_start(self):
        """The server read the user's own config at its start. The user then copied the artifact over it, as the
        cockpit suggests, and the fit raised both: the two files are the same bytes again, the artifact's line is
        written, and the user's file, which the server never read, still restarts it."""
        m = self.fit()
        self.write(self.configs[1], self.configs[0].read_text(), 60)
        before = self.before()
        for f in self.configs:
            self.write(f, "limits raised, the same bytes in both\n", 120)
        m.keep_agent(before, str(self.START))
        self.assertEqual(self.kept.read_text().split(), ["artifact", self.sha(self.configs[0]), str(self.START)])
        self.assertTrue(self.restarted())

    def test_a_config_dated_the_second_the_server_started_counts_as_read(self):
        """To the whole second, as the script's own check compares them: a file dated the server's start is not
        newer than it there, so the fit vouches for it too."""
        m = self.fit()
        for f in self.configs:
            os.utime(f, (self.START, self.START))
        before = self.before()
        for f in self.configs:
            self.write(f, f"limits raised: {f}\n", 3)
        m.keep_agent(before, str(self.START))
        self.assertFalse(self.restarted())

    def test_a_record_that_cannot_be_read_or_written_does_not_stop_the_fit(self):
        """The limits are written by the time the record is: a broken record costs a restart, never the fit."""
        m = self.fit()
        self.kept.write_bytes(b"\xff\xfe not text\n")
        before = self.before()
        for f in self.configs:
            self.write(f, f"limits raised: {f}\n", 3)
        m.keep_agent(before, str(self.START))
        self.assertFalse(self.restarted(), "the unreadable record is replaced by a good one")
        self.kept.unlink()
        self.kept.mkdir()                       # nothing can replace a directory
        m.keep_agent(self.before(), str(self.START))
        self.kept.rmdir()
        self.assertTrue(self.restarted())

    def test_the_fit_lists_nothing_for_a_server_that_does_not_run(self):
        m = self.fit()
        self.raise_both()
        before = [(w, f, self.START - 100, "x") for w, f in zip(self.WHICH, self.configs)]
        m.keep_agent(before, "")
        self.assertFalse(self.kept.exists(), "the lines of a server that is gone go with it")


class TheCockpitFollowsTheReleaseItNames(unittest.TestCase):
    """The cockpit reads the release it names (CHANGELOG.md's first heading) at start, and
    install-dashboard.sh restarted it only for its python or its unit: a box updated to
    v1.20.0, whose update touched only the page's static files and the CHANGELOG, kept
    naming v1.19.0 (2026-10-01). The guard, run as written against a fake systemctl."""
    TEXT = SCRIPTS["dashboard/install-dashboard.sh"]
    FUNC = TEXT[TEXT.index("stale_since(){"):TEXT.index("\n}\n", TEXT.index("stale_since(){")) + 3]
    GUARD = TEXT[TEXT.index('if [ "$DASH_CHANGED" -eq 1 ] || stale_since'):]
    GUARD = GUARD[:GUARD.index("\nfi\n") + 4]

    def restarted(self, changelog_after_start):
        t = pathlib.Path(tempfile.mkdtemp(prefix="dash-release-"))
        here, repo, bin_ = t / "dashboard", t, t / "bin"
        here.mkdir(); bin_.mkdir()
        start = 1_800_000_000
        for f in (here / "cockpit.py", repo / "oc-fit-limits.py", repo / "CHANGELOG.md"):
            f.write_text("x\n")
            os.utime(f, (start - 100, start - 100))
        if changelog_after_start:
            os.utime(repo / "CHANGELOG.md", (start + 100, start + 100))
        log = t / "calls"
        (bin_ / "systemctl").write_text(
            f'#!/bin/sh\ncase "$*" in *ActiveState*) echo active ;; *ExecMainStartTimestamp*) echo @{start} ;; esac\n')
        (bin_ / "sudo").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n')
        for f in ("systemctl", "sudo"):
            (bin_ / f).chmod(0o755)
        script = (f'set -euo pipefail\nUNIT=qwen38-dashboard.service; DASH_CHANGED=0\n'
                  f'HERE="{here}"; REPO_DIR="{repo}"\n' + self.FUNC + "\n" + self.GUARD)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{bin_}:/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return log.exists() and "try-restart qwen38-dashboard.service" in log.read_text()

    def test_a_new_release_restarts_it(self):
        self.assertTrue(self.restarted(changelog_after_start=True))

    def test_the_same_release_does_not(self):
        self.assertFalse(self.restarted(changelog_after_start=False))


class TheGeneratedConfigKeepsItsFit(unittest.TestCase):
    """The generator of ~/.config/qwen38/opencode.json, run twice in one config dir with the
    environment install.sh gives it: once as a fresh install, then again after the
    end-of-install fit wrote the pool's pair into the file."""

    BODY = None

    def run_generator(self, d, mode, cls="128"):
        start = INSTALL.index("OC_CONFIG_DIR=\"$CONFIG_DIR\" python3 - <<'PYEOF'")
        body = INSTALL[INSTALL.index("\n", start) + 1:INSTALL.index("\nPYEOF\n", start)]
        ctx, out = ("700000", "200000") if mode == "1m" else ("173000", "64000")
        env = dict(os.environ, OC_LANE="27b", OC_27B="1", OC_FLASH="0", OC_PORT="30001", OC_CTX=ctx,
                   OC_OUT=out, OC_LABEL="local, 1M", OC_CONTEXT_MODE=mode, OC_27B_CTX=ctx, OC_27B_OUT=out,
                   OC_KEEP="38000", OC_PIN="1", OC_CONFIG_DIR=str(d), OC_MEM_CLASS=cls)
        r = subprocess.run(["python3", "-c", body], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        f = d / "opencode.json"
        lim = json.loads(f.read_text())["provider"]["qwen38"]["models"]["qwen3.8-27b"]["limit"]
        return (lim["context"], lim["output"]), os.stat(f).st_mtime, f.read_text()

    def fitted_dir(self, ctx, out):
        d = pathlib.Path(tempfile.mkdtemp(prefix="oc-gen-"))
        self.run_generator(d, "1m")
        f = d / "opencode.json"
        f.write_text(f.read_text().replace('"context": 700000', f'"context": {ctx}').replace(
            '"input": 700000', f'"input": {ctx}').replace('"output": 200000', f'"output": {out}'))
        os.utime(f, (1000, 1000))
        return d

    def test_a_fit_under_the_1m_bounds_is_kept(self):
        pair, _, _ = self.run_generator(self.fitted_dir(559000, 186000), "1m")
        self.assertEqual(pair, (559000, 186000))

    def test_the_same_config_is_not_rewritten(self):
        d = self.fitted_dir(559000, 186000)
        before = (d / "opencode.json").read_text()
        _, mtime, text = self.run_generator(d, "1m")
        self.assertEqual(text, before)
        self.assertEqual(mtime, 1000, "an identical config was written again, and its date moved")

    def keep_after(self, d, mode="1m", lane="27b", cls="128"):
        """OC_KEEP as install.sh resolves it for the config in d."""
        start = INSTALL.index("# On 1m the table gives bounds and the fit to the pool sets the pair")
        block = INSTALL[start:INSTALL.index("\nfi\n", INSTALL.index("  if [ -n \"$OC_FIT_CTX\" ]; then", start)) + 4]
        script = (f'set -euo pipefail\ndie(){{ echo "DIE $*"; exit 1; }}\nREPO_DIR="{REPO}"\nCONFIG_DIR="{d}"\n'
                  f'CONTEXT_MODE={mode}\nLANE={lane}\nMEM_CLASS={cls}\nOC_CTX=700000\nOC_OUT=200000\nOC_KEEP=170000\n'
                  + block + 'echo "KEEP=$OC_KEEP"\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return int(r.stdout.strip().rsplit("KEEP=", 1)[1])

    def test_the_compaction_goes_with_the_fitted_pair(self):
        """Sized from the table's bound, the compaction was set back at every run and the fit
        at the end set it again: two writes, two backups and two opencode-web restarts per
        update on the reference box (2026-09-25). A fitted pair keeps its own."""
        want = int(subprocess.run([str(REPO / "oc-limits.sh"), "--preserve", "560000"],
                                  capture_output=True, text=True).stdout)
        self.assertEqual(self.keep_after(self.fitted_dir(560000, 186000)), want)
        self.assertLess(want, 170000)
        # the table's own pair, no fit, and the other lanes keep the table's value
        self.assertEqual(self.keep_after(self.fitted_dir(700000, 200000)), 170000)
        self.assertEqual(self.keep_after(self.fitted_dir(560000, 186000), mode="native"), 170000)
        self.assertEqual(self.keep_after(self.fitted_dir(560000, 186000), lane="flash"), 170000)
        self.assertEqual(self.keep_after(pathlib.Path(tempfile.mkdtemp(prefix="oc-none-"))), 170000)

    def test_a_native_install_writes_the_table(self):
        pair, _, _ = self.run_generator(self.fitted_dir(559000, 186000), "native")
        self.assertEqual(pair, (173000, 64000))

    def test_a_64_gb_box_keeps_its_fit_under_the_native_bounds(self):
        # its native pair is a bound, fitted to the pool at the end as 1m's is: writing the bound over
        # the fit made every update write twice, with two backups and two opencode-web restarts
        # (found in review, 2026-10-23)
        pair, _, _ = self.run_generator(self.fitted_dir(140000, 32000), "native", cls="64")
        self.assertEqual(pair, (140000, 32000))
        want = int(subprocess.run([str(REPO / "oc-limits.sh"), "--preserve", "140000"],
                                  capture_output=True, text=True).stdout)
        self.assertEqual(self.keep_after(self.fitted_dir(140000, 32000), mode="native", cls="64"), want)
        # a 128 GB box's native install still writes the table, and keeps its compaction
        self.assertEqual(self.keep_after(self.fitted_dir(140000, 32000), mode="native"), 170000)


class Step7OnA64GBBox(unittest.TestCase):
    """Step 7's merge of the 27B's limits, run from install.sh's own lines with a merge that
    writes down what it is asked (found in review, 2026-10-23)."""

    def run_step(self, cls, unit_ctx=1010000):
        d = pathlib.Path(tempfile.mkdtemp(prefix="step7-64-"))
        (d / "oc-merge-limits.py").write_text(f'import sys\nopen("{d}/calls", "a").write(" ".join(sys.argv[1:]) + "\\n")\n')
        (d / "unit").write_text(f"ExecStart=... --context-length {unit_ctx} ...\n")
        start = INSTALL.index('    UNIT_CTX="$(grep -oE -- \'--context-length [0-9]+\'')
        block = INSTALL[start:INSTALL.index("    # Offered whatever the limits branch decided above", start)]
        script = (f'set -euo pipefail\nREPO_DIR="{d}"\nSGL_UNIT_PATH="{d}/unit"\nCONTEXT_MODE=native\n_ENV_CONTEXT_MODE=\n'
                  f'MEM_CLASS={cls}\nOC_USER_CFG="{d}/opencode.json"\nOC_CTX=173000\nOC_OUT=64000\nOC_KEEP=38000\n'
                  "if true; then\n" + block + "fi\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = (d / "calls").read_text().splitlines() if (d / "calls").exists() else []
        return r.stdout, calls

    def test_a_64_gb_box_with_a_1m_unit_has_its_limits_managed(self):
        # the convergence takes it back to the native window: keeping the 1M limits asked 700,000 of
        # a 262,144 window, under advice to pass the CONTEXT_MODE=1m it refuses
        out, calls = self.run_step("64")
        self.assertNotIn("keeping the existing", out)
        self.assertIn(f"{self.cfg_of(calls)} qwen38 qwen3.8-27b 173000 64000 --keep-lower", calls)

    def test_its_merge_keeps_a_fitted_pair_under_the_bound(self):
        out, calls = self.run_step("64", unit_ctx=262144)
        self.assertIn(f"{self.cfg_of(calls)} qwen38 qwen3.8-27b 173000 64000 --keep-lower", calls)

    def test_a_128_gb_box_is_as_before(self):
        out, calls = self.run_step("128")
        self.assertIn("keeping the existing", out)
        self.assertEqual(calls, [])
        out, calls = self.run_step("128", unit_ctx=262144)
        self.assertIn(f"{self.cfg_of(calls)} qwen38 qwen3.8-27b 173000 64000", calls)

    @staticmethod
    def cfg_of(calls):
        return calls[0].split()[0] if calls else "?"


if __name__ == "__main__":
    unittest.main(verbosity=2)
