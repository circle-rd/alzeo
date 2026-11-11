#!/usr/bin/env python3
"""tests/lane-templates-check.py, without the network: the text lanes it knows are every text lane,
each served the way its installer serves it, and the shapes it renders are the captured ones.

The check itself, with the network and jinja2, renders every lane's template at its pin (a CI step of
its own): the Ornith lane's published template refused opencode 1.19.4's second system message, a 400
on every opencode request from v1.38.6 to v1.40.1 that no test saw (2026-10-24)."""
import importlib.util
import json
import pathlib
import shutil
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lane_templates_check", REPO / "tests" / "lane-templates-check.py")
ltc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ltc)


class TheLanesItKnows(unittest.TestCase):
    def test_every_text_lane_with_how_its_installer_serves_its_template(self):
        specs = {name: env for name, _, _, env in ltc.lane_specs()}
        self.assertEqual(sorted(specs), sorted(["stock", "uncensored", "fp8", "uncensored-fp8", "flash", "flash-nvda",
                                                "flash-uncensored", "flash-local", "ornith-122b", "kolibri",
                                                "kolibri-nvfp4", "nemotron", "nemotron-super"]))
        self.assertEqual(specs["ornith-122b"], {"patches": "system-reminder,tool-reference"})
        self.assertEqual(specs["stock"], {})
        self.assertIsNone(specs["kolibri"])
        # the Qwen3.8 Flash-Next FP4-Local lane at its default, lean (v1.46.4), given as such: left unset, the patcher
        # keeps the default of the template installed, xhigh on every box this lane served until then. The line that
        # moves it to xhigh is FLASH_LOCAL_EFFORT's, below the one this check reads.
        self.assertEqual(specs["flash-local"], {"LEAN_DEFAULT": "1"})

    def test_each_kolibri_checkpoint_at_its_own_pin(self):
        # the check read only the NVFP4's pin, while the FP8 is the lane's default (2026-10-24)
        repos = {name: repo for name, repo, _, _ in ltc.lane_specs()}
        self.assertEqual(repos["kolibri"], "Aleph-Alpha/Kolibri-1")
        self.assertEqual(repos["kolibri-nvfp4"], "Quantinine/Kolibri-1-NVFP4A16-FP8")

    def copy(self):
        d = pathlib.Path(tempfile.mkdtemp(prefix="ltc-"))
        self.addCleanup(shutil.rmtree, d, True)
        for f in [REPO / "install.sh", REPO / "switch-model.sh", *REPO.glob("install-*.sh")]:
            shutil.copy(f, d / f.name)
        return d

    def test_a_switch_target_it_does_not_know_stops_it(self):
        d = self.copy()
        f = d / "switch-model.sh"
        f.write_text(f.read_text().replace('case "$CHOICE" in stock|', 'case "$CHOICE" in stock|newlane|', 1))
        with self.assertRaisesRegex(SystemExit, r"text targets \['newlane'\] this check does not know"):
            ltc.lane_specs(d)

    def test_a_vllm_lane_it_does_not_know_stops_it(self):
        d = self.copy()
        (d / "install-newlane.sh").write_text('vlane_put_unit "$RENDER"\n')
        with self.assertRaisesRegex(SystemExit, "add the new lane to VLLM_LANES"):
            ltc.lane_specs(d)

    def test_an_installer_that_serves_its_template_otherwise_stops_it(self):
        d = self.copy()
        f = d / "install-ornith-122b.sh"
        f.write_text(f.read_text().replace("TEMPLATE_FIXES=(--patches=system-reminder,tool-reference)", "TEMPLATE_FIXES=()"))
        with self.assertRaisesRegex(SystemExit, "install-ornith-122b.sh serves its template otherwise"):
            ltc.lane_specs(d)

    def edit(self, old, new):
        d = self.copy()
        f = d / "install-ornith-122b.sh"
        text = f.read_text()
        self.assertIn(old, text)
        f.write_text(text.replace(old, new))
        return d

    def test_an_installer_that_runs_the_patcher_itself_stops_it(self):
        # it would read as serving its template as published, however the call is written (code
        # review, 2026-10-24)
        calls = ['python3 "$HERE/patch-template.py" "$HF_CACHE" "$TEMPLATE" "$MODEL_REV" "$MODEL"',
                 'python3 "${HERE}/patch-template.py" "$HF_CACHE" "$TEMPLATE"',
                 'python3 -I "$HERE/patch-template.py" "$HF_CACHE" "$TEMPLATE"',
                 '"$HERE/patch-template.py" "$HF_CACHE" "$TEMPLATE"',
                 'python3 "$HERE"/patch-template.py "$HF_CACHE" "$TEMPLATE"',
                 'python3 \\\n  "$HERE/patch-template.py" "$HF_CACHE" "$TEMPLATE"']
        for call in calls:
            with self.subTest(call=call):
                d = self.edit('TEMPLATE_FIXES=(--patches=system-reminder,tool-reference)\n', call + "\n")
                with self.assertRaisesRegex(SystemExit, "runs patch-template.py itself"):
                    ltc.lane_specs(d)

    def test_fixes_set_twice_stop_it(self):
        d = self.edit("TEMPLATE_FIXES=(--patches=system-reminder,tool-reference)\n",
                      "TEMPLATE_FIXES=(--patches=system-reminder,tool-reference)\nTEMPLATE_FIXES+=(LEAN_DEFAULT=0)\n")
        with self.assertRaisesRegex(SystemExit, "sets TEMPLATE_FIXES more than once"):
            ltc.lane_specs(d)

    def test_the_27b_and_flash_patcher_calls_are_read_too(self):
        # the check gives their targets the five fixes: a call of install.sh or switch-model.sh with fixes
        # of its own would pass it unseen, as the first push of this release did (code review, 2026-10-24)
        for script in ("install.sh", "switch-model.sh"):
            with self.subTest(script=script):
                d = self.copy()
                f = d / script
                text = f.read_text()
                self.assertIn('python3 "$REPO_DIR/patch-template.py"', text)
                f.write_text(text.replace('python3 "$REPO_DIR/patch-template.py"',
                                          'python3 "$REPO_DIR/patch-template.py" --patches=system-reminder', 1))
                with self.assertRaisesRegex(SystemExit, f"{script} runs patch-template.py otherwise"):
                    ltc.lane_specs(d)

    def test_the_patcher_in_a_preflight_list_is_no_call(self):
        self.assertEqual({name for name, _, _, env in ltc.lane_specs() if env and "patches" in env}, {"ornith-122b"})

    def test_fixes_left_out_of_a_call_stop_it(self):
        d = self.edit('vlane_template_if_cached "$TEMPLATE" "${TEMPLATE_FIXES[@]}"', 'vlane_template_if_cached "$TEMPLATE"')
        with self.assertRaisesRegex(SystemExit, "does not pass them all"):
            ltc.lane_specs(d)

    def test_an_option_it_cannot_pass_on_stops_it(self):
        d = self.edit("TEMPLATE_FIXES=(--patches=system-reminder,tool-reference)", "TEMPLATE_FIXES=(--patches=system-reminder,tool-reference --keep=x)")
        with self.assertRaisesRegex(SystemExit, "holds --keep=x"):
            ltc.lane_specs(d)


