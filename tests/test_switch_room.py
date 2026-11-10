#!/usr/bin/env python3
"""A switch checks for room before it downloads, and the cockpit gives it the time to.

switch-model.sh can download a whole checkpoint (22 to 136 GB), and a cockpit button can
start it: it checked no free space, blamed HF_TOKEN for any failed download, and the
cockpit's switch job timed out at 30 min, when a flash download alone takes about 23 at
the reference box's 89 MB/s (found in review, 2026-09-24). The switch's own lines run
here against a fake cache and a df stub.
"""
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "switch-model.sh").read_text()
START = TEXT.index('DL_NEED_GB="$(ckpt_room_gb ')
BLOCK = TEXT[START:TEXT.index("DL_TOKEN_ARGS=()", START)]

# the room checks read free space through one function, defined once before every
# branch runs; every replay carries it in front of the block it exercises
_lines = TEXT.splitlines(keepends=True)
_at = next(i for i, ln in enumerate(_lines) if ln.startswith("dl_free_gb(){"))
DLFREE = "".join(_lines[_at:_at + 2])


def run(lane, free_gb, cached_gib=0.0, whole=False):
    d = pathlib.Path(tempfile.mkdtemp(prefix="sw-room-"))
    blobs = d / "hf/hub/models--org--target/blobs"
    blobs.mkdir(parents=True)
    if cached_gib:
        with open(blobs / "b", "wb") as f:
            f.truncate(int(cached_gib * 1024 ** 3))
    if whole:                                    # the target's revision whole in the cache
        (d / "hf/hub/models--org--target/snapshots/abc123").mkdir(parents=True)
        (d / "hf/hub/models--org--target/snapshots/abc123/model.safetensors").write_text("w")
    (d / "df").write_text(f"#!/bin/sh\necho Avail; echo {free_gb}G\n")
    (d / "df").chmod(0o755)
    script = ('set -euo pipefail\ndie(){ echo "DIE: $*"; exit 1; }\n'
              f'HF_CACHE="{d}/hf"; TARGET_REPO=org/target; TARGET_REV=abc123; TARGET_LANE={lane}\n. "{REPO}/lanes.sh"\n'
              + DLFREE + BLOCK + "echo ROOM-OK\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{d}:/usr/bin:/bin"})
    return r.stdout + r.stderr


class ASwitchChecksForRoom(unittest.TestCase):
    def test_a_flash_switch_without_room_stops_before_anything(self):
        out = run("flash", free_gb=50)
        self.assertIn("DIE: not enough room", out)
        self.assertNotIn("ROOM-OK", out)

    def test_what_is_cached_comes_off_the_need(self):
        self.assertIn("ROOM-OK", run("flash", free_gb=60, cached_gib=126))

    def test_a_checkpoint_already_whole_asks_its_caches_only(self):
        # 10, as install.sh asks: it was 180 less what the blobs hold (54 for a whole flash
        # checkpoint, found in review, 2026-10-15)
        self.assertIn("ROOM-OK", run("flash", 10, whole=True))
        self.assertIn("DIE: not enough room", run("flash", 9, whole=True))

    def test_a_27b_switch_with_room_goes_on(self):
        self.assertIn("ROOM-OK", run("27b", free_gb=100))


VID_START = TEXT.index('VID_WANT_GB="$(grep')
VID_END = TEXT.index('nothing was changed). Free some space first, or point HF_HOME at a bigger disk."', VID_START)
VID_BLOCK = TEXT[VID_START:TEXT.index("\n", VID_END) + 1]


