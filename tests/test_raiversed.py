#!/usr/bin/env python3
"""reverse-agent beside every lane: one agent per served family, started and stopped with its lane.

The hook must never fail a lane, must announce the models each lane actually serves, and must keep
one identity per family. These are the facts that keep those promises.
"""
import importlib.util
import io
import pathlib
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("raiversed_lane", REPO / "raiversed-lane.py")
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)

INSTALLER = (REPO / "install-raiversed.sh").read_text()
LANES = hook.lane_registry.LANES


def config(**overrides):
    values = dict(hook.DEFAULTS)
    values["RAIVERSED_STATE_DIR"] = "/tmp/raiversed/reverse-agent/state"
    values["RAIVERSED_API_KEY_FILE"] = "/tmp/raiversed/api-key"
    values.update(overrides)
    return values


class TheFamilies(unittest.TestCase):
    def test_every_lane_kind_has_a_family_and_every_unit_maps_to_one(self):
        kinds = {spec["kind"] for spec in LANES.values()}
        self.assertEqual(kinds, {"text", "embedding", "speech", "transcribe", "image", "video"})
        for name, spec in LANES.items():
            with self.subTest(lane=name):
                self.assertIsNotNone(hook.family_of(spec))

    def test_the_hook_and_the_installer_agree_on_the_family_suffixes(self):
        suffixes = set(re.search(r'FAMILY_SUFFIXES="([^"]+)"', INSTALLER).group(1).split())
        families = set(hook.FAMILY_OF_KIND.values()) - {"text"}
        self.assertEqual(suffixes, families)

    def test_containers_and_state_directories_are_per_family(self):
        cfg = config()
        self.assertEqual(hook.container_name("reverse-agent", "text"), "reverse-agent")
        self.assertEqual(hook.container_name("reverse-agent", "embedding"), "reverse-agent-embedding")
        self.assertEqual(str(hook.state_dir(cfg, "text")), "/tmp/raiversed/reverse-agent/state")
        self.assertEqual(str(hook.state_dir(cfg, "video")), "/tmp/raiversed/reverse-agent/video")


class TheAnnouncements(unittest.TestCase):
    def test_a_text_lane_announces_its_served_name_through_the_proxy(self):
        upstream, models, extra = hook.announced("qwen38-flash.service", "flash", LANES["flash"])
        self.assertEqual(upstream, hook.TEXT_PROXY)
        self.assertEqual(models, [("qwen3.8-flash-next", ("text", "tools"))])
        self.assertFalse(extra)

    def test_a_vision_text_lane_is_tagged_vision(self):
        _, models, _ = hook.announced("qwen38-nemotron-super.service", "nemotron-super", LANES["nemotron-super"])
        self.assertEqual(models, [("nemotron-3.5-super-vl", ("text", "vision", "tools"))])

    def test_the_embedding_lane_announces_its_alias_on_the_port_the_proxy_reads(self):
        with mock.patch.object(hook, "EMBED_DROPIN", pathlib.Path("/nonexistent")), \
             mock.patch.object(hook, "CONFIG_DIR", pathlib.Path(tempfile.mkdtemp())):
            upstream, models, _ = hook.announced("qwen38-embed.service", "embedding", LANES["embedding"])
        self.assertEqual(upstream, "http://127.0.0.1:30027")
        self.assertEqual(models, [("pplx-embed-v1-0.6b", ("embedding",))])

    def test_the_speech_lane_announces_recognition_and_synthesis_with_their_own_tags(self):
        upstream, models, extra = hook.announced("qwen38-speech.service", "speech", LANES["speech"])
        self.assertEqual(upstream, "http://127.0.0.1:30024")
        self.assertEqual(dict(models), {"qwen3-asr-0.6b": ("transcription",), "kokoro-82m": ("speech",)})
        self.assertFalse(extra)

    def test_the_transcription_lane_announces_the_gguf_derived_id(self):
        upstream, models, _ = hook.announced("qwen38-transcribe.service", "transcribe", LANES["transcribe"])
        self.assertEqual(upstream, "http://127.0.0.1:30036")
        self.assertEqual(models[0][1], ("transcription",))
        self.assertTrue(models[0][0].endswith(".gguf"))

    def test_image_and_video_declare_their_models_as_extras_on_their_own_ports(self):
        upstream, models, extra = hook.announced("qwen38-image.service", "image", LANES["image"])
        self.assertEqual((upstream, models, extra), ("http://127.0.0.1:30020", [("Qwen/Qwen-Image-2.1", ("image",))], True))
        upstream, models, extra = hook.announced("qwen38-image-h3.service", "image-h3", LANES["image-h3"])
        self.assertEqual((upstream, extra), ("http://127.0.0.1:30026", True))
        self.assertEqual(models, [("MiniMaxAI/MiniMax-H3-Image", ("image",))])
        upstream, models, extra = hook.announced("qwen38-video.service", "video", LANES["video"])
        self.assertEqual((upstream, models, extra), ("http://127.0.0.1:30022", [("MiniMaxAI/MiniMax-H3", ("video",))], True))


