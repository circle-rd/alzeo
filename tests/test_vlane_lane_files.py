#!/usr/bin/env python3
"""What a vLLM lane's start reads, as vlane_put_unit (vllm-lane.sh) lists it to say a serving lane runs
older files than the ones just installed.

The Qwen3.8 Flash-Next FP4-Local lane's start also reads its patched chat template and its seccomp filter, which its
installer added to the list only after the shared step had said its NOTE, so a run that changed only
those said nothing; and the list named engine-secrets.sh, which its unit never runs, so a change of that
script alone would have asked the lane for a restart it does not need (found in review, 2026-10-20).
vllm-lane.sh's own function runs here against a fake unit, with a systemctl that says the lane serves
since a given time, and files older or newer than that."""
import os
import pathlib
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]


class TheListOfWhatTheLaneReads(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="vlane-files-"))
        self.cfg = self.d / "cfg"
        (self.cfg / "vllm-auth").mkdir(parents=True)
        self.bin = self.d / "bin"
        self.bin.mkdir()
        self.started = int(time.time()) - 600
        (self.bin / "systemctl").write_text(
            "#!/bin/bash\ncase \"$*\" in\n"
            "  *ActiveState*) echo active ;;\n"
            f"  *ExecMainStartTimestamp*) echo {self.started} ;;\nesac\nexit 0\n")
        (self.bin / "sudo").write_text("#!/bin/bash\nexit 0\n")
        for f in self.bin.iterdir():
            f.chmod(0o755)

    def file(self, name, newer):
        p = self.cfg / name
        p.write_text("x\n")
        t = self.started + (60 if newer else -60)
        os.utime(p, (t, t))
        return p

    def put(self, unit_text, *extra):
        unit = self.d / "qwen38-x.service"
        unit.write_text(unit_text)
        os.utime(unit, (self.started - 60, self.started - 60))
        script = "\n".join([
            "set -euo pipefail", f'LANES_UNIT_DIR="{self.d}"', f'. "{REPO}/lanes.sh"', f'. "{REPO}/vllm-lane.sh"',
            'die(){ echo "die: $*"; exit 1; }', "unit_put(){ echo same; }",
            f'CONFIG_DIR="{self.cfg}"', f'INSTALLED="{unit}"', "UNIT=qwen38-x.service", "VLANE_SHORT=X", "SMOKE=0",
            f'vlane_put_unit "{unit}" ' + " ".join(f'"{e}"' for e in extra),
            'printf "%s\\n" "${LANE_FILES[@]}"'])
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def shared(self, newer=()):
        for name in ("vllm-auth/vllm_engine_auth.py", "engine-preflight.sh", "engine-secrets.sh"):
            self.file(name, name in newer)

    def test_the_files_its_installer_names_are_read_before_the_note(self):
        self.shared()
        template = self.file("chat-template-flash-local.jinja", newer=True)
        out = self.put("ExecStart=/bin/bash -c 'exec docker run x'\n", self.cfg / "seccomp-io-uring.json", template)
        self.assertIn("runs older files than the ones installed now", out)
        self.assertIn(str(template), out)

    def test_a_unit_that_runs_no_secrets_script_does_not_wait_on_it(self):
        self.shared(newer=("engine-secrets.sh",))
        out = self.put("ExecStart=/bin/bash -c 'exec docker run x'\n")
        self.assertNotIn("runs older files", out)
        self.assertNotIn("engine-secrets.sh", out)

    def test_a_unit_that_runs_it_still_does(self):
        self.shared(newer=("engine-secrets.sh",))
        out = self.put("ExecStartPre=/bin/bash /cfg/engine-secrets.sh\nExecStart=/bin/bash -c 'exec docker run x'\n")
        self.assertIn("runs older files than the ones installed now", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
