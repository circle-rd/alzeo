#!/usr/bin/env python3
"""The byte count of a checkpoint download (hf-progress.py), and how switch-model.sh wires it.

Through a 22-minute switch to a 135 GB checkpoint started from the cockpit, the Load window
said "Step 1 of 2" and read as frozen (2026-10-05): snapshot_download counts files, a 53 GB
safetensors the same as a 2 KB config. hf-progress.py counts bytes. These tests hold what
it counts (the revision's blobs, whole or on their way, under either name huggingface_hub
gives a file on its way), how it says it, and that it never stands between a switch and its
checkpoint: no sizes means one line and the download as before, and the download's own
failure still fails the switch. They need no huggingface_hub and no network: the Hub is a
stub module, the cache a temporary directory.
"""
import ast
import contextlib
import importlib.util
import os
import pathlib
import re
import sys
import tempfile
import threading
import time
import types
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SWITCH = (REPO / "switch-model.sh").read_text()


def load():
    spec = importlib.util.spec_from_file_location("hf_progress_under_test", REPO / "hf-progress.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HP = load()
SHA_A = "a" * 64      # an LFS file's blob: its sha256
SHA_B = "b" * 64
GIT_C = "c" * 40      # any other file's blob: its git blob id


def sib(name, size=None, blob_id=None, lfs=None):
    return types.SimpleNamespace(rfilename=name, size=size, blob_id=blob_id, lfs=lfs)


def lfs(sha, size):
    return types.SimpleNamespace(sha256=sha, size=size, pointer_size=134)


class WhatIsCounted(unittest.TestCase):
    def test_lfs_files_by_sha256_and_others_by_git_blob_id(self):
        sizes, unsized = HP.blob_sizes([sib("model.safetensors", 134, "p" * 40, lfs(SHA_A, 5_000)),
                                        sib("config.json", 700, GIT_C)])
        self.assertEqual(sizes, {SHA_A: 5_000, GIT_C: 700})
        self.assertEqual(unsized, 0)

    def test_the_dicts_older_releases_gave_are_read_too(self):
        sizes, _ = HP.blob_sizes([{"rfilename": "a", "size": 9, "blob_id": "x" * 40,
                                   "lfs": {"sha256": SHA_A, "size": 42}}])
        self.assertEqual(sizes, {SHA_A: 42})

    def test_one_content_in_two_files_is_one_blob(self):
        sizes, _ = HP.blob_sizes([sib("a.bin", 1, "p" * 40, lfs(SHA_A, 10)),
                                  sib("copy/a.bin", 1, "q" * 40, lfs(SHA_A, 10))])
        self.assertEqual(sizes, {SHA_A: 10})

    def test_a_file_without_a_size_is_counted_as_missing_not_as_zero(self):
        sizes, unsized = HP.blob_sizes([sib("a", None, GIT_C), sib("b", 3, None)])
        self.assertEqual((sizes, unsized), ({}, 2))


class WhatIsHere(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.blobs = pathlib.Path(self.tmp.name) / "blobs"
        self.blobs.mkdir()
        self.sizes = {SHA_A: 1000, SHA_B: 500, GIT_C: 20}

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, n, age=0.0):
        p = self.blobs / name
        p.write_bytes(b"x" * n)
        if age:
            t = time.time() - age
            os.utime(p, (t, t))

    def test_whole_blobs_count_whole(self):
        self.put(SHA_A, 1000)
        self.put(GIT_C, 20)
        self.assertEqual(HP.bytes_here(self.blobs, self.sizes), 1020)

    def test_a_file_on_its_way_counts_under_the_old_name(self):
        self.put(SHA_A + ".incomplete", 400)          # huggingface_hub 1.14 on this box
        self.assertEqual(HP.bytes_here(self.blobs, self.sizes), 400)

    def test_and_under_the_per_process_name_of_recent_releases(self):
        self.put(SHA_A + ".1f2e3d4c.incomplete", 400)  # 1.30 and 1.33, the pinned images
        self.assertEqual(HP.bytes_here(self.blobs, self.sizes), 400)

    def test_the_live_partial_is_the_one_written_last(self):
        self.put(SHA_A + ".deadbeef.incomplete", 900, age=3600)   # left by a killed fetch
        self.put(SHA_A + ".0badcafe.incomplete", 100)              # this fetch, restarted
        self.assertEqual(HP.bytes_here(self.blobs, self.sizes), 100)

    def test_a_whole_blob_wins_over_a_stale_partial(self):
        self.put(SHA_B, 500)
        self.put(SHA_B + ".incomplete", 300)
        self.assertEqual(HP.bytes_here(self.blobs, self.sizes), 500)

    def test_nothing_is_counted_past_the_size_the_hub_gave(self):
        self.put(SHA_B, 9999)
        self.assertEqual(HP.bytes_here(self.blobs, self.sizes), 500)

    def test_blobs_of_another_revision_are_not_this_ones(self):
        self.put("d" * 64, 5000)
        self.put("e" * 64 + ".incomplete", 5000)
        self.assertEqual(HP.bytes_here(self.blobs, self.sizes), 0)

    def test_a_cache_not_made_yet_holds_nothing(self):
        self.assertEqual(HP.bytes_here(self.blobs / "nope", self.sizes), 0)


class HowItIsSaid(unittest.TestCase):
    def test_a_line_with_rate_and_time_left(self):
        self.assertEqual(HP.progress_line(45_200_000_000, 126_000_000_000, 105_000_000),
                         "downloading: 45.2 of 126.0 GB (35 %), 105 MB/s, about 13 min left")

    def test_the_percentage_says_100_only_when_all_is_here(self):
        self.assertIn("(99 %)", HP.progress_line(999_999, 1_000_000, 1))
        self.assertIn("(100 %)", HP.progress_line(1_000_000, 1_000_000, 1))

    def test_no_rate_no_time_left(self):
        self.assertEqual(HP.progress_line(0, 10_000_000_000, 0), "downloading: 0.0 of 10.0 GB (0 %)")
        self.assertEqual(HP.progress_line(5, 10, -3), "downloading: 0.0 of 0.0 GB (50 %)")

    def test_times(self):
        self.assertEqual(HP.say_left(0.2), "about 1 s")
        self.assertEqual(HP.say_left(89), "about 89 s")
        self.assertEqual(HP.say_left(780), "about 13 min")
        self.assertEqual(HP.say_left(3900), "about 1 h 05")


def fake_hub(cache, siblings, fail=None):
    """A stub huggingface_hub with the four names reporting() imports."""
    hub = types.ModuleType("huggingface_hub")
    calls = []

    class HfApi:
        def model_info(self, repo, revision=None, files_metadata=False):
            calls.append((repo, revision, files_metadata))
            if fail:
                raise fail
            return types.SimpleNamespace(siblings=siblings)

    hub.HfApi = HfApi
    hub.constants = types.SimpleNamespace(HF_HUB_CACHE=str(cache))
    fd = types.ModuleType("huggingface_hub.file_download")
    fd.repo_folder_name = lambda *, repo_id, repo_type: f"{repo_type}s--" + repo_id.replace("/", "--")
    utils = types.ModuleType("huggingface_hub.utils")

    def filter_repo_objects(items, *, allow_patterns=None, ignore_patterns=None, key=None):
        import fnmatch
        for it in items:
            if allow_patterns is None or any(fnmatch.fnmatch(key(it), p) for p in allow_patterns):
                yield it
    utils.filter_repo_objects = filter_repo_objects
    return {"huggingface_hub": hub, "huggingface_hub.file_download": fd, "huggingface_hub.utils": utils}, calls


class ItReportsAndNeverStandsInTheWay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = pathlib.Path(self.tmp.name)
        self.blobs = self.cache / "models--org--model" / "blobs"
        self.siblings = [sib("model.safetensors", 134, "p" * 40, lfs(SHA_A, 3_000_000)),
                         sib("config.json", 500, GIT_C)]
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def stub(self, **kw):
        mods, calls = fake_hub(self.cache, self.siblings, **kw)
        return mock_modules(mods), calls

    def test_bytes_are_said_while_the_block_fetches(self):
        ctx, calls = self.stub()
        with ctx:
            with HP.reporting("org/model", revision="abc", every=0.05, say=self.lines.append):
                self.blobs.mkdir(parents=True)
                part = self.blobs / (SHA_A + ".0a1b2c3d.incomplete")
                for n in (1, 2, 3):
                    # grown by appending, as a download grows its .incomplete file: rewriting
                    # it whole truncated it to 0 first, and a read in between said 0 % after
                    # 66 % on a slow CI runner (2026-10-10)
                    with open(part, "ab") as f:
                        f.write(b"\0" * 1_000_000)
                    time.sleep(0.12)
                part.rename(self.blobs / SHA_A)
                (self.blobs / GIT_C).write_bytes(b"{}" * 250)
                time.sleep(0.12)
        self.assertEqual(calls, [("org/model", "abc", True)])
        self.assertEqual(self.lines[0], "checkpoint: 2 files, 0.0 GB at this revision, 0.0 GB already here")
        pcts = [int(re.search(r"\((\d+) %\)", ln).group(1)) for ln in self.lines if ln.startswith("downloading:")]
        self.assertEqual(pcts, sorted(pcts), self.lines)
        for seen in (33, 66, 100):
            self.assertIn(seen, pcts, self.lines)
        self.assertEqual(self.lines[-1], "checkpoint: 0.0 of 0.0 GB here")

    def test_allow_patterns_count_only_the_files_fetched(self):
        ctx, _ = self.stub()
        with ctx:
            with HP.reporting("org/model", allow_patterns=["*.json"], every=0.05, say=self.lines.append):
                time.sleep(0.08)
        self.assertEqual(self.lines[0], "checkpoint: 1 files, 0.0 GB at this revision, 0.0 GB already here")

    def test_no_sizes_is_one_line_and_the_download_as_before(self):
        ctx, _ = self.stub(fail=OSError("offline"))
        ran = []
        with ctx:
            with HP.reporting("org/model", every=0.05, say=self.lines.append):
                ran.append(True)
        self.assertEqual(ran, [True])
        self.assertEqual(self.lines, ["the Hub did not give this checkpoint's file sizes (OSError): "
                                      "the download goes on without a byte count"])

    def test_without_huggingface_hub_at_all_it_is_the_same_one_line(self):
        with mock_modules({"huggingface_hub": None}):
            with HP.reporting("org/model", say=self.lines.append):
                pass
        self.assertEqual(len(self.lines), 1)
        self.assertIn("the download goes on without a byte count", self.lines[0])

    def test_the_downloads_own_failure_still_fails_the_switch(self):
        ctx, _ = self.stub()
        with ctx, self.assertRaises(RuntimeError):
            with HP.reporting("org/model", every=0.05, say=self.lines.append):
                raise RuntimeError("the Hub went away")
        self.assertFalse([t for t in threading.enumerate() if t.name != "MainThread" and t.is_alive()
                          and t.daemon and "_watch" in repr(getattr(t, "_target", ""))])

    def test_a_broken_count_stops_its_lines_not_the_download(self):
        ctx, _ = self.stub()
        orig = HP.bytes_here
        HP.bytes_here = lambda *a: (_ for _ in ()).throw(PermissionError("no"))
        try:
            ran = []
            with ctx:
                with HP.reporting("org/model", every=0.05, say=self.lines.append):
                    time.sleep(0.1)
                    ran.append(True)
        finally:
            HP.bytes_here = orig
        self.assertEqual(ran, [True])
        self.assertEqual(self.lines, ["progress stopped (PermissionError); the download goes on"])


@contextlib.contextmanager
def mock_modules(mods):
    saved = {k: sys.modules.get(k, ...) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is ...:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


FETCH = (REPO / "ckpt-fetch.py").read_text()


def reporting_from(file):
    """ckpt-fetch.py's reporting(), as it runs from `file`: it loads hf-progress.py beside itself."""
    tree = ast.parse(FETCH)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "reporting")
    ns = {"__file__": str(file)}
    exec(compile("import contextlib, importlib.util, os\n" + ast.get_source_segment(FETCH, fn), "ckpt-fetch.py", "exec"), ns)
    return ns["reporting"]


class EveryFetchCountsItsBytes(unittest.TestCase):
    """ckpt-fetch.py, the one fetch of every lane (switch-model.sh's four and the installers'),
    counts its bytes, and keeps its download if the count cannot load."""

    def test_every_resumed_attempt_runs_inside_the_count(self):
        loop = FETCH.index("with reporting(a.repo, revision=a.rev, **kw):")
        self.assertLess(loop, FETCH.index("for attempt in range(1, ATTEMPTS + 1):"), "every resumed attempt is counted")

    def test_a_cache_that_answers_alone_asks_the_hub_nothing(self):
        self.assertLess(FETCH.index("local_files_only=True"), FETCH.index("with reporting("))

    def test_the_count_that_cannot_load_is_no_count(self):
        with tempfile.TemporaryDirectory() as d:
            cm = reporting_from(pathlib.Path(d) / "ckpt-fetch.py")("org/model")
        self.assertIsInstance(cm, contextlib.nullcontext)

    def test_the_count_loads_from_beside_the_script(self):
        cm = reporting_from(REPO / "ckpt-fetch.py")("org/model")
        self.assertNotIsInstance(cm, contextlib.nullcontext)

    def test_a_container_gets_the_file_only_when_it_is_there(self):
        # beside ckpt-fetch.py in /opt/qwen38, where the docker callers mount both
        self.assertIn('if [ -f "$REPO_DIR/hf-progress.py" ]; then', SWITCH)
        self.assertEqual(SWITCH.count('-v "$REPO_DIR/hf-progress.py:/opt/qwen38/hf-progress.py:ro")'), 5,
                         "the text lanes', Kolibri's, Nemotron's, Flash FP4-Local's and Ornith's")
        self.assertIn('"${DL_TOKEN_ARGS[@]}" "${DL_PROGRESS_ARGS[@]}"', SWITCH)
        self.assertNotIn("QWEN38_REPO_DIR", SWITCH, "the script finds it beside itself")
        install = (REPO / "install.sh").read_text()
        self.assertIn('[ -f "$REPO_DIR/hf-progress.py" ] && DL_PROGRESS_ARGS=(-v "$REPO_DIR/hf-progress.py:/opt/qwen38/hf-progress.py:ro")', install)

    def test_the_vllm_lanes_installers_mount_it_only_when_it_is_there_too(self):
        # docker -v of a host file that is not there makes a root-owned directory in its place,
        # here in the repository (found in review, 2026-10-14)
        # the three vLLM lanes' installers fetch through what they share, vllm-lane.sh
        text = (REPO / "vllm-lane.sh").read_text()
        self.assertIn('[ -f "$HERE/hf-progress.py" ] && progress_args=(-v "$HERE/hf-progress.py:/opt/qwen38/hf-progress.py:ro")', text)
        self.assertIn('-v "$HERE/ckpt-fetch.py:/opt/qwen38/ckpt-fetch.py:ro" "${progress_args[@]}"', text)
        self.assertEqual(text.count("hf-progress.py:/opt/qwen38/hf-progress.py"), 1, "one mount, the guarded one")
        for script in ("install-kolibri.sh", "install-nemotron.sh", "install-nemotron-super.sh", "install-flash-local.sh",
                       "install-ornith-122b.sh"):
            with self.subTest(script=script):
                inst = (REPO / script).read_text()
                self.assertNotIn("hf-progress.py", inst, "no mount of its own")
                self.assertIn('\nvlane_fetch "$MODEL" "$MODEL_REV"\n', inst)

    def test_bars_go_only_without_a_terminal(self):
        self.assertIn('DL_NO_BARS=""; [ -t 1 ] || DL_NO_BARS=1', SWITCH)
        # the text lanes', the image lane's, the video lane's, the Kolibri lane's, the Nemotron lane's,
        # the Qwen3.8 Flash-Next FP4-Local lane's and the Ornith lane's
        self.assertEqual(SWITCH.count('HF_HUB_DISABLE_PROGRESS_BARS="$DL_NO_BARS"'), 7, "all seven fetches")


@contextlib.contextmanager
def mock_env(env):
    saved = dict(os.environ)
    os.environ.pop("QWEN38_REPO_DIR", None)
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