def run_video(free_gb, cached_gib=0.0, caches_there=False, home_disk_gb=None, repo_dir=REPO):
    """home_disk_gb: $HOME on another disk than the cache, with that free (df and stat stubs); repo_dir:
    where the switch reads install-video.sh."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="sw-room-"))
    blobs = d / "hf/hub/models--MiniMaxAI--MiniMax-H3/blobs"
    blobs.mkdir(parents=True)
    if cached_gib:
        with open(blobs / "b", "wb") as f:
            f.truncate(int(cached_gib * 1024 ** 3))
    # the two caches the runtime writes at its first start, already written (lanes.sh)
    if caches_there:
        for sub, gib in (("sglang/diffusion/host_spill", 12), ("sgl_diffusion/decode_dtype_store", 5)):
            (d / "home/.cache" / sub).mkdir(parents=True)
            with open(d / "home/.cache" / sub / "w.bin", "wb") as f:
                f.truncate(gib * 1024 ** 3)
    (d / "df").write_text(f"#!/bin/sh\necho Avail; echo {free_gb}G\n")
    if home_disk_gb is not None:
        (d / "home").mkdir(exist_ok=True)
        (d / "df").write_text('#!/bin/bash\necho Avail\ncase "${@: -1}" in ' + f'"{d}/home"*) echo {home_disk_gb}G ;; *) echo {free_gb}G ;; esac\n')
        (d / "stat").write_text('#!/bin/bash\ncase "${@: -1}" in ' + f'"{d}/home"*) echo 2 ;; *) echo 1 ;; esac\n')
        (d / "stat").chmod(0o755)
    (d / "df").chmod(0o755)
    script = ('set -euo pipefail\ndie(){ echo "DIE: $*"; exit 1; }\n'
              f'REPO_DIR="{repo_dir}"; HF_CACHE="{d}/hf"; HOME="{d}/home"; VID_MODEL=MiniMaxAI/MiniMax-H3; '
              f'VID_MODEL_DIR="{d}/hf/hub/models--MiniMaxAI--MiniMax-H3"\n. "{REPO}/lanes.sh"\n'
              + DLFREE + VID_BLOCK + 'echo "NEED=$VID_WANT_GB"; echo ROOM-OK\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{d}:/usr/bin:/bin"})
    return r.stdout + r.stderr


class TheVideoSwitchChecksForRoomToo(unittest.TestCase):
    """145 GB of fl2va weights behind a button, under a serving engine: the video
    branch skipped the room check the text branch had learned (reviews, 2026-09-28).
    The need is read from install-video.sh, so the two files drift together."""

    def test_a_video_switch_without_room_stops_before_anything(self):
        # this box's real numbers of 2026-09-28: 90 GB free, empty cache, 145 needed
        out = run_video(free_gb=90)
        self.assertIn("DIE: not enough room", out)
        self.assertNotIn("ROOM-OK", out)

    def test_the_complete_cache_of_a_real_box_answers_without_a_die(self):
        self.assertIn("ROOM-OK", run_video(free_gb=90, cached_gib=135))

    def test_a_video_switch_with_room_goes_on(self):
        self.assertIn("ROOM-OK", run_video(free_gb=200))

    def test_the_caches_of_its_first_start_count_with_the_checkpoint(self):
        # 150 GB free, nothing cached: 135 for the checkpoint passed, and its first start then
        # wrote 17 more onto a disk with 15 left (found in review, 2026-10-15); written already,
        # they take nothing more
        out = run_video(free_gb=150)
        self.assertIn("DIE: not enough room", out)
        self.assertIn("12 for the runtime's transformed weights, 5 for the decoder's converted weights", out)
        out = run_video(free_gb=150, caches_there=True)
        self.assertIn("NEED=135", out)
        self.assertIn("ROOM-OK", out)

    def test_the_caches_on_another_disk_are_summed_there(self):
        # $HOME on its own disk with 15 GB free: 12 and 5 passed one by one (found in review,
        # 2026-10-15)
        out = run_video(free_gb=200, home_disk_gb=16)
        self.assertIn("DIE: not enough room for the video lane's first start", out)
        self.assertIn("about 17 GB needed", out)
        # each folder named, not the last one alone (found in review, 2026-10-15)
        self.assertIn("/home/.cache/sglang/diffusion/host_spill and ", out)
        self.assertIn("/home/.cache/sgl_diffusion/decode_dtype_store: 16 GB free", out)
        self.assertIn("NEED=135", run_video(free_gb=200, home_disk_gb=17))

    def test_without_install_video_sh_it_asks_the_same_room(self):
        # its fallback, when install-video.sh or its WEIGHTS_GB line cannot be read, is that number in
        # GiB: it said 145, the GB install-video.sh counted before v1.20.0 (found 2026-11-10)
        want = re.search(r"^WEIGHTS_GB=(\d+);", (REPO / "install-video.sh").read_text(), re.M).group(1)
        with tempfile.TemporaryDirectory(prefix="sw-room-norepo-") as nowhere:
            out = run_video(free_gb=200, caches_there=True, repo_dir=nowhere)
        self.assertIn("install-video.sh: No such file", out, "the switch did not look where it was pointed")
        self.assertIn(f"NEED={want}", out)
        self.assertIn("ROOM-OK", out)


class TheCockpitGivesItTheTime(unittest.TestCase):
    def test_the_switch_job_outlives_a_flash_download(self):
        cockpit = (REPO / "dashboard/cockpit.py").read_text()
        block = cockpit[cockpit.index('"switch": {'):]
        timeout = int(re.search(r'"timeout":\s*(\d+)', block).group(1))
        self.assertGreaterEqual(timeout, 2 * 3600, "124 GB at 89 MB/s is 23 min, at 20 MB/s 1 h 45")


if __name__ == "__main__":
    unittest.main(verbosity=2)
