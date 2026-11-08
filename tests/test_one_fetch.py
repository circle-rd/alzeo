#!/usr/bin/env python3
"""Every lane's checkpoint goes through ckpt-fetch.py, and no script keeps a fetch of its own.

Seven copies of the fetch had drifted (found in review, 2026-10-14): the image and video
switches gave up at the first network error, only Kolibri's checked the shards of a cached
snapshot, and the Xet exception lived in the text lanes' copies alone. tests/test_ckpt_fetch.py
holds what the script does; this holds who runs it, with which options, that each keeps the
Xet ban the script lifts for one repo only, and that each caller mounting it in a container
makes sure it is there first (docker -v of a missing file makes a root-owned directory in its
place).
"""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]


def text(name):
    return (REPO / name).read_text()


# who runs it, as each script writes the call: the options are what each fetch is
CALLS = {
    "install.sh, the text lanes":
        ("install.sh", '"$PULLED_IMAGE" /opt/qwen38/ckpt-fetch.py "$1" "$2" --refs-main fill'),
    "switch-model.sh, the text lanes":
        ("switch-model.sh", '"$DL_IMAGE" /opt/qwen38/ckpt-fetch.py "$TARGET_REPO" "$TARGET_REV" --offline-first '
                            '--refs-main fill'),
    "install-image.sh":
        ("install-image.sh", '"$VENV/bin/python" "$HERE/ckpt-fetch.py" "$MODEL" "$MODEL_REV" --refs-main pin'),
    "switch-model.sh, the image lane":
        ("switch-model.sh", '"$IMG_PY" "$REPO_DIR/ckpt-fetch.py" "$IMG_MODEL" "$IMG_REV" --offline-first \\\n'
                            '    --need-gb 34 --free-gb "$(dl_free_gb "${IMG_HF:-$HF_CACHE}")" --refs-main pin'),
    "install-video.sh":
        ("install-video.sh", '"$VENV/bin/python" "$HERE/ckpt-fetch.py" "$MODEL" "$MODEL_REV" --allow "$ALLOW" --refs-main pin'),
    "switch-model.sh, the video lane":
        ("switch-model.sh", '"$VID_PY" "$REPO_DIR/ckpt-fetch.py" "$VID_MODEL" "$VID_REV" --allow "$VID_ALLOW" '
                            '--offline-first --refs-main fill'),
    "vllm-lane.sh, the checkpoints of the five vLLM lanes' installers":
        ("vllm-lane.sh", '"$LOCAL_IMAGE" /opt/qwen38/ckpt-fetch.py "$1" "$2" --offline-first'),
    "switch-model.sh, the Kolibri lane":
        ("switch-model.sh", '"$KOL_IMAGE" /opt/qwen38/ckpt-fetch.py "$KOL_MODEL" "$KOL_REV" --offline-first \\\n'
                            '    --need-gb "$KOL_NEED_GB" --free-gb "$(dl_free_gb "$KOL_HF")"'),
    "switch-model.sh, the Nemotron lane":
        ("switch-model.sh", '"$NEM_IMAGE" /opt/qwen38/ckpt-fetch.py "$NEM_R" "$NEM_V" --offline-first \\\n'
                            '      --need-gb "$NEM_ASK" --free-gb "$(dl_free_gb "$NEM_HF")"'),
    "switch-model.sh, the Qwen3.8 Flash-Next FP4-Local lane":
        ("switch-model.sh", '"$FLASH_LOCAL_IMAGE" /opt/qwen38/ckpt-fetch.py "$FLASH_LOCAL_REPO" "$FLASH_LOCAL_REV" --offline-first \\\n'
                            '    --need-gb "$FLASH_LOCAL_GB" --free-gb "$(dl_free_gb "$FLL_HF")"'),
    "switch-model.sh, the Ornith lane":
        ("switch-model.sh", '"$FLASH_LOCAL_IMAGE" /opt/qwen38/ckpt-fetch.py "$ORNITH_122B_REPO" "$ORNITH_122B_REV" --offline-first \\\n'
                            '    --need-gb "$ORNITH_122B_GB" --free-gb "$(dl_free_gb "$ORN_HF")"'),
}
SCRIPTS = ("install.sh", "switch-model.sh", "install-image.sh", "install-video.sh", "install-kolibri.sh",
           "install-nemotron.sh", "install-nemotron-super.sh", "install-flash-local.sh", "install-ornith-122b.sh",
           "vllm-lane.sh")