class TheEnvironment(unittest.TestCase):
    def test_tags_are_written_per_model_and_extras_only_when_declared(self):
        models = [("qwen3-asr-0.6b", ("transcription",)), ("kokoro-82m", ("speech",))]
        env = hook.desired_env("http://127.0.0.1:30024", models, False, config())
        self.assertEqual(env["AGENT_MODEL_TAGS"], "qwen3-asr-0.6b=transcription;kokoro-82m=speech")
        self.assertNotIn("AGENT_EXTRA_MODELS", env)
        env = hook.desired_env("http://127.0.0.1:30022", [("MiniMaxAI/MiniMax-H3", ("video",))], True, config())
        self.assertEqual(env["AGENT_EXTRA_MODELS"], "MiniMaxAI/MiniMax-H3")

    def test_the_relay_and_the_scope_are_the_reference_ones(self):
        env = hook.desired_env(hook.TEXT_PROXY, [("m", ("text",))], False, config())
        self.assertEqual(env["AGENT_RELAY_URL"], "https://relay.circle-cyber.com")
        self.assertEqual(env["AGENT_AUTH_CLIENT_ID"], "reverse-agent")
        self.assertEqual(env["AGENT_AUTH_SCOPE"], "openid profile offline_access tunnel:serve")


class TheContainer(unittest.TestCase):
    def test_the_run_command_is_sandboxed_and_carries_no_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = pathlib.Path(tmp) / "api-key"
            key.write_text("sk-secret\n")
            cfg = config(RAIVERSED_STATE_DIR=f"{tmp}/reverse-agent/state", RAIVERSED_API_KEY_FILE=str(key))
            env = {"AGENT_UPSTREAM_URL": "http://127.0.0.1:30022", "AGENT_MODEL_TAGS": "m=video"}
            out = io.StringIO()
            with redirect_stdout(out):
                hook.recreate("reverse-agent-video", "video", env, cfg, dry_run=True)
            text = out.getvalue()
            for flag in ("--read-only", "--cap-drop", "ALL", "no-new-privileges:true", "--network", "host"):
                self.assertIn(flag, text)
            self.assertIn(f"{tmp}/reverse-agent/video:/data", text)
            self.assertNotIn("sk-secret", text)

    def test_stopping_a_lane_removes_only_its_family_container(self):
        out = io.StringIO()
        with redirect_stdout(out):
            hook.remove("reverse-agent-video", dry_run=True)
        self.assertIn("reverse-agent-video", out.getvalue())
        self.assertNotIn("reverse-agent\"", out.getvalue())


class TheInstaller(unittest.TestCase):
    def test_the_drop_ins_start_and_stop_the_family_with_its_lane(self):
        self.assertIn("ExecStartPost=-/usr/bin/python3", INSTALLER)
        self.assertIn('"$HOOK" %n start', INSTALLER)
        self.assertIn("ExecStopPost=-/usr/bin/python3", INSTALLER)
        self.assertIn('"$HOOK" %n stop', INSTALLER)

    def test_the_reference_defaults_are_the_ones_the_box_runs(self):
        self.assertIn("https://relay.circle-cyber.com", INSTALLER)
        self.assertIn("https://auth.circle-cyber.com", INSTALLER)
        self.assertIn('CLIENT_ID="${RAIVERSED_CLIENT_ID:-reverse-agent}"', INSTALLER)
        self.assertIn("openid profile offline_access tunnel:serve", INSTALLER)
        self.assertIn("ghcr.io/circle-rd/ai-reverse-agent:nightly", INSTALLER)

    def test_no_fallback_to_the_text_proxy_remains(self):
        self.assertNotIn("--fallback", INSTALLER)
        self.assertNotIn("--fallback", (REPO / "raiversed-lane.py").read_text())


if __name__ == "__main__":
    unittest.main()
