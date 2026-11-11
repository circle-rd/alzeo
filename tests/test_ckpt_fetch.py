#!/usr/bin/env python3
"""ckpt-fetch.py, the one fetch of every lane's checkpoint, run whole against a fake hub.

Seven copies before it had drifted (found in review, 2026-10-14): only Kolibri's checked that
every shard an index names is in the cache before trusting a snapshot, a switch to the image or
video lane gave up at the first network error, the room was GiB in one copy and GB in another,
and the Xet exception for the one file over the CDN's limit lived in the text lanes' copies
alone. Each behaviour is held here once; tests/test_one_fetch.py holds that every caller runs
this script and none keeps a fetch of its own.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
FETCH = REPO / "ckpt-fetch.py"
SHA = "4" * 40
OTHER = "5" * 40

# The hub's calls, written down; the online path completes the snapshot from FAKE_FILES, after
# FAKE_FAILS transient errors, and refuses a file over the CDN's limit while Xet is off when
# FAKE_TOO_LARGE is set.
FAKE_HUB = '''
import json, os, pathlib


class _Constants:
    HF_HUB_CACHE = os.path.join(os.environ["HF_HOME"], "hub")
    HF_HUB_DISABLE_XET = True


constants = _Constants()


def snapshot_download(repo, revision=None, local_files_only=False, allow_patterns=None, **kw):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps({"repo": repo, "rev": revision, "local": local_files_only,
                            "allow": allow_patterns, "xet_off": constants.HF_HUB_DISABLE_XET}) + "\\n")
    snap = pathlib.Path(os.environ["SNAP"])
    if local_files_only:
        if not snap.exists():
            raise FileNotFoundError(repo)
        return str(snap)
    count = pathlib.Path(os.environ["FAKE_LOG"] + ".fails")
    n = int(count.read_text()) if count.exists() else 0
    if n < int(os.environ.get("FAKE_FAILS", "0")):
        count.write_text(str(n + 1))
        raise ConnectionError("reset by peer")
    if os.environ.get("FAKE_TOO_LARGE") and constants.HF_HUB_DISABLE_XET:
        raise ValueError("The file is too large to be downloaded using the regular download method. Use hf_xet.")
    snap.mkdir(parents=True, exist_ok=True)
    for name in json.loads(os.environ.get("FAKE_FILES", "[]")):
        (snap / name).parent.mkdir(parents=True, exist_ok=True)
        if (snap / name).is_symlink():   # a link to a blob that is gone: the hub replaces it
            (snap / name).unlink()
        (snap / name).write_text("w")
    return str(snap)
'''
# no 10 s pause between two attempts in a test: the interpreter of the fake loads this first
SITECUSTOMIZE = "import time\ntime.sleep = lambda s: None\n"


class Box:
    def __init__(self, files=(), index=None, cached=True, fails=0, too_large=False, online_files=None,
                 refs=None, blobs_gib=0, blobs_bytes=0):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="ckpt-fetch-"))
        (self.d / "py" / "huggingface_hub").mkdir(parents=True)
        (self.d / "py" / "huggingface_hub" / "__init__.py").write_text(FAKE_HUB)
        (self.d / "py" / "sitecustomize.py").write_text(SITECUSTOMIZE)
        self.cache = self.d / "hub" / "models--org--model"
        self.snap = self.cache / "snapshots" / SHA
        if cached:
            self.snap.mkdir(parents=True)
            if index is not None:
                (self.snap / "model.safetensors.index.json").write_text(json.dumps({"weight_map": index}))
            for name in files:
                (self.snap / name).parent.mkdir(parents=True, exist_ok=True)
                (self.snap / name).write_text("w")
        if refs is not None:
            (self.cache / "refs").mkdir(parents=True, exist_ok=True)
            (self.cache / "refs" / "main").write_text(refs)
        if blobs_gib or blobs_bytes:
            (self.cache / "blobs").mkdir(parents=True, exist_ok=True)
            with open(self.cache / "blobs" / ("0" * 64 + ".incomplete"), "wb") as f:
                f.truncate(blobs_gib * 2**30 + blobs_bytes)
        self.log = self.d / "calls.log"
        self.env = dict(os.environ, PYTHONPATH=str(self.d / "py"), HF_HOME=str(self.d), FAKE_LOG=str(self.log),
                        SNAP=str(self.snap), FAKE_FAILS=str(fails),
                        FAKE_FILES=json.dumps(list(online_files if online_files is not None else files or ["model.safetensors"])))
        if too_large:
            self.env["FAKE_TOO_LARGE"] = "1"

    def run(self, *args, rev=SHA):
        r = subprocess.run([sys.executable, str(FETCH), "org/model", rev, *args], capture_output=True, text=True,
                           env=self.env, timeout=120)
        calls = [json.loads(ln) for ln in self.log.read_text().splitlines()] if self.log.exists() else []
        return r.returncode, r.stdout, r.stderr, calls

    def ref(self):
        p = self.cache / "refs" / "main"
        return p.read_text() if p.exists() else None


INDEX = {"a.weight": "model-00001-of-00002.safetensors", "b.weight": "model-00002-of-00002.safetensors"}
SHARDS = list(INDEX.values())


class OfflineFirst(unittest.TestCase):
    def test_a_whole_snapshot_answers_without_the_network(self):
        b = Box(SHARDS, INDEX)
        rc, out, err, calls = b.run("--offline-first")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c["local"] for c in calls], [True])
        self.assertEqual(out.splitlines()[-1], str(b.snap), "the snapshot's path, last")
        self.assertNotIn("fetching", out)

    def test_a_snapshot_missing_a_shard_is_fetched(self):
        rc, out, err, calls = Box(SHARDS[:1], INDEX, online_files=SHARDS).run("--offline-first")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c["local"] for c in calls], [True, False])
        self.assertIn("not complete in the cache, fetching the rest", out)

    def test_a_shard_whose_blob_is_gone_is_missing(self):
        b = Box(SHARDS[:1], INDEX, online_files=SHARDS)
        (b.snap / SHARDS[1]).symlink_to(b.d / "blobs-gone" / "x")
        rc, _, err, calls = b.run("--offline-first")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c["local"] for c in calls], [True, False])

    def test_an_index_in_a_component_is_read_too(self):
        # a diffusion model: the transformer and the text encoder each have their own index
        idx = {"w": "diffusion_pytorch_model-00001-of-00001.safetensors"}
        b = Box(["vae/diffusion_pytorch_model.safetensors"], online_files=[
            "vae/diffusion_pytorch_model.safetensors", "transformer/diffusion_pytorch_model-00001-of-00001.safetensors"])
        (b.snap / "transformer").mkdir()
        (b.snap / "transformer" / "diffusion_pytorch_model.safetensors.index.json").write_text(json.dumps({"weight_map": idx}))
        rc, _, err, calls = b.run("--offline-first")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c["local"] for c in calls], [True, False], "the transformer's shard is missing")
        rc, _, err, calls = b.run("--offline-first")
        self.assertEqual([c["local"] for c in calls[2:]], [True], "now whole")

    def test_only_what_the_allow_patterns_cover_counts(self):
        # the reference box's MiniMax-H3: a full pull once began Ref2VA (its transformer index is
        # there, none of its 13 shards), and the lane serves the whole FL2VA partition
        idx = {"w": "model-00001-of-00001.safetensors"}
        b = Box(["FL2VA/transformer/model-00001-of-00001.safetensors"], online_files=[])
        for part in ("FL2VA", "Ref2VA"):
            (b.snap / part / "transformer").mkdir(parents=True, exist_ok=True)
            (b.snap / part / "transformer" / "model.safetensors.index.json").write_text(json.dumps({"weight_map": idx}))
        rc, out, err, calls = b.run("--offline-first", "--allow", "FL2VA/* model_index.json *.md")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c["local"] for c in calls], [True], "the partition served is whole")
        rc, out, err, calls = b.run("--offline-first")
        self.assertEqual([c["local"] for c in calls[1:]], [True, False], "the whole repo is not")

    def test_a_weights_file_outside_the_patterns_does_not_make_a_snapshot_whole(self):
        # no index the patterns cover, and a stray single file of another partition
        b = Box(["Ref2VA/vae/diffusion_pytorch_model.safetensors"], online_files=["FL2VA/vae/diffusion_pytorch_model.safetensors"])
        rc, _, err, calls = b.run("--offline-first", "--allow", "FL2VA/*")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c["local"] for c in calls], [True, False])

    def test_a_folder_pattern_covers_its_content(self):
        b = Box(["sub/model.safetensors"])
        rc, _, err, calls = b.run("--offline-first", "--allow", "sub/")
        self.assertEqual([c["local"] for c in calls], [True])

    def test_a_single_file_checkpoint_needs_its_file(self):
        rc, _, err, calls = Box([], online_files=["model.safetensors"]).run("--offline-first")
        self.assertEqual([c["local"] for c in calls], [True, False])
        rc, _, err, calls = Box(["model.safetensors"]).run("--offline-first")
        self.assertEqual([c["local"] for c in calls], [True])

    def test_nothing_in_the_cache_is_fetched(self):
        rc, out, err, calls = Box(cached=False, online_files=["model.safetensors"]).run("--offline-first")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c["local"] for c in calls], [True, False])


class Online(unittest.TestCase):
    def test_without_offline_first_the_hub_is_asked(self):
        # the installers: the Hub finds and fetches any missing file, the index or not
        rc, out, err, calls = Box(SHARDS, INDEX).run()
        self.assertEqual(rc, 0, err)
        self.assertEqual([c["local"] for c in calls], [False])
        self.assertNotIn("not complete", out)

    def test_the_allow_patterns_go_to_both_asks(self):
        rc, _, err, calls = Box([], online_files=["FL2VA/x.safetensors"]).run("--offline-first", "--allow", "FL2VA/* *.md")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c["allow"] for c in calls], [["FL2VA/*", "*.md"]] * 2)
        rc, _, err, calls = Box(["model.safetensors"]).run()
        self.assertEqual(calls[0]["allow"], None, "no pattern: the whole repo")


class Resumes(unittest.TestCase):
    def test_four_interruptions_then_the_fetch(self):
        rc, out, err, calls = Box(cached=False, fails=4).run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(calls), 5)
        self.assertIn("download interrupted (ConnectionError), resuming (4/5)...", out)

    def test_five_interruptions_fail(self):
        rc, out, err, calls = Box(cached=False, fails=5).run()
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(calls), 5)
        self.assertIn("ConnectionError", err)

    def test_a_switch_resumes_too(self):
        # the image and video switches gave up at the first error
        rc, out, err, calls = Box(cached=False, fails=2).run("--offline-first")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c["local"] for c in calls], [True, False, False, False])


class TheXetException(unittest.TestCase):
    def test_a_file_over_the_cdn_limit_is_fetched_with_xet(self):
        # (that it goes off again after: test_install_xet_retry, which reads the switch in-process)
        rc, out, err, calls = Box(cached=False, too_large=True).run()
        self.assertEqual(rc, 0, err)
        self.assertIn("a file is over the classic CDN limit; retrying this repo with Xet", out)
        self.assertEqual([c["xet_off"] for c in calls], [True, False])

    def test_any_other_error_is_not_retried_with_xet(self):
        rc, out, err, calls = Box(cached=False, fails=1).run()
        self.assertEqual(rc, 0, err)
        self.assertTrue(all(c["xet_off"] for c in calls))
        self.assertNotIn("Xet", out)


class Room(unittest.TestCase):
    def test_no_room_stops_before_the_first_byte(self):
        rc, out, err, calls = Box(cached=False).run("--offline-first", "--need-gb", "34", "--free-gb", "10")
        self.assertNotEqual(rc, 0)
        self.assertIn("10 GiB free in the cache, and org/model needs about 34 GB more: free some space first", err)
        self.assertEqual([c["local"] for c in calls], [True], "nothing asked of the network")

    def test_what_the_cache_holds_comes_off_the_need(self):
        # counted in bytes: the need in GB (10^9), the free side in df -BG's GiB; 30 GiB held of
        # 34 GB leave 1.79 GB to fetch
        rc, out, err, calls = Box(cached=False, blobs_gib=30).run("--offline-first", "--need-gb", "34", "--free-gb", "1")
        self.assertIn("needs about 2 GB more", err)
        rc, out, err, calls = Box(cached=False, blobs_gib=30).run("--offline-first", "--need-gb", "34", "--free-gb", "2")
        self.assertEqual(rc, 0, err)

    def test_kolibris_partial_fetch_is_charged_what_it_lacks(self):
        # a Kolibri FP8 fetch (79 GB) stopped at 70 GB: 9 GB remain, which 12 GiB free hold; GiB
        # held taken off GB needed charged it 14 (found in review, 2026-10-14)
        rc, out, err, calls = Box(cached=False, blobs_bytes=70 * 10**9).run(
            "--offline-first", "--need-gb", "79", "--free-gb", "12")
        self.assertEqual(rc, 0, err)
        rc, out, err, calls = Box(cached=False, blobs_bytes=70 * 10**9).run(
            "--offline-first", "--need-gb", "79", "--free-gb", "8")
        self.assertIn("8 GiB free in the cache, and org/model needs about 9 GB more", err)

    def test_a_free_space_df_could_not_say_checks_nothing(self):
        # dl_free_gb answers "" when df cannot: the copies before took that as no check
        rc, out, err, calls = Box(cached=False).run("--offline-first", "--need-gb", "34", "--free-gb", "")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c["local"] for c in calls], [True, False])

    def test_a_whole_snapshot_needs_no_room(self):
        rc, out, err, calls = Box(["model.safetensors"]).run("--offline-first", "--need-gb", "34", "--free-gb", "1")
        self.assertEqual(rc, 0, err)


class RefsMain(unittest.TestCase):
    def test_pin_points_it_at_the_commit_fetched(self):
        for old in (None, OTHER, "garbage"):
            with self.subTest(old=old):
                b = Box(["model.safetensors"], refs=old)
                rc, out, err, _ = b.run("--refs-main", "pin")
                self.assertEqual(rc, 0, err)
                self.assertEqual(b.ref(), SHA)
                self.assertIn(f"refs/main -> {SHA[:12]}", out)

    def test_pin_moves_nothing_for_a_branch(self):
        b = Box(["model.safetensors"], refs=OTHER)
        rc, out, err, _ = b.run("--refs-main", "pin", rev="main")
        self.assertEqual(rc, 0, err)
        self.assertEqual(b.ref(), OTHER)

    def test_fill_writes_a_missing_or_broken_ref_and_keeps_a_commit(self):
        for old, want in ((None, SHA), ("garbage", SHA), (OTHER, OTHER)):
            with self.subTest(old=old):
                b = Box(["model.safetensors"], refs=old)
                rc, out, err, _ = b.run("--offline-first", "--refs-main", "fill")
                self.assertEqual(rc, 0, err)
                self.assertEqual(b.ref(), want)

    def test_without_the_option_refs_is_left_alone(self):
        b = Box(["model.safetensors"])
        rc, _, err, _ = b.run("--offline-first")
        self.assertEqual(rc, 0, err)
        self.assertIsNone(b.ref())


if __name__ == "__main__":
    unittest.main()
