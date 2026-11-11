#!/usr/bin/env python3
"""The GPU clock cap the cockpit keeps goes with the cockpit (decision of 2026-11-11).

The Machine view's cap holds until the next reboot of the machine, and the cockpit keeps it in
~/.config/qwen38/cockpit-gpu-clock.json to set it again at each of its starts. Left by an uninstall,
the cap held with nothing left to say it, and the file set it again under a later install. So
uninstall.sh lifts it (nvidia-smi -rgc, the line its sudoers allowlist names) once the cockpit is
stopped and before that allowlist goes, then removes the file, whether the config folder stays or
not; a cap the cockpit does not keep (set by hand) is left alone; a refused sudo leaves both, as it
leaves the services. --list names the file, and install-dashboard.sh's "Remove with" line lifts the
cap in the same place. uninstall.sh runs as in test_uninstall_without_sudo.py: a copy, its system
paths in a sandbox, sudo, docker and systemctl stubs that only record what they were asked.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_uninstall_without_sudo import Box  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
LIFT = "sudo /usr/bin/nvidia-smi -rgc"


def with_cap(box, text='{"cap": "2400"}\n'):
    f = box.home / ".config/qwen38/cockpit-gpu-clock.json"
    f.write_text(text)
    return f


class UninstallLiftsTheCap(unittest.TestCase):
    def test_lifted_once_the_cockpit_stops_and_before_its_sudoers_lines_go(self):
        box = Box(units=("qwen38-dashboard.service",), sudo_ok=True)
        self.addCleanup(shutil.rmtree, box.d, True)
        cap = with_cap(box)
        rc, out, calls = box.run()                     # no --yes, no terminal: the config folder stays
        self.assertEqual(rc, 0, out)
        lines = calls.splitlines()
        self.assertEqual(lines.count(LIFT), 1, calls)
        stop = lines.index("sudo systemctl disable --now qwen38-dashboard.service")
        gone = next(i for i, ln in enumerate(lines) if ln.startswith("sudo rm -f") and f"{box.etc}/sudoers" in ln)
        self.assertLess(stop, lines.index(LIFT), "the cockpit could set it again")
        self.assertLess(lines.index(LIFT), gone, "lifted after the allowlist that names it went")
        self.assertIn("GPU clock cap lifted.", out)
        self.assertFalse(cap.exists(), "a later install's cockpit would set it again")
        self.assertTrue((box.home / ".config/qwen38/api-key").exists(), "the rest of the config folder stays")

    def test_no_cap_kept_nothing_lifted(self):
        box = Box(units=("qwen38-dashboard.service",), sudo_ok=True)
        self.addCleanup(shutil.rmtree, box.d, True)
        rc, out, calls = box.run("--yes")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("nvidia-smi", calls, "a cap set by hand outside the cockpit is the operator's")

    def test_refused_sudo_leaves_the_cap_and_its_file(self):
        box = Box(units=("qwen38-dashboard.service",), sudo_ok=False)
        self.addCleanup(shutil.rmtree, box.d, True)
        cap = with_cap(box)
        rc, out, calls = box.run("--yes")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("nvidia-smi", calls)
        self.assertTrue(cap.exists(), "the cockpit still running sets it again from this file")

    def test_a_cap_kept_alone_is_still_lifted(self):
        # the cockpit gone by hand, its file left: the cap may hold, and only root lifts it
        box = Box(units=(), sudo_ok=True)
        self.addCleanup(shutil.rmtree, box.d, True)
        cap = with_cap(box)
        rc, out, calls = box.run()
        self.assertEqual(rc, 0, out)
        self.assertIn("sudo -v", calls)
        self.assertIn(LIFT, calls.splitlines())
        self.assertFalse(cap.exists())

    def test_a_lift_nvidia_smi_refuses_is_said_and_the_file_goes(self):
        box = Box(units=("qwen38-dashboard.service",), sudo_ok=True)
        self.addCleanup(shutil.rmtree, box.d, True)
        (box.stub / "sudo").write_text(f'#!/bin/sh\necho "sudo $*" >> {box.log}\ncase "$*" in *nvidia-smi*) exit 3 ;; esac\nexit 0\n')
        cap = with_cap(box)
        rc, out, _ = box.run()
        self.assertEqual(rc, 0, out)
        self.assertIn("NOTE: nvidia-smi did not lift the GPU clock cap: the next reboot does, or sudo /usr/bin/nvidia-smi -rgc now.", out)
        self.assertFalse(cap.exists())

    def test_the_inventory_names_the_file_and_its_cap(self):
        box = Box(units=("qwen38-dashboard.service",), sudo_ok=True)
        self.addCleanup(shutil.rmtree, box.d, True)
        cap = with_cap(box, '{"cap": "2200"}\n')
        rc, out, calls = box.run("--list")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"  setting   {cap} (the cockpit's GPU clock cap, 2200 MHz, set again at each of its starts: "
                      "lifted with the cockpit)", out)
        self.assertNotIn("nvidia-smi", calls, "--list changes nothing")
        self.assertTrue(cap.exists())


class TheRemoveLineOfTheDashboardInstaller(unittest.TestCase):
    """install-dashboard.sh prints how to remove the cockpit alone: with a cap kept, the lift comes
    after the stop of the cockpit and before its allowlist goes, as uninstall.sh does it."""

    def summary(self, cap):
        text = (REPO / "dashboard" / "install-dashboard.sh").read_text()
        body = text[text.index("# DASH_QUIET=1:"):]
        home = pathlib.Path(tempfile.mkdtemp(prefix="dash-cap-"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / ".config/qwen38").mkdir(parents=True)
        (home / ".config/qwen38/api-key").write_text("k\n")
        if cap:
            (home / ".config/qwen38/cockpit-gpu-clock.json").write_text('{"cap": "2400"}\n')
        script = ("set -euo pipefail\nPROBE=127.0.0.1; PORT=30090; BIND=127.0.0.1; AGENT_PORT=0; "
                  "AGENT_BIND=tailscale; AGENT_UPSTREAM=http://127.0.0.1:4096; UNIT=qwen38-dashboard.service; "
                  f"INSTALLED=/etc/systemd/system/qwen38-dashboard.service; HOME={home}\n" + body)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin", "DASH_QUIET": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return next(ln for ln in r.stdout.splitlines() if ln.startswith("Remove with: ")), home

    def test_a_kept_cap_is_lifted_between_the_stop_and_the_sudoers(self):
        line, home = self.summary(cap=True)
        self.assertIn("sudo systemctl disable --now qwen38-dashboard.service; sudo /usr/bin/nvidia-smi -rgc; "
                      f"rm -f {home}/.config/qwen38/cockpit-gpu-clock.json; sudo rm -f /etc/systemd/system/qwen38-dashboard.service "
                      "/etc/sudoers.d/qwen38-cockpit", line)

    def test_no_cap_kept_no_lift(self):
        line, _ = self.summary(cap=False)
        self.assertNotIn("nvidia-smi", line)
        self.assertIn("sudo systemctl disable --now qwen38-dashboard.service; sudo rm -f /etc/systemd/system/", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
