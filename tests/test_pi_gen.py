#!/usr/bin/env python3
"""pi-gen.py derives the pi and omp agent configs from the generated opencode.json.

pi and omp read no opencode.json (docs/pi.md): their default model, provider
names and limits are plain values in their own files, so the files this
generator writes are where those facts live. These check the derivation against
the artifact: key form, limits, the pi/omp value parity (one build, two
serializations), the omp thinking ladder taken from the artifact's variants,
the compaction sizing, and the quiet paths (no artifact, foreign providers).
The omp YAML is read back by a mini reader for the emitter's exact subset:
pi-gen.py is stdlib-only, this CI step installs no PyYAML, and neither side
of the parity check may import one. The spelling on disk is verified against
omp itself (docs/pi.md); what these tests pin is the pair's values.
"""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import tempfile
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
GEN = REPO / "pi-gen.py"

ARTIFACT = {
    "provider": {
        "qwen38": {
            "options": {"baseURL": "http://127.0.0.1:30001/v1", "apiKey": "{file:/keys/api-key}"},
            "models": {"qwen3.8-27b": {
                "name": "Qwen3.8-27B (local)",
                "limit": {"context": 173000, "input": 173000, "output": 64000},
                "variants": {"lean": {}, "low": {}, "medium": {}, "xhigh": {}},
                "modalities": {"input": ["text", "image"]}}},
        },
        "someone-elses": {"options": {"baseURL": "https://x.test/v1"}, "models": {"m": {}}},
    },
    "compaction": {"preserve_recent_tokens": 46000},
    "model": "qwen38/qwen3.8-27b",
}


def run(args):
    return subprocess.run([sys.executable, str(GEN)] + args, capture_output=True, text=True)


def yload(text):
    """Read what pi-gen.py's own writer emits: quoted strings, bare true/
    false/int, two-space indents, '- ' items. Nothing else, on purpose: the
    generator promises those shapes and the real omp parses them."""
    rows = [(len(l) - len(l.lstrip(" ")), l.strip()) for l in text.splitlines() if l.strip()]
    i = 0

    def sc(tok):
        if tok.startswith('"'):
            return json.loads(tok)
        if tok in ("true", "false"):
            return tok == "true"
        if tok in ("{}", "[]"):
            return {} if tok == "{}" else []
        return int(tok)

    def block(ind):
        nonlocal i
        if i < len(rows) and rows[i][1].startswith("-"):
            out = []
            while i < len(rows) and rows[i][0] == ind and rows[i][1].startswith("-"):
                body = rows[i][1][1:].strip()
                if body.startswith('"') or ":" not in body:
                    i += 1
                    out.append(sc(body))
                else:                       # '- id: x': a mapping item, first key inlined
                    rows[i] = (ind + 2, body)
                    out.append(block(ind + 2))
            return out
        out = {}
        while i < len(rows) and rows[i][0] == ind and not rows[i][1].startswith("-"):
            key, _, val = rows[i][1].partition(":")
            val = val.strip()
            i += 1
            if val:
                out[key] = sc(val)          # quoted strings keep any ':' inside
            else:
                out[key] = block(ind + 2) if i < len(rows) and rows[i][0] >= ind + 2 else {}
        return out

    return block(0)