class WhatItRendersAsTheEnginesDo(unittest.TestCase):
    # three ways the check rendered otherwise than the engines and the installers (code review, 2026-10-24)
    def test_named_templates_give_tool_use_when_tools_are_sent(self):
        named = {"default": "D", "tool_use": "T"}
        self.assertEqual((ltc.pick(named, None), ltc.pick(named, ltc.TOOLS)), ("D", "T"))
        self.assertEqual(ltc.pick({"default": "D"}, ltc.TOOLS), "D")
        self.assertEqual(ltc.pick("one", ltc.TOOLS), "one")

    def test_each_content_also_goes_as_a_list_of_parts(self):
        m = ltc.messages(["system", "user", "assistant_tool_call", "tool"], parts=True)
        self.assertEqual(m[1]["content"], [{"type": "text", "text": "A user message, number 1."}])
        self.assertEqual(m[3]["content"], [{"type": "text", "text": "7351"}])
        env = ltc.environment()
        strings_only = "{% for m in messages %}{{ m.content + '!' }}{% endfor %}"
        self.assertIsNone(ltc.render(env, strings_only, ["user"], None))
        self.assertIsNotNone(ltc.render(env, strings_only, ["user"], None, parts=True))

    def test_a_patched_lane_needs_the_file_its_installer_patches(self):
        with self.assertRaisesRegex(SystemExit, "its installer patches chat_template.jinja"):
            ltc.served("org/x", "a" * 40, ("tpl", "tokenizer_config.json"), {"patches": "system-reminder"})
        self.assertEqual(ltc.served("org/x", "a" * 40, ("tpl", "tokenizer_config.json"), None), "tpl")

    def test_a_429_is_waited_out_and_a_404_is_none(self):
        import io
        import urllib.error
        calls, slept = [], []

        def urlopen(url, timeout=60):
            calls.append(url)
            if url.endswith("chat_template.jinja") and len(calls) < 3:
                raise urllib.error.HTTPError(url, 429, "busy", {"Retry-After": "7"}, None)
            if url.endswith("chat_template.jinja"):
                return io.BytesIO(b"TEMPLATE")
            raise urllib.error.HTTPError(url, 404, "no", {}, None)
        real = ltc.urllib.request.urlopen
        ltc.urllib.request.urlopen = urlopen
        try:
            self.assertEqual(ltc.fetch("org/x", "a" * 40, sleep=slept.append), ("TEMPLATE", "chat_template.jinja"))
        finally:
            ltc.urllib.request.urlopen = real
        # chat_template.jinja three times, then the listing of additional_chat_templates/, which is not there
        self.assertEqual((len(calls), slept), (4, [7, 7]))
        self.assertTrue(calls[3].endswith("/tree/" + "a" * 40 + "/additional_chat_templates"), calls[3])

    def test_a_429_waits_for_the_end_of_hugging_faces_window(self):
        # its quotas count over windows of five minutes and its 429 says when the window ends, in its
        # RateLimit header: the doubling waits gave up after 30 s (code review, 2026-10-24)
        self.assertEqual(ltc.server_wait({"RateLimit": '"resolvers";r=0;t=55'}), 56)
        self.assertEqual(ltc.server_wait({"RateLimit": '"resolvers";r=0;t=3600', "Retry-After": "9"}), ltc.WAIT_MAX)
        self.assertEqual(ltc.server_wait({"Retry-After": "9"}), 9)
        self.assertIsNone(ltc.server_wait({"Retry-After": "Fri, 31 Oct 2026 07:28:00 GMT"}))
        self.assertIsNone(ltc.server_wait(None))
        import email.message
        import urllib.error
        headers = email.message.Message()
        headers["ratelimit"] = '"resolvers";r=0;t=120'
        slept = []

        def urlopen(url, timeout=60):
            raise urllib.error.HTTPError(url, 429, "busy", headers, None)
        real = ltc.urllib.request.urlopen
        ltc.urllib.request.urlopen = urlopen
        try:
            with self.assertRaises(urllib.error.HTTPError):
                ltc.fetch("org/x", "a" * 40, tries=3, sleep=slept.append)
        finally:
            ltc.urllib.request.urlopen = real
        self.assertEqual(slept, [121, 121])


