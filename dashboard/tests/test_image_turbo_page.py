#!/usr/bin/env python3
"""The image lane's Turbo target on the page: a variant of the lane like flash-nvda of the
flash, loaded the same way, and an Image view that stops offering a step count the
checkpoint ignores (its eight-step sigma grid is in its model_index.json; the cookbook says
to leave num_inference_steps out). Same harness as test_page_behaviour.py."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_page_behaviour import run  # noqa: E402

IMG = "'qwen38-image.service'"


def lanes(image_state, image_target, text_state="stopped"):
    return (f"feed({{units: UNITS, lifecycle: life({{'qwen38-sglang.service': eng('{text_state}'), "
            f"'qwen38-flash.service': eng('stopped', {{target: 'flash'}}), "
            f"{IMG}: eng('{image_state}', {{target: '{image_target}'}})}})}});\n")


class TheTargetIsALaneVariant(unittest.TestCase):
    def test_it_belongs_to_the_image_lane_and_has_a_name_of_its_own(self):
        r = run(self, r"""
        report({unit: TARGET_UNIT('image-turbo'), base: TARGET_UNIT('image'), list: LANE_TARGETS[IMAGE_UNIT],
                name: TARGET_NAME['image-turbo'], button: TARGET_SHORT_NAME['image-turbo'], note: TARGET_NOTE['image-turbo'] || ''});
        """)
        self.assertEqual(r["unit"], "qwen38-image.service")
        self.assertEqual(r["base"], "qwen38-image.service")
        self.assertEqual(r["list"], ["image", "image-turbo"])
        self.assertEqual(r["name"], "Qwen-Image 2.1 Turbo · Qwen")         # its official name (decision 1180)
        self.assertEqual(r["button"], "Turbo")
        self.assertIn("eight", r["note"])

    def test_the_switch_phrase_names_it(self):
        r = run(self, r"""
        report([actionPhrase('switch', {target: 'image'}), actionPhrase('switch', {target: 'image-turbo'})]);
        """)
        self.assertNotEqual(r[0], r[1])
        self.assertIn("Turbo", r[1])


class LoadingIt(unittest.TestCase):
    """The journey is the lanes' own: point the unit at the checkpoint, then restart it when
    it serves the other one, or stop the serving lane and start it."""

    def test_from_the_base_serving_it_is_a_switch_then_a_restart(self):
        r = run(self, lanes("ready", "image") + r"""
        report(laneJourneySteps('image-turbo').map(s => s.name + (s.params.verb ? ':' + s.params.verb : '')));
        """)
        self.assertEqual(r, ["switch", "unit:restart"])

    def test_from_a_text_lane_it_is_a_switch_a_stop_and_a_start(self):
        r = run(self, lanes("stopped", "image", text_state="ready") + r"""
        report(laneJourneySteps('image-turbo').map(s => s.name + (s.params.verb ? ':' + s.params.verb + ' ' + s.params.unit : '')));
        """)
        self.assertEqual(r, ["switch", "unit:stop qwen38-sglang.service", "unit:start qwen38-image.service"])

    def test_a_lane_not_prepared_yet_builds_its_runtime_first(self):
        # installed on demand (2026-10-14): its units at install, its runtime at its first Load
        r = run(self, lanes("stopped", "image", text_state="ready") + r"""
        feed({lanes_ready: {image: false, video: true, kolibri: null}});
        const steps = laneJourneySteps('image');
        report({steps: steps.map(s => s.name + (s.params.verb ? ':' + s.params.verb : '') + (s.name === 'prepare' ? ' ' + laneOfTarget(s.params.target) : '')),
                title: steps[0].title, cmd: argvFor(steps[0].name, steps[0].params).join(' ')});
        """)
        self.assertEqual(r["steps"], ["prepare image", "switch", "unit:stop", "unit:start"])
        self.assertIn("runtime", r["title"])
        self.assertEqual(r["cmd"], "bash prepare-lane.sh image")

    def test_the_cards_say_its_first_load_builds_it(self):
        r = run(self, lanes("stopped", "image", text_state="ready") + r"""
        feed({lanes_ready: {image: false, video: true, kolibri: null}});
        showView('now'); showView('lanes');
        report({now: document.body.textContent.includes('built at its first Load'),
                lanes: document.body.textContent.includes('built at its first Load: a venv and SGLang Diffusion')});
        """)
        self.assertEqual(r, {"now": True, "lanes": True})

    def test_the_flash_lane_not_prepared_is_prepared_first_too(self):
        r = run(self, lanes("stopped", "image", text_state="ready") + r"""
        feed({lanes_ready: {image: true, video: true, kolibri: null, flash: false}});
        const steps = laneJourneySteps('flash');
        report({steps: steps.map(s => s.name + (s.name === 'prepare' ? ' ' + laneOfTarget(s.params.target) : '')), desc: steps[0].desc,
                cmd: argvFor(steps[0].name, steps[0].params).join(' ')});
        """)
        self.assertEqual(r["steps"][0], "prepare flash")
        self.assertIn("switch", r["steps"])
        self.assertIn("checkpoint (about 136 GB)", r["desc"])
        self.assertNotIn("The next step fetches", r["desc"])
        self.assertEqual(r["cmd"], "bash prepare-lane.sh flash flash")

    def test_a_first_load_prepares_the_target_it_loads(self):
        # the NVIDIA export loaded first: its preparation fetches that one, not the default the
        # launcher names (found in review, 2026-10-15: the default came first, 135 GB)
        r = run(self, lanes("stopped", "image", text_state="ready") + r"""
        feed({lanes_ready: {image: true, video: true, kolibri: null, flash: false}});
        const steps = laneJourneySteps('flash-nvda');
        report({prep: steps[0].params, cmd: argvFor(steps[0].name, steps[0].params).join(' '),
                switch: (steps.find(s => s.name === 'switch') || {}).params});
        """)
        self.assertEqual(r["prep"], {"target": "flash-nvda"})
        self.assertEqual(r["cmd"], "bash prepare-lane.sh flash flash-nvda")
        self.assertEqual(r["switch"], {"target": "flash-nvda"})

    def test_the_27b_beside_a_serving_flash_not_prepared_is_prepared_first_too(self):
        r = run(self, lanes("stopped", "image") + r"""
        feed({lanes_ready: {image: true, video: true, kolibri: null, flash: true, '27b': false}});
        const steps = laneJourneySteps('stock');
        report({steps: steps.map(s => s.name + (s.name === 'prepare' ? ' ' + laneOfTarget(s.params.target) : '')), desc: steps[0].desc,
                cmd: argvFor(steps[0].name, steps[0].params).join(' '), start: !LCARDS.has(U27) || true});
        """)
        self.assertEqual(r["steps"][0], "prepare 27b")
        self.assertIn("its DFlash2 draft (about 2 GB) and its checkpoint", r["desc"])
        self.assertNotIn("The next step fetches", r["desc"])
        self.assertEqual(r["cmd"], "bash prepare-lane.sh 27b stock")

    def test_the_window_does_not_time_a_first_load_by_its_boot_alone(self):
        # "4 steps, about 90 s in all" stood over 8 minutes of preparation and download on the
        # reference box (2026-10-15): the page can time the boot, not the downloads
        def status(ready):
            return run(self, lanes("stopped", "image", text_state="ready") + r"""
            feed({lanes_ready: {image: %s, video: true, kolibri: null}});
            askJourney('image');
            report($('sh-status').textContent);
            """ % ready)
        first = status("false")
        self.assertIn("its first Load downloads first", first)
        self.assertIn("then it answers in about", first)
        self.assertNotIn("in all", first)
        self.assertRegex(status("true"), r"^\d+ steps, about .+ in all$")

    def test_a_lane_not_prepared_offers_its_load_and_no_start(self):
        # Start as installed would start a unit with nothing to run (the flash lane's launcher
        # would pull its image inside the start, then find no checkpoint): its Load prepares it
        # first, and that is the one verb it offers
        r = run(self, lanes("stopped", "image") + r"""
        feed({lanes_ready: {image: false, video: true, kolibri: null, flash: false}});
        showView('lanes');
        const st = u => { const c = LCARDS.get(u); return {start: !c.start.hidden, load: !c.load.hidden}; };
        report({image: st(IMAGE_UNIT), flash: st(UFLASH), video: st(VIDEO_UNIT), text: st(U27)});
        """)
        self.assertEqual(r["image"], {"start": False, "load": True})
        self.assertEqual(r["flash"], {"start": False, "load": True})
        self.assertEqual(r["video"], {"start": True, "load": True})
        self.assertEqual(r["text"], {"start": True, "load": True})

    def test_a_lane_prepared_and_not_switched_to_offers_its_load_and_no_start(self):
        # its preparation done, its switch not (the page closed during a Load): a Start would
        # run a unit with no checkpoint or chat template yet, crash-looping, and its Conflicts=
        # stopped the lane that served (found in review, 2026-10-15)
        r = run(self, lanes("stopped", "image") + r"""
        feed({lanes_ready: {image: true, video: true, kolibri: null, flash: true},
              lanes_startable: {image: false, video: true, kolibri: null, flash: true}});
        showView('lanes');
        const st = u => { const c = LCARDS.get(u); return {start: !c.start.hidden, load: !c.load.hidden}; };
        report({image: st(IMAGE_UNIT), video: st(VIDEO_UNIT), flash: st(UFLASH)});
        """)
        self.assertEqual(r["image"], {"start": False, "load": True})
        self.assertEqual(r["video"], {"start": True, "load": True})
        self.assertEqual(r["flash"], {"start": True, "load": True})

    def test_a_prepared_lane_loads_as_before(self):
        r = run(self, lanes("stopped", "image", text_state="ready") + r"""
        feed({lanes_ready: {image: true, video: true, kolibri: null}});
        report(laneJourneySteps('image').map(s => s.name));
        """)
        self.assertNotIn("prepare", r)

    def test_already_serving_it_is_nothing_to_do(self):
        r = run(self, r"""
        feed({units: {units: Object.assign({}, UNITS.units, {'qwen38-sglang.service': {active: 'inactive', enabled: 'disabled'},
                                                             'qwen38-image.service': {active: 'active', enabled: 'enabled'}})},
              lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image-turbo'})})});
        report(laneJourneySteps('image-turbo').length);
        """)
        self.assertEqual(r, 0)


class TheImageViewUnderTheTurbo(unittest.TestCase):
    def test_the_request_leaves_the_step_count_out_and_the_slider_says_why(self):
        r = run(self, lanes("ready", "image-turbo") + r"""
        $('img-prompt').value = 'a cat'; $('img-steps').value = '40'; imgSync();
        report({payload: imgPayload(), steps: imgFormRequest().steps, disabled: $('img-steps').disabled,
                shown: txt('img-steps-o'), hint: txt('img-steps-hint'), problem: imgProblem()});
        """)
        self.assertNotIn("num_inference_steps", r["payload"])
        self.assertEqual(r["steps"], 8)
        self.assertTrue(r["disabled"])
        self.assertEqual(r["shown"], "8")
        self.assertIn("fixed at 8", r["hint"])
        self.assertEqual(r["problem"], "")

    def test_the_estimate_counts_eight_steps(self):
        r = run(self, lanes("ready", "image-turbo") + r"""
        $('img-steps').value = '40'; const turbo = imgEstimate();
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})})});
        const base = imgEstimate();
        report({turbo, base});
        """)
        self.assertLess(r["turbo"], r["base"] / 4)

    def test_the_thumb_says_eight_and_the_base_gets_its_setting_back(self):
        """Seen on the reference box under the Turbo: the slider's thumb on the base's 40,
        beside the 8 it printed."""
        r = run(self, lanes("ready", "image") + r"""
        $('img-steps').value = '33'; imgSync();
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image-turbo'})})});
        imgSync(); const turbo = $('img-steps').value;
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})})});
        imgSync(); report({turbo, base: $('img-steps').value, payload: imgPayload().num_inference_steps});
        """)
        self.assertEqual(r["turbo"], "8")
        self.assertEqual(r["base"], "33")
        self.assertEqual(r["payload"], 33)

    def test_a_reset_under_the_turbo_keeps_the_thumb_on_eight(self):
        r = run(self, lanes("ready", "image-turbo") + r"""
        $('img-reset').click(); report($('img-steps').value);
        """)
        self.assertEqual(r, "8")

    def test_the_base_keeps_its_slider_and_its_setting(self):
        r = run(self, lanes("ready", "image") + r"""
        $('img-prompt').value = 'a cat'; $('img-steps').value = '33'; imgSync();
        report({payload: imgPayload(), disabled: $('img-steps').disabled, shown: txt('img-steps-o'), hint: txt('img-steps-hint')});
        """)
        self.assertEqual(r["payload"]["num_inference_steps"], 33)
        self.assertFalse(r["disabled"])
        self.assertEqual(r["shown"], "33")
        self.assertEqual(r["hint"], "model default 40")

    def test_the_load_button_loads_the_checkpoint_the_unit_names(self):
        r = run(self, lanes("stopped", "image-turbo", text_state="ready") + r"""
        const asked = []; askJourney = t => asked.push(t);
        imgRenderLane();
        const b = [...document.querySelectorAll('button')].find(x => x.textContent === 'Load Qwen-Image');
        if (b) b.click();
        report(asked);
        """)
        self.assertEqual(r, ["image-turbo"])


class WhatTheEngineServes(unittest.TestCase):
    """The unit's file says what the lane's next start loads; the engine's own /model_info,
    read by the cockpit once per life of the unit (served_target), says what it serves. A
    switch from a terminal rewrites the unit under a serving engine: the page sent the
    Turbo's request to the base and found no restart owed (found in review, 2026-10-10)."""

    def served(self, unit_target, served_target, body):
        return run(self, r"""
        feed({units: {units: Object.assign({}, UNITS.units, {'qwen38-sglang.service': {active: 'inactive', enabled: 'disabled'},
                                                             'qwen38-image.service': {active: 'active', enabled: 'enabled'}})},
              lifecycle: life({'qwen38-sglang.service': eng('stopped'),
                               'qwen38-image.service': eng('ready', {target: '%s', served_target: '%s'})})});
        """ % (unit_target, served_target) + body)

    def test_a_unit_switched_under_the_engine_owes_its_restart(self):
        r = self.served("image-turbo", "image", r"""
        report({steps: laneJourneySteps('image-turbo').map(s => s.name + (s.params.verb ? ':' + s.params.verb : '')),
                banners: txt('banners'), turbo: imgTurbo()});
        """)
        self.assertEqual(r["steps"], ["unit:restart"])
        self.assertIn("Qwen-Image 2.1 · Qwen is serving, and the lane's next start loads Qwen-Image 2.1 Turbo · Qwen.", r["banners"])
        self.assertFalse(r["turbo"], "the page took the unit's word over the engine's")

    def test_the_page_follows_the_engine(self):
        r = self.served("image", "image-turbo", r"""
        $('img-prompt').value = 'a cat'; $('img-steps').value = '40'; imgSync();
        report({payload: imgPayload(), turbo: imgTurbo(), steps: laneJourneySteps('image').map(s => s.name + (s.params.verb ? ':' + s.params.verb : ''))});
        """)
        self.assertTrue(r["turbo"])
        self.assertNotIn("num_inference_steps", r["payload"])
        self.assertEqual(r["steps"], ["unit:restart"])

    def test_serving_what_the_unit_names_owes_nothing(self):
        r = self.served("image-turbo", "image-turbo", r"""
        report({n: laneJourneySteps('image-turbo').length, banners: txt('banners')});
        """)
        self.assertEqual(r["n"], 0)
        self.assertNotIn("next start loads", r["banners"])

    def test_pointing_the_unit_at_what_the_engine_serves_restarts_nothing(self):
        """The terminal switch the other way: the engine serves the Turbo, its unit names the
        base. Loading the Turbo points the unit back at it, and a restart would only end a
        generation in flight."""
        r = self.served("image", "image-turbo", r"""
        report(laneJourneySteps('image-turbo').map(s => s.name + (s.params.verb ? ':' + s.params.verb : '')));
        """)
        self.assertEqual(r, ["switch"])

    def test_restarting_it_on_the_other_checkpoint_says_a_generation_is_lost(self):
        r = self.served("image", "image", r"""
        askJourney('image-turbo');
        report({warns: txt('sh-warns'), steps: [...$('sh-journey').children].map(li => li.textContent)});
        """)
        self.assertIn("A generation in flight on Qwen-Image", r["warns"])
        self.assertTrue(any("A generation in flight is lost." in s for s in r["steps"]), r["steps"])


