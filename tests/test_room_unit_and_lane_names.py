#!/usr/bin/env python3
"""A room message says the unit it counts in, and a vLLM lane is named by its official name (v1.46.3).

Every room check of the installers and the switch holds what `df -BG` prints, which is GiB, against a need
counted in GiB, and said both in "GB": "Need ~45 GB free ...; found 44 GB" on a disk with 47.2 GB free, where
the README gives the same check as 49 GB (v1.46.1 gave each size one figure in its unit, and left these
messages; found on 2026-11-10 by the review of that version). They say GiB now. ple-table.py divides bytes by
10^9 and keeps its GB.

The vLLM lanes' installers named their lane in a sentence by a short name (VLANE_SHORT: "Kolibri", "Nemotron",
"Super VL") where every other line of a run says the official one (decision 1180 of 2026-11-07, lane_title),
and check-pins.sh headed their pins "Kolibri lane", "Nemotron lane" and "Nemotron Super lane".

Run: python3 tests/test_room_unit_and_lane_names.py
"""
import importlib.util
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]

# what a room message said of a figure df -BG gave, or of a need held against one
GIB_SAID_GB = re.compile(r"\bGB free\b|\bGB left under\b|\bfound \$\{[A-Za-z_]+:-(?:unknown|\?)\} GB\b"
                         r"|\bGB needed \(|\bneeds? about \$[{(A-Za-z_]+[^ ]* GB\b"
                         r"|\$\{?[A-Za-z_]*(?:NEED|WANT)[A-Za-z_]*\}? GB\b|IMG_LABEL=\"\d+ GB\b")


def registry():
    spec = importlib.util.spec_from_file_location("lane_registry", REPO / "lane_registry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def code_lines(path):
    """(number, line) of the lines of a shell script that are not comments."""
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield n, line


class ARoomMessageSaysGiB(unittest.TestCase):
    def test_the_rule_reads_the_messages_it_is_for(self):
        """The phrases this file looks for are the ones the scripts wrote: each is found in its old wording, and
        none in its new one. A rule that matches nothing proves nothing."""
        old = ['die "${FREE_W:-?} GB free under HF_CACHE=$HF_CACHE, the checkpoint needs about $WEIGHTS_NEED GB more."',
               'die "Need ~${NEED_GB} GB free for the checkpoints; found ${FREE_DISK_GB:-unknown} GB. Free some space"',
               'die "not enough room: ${DL_FREE_GB:-?} GB free, about $DL_NEED_GB GB needed (nothing was changed)."',
               'die "download failed ($(dl_free_gb) GB left under $HF_CACHE; re-run to resume)"',
               'die "${free_d:-?} GiB free under $DOCKER_ROOT, the image needs about $IMAGE_NEED GB."',
               'VID_WHAT="about $VID_WANT_GB GB for the checkpoint"',
               'NEED_GB=45; DOCKER_NEED_GB=48; IMG_LABEL="31 GB Docker image"',
               """|| echo "$WEIGHTS_NEED GB to download into $HF_CACHE")"."""]
        for line in old:
            self.assertRegex(line, GIB_SAID_GB)
        new = ['die "${FREE_W:-?} GiB free under HF_CACHE=$HF_CACHE, the checkpoint needs about $WEIGHTS_NEED GiB more."',
               'die "Need ~${NEED_GB} GiB free for the checkpoints; found ${FREE_DISK_GB:-unknown} GiB. Free some space"',
               'die "not enough room: ${DL_FREE_GB:-?} GiB free, about $DL_NEED_GB GiB needed (nothing was changed)."',
               'echo "about $dl_gb GB to download"',
               'VID_WHAT="about $VID_WANT_GB GiB for the checkpoint"',
               'NEED_GB=45; DOCKER_NEED_GB=48; IMG_LABEL="31 GiB Docker image"',
               """|| echo "$WEIGHTS_NEED GiB to download into $HF_CACHE")"."""]
        for line in new:
            self.assertNotRegex(line, GIB_SAID_GB)

    def test_no_script_says_gb_of_what_df_counts_in_gib(self):
        said = [f"{path.name}:{n}: {line.strip()[:120]}" for path in sorted(REPO.glob("*.sh"))
                for n, line in code_lines(path) if GIB_SAID_GB.search(line)]
        self.assertEqual(said, [])

    def test_the_scripts_still_have_their_room_messages(self):
        """The rule above passes on a script with no room message at all: these still have theirs, in GiB."""
        for name, least in (("install.sh", 5), ("install-image.sh", 3), ("install-video.sh", 1), ("install-speech.sh", 1),
                            ("vllm-lane.sh", 3), ("lanes.sh", 1), ("prepare-lane.sh", 1), ("switch-model.sh", 4)):
            n = sum(len(re.findall(r"\bGiB (?:free|left under)\b", line)) for _, line in code_lines(REPO / name))
            self.assertGreaterEqual(n, least, name)

    def test_the_fetch_says_gib_of_the_free_side_and_gb_of_the_need(self):
        """ckpt-fetch.py is handed df -BG's figure (--free-gb) and a need in GB of 10^9 bytes (--need-gb), which
        its room() compares in bytes: each side is said in its own unit."""
        text = (REPO / "ckpt-fetch.py").read_text()
        self.assertIn('f"{free_gb} GiB free in the cache, and {repo} needs about {-(-rest // 10**9)} GB more: "', text)
        self.assertNotIn("GB free in the cache", text.replace("GiB free in the cache", ""))


class AVllmLaneIsNamedOfficially(unittest.TestCase):
    def test_each_installer_names_its_lane_by_its_title(self):
        reg = registry()
        lanes = reg.lanes(engine="vllm")
        self.assertGreaterEqual(len(lanes), 5)
        for lane in lanes:
            text = (REPO / reg.LANES[lane]["installer"]).read_text()
            m = re.search(r'^VLANE=([a-z0-9-]+); VLANE_SHORT=("[^"\n]*"|\S+)$', text, re.M)
            self.assertIsNotNone(m, lane)
            self.assertEqual((m.group(1), m.group(2).strip('"')), (lane, reg.LANES[lane]["title"]))

    def test_the_first_step_of_an_installer_names_no_short_name(self):
        """ "1/5 Preflight (Bolt: ...", "(Super VL: ..." and "(FP4-Local: ..." were the three short names left."""
        reg = registry()
        for lane in ("nemotron", "nemotron-super", "flash-local", "ornith-122b"):
            text = (REPO / reg.LANES[lane]["installer"]).read_text()
            self.assertIn(f'step "1/5 Preflight ({reg.LANES[lane]["title"]}: $MODEL', text, lane)

    def test_check_pins_heads_each_vllm_lane_by_its_title(self):
        reg = registry()
        heads = re.findall(r'^echo "([^"]+ lane)"$', (REPO / "check-pins.sh").read_text(), re.M)
        for lane in reg.lanes(engine="vllm"):
            self.assertIn(f'{reg.LANES[lane]["title"]} lane', heads, lane)
        for short in ("Kolibri lane", "Nemotron lane", "Nemotron Super lane"):
            self.assertNotIn(short, heads)

    def test_the_switch_names_kolibri_1(self):
        text = "\n".join(line for _, line in code_lines(REPO / "switch-model.sh"))
        self.assertEqual(re.findall(r'die "the Kolibri (?:checkpoint|unit)', text), [])
        self.assertEqual(len(re.findall(r'die "the Kolibri-1 (?:checkpoint|unit)', text)), 3)


if __name__ == "__main__":
    unittest.main()