class TheNamedTemplatesOfARepo(unittest.TestCase):
    # transformers reads additional_chat_templates/ beside chat_template.jinja (second review, 2026-10-24)
    def fetched(self, files):
        import io
        import json as js
        import urllib.error
        base = "https://huggingface.co/org/x/resolve/" + "a" * 40 + "/"

        def urlopen(url, timeout=60):
            if url.endswith("/tree/" + "a" * 40 + "/additional_chat_templates"):
                listing = [{"type": "file", "path": p} for p in files if p.startswith("additional_chat_templates/")]
                if listing:
                    return io.BytesIO(js.dumps(listing + [{"type": "directory", "path": "x"}]).encode())
            elif url.startswith(base) and url[len(base):] in files:
                return io.BytesIO(files[url[len(base):]].encode())
            raise urllib.error.HTTPError(url, 404, "no", {}, None)
        real = ltc.urllib.request.urlopen
        ltc.urllib.request.urlopen = urlopen
        try:
            return ltc.fetch("org/x", "a" * 40, sleep=lambda s: None)
        finally:
            ltc.urllib.request.urlopen = real

    def test_named_templates_join_the_default(self):
        got = self.fetched({"chat_template.jinja": "D", "additional_chat_templates/tool_use.jinja": "T"})
        self.assertEqual(got, ({"default": "D", "tool_use": "T"}, "chat_template.jinja"))
        self.assertEqual(ltc.pick(got[0], ltc.TOOLS), "T")

    def test_a_default_jinja_among_the_named_ones_wins_as_in_transformers(self):
        got = self.fetched({"chat_template.jinja": "D", "additional_chat_templates/default.jinja": "X"})
        self.assertEqual(got, ("X", "chat_template.jinja"))

    def test_named_ones_alone(self):
        got = self.fetched({"additional_chat_templates/tool_use.jinja": "T"})
        self.assertEqual(got, ({"tool_use": "T"}, "additional_chat_templates"))

    def test_a_default_alone_stays_a_string(self):
        self.assertEqual(self.fetched({"chat_template.jinja": "D"}), ("D", "chat_template.jinja"))

    def test_a_patched_lane_serves_its_patched_file_alone(self):
        # its installer hands the engine that one file with --chat-template, for every request: a named
        # template of the repo is never used there (second review, 2026-10-24)
        pts = importlib.util.spec_from_file_location("pt_for_named", REPO / "patch-template.py")
        pt = importlib.util.module_from_spec(pts)
        pts.loader.exec_module(pt)
        body = "HEAD\n" + pt.SYSTEM_ANCHOR + "\nTAIL\n"
        got = ltc.served("org/x", "a" * 40, ({"default": body, "tool_use": "T"}, "chat_template.jinja"),
                         {"patches": "system-reminder"})
        self.assertEqual(got, body.replace(pt.SYSTEM_ANCHOR, pt.SYSTEM_PATCHED))
        self.assertEqual(ltc.pick(got, ltc.TOOLS), got)


