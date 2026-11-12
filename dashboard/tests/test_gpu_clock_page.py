#!/usr/bin/env python3
"""The GPU clock cap in the Machine view (decision of 2026-11-11), run: the page's scripts in node on a
DOM built from its own markup (pagejs.py, fakedom.js), state delivered as the server's stream does.

The cap is a setting of four positions, none by default, each with what it cost and gave on the
reference box. What is held: the lit position is the cap in effect as far as the server knows, a kept
one only while the start sets it again or when that try did not answer, and None once it failed;
each position asks first, in the sheet, with its exact command, its figures
and the limits of the measurement, and sends its value and nothing else; the outcome of the job is
said and the panel reads the state again; a machine without nvidia-smi, a refused sudo, nvidia-smi's
own refusal and a call that did not answer each say what happened and what to do; the arrows move the
focus and never ask, the focus rests on the lit position, and the group has a name; and the figures
are the ones docs/cockpit.md gives. Its layout at 375 px is the browser checks' (touch-check.mjs).
"""
import json
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_page_behaviour import run  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
# what the server says of a call that did not answer within its 20 s (cockpit.py, gpu_clock_run)
TIMEOUT = "nvidia-smi did not answer within 20 s and may still run, so the cap in effect is not known"
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
  tabs: [...$('gc-pick').querySelectorAll('button')].map(b => b.tabIndex),
  cap: txt('gc-cap'), capCls: $('gc-cap').className, lamp: $('gc-cap').firstChild ? $('gc-cap').firstChild.className : null,
  mhz: txt('gc-mhz'), state: txt('gc-state'), line: txt('gc-line'),
  why: $('gc-why').hidden ? null : txt('gc-why')});
