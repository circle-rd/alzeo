#!/usr/bin/env python3
"""./install-flash-local.sh --uninstall removes the copies of the n-gram table ple-table.py made, and
nothing else.

The copies sit in the folder the unit mounts them from, the folder FLASH_LOCAL_PLE_DIR chose (kept in
~/.config/qwen38/flash-local-ple-dir) or ~/flash-local-ple, and ple-table.py lists them in the folder's
.qwen38-ple-copies. The uninstall removes the copies that list names, their .part, the list, and the
folder once nothing else is in it: a folder of the user's own keeps the user's files. The script's own
uninstall block runs here, whole, against a fake unit, a fake config directory and a fake home; sudo,
systemctl and docker are stubs that write down every call, as in test_nemotron_uninstall.py."""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install-flash-local.sh").read_text()
START = TEXT.index('if [ "$ACTION" = uninstall ]; then')
BLOCK = TEXT[START:TEXT.index("\n  exit 0\nfi\n", START) + len("\n  exit 0\nfi\n")]
PINS = [ln for ln in TEXT.splitlines() if ln.startswith(("FLASH_LOCAL_REPO=", "FLASH_LOCAL_IMAGE=", "VLANE="))]
HUB = "/root/.cache/huggingface/hub/models--nvidia--Qwen3.8-Flash-Next-FP4-Local/blobs"
A, B, OLD = "a" * 64, "b" * 64, "c" * 64


class TheUninstall(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="fl-uninstall-"))
        self.home = self.d / "home"
        self.cfg = self.home / ".config" / "qwen38"
        self.cfg.mkdir(parents=True)
        (self.cfg / "vllm-auth").mkdir()
        for f in ("chat-template-flash-local.jinja", "seccomp-io-uring.json", "flash-local-files.sh",
                  "flash-local-context", "flash-local-yarn.json"):
            (self.cfg / f).write_text("x\n")
        self.sd = self.d / "systemd"
        self.sd.mkdir()
        self.bin = self.d / "bin"
        self.bin.mkdir()
        self.log = self.d / "calls.log"
        for tool in ("sudo", "docker", "systemctl"):
            (self.bin / tool).write_text(f'#!/bin/bash\necho "{tool} $*" >> "{self.log}"\n'
                                         + ('[ "$1" = is-enabled ] && exit 1\n' if tool == "systemctl" else "")
                                         + "exit 0\n")
            (self.bin / tool).chmod(0o755)
        self.unit = self.sd / "qwen38-flash-local.service"

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    @staticmethod
    def folder(path, ours, theirs=()):
        path.mkdir(parents=True, exist_ok=True)
        for name in ours:
            (path / name).write_bytes(b"copy")
        (path / (ours[0] + ".part")).write_bytes(b"cut short")
        (path / ".qwen38-ple-copies").write_text("".join(n + "\n" for n in ours))
        for name in theirs:
            (path / name).write_bytes(b"not ours")
        return path

    def run_block(self, mounted=None):
        mounts = "".join(f" -v {mounted}/{n}:{HUB}/{n}:ro" for n in (A, B)) if mounted else ""
        self.unit.write_text("ExecStart=/bin/bash -c 'exec /usr/bin/docker run" + mounts + " x'\n")
        script = "\n".join(["set -euo pipefail", "step(){ :; }", f'LANES_UNIT_DIR="{self.sd}"',
                            f'. "{REPO}/lanes.sh"', f'. "{REPO}/vllm-lane.sh"', *PINS, f'HERE="{REPO}"',
                            "ACTION=uninstall", "UNIT=qwen38-flash-local.service", f'INSTALLED="{self.unit}"',
                            "CONTAINER=qwen38-flash-local", f'CONFIG_DIR="{self.cfg}"', f'HF_CACHE="{self.d}/hf"',
                            BLOCK])
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", HOME=str(self.home))
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_the_copies_the_unit_mounts_go_with_their_list_and_their_folder(self):
        f = self.folder(self.d / "mounted", [A, B])
        out = self.run_block(mounted=f)
        self.assertFalse(f.exists())
        self.assertIn(f"the n-gram table's copies removed from {f} (2 files)", out)

    def test_in_a_folder_of_the_users_only_its_own_copies_go(self):
        f = self.folder(self.d / "data", [A, B, OLD], theirs=["d" * 64, "notes.txt"])
        (self.cfg / "flash-local-ple-dir").write_text(f"{f}\n")
        self.run_block()
        self.assertEqual(sorted(p.name for p in f.iterdir()), ["d" * 64, "notes.txt"])
        self.assertFalse((self.cfg / "flash-local-ple-dir").exists())

    def test_the_default_folder_goes_and_the_lanes_files_with_it(self):
        f = self.folder(self.home / "flash-local-ple", [A, B])
        self.run_block()
        self.assertFalse(f.exists())
        # the 1M window's choice and overrides too: kept, a later install came back to it unasked (code review of
        # v1.45.2)
        for gone in ("chat-template-flash-local.jinja", "seccomp-io-uring.json", "flash-local-files.sh",
                     "flash-local-context", "flash-local-yarn.json"):
            self.assertFalse((self.cfg / gone).exists(), gone)

    def test_a_folder_with_no_list_is_left_alone(self):
        f = self.d / "plain"
        f.mkdir()
        (f / A).write_bytes(b"whose?")
        (self.cfg / "flash-local-ple-dir").write_text(f"{f}\n")
        self.run_block(mounted=f)
        self.assertEqual((f / A).read_bytes(), b"whose?")


