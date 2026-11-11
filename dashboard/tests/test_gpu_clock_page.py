#!/usr/bin/env python3
"""The GPU clock cap in the Machine view (decision of 2026-11-11), run: the page's scripts in node on a
DOM built from its own markup (pagejs.py, fakedom.js), state delivered as the server's stream does.

The cap is a setting of four positions, none by default, each with what it cost and gave on the
reference box. What is held: the lit position is the cap in effect as far as the server knows, never
one that is only kept; each position asks first, in the sheet, with its exact command, its figures
and the limits of the measurement, and sends its value and nothing else; the outcome of the job is
said and the panel reads the state again; a machine without nvidia-smi, a refused sudo and nvidia-smi's
own refusal each say what happened and what to do; the arrows move the focus and never ask; and the
figures are the ones docs/cockpit.md gives. Its layout at 375 px is the browser checks' (touch-check.mjs).
"""
import json
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_page_behaviour import run  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
ARGV = {"none": ["sudo", "-n", "/usr/bin/nvidia-smi", "-rgc"],
        "2400": ["sudo", "-n", "/usr/bin/nvidia-smi", "-lgc", "0,2400"],
        "2200": ["sudo", "-n", "/usr/bin/nvidia-smi", "-lgc", "0,2200"],
        "2000": ["sudo", "-n", "/usr/bin/nvidia-smi", "-lgc", "0,2000"]}

# a state as the server's gpu collector delivers it (cockpit.py, collect_gpu and gpu_clock_state)
SETUP = r"""
const CFG = Object.assign({}, CONFIG, {gpu_clock_argv: %s});
const clock = o => Object.assign({mhz: 2411, nvidia_smi: true, kept: 'none', kept_bad: null, set: null, at: null, origin: null,
                                  tried: null, error: null, said: null}, o || {});
const gpu = o => feed({gpu: {power_w: 10.6, temp_c: 46, procs: [], clock: clock(o)}});
const panel = () => ({lit: [...$('gc-pick').querySelectorAll('button')].filter(b => b.getAttribute('aria-checked') === 'true').map(b => b.dataset.cap),
  disabled: [...$('gc-pick').querySelectorAll('button')].map(b => b.disabled), titles: [...$('gc-pick').querySelectorAll('button')].map(b => b.title),
  cap: txt('gc-cap'), capCls: $('gc-cap').className, mhz: txt('gc-mhz'), state: txt('gc-state'), line: txt('gc-line'),
  why: $('gc-why').hidden ? null : txt('gc-why')});
const opt = cap => $('gc-pick').querySelector(`button[data-cap="${cap}"]`);
feed({config: CFG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
showView('machine');
""" % json.dumps(ARGV)


def page(test, body):
    return run(test, SETUP + body)