const opt = cap => $('gc-pick').querySelector(`button[data-cap="${cap}"]`);
feed({config: CFG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
showView('machine');
""" % json.dumps(ARGV)


def page(test, body):
    return run(test, SETUP + body)


class NoneIsTheDefaultAndEachCapSaysWhatItCost(unittest.TestCase):
    def test_none_is_lit_and_the_clock_read_now_is_said(self):
        r = page(self, "gpu(); report(Object.assign(panel(), {group: $('gc-pick').getAttribute('role'), "
                       "named: $('gc-pick').getAttribute('aria-labelledby'), name: txt('gc-label')}));")
        self.assertEqual(r["lit"], ["none"])
        self.assertEqual(r["mhz"], "2,411 MHz")
        self.assertEqual(r["cap"], "no cap")
        # a cap set by hand outside the cockpit is not read back: the state says what the cockpit set, not the
        # factory clocks it cannot see (review of v1.47.0)
        self.assertEqual(r["state"], "none set from the cockpit")
        self.assertEqual(r["line"], "No cap, the factory setting: 105.74 tok/s at 4 streams, the GPU at 46.6 W, "
                                    "the box’s hottest zone at 92.6 °C on the reference box.")
        self.assertIsNone(r["why"])
        self.assertEqual(r["disabled"], [False] * 4)
        # one stop of the Tab key, on the lit position, and a group a screen reader names
        self.assertEqual(r["tabs"], [0, -1, -1, -1])
        self.assertEqual((r["group"], r["named"], r["name"]), ("radiogroup", "gc-label", "Cap"))

    def test_the_clock_read_is_said_to_lag_then_follow_the_cap(self):
        # Measured on the reference box, 2026-11-11: an idle GPU still read 2,502 MHz two seconds after a cap of
        # 2,000 was set, and 1,989 MHz just after it was lifted; after 51 to 111 s of rest, idle, it read each
        # cap (1,989 MHz under 2,000) and 2,411 MHz with none, 51 s after it was lifted. The page said the reading
        # did not move at idle, then that it lagged "a few seconds", a lag only known to be between 2 and 51 s (the
        # final review of v1.47.0).
        html = (REPO / "dashboard" / "static" / "index.html").read_text()
        for wrong in ("Clock now", "last run at", "last ran at", "does not move"):
            self.assertNotIn(wrong, html)
        self.assertIn('<dt>Clock read</dt><dd class="num" id="gc-mhz">', html)
        said = re.search(r'<p class="faint" id="gc-read"[^>]*>([^<]+)</p>', html).group(1)
        for words in ("lag behind a change of cap", "still the old one 2 s after it on the reference box",
                      "follows the cap within a minute", "idle included",
                      "above the cap just after it is set is not a cap that failed"):
            self.assertIn(words, said)
        self.assertNotIn("a few seconds", said)
        doc = " ".join((REPO / "docs" / "cockpit.md").read_text().split())
        for wrong in ("last ran at", "does not move", "last run at", "lag a few seconds"):
            self.assertNotIn(wrong, doc)
        for words in ("can lag behind a change of cap (still the old one 2 s after it on the reference box), then follows "
                      "the cap within a minute, the GPU idle included",
                      "a reading above the cap just after it is set is not a cap that failed"):
            self.assertIn(words, doc)

    def test_each_cap_says_its_throughput_its_watts_and_its_degrees(self):
        r = page(self, "gpu(); report({titles: panel().titles, rows: [...$('gc-table').tBodies[0].rows].map(tr => [...tr.cells].map(c => c.textContent)), note: txt('gc-note')});")
        # the hottest zone is the box's (the hottest of all its thermal zones), not the GPU's (review of v1.47.0)
        self.assertEqual(r["titles"], [
            "No cap: 105.74 tok/s at 4 streams, the GPU at 46.6 W, the box’s hottest zone at 92.6 °C on the reference box",
            "2,400 MHz: 105.87 tok/s at 4 streams, the GPU at 35.9 W, the box’s hottest zone at 90.2 °C on the reference box",
            "2,200 MHz: 103.77 tok/s at 4 streams, the GPU at 28.1 W, the box’s hottest zone at 86.6 °C on the reference box",
            "2,000 MHz: 103.15 tok/s at 4 streams, the GPU at 22.8 W, the box’s hottest zone at 84.4 °C on the reference box"])
        self.assertEqual(r["rows"], [["None", "2,502 MHz", "105.74 tok/s", "46.6 W", "92.6 °C"],
                                     ["2,400 MHz", "2,387 MHz", "105.87 tok/s", "35.9 W", "90.2 °C"],
                                     ["2,200 MHz", "2,190 MHz", "103.77 tok/s", "28.1 W", "86.6 °C"],
                                     ["2,000 MHz", "1,989 MHz", "103.15 tok/s", "22.8 W", "84.4 °C"],
                                     ["None, again at the end", "2,494 MHz", "105.02 tok/s", "47.7 W", "93.2 °C"]])
        # the limits, said where the figures are; the prefill with no cap is the median of 2,076.7 and 2,078.4
        for words in ("One lane measured, a text one", "one run per cap (None twice, first and last)",
                      "the image, video and 27B lanes are not measured yet",
                      "The watts are the GPU’s alone, as nvidia-smi reads them", "56.20 to 54.62 tok/s", "2,078 to 2,004 tok/s"):
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
        for words in ("105.87 tok/s at 4 streams, the GPU at 35.9 W, the box’s hottest zone at 90.2 °C",
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
        const set = Object.assign(panel(), {toast: toast1, dock: txt('dock-what')});
        gpu({kept: 'none', set: 'none', at: Date.now() / 1000, origin: 'ui', tried: 'none', said: 'GPU clock cap lifted: the GPU runs at its own clocks'});
        report(Object.assign(set, {lifted: panel()}));
        """)
        self.assertIn("GPU clock capped at 2,400 MHz.", r["toast"])
        self.assertEqual(r["lit"], ["2400"])
        self.assertEqual(r["tabs"], [-1, 0, -1, -1], "the Tab key lands on the cap lit")
        self.assertEqual((r["cap"], r["capCls"]), ("2,400 MHz", "cap ok"))
        self.assertRegex(r["state"], r"^2,400 MHz, set at \d\d:\d\d$")
        self.assertTrue(r["line"].startswith("2,400 MHz: 105.87 tok/s at 4 streams"), r["line"])
        self.assertEqual(r["dock"], "Cap the GPU clock at 2,400 MHz")
        # then lifted from the view: when, and that the GPU runs at its own clocks (no test held it: the final review
        # of v1.47.0)
        self.assertRegex(r["lifted"]["state"], r"^none, lifted at \d\d:\d\d: the GPU runs at its own clocks$")

    def test_a_dry_run_says_nothing_was_run(self):
        r = page(self, self.JOB + r"""
        gpu();
        feed({job: {current: null, recent: [job('j1', 'done', {ok: true, dry_run: true})]}});
        report(Object.assign(panel(), {toast: toastText()}));
        """)
        self.assertIn("Cap the GPU clock at 2,400 MHz: dry run, nothing was run.", r["toast"])
        self.assertNotIn("done", r["toast"])
        self.assertEqual(r["lit"], ["none"])

    def test_the_cap_set_again_at_the_start_says_so_in_the_dock(self):
        r = page(self, self.JOB + r"""
        gpu({kept: '2200'});
        feed({job: {current: Object.assign(job('j1', 'running', {}, {cap: '2200'}), {origin: 'boot', ended: null}), recent: []}});
        report({dock: txt('dock-what')});
        """)
        self.assertEqual(r["dock"], "Cap the GPU clock at 2,200 MHz, again at the cockpit’s start")

    def test_a_refused_sudo_says_to_run_the_installer_again(self):
        said = ("sudo refused the call: the cockpit's sudoers allowlist on this box has no line for it yet, which the "
                "cockpit's installer writes (cd /r && ./dashboard/install-dashboard.sh)")
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
        gpu({kept: '2200', tried: '2200', error: 'failed', said: 'nvidia-smi refused: Setting locked GPU clocks is not supported for GPU 0000000F:01:00.0.'});
        const failed = panel();
        gpu({kept: '2200', tried: '2200', error: 'timeout', said: %s});
        const unknown = panel();
        gpu({kept: '2200', set: '2200', at: Date.now() / 1000, origin: 'boot', tried: '2200', said: 'GPU clock capped at 2,200 MHz'});
        report({waiting, failed, unknown, set: panel()});
        """ % json.dumps(TIMEOUT))
        w, f, u, s = r["waiting"], r["failed"], r["unknown"], r["set"]
        self.assertEqual((w["cap"], w["capCls"]), ("2,200 MHz, being set", "cap warn"))
        self.assertEqual(w["state"], "2,200 MHz, kept for the cockpit’s start: being set again")
        # its lamp is live while the start sets it, and only then (no test held it: the final review of v1.47.0)
        self.assertEqual((w["lamp"], f["lamp"]), ("lamp warn live", "lamp err"))
        self.assertEqual((f["cap"], f["capCls"]), ("2,200 MHz, not set", "cap err"))
        self.assertEqual(f["state"], "2,200 MHz, kept for the cockpit’s start: not set, its last try failed")
        self.assertEqual(f["why"], "Capping the GPU clock at 2,200 MHz failed: nvidia-smi refused: Setting locked GPU clocks is "
                                   "not supported for GPU 0000000F:01:00.0.")
        # a try that failed leaves the GPU at its own clocks: None is lit, with the figures of no cap, where the kept cap
        # stayed lit (the final review of v1.47.0); one that did not answer may still apply, and keeps it lit
        self.assertEqual((f["lit"], u["lit"]), (["none"], ["2200"]))
        self.assertTrue(f["line"].startswith("No cap, the factory setting: 105.74 tok/s at 4 streams"), f["line"])
        # no answer at the start: nvidia-smi may still set it, so it is neither said set nor said not set
        self.assertEqual((u["cap"], u["capCls"]), ("2,200 MHz, not known", "cap warn"))
        self.assertEqual(u["state"], "2,200 MHz, kept for the cockpit’s start: not known, its last try did not answer")
        self.assertEqual(u["why"], f"Capping the GPU clock at 2,200 MHz: {TIMEOUT}.")
        self.assertEqual((s["cap"], s["capCls"]), ("2,200 MHz", "cap ok"))
        self.assertRegex(s["state"], r"^2,200 MHz, set again at the cockpit’s start at \d\d:\d\d$")

    def test_a_call_that_did_not_answer_says_the_cap_is_not_known(self):
        # the cockpit ends its sudo at 20 s, but the nvidia-smi it started runs as root and may still set the cap
        # tried, or not: the cap lit stays the one before, and the state says neither set nor lifted
        r = page(self, r"""
        gpu({kept: '2400', set: '2400', at: Date.now() / 1000, origin: 'ui', tried: '2000', error: 'timeout', said: %s});
        const capped = panel();
        gpu({kept: 'none', set: 'none', at: Date.now() / 1000, origin: 'ui', tried: '2200', error: 'timeout', said: %s});
        report({capped, lifted: panel()});
        """ % (json.dumps(TIMEOUT), json.dumps(TIMEOUT)))
        c, n = r["capped"], r["lifted"]
        self.assertEqual(c["lit"], ["2400"], "the cap lit is the one set before the try")
        self.assertEqual((c["cap"], c["capCls"]), ("not known", "cap warn"))
        self.assertEqual(c["state"], "not known: the try to cap it at 2,000 MHz did not answer, and may still apply")
        self.assertEqual(c["why"], f"Capping the GPU clock at 2,000 MHz: {TIMEOUT}.")
        self.assertEqual(n["lit"], ["none"])
        self.assertEqual(n["cap"], "not known")
        self.assertEqual(n["state"], "not known: the try to cap it at 2,200 MHz did not answer, and may still apply")
        for state in (c["state"], n["state"]):
            self.assertNotRegex(state, r"\b(set|lifted)\b")

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

    def test_a_cap_kept_there_lights_none_and_none_forgets_it(self):
        # a cap kept, then nvidia-smi gone: the cap was lit under "not on this machine", and every position was
        # off, so the file could not be forgotten and each start tried it in vain (review of v1.47.0)
        r = page(self, r"""
        gpu({nvidia_smi: false, mhz: null, kept: '2400'});
        const off = panel();
        opt('none').focus();
        for (const k of ['ArrowRight', 'ArrowDown', 'ArrowLeft']) $('gc-pick').querySelector('button[data-cap="none"]')
          .dispatchEvent({type: 'keydown', key: k, bubbles: true});
        const focused = document.activeElement.dataset.cap;
        opt('none').click();
        const sheet = {open: !$('scrim').hidden, cmd: txt('sh-cmd'), lede: txt('sh-lede')};
        $('sh-cancel').click();
        gpu({nvidia_smi: false, mhz: null});
        report({off, focused, sheet, forgotten: panel()});
        """)
        off = r["off"]
        self.assertEqual(off["lit"], ["none"], "no cap can hold where nvidia-smi is not")
        self.assertEqual(off["disabled"], [False, True, True, True])
        self.assertEqual(off["why"], "This machine has no /usr/bin/nvidia-smi, the one the cockpit’s sudoers lines name: the "
                                     "GPU clock cannot be capped from here. The cockpit keeps 2,400 MHz for its start, which "
                                     "it cannot set here: None forgets it.")
        self.assertEqual(r["focused"], "none", "an arrow moved the focus to a cap that is off")
        self.assertTrue(r["sheet"]["open"])
        self.assertEqual(r["sheet"]["cmd"], "cockpit gpu_clock", "the sheet named an nvidia-smi call the job does not make")
        self.assertTrue(r["sheet"]["lede"].startswith("This machine has no /usr/bin/nvidia-smi, so nothing is run: the cockpit "
                                                      "forgets the cap it keeps for its next start (2,400 MHz)"), r["sheet"]["lede"])
        self.assertEqual(r["forgotten"]["disabled"], [True] * 4, "with nothing kept, None has nothing to do")

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