class PiGen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.art = os.path.join(self.tmp.name, "opencode.json")
        with open(self.art, "w") as f:
            f.write(json.dumps(ARTIFACT))
        self.out = os.path.join(self.tmp.name, "out")

    def tearDown(self):
        self.tmp.cleanup()

    def gen(self, *extra):
        r = run(["--config", self.art, "--out", self.out] + list(extra))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def read_pi(self):
        with open(os.path.join(self.out, "pi", "models.json")) as f:
            models = json.loads(f.read())
        with open(os.path.join(self.out, "pi", "settings.json")) as f:
            settings = json.loads(f.read())
        return models, settings

    def read_omp(self):
        with open(os.path.join(self.out, "omp", "models.yml")) as f:
            models = yload(f.read())
        with open(os.path.join(self.out, "omp", "config.yml")) as f:
            cfg = yload(f.read())
        return models, cfg

    def test_generation(self):
        self.gen()
        models, settings = self.read_pi()
        prov = models["providers"]["qwen38"]
        self.assertNotIn("someone-elses", models["providers"])
        self.assertEqual(prov["apiKey"], "!cat /keys/api-key")
        entry = prov["models"][0]
        self.assertEqual((entry["contextWindow"], entry["maxTokens"]), (173000, 64000))
        self.assertEqual(entry["input"], ["text", "image"])
        self.assertEqual(settings["defaultProvider"], "qwen38")
        self.assertEqual(settings["defaultModel"], "qwen3.8-27b")
        self.assertEqual(settings["compaction"]["keepRecentTokens"], 46000)
        self.assertEqual(settings["compaction"]["modelOverrides"]["qwen38/qwen3.8-27b"], {"reserveTokens": 64000})
        self.assertEqual(settings["modelThinkingLevels"], {"qwen38/qwen3.8-27b": "off"})
        # its system prompt goes out as "system": as "developer" the served template refused
        # every request (400 "Unexpected message role.", pi 0.99.2, 2026-10-01)
        self.assertEqual(entry["compat"], {"supportsDeveloperRole": False})

    def test_omp_parity_and_thinking(self):
        self.gen()
        models, _ = self.read_pi()
        omp_models, omp_cfg = self.read_omp()
        pi_entry = dict(models["providers"]["qwen38"]["models"][0])
        omp_entry = dict(omp_models["providers"]["qwen38"]["models"][0])
        pi_entry.pop("inputLimits")
        pi_entry.pop("compat")
        thinking = omp_entry.pop("thinking")
        compat = omp_entry.pop("compat")
        self.assertEqual(omp_entry, pi_entry)            # one build, two serializations
        # the template's lean rides on minimal through reasoningEffortMap, and
        # qwenTemplateReasoningEffort is what routes the mapped pick onto
        # chat_template_kwargs.reasoning_effort. Without that flag the picker
        # moved and no effort field was sent (captured against omp 18.4.6,
        # 2026-10-01): it is the one field the ladder needs.
        self.assertEqual(thinking, {"mode": "effort", "efforts": ["minimal", "low", "medium", "xhigh"],
                                    "defaultLevel": "minimal"})
        # and rides chat_template_kwargs alone: omp's default dialect also sent a top-level
        # reasoning_effort, which SGLang refuses as "lean" (a 400 on every request at the
        # default level, omp 18.4.9, 2026-10-01)
        self.assertEqual(compat, {"thinkingFormat": "qwen-chat-template", "qwenTemplateReasoningEffort": True,
                                  "reasoningEffortMap": {"minimal": "lean"}})
        self.assertEqual(omp_cfg["modelRoles"]["default"], "qwen38/qwen3.8-27b")

    def test_kolibri_is_sent_its_effort_and_starts_at_high(self):
        # Kolibri reasons at high with no effort field: pi's "off" sent nothing, so a pi user saw
        # thinking off while every request reasoned at high (found in review, 2026-10-14)
        art = json.loads(json.dumps(ARTIFACT))
        art["provider"]["kolibri"] = {
            "options": {"baseURL": "http://127.0.0.1:30001/v1", "apiKey": "{file:/keys/api-key}"},
            "models": {"kolibri-1": {"name": "Kolibri-1 FP8 official (local, 262K)",
                                     "limit": {"context": 173000, "input": 173000, "output": 64000},
                                     "variants": {"none": {}, "low": {}, "medium": {}, "high": {}},
                                     "modalities": {"input": ["text"], "output": ["text"]}}}}
        art["model"] = "kolibri/kolibri-1"
        with open(self.art, "w") as f:
            f.write(json.dumps(art))
        self.gen()
        models, settings = self.read_pi()
        entry = models["providers"]["kolibri"]["models"][0]
        self.assertEqual(entry["compat"], {"supportsDeveloperRole": False, "supportsReasoningEffort": True})
        self.assertEqual(entry["thinkingLevelMap"], {"off": "none", "minimal": "low", "xhigh": "high"})
        self.assertEqual(settings["modelThinkingLevels"]["kolibri/kolibri-1"], "high")
        self.assertEqual(settings["modelThinkingLevels"]["qwen38/qwen3.8-27b"], "off", "the Qwen lanes as before")
        self.assertNotIn("thinkingLevelMap", models["providers"]["qwen38"]["models"][0])
        omp_models, _ = self.read_omp()
        omp_entry = dict(omp_models["providers"]["kolibri"]["models"][0])
        self.assertEqual(omp_entry.pop("thinking"), {"mode": "effort", "efforts": ["minimal", "low", "medium", "high"],
                                                     "defaultLevel": "high"})
        self.assertEqual(omp_entry.pop("compat"), {"thinkingFormat": "qwen-chat-template",
                                                   "qwenTemplateReasoningEffort": True,
                                                   "reasoningEffortMap": {"minimal": "none"}})
        pi_entry = {k: v for k, v in entry.items() if k not in ("compat", "thinkingLevelMap")}
        self.assertEqual(omp_entry, pi_entry, "one build, two serializations; omp has no thinkingLevelMap")

    def test_omp_starts_a_lane_at_its_installed_templates_default(self):
        # omp sends its default level on every request, and took lean for every patched Qwen template:
        # a box installed with LEAN_DEFAULT=0, whose templates keep Qwen's xhigh, got lean in omp alone
        # (found in review, 2026-10-20). The default is the installed template's, read beside the configs.
        os.makedirs(self.out, exist_ok=True)

        def level(default):
            if default is None:
                p = pathlib.Path(self.out, "chat-template-sglang.jinja")
                if p.exists():
                    p.unlink()
            else:
                pathlib.Path(self.out, "chat-template-sglang.jinja").write_text(
                    "{%- set reasoning_effort = reasoning_effort|default('" + default + "') %}\n")
            self.gen()
            omp_models, _ = self.read_omp()
            return omp_models["providers"]["qwen38"]["models"][0]["thinking"]["defaultLevel"]
        self.assertEqual(level("xhigh"), "xhigh")
        self.assertEqual(level("lean"), "minimal")
        self.assertEqual(level(None), "minimal")          # no template there: the patched default

    def test_the_flash_local_lane_starts_at_xhigh_in_omp(self):
        # its template's default is Qwen's own, xhigh (Hasan's choice for this lane, 2026-10-19), which
        # its provider lists first: omp took "lean" in its tiers for the patched template's default
        # and started it at minimal, sent as lean (found in review, 2026-10-20)
        import importlib.util
        spec = importlib.util.spec_from_file_location("oc_flash_local", REPO / "oc-flash-local.py")
        ocf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ocf)
        art = json.loads(json.dumps(ARTIFACT))
        art["provider"]["flash-local"] = ocf.provider("http://127.0.0.1:30001/v1", "/keys/api-key", 205000, 32000,
                                                      "Qwen3.8-Flash-Next FP4-Local (local, 262K)")
        art["model"] = "flash-local/qwen3.8-flash-next-fp4"
        with open(self.art, "w") as f:
            f.write(json.dumps(art))
        self.gen()
        _, settings = self.read_pi()
        # pi sends no effort, so the template's own default, xhigh
        self.assertEqual(settings["modelThinkingLevels"]["flash-local/qwen3.8-flash-next-fp4"], "off")
        omp_models, _ = self.read_omp()
        entry = omp_models["providers"]["flash-local"]["models"][0]
        self.assertEqual(entry["thinking"], {"mode": "effort", "efforts": ["xhigh", "minimal", "low", "medium"],
                                             "defaultLevel": "xhigh"})
        self.assertEqual(entry["compat"]["reasoningEffortMap"], {"minimal": "lean"})
        self.assertEqual(omp_models["providers"]["qwen38"]["models"][0]["thinking"]["defaultLevel"], "minimal",
                         "the patched template's default, lean, for the lanes that keep it")

    def test_nemotron_is_sent_its_effort_starts_at_high_and_takes_images(self):
        # Bolt reasons at its full depth unasked, as Kolibri does at high; the proxy turns the effort
        # field into the template's words (keepalive-proxy.py, nemotron_effort), and Bolt reads images
        art = json.loads(json.dumps(ARTIFACT))
        art["provider"]["nemotron"] = {
            "options": {"baseURL": "http://127.0.0.1:30001/v1", "apiKey": "{file:/keys/api-key}"},
            "models": {"nemotron-3.5-bolt": {"name": "Nemotron 3.5 Bolt (local, 262K)",
                                             "limit": {"context": 205000, "input": 205000, "output": 32000},
                                             "variants": {"none": {}, "low": {}, "medium": {}, "high": {}},
                                             "attachment": True,
                                             "modalities": {"input": ["text", "image"], "output": ["text"]}}}}
        art["model"] = "nemotron/nemotron-3.5-bolt"
        with open(self.art, "w") as f:
            f.write(json.dumps(art))
        self.gen()
        models, settings = self.read_pi()
        entry = models["providers"]["nemotron"]["models"][0]
        self.assertEqual(entry["compat"], {"supportsDeveloperRole": False, "supportsReasoningEffort": True})
        self.assertEqual(entry["thinkingLevelMap"], {"off": "none", "minimal": "low", "xhigh": "high"})
        self.assertEqual(entry["input"], ["text", "image"])
        self.assertEqual(settings["modelThinkingLevels"]["nemotron/nemotron-3.5-bolt"], "high")
        self.assertEqual(settings["modelThinkingLevels"]["qwen38/qwen3.8-27b"], "off", "the Qwen lanes as before")

    def test_ornith_is_sent_its_levels_as_they_are_starts_at_high_and_takes_images(self):
        # Ornith's template reads enable_thinking alone: the proxy turns none and minimal off and every
        # other level on (keepalive-proxy.py, ornith_effort), so pi sends its levels as they are, its off
        # as none; Kolibri's map sent its minimal as low, which the proxy kept whole (found in review,
        # 2026-10-22)
        art = json.loads(json.dumps(ARTIFACT))
        art["provider"]["ornith-122b"] = {
            "options": {"baseURL": "http://127.0.0.1:30001/v1", "apiKey": "{file:/keys/api-key}"},
            "models": {"ornith-1.5-122b": {"name": "Ornith 1.5 122B NVFP4 + MTP (local, 1M)",
                                           "limit": {"context": 990000, "input": 990000, "output": 32000},
                                           "variants": {"high": {}, "none": {}},
                                           "attachment": True,
                                           "modalities": {"input": ["text", "image"], "output": ["text"]}}}}
        art["model"] = "ornith-122b/ornith-1.5-122b"
        with open(self.art, "w") as f:
            f.write(json.dumps(art))
        self.gen()
        models, settings = self.read_pi()
        entry = models["providers"]["ornith-122b"]["models"][0]
        self.assertEqual(entry["compat"], {"supportsDeveloperRole": False, "supportsReasoningEffort": True})
        self.assertEqual(entry["thinkingLevelMap"], {"off": "none"})
        self.assertEqual(entry["input"], ["text", "image"])
        self.assertEqual(settings["modelThinkingLevels"]["ornith-122b/ornith-1.5-122b"], "high")

    def test_idempotent(self):
        self.gen()
        again = self.gen()
        self.assertNotIn("wrote", again.stdout)

    def test_the_documented_command_runs(self):
        """The README and docs/pi.md run it as ./pi-gen.py, which a file committed without
        its execute bit answered with Permission denied (found on the reference box, 2026-10-06)."""
        self.assertTrue(os.access(GEN, os.X_OK), "pi-gen.py is not executable")

    def test_no_artifact_is_quiet_success(self):
        r = run(["--config", os.path.join(self.tmp.name, "none.json"), "--out", self.out])
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.out))

    def test_foreign_artifact_writes_nothing(self):
        art = os.path.join(self.tmp.name, "foreign.json")
        with open(art, "w") as f:
            f.write('{"provider": {"other": {}}, "model": "other/x"}')
        r = run(["--config", art, "--out", self.out])
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("wrote", r.stdout)

    def test_a_model_without_tiers_still_speaks_the_sglang_dialect(self):
        art = json.loads(json.dumps(ARTIFACT))
        del art["provider"]["qwen38"]["models"]["qwen3.8-27b"]["variants"]
        with open(self.art, "w") as f:
            f.write(json.dumps(art))
        self.gen()
        omp_models, _ = self.read_omp()
        entry = omp_models["providers"]["qwen38"]["models"][0]
        self.assertNotIn("thinking", entry)
        self.assertEqual(entry["compat"], {"thinkingFormat": "qwen-chat-template"})

    def test_a_key_path_with_a_space_is_quoted_for_the_shell(self):
        # both agents run the !command in a shell
        art = json.loads(json.dumps(ARTIFACT))
        art["provider"]["qwen38"]["options"]["apiKey"] = "{file:/home/my user/api-key}"
        with open(self.art, "w") as f:
            f.write(json.dumps(art))
        self.gen()
        models, _ = self.read_pi()
        self.assertEqual(models["providers"]["qwen38"]["apiKey"], "!cat '/home/my user/api-key'")
        with open(os.path.join(self.out, "omp", "models.yml")) as f:
            self.assertIn('apiKey: "!cat \'/home/my user/api-key\'"', f.read())

    def test_key_line_is_quoted(self):
        # A plain scalar may not start with '!': unquoted, the !cat key is a
        # YAML tag, not a string, and omp would misread the provider. The
        # reader above would shrug at that; the byte on disk cannot.
        self.gen()
        with open(os.path.join(self.out, "omp", "models.yml")) as f:
            self.assertIn('apiKey: "!cat /keys/api-key"', f.read())