class TheContentFormEachEngineHands(unittest.TestCase):
    # Each engine reads the template, then hands it the messages its own way, read in their sources:
    # rendering lists everywhere refused Kolibri's templates, which neither engine hands a list, and the
    # check rendered a tool's answer as a list, which neither engine does (2026-10-24)
    def setUp(self):
        self.env = ltc.environment()

    def test_vllm_reads_openai_from_a_template_that_loops_over_the_content(self):
        loops = "{% for m in messages %}{% for c in m['content'] %}{{ c.text }}{% endfor %}{% endfor %}"
        concat = "{% for m in messages %}{{ m['content'] + '!' }}{% endfor %}"
        self.assertTrue(ltc.vllm_takes_parts(loops, self.env))
        self.assertFalse(ltc.vllm_takes_parts(concat, self.env))

    def test_vllm_follows_the_content_into_a_macro_after_v0_29(self):
        macro = ("{% macro show(body) %}{% for part in body %}{{ part.text }}{% endfor %}{% endmacro %}"
                 "{% for m in messages %}{{ show(m.content) }}{% endfor %}")
        self.assertTrue(ltc.vllm_takes_parts(macro, self.env))
        self.assertFalse(ltc.vllm_takes_parts(macro, self.env, macros=False))
        in_macro = ("{% macro show(content) %}{% for part in content %}{{ part }}{% endfor %}{% endmacro %}"
                    "{% for m in messages %}{{ m.content }}{% endfor %}")
        self.assertFalse(ltc.vllm_takes_parts(in_macro, self.env))
        self.assertTrue(ltc.vllm_takes_parts(in_macro, self.env, macros=False))

    def test_vllm_gives_its_default_where_its_assertions_fail(self):
        # messages put into a namespace, or a loop over them with two targets: vLLM's assert fails and
        # its detector falls back to "string", where a loop over the content would have said "openai"
        ns = ("{% set ns = namespace(msgs=[]) %}{% set ns.msgs = messages %}"
              "{% for m in messages %}{% for c in m.content %}{{ c.text }}{% endfor %}{% endfor %}")
        two = "{% for i, m in messages %}{% for c in m.content %}{{ c.text }}{% endfor %}{% endfor %}"
        self.assertFalse(ltc.vllm_takes_parts(ns, self.env))
        self.assertFalse(ltc.vllm_takes_parts(two, self.env))

    def test_sglang_reads_openai_more_simply(self):
        loops = "{% for message in messages %}{% for c in message.content %}{{ c.text }}{% endfor %}{% endfor %}"
        self.assertTrue(ltc.sglang_takes_parts(loops, self.env))
        self.assertFalse(ltc.sglang_takes_parts("{{ messages[0].content }}", self.env))
        self.assertTrue(ltc.sglang_takes_parts("{# image #}{{ messages[0].content }}", self.env))

    def test_what_vllm_hands_a_template(self):
        sent = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": ""},
                {"role": "assistant", "content": None}, {"role": "tool", "content": [{"type": "text", "text": "a"},
                                                                                    {"type": "text", "text": "b"}]}]
        self.assertEqual([m["content"] for m in ltc.vllm_hands(sent, True)],
                         [[{"type": "text", "text": "hi"}], [{"type": "text", "text": ""}], [], "a\nb"])
        self.assertEqual([m["content"] for m in ltc.vllm_hands(sent, False)], ["hi", "", "", "a\nb"])

    def test_what_sglang_hands_a_template(self):
        parts = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        sent = [{"role": "user", "content": "hi"}, {"role": "user", "content": parts},
                {"role": "assistant", "content": None, "name": None}, {"role": "tool", "content": parts}]
        self.assertEqual(ltc.sglang_hands(sent, True), [{"role": "user", "content": "hi"}, {"role": "user", "content": parts},
                                                        {"role": "assistant", "content": ""}, {"role": "tool", "content": "a b"}])
        self.assertEqual([m["content"] for m in ltc.sglang_hands(sent, False)], ["hi", "a b", "", "a b"])

    def test_kolibris_vllm_reads_no_macro(self):
        self.assertEqual(ltc.VLLM_WITHOUT_MACROS, {"kolibri", "kolibri-nvfp4"})
        self.assertIn("vllm0.29.0", (ltc.REPO / "install-kolibri.sh").read_text())

    def test_the_generation_tag_of_transformers_renders_its_body(self):
        tpl = "{% for m in messages %}{% generation %}{{ m.content }}{% endgeneration %}{% endfor %}"
        self.assertEqual(self.env.from_string(tpl).render(messages=[{"content": "ok"}]), "ok")