UN = (REPO / "uninstall.sh").read_text()


def uninstall_lines(folder):
    """uninstall.sh's own lines for the lane's copies, run on one folder: its inventory, then the
    command it prints to reclaim the space."""
    start = UN.index("fl_copies() {")
    fn = UN[start:UN.index("\n", start) + 1]
    loops, i = [], 0
    while (j := UN.find('done <<< "$FL_TABLE_DIRS"', i)) >= 0:
        loops.append(UN[UN.rfind("while IFS= read -r p; do", 0, j):j + len('done <<< "$FL_TABLE_DIRS"')])
        i = j + 1
    script = fn + f"FL_TABLE_DIRS='{folder}'\n" + "\n".join(loops) + "\n"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin"})
    return r.returncode, r.stdout


class TheUninstallersCommands(unittest.TestCase):
    """uninstall.sh names the lane's copies in a folder by the list ple-table.py keeps there: a
    folder of the user's own (FLASH_LOCAL_PLE_DIR) was offered to `rm -rf` whole."""

    def test_its_command_removes_the_copies_and_nothing_else_when_pasted(self):
        d = pathlib.Path(tempfile.mkdtemp(prefix="fl-reclaim-"))
        try:
            f = TheUninstall.folder(d / "data", [A, B], theirs=["d" * 64, "notes.txt"])
            rc, out = uninstall_lines(f)
            self.assertEqual(rc, 0, out)
            inventory, command = [ln for ln in out.splitlines() if ln.strip()]
            self.assertIn(f"table     {f} (", inventory)
            self.assertNotIn("rm -rf", command)
            r = subprocess.run(["bash", "-c", command.split("    #")[0]], capture_output=True, text=True, timeout=30)
            self.assertEqual(sorted(p.name for p in f.iterdir()), ["d" * 64, "notes.txt"], r.stderr)
            # alone in its folder, the folder goes too
            g = TheUninstall.folder(d / "ple", [A])
            rc, out = uninstall_lines(g)
            subprocess.run(["bash", "-c", out.splitlines()[-1].split("    #")[0]], capture_output=True, timeout=30)
            self.assertFalse(g.exists())
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_folder_with_no_list_is_not_offered(self):
        d = pathlib.Path(tempfile.mkdtemp(prefix="fl-reclaim-"))
        try:
            (d / A).write_bytes(b"whose?")
            self.assertEqual(uninstall_lines(d), (0, ""))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