def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TheDocSaysWhatEachLaneIsSent(unittest.TestCase):
    """docs/pi.md has a line for each lane pi sends its effort field to (EFFORT_FIELD), under its family's name
    (Kolibri, Nemotron, Ornith), with the levels pi-gen.py maps and the one pi starts at: Ornith 1.5 122B had its
    table in pi-gen.py and no line in the page (found 2026-11-10)."""

    def test_every_lane_of_effort_field(self):
        gen = load("pi_gen_for_its_doc", GEN)
        reg = load("lane_registry_for_pi_doc", REPO / "lane_registry.py")
        page = (REPO / "docs" / "pi.md").read_text()
        section = page[page.index("## Reasoning effort"):]
        section = section[:section.index("\n## ", 1)]
        bullets = re.findall(r"^- \*\*([^*]+)\*\*(.*)$", section, re.M)
        self.assertGreaterEqual(len(bullets), 3, section)
        for prov, (level_map, start) in gen.EFFORT_FIELD.items():
            with self.subTest(prov):
                title = next(f["title"] for f in reg.LANES.values() if f["provider"] == prov)
                family = re.sub(r"-\d+$", "", title.split()[0])
                said = [rest for name, rest in bullets if name.startswith(family)]
                self.assertEqual(len(said), 1, f"docs/pi.md has {len(said)} lines for {family} ({prov}), one expected")
                for frm, to in level_map.items():
                    self.assertIn(f"`{frm}` to `{to}`", said[0])
                # pi's: omp's start, in the same line, is said "starts at" alone
                self.assertIn(f"its level starts at `{start}`", said[0])