VLLM_INSTALLERS = ("install-kolibri.sh", "install-nemotron.sh", "install-nemotron-super.sh", "install-flash-local.sh",
                   "install-ornith-122b.sh")


class EveryLaneFetchesThroughIt(unittest.TestCase):
    def test_each_caller_runs_it_with_its_options(self):
        for what, (script, call) in CALLS.items():
            with self.subTest(what):
                self.assertEqual(text(script).count(call), 1, f"{script} no longer runs it this way")
        self.assertEqual(text("switch-model.sh").count("ckpt-fetch.py\" \"$") + text("switch-model.sh").count(
            "/opt/qwen38/ckpt-fetch.py \"$"), 7, "switch-model.sh fetches for seven lanes, each through it")

    def test_no_script_keeps_a_fetch_of_its_own(self):
        for script in SCRIPTS:
            t = text(script)
            with self.subTest(script):
                self.assertNotIn("snapshot_download(", t)
                self.assertNotIn("<<'PYEOF' || die \"Checkpoint", t)
                # one single-file fetch is left, the video lane's Turbo adapter, which the lane serves
                # without (a failure there is a note)
                self.assertEqual(t.count("hf_hub_download("), 1 if script == "install-video.sh" else 0)
        self.assertFalse((REPO / "kolibri-fetch.py").exists(), "the Kolibri lane's own copy is back")

    def test_every_call_keeps_the_xet_ban(self):
        # the script lifts it for the one repo the CDN refuses, and only where it is on
        for what, (script, call) in CALLS.items():
            t = text(script)
            at = t.index(call)
            with self.subTest(what):
                self.assertIn("HF_HUB_DISABLE_XET=1", t[max(0, at - 700):at])

    def test_a_container_gets_the_script_only_once_it_is_known_to_be_there(self):
        for script, check, mount in (
                ("install.sh", '[ -f "$REPO_DIR/ckpt-fetch.py" ] || die',
                 '-v "$REPO_DIR/ckpt-fetch.py:/opt/qwen38/ckpt-fetch.py:ro"'),
                ("switch-model.sh", '[ -f "$REPO_DIR/ckpt-fetch.py" ] || die',
                 '-v "$REPO_DIR/ckpt-fetch.py:/opt/qwen38/ckpt-fetch.py:ro"'),
                ("vllm-lane.sh", '[ -f "$HERE/$f" ] || die',
                 '-v "$HERE/ckpt-fetch.py:/opt/qwen38/ckpt-fetch.py:ro"')):
            t = text(script)
            with self.subTest(script):
                self.assertIn(check, t)
                self.assertIn(mount, t)
                self.assertLess(t.index(check), t.index(mount), "checked before it is mounted")

    def test_each_vllm_installer_names_the_script_in_its_preflight_before_it_fetches(self):
        # the three vLLM installers check ckpt-fetch.py in their preflight (vllm-lane.sh's
        # vlane_preflight checks each file it is given), before their first vlane_fetch
        for script in VLLM_INSTALLERS:
            t = text(script)
            with self.subTest(script):
                pre = re.search(r"\nvlane_preflight [^\n]*\bckpt-fetch\.py\b", t)
                self.assertIsNotNone(pre, "its preflight checks ckpt-fetch.py")
                self.assertLess(pre.start(), t.index('\nvlane_fetch "'))


if __name__ == "__main__":
    unittest.main()
