#!/usr/bin/env python3
"""The Qwen3.8 Flash-Next FP4-Local lane reads its n-gram table from the disk of the home directory.

The table lives in seven of the checkpoint's files and is read at every token: from a USB SSD the
lane decoded 15.6 tokens a second for eight requests, from the internal NVMe 77.7 (reference box,
2026-10-19). ple-table.py copies those files next to the home directory when the cache is on
another disk, checks every copy against its blob's sha256, and gives the unit the mounts that put
the copies where the engine reads. These run it on a fake cache of a few kilobytes."""
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "ple-table.py"
NAME = "nvidia/Qwen3.8-Flash-Next-FP4-Local"
REV = "b9ce18a6887e18c089fdea8509a33e63429a3300"
HUB = "models--nvidia--Qwen3.8-Flash-Next-FP4-Local"


def load():
    spec = importlib.util.spec_from_file_location("ple_table", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCache:
    """A cache holding a checkpoint whose table is in two of its three files, as blobs named by sha256."""

    def __init__(self, root: pathlib.Path):
        self.root = root
        self.blobs = root / "hub" / HUB / "blobs"
        self.snap = root / "hub" / HUB / "snapshots" / REV
        (self.snap / "tensors").mkdir(parents=True)
        self.blobs.mkdir(parents=True)
        self.files = {}
        weight_map = {}
        for i, (tensors, body) in enumerate([
                (["model.layers.0.mlp.experts.0.weight"], b"experts" * 100),
                (["model.language_model.layers.0.ple.ple_embedding.ngram_embedding.shard_0.weight",
                  "model.language_model.layers.0.self_attn.q_proj.weight"], b"table-and-more" * 100),
                (["model.language_model.layers.0.ple.ple_embedding.ngram_embedding.shard_1.weight"], b"table" * 100)]):
            name = f"tensors/model-0000{i + 1}-of-00003.safetensors"
            sha = hashlib.sha256(body).hexdigest()
            (self.blobs / sha).write_bytes(body)
            os.symlink(os.path.relpath(self.blobs / sha, (self.snap / name).parent), self.snap / name)
            self.files[name] = sha
            for t in tensors:
                weight_map[t] = name
        (self.snap / "model.safetensors.index.json").write_text(json.dumps({"weight_map": weight_map}))
        self.table = [self.files["tensors/model-00002-of-00003.safetensors"],
                      self.files["tensors/model-00003-of-00003.safetensors"]]


class TableFiles(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="ple-table-"))
        self.cache = FakeCache(self.d / "hf")
        self.home = self.d / "home"
        self.home.mkdir()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_it(self, action, *extra):
        return subprocess.run([sys.executable, "-I", str(SCRIPT), str(self.cache.root), NAME, REV,
                               "--home", str(self.home), *extra, action],
                              capture_output=True, text=True, timeout=60)

    def test_the_files_of_the_table_are_the_ones_its_tensors_are_in(self):
        mod = load()
        got = [b.name for _, b in mod.table_files(self.cache.snap)]
        self.assertEqual(got, self.cache.table)

    def test_a_cache_on_the_home_directorys_disk_is_read_in_place(self):
        r = self.run_it("plan")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("in place", r.stdout)
        self.assertEqual(self.run_it("mounts").stdout, "")

    def test_a_cache_on_another_disk_is_read_from_copies_in_the_home_directory(self):
        mod = load()
        real = os.stat

        def fake_stat(p, *a, **k):
            st = real(p, *a, **k)
            if pathlib.Path(p) == self.cache.blobs:
                return types.SimpleNamespace(st_dev=st.st_dev + 1)
            return st
        args = types.SimpleNamespace(dir="", home=str(self.home))
        with mock.patch.object(mod.os, "stat", fake_stat):
            self.assertEqual(mod.where(args, self.cache.blobs), self.home / "flash-local-ple")
        self.assertIsNone(mod.where(args, self.cache.blobs))
        self.assertIsNone(mod.where(types.SimpleNamespace(dir="none", home=str(self.home)), self.cache.blobs))

    def test_copies_are_checked_then_mounted_over_the_caches_blobs(self):
        folder = self.d / "copies"
        r = self.run_it("copy", "--dir", str(folder))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(p.name for p in folder.iterdir() if not p.name.startswith(".")), sorted(self.cache.table))
        mounts = self.run_it("mounts", "--dir", str(folder)).stdout.split()
        want = [x for sha in self.cache.table for x in
                ("-v", f"{folder / sha}:/root/.cache/huggingface/hub/{HUB}/blobs/{sha}:ro")]
        self.assertEqual(mounts, want)

    def test_a_damaged_cache_file_is_never_copied(self):
        folder = self.d / "copies"
        (self.cache.blobs / self.cache.table[1]).write_bytes(b"bitrot" * 100)
        r = self.run_it("copy", "--dir", str(folder))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not match its sha256", r.stderr)
        self.assertFalse((folder / self.cache.table[1]).exists())
        self.assertFalse((folder / (self.cache.table[1] + ".part")).exists())

    def test_a_copy_that_changed_since_is_made_again(self):
        folder = self.d / "copies"
        self.assertEqual(self.run_it("copy", "--dir", str(folder)).returncode, 0)
        good = (folder / self.cache.table[0]).read_bytes()
        (folder / self.cache.table[0]).write_bytes(b"X" * len(good))
        self.assertEqual(self.run_it("copy", "--dir", str(folder)).returncode, 0)
        self.assertEqual((folder / self.cache.table[0]).read_bytes(), good)

    def test_an_empty_folder_left_in_a_copys_place_goes_and_one_with_files_is_said(self):
        # a start of v1.37.0 with a copy missing made Docker create a folder in its place, and the
        # copy's rename failed on every Load after copying it whole (found in review, 2026-10-20)
        folder = self.d / "copies"
        folder.mkdir()
        (folder / self.cache.table[0]).mkdir()
        r = self.run_it("copy", "--dir", str(folder))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((folder / self.cache.table[0]).is_file())
        self.assertEqual(hashlib.sha256((folder / self.cache.table[0]).read_bytes()).hexdigest(), self.cache.table[0])
        (folder / self.cache.table[1]).unlink()
        (folder / self.cache.table[1]).mkdir()
        (folder / self.cache.table[1] / "x").write_text("x")
        r = self.run_it("copy", "--dir", str(folder))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("is a folder, not a copy", r.stderr)
        self.assertIn("sudo rm -r", r.stderr)
        self.assertFalse((folder / (self.cache.table[1] + ".part")).exists(), "nothing copied before saying so")

    def new_revision_of_the_table(self):
        """The checkpoint's second table file changed, as a later revision would change it."""
        body = b"table, revised" * 100
        sha = hashlib.sha256(body).hexdigest()
        (self.cache.blobs / sha).write_bytes(body)
        link = self.cache.snap / "tensors/model-00003-of-00003.safetensors"
        link.unlink()
        os.symlink(os.path.relpath(self.cache.blobs / sha, link.parent), link)
        return [self.cache.table[0], sha]

    def listed(self, folder):
        return sorted(p.name for p in folder.iterdir() if not p.name.startswith("."))

    def test_an_earlier_revisions_copies_go_and_nothing_else(self):
        folder = self.d / "copies"
        self.assertEqual(self.run_it("copy", "--dir", str(folder)).returncode, 0)
        (folder / "notes.txt").write_text("mine")
        gone = self.cache.table[1]
        (folder / (gone + ".part")).write_bytes(b"cut short")
        table = self.new_revision_of_the_table()
        r = self.run_it("copy", "--dir", str(folder))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.listed(folder), sorted(table + ["notes.txt"]))

    def test_a_file_it_did_not_make_stays_whatever_its_name(self):
        # its copies are named by their sha256, as the cache's blobs are: a folder that holds other
        # such files (another model's blobs) lost them all at the first copy (found in review,
        # 2026-10-20)
        folder = self.d / "copies"
        folder.mkdir()
        theirs = ["b" * 64, "c" * 64 + ".part"]
        for name in theirs:
            (folder / name).write_bytes(b"not ours")
        self.assertEqual(self.run_it("copy", "--dir", str(folder)).returncode, 0)
        self.new_revision_of_the_table()
        self.assertEqual(self.run_it("copy", "--dir", str(folder)).returncode, 0)
        for name in theirs:
            self.assertEqual((folder / name).read_bytes(), b"not ours")

    def test_a_folder_inside_the_cache_is_refused_and_the_cache_kept(self):
        # pointed at the checkpoint's own blobs, the copy found the table's files already there and
        # removed every other blob of the checkpoint as an earlier revision's copies
        before = sorted(p.name for p in self.cache.blobs.iterdir())
        for folder in (self.cache.blobs, self.cache.root / "ple"):
            r = self.run_it("copy", "--dir", str(folder))
            self.assertNotEqual(r.returncode, 0, folder)
            self.assertIn("inside the Hugging Face cache", r.stdout + r.stderr)
        self.assertEqual(sorted(p.name for p in self.cache.blobs.iterdir()), before)
        self.assertFalse((self.cache.root / "ple").exists())

    def test_a_folder_the_unit_cannot_name_is_refused(self):
        # the unit names the copies inside bash -c '...', put in by sed: a space split the mount, a
        # quote ended the command, a & or a | broke the substitution, and the unit restarted in a loop
        # and a : split docker's -v into more fields than it takes (found in review, 2026-10-20):
        # install.sh's own rule for the paths a unit carries, the same
        for bad in ("copies here", "it's", "a&b", "a|b", "ple:2026", "a%b"):
            with self.subTest(folder=bad):
                for action in ("copy", "check"):
                    r = self.run_it(action, "--dir", str(self.d / bad))
                    self.assertNotEqual(r.returncode, 0)
                    self.assertIn("letters, digits and . _ / + @ - only", r.stdout + r.stderr)
                self.assertFalse((self.d / bad).exists())

    def test_check_needs_no_checkpoint_and_passes_a_plain_folder(self):
        shutil.rmtree(self.cache.snap)
        for good in ("none", "", str(self.d / "ple-copies_2.0+x@y")):
            with self.subTest(folder=good):
                r = self.run_it("check", "--dir", good)
                self.assertEqual(r.returncode, 0, r.stderr)

    def room(self, folder, free):
        mod = load()
        args = ["ple-table.py", str(self.cache.root), NAME, REV, "--home", str(self.home), "--dir", str(folder),
                "--need-gb", "33", "room"]
        with mock.patch.object(sys, "argv", args), \
             mock.patch.object(mod.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=free)):
            mod.main()

    def test_room_is_said_before_any_download(self):
        # the copy refused only after the checkpoint's 102 GB were fetched (found in review, 2026-10-20)
        folder = self.d / "copies"
        # with the checkpoint there, what is missing of the copies: a few KB here
        self.room(folder, free=2 * 2 ** 30)
        with self.assertRaises(SystemExit):
            self.room(folder, free=2 ** 29)
        # before it, the table's size
        shutil.rmtree(self.cache.snap)
        with self.assertRaises(SystemExit) as e:
            self.room(folder, free=20 * 10 ** 9)
        self.assertIn("need about 34 GB", str(e.exception))
        self.room(folder, free=40 * 10 ** 9)
        # read where the cache is, nothing to make room for
        self.room("none", free=0)

    def test_mounts_refuse_while_a_copy_is_missing(self):
        r = self.run_it("mounts", "--dir", str(self.d / "empty"))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")

    def test_an_empty_or_cut_copy_is_no_copy_to_mount(self):
        # mounts took any file for a copy: an empty one, which the unit's check refuses at every
        # start, went into the unit (found in review, 2026-10-20)
        folder = self.d / "copies"
        self.assertEqual(self.run_it("copy", "--dir", str(folder)).returncode, 0)
        self.assertEqual(self.run_it("mounts", "--dir", str(folder)).returncode, 0)
        copy = folder / self.cache.table[0]
        good = copy.read_bytes()
        for broken in (b"", good[:-1]):
            with self.subTest(size=len(broken)):
                copy.write_bytes(broken)
                r = self.run_it("mounts", "--dir", str(folder))
                self.assertEqual((r.returncode, r.stdout), (1, ""), r.stderr)
                self.assertIn(f"{len(good)} to copy", self.run_it("plan", "--dir", str(folder)).stdout)
        copy.write_bytes(good)

    def test_a_folder_it_cannot_write_is_said_with_its_remedy(self):
        # a start of v1.37.0 that found the whole folder missing made it, as root: the copy ended in a
        # traceback, or said a folder that held no file held some (found in review, 2026-10-20)
        folder = self.d / "copies"
        folder.mkdir()
        (folder / self.cache.table[0]).mkdir()
        folder.chmod(0o555)
        try:
            r = self.run_it("copy", "--dir", str(folder))
        finally:
            folder.chmod(0o755)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("holds files", r.stderr)
        self.assertIn(f"{folder} is not writable by", r.stderr)
        # the user's own folder: chmod gives it back, not chown (found in review, 2026-10-20)
        self.assertIn(f"chmod u+w {folder}", r.stderr)
        self.assertNotIn("sudo chown", r.stderr)

    def test_a_folder_it_cannot_make_is_said_not_a_traceback(self):
        # FLASH_LOCAL_PLE_DIR under a parent it cannot write: the copy ended in a PermissionError
        # traceback at every Load (found in review, 2026-10-20)
        parent = self.d / "locked"
        parent.mkdir()
        parent.chmod(0o555)
        try:
            r = self.run_it("copy", "--dir", str(parent / "copies"))
        finally:
            parent.chmod(0o755)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("could not be made", r.stderr)
        self.assertIn("FLASH_LOCAL_PLE_DIR", r.stderr)

    def test_each_copy_is_on_the_disk_before_it_takes_its_name(self):
        # a power cut between the rename and the write-back left a renamed copy empty or cut short,
        # which a start then read (found in review, 2026-10-20): the copy is synced, then renamed,
        # then its folder synced
        import sys as _sys
        from unittest import mock
        mod = load()
        folder = self.d / "copies"
        events = []
        real_fsync, real_rename = os.fsync, pathlib.Path.rename

        def fsync(fd):
            events.append(("fsync", os.readlink(f"/proc/self/fd/{fd}")))
            return real_fsync(fd)

        def rename(self_, target):
            events.append(("rename", str(self_)))
            return real_rename(self_, target)
        argv = ["ple-table.py", str(self.cache.root), NAME, REV, "--home", str(self.home), "--dir", str(folder), "copy"]
        with mock.patch.object(_sys, "argv", argv), mock.patch.object(mod.os, "fsync", fsync), \
                mock.patch.object(pathlib.Path, "rename", rename):
            mod.main()
        for sha in self.cache.table:
            part = str(folder / (sha + ".part"))
            synced = events.index(("fsync", part))
            renamed = events.index(("rename", part))
            self.assertLess(synced, renamed, events)
            self.assertEqual(events[renamed + 1], ("fsync", str(folder)), events)

    def test_no_snapshot_is_said_with_status_2(self):
        shutil.rmtree(self.cache.snap)
        r = self.run_it("plan")
        self.assertEqual(r.returncode, 2)
        self.assertIn("fetch the checkpoint first", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