class AnUpdateRegeneratesThemToo(unittest.TestCase):
    """install.sh regenerates pi's and omp's files as switch-model.sh does after a switch (block
    4f): v1.30.0's update changed Kolibri's pair in opencode.json and left pi's models.json at the
    old one until the next switch (found 2026-10-14)."""
    BLOCK = ('if [ -d "$CONFIG_DIR/pi" ] || [ -d "$CONFIG_DIR/omp" ]; then\n'
             '  python3 "$REPO_DIR/pi-gen.py" --config "$CONFIG_DIR/opencode.json" --out "$CONFIG_DIR" \\\n')

    def test_install_and_the_switch_run_it_the_same_way(self):
        for script in ("install.sh", "switch-model.sh"):
            with self.subTest(script):
                self.assertEqual((REPO / script).read_text().count(self.BLOCK), 1)

    def test_install_runs_it_once_opencode_json_is_written(self):
        text = (REPO / "install.sh").read_text()
        at = text.index(self.BLOCK)
        self.assertLess(text.rindex('python3 "$REPO_DIR/oc-point-default.py" "$CONFIG_DIR/opencode.json"'), at,
                        "after the last write of the artifact it reads")
        self.assertLess(at, text.index('echo "opencode-web.service restarted so it reads the new limits"'))

if __name__ == "__main__":
    unittest.main()