class NoneIsTheDefaultAndEachCapSaysWhatItCost(unittest.TestCase):
    def test_none_is_lit_and_the_clock_read_now_is_said(self):
        r = page(self, "gpu(); report(panel());")
        self.assertEqual(r["lit"], ["none"])
        self.assertEqual(r["mhz"], "2,411 MHz")
        self.assertEqual(r["cap"], "no cap")
        self.assertEqual(r["state"], "none: the GPU runs at its own clocks, the factory setting")
        self.assertEqual(r["line"], "No cap, the factory setting: 105.74 tok/s at 4 streams, the GPU at 46.6 W, "
                                    "its hottest zone at 92.6 °C on the reference box.")
        self.assertIsNone(r["why"])
        self.assertEqual(r["disabled"], [False] * 4)

    def test_each_cap_says_its_throughput_its_watts_and_its_degrees(self):
        r = page(self, "gpu(); report({titles: panel().titles, rows: [...$('gc-table').tBodies[0].rows].map(tr => [...tr.cells].map(c => c.textContent)), note: txt('gc-note')});")
        self.assertEqual(r["titles"], [
            "No cap: 105.74 tok/s at 4 streams, the GPU at 46.6 W, its hottest zone at 92.6 °C on the reference box",
            "2,400 MHz: 105.87 tok/s at 4 streams, the GPU at 35.9 W, its hottest zone at 90.2 °C on the reference box",
            "2,200 MHz: 103.77 tok/s at 4 streams, the GPU at 28.1 W, its hottest zone at 86.6 °C on the reference box",
            "2,000 MHz: 103.15 tok/s at 4 streams, the GPU at 22.8 W, its hottest zone at 84.4 °C on the reference box"])
        self.assertEqual(r["rows"], [["None", "2,502 MHz", "105.74 tok/s", "46.6 W", "92.6 °C"],
                                     ["2,400 MHz", "2,387 MHz", "105.87 tok/s", "35.9 W", "90.2 °C"],
                                     ["2,200 MHz", "2,190 MHz", "103.77 tok/s", "28.1 W", "86.6 °C"],
                                     ["2,000 MHz", "1,989 MHz", "103.15 tok/s", "22.8 W", "84.4 °C"],
                                     ["None, again at the end", "2,494 MHz", "105.02 tok/s", "47.7 W", "93.2 °C"]])
        # the limits, said where the figures are
        for words in ("One lane measured, a text one", "the image, video and 27B lanes are not measured yet",
                      "The watts are the GPU’s alone, as nvidia-smi reads them", "56.20 to 54.62 tok/s", "2,077 to 2,004 tok/s"):
            self.assertIn(words, r["note"])

    def test_every_figure_is_the_one_the_docs_give(self):
        r = page(self, "report({caps: GPU_CAPS, again: GPU_CAP_AGAIN});")
        doc = (REPO / "docs" / "cockpit.md").read_text()
        rows = re.findall(r"^\| (None|None, again at the end|[\d,]+ MHz) \| ([\d,]+) MHz \| ([\d.]+) \| ([\d.]+) W \| "
                          r"([\d.]+) °C \| ([\d.]+) \| ([\d,]+) \|$", doc, re.M)
        self.assertEqual(len(rows), 5, "the docs' table is no longer where this check reads it")
        num = lambda s: float(s.replace(",", ""))   # noqa: E731
        for (cap, mhz, tps4, w, hot, tps1, prefill), m in zip(rows, r["caps"] + [r["again"]]):
            with self.subTest(cap=cap):
                want = "none" if cap.startswith("None") else cap.replace(",", "").split()[0]
                self.assertEqual(m["cap"], want)
                self.assertEqual([m["mhz"], m["tps4"], m["w"], m["hot"], m["tps1"], m["prefill"]],
                                 [num(mhz), num(tps4), num(w), num(hot), num(tps1), num(prefill)])


class EachCapAsksFirst(unittest.TestCase):
    def test_the_sheet_shows_the_exact_command_what_it_cost_and_the_limits(self):
        r = page(self, r"""
        gpu();
        const seen = {};
        for (const cap of ['2400', 'none']){
          opt(cap).click();
          seen[cap] = {open: !$('scrim').hidden, title: txt('sh-title'), cmd: txt('sh-cmd'), lede: txt('sh-lede'), go: txt('sh-go')};
          $('sh-cancel').click();
        }
        report({seen, lit: panel().lit, fetched: __fetches.map(f => f.url)});
        """)
        cap = r["seen"]["2400"]
        self.assertTrue(cap["open"])
        self.assertEqual((cap["title"], cap["cmd"], cap["go"]),
                         ("Cap the GPU clock at 2,400 MHz", "sudo -n /usr/bin/nvidia-smi -lgc 0,2400", "Cap at 2,400 MHz"))
        for words in ("105.87 tok/s at 4 streams, the GPU at 35.9 W, its hottest zone at 90.2 °C",
                      "against 105.74 tok/s, 46.6 W and 92.6 °C with no cap",
                      "until the next reboot", "sets it again at its start", "a generation in flight included",
                      "One lane measured", "The watts are the GPU’s alone"):
            self.assertIn(words, cap["lede"])
        lift = r["seen"]["none"]
        self.assertEqual((lift["title"], lift["cmd"], lift["go"]),
                         ("Lift the GPU clock cap", "sudo -n /usr/bin/nvidia-smi -rgc", "Lift the cap"))
        self.assertIn("the factory setting", lift["lede"])
        self.assertEqual(r["lit"], ["none"], "a cancelled sheet changes nothing")
        self.assertNotIn("/api/action", r["fetched"])

    def test_it_sends_the_cap_and_nothing_else(self):
        r = page(self, r"""
        __fetch = async (url, init) => url === '/api/csrf' ? __response(200, {token: 't'})
          : url === '/api/action' ? __response(202, {job: 'j1', argv: null, warnings: [], dry_run: false}) : new Promise(() => {});
        gpu();
        opt('2200').click();
        $('sh-go').click();
        await __settle(); await __settle();
        const sent = __fetches.filter(f => f.url === '/api/action').map(f => JSON.parse(f.init.body));
        report({sent, open: !$('scrim').hidden, toast: toastText()});
        """)
        self.assertEqual(r["sent"], [{"name": "gpu_clock", "params": {"cap": "2200"}, "csrf": "t"}])
        self.assertFalse(r["open"])
        self.assertIn("Cap the GPU clock at 2,200 MHz: running", r["toast"])

    def test_the_arrows_move_the_focus_and_never_ask(self):
        r = page(self, r"""
        gpu();
        opt('none').focus();
        const key = k => $('gc-pick').querySelector('button[data-cap="' + document.activeElement.dataset.cap + '"]')
                           .dispatchEvent({type: 'keydown', key: k, bubbles: true});
        const went = [];
        for (const k of ['ArrowRight', 'ArrowRight', 'ArrowLeft', 'ArrowUp']){ key(k); went.push(document.activeElement.dataset.cap); }
        report({went, open: !$('scrim').hidden});
        """)
        self.assertEqual(r["went"], ["2400", "2200", "2400", "none"])
        self.assertFalse(r["open"], "an arrow asked for a privileged change")