class ItsOwnFaults(unittest.TestCase):
    # its conversion of the messages stood outside the canary, and a fault in it read as the template's
    # refusal (code review, 2026-10-24)
    def test_a_conversion_that_loses_the_messages_stops_it_at_the_canary(self):
        real = ltc.vllm_hands
        ltc.vllm_hands = lambda msgs, openai: []
        try:
            with self.assertRaisesRegex(SystemExit, "canary"):
                ltc.main()
        finally:
            ltc.vllm_hands = real

    def test_a_fault_of_the_conversion_is_no_refusal(self):
        env = ltc.environment()
        with self.assertRaises(KeyError):
            ltc.render(env, "{{ messages }}", ["user"], None, False, lambda msgs: {}["missing"])

    def test_a_tool_call_sent_as_parts_has_null_content(self):
        m = ltc.messages(["user", "assistant_tool_call", "tool"], parts=True)
        self.assertIsNone(m[1]["content"])
        self.assertEqual(ltc.vllm_hands(m, True)[1]["content"], [])
        self.assertEqual(ltc.sglang_hands(m, True)[1]["content"], "")


class TheShapesItRenders(unittest.TestCase):
    def test_opencodes_two_system_messages_are_there(self):
        shapes = json.loads(ltc.SHAPES.read_text())["shapes"]
        self.assertIn(["system", "system", "user"], [s["roles"] for s in shapes])

    def test_opencodes_second_request_is_there(self):
        # two user messages in a row after its two system messages, captured on 2026-10-24 and missing
        # from the first fixture: a template that wants roles to alternate would refuse it
        shapes = json.loads(ltc.SHAPES.read_text())["shapes"]
        self.assertIn(["system", "system", "user", "user"], [s["roles"] for s in shapes])

    def test_a_tool_round_is_a_call_and_its_answer(self):
        m = ltc.messages(["system", "user", "assistant_tool_call", "tool"])
        self.assertEqual([x["role"] for x in m], ["system", "user", "assistant", "tool"])
        self.assertEqual(m[3]["tool_call_id"], m[2]["tool_calls"][0]["id"])
        self.assertIsInstance(m[2]["tool_calls"][0]["function"]["arguments"], dict)


if __name__ == "__main__":
    unittest.main()
