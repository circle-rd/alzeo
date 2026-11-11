#!/usr/bin/env python3
"""A vLLM lane's room check counts the checkpoints in the unit df counts free space in.

vllm-lane.sh's vlane_room took the checkpoints' share in GB of 10^9 bytes, added the image's
in GiB, and held the sum against df -BG, which counts GiB: a Kolibri-1 install (79 GB of
checkpoint, 32 GiB for the image's pull) asked for 111 GiB free where 106 hold it, and the
Nemotron lanes the same five GiB too many (found in review, 2026-10-16). Every other room check
of the repo counts in GiB (install.sh, install-image.sh, install-video.sh, lanes.sh). The
function runs here as the installers source it, against a fake cache, df and docker.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
GIB = 1024 ** 3


def run(weights_gb, image_gb, free_gib, cached_bytes=0, mode="full", image_here=False):
    d = pathlib.Path(tempfile.mkdtemp(prefix="vlane-room-"))
    blobs = d / "hf/hub/models--org--model/blobs"
    blobs.mkdir(parents=True)
    (d / "docker-root").mkdir()
    if cached_bytes:
        with open(blobs / "b", "wb") as f:
            f.truncate(cached_bytes)
    (d / "bin").mkdir()
    (d / "bin/df").write_text(f"#!/bin/sh\necho Avail; echo {free_gib}G\n")
    (d / "bin/docker").write_text(
        "#!/bin/sh\n"
        f'[ "$1" = info ] && {{ echo "{d}/docker-root"; exit 0; }}\n'
        f'[ "$1 $2" = "image inspect" ] && exit {0 if image_here else 1}\n'
        "exit 1\n")
    for f in ("df", "docker"):
        (d / "bin" / f).chmod(0o755)
    script = ('set -euo pipefail\ndie(){ echo "DIE: $*"; exit 1; }\n'
              f'. "{REPO}/lanes.sh"\n. "{REPO}/vllm-lane.sh"\n'
              f'HF_CACHE="{d}/hf"; WEIGHTS_GB={weights_gb}; IMAGE_GB={image_gb}; LOCAL_IMAGE=local:tag\n'
              f'MODE={mode}; VLANE=kolibri; ENGINE_BIND=127.0.0.1; PORT=30000\n'
              'vlane_room base:tag org/model\n'
              'echo "NEED weights=$WEIGHTS_NEED image=$IMAGE_NEED"\necho ROOM-OK\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{d}/bin:/usr/bin:/bin", "HOME": str(d)})
    return r.stdout + r.stderr


class AVllmLaneCountsItsRoomInOneUnit(unittest.TestCase):
    def test_the_checkpoints_share_is_in_gib_rounded_up(self):
        # 79 GB is 73.6 GiB: 74 once rounded up, never 79
        self.assertIn("NEED weights=74 image=32", run(79, 32, 0, mode="units"))
        # Bolt's 26 GB and Super VL's 77
        self.assertIn("NEED weights=25 image=35", run(26, 35, 0, mode="units"))
        self.assertIn("NEED weights=72 image=35", run(77, 35, 0, mode="units"))

    def test_the_free_space_that_holds_the_install_is_enough(self):
        # 74 + 32 GiB on one disk: 106 GiB free holds it, 105 does not
        out = run(79, 32, 106)
        self.assertIn("ROOM-OK", out, out)
        out = run(79, 32, 105)
        self.assertIn("DIE: 105 GiB free on the disk of", out)
        self.assertIn("this needs about 106 GiB (74 for the checkpoint, 32 for the image)", out)

    def test_what_the_cache_holds_comes_off_by_bytes(self):
        # 40 GB of the 79 already there: 39 GB, 36.3 GiB, so 37 left to fetch
        self.assertIn("NEED weights=37 image=32", run(79, 32, 0, cached_bytes=40_000_000_000, mode="units"))
        # all of it there: nothing to fetch, whatever the rounding
        self.assertIn("NEED weights=0 image=32", run(79, 32, 0, cached_bytes=79_000_000_000, mode="units"))
        self.assertIn("NEED weights=0 image=32", run(79, 32, 0, cached_bytes=80 * GIB, mode="units"))

    def test_the_line_that_says_it_fits_gives_the_download_in_gb(self):
        # the check counts 74 GiB, and the line still says what is downloaded: 79 GB, 39 of 40 cached
        self.assertIn("checkpoint about 79 GB to download into", run(79, 32, 106))
        self.assertIn("checkpoint about 39 GB to download into", run(79, 32, 106, cached_bytes=40_000_000_000))
        self.assertIn("checkpoint already cached into", run(79, 32, 106, cached_bytes=79_000_000_000))

    def test_an_image_already_here_needs_no_room(self):
        out = run(79, 32, 74, image_here=True)
        self.assertIn("NEED weights=74 image=0", out)
        self.assertIn("ROOM-OK", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