class TheOutcomeIsSaidAndReadAgain(unittest.TestCase):
    JOB = r"""
    const job = (id, status, result, params) => ({id, action: 'gpu_clock', params: params || {cap: '2400'}, status, origin: 'ui',
      started: Date.now() / 1000 - 2, ended: Date.now() / 1000, elapsed: 1, rc: status === 'done' ? 0 : 1,
      argv: ['sudo', '-n', '/usr/bin/nvidia-smi', '-lgc', '0,2400'], result});
    feed({job: {current: null, recent: [job('j0', 'done', {ok: true}, {cap: 'none'})]}});   // seen at load: adopted, not said
    """

    def test_a_cap_set_is_said_and_lit_with_its_time(self):
        r = page(self, self.JOB + r"""
        gpu();
        feed({job: {current: null, recent: [job('j1', 'done', {ok: true, cap: '2400', error: null, said: 'GPU clock capped at 2,400 MHz'})]}});
        const toast1 = toastText();
        gpu({kept: '2400', set: '2400', at: Date.now() / 1000, origin: 'ui', tried: '2400', said: 'GPU clock capped at 2,400 MHz'});
        report(Object.assign(panel(), {toast: toast1, dock: txt('dock-what')}));
        """)
        self.assertIn("GPU clock capped at 2,400 MHz.", r["toast"])
        self.assertEqual(r["lit"], ["2400"])
        self.assertEqual((r["cap"], r["capCls"]), ("2,400 MHz", "cap ok"))
        self.assertRegex(r["state"], r"^2,400 MHz, set at \d\d:\d\d$")
        self.assertTrue(r["line"].startswith("2,400 MHz: 105.87 tok/s at 4 streams"), r["line"])
        self.assertEqual(r["dock"], "Cap the GPU clock at 2,400 MHz")

    def test_a_refused_sudo_says_to_run_the_installer_again(self):
        said = "sudo refused the call: the cockpit's sudoers allowlist on this box has no line for it yet, which the installer writes (cd /r && ./install.sh)"
        r = page(self, self.JOB + r"""
        gpu();
        feed({job: {current: null, recent: [job('j1', 'failed', {ok: false, cap: '2400', error: 'sudo', said: %s})]}});
        const toast1 = toastText();
        gpu({tried: '2400', error: 'sudo', said: %s});
        report(Object.assign(panel(), {toast: toast1}));
        """ % (json.dumps(said), json.dumps(said)))
        want = f"Capping the GPU clock at 2,400 MHz failed: {said}."
        self.assertIn(want, r["toast"])
        self.assertEqual(r["why"], want)
        self.assertEqual(r["lit"], ["none"], "a refused cap is not lit")
        self.assertEqual(r["disabled"], [False] * 4, "it can be asked again once the installer ran")

    def test_nvidia_smi_s_own_refusal_is_quoted(self):
        said = "nvidia-smi refused: Setting locked GPU clocks is not supported for GPU 0000000F:01:00.0."
        r = page(self, "gpu({kept: '2400', set: '2400', at: Date.now() / 1000, origin: 'ui', tried: '2000', error: 'failed', said: %s}); report(panel());"
                 % json.dumps(said))
        self.assertEqual(r["why"], f"Capping the GPU clock at 2,000 MHz failed: {said}", "its period, once")
        self.assertEqual(r["lit"], ["2400"], "the cap that still holds stays lit")
        self.assertEqual(r["capCls"], "cap warn")

    def test_a_cap_set_and_not_kept_says_it_holds_until_the_reboot(self):
        said = ("GPU clock capped at 2,400 MHz, but the cockpit could not keep it for its next start (No such file or "
                "directory): it holds until the next reboot, and nothing sets it again then")
        r = page(self, "gpu({kept: 'none', set: '2400', at: Date.now() / 1000, origin: 'ui', tried: '2400', error: 'keep', said: %s}); report(panel());"
                 % json.dumps(said))
        self.assertEqual(r["why"], said + ".")
        self.assertEqual(r["lit"], ["2400"])