class TheSampleAndTheResetUnderTheTurbo(unittest.TestCase):
    def test_the_sample_leaves_the_steps_to_the_grid_and_the_reset_says_eight(self):
        r = run(self, lanes("ready", "image-turbo") + r"""
        let sent = null;
        __fetch = async (url, init) => url === '/api/csrf' ? __response(200, {token: 't'})
          : url === '/api/image/generate' ? (sent = JSON.parse(init.body), __response(200, {data: [{b64_json: 'AAAA'}]}))
          : url === '/api/image' ? __response(200, {available: true, installed: true, progress: {}}) : new Promise(() => {});
        await imgSample();
        $('img-reset').click();
        report({sent, toast: toastText()});
        """)
        self.assertIsNotNone(r["sent"])
        self.assertNotIn("num_inference_steps", r["sent"])
        self.assertIn("the Turbo’s 8 steps", r["toast"])
        self.assertNotIn("40 steps", r["toast"])

    def test_the_base_sample_keeps_its_twenty_steps(self):
        r = run(self, lanes("ready", "image") + r"""
        let sent = null;
        __fetch = async (url, init) => url === '/api/csrf' ? __response(200, {token: 't'})
          : url === '/api/image/generate' ? (sent = JSON.parse(init.body), __response(200, {data: [{b64_json: 'AAAA'}]}))
          : url === '/api/image' ? __response(200, {available: true, installed: true, progress: {}}) : new Promise(() => {});
        await imgSample();
        $('img-reset').click();
        report({sent, toast: toastText()});
        """)
        self.assertEqual(r["sent"]["num_inference_steps"], 20)
        self.assertIn("40 steps", r["toast"])


class WhatThePageSaysOfEach(unittest.TestCase):
    def test_the_now_card_times_the_checkpoint_it_loads(self):
        r = run(self, lanes("stopped", "image-turbo", text_state="ready") + r"""
        renderRack(); const turbo = txt('view-now');
        feed({lifecycle: life({'qwen38-sglang.service': eng('ready'), 'qwen38-image.service': eng('stopped', {target: 'image'})})});
        renderRack(); report({turbo, base: txt('view-now')});
        """)
        self.assertIn("1024 image about 7.5 s", r["turbo"])
        self.assertIn("1024 image about 34 s", r["base"])
        self.assertNotIn("38 s", r["turbo"] + r["base"])

    def test_the_switch_says_what_it_rewrites_for_each_lane(self):
        r = run(self, r"""
        report(['stock', 'image', 'image-turbo', 'video'].map(t => EXPLAIN.switch({target: t}).split('\n\n').pop()));
        """)
        self.assertIn("rewrites its unit, the proxy ceiling and the opencode default model", r[0])
        for x in r[1:3]:
            self.assertIn("points its unit at this checkpoint", x)
            self.assertNotIn("proxy", x)
        self.assertIn("changes nothing else", r[3])


if __name__ == "__main__":
    unittest.main()
