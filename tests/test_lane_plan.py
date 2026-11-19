#!/usr/bin/env python3
"""The one lane-load plan: what a Load does, in order, from the state the cockpit has.

The page used to build this plan and send each step, so a sign-out between two steps
could leave the box without a serving engine. The plan now lives in lane_plan.py and
the cockpit runs it as one job; these tests hold the order and the cases that matter.
"""
import unittest

import lane_plan


def engine(state="ready", target=None, served=None):
    return {"state": state, "target": target, "served_target": served}


def state(engines=None, units=None, serving=None, boot=None, ready=None, startable=None, owed=None):
    return {"engines": engines or {}, "units": units or {}, "serving": serving, "boot": boot,
            "lanes_ready": ready or {}, "lanes_startable": startable or {}, "update_owed": owed or {}}


class ALaneLoadIsOneOrderedPlan(unittest.TestCase):
    def test_a_lane_already_serving_what_was_asked_loads_nothing(self):
        s = state({"qwen38-sglang.service": engine(target="stock", served="stock")},
                  serving="qwen38-sglang.service", boot="qwen38-sglang.service")
        self.assertEqual(lane_plan.plan("stock", s), [])

    def test_another_target_of_the_serving_lane_switches_then_restarts_it(self):
        s = state({"qwen38-sglang.service": engine(target="stock", served="stock")},
                  serving="qwen38-sglang.service", boot="qwen38-sglang.service")
        steps = lane_plan.plan("uncensored", s)
        self.assertEqual([x["name"] for x in steps], ["switch", "unit"])
        self.assertEqual(steps[0]["params"], {"target": "uncensored"})
        self.assertEqual(steps[1]["params"], {"verb": "restart", "unit": "qwen38-sglang.service"})

    def test_another_lane_is_stopped_after_the_boot_is_pointed_at_the_new_one(self):
        s = state({"qwen38-sglang.service": engine(target="stock", served="stock"),
                   "qwen38-flash.service": engine("stopped", target="flash")},
                  serving="qwen38-sglang.service", boot="qwen38-sglang.service")
        steps = lane_plan.plan("flash", s)
        self.assertEqual([x["name"] for x in steps], ["switch", "unit", "unit"])
        self.assertEqual(steps[1]["params"], {"verb": "stop", "unit": "qwen38-sglang.service"})
        self.assertEqual(steps[2]["params"], {"verb": "start", "unit": "qwen38-flash.service"})

    def test_a_switch_not_restarted_yet_owes_only_the_restart(self):
        s = state({"qwen38-flash.service": engine(target="flash-uncensored", served="flash")},
                  serving="qwen38-flash.service", boot="qwen38-flash.service")
        steps = lane_plan.plan("flash-uncensored", s)
        self.assertEqual([x["name"] for x in steps], ["unit"])
        self.assertEqual(steps[0]["params"], {"verb": "restart", "unit": "qwen38-flash.service"})

    def test_an_update_owed_to_the_serving_lane_is_only_its_restart(self):
        s = state({"qwen38-nemotron.service": engine(target="nemotron")},
                  serving="qwen38-nemotron.service", boot="qwen38-nemotron.service",
                  owed={"qwen38-nemotron.service": "its unit changed"})
        steps = lane_plan.plan("nemotron", s)
        self.assertEqual([x["name"] for x in steps], ["unit"])
        self.assertEqual(steps[0]["params"], {"verb": "restart", "unit": "qwen38-nemotron.service"})
        self.assertIn("update installed", steps[0]["title"])

    def test_a_lane_whose_runtime_is_not_built_is_prepared_first(self):
        s = state({"qwen38-image-h3.service": engine("stopped", target="image-h3")},
                  boot="qwen38-sglang.service", ready={"image-h3": False}, startable={"image-h3": False})
        steps = lane_plan.plan("image-h3", s)
        self.assertEqual([x["name"] for x in steps], ["prepare", "switch", "unit"])
        self.assertEqual(steps[0]["params"], {"target": "image-h3"})
        self.assertEqual(steps[-1]["params"], {"verb": "start", "unit": "qwen38-image-h3.service"})

    def test_a_boot_lane_whose_clients_follow_its_window_sets_them_before_the_restart(self):
        s = state({"qwen38-flash-unsloth.service": engine(target="flash-unsloth", served="flash-unsloth")},
                  serving="qwen38-flash-unsloth.service", boot="qwen38-flash-unsloth.service",
                  owed={"qwen38-flash-unsloth.service": "its unit changed",
                        "qwen38-flash-local.service": "its unit changed"})
        # flash-unsloth is the one asked: its clients step comes first, then the restart
        steps = lane_plan.plan("flash-unsloth", s)
        self.assertEqual([x["name"] for x in steps], ["clients", "unit"])
        self.assertEqual(steps[0]["params"], {"target": "flash-unsloth"})
        self.assertEqual(steps[1]["params"], {"verb": "restart", "unit": "qwen38-flash-unsloth.service"})

    def test_an_unknown_target_is_refused(self):
        with self.assertRaises(ValueError):
            lane_plan.plan("not-a-lane", state())

    def test_speech_and_embedding_are_not_load_targets(self):
        for target in ("speech", "embedding", "embed"):
            self.assertNotIn(target, lane_plan.TARGET_UNIT)


if __name__ == "__main__":
    unittest.main()