class AKeptCapIsNeverSaidToHold(unittest.TestCase):
    def test_while_the_start_sets_it_again_and_when_it_could_not(self):
        r = page(self, r"""
        gpu({kept: '2200'});
        const waiting = panel();
        gpu({kept: '2200', tried: '2200', error: 'failed', said: 'nvidia-smi did not answer within 20 s'});
        const failed = panel();
        gpu({kept: '2200', set: '2200', at: Date.now() / 1000, origin: 'boot', tried: '2200', said: 'GPU clock capped at 2,200 MHz'});
        report({waiting, failed, set: panel()});
        """)
        w, f, s = r["waiting"], r["failed"], r["set"]
        self.assertEqual((w["cap"], w["capCls"]), ("2,200 MHz, being set", "cap warn"))
        self.assertEqual(w["state"], "2,200 MHz, kept for the cockpit’s start: being set again")
        self.assertEqual((f["cap"], f["capCls"]), ("2,200 MHz, not set", "cap err"))
        self.assertEqual(f["state"], "2,200 MHz, kept for the cockpit’s start: not set, its last try failed")
        self.assertEqual(f["why"], "Capping the GPU clock at 2,200 MHz failed: nvidia-smi did not answer within 20 s.")
        self.assertEqual((s["cap"], s["capCls"]), ("2,200 MHz", "cap ok"))
        self.assertRegex(s["state"], r"^2,200 MHz, set again at the cockpit’s start at \d\d:\d\d$")

    def test_a_file_that_names_no_cap_is_said(self):
        r = page(self, "gpu({kept_bad: 'it names no cap the cockpit offers'}); report(panel());")
        self.assertEqual(r["why"], "The file the cockpit keeps its cap in (cockpit-gpu-clock.json) was not used: "
                                   "it names no cap the cockpit offers.")
        self.assertEqual(r["lit"], ["none"])


class WhatCannotRunIsOffWithWhy(unittest.TestCase):
    def test_a_machine_without_nvidia_smi_offers_no_cap(self):
        r = page(self, r"""
        gpu({nvidia_smi: false, mhz: null});
        const off = panel();
        opt('2400').click();
        const asked = !$('scrim').hidden;
        gpu({nvidia_smi: true});
        report({off, asked, on: panel()});
        """)
        off = r["off"]
        why = ("This machine has no /usr/bin/nvidia-smi, the one the cockpit’s sudoers lines name: the GPU clock cannot "
               "be capped from here.")
        self.assertEqual(off["disabled"], [True] * 4)
        self.assertEqual(off["titles"], [why] * 4)
        self.assertEqual((off["why"], off["cap"], off["mhz"]), (why, "not on this machine", "n/a (nvidia-smi did not answer)"))
        self.assertFalse(r["asked"])
        self.assertEqual(r["on"]["disabled"], [False] * 4)
        self.assertIsNone(r["on"]["why"])

    def test_the_caps_wait_while_another_job_runs(self):
        r = page(self, r"""
        gpu();
        feed({job: {current: {id: 'j9', action: 'smoke', params: {}, status: 'running', origin: 'ui', started: Date.now() / 1000, elapsed: 3, argv: null}, recent: []}});
        report(panel());
        """)
        self.assertEqual(r["disabled"], [True] * 4)
        self.assertTrue(all("Another action is running" in t for t in r["titles"]), r["titles"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
