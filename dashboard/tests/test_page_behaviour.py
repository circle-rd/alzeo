#!/usr/bin/env python3
"""What the cockpit page does, run: its scripts in node, on a DOM built from the page's
own markup (pagejs.py, fakedom.js).

Each class is one defect found in a review of the page (v1.18.6 on 2026-09-24, the video
lane in September 2026), reproduced before it was fixed, and carried over to the page as
it was rebuilt: the page said things that were not true, kept doing work nobody asked
for, or lost a control under a person's finger. State reaches the page the way the
server's event stream delivers it: one payload through apply().
"""
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pagejs  # noqa: E402

# Shared by the bodies below: a state payload the way the server wraps it, and text reads.
HELPERS = r"""
const wrap = o => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, {data: v, ts: Date.now() / 1000}]));
const feed = o => apply(wrap(o));
const txt = id => ($(id) ? $(id).textContent : null);
const CONFIG = {version: '1.1.2', dry_run: false, usable_frac: 0.92, periods: {}};
const UNITS = {units: {'qwen38-sglang.service': {active: 'active', enabled: 'enabled'},
                       'qwen38-flash.service': {active: 'inactive', enabled: 'disabled'},
                       'qwen38-image.service': {active: 'inactive', enabled: 'disabled'},
                       'qwen38-video.service': {active: 'inactive', enabled: 'disabled'},
                       'qwen38-kolibri.service': {active: 'inactive', enabled: 'disabled'},
                       'qwen38-nemotron.service': {active: 'inactive', enabled: 'disabled'},
                       'qwen38-nemotron-super.service': {active: 'inactive', enabled: 'disabled'},
                       'qwen38-flash-local.service': {active: 'inactive', enabled: 'disabled'},
                       'qwen38-ornith-122b.service': {active: 'inactive', enabled: 'disabled'},
                       'qwen38-keepalive.service': {active: 'active', enabled: 'enabled'}}};
const life = (engines, extra) => Object.assign({engines, events: [], blocked: {}}, extra || {});
const eng = (state, extra) => Object.assign({state, target: 'stock', elapsed: 600}, extra || {});
const toastText = () => txt('toasts');
"""
SCRIPTS = pagejs.SCRIPTS


def run(test, body, **kw):
    kw.setdefault("scripts", SCRIPTS)
    return pagejs.run(test, HELPERS + body, **kw)


def css_rules(css=None):
    """(selector, [enclosing @media conditions], declarations) for every rule of the page's
    stylesheet, comments dropped."""
    css = css if css is not None else (pagejs.STATIC / "css" / "cockpit.css").read_text()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, stack, buf, i = [], [], "", 0
    while i < len(css):
        ch = css[i]
        if ch == "{":
            head = buf.strip()
            buf = ""
            if head.startswith("@media") or head.startswith("@supports"):
                stack.append(head)
            elif head.startswith("@"):
                depth, j = 1, i + 1       # @keyframes, @font-face: skip the whole block
                while depth:
                    depth += {"{": 1, "}": -1}.get(css[j], 0)
                    j += 1
                i = j - 1
            else:
                end = css.index("}", i)
                out.append((head, list(stack), css[i + 1:end].strip()))
                i = end
        elif ch == "}":
            if stack:
                stack.pop()
            buf = ""
        else:
            buf += ch
        i += 1
    return out


class AWedgeSaysWhatHappensNext(unittest.TestCase):
    """The page said "the autoheal belt restarts it after its grace period" of every wedged
    engine, and the belt is off unless COCKPIT_AUTOHEAL=1: on a default install nothing
    restarts it, and waiting for it is the wrong move."""

    BODY = r"""
    feed({config: Object.assign({}, CONFIG, %s), units: UNITS.units ? UNITS : {}, lifecycle: life({'qwen38-sglang.service': eng('wedged')})});
    report({banner: txt('banners')});
    """

    def test_off_by_default_it_does_not_promise_a_restart(self):
        r = run(self, self.BODY % "{autoheal: false}")
        self.assertNotIn("belt restarts it", r["banner"])
        self.assertIn("Nothing restarts it by itself", r["banner"])
        self.assertIn("COCKPIT_AUTOHEAL=1", r["banner"])

    def test_armed_it_says_the_belt_restarts_it(self):
        r = run(self, self.BODY % "{autoheal: true, autoheal_grace_s: 120}")
        self.assertIn("autoheal belt restarts it", r["banner"])
        self.assertIn("2 min", r["banner"])



class TheKolibriLaneIsSaidAsItIs(unittest.TestCase):
    """The Kolibri lane in the page (review of 2026-10-14): one prompt is capped by the engine's
    window as well as by the pool (a 2.2M-token pool read "about 1.8M tokens" where vLLM refuses
    past 262,144), and the actions its vLLM has no route for are refused with why, not offered
    to fail."""

    KOLIBRI = r"""
    const U = JSON.parse(JSON.stringify(UNITS));
    U.units['qwen38-sglang.service'] = {active: 'inactive', enabled: 'disabled'};
    U.units['qwen38-kolibri.service'] = {active: 'active', enabled: 'enabled'};
    feed({config: CONFIG, units: U, lifecycle: life({'qwen38-kolibri.service': eng('ready', {target: 'kolibri'})})});
    """

    def test_one_prompt_is_capped_by_the_engines_window(self):
        r = run(self, r"""
        F.pool = 2168247; F.ceiling = 0; F.window = 262144; const kolibri = singleLimit();
        F.pool = 888000; F.window = 1010000; const q1m = singleLimit();
        F.pool = 420000; F.ceiling = 250000; F.window = 262144; const flash = singleLimit();
        report({kolibri, q1m, flash});
        """)
        self.assertEqual((r["kolibri"], r["q1m"], r["flash"]), (262144, round(888000 * 0.92), 250000))

    def test_what_its_vllm_has_no_route_for_is_refused_with_why(self):
        r = run(self, self.KOLIBRI + r"""
        askAction('abort_all', {}, []);
        report({toast: toastText(), flush: cannotWhy('flush_cache'), fit: cannotWhy('fit_opencode'),
                smoke: cannotWhy('smoke'), sheet: !$('scrim').hidden});
        """)
        self.assertIn("no route that aborts", r["toast"])
        self.assertIn("no cache flush route", r["flush"])
        self.assertIn("nothing to fit", r["fit"])
        self.assertEqual(r["smoke"], "", "the smoke probe works there")
        self.assertFalse(r["sheet"], "no confirmation sheet opened for an action that can only fail")

    def test_traffic_says_kolibri_serves_and_reads_its_own_cache(self):
        r = run(self, self.KOLIBRI + r"""
        feed({engine_fast: {load: [{num_reqs: 1, num_waiting_reqs: 0, num_tokens: 2000000, num_used_tokens: 500000}]}});
        feed({metrics: {lane: null}, decode: {lane: null}});    // what the server sends on this lane
        report({verdict: txt('wm-verdict'), acc: txt('tr-acc'), kv: txt('tr-kv')});
        """)
        self.assertIn("serves on vLLM", r["verdict"])
        # its name as every other view says it (LANE_NAME): a copy said "Kolibri" and "Nemotron"
        self.assertIn("The Kolibri-1 lane serves on vLLM", r["verdict"])
        self.assertNotIn("No text engine is serving", r["verdict"])
        self.assertEqual((r["acc"], r["kv"]), ("no draft model", "25.0 %"))

    def test_the_probe_line_says_it_does_not_run_there(self):
        # the canary skips on Kolibri, and the page showed the last Qwen lane's result as its own:
        # "ok, 0.4 s" or "2 failures in a row: URLError" (found in review, 2026-10-14)
        out = {}
        for name, canary in (("ok", "{skipped: true, why: WHY, last_ok: 1700000000, latency: 0.4, fails: 0}"),
                             ("fail", "{skipped: true, why: WHY, last_ok: 1700000000, fails: 2, last_err: 'URLError'}")):
            out[name] = run(self, self.KOLIBRI + r"""
            const WHY = "the Kolibri-1 lane is checked by vLLM's own /health, not by a generation";
            feed({canary: %s});
            report({line: txt('px-probe'), last: txt('px-last')});
            """ % canary)
        for r in out.values():
            self.assertIn("not run", r["line"])
            self.assertIn("vLLM's own /health", r["line"])
            self.assertNotIn("ok, 0.4 s", r["line"])
            self.assertNotIn("failures in a row", r["line"])
            self.assertEqual(r["last"], "none on the Kolibri-1 lane")

    def test_traffic_while_it_loads_says_it_serves_nothing_yet(self):
        r = run(self, r"""
        const U = JSON.parse(JSON.stringify(UNITS));
        U.units['qwen38-sglang.service'] = {active: 'inactive', enabled: 'disabled'};
        U.units['qwen38-kolibri.service'] = {active: 'active', enabled: 'enabled'};
        feed({config: CONFIG, units: U, lifecycle: life({'qwen38-kolibri.service': eng('loading-weights', {target: 'kolibri'})})});
        feed({metrics: {lane: null}, decode: {lane: null}});
        report({verdict: txt('wm-verdict'), cap: txt('wm-cap')});
        """)
        self.assertNotIn("serves on vLLM", r["verdict"])
        self.assertIn("loading", r["verdict"])

    def test_the_pool_guard_says_it_cannot_act_there_and_the_floor_how_it_does(self):
        # since v1.40.3 the floor stops vLLM's generations through the proxy, which closes their
        # connections; the pool guard still has no flush route there
        r = run(self, self.KOLIBRI + r"""
        const L = life({'qwen38-kolibri.service': eng('ready', {target: 'kolibri'})},
                       {pool_guard: {enabled: true, threshold: 0.6, fails: 0, flushes: 0}});
        feed({lifecycle: L, engine_fast: {mem_floor: {gib: 3, aborts: 0}}});
        report({floor: txt('mm-floor'), guard: txt('mm-guard')});
        """)
        self.assertEqual(r["floor"], "stops generations under 3 GiB through the proxy (on vLLM, by closing "
                                     "their connections), never fired")
        self.assertIn("not on the Kolibri-1 lane", r["guard"])

    def test_the_floor_banner_says_what_the_stop_did_no_more(self):
        # it said every generation stopped, clients cut and new requests held whatever the proxy did,
        # where it had cut none (code review, 2026-10-26): the cockpit's floor_said is what it says
        r = run(self, r"""
        const now = Date.now() / 1000;
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-flash.service': eng('ready', {target: 'flash'})})});
        feed({engine_fast: {mem_floor: {gib: 3, aborts: 1, last_abort: now - 30, via: 'proxy',
              said: "every generation stopped through the proxy (2 cut, their clients retry, the others ended by SGLang's abort_all), and new ones wait for memory"}}});
        const proxy = txt('banners');
        feed({engine_fast: {mem_floor: {gib: 3, aborts: 1, last_abort: now - 30, via: 'proxy',
              said: "no generation ran through the proxy, and SGLang's abort_all ended the others"}}});
        const none = txt('banners');
        feed({engine_fast: {mem_floor: {gib: 3, aborts: 1, last_abort: now - 30, via: 'engine'}}});
        report({proxy, none, engine: txt('banners'), floor: txt('mm-floor')});
        """)
        self.assertIn("To keep the box out of a livelock: every generation stopped through the proxy (2 cut, "
                      "their clients retry", r["proxy"])
        self.assertIn("new ones wait for memory.", r["proxy"])
        self.assertIn("no generation ran through the proxy, and SGLang's abort_all ended the others.", r["none"])
        self.assertNotIn("cut", r["none"])
        self.assertNotIn("wait", r["none"])
        self.assertIn("every generation was aborted", r["engine"])
        self.assertNotIn("cut", r["engine"])
        self.assertEqual(r["floor"], "stops generations under 3 GiB through the proxy, fired 1 time")

    def test_the_floor_belts_line_wraps_like_the_guards(self):
        # its line is longer than the row, and it was cut with an ellipsis on the Machine view
        # where the guard's wraps (the reference box, v1.29.0, 2026-10-14)
        r = run(self, r"""report({floor: $('mm-floor').className, guard: $('mm-guard').className});""")
        self.assertIn("wrap", r["floor"].split())
        self.assertIn("wrap", r["guard"].split())

    def test_the_engine_row_names_the_engine(self):
        r = run(self, self.KOLIBRI + r"""
        feed({engine_info: {info: {version: 'vLLM 0.29.0', served_model_name: 'kolibri-1'}}});
        const kol = txt('eng-ver');
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
        feed({engine_info: {info: {version: '0.5.21+cu130'}}});
        report({kol, qwen: txt('eng-ver'), label: $('eng-ver').previousElementSibling.textContent});
        """)
        self.assertEqual((r["kol"], r["qwen"], r["label"]), ("vLLM 0.29.0", "SGLang 0.5.21", "Engine"))

    def test_the_switch_sheet_says_what_a_kolibri_switch_does(self):
        r = run(self, self.KOLIBRI + r"""
        report({kol: EXPLAIN.switch({target: 'kolibri'}), q: EXPLAIN.switch({target: 'stock'})});
        """)
        self.assertNotIn("proxy ceiling", r["kol"], "the switch to Kolibri never touches it")
        self.assertIn("makes Kolibri-1 opencode's default model", r["kol"])
        self.assertIn("proxy ceiling", r["q"])

    def test_on_a_qwen_lane_nothing_is_refused(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
        report({abort: cannotWhy('abort_all'), flush: cannotWhy('flush_cache')});
        """)
        self.assertEqual((r["abort"], r["flush"]), ("", ""))


class EachVllmLaneIsSaidByItsOwnName(unittest.TestCase):
    """The page said "the Nemotron lanes" and "The Nemotron lane" for Bolt and for Super VL, one sentence for the two,
    and "the Kolibri lane" and "the Ornith lane" beside the official names of every other view: the probe's lines, the
    request id override, the pool guard and the actions their vLLM has no route for (found 2026-11-10). Each says the
    lane that serves, by its official name (Hasan, 2026-11-10)."""

    SERVES = r"""
    const U = JSON.parse(JSON.stringify(UNITS));
    U.units['qwen38-sglang.service'] = {active: 'inactive', enabled: 'disabled'};
    U.units['%(unit)s'] = {active: 'active', enabled: 'enabled'};
    feed({config: CONFIG, units: U, lifecycle: life({'%(unit)s': eng('ready', {target: '%(target)s'})},
                                                    {pool_guard: {enabled: true, threshold: 0.6, fails: 0, flushes: 0}})});
    feed({canary: {skipped: true, last_ok: 1700000000, latency: 0.4, fails: 0},
          reqguard: {lane: null, override: null, state: 'ok', verdict: 'no abandoned request in this window', window: '10m',
                     guard: {}, zombies: {}}});
    report({probe: txt('px-probe'), last: txt('px-last'), override: txt('zg-override'), guard: txt('mm-guard'),
            flush: cannotWhy('flush_cache'), abort: cannotWhy('abort_all'), fit: cannotWhy('fit_opencode')});
    """
    LANES = (("qwen38-kolibri.service", "kolibri", "Kolibri-1"),
             ("qwen38-nemotron.service", "nemotron", "Nemotron 3.5 Bolt"),
             ("qwen38-nemotron-super.service", "nemotron-super", "Nemotron 3.5 Super VL"),
             ("qwen38-flash-local.service", "flash-local", "Qwen3.8 Flash-Next FP4-Local"),
             ("qwen38-ornith-122b.service", "ornith-122b", "Ornith 1.5 122B"))

    def test_each_says_the_lane_that_serves(self):
        for unit, target, name in self.LANES:
            with self.subTest(name):
                r = run(self, self.SERVES % {"unit": unit, "target": target})
                self.assertEqual(r["probe"], f"not run: the {name} lane is checked by its own /health")
                self.assertEqual(r["last"], f"none on the {name} lane")
                self.assertEqual(r["override"], f"not on the {name} lane (an SGLang setting)")
                self.assertEqual(r["guard"], f"not on the {name} lane: vLLM has no cache flush route, and evicts its "
                                             "prefix cache by itself")
                for k in ("flush", "abort", "fit"):
                    self.assertTrue(r[k].startswith(f"The {name} lane"), r[k])
                said = json.dumps(r)
                self.assertEqual([x for _, _, x in self.LANES if x != name and x in said], [])


class TheProbeLineIsNotAStaleOk(unittest.TestCase):
    """With no text engine serving (stopped, or a diffusion lane up) the canary skips, and
    the page kept its last success on screen as "ok, 0.4 s (skipped this round: busy)"."""

    def probe(self, engines, canary):
        return run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life(%s), canary: %s});
        report({line: txt('px-probe')});
        """ % (engines, canary))

    def test_no_text_engine(self):
        r = self.probe("{'qwen38-sglang.service': eng('stopped')}",
                       "{skipped: true, why: 'no text engine is ready', last_ok: Date.now() / 1000 - 600, fails: 0, latency: 0.4}")
        self.assertFalse(r["line"].startswith("ok"), r)
        self.assertIn("no text engine", r["line"])

    def test_a_diffusion_lane_serving(self):
        for unit, target, word in (("qwen38-image.service", "image", "image"), ("qwen38-video.service", "video", "video")):
            with self.subTest(unit=unit):
                r = self.probe("{'qwen38-sglang.service': eng('stopped'), '%s': eng('ready', {target: '%s'})}" % (unit, target),
                               "{skipped: true, why: 'no text engine is ready', last_ok: Date.now() / 1000 - 600, fails: 0, latency: 0.4}")
                self.assertIn("no text engine", r["line"])
                self.assertIn(word, r["line"])
                self.assertNotIn("ok,", r["line"])

    def test_a_skip_names_its_own_reason(self):
        r = self.probe("{'qwen38-sglang.service': eng('ready')}",
                       "{skipped: true, why: 'a client was active in the last minute', last_ok: Date.now() / 1000 - 60, fails: 0, latency: 0.4}")
        self.assertTrue(r["line"].startswith("ok, 0.4 s"), r)
        self.assertIn("a client was active in the last minute", r["line"])
        self.assertNotIn("busy", r["line"])


class TheFitLineSaysTheFailingPair(unittest.TestCase):
    """The limits line said "too large for this pool" and showed prompt plus answer against
    the pool, whatever the reason: a flash limit over the proxy's 250,000 ceiling read
    "730,000 asked, 827,968 servable". The failing pair is the one the server names."""

    def fit(self, fit):
        return run(self, r"""
        feed({opencode: {enabled: true, real: {present: true, default: 'flashnext/qwen3.8-flash-next', limits: {}},
                         launcher: {present: true, ours: true, cap: 200000}, follows: true, why: 'follows', fit: %s}});
        report({line: txt('oc-fit')});
        """ % fit)

    def test_over_the_proxy_ceiling(self):
        r = self.fit("{ok: false, why: 'the prompt alone exceeds what the proxy relays', asked: 548000, limit: 250000,"
                     " worst: 730000, usable: 827968, prompt_cap: 250000, pool: 899965, window: 1048576,"
                     " context: 548000, output: 182000, served: 'qwen3.8-flash-next'}")
        self.assertIn("548,000 asked, 250,000 servable", r["line"])
        self.assertNotIn("827,968", r["line"])
        self.assertNotIn("pool", r["line"])

    def test_over_the_window(self):
        r = self.fit("{ok: false, why: \"the prompt at opencode's compaction point plus the answer exceeds the engine's window\","
                     " asked: 280863, limit: 262144, worst: 237000, usable: 519203, prompt_cap: 250000, pool: 564352,"
                     " window: 262144, context: 205000, output: 32000, served: 'qwen3.8-flash-next'}")
        self.assertIn("280,863 asked, 262,144 servable", r["line"])
        self.assertNotIn("pool", r["line"])

    def test_over_the_pool(self):
        r = self.fit("{ok: false, why: 'prompt plus answer exceeds the pool', asked: 900000, limit: 827968,"
                     " worst: 900000, usable: 827968, prompt_cap: 827968, pool: 899965, window: 1048576,"
                     " context: 700000, output: 200000, served: 'qwen3.8-27b'}")
        self.assertIn("900,000 asked, 827,968 servable", r["line"])


class TheCockpitsOwnFitIsANoteNotAWarning(unittest.TestCase):
    """A Load writes opencode's nominal pair (900,000 on the 1M lanes) before the engine has a
    pool, and the cockpit lowers it once the engine is ready: for those seconds the page showed
    a yellow warning that a session would break, after every Load of the 27B (second GX10,
    2026-10-22). While the cockpit's own fit runs, the page says so in a note."""

    BODY = r"""
    feed({config: CONFIG, units: UNITS, opencode: {enabled: true, fit: {ok: false,
          why: 'prompt plus answer exceeds the pool', asked: 900000, limit: 791259, worst: 900000,
          usable: 791259, pool: 860064, served: 'qwen3.8-27b', autofit: %s}}});
    report({banner: txt('banners'), warn: document.querySelectorAll('#banners .lamp.warn').length});
    """

    def test_while_its_own_fit_runs_the_page_says_so_calmly(self):
        for state in ("'started'", "'fitting'", "'waiting for the engine'"):
            r = run(self, self.BODY % state)
            self.assertIn("The cockpit is fitting opencode to this engine.", r["banner"], state)
            self.assertIn("this boot serves 791,259", r["banner"], state)
            self.assertNotIn("would break", r["banner"], state)
            self.assertEqual(r["warn"], 0, state)
            # while the engine still warms up, the note says the fit comes once it is ready
            self.assertEqual("as soon as the engine is ready" in r["banner"], state == "'waiting for the engine'", state)

    def test_when_nothing_is_fitting_it_it_still_warns(self):
        for state in ("'already handled in this activation'", "'left alone: the fit would raise a declared limit'",
                      "'not needed'", "'no text engine is ready'", "null"):
            r = run(self, self.BODY % state)
            self.assertIn("opencode asks for more than this engine can hold.", r["banner"], state)
            self.assertIn('Settings has "Fit the limits to this engine".', r["banner"], state)
            self.assertNotIn("fitting opencode", r["banner"], state)
            self.assertEqual(r["warn"], 1, state)


class TheCheckpointSelectorFollowsTheLaneAgain(unittest.TestCase):
    """Touching a target selector froze it for the life of the page: a choice cancelled in
    the confirmation, or one the lane has served since, left it on that choice while the
    lanes moved under it."""

    SERVE = r"""
    const serve = (unit, target) => feed({engine_info: {served_target: target, prompt_ceiling_tokens: 0,
                     info: {served_model_name: 'x', max_total_num_tokens: 800000, context_length: 1048576}},
                   lifecycle: life({[unit]: eng('ready', {target})})});
    feed({config: CONFIG, units: UNITS});
    """

    def test_a_cancelled_choice_gives_the_selector_back(self):
        r = run(self, self.SERVE + r"""
        serve('qwen38-sglang.service', 'stock');
        const c = LCARDS.get('qwen38-sglang.service');
        c.opts.find(b => b.dataset.target === 'fp8').click();
        c.load.click();
        const asked = !$('scrim').hidden;
        $('sh-cancel').click();
        serve('qwen38-sglang.service', 'stock');
        report({asked, value: c.target});
        """)
        self.assertTrue(r["asked"], "Load opens the journey")
        self.assertEqual(r["value"], "stock")

    def test_once_the_choice_is_served_it_follows_again(self):
        r = run(self, self.SERVE + r"""
        serve('qwen38-sglang.service', 'stock');
        const c = LCARDS.get('qwen38-sglang.service');
        c.opts.find(b => b.dataset.target === 'fp8').click();
        serve('qwen38-sglang.service', 'stock');
        const kept = c.target;                        // chosen, not served yet: kept
        serve('qwen38-sglang.service', 'fp8');        // the switch, then a restart
        serve('qwen38-sglang.service', 'uncensored'); // later, another checkpoint
        report({kept, value: c.target});
        """)
        self.assertEqual(r["kept"], "fp8")
        self.assertEqual(r["value"], "uncensored")


class ControlsSurviveARefresh(unittest.TestCase):
    """The keepalive stop/start button and the job history's log buttons were rebuilt on
    every state message, up to twice a second, so a click whose press and release straddled
    a refresh landed on a node that no longer existed (25 of 40 clicks registered)."""

    def test_the_proxy_button_is_the_same_node_and_acts_on_what_it_shows(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS}); const a = $('px-btn'); const shown1 = a.textContent;
        const stopped = JSON.parse(JSON.stringify(UNITS)); stopped.units['qwen38-keepalive.service'].active = 'inactive';
        feed({units: stopped}); const b = $('px-btn');
        b.click();
        report({same: a === b, before: shown1, after: b.textContent, argv: txt('sh-cmd'), title: txt('sh-title')});
        """)
        self.assertTrue(r["same"])
        self.assertIn("Stop", r["before"])
        self.assertIn("Start", r["after"])
        self.assertIn(" start ", r["argv"] + " ")
        self.assertNotIn(" stop ", r["argv"] + " ")

    def test_the_log_buttons_are_the_same_nodes(self):
        r = run(self, r"""
        const job = (id, status, elapsed) => ({id, action: 'smoke', params: {}, status, started: Date.now() / 1000 - 100, elapsed});
        feed({job: {current: job('b', 'running', 5), recent: [job('a', 'done', 3)]}});
        const a = [...document.querySelectorAll('#job-hist button')];
        feed({job: {current: job('b', 'running', 6), recent: [job('a', 'done', 3)]}});
        const b = [...document.querySelectorAll('#job-hist button')];
        const when = [...document.querySelectorAll('#job-hist .faint')][0].textContent;
        feed({job: {current: null, recent: [job('b', 'done', 7), job('a', 'done', 3)]}});
        const c = [...document.querySelectorAll('#job-hist button')];
        report({n: [a.length, b.length, c.length], same: a.every((x, i) => x === b[i]), when, kept: c.includes(a[1])});
        """)
        self.assertEqual(r["n"], [2, 2, 2])
        self.assertTrue(r["same"])
        self.assertTrue(r["when"].endswith("6 s"), r["when"])
        self.assertTrue(r["kept"], "an unchanged job keeps its row when another one finishes")


class FullscreenCanBeLeftWithoutStorage(unittest.TestCase):
    """On a phone the Agent view opens fullscreen unless a choice to leave it was stored.
    Without localStorage (private mode) the choice was never kept, and the next state tick,
    a second later, put the frame back over the page."""

    def test_the_exit_holds_for_the_page(self):
        r = run(self, r"""
        const agent = {enabled: true, relay: {listening: true, port: 30091, bind: '127.0.0.1'},
                       server: {healthy: true, version: '1.18.32'}, unit: {active: 'active', enabled: 'enabled'},
                       unit_installed: true, pinned: '1.18.32', auto: true, auto_live: true};
        feed({agent});
        const opened = document.body.classList.contains('agentmax');
        $('ag-exit').click();
        const left = !document.body.classList.contains('agentmax');
        feed({agent}); feed({agent});
        report({opened, left, after: document.body.classList.contains('agentmax')});
        """, opts={"noStorage": True, "media": {"(max-width:980px)": True}, "location": {"hash": "#agent"}})
        self.assertTrue(r["opened"], "a phone opens the Agent view fullscreen")
        self.assertTrue(r["left"])
        self.assertFalse(r["after"], "the frame came back over the page")


class TheTimelineAnnouncesOnlyWhatIsNew(unittest.TestCase):
    """The event lists are live regions, and every lifecycle tick emptied and refilled them:
    a screen reader read the whole list again twice a second. Rows now stay; a new event is
    one addition."""

    def test_rows_are_kept_and_a_new_event_is_one_new_node(self):
        r = run(self, r"""
        const ev = (ts, msg) => ({ts, kind: 'state', msg});
        const evs = [ev(100, 'a'), ev(200, 'b'), ev(300, 'c')];
        feed({lifecycle: life({'qwen38-sglang.service': eng('ready')}, {events: evs})});
        const before = [...$('now-timeline').children];
        feed({lifecycle: life({'qwen38-sglang.service': eng('ready')}, {events: evs})});
        const same = [...$('now-timeline').children];
        feed({lifecycle: life({'qwen38-sglang.service': eng('ready')}, {events: evs.concat([ev(400, 'd')])})});
        const after = [...$('now-timeline').children];
        report({n: [before.length, same.length, after.length], unchanged: before.every((x, i) => x === same[i]),
                top: after[0].textContent, kept: before.every(x => after.includes(x)),
                order: after.map(x => x.textContent.slice(-1)).join('')});
        """)
        self.assertEqual(r["n"], [3, 3, 4])
        self.assertTrue(r["unchanged"], "an unchanged list is left alone")
        self.assertTrue(r["top"].endswith("d"))
        self.assertTrue(r["kept"], "the rows already announced stay")
        self.assertEqual(r["order"], "dcba")

    def test_now_keeps_its_last_eight_and_logs_keeps_forty(self):
        r = run(self, r"""
        const evs = Array.from({length: 50}, (_, i) => ({ts: 100 + i, kind: 'state', msg: 'm' + i}));
        feed({lifecycle: life({'qwen38-sglang.service': eng('ready')}, {events: evs})});
        showView('logs');
        report({short: $('now-timeline').children.length, top: $('now-timeline').children[0].textContent,
                long: $('log-events').children.length});
        """)
        self.assertEqual(r["short"], 8)
        self.assertTrue(r["top"].endswith("m49"))
        self.assertEqual(r["long"], 40)

    def test_driver_refusals_in_a_row_are_one_row(self):
        r = run(self, r"""
        const k = (ts, n) => ({ts, kind: 'kernel', msg: `GPU driver refused ${n} allocation(s): memory edge during a prefill`});
        const evs = [{ts: 90, kind: 'state', msg: 'qwen38-video: starting → ready'}, k(100, 3), k(110, 20), k(120, 1),
                     {ts: 130, kind: 'state', msg: 'qwen38-video: ready → stopping'}];
        feed({lifecycle: life({'qwen38-video.service': eng('stopping', {target: 'video'})}, {events: evs})});
        report([...$('now-timeline').children].map(x => x.textContent));
        """)
        self.assertEqual(len(r), 3, r)
        self.assertIn("refused 24 allocations", r[1])
        self.assertIn("video: ready to stopping", r[0])


class TheImagePollStops(unittest.TestCase):
    """Once the Image view had seen someone else's generation it asked /api/image every 2 s
    for the life of the page (each answer runs a journalctl on the box), whichever view was
    open and whether or not the page was visible."""

    BODY = r"""
    let busy = true;
    __fetch = async url => url === '/api/image'
      ? __response(200, {port: 30020, host: '127.0.0.1', available: true, installed: true,
                         progress: busy ? {kind: 'stage', stage: 'denoising', label: 'denoising'} : {}})
      : new Promise(() => {});
    feed({config: CONFIG, units: UNITS,
          lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})})});
    showView('image'); await __settle();
    const count = () => __fetches.filter(f => f.url === '/api/image').length;
    """

    def test_it_stops_when_the_other_generation_ends(self):
        r = run(self, self.BODY + r"""
        await __advance(6000); const during = count();
        busy = false;
        await __advance(4000); const ended = count();
        await __advance(60000);
        report({during, ended, later: count()});
        """)
        self.assertGreaterEqual(r["during"], 3, "it follows the other generation while it runs")
        self.assertEqual(r["later"], r["ended"], "and asks nothing more once it is over")

    def test_it_does_not_run_behind_another_view(self):
        r = run(self, self.BODY + r"""
        await __advance(4000);
        showView('now'); await __settle(); const left = count();
        await __advance(30000);
        report({left, later: count()});
        """)
        self.assertEqual(r["later"], r["left"])

    def test_it_does_not_run_in_a_hidden_page(self):
        r = run(self, self.BODY + r"""
        await __advance(4000);
        document.hidden = true; const hid = count();
        await __advance(30000);
        report({hid, later: count()});
        """)
        self.assertEqual(r["later"], r["hid"])

    def test_its_own_request_is_followed_whatever_the_view(self):
        r = run(self, self.BODY + r"""
        busy = false; await __advance(3000);
        IS.inflight = Date.now(); imgWatch(true); showView('now');
        const start = count(); await __advance(6000);
        report({grew: count() - start});
        """)
        self.assertGreaterEqual(r["grew"], 3)


class TheVideoPollStops(unittest.TestCase):
    """The same rule for the video lane, whose answer also reads its journal: another
    person's video is followed on a visible Video view only, this page's own from anywhere."""

    BODY = r"""
    let busy = true;
    __fetch = async url => url === '/api/video'
      ? __response(200, {installed: true, port: 30022, state: 'ready', available: true,
                         progress: busy ? {id: 'v1', status: 'queued', progress: 0} : {},
                         run: busy ? {phase: 'denoise', step: 3, steps: 49, s_per_step: 14.8, left_s: 680, label: 'denoising'} : {}})
      : new Promise(() => {});
    feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-video.service': eng('ready', {target: 'video'})})});
    showView('video'); await __settle();
    const count = () => __fetches.filter(f => f.url === '/api/video').length;
    """

    def test_it_stops_when_the_video_ends_and_behind_another_view(self):
        r = run(self, self.BODY + r"""
        await __advance(12000); const during = count();
        showView('traffic'); await __settle(); const left = count();
        await __advance(9000); const away = count();
        showView('video'); await __settle(); busy = false; await __advance(6000); const ended = count();
        await __advance(60000);
        report({during, left, away, ended, later: count()});
        """)
        self.assertGreaterEqual(r["during"], 3)
        self.assertLessEqual(r["away"] - r["left"], 1, "at most the ten-second ask the spine makes while the lane serves")
        self.assertLessEqual(r["later"] - r["ended"], 6, "no five-second follow once it is over")


class ACrashIsNotACancel(unittest.TestCase):
    """A lane that died under a generation came back from the cockpit as interrupted, and
    the page said "Cancelled: the lane was stopped or restarted". The cockpit tells a crash
    from a stop; the page says which."""

    def test_the_image_page_says_the_lane_crashed(self):
        r = run(self, r"""
        __fetch = async url => url === '/api/csrf' ? __response(200, {token: 't'})
          : url === '/api/image/generate' ? __response(502, {crashed: true, error: 'the image lane crashed while this image was being made'})
          : url === '/api/image' ? __response(200, {available: true, installed: true, progress: {}}) : new Promise(() => {});
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})})});
        $('img-prompt').value = 'a cat'; imgSync();
        await imgRun();
        report(toastText());
        """)
        self.assertIn("crashed", r)
        self.assertNotIn("Cancelled", r)

    def test_the_video_page_says_the_lane_crashed(self):
        r = run(self, r"""
        __fetch = async url => url === '/api/csrf' ? __response(200, {token: 't'})
          : url === '/api/video/generate' ? __response(502, {crashed: true, error: 'the video lane crashed (systemd: signal)'})
          : url === '/api/video' ? __response(200, {installed: true, available: true, state: 'ready', progress: {}, run: {}}) : new Promise(() => {});
        feed({lifecycle: life({'qwen38-video.service': eng('ready', {target: 'video'})})});
        await vidLane(); $('vid-prompt').value = 'a fox'; vidSync();
        await vidRun();
        report(toastText());
        """)
        self.assertIn("crashed", r)
        self.assertNotIn("cancelled", r.lower())


class DecideFollowsTheLane(unittest.TestCase):
    """The System One view probed once per page load: after a switch, a stop or a diffusion
    lane taking the box it went on saying it was served, and a probe that once failed left
    Ask disabled for good."""

    def test_the_view_asks_again_when_the_lane_changes(self):
        r = run(self, r"""
        let answer = {available: false, lane: '', reason: 'no text lane is serving'};
        __fetch = async url => url === '/api/systemone' ? __response(200, answer) : new Promise(() => {});
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('stopped')})});
        showView('decide'); await __settle();
        const first = {cap: txt('s1-cap'), off: $('s1-run').disabled, note: txt('s1-note')};
        answer = {available: true, lane: 'qwen3.8-27b', reason: ''};
        feed({lifecycle: life({'qwen38-sglang.service': eng('ready')})}); await __settle();
        report({first, cap: txt('s1-cap'), off: $('s1-run').disabled, noteHidden: $('s1-note').hidden,
                probes: __fetches.filter(f => f.url === '/api/systemone').length});
        """)
        self.assertIn("not served", r["first"]["cap"])
        self.assertTrue(r["first"]["off"])
        self.assertIn("No text lane", r["first"]["note"])
        self.assertIn("qwen3.8-27b", r["cap"])
        self.assertFalse(r["off"])
        self.assertTrue(r["noteHidden"], "the old refusal is gone")
        self.assertEqual(r["probes"], 2)

    def test_a_revisit_asks_again(self):
        r = run(self, r"""
        __fetch = async url => url === '/api/systemone' ? __response(200, {available: true, lane: 'qwen3.8-27b'}) : new Promise(() => {});
        showView('decide'); await __settle(); showView('now'); showView('decide'); await __settle();
        report({probes: __fetches.filter(f => f.url === '/api/systemone').length});
        """)
        self.assertEqual(r["probes"], 2)


class TheLogsViewOffersWhatThePageSendsYouTo(unittest.TestCase):
    """Five places send a person to "its journal in Logs", and the view offered containers
    and the proxy only: no unit journal, so neither a text lane that failed at start (its
    container is gone) nor a diffusion lane (it has no container)."""

    def source(self, engines):
        return run(self, r"""
        __fetch = async () => __response(200, {lines: []});
        feed({units: UNITS, lifecycle: life(%s)});
        showView('logs'); await __settle();
        report($('log-src').value);
        """ % engines)

    def test_a_diffusion_lane_serving_opens_its_journal(self):
        self.assertEqual(self.source("{'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})}"),
                         "qwen38-image.service")
        self.assertEqual(self.source("{'qwen38-video.service': eng('ready', {target: 'video'})}"), "qwen38-video.service")

    def test_a_failed_text_lane_opens_its_journal(self):
        self.assertEqual(self.source("{'qwen38-sglang.service': eng('failed')}"), "qwen38-sglang.service")

    def test_a_serving_text_lane_still_opens_its_container(self):
        self.assertEqual(self.source("{'qwen38-flash.service': eng('ready', {target: 'flash'})}"), "qwen38-flash")

    def test_the_journal_button_of_a_lane_opens_that_journal(self):
        r = run(self, r"""
        __fetch = async () => __response(200, {lines: []});
        feed({units: UNITS, lifecycle: life({'qwen38-flash.service': eng('ready', {target: 'flash'}), 'qwen38-video.service': eng('stopped', {target: 'video'})})});
        LCARDS.get('qwen38-video.service').logs.click(); await __settle();
        report({view: activeView, src: $('log-src').value});
        """)
        self.assertEqual(r, {"view": "logs", "src": "qwen38-video.service"})


class TheSessionCardDoesNotHammerADeadRelay(unittest.TestCase):
    """agent-mobile.js, the script the relay injects into opencode's page on a phone,
    fetched the session list again the moment a fetch failed: with the relay unreachable
    that is a loop, one request per failure (2,021 in 3 s in Chromium)."""

    MARKUP = ('<html><head><meta name="theme-color" content="#fff"></head>'
              '<body><div id="root"><div>opencode</div></div></body></html>')
    REFUSED = "__fetch = () => new Promise((_, no) => setTimeout(() => no(new TypeError('Failed to fetch')), 5));"
    SLOW = "__fetch = () => new Promise(ok => setTimeout(() => ok(__response(200, [])), 4000));"

    def card(self, setup, body):
        return pagejs.run(self, body, setup=setup, scripts=("agent-mobile.js",), markup=self.MARKUP,
                          opts={"media": {"(pointer: coarse)": True}})

    def test_a_failing_fetch_is_retried_later_not_at_once(self):
        r = self.card(self.REFUSED, r"""
        await __advance(3000);
        const early = __fetches.length;
        await __advance(60000);
        report({early, minute: __fetches.length});
        """)
        self.assertLessEqual(r["early"], 2, r)
        self.assertLessEqual(r["minute"], 8, r)
        self.assertGreaterEqual(r["minute"], 2, "it does try again")

    def test_a_slow_answer_is_not_asked_again_meanwhile(self):
        r = self.card(self.SLOW, r"""
        await __advance(3000);
        report({n: __fetches.length});
        """)
        self.assertEqual(r["n"], 1)


class TheEditCommandCannotRunAFileName(unittest.TestCase):
    """The curl shown for an edit put each reference's file name inside double quotes, where
    a shell still expands $(...): a reference named x$(cmd).png ran cmd for whoever pasted
    the line into a terminal."""

    def curl_for(self, name):
        return run(self, r"""
        IS.mode = 'edit'; IS.refs = [{name: NAME, dataUrl: 'data:image/png;base64,AAAA', w: 1, h: 1}];
        imgCurl();
        report(txt('img-curl'));
        """.replace("NAME", json.dumps(name)))

    def pasted(self, cmd):
        """The command as a terminal runs it, with curl replaced by a printer of its argv."""
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run(["bash", "-c", "curl(){ printf '%s\\n' \"$@\"; }\n" + cmd],
                               cwd=d, capture_output=True, text=True, timeout=20)
            made = sorted(x.name for x in pathlib.Path(d).iterdir())
        return p, made

    def test_a_command_in_a_name_is_not_run(self):
        p, made = self.pasted(self.curl_for("x$(touch PWNED)`touch PWNED2`.png"))
        self.assertEqual(made, [], "the pasted command ran something")
        args = p.stdout.splitlines()
        self.assertEqual(args[args.index("-F") + 1], 'image[]=@"x$(touch PWNED)`touch PWNED2`.png";type=image/png')

    def test_quotes_and_semicolons_stay_in_the_name(self):
        p, made = self.pasted(self.curl_for("it's \"a\"; b.png"))
        self.assertEqual((p.returncode, made), (0, []), p.stderr)
        args = p.stdout.splitlines()
        self.assertEqual(args[args.index("-F") + 1], 'image[]=@"it\'s \\"a\\"; b.png";type=image/png')


class AnEditLeavesOutWhatTheEditsEndpointDropsUnread(unittest.TestCase):
    """The editing endpoint has no flow_shift field: the page sent the Shift box's value with
    an edit, and showed it in the edit's curl, and the lane dropped it unread."""

    def payload(self, mode):
        return run(self, r"""
        IS.mode = MODE; $('img-shift').value = '3.5';
        report({payload: imgPayload(), curl: (imgCurl(), txt('img-curl'))});
        """.replace("MODE", json.dumps(mode)))

    def test_a_generation_keeps_the_shift(self):
        r = self.payload("t2i")
        self.assertEqual(r["payload"].get("flow_shift"), 3.5)
        self.assertIn("flow_shift", r["curl"])

    def test_an_edit_leaves_it_out(self):
        r = self.payload("edit")
        self.assertNotIn("flow_shift", r["payload"])
        self.assertNotIn("flow_shift", r["curl"])


class TheCollapsedRailIsADesktopThing(unittest.TestCase):
    """A rail collapsed in a wide window stayed collapsed on a narrow one, where the rail is
    a drawer: 64 px of unlabelled icons, measured at 390 and 800 px."""

    def test_every_collapsed_rail_rule_is_scoped_above_980_px(self):
        rules = [(sel, media) for sel, media, _ in css_rules() if "body.railmin" in sel]
        self.assertTrue(rules, "the page has collapsed-rail rules")
        for sel, media in rules:
            with self.subTest(sel=sel):
                self.assertTrue(any("min-width:981px" in m.replace(" ", "") for m in media), media)



def z_index(sel, media_frag=None):
    """The z-index a selector declares, in the @media whose condition holds media_frag
    (None for a rule outside any @media), or None."""
    for s, media, decl in css_rules():
        if sel not in [x.strip() for x in s.split(",")]:
            continue
        cond = "".join(media).replace(" ", "")
        if (media_frag is None and not media) or (media_frag and media_frag.replace(" ", "") in cond):
            m = re.search(r"z-index:\s*(-?\d+)", decl)
            if m:
                return int(m.group(1))
    return None


class TheDrawerIsNotTrappedUnderItsVeil(unittest.TestCase):
    """On a phone no rail item could be tapped: .shell carried z-index:1, which made it a
    stacking context, so the drawer inside it (z-index 60) stacked as a whole at 1, under
    the veil its own opening draws (body::after, z-index 55). Every tap landed on the veil,
    whose click closes the drawer: the view never changed (2026-09-30). The same trap held
    the Agent's fullscreen frame under the head, over its own exit button. The browser
    check (touch-check.mjs) taps for real; these hold the mechanism in the stylesheet."""

    def test_the_shell_is_no_stacking_context(self):
        decl = [d for s, media, d in css_rules() if s.strip() == ".shell" and not media]
        self.assertTrue(decl, "the page has a .shell rule")
        self.assertNotRegex(decl[0], r"z-index", "a z-index here traps the drawer and the fullscreen frame")

    def test_no_view_keeps_a_filling_animation(self):
        """An animation still filling on opacity keeps the view a stacking context after it
        ends, which held the fullscreen frame under the head with .shell fixed."""
        for s, media, d in css_rules():
            if s.strip() == ".view":
                self.assertNotRegex(d, r"animation:[^;]*\b(both|forwards)\b", d)
                return
        self.fail("no .view rule")

    def test_on_a_phone_the_drawer_is_over_its_veil_and_both_over_the_dock(self):
        rail = z_index(".rail", "max-width:980px")
        veil = z_index("body.railopen::after", "max-width:980px")
        dock = z_index(".dock")
        self.assertIsNotNone(rail); self.assertIsNotNone(veil); self.assertIsNotNone(dock)
        self.assertGreater(rail, veil)
        self.assertGreater(veil, dock, "the dock is under the veil while the drawer is open")

    def test_the_fullscreen_frame_is_over_the_head_and_under_the_dialogs(self):
        frame = z_index("body.agentmax .agentframe")
        self.assertGreater(frame, z_index(".spine"))
        self.assertGreater(frame, z_index(".rail", "max-width:980px"))
        self.assertLess(frame, z_index(".scrim"))


class TheDrawerAndTheDialogsHandTheFocusBack(unittest.TestCase):
    """The drawer had no Escape and a closed dialog left the focus on the body: a keyboard
    started again from the top of the page every time."""

    def test_the_drawer_takes_the_focus_to_where_you_are_and_escape_gives_it_back(self):
        out = run(self, r"""
        showView('lanes');
        $('menubtn').click(); await __advance(10);
        const opened = document.body.classList.contains('railopen');
        const inside = document.activeElement && document.activeElement.dataset.view;
        document.dispatchEvent({type: 'keydown', key: 'Escape'});
        report({opened, inside, closed: !document.body.classList.contains('railopen'),
                back: document.activeElement === $('menubtn')});
        """)
        self.assertEqual(out, {"opened": True, "inside": "lanes", "closed": True, "back": True})

    def test_the_actions_menu_takes_the_focus_and_gives_it_back(self):
        out = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
        $('actbtn').focus(); $('actbtn').click(); await __advance(10);
        const first = document.activeElement && document.activeElement.dataset.act;
        document.dispatchEvent({type: 'keydown', key: 'Escape'});
        report({first, hidden: $('menu').hidden, back: document.activeElement === $('actbtn')});
        """)
        self.assertEqual(out["hidden"], True)
        self.assertEqual(out["back"], True)
        self.assertTrue(out["first"], "the first action has the focus while the menu is open")


class ATableCellCarriesItsColumnName(unittest.TestCase):
    """On a phone the Library's wide tables stack each row into a card, and a line without
    its column's name is a value nobody can read. A cell spanning columns has no one name."""

    def test_each_cell_is_labelled_by_its_column_and_a_span_is_not(self):
        out = run(self, r"""
        const t = $('rcp-table'), tb = t.tBodies[0];
        const a = tb.insertRow(); ['r', 'img', 'ckpt', 'drafter', 'serve', 'disk', 'lane'].forEach(v => { a.insertCell().textContent = v; });
        const b = tb.insertRow(); b.insertCell().textContent = 'bad'; const w = b.insertCell(); w.colSpan = 6; w.textContent = 'invalid';
        labelCells(t);
        report({a: [...a.cells].map(c => c.dataset.label || null), b: [...b.cells].map(c => c.dataset.label || null)});
        """)
        self.assertEqual(out["a"], ["Recipe", "Engine", "Checkpoint", "Drafter", "Serving", "On disk", "Against the lane"])
        self.assertEqual(out["b"], ["Recipe", None])


class ALongPromptIsSaidWhileItIsComputed(unittest.TestCase):
    """Issue #61 (2026-10-30): a prompt of 494,089 tokens the cache could not serve took 19 min before
    its first token on the Ornith lane, and the page said "1 running", its last five minutes "quiet",
    and showed the request "in flight" with an empty Took cell: a user took it for a hang."""

    BODY = r"""
    const U = JSON.parse(JSON.stringify(UNITS));
    U.units['qwen38-sglang.service'] = {active: 'inactive', enabled: 'disabled'};
    U.units['qwen38-ornith-122b.service'] = {active: 'active', enabled: 'enabled'};
    feed({config: CONFIG, units: U, lifecycle: life({'qwen38-ornith-122b.service': eng('ready', {target: 'ornith-122b'})})});
    feed({engine_fast: {load: [{num_reqs: 1, num_waiting_reqs: 0, num_tokens: 1301680, num_used_tokens: 312000}], prompt: %s}});
    feed({feed: {rows: [{ts: '2026-10-30T20:17:24', peer: '127.0.0.1:58172', path: '/v1/chat/completions', bytes: 1885967,
                         secs: null, age: 760.2, kind: 'live', outcome: 'in flight', detail: null}]}});
    feed({metrics: {lane: 'qwen38-ornith-122b.service', requests: 0, watched_s: 160}});
    const row = $('feed').tBodies[0].rows[0];
    report({act: txt('act-facts'), cap: txt('tr-cap'), took: row.cells[4].textContent,
            outcome: row.cells[5].textContent, wm: txt('wm-cap')});
    """
    PROMPT = "{tokens: 494089, held: 312000, share: 0.631, elapsed: 760, left_min_s: 380}"

    def test_how_far_the_prompt_has_got_and_a_floor_of_the_time_left(self):
        r = run(self, self.BODY % self.PROMPT)
        progress = "63 % of 494,089 tokens after 12 min 40, at least 6 min left"
        said = "computing its prompt: " + progress
        # on Now, the Activity panel says it: the view's lede, which said it first, is gone (Hasan, 2026-11-08)
        self.assertIn("Computing its prompt" + progress, r["act"])
        self.assertEqual(r["cap"], "1 running, computing a prompt")
        self.assertEqual(r["took"], "12 min 40", "the request in flight says how long it has been so")
        self.assertIn(said, r["outcome"])
        self.assertEqual(r["wm"], "computing a prompt", "not \"quiet\"")

    def test_without_a_prompt_being_computed_the_page_says_what_it_said(self):
        r = run(self, self.BODY % "null")
        self.assertNotIn("computing", (r["act"] + r["cap"] + r["outcome"]).lower())
        self.assertEqual(r["cap"], "1 running")
        self.assertEqual(r["took"], "12 min 40")
        self.assertEqual(r["wm"], "quiet")

    def test_a_floor_is_said_rounded_down(self):
        # fmtMin rounds to the nearest minute: a floor of 150 s read "at least 3 min" (code review, 2026-10-31)
        for left, said in ((150, "at least 2 min left"), (89.9, "at least 89 s left"), (3659, "at least 1 h 00 left"),
                           (90, "at least 1 min left")):
            with self.subTest(left=left):
                r = run(self, self.BODY % f"{{tokens: 494089, held: 312000, share: 0.631, elapsed: 760, left_min_s: {left}}}")
                self.assertIn(said, r["act"])

    def test_near_its_end_no_floor_is_said(self):
        r = run(self, self.BODY % "{tokens: 494089, held: 489148, share: 0.99, elapsed: 1121, left_min_s: null}")
        self.assertIn("Computing its prompt99 % of 494,089 tokens after 18 min 41, almost done", r["act"])
        self.assertIn("computing its prompt: 99 % of 494,089 tokens after 18 min 41, almost done", r["outcome"])


class ALongPromptSentAgainIsSaid(unittest.TestCase):
    """Issue #61 (2026-10-30): Claude Code left a prompt of 500,161 tokens without an answer after 620 s,
    twice, then 300 s, twice, and the engine kept what it had computed only every other time. The
    proxy says so (v6.49), and the page with it, with what widens the client's timeout."""

    def test_the_banner_says_who_left_after_how_long_and_what_to_change(self):
        r = run(self, r"""
        const now = Date.now() / 1000;
        feed({config: CONFIG, units: UNITS, reqguard: {guard: {}, zombies: {}, state: 'ok', verdict: 'holding',
              resent: {count: 2, last: {at: now - 600, secs: 621, n: 2, who: 'claude-cli/2.1.314',
                                        advice: 'Claude Code gives up on a turn after 10 min, then 5 min without streaming: claude-local lets it wait (docs/clients.md)'}}}});
        report({banner: txt('banners')});
        """)
        self.assertIn("A client gave up on a long prompt and sent it again.", r["banner"])
        self.assertIn("2 times in the last 24 h, the last 10 min 00 ago: claude-cli/2.1.314 left it without an answer "
                      "after 10 min 21 (2 times in a row)", r["banner"])
        self.assertIn("claude-local lets it wait (docs/clients.md).", r["banner"])
        # what its prefix cache kept, if anything: on 2026-10-30 it kept nothing of the first 10 min
        self.assertIn("and the engine computed it again, from what its prefix cache had kept of it, if anything.", r["banner"])

    def test_no_banner_without_a_line(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, reqguard: {guard: {}, zombies: {}, state: 'ok', verdict: 'holding',
              resent: {count: 0, last: null}}});
        report({banner: txt('banners')});
        """)
        self.assertNotIn("sent it again", r["banner"] or "")


class TheCockpitsProbeWearsNoAlarmColour(unittest.TestCase):
    """The request log painted the cockpit's own route probe the red of a failure."""

    def test_a_probe_row_is_not_painted_as_a_failure(self):
        out = run(self, r"""
        feed({feed: {rows: [{ts: '2026-09-30T17:04:05', peer: '127.0.0.1:56836', path: '/v1/systemone', bytes: 40, secs: 0,
                             kind: 'probe', outcome: "422 as designed: the cockpit's probe"},
                            {ts: '2026-09-30T17:04:06', peer: '100.78.198.77:1', path: '/v1/chat/completions', bytes: 9, secs: 0,
                             kind: 'fail', outcome: '503 engine unreachable'}]}});
        report([...$('feed').tBodies[0].rows].map(tr => tr.cells[5].querySelector('.tag').className.trim()));
        """)
        self.assertEqual(sorted(out), ["tag", "tag err"])


class AVideoNeverGoesBackAPhase(unittest.TestCase):
    """Once the decode was done, the lane's journal named no phase while the file was
    written, and the Video view read "Reading the prompt" for the last seconds of every
    run (the reference box, 2026-09-30)."""

    def test_after_the_steps_it_reads_decoding_not_reading_the_prompt(self):
        out = run(self, r"""
        VS.inflight = Date.now(); VS.seen = null;
        VS.run = {phase: 'denoise', step: 48, steps: 49, s_per_step: 12.8, left_s: 13}; vidDrawProgress();
        const during = txt('vid-prog-lab');
        VS.run = null; vidDrawProgress();
        const after = txt('vid-prog-lab');
        VS.inflight = Date.now(); VS.seen = null; VS.run = null; vidDrawProgress();
        report({during, after, fresh: txt('vid-prog-lab')});
        """)
        self.assertTrue(out["during"].startswith("Denoising"), out)
        self.assertEqual(out["after"], "Decoding video and sound")
        self.assertEqual(out["fresh"], "Reading the prompt", "a new request starts from the prompt")


class TheServingLanesPoolIsTheLiveOne(unittest.TestCase):
    """The rack and the Lanes view read the pool from the boots this cockpit watched: one
    restarted after the serving lane booted said "KV pool this boot: 400,384 tokens" while
    the engine reported 491,136 (2026-09-30)."""

    def test_the_serving_text_lane_shows_what_its_engine_reports(self):
        out = run(self, r"""
        const flash = eng('ready', {target: 'flash', pools: {n: 3, last: 400384, min: 400384, max: 575744, spread_pct: 30.5}, boots: [650, 660, 670]});
        const u27 = eng('stopped', {pools: {n: 2, last: 926495, min: 880417, max: 926495, spread_pct: 5}, boots: [460]});
        const units = {units: Object.assign({}, UNITS.units, {'qwen38-flash.service': {active: 'active', enabled: 'enabled'},
                                                          'qwen38-sglang.service': {active: 'inactive', enabled: 'disabled'}})};
        feed({config: CONFIG, units, engine_info: {info: {max_total_num_tokens: 491136}},
              lifecycle: life({'qwen38-flash.service': flash, 'qwen38-sglang.service': u27})});
        report({flash: lanePool('qwen38-flash.service', flash), u27: lanePool('qwen38-sglang.service', u27)});
        """)
        self.assertEqual(out, {"flash": 491136, "u27": 926495})

    def test_right_after_a_switch_the_other_lanes_pool_is_not_shown_as_this_boots(self):
        """The engine read refreshes every 30 s: just after a switch it still holds the
        previous lane's pool, and says which target it read."""
        out = run(self, r"""
        const flash = eng('ready', {target: 'flash', pools: {n: 1, last: 400384}, boots: [650]});
        const units = {units: Object.assign({}, UNITS.units, {'qwen38-flash.service': {active: 'active', enabled: 'enabled'},
                                                          'qwen38-sglang.service': {active: 'inactive', enabled: 'disabled'}})};
        feed({config: CONFIG, units, engine_info: {served_target: 'uncensored', info: {max_total_num_tokens: 926495}},
              lifecycle: life({'qwen38-flash.service': flash})});
        report(lanePool('qwen38-flash.service', flash));
        """)
        self.assertEqual(out, 400384)


class RunningIsWhatTheEngineRuns(unittest.TestCase):
    """The engine's load counts a queued request in num_reqs too (the /get_load shape the
    cockpit projects: running plus waiting, the waiting ones again in num_waiting_reqs). The
    page wrote num_reqs under "Running" beside "Waiting": eight long prompts on a lane that
    ran two and queued three read "5 Running, 3 Waiting" (found live, 2026-10-01)."""

    BODY = r"""
    feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')}),
          engine_fast: {load: [{num_reqs: %d, num_waiting_reqs: %d, num_tokens: 1000, num_used_tokens: 1000}]}});
    report({run: txt('tr-run'), wait: txt('tr-wait'), cap: txt('tr-cap'),
            sub: txt('serving-sub'), act: txt('act-v'), actk: txt('act-k'), series: SERIES.req.slice(-1)[0]});
    """

    def test_running_and_waiting_add_up_to_what_the_engine_holds(self):
        r = run(self, self.BODY % (5, 3))
        self.assertEqual((r["run"], r["wait"]), ("2", "3"), r)
        self.assertEqual(r["cap"], "2 running, 3 waiting", r)
        self.assertTrue(r["sub"].startswith("2 running, 3 waiting"), r)
        self.assertEqual((r["act"], r["actk"]), ("2", "running, 3 waiting"), r)
        self.assertEqual(r["series"], 2, r)

    def test_a_queue_with_nothing_running_says_so(self):
        """Every request queued, none admitted yet: the page said "3 running"."""
        r = run(self, self.BODY % (3, 3))
        self.assertEqual((r["run"], r["wait"]), ("0", "3"), r)
        self.assertEqual(r["cap"], "3 waiting", r)
        self.assertEqual((r["act"], r["actk"]), ("0", "nothing running, 3 waiting"), r)

    def test_one_running_reads_singular_and_idle_reads_idle(self):
        r = run(self, self.BODY % (1, 0))
        self.assertEqual((r["run"], r["cap"]), ("1", "1 running"), r)
        self.assertEqual((r["act"], r["actk"]), ("1", "running, 0 waiting"), r)
        r = run(self, self.BODY % (0, 0))
        self.assertEqual((r["run"], r["wait"], r["cap"]), ("0", "0", "idle"), r)
        self.assertEqual((r["act"], r["actk"]), ("0", "nothing running"), r)



class TheNowViewStartsWithThePool(unittest.TestCase):
    """Hasan, 2026-11-08 (capture, MacBook): "Now" in 40 px over a sentence that said the capsule again (« 27B stock
    holds the box and has served for 22 h 22. No request is running. ») took 100 px above the memory pool for
    nothing: the rail already says Now, the capsule says what serves, its state, its load and how long it has been up."""

    def test_its_title_is_for_screen_readers_and_no_sentence_repeats_the_capsule(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')}),
              engine_fast: {load: [{num_reqs: 0, num_waiting_reqs: 0, num_tokens: 1000, num_used_tokens: 0}]}});
        const v = $('view-now'), h = $('h-now');
        report({sr: h.classList.contains('sr'), labelled: v.getAttribute('aria-labelledby'), head: !!v.querySelector('.vhead'),
                lede: !!$('now-lede'), kids: v.children.map(c => c.id || c.localName), sub: txt('serving-sub')});
        """)
        self.assertTrue(r["sr"], r)
        self.assertEqual(r["labelled"], "h-now")
        self.assertFalse(r["head"], r)
        self.assertFalse(r["lede"], r)
        self.assertEqual(r["kids"][:2], ["h-now", "pool"], "the pool comes first on screen")
        # what the sentence said, the capsule says: idle, up 10 min
        self.assertIn("idle", r["sub"])
        self.assertIn("up 10 min", r["sub"])


class EachVersionIsNamedOnce(unittest.TestCase):
    """The Now view's card of a lane says the official name of the version it loads (decision 1180), its model said
    once: the lane's name with a version after it read "Nemotron 3.5 Bolt Bolt" on the reference box (2026-10-16),
    and the FP4-Local lane's "Flash FP4-Local (NVIDIA) FP4-Local" (2026-10-20). Who publishes the build comes after
    " · " and may share a word with the model (Ornith 1.5 122B NVFP4 · Ornith AI)."""

    def test_no_card_repeats_its_version(self):
        r = run(self, r"""
        const out = {};
        for (const [unit, targets] of Object.entries(LANE_TARGETS)) for (const t of targets) {
          const units = {units: Object.assign({}, UNITS.units, {[unit]: {active: 'active', enabled: 'enabled'}})};
          feed({config: CONFIG, units, lifecycle: life({[unit]: eng('ready', {target: t})})});
          out[t] = bay(unit).h.textContent;
        }
        report(out);
        """)
        self.assertGreaterEqual(len(r), 14, r)
        for t, title in r.items():
            with self.subTest(target=t):
                words = title.split(" · ")[0].split()
                self.assertEqual(len(words), len(set(words)), title)
        self.assertEqual((r["nemotron"], r["nemotron-super"], r["kolibri-nvfp4"], r["stock"], r["flash-local"]),
                         ("Nemotron 3.5 Bolt 24GB · NVIDIA", "Nemotron 3.5 Super VL NVFP4 · NVIDIA",
                          "Kolibri-1 NVFP4 · Quantinine", "Qwen3.8 27B NVFP4 · RadixArk", "Qwen3.8 Flash-Next FP4-Local · NVIDIA"))


class TheLanesViewCountsNoLanes(unittest.TestCase):
    def test_its_intro_names_no_number_of_lanes(self):
        # "Five lanes share one pool" stayed when the sixth came (found on the reference box,
        # 2026-10-16): the view lists them, its intro does not count them
        html = (pagejs.STATIC / "index.html").read_text()
        intro = re.search(r'<h1 id="h-lanes">Lanes</h1><p>(.*?)</p>', html, re.S).group(1)
        self.assertIn("share one pool", intro)
        self.assertNotRegex(intro, r"(?i)\b(two|three|four|five|six|seven|eight|nine|ten|\d+) lanes\b")


class TheStartGuardAndALostSchedulerAreSaidAsTheyAre(unittest.TestCase):
    """A lane the start guard held read "starting", one it refused "keeps crashing: it dies
    during startup", and a server left without its scheduler only "stopped answering",
    which says it may come back (issue #26: none of the three does by waiting)."""
    U = 'qwen38-sglang.service'
    HELD = 'what is left of an engine still holds 16 GiB of GPU memory: 1756018 sglang::stuck 16578 MiB'

    def page(self, engine, config="{}"):
        return run(self, r"""
        const units = {units: Object.assign({}, UNITS.units, {'qwen38-sglang.service': {active: 'activating', enabled: 'enabled'}})};
        feed({config: Object.assign({}, CONFIG, %s), units, lifecycle: life({'qwen38-sglang.service': %s})});
        report({banner: txt('banners'), card: laneCard('qwen38-sglang.service').state.textContent,
                boot: laneCard('qwen38-sglang.service').boot.textContent, bay: bay('qwen38-sglang.service').state.textContent,
                why: laneCard('qwen38-sglang.service').why.textContent, whyClass: laneCard('qwen38-sglang.service').why.className});
        """ % (config, engine))

    def test_a_held_start_says_what_holds_it(self):
        r = self.page("eng('starting', {held: '%s', elapsed: 20})" % self.HELD)
        self.assertIn("waits for GPU memory.", r["banner"])
        self.assertIn("What is left of an engine still holds 16 GiB", r["banner"])
        self.assertIn("It starts by itself once that memory is back", r["banner"])
        self.assertNotIn("crashing", r["banner"])
        self.assertEqual((r["card"], r["bay"]), ("waiting for GPU memory", "waiting for GPU memory"))
        self.assertIn("Waiting for GPU memory: " + self.HELD, r["boot"])

    def test_a_refused_start_is_no_crash_loop(self):
        r = self.page("eng('failed', {restarting: true, restarts: 4, refused: 'the GPU driver holds 50.9 GiB that no process accounts for'})")
        self.assertIn("is not starting.", r["banner"])
        self.assertIn("The GPU driver holds 50.9 GiB that no process accounts for.", r["banner"])
        self.assertIn("systemd checks again every 15 s", r["banner"])
        self.assertNotIn("keeps crashing", r["banner"])
        self.assertEqual(r["card"], "not starting: GPU memory held")

    def test_a_gpu_that_cannot_start_it_is_no_matter_of_memory(self):
        """Issue #54: a flash lane on a GPU outside its ATS mode read "not starting: GPU memory
        held", sent after memory and a reboot that change nothing (review, 2026-10-19)."""
        r = self.page("eng('failed', {restarting: false, cannot: \"the GPU's addressing mode is None "
                      "(nvidia-smi --query-gpu=addressing_mode) and the flash lane needs ATS\"})")
        self.assertIn("cannot start on this box.", r["banner"])
        self.assertIn("The GPU's addressing mode is None", r["banner"])
        self.assertIn("Load another lane from Lanes.", r["banner"])
        for wrong in ("GPU memory", "reboot", "keeps crashing", "memory is back"):
            self.assertNotIn(wrong, r["banner"])
        self.assertEqual(r["card"], "cannot start on this GPU")

    def test_a_missing_file_says_which_and_what_brings_it_back(self):
        """The Flash FP4-Local unit's guard: a file its engine reads is missing, and the unit exited
        78. Its line was of no form the cockpit reads, so the lane said "keeps crashing", and a
        "cannot start" would have blamed the GPU's ATS mode (found in review, 2026-10-20)."""
        why = ("/home/u/flash-local-ple/2a1129b5 is missing (its checkpoint, or a copy of its n-gram table). "
               "Load the lane from the cockpit, or run ./switch-model.sh flash-local")
        r = self.page("eng('failed', {restarting: false, lacks: %s})" % json.dumps(why))
        self.assertIn("cannot start: a file it reads is missing.", r["banner"])
        self.assertIn("/home/u/flash-local-ple/2a1129b5 is missing", r["banner"])
        self.assertIn("./switch-model.sh flash-local", r["banner"])
        for wrong in ("ATS", "GPU memory", "keeps crashing"):
            self.assertNotIn(wrong, r["banner"] + r["why"])
        self.assertEqual(r["card"], "cannot start: a file is missing")
        self.assertIn("Load the lane from the cockpit", r["why"])

    def test_a_missing_files_path_breaks_inside_its_card(self):
        """The reason holds a whole path, with no space to break at, in a flex line that did not
        break: on a phone the page scrolled sideways while the lane was down (found in review,
        2026-10-20, 677 px of page for 500 on screen). The line breaks anywhere, at every width."""
        why = ("/home/u/.cache/huggingface/hub/models--nvidia--Qwen3.8-Flash-Next-FP4-Local/snapshots/"
               "b9ce18a6887e18c089fdea8509a33e63429a3300/model.safetensors.index.json is missing")
        r = self.page("eng('failed', {restarting: false, lacks: %s})" % json.dumps(why))
        self.assertIn(why, r["why"])
        cls = r["whyClass"].split()
        decls = [d for sel, media, d in css_rules() if not media
                 and any(part.strip() == "." + c for c in cls for part in sel.split(","))]
        self.assertTrue(any(re.search(r"overflow-wrap:\s*anywhere", d) and re.search(r"min-width:\s*0\b", d)
                            for d in decls), decls)

    def test_its_banner_goes_once_another_lane_serves(self):
        """The banner says to load another lane, and a unit that exited 78 stays failed until a
        reboot (systemd 255 on the reference box, 2026-10-19: a Conflicts= start and a stop leave
        it so), so the banner stayed red over the lane that serves (found in review, 2026-10-19).
        Its card still says why."""
        r = run(self, r"""
        const units = {units: Object.assign({}, UNITS.units, {'qwen38-flash.service': {active: 'failed', enabled: 'disabled'},
                                                              'qwen38-sglang.service': {active: 'active', enabled: 'enabled'}})};
        const cannot = eng('failed', {cannot: "the GPU's addressing mode is None"});
        feed({config: CONFIG, units, lifecycle: life({'qwen38-flash.service': cannot})});
        const alone = txt('banners');
        feed({config: CONFIG, units, lifecycle: life({'qwen38-flash.service': cannot, 'qwen38-sglang.service': eng('ready')})});
        report({alone, served: txt('banners'), why: laneCard('qwen38-flash.service').why.textContent});
        """)
        self.assertIn("Qwen3.8 Flash-Next cannot start on this box.", r["alone"])
        self.assertNotIn("cannot start on this box.", r["served"])
        self.assertNotIn("Qwen3.8 Flash-Next failed.", r["served"])
        self.assertIn("This GPU cannot start it", r["why"])

    def test_the_card_of_a_lane_its_gpu_cannot_start_says_so_too(self):
        """The banner said the GPU cannot start it, and the card still said to read the journal
        and start it again (found in review, 2026-10-19)."""
        r = run(self, r"""
        const units = {units: Object.assign({}, UNITS.units, {'qwen38-sglang.service': {active: 'failed', enabled: 'enabled'}})};
        feed({config: CONFIG, units, lifecycle: life({'qwen38-sglang.service': eng('failed', {cannot: "the GPU's addressing mode is None"})})});
        report({why: laneCard('qwen38-sglang.service').why.textContent, hidden: laneCard('qwen38-sglang.service').why.hidden});
        """)
        self.assertFalse(r["hidden"])
        self.assertIn("This GPU cannot start it: its addressing mode is not ATS", r["why"])
        self.assertIn("Load another lane.", r["why"])
        self.assertNotIn("read its journal", r["why"])

    def test_a_crash_while_serving_is_no_crash_loop(self):
        r = self.page("eng('failed', {restarting: true, restarts: 1, crashed_serving: true})")
        self.assertIn("stopped while it was serving and is starting again.", r["banner"])
        self.assertIn("comes back by itself", r["banner"])
        self.assertNotIn("keeps crashing", r["banner"])
        self.assertEqual(r["card"], "restarting after a crash")

    def test_a_real_crash_loop_still_says_so(self):
        r = self.page("eng('failed', {restarting: true, restarts: 4})")
        self.assertIn("keeps crashing.", r["banner"])
        self.assertNotIn("not starting", r["banner"])

    def test_a_server_without_its_scheduler_says_who_restarts_it(self):
        r = self.page("eng('degraded', {zombie: true, zombie_in: 90})", "{zombie_restart: true}")
        self.assertIn("lost its scheduler.", r["banner"])
        self.assertIn("would never come back by itself", r["banner"])
        self.assertIn("The cockpit restarts it in 1 min 30", r["banner"])
        self.assertNotIn("stopped answering", r["banner"])
        self.assertEqual(r["card"], "lost its scheduler")
        r = self.page("eng('degraded', {zombie: true, zombie_in: 0})", "{zombie_restart: true}")
        self.assertIn("The cockpit restarts it now.", r["banner"])
        r = self.page("eng('degraded', {zombie: true, zombie_in: 0})", "{zombie_restart: false}")
        self.assertIn("Restart it from Lanes", r["banner"])
        self.assertNotIn("The cockpit restarts it", r["banner"])

    def test_a_plain_degraded_engine_keeps_its_own_words(self):
        r = self.page("eng('degraded')")
        self.assertIn("stopped answering.", r["banner"])
        self.assertNotIn("scheduler", r["banner"])


class AnEmptyStateIsSaidNotPending(unittest.TestCase):
    """'…' is a value on its way. With no text engine serving, the Traffic counters read it
    for good; with nvidia-smi silent, so did the GPU vitals, beside a temperature called
    "cool", "0 processes" nobody had counted and "No process on the GPU" (found by the
    monkey check, 2026-10-02)."""

    def test_the_traffic_counters_say_there_is_no_engine(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('stopped')}),
              engine_fast: {load: null}, decode: {lane: null}});
        report(['tr-run', 'tr-wait', 'tr-tok', 'tr-acc', 'tr-kv', 'tr-cap'].map(txt));
        """)
        self.assertEqual(r[:5], ["none"] * 5, r)
        self.assertEqual(r[5], "no text engine", r)

    def test_a_lane_whose_launch_file_names_no_checkpoint_says_unknown(self):
        r = run(self, r"""
        const cps = () => [...document.querySelectorAll('#view-lanes dt')].filter(d => d.textContent === 'Checkpoint')
                                                                    .map(d => d.nextSibling.textContent);
        feed({config: CONFIG, units: UNITS});
        showView('lanes');
        const before = cps();
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped', {target: 'uncensored'}),
                               'qwen38-flash.service': eng('ready', {target: 'flash', model: '/models/x/Qwen3.8-Flash-NVFP4'})})});
        report({before, after: cps()});
        """)
        self.assertTrue(r["before"] and all(c == "…" for c in r["before"]), r)
        self.assertIn("unknown", r["after"], r)
        self.assertIn("Qwen3.8-Flash-NVFP4", r["after"], r)

    def test_a_box_whose_systemctl_did_not_answer_lists_what_the_lifecycle_lists(self):
        """A CI runner: systemctl answers nothing and no diffusion unit file exists, so the
        lifecycle lists the two text lanes alone. Read as installed, systemd's '?' showed the
        image and video lanes too, their checkpoint "…" for good (GitHub Actions, 2026-10-02)."""
        r = run(self, r"""
        const silent = {units: Object.fromEntries(Object.keys(UNITS.units).map(u => [u, {active: '?', enabled: '?'}]))};
        feed({config: CONFIG, units: silent, lifecycle: life({'qwen38-sglang.service': eng('stopped'),
                                                             'qwen38-flash.service': eng('stopped', {target: 'flash'})})});
        showView('lanes');
        const cps = [...document.querySelectorAll('#view-lanes dt')].filter(d => d.textContent === 'Checkpoint')
                                                                  .map(d => d.nextSibling.textContent);
        report({cps, image: installed('qwen38-image.service'), video: installed('qwen38-video.service'),
                kolibri: installed('qwen38-kolibri.service'), nemotron: installed('qwen38-nemotron.service'),
                nsuper: installed('qwen38-nemotron-super.service'), flocal: installed('qwen38-flash-local.service'),
                ornith: installed('qwen38-ornith-122b.service'),
                u27: installed('qwen38-sglang.service'), flash: installed('qwen38-flash.service')});
        """)
        self.assertNotIn("…", r["cps"], r)
        self.assertEqual(sorted(r["cps"]), ["not installed"] * 7 + ["unknown"] * 2, r)
        self.assertEqual((r["image"], r["video"], r["kolibri"], r["nemotron"], r["nsuper"], r["flocal"], r["ornith"], r["u27"],
                          r["flash"]), (False, False, False, False, False, False, False, True, True), r)

    def test_a_gpu_that_did_not_answer_reads_no_reading_and_nothing_made_up(self):
        r = run(self, r"""
        const v = id => [$(id).children[1].textContent, $(id).children[2].textContent];
        const procs = () => $('gpu-procs').tBodies[0].textContent;
        feed({config: CONFIG, units: UNITS, lifecycle: life({})});
        const before = v('vt-gpu');
        feed({gpu: {node_id: 'local', power_w: null, temp_c: null, procs: null},
              kernel: {node_id: 'local', nvrm_oom_1h: null, nvrm_last: null}});
        const silent = {gpu: v('vt-gpu'), temp: v('vt-temp'), nvrm: v('vt-nvrm'), procs: procs(),
                        cap: txt('gpu-cap'), lamp: $('gpu-cap').className, dk: txt('dk-nvrm')};
        feed({gpu: {node_id: 'local', power_w: 31.4, temp_c: 52, procs: []},
              kernel: {node_id: 'local', nvrm_oom_1h: 0, nvrm_last: null}});
        const read = {gpu: v('vt-gpu'), temp: v('vt-temp'), nvrm: v('vt-nvrm'), procs: procs(), cap: txt('gpu-cap')};
        report({before, silent, read});
        """)
        self.assertEqual(r["before"][0], "…", "before the first answer the value is on its way")
        self.assertEqual(r["silent"]["gpu"], ["n/a", ""], r)
        self.assertEqual(r["silent"]["temp"], ["n/a", ""], r)
        self.assertEqual(r["silent"]["nvrm"], ["n/a", ""], r)
        self.assertEqual(r["silent"]["procs"], "No reading: nvidia-smi did not answer.", r)
        self.assertEqual((r["silent"]["cap"], r["silent"]["lamp"]), ("n/a", "cap"), r)
        self.assertTrue(r["silent"]["dk"].startswith("n/a"), r)
        self.assertEqual(r["read"]["gpu"], ["31 W", "0 processes"], r)
        self.assertEqual(r["read"]["temp"], ["52 °C", "cool"], r)
        self.assertEqual(r["read"]["nvrm"], ["0", "none"], r)
        self.assertEqual((r["read"]["procs"], r["read"]["cap"]), ("No process on the GPU.", "52 °C"), r)


class TheBootLaneIsSaidOnlyWhenOneIs(unittest.TestCase):
    """With no lane enabled at boot, the spine said "27B starts at boot" (the page's default
    for which logs to open, said as a fact), and Load left out the step that makes the lane
    the boot one (found 2026-10-02)."""

    BODY = r"""
    const units = JSON.parse(JSON.stringify(UNITS));
    for (const u of ['qwen38-sglang.service', 'qwen38-flash.service', 'qwen38-image.service', 'qwen38-video.service', 'qwen38-kolibri.service',
                     'qwen38-nemotron.service', 'qwen38-nemotron-super.service', 'qwen38-flash-local.service',
                     'qwen38-ornith-122b.service'])
      if (%s !== null) units.units[u].enabled = %s;
    feed({config: CONFIG, units, lifecycle: life({'qwen38-sglang.service': eng('stopped', {target: 'uncensored'}),
                                                 'qwen38-flash.service': eng('stopped', {target: 'flash'})})});
    askJourney('uncensored');
    const steps = [...$('sh-journey').children].map(li => li.textContent);
    closeSheet();
    showView('lanes');
    const atBoot = [...document.querySelectorAll('#view-lanes dt')].filter(d => d.textContent === 'At boot').map(d => d.nextSibling.textContent);
    const notes = [...BAYS.values()].map(b => b.note.textContent);
    report({sub: txt('serving-sub'), facts: txt('act-facts'), steps, atBoot, notes});
    """

    def page(self, enabled):
        return run(self, self.BODY % (enabled, enabled))

    def test_no_lane_enabled_says_so_and_load_makes_the_lane_the_boot_one(self):
        r = self.page("'disabled'")
        self.assertEqual(r["sub"], "no lane starts at boot; load one from Lanes", r)
        self.assertTrue(r["atBoot"] and all(a == "manual start" for a in r["atBoot"]), r["atBoot"])
        self.assertEqual(r["notes"], ["manual start"] * 9, r["notes"])
        self.assertIn("Boot lanenone", r["facts"])
        self.assertEqual(len(r["steps"]), 2, r["steps"])
        self.assertIn("switch-model.sh uncensored", r["steps"][0])
        self.assertIn("systemctl start qwen38-sglang.service", r["steps"][1])

    def test_a_systemctl_that_did_not_answer_claims_nothing(self):
        r = self.page("'?'")
        self.assertEqual(r["sub"], "load a lane from Lanes", r)
        self.assertIn("Boot laneunknown", r["facts"])
        self.assertTrue(r["atBoot"] and all(a == "unknown" for a in r["atBoot"]), r["atBoot"])
        # the text lanes say nothing of the boot; the diffusion lanes, which this lifecycle
        # does not list, say how to install them
        self.assertEqual(r["notes"][:2], ["", ""], r["notes"])
        self.assertNotIn("manual start", r["notes"], r["notes"])

    def test_the_lane_enabled_at_boot_is_named_and_not_switched_to_again(self):
        r = self.page("null")
        self.assertEqual(r["sub"], "Qwen3.8 27B starts at boot; load a lane from Lanes", r)
        self.assertIn("Boot laneQwen3.8 27B", r["facts"])
        self.assertEqual(len(r["steps"]), 1, r["steps"])
        self.assertIn("systemctl start qwen38-sglang.service", r["steps"][0])


class TheLogsSayTheBoxsTimeAndDimTheBoxsOwnPolling(unittest.TestCase):
    """The cockpit asks the engine its load every second, and those lines filled the whole
    engine log while only /health was dimmed; and the engine writes its times in UTC, which
    the server now rewrites in the box's own time and the page says so (2026-10-02)."""

    BODY = r"""
    let answer = null;
    __fetch = async url => url.startsWith('/api/logs/') ? __response(200, answer) : new Promise(() => {});
    feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-flash.service': eng('ready', {target: 'flash'})})});
    const read = async a => { answer = a; await tailLog(); await __settle();
      return {cls: [...$('log-view').children].map(c => c.className || ''), note: txt('log-note'), hidden: $('log-note').hidden}; };
    const lines = ['[2026-10-02 18:37:16] INFO:     127.0.0.1:1 - "GET /v1/loads?include=core HTTP/1.1" 200 OK',
                   '[2026-10-02 18:37:17] INFO:     127.0.0.1:2 - "GET /server_info HTTP/1.1" 200 OK',
                   '[2026-10-02 18:37:18] INFO:     127.0.0.1:3 - "POST /v1/chat/completions HTTP/1.1" 200 OK'];
    report({converted: await read({name: 'qwen38-flash', tz: 'CEST', local_stamps: 3, lines}),
            native: await read({name: 'qwen38-flash', tz: 'CEST', local_stamps: 0, lines})});
    """

    def test_the_polling_is_dimmed_and_a_conversion_is_said(self):
        r = run(self, self.BODY)
        self.assertEqual(r["converted"]["cls"], ["l-dim", "l-dim", ""], r)
        self.assertFalse(r["converted"]["hidden"], r)
        self.assertIn("local time (CEST)", r["converted"]["note"])
        self.assertIn("UTC", r["converted"]["note"])
        self.assertTrue(r["native"]["hidden"], "no line was rewritten: nothing to say")


class TheTrafficBadgeCountsEveryRequestInFlight(unittest.TestCase):
    """The badge counts the feed's rows in flight, and the server now sends every request
    still in flight, however old, beside the 25 newest: eight agents read "8 running"
    beside a badge that fell to 2 (2026-10-02)."""

    def test_an_old_request_in_flight_is_in_the_badge_and_the_table(self):
        r = run(self, r"""
        const row = (ts, peer, outcome, kind, secs) => ({ts, peer, method: 'POST', path: '/v1/chat/completions', bytes: 1000,
                                                        outcome, kind, secs, detail: null});
        const rows = [row('2026-10-02T23:16:01', '127.0.0.1:49398', 'in flight', 'live', null)];
        for (let i = 0; i < 25; i++) rows.push(row('2026-10-02T23:40:' + String(i).padStart(2, '0'), '127.0.0.1:' + (41000 + i), 'ok', 'ok', 1.0));
        rows.push(row('2026-10-02T23:41:00', '127.0.0.1:42000', 'in flight', 'live', null));
        feed({config: CONFIG, units: UNITS, feed: {rows}});
        const live = [...$('feed').tBodies[0].children].filter(tr => tr.textContent.includes('in flight')).length;
        report({badge: txt('bdg-traffic'), live, rows: $('feed').tBodies[0].children.length});
        """)
        self.assertEqual(r["badge"], "2", r)
        self.assertEqual((r["live"], r["rows"]), (2, 27), r)


FLASH_REPO = "RadixArk/Qwen3.8-Flash-Next-NVFP4"
FLASH_UNC_REPO = "dealignai/Qwen3.8-Flash-Next-ABLITERATED-NVFP4"


class ASwitchNotRestartedYetIsSaidAndFinished(unittest.TestCase):
    """A switch rewrites what a lane's next start loads and restarts nothing. When the restart
    after it did not run (a journey stopped after its first step, switch-model.sh from a
    terminal), Load read the unit's file alone, had no step left and said "already serving"
    of a checkpoint the engine did not serve, the lane's card named the next checkpoint as the
    one serving, and nothing said a restart was owed (found 2026-10-05: flash served while its
    unit pointed at flash-uncensored, and the switch could not be finished from the page)."""

    def page(self, served, served_repo, nxt, next_repo, info_age, ask):
        # the engine's own read is info_age seconds old, against an engine up for 600 s
        return run(self, r"""
        const units = JSON.parse(JSON.stringify(UNITS));
        units.units['qwen38-sglang.service'] = {active: 'inactive', enabled: 'disabled'};
        units.units['qwen38-flash.service'] = {active: 'active', enabled: 'enabled'};
        const now = Date.now() / 1000, args = %s;
        apply({config: {data: CONFIG, ts: now}, units: {data: units, ts: now},
               engine_info: {data: {served_target: args.served, prompt_ceiling_tokens: 0,
                                    info: {model_path: args.served_repo, served_model_name: 'x',
                                           max_total_num_tokens: 800000, context_length: 262144}},
                             ts: now - args.info_age},
               lifecycle: {data: life({'qwen38-flash.service': eng('ready', {target: args.next, model: args.next_repo, elapsed: 600}),
                                       'qwen38-sglang.service': eng('stopped', {target: 'stock'})}), ts: now}});
        showView('lanes');
        const dl = LCARDS.get('qwen38-flash.service').right, card = {};
        [...dl.children].forEach((n, k, a) => { if (n.tagName === 'DT') card[n.textContent] = a[k + 1].textContent; });
        const banners = txt('banners');
        askJourney(args.ask);
        report({open: !$('scrim').hidden, steps: [...$('sh-journey').children].map(li => li.textContent),
                toast: toastText(), banners, card, label: laneLabel('qwen38-flash.service')});
        """ % json.dumps({"served": served, "served_repo": served_repo, "next": nxt,
                          "next_repo": next_repo, "info_age": info_age, "ask": ask}))

    def test_load_offers_the_restart_the_switch_still_owes(self):
        r = self.page("flash", FLASH_REPO, "flash-uncensored", FLASH_UNC_REPO, 5, "flash-uncensored")
        self.assertTrue(r["open"], r)
        self.assertEqual(len(r["steps"]), 1, r["steps"])
        self.assertIn("Restart Qwen3.8 Flash-Next on the new checkpoint", r["steps"][0])
        self.assertIn("systemctl restart qwen38-flash.service", r["steps"][0])
        self.assertNotIn("already serving", r["toast"])

    def test_the_banner_and_the_card_say_what_serves_and_what_comes_next(self):
        r = self.page("flash", FLASH_REPO, "flash-uncensored", FLASH_UNC_REPO, 5, "flash-uncensored")
        self.assertIn("Qwen3.8 Flash-Next NVFP4 · RadixArk is serving, and the lane's next start loads Qwen3.8 Flash-Next abliterated NVFP4 · dealignai.", r["banners"])
        self.assertIn("clients still get Qwen3.8 Flash-Next NVFP4 · RadixArk", r["banners"])
        self.assertEqual(r["card"]["Checkpoint"], "Qwen3.8-Flash-Next-NVFP4", r["card"])
        self.assertEqual(r["card"]["Target"], "Qwen3.8 Flash-Next NVFP4 · RadixArk", r["card"])
        self.assertEqual(r["card"]["Next start loads"], "Qwen3.8 Flash-Next abliterated NVFP4 · dealignai", r["card"])
        self.assertEqual(r["label"], "Qwen3.8 Flash-Next NVFP4 · RadixArk", r)

    def test_a_lane_serving_what_its_unit_loads_owes_nothing(self):
        r = self.page("flash", FLASH_REPO, "flash", FLASH_REPO, 5, "flash")
        self.assertFalse(r["open"], r)
        self.assertIn("already serving", r["toast"])
        self.assertNotIn("next start loads", r["banners"])
        self.assertNotIn("Next start loads", r["card"])
        self.assertEqual(r["card"]["Checkpoint"], "Qwen3.8-Flash-Next-NVFP4", r["card"])

    def test_a_read_older_than_the_engine_names_nothing(self):
        # read 700 s ago, from the engine before this one (up 600 s): the unit's file is the
        # best word on what this engine loaded, and no restart is invented from the old read
        r = self.page("flash", FLASH_REPO, "flash-uncensored", FLASH_UNC_REPO, 700, "flash-uncensored")
        self.assertFalse(r["open"], r)
        self.assertIn("already serving", r["toast"])
        self.assertNotIn("next start loads", r["banners"])
        self.assertNotIn("Next start loads", r["card"])
        self.assertEqual(r["card"]["Target"], "Qwen3.8 Flash-Next abliterated NVFP4 · dealignai", r["card"])
        self.assertEqual(r["card"]["Checkpoint"], "Qwen3.8-Flash-Next-ABLITERATED-NVFP4", r["card"])

    def test_a_diffusion_lane_never_takes_the_text_engines_target(self):
        # a fresh read naming a 27B target, the image lane serving: its label and its
        # journey are its own, and no "next start" banner pairs the two lanes
        r = run(self, r"""
        const now = Date.now() / 1000;
        apply({config: {data: CONFIG, ts: now}, units: {data: UNITS, ts: now},
               engine_info: {data: {served_target: 'stock', prompt_ceiling_tokens: 0, info: {model_path: 'x/y'}}, ts: now - 5},
               lifecycle: {data: life({'qwen38-image.service': eng('ready', {target: 'image', elapsed: 600}),
                                       'qwen38-sglang.service': eng('stopped', {target: 'stock'})}), ts: now}});
        report({label: laneLabel('qwen38-image.service'), served: servedTarget('qwen38-image.service'),
                lane: laneTarget('qwen38-image.service'), banners: txt('banners')});
        """)
        self.assertIsNone(r["served"], r)
        self.assertEqual(r["lane"], "image", r)
        self.assertNotIn("27B", r["label"], r)
        self.assertNotIn("next start loads", r["banners"])



class AnUpdateOwedToTheServingEngineIsSaidAndFinished(unittest.TestCase):
    """An update rewrote what the serving engine starts with and left it serving: what it
    brought applies at the next start, and only the installer's output said so (found
    2026-10-16: the prefix cache settings v1.35.0 gives Bolt's unit). The banner strip says it,
    the lane's card offers the restart, and its Load is that one restart."""

    def page(self, owed):
        return run(self, r"""
        const units = JSON.parse(JSON.stringify(UNITS));
        units.units['qwen38-sglang.service'] = {active: 'inactive', enabled: 'disabled'};
        units.units['qwen38-nemotron.service'] = {active: 'active', enabled: 'enabled'};
        const now = Date.now() / 1000, owed = %s;
        apply({config: {data: CONFIG, ts: now}, units: {data: units, ts: now},
               update: {data: Object.assign({installed: 'v1.35.0'}, owed ? {stale_engines: {'qwen38-nemotron.service': 'Its unit changed (what it runs and how)'}} : {}), ts: now},
               lifecycle: {data: life({'qwen38-nemotron.service': eng('ready', {target: 'nemotron'}),
                                       'qwen38-sglang.service': eng('stopped', {target: 'stock'})}), ts: now}});
        showView('lanes');
        const c = LCARDS.get('qwen38-nemotron.service');
        const out = {banners: txt('banners'), loadHidden: c.load.hidden, loadText: c.load.textContent,
                     name: LANE_NAME['qwen38-nemotron.service']};
        askJourney('nemotron');
        report(Object.assign(out, {open: !$('scrim').hidden, toast: toastText(),
                                   steps: [...$('sh-journey').children].map(li => li.textContent)}));
        """ % ("true" if owed else "false"))

    def test_the_banner_says_what_the_update_left_behind(self):
        r = self.page(True)
        self.assertIn("runs what it started with, not what the update installed.", r["banners"])
        self.assertIn("Its unit changed (what it runs and how). It applies at its next start", r["banners"])

    def test_the_card_offers_the_restart_and_its_load_is_that_restart(self):
        r = self.page(True)
        self.assertFalse(r["loadHidden"], r)
        self.assertEqual(r["loadText"], f"Restart {r['name']} to apply the update")
        self.assertTrue(r["open"], r)
        self.assertEqual(len(r["steps"]), 1, r["steps"])
        self.assertIn(f"Restart {r['name']} on what the update installed", r["steps"][0])
        self.assertIn("systemctl restart qwen38-nemotron.service", r["steps"][0])

    def test_a_switch_not_restarted_yet_is_not_an_update(self):
        # flash serves flash, its launcher was switched to flash-uncensored and not restarted:
        # the file the switch rewrote reads as changed after the start, and the restart offered
        # as "apply the update" switched it back (found in review, 2026-10-17)
        r = run(self, r"""
        const units = JSON.parse(JSON.stringify(UNITS));
        units.units['qwen38-sglang.service'] = {active: 'inactive', enabled: 'disabled'};
        units.units['qwen38-flash.service'] = {active: 'active', enabled: 'enabled'};
        const now = Date.now() / 1000;
        apply({config: {data: CONFIG, ts: now}, units: {data: units, ts: now},
               update: {data: {installed: 'v1.35.0', stale_engines: {'qwen38-flash.service': 'launch-flash.sh changed (a file it reads when it starts)'}}, ts: now},
               engine_info: {data: {served_target: 'flash', prompt_ceiling_tokens: 0,
                                    info: {model_path: '%s', served_model_name: 'x',
                                           max_total_num_tokens: 800000, context_length: 262144}}, ts: now - 5},
               lifecycle: {data: life({'qwen38-flash.service': eng('ready', {target: 'flash-uncensored', model: '%s', elapsed: 600}),
                                       'qwen38-sglang.service': eng('stopped', {target: 'stock'})}), ts: now}});
        showView('lanes');
        const c = LCARDS.get('qwen38-flash.service');
        report({banners: txt('banners'), loadText: c.load.textContent});
        """ % (FLASH_REPO, FLASH_UNC_REPO))
        self.assertIn("next start loads Qwen3.8 Flash-Next abliterated NVFP4 · dealignai", r["banners"])
        self.assertNotIn("not what the update installed", r["banners"])
        self.assertNotIn("to apply the update", r["loadText"])

    def test_an_engine_that_boots_owes_nothing_yet(self):
        # the tier's answer from before the restart still names the engine that now boots:
        # offering the restart again would throw that boot away (found in review, 2026-10-17)
        r = run(self, r"""
        const units = JSON.parse(JSON.stringify(UNITS));
        units.units['qwen38-sglang.service'] = {active: 'inactive', enabled: 'disabled'};
        units.units['qwen38-nemotron.service'] = {active: 'activating', enabled: 'enabled'};
        const now = Date.now() / 1000;
        apply({config: {data: CONFIG, ts: now}, units: {data: units, ts: now},
               update: {data: {installed: 'v1.35.0', stale_engines: {'qwen38-nemotron.service': 'Its unit changed (what it runs and how)'}}, ts: now},
               lifecycle: {data: life({'qwen38-nemotron.service': eng('loading-weights', {target: 'nemotron'}),
                                       'qwen38-sglang.service': eng('stopped', {target: 'stock'})}), ts: now}});
        showView('lanes');
        const c = LCARDS.get('qwen38-nemotron.service');
        report({banners: txt('banners'), loadText: c.load.textContent, loadHidden: c.load.hidden});
        """)
        self.assertNotIn("not what the update installed", r["banners"])
        self.assertFalse(not r["loadHidden"] and "to apply the update" in r["loadText"], r)

    def test_the_update_restart_leaves_the_boot_lane_alone(self):
        # Bolt was started as installed while the 27B stays the lane enabled at boot: the Load
        # that applies the update is the restart alone, and it does not point the boot at Bolt
        # (found in review, 2026-10-17)
        r = run(self, r"""
        const units = JSON.parse(JSON.stringify(UNITS));
        units.units['qwen38-sglang.service'] = {active: 'inactive', enabled: 'enabled'};
        units.units['qwen38-nemotron.service'] = {active: 'active', enabled: 'disabled'};
        const now = Date.now() / 1000;
        apply({config: {data: CONFIG, ts: now}, units: {data: units, ts: now},
               update: {data: {installed: 'v1.35.0', stale_engines: {'qwen38-nemotron.service': 'Its unit changed (what it runs and how)'}}, ts: now},
               lifecycle: {data: life({'qwen38-nemotron.service': eng('ready', {target: 'nemotron'}),
                                       'qwen38-sglang.service': eng('stopped', {target: 'stock'})}), ts: now}});
        showView('lanes');
        askJourney('nemotron');
        report({open: !$('scrim').hidden, steps: [...$('sh-journey').children].map(li => li.textContent)});
        """)
        self.assertTrue(r["open"], r)
        self.assertEqual(len(r["steps"]), 1, r["steps"])
        self.assertIn("on what the update installed", r["steps"][0])
        self.assertNotIn("Point the boot", " ".join(r["steps"]))

    def flash_page(self, state, nxt, next_repo, ask, owed=True):
        return run(self, r"""
        const units = JSON.parse(JSON.stringify(UNITS));
        units.units['qwen38-sglang.service'] = {active: 'inactive', enabled: 'disabled'};
        units.units['qwen38-flash.service'] = {active: 'active', enabled: 'enabled'};
        const now = Date.now() / 1000, a = %s;
        apply({config: {data: CONFIG, ts: now}, units: {data: units, ts: now},
               update: {data: Object.assign({installed: 'v1.35.0'}, a.owed ? {stale_engines: {'qwen38-flash.service': 'Its unit changed (what it runs and how)'}} : {}), ts: now},
               engine_info: {data: {served_target: 'flash', prompt_ceiling_tokens: 0,
                                    info: {model_path: a.flash_repo, served_model_name: 'x',
                                           max_total_num_tokens: 800000, context_length: 262144}}, ts: now - 5},
               lifecycle: {data: life({'qwen38-flash.service': eng(a.state, {target: a.next, model: a.next_repo, elapsed: 600}),
                                       'qwen38-sglang.service': eng('stopped', {target: 'stock'})}), ts: now}});
        showView('lanes');
        const banners = txt('banners');
        askJourney(a.ask);
        report({banners, open: !$('scrim').hidden, steps: [...$('sh-journey').children].map(li => li.textContent)});
        """ % json.dumps({"state": state, "next": nxt, "next_repo": next_repo, "ask": ask, "owed": owed,
                          "flash_repo": FLASH_REPO}))

    def test_another_target_picked_with_an_update_owed_still_switches_then_restarts(self):
        # the update's restart is the Load of the target that serves; another target picked on the
        # card keeps its own journey, the switch and then the restart (found in review, 2026-10-17)
        r = self.flash_page("ready", "flash", FLASH_REPO, "flash-uncensored")
        self.assertTrue(r["open"], r)
        self.assertEqual(len(r["steps"]), 2, r["steps"])
        self.assertIn("Point the boot at", r["steps"][0])
        self.assertIn("Restart Qwen3.8 Flash-Next", r["steps"][1])

    def test_a_degraded_engine_with_a_switch_waiting_is_said(self):
        # the update banner keeps quiet while a switch waits; the switch's banner had to say it in
        # degraded too, or nothing did (found in review, 2026-10-17)
        r = self.flash_page("degraded", "flash-uncensored", FLASH_UNC_REPO, "flash-uncensored")
        self.assertIn("next start loads Qwen3.8 Flash-Next abliterated NVFP4 · dealignai", r["banners"])
        self.assertNotIn("not what the update installed", r["banners"])

    def test_an_engine_up_to_date_owes_nothing(self):
        r = self.page(False)
        self.assertNotIn("what the update installed", r["banners"])
        self.assertTrue(r["loadHidden"], r)
        self.assertFalse(r["open"], r)
        self.assertIn("already serving", r["toast"])

class TheLoadWindowFollowsAStepToItsEnd(unittest.TestCase):
    """The window gave each step 20 min and then said "did not finish in time": a switch has
    7,200 s on the server, for a download, and the switch to flash-uncensored (135 GB) took
    22 min, finished well, and the restart after it never ran. And all that time the window
    said "Step 1 of 2" and nothing else, which read as frozen (2026-10-05)."""

    BODY = r"""
    let jobs = 0;
    __fetch = async url => url === '/api/csrf' ? __response(200, {token: 't'})
      : url === '/api/action' ? __response(202, {job: 'j' + (++jobs)})
      : new Promise(() => {});
    const units = JSON.parse(JSON.stringify(UNITS));
    feed({config: CONFIG, units, lifecycle: life({'qwen38-sglang.service': eng('stopped', {target: 'stock'}),
                                                  'qwen38-flash.service': eng('stopped', {target: 'flash'})})});
    askJourney('uncensored');
    const steps0 = [...$('sh-journey').children].map(li => li.textContent);
    $('sh-go').click(); await __settle(); await __advance(1000);
    const job = (current, recent) => feed({job: {current, recent: recent || []}});
    const running = (id, elapsed, line) => job({id, action: 'switch', params: {target: 'uncensored'}, elapsed, lines: [line]});
    const state = () => ({cls: [...$('sh-journey').children].map(li => li.className),
                          status: txt('sh-status'), live: [...$('sh-journey').children].map(li => (li.querySelector('.live') || {}).textContent || ''),
                          actions: __fetches.filter(f => f.url === '/api/action').map(f => JSON.parse(f.init.body).name)});
    """

    def test_a_long_step_is_followed_past_twenty_minutes_and_the_next_one_runs(self):
        r = run(self, self.BODY + r"""
        for (let s = 60; s <= 25 * 60; s += 60){
          running('j1', s, '\x1b[1;36mdownloading dealignai/x: ' + s + ' GB\x1b[0m');
          await __advance(60000);
        }
        const at25 = state();
        job(null, [{id: 'j1', action: 'switch', status: 'done', rc: 0, elapsed: 1500}]);
        await __advance(2000); await __settle(); await __advance(2000);
        report({steps0, at25, after: state()});
        """)
        self.assertEqual(len(r["steps0"]), 2, r["steps0"])
        self.assertEqual(r["at25"]["cls"][0], "run", r["at25"])
        self.assertNotIn("did not finish", r["at25"]["status"])
        self.assertIn("25 min 00 so far: downloading dealignai/x: 1500 GB", r["at25"]["live"][0])
        self.assertNotIn("\x1b", r["at25"]["live"][0])
        self.assertEqual(r["after"]["cls"][0], "done", r["after"])
        self.assertEqual(r["after"]["live"][0], "", "the live line goes once the step is done")
        self.assertEqual(r["after"]["actions"], ["switch", "unit"], "the step after the download ran")

    def test_a_failed_step_says_so_and_stops_the_chain(self):
        r = run(self, self.BODY + r"""
        running('j1', 30, 'downloading');
        await __advance(1000);
        job(null, [{id: 'j1', action: 'switch', status: 'failed', rc: 1, elapsed: 31}]);
        await __advance(2000); await __settle();
        report({after: state()});
        """)
        self.assertEqual(r["after"]["cls"][0], "fail", r["after"])
        self.assertIn("Step 1 failed with exit code 1. Its log is in the dock.", r["after"]["status"])
        self.assertEqual(r["after"]["actions"], ["switch"], "nothing ran after the failed step")

    def test_a_job_the_cockpit_forgot_is_said_unknown_not_failed(self):
        r = run(self, self.BODY + r"""
        running('j1', 30, 'downloading');
        await __advance(1000);
        job(null, []);                                 // a cockpit restarted: no history
        await __advance(30000); const at30 = state();
        await __advance(40000);
        report({at30, after: state()});
        """)
        self.assertEqual(r["at30"]["cls"][0], "run", "a minute of grace before saying anything")
        self.assertEqual(r["after"]["cls"][0], "fail", r["after"])
        self.assertIn("is no longer known to the cockpit (restarted?), so how it ended is unknown", r["after"]["status"])
        self.assertIn("The Now view and Logs say where the lane is", r["after"]["status"])
        self.assertEqual(r["after"]["actions"], ["switch"], r["after"])


class TheLoadWindowCanBeHiddenWhileItsStepsRun(unittest.TestCase):
    """While a step ran, the load window could not be closed: Cancel was disabled, a click
    outside it and Escape did nothing, and a 20-minute download held the whole page until it
    was reloaded, which also stopped the chain (Hasan, 2026-10-12). Now Hide puts the window
    away, the steps go on, the job button brings it back, a failure brings it back on its own,
    and nothing else starts while the hidden chain runs."""

    BODY = TheLoadWindowFollowsAStepToItsEnd.BODY + r"""
    const done = (id, name) => ({id, action: name, status: 'done', rc: 0, elapsed: 31});
    """

    def test_hide_puts_the_window_away_and_the_steps_go_on(self):
        r = run(self, self.BODY + r"""
        running('j1', 30, 'downloading'); await __advance(1000);
        const during = {label: txt('sh-cancel'), disabled: $('sh-cancel').disabled};
        $('sh-cancel').click(); await __settle();
        const hidden = $('scrim').hidden;
        job(null, [done('j1', 'switch')]);
        await __advance(2000); await __settle(); await __advance(2000);
        report({during, hidden, actions: state().actions});
        """)
        self.assertEqual(r["during"], {"label": "Hide", "disabled": False}, r)
        self.assertTrue(r["hidden"], "Hide puts the window away")
        self.assertEqual(r["actions"], ["switch", "unit"], "the next step ran with the window hidden")

    def test_nothing_else_starts_while_a_hidden_load_runs(self):
        r = run(self, self.BODY + r"""
        running('j1', 30, 'downloading'); await __advance(1000);
        $('sh-cancel').click(); await __settle();
        job(null, [done('j1', 'switch')]);              // between two steps: no job runs
        await __advance(600); await __settle();
        askAction('flush_cache', {});
        report({scrim: $('scrim').hidden, toast: toastText(), mode: SHEET && SHEET.mode});
        """)
        self.assertTrue(r["scrim"], "no other window opened")
        self.assertEqual(r["mode"], "journey", "the running load keeps the window")
        self.assertIn("is still running", r["toast"])

    def test_a_hidden_load_that_ends_leaves_the_view_alone(self):
        r = run(self, self.BODY + r"""
        showView('library');
        running('j1', 30, 'downloading'); await __advance(1000);
        $('sh-cancel').click(); await __settle();
        job(null, [done('j1', 'switch')]); await __advance(2000); await __settle(); await __advance(1000);
        job(null, [done('j2', 'unit'), done('j1', 'switch')]); await __advance(2000); await __settle(); await __advance(2000);
        report({view: activeView, sheet: SHEET, scrim: $('scrim').hidden, toast: toastText(), actions: state().actions});
        """)
        self.assertEqual(r["actions"], ["switch", "unit"], r)
        self.assertEqual(r["view"], "library", "the page stays where the person went")
        self.assertIsNone(r["sheet"])
        self.assertTrue(r["scrim"])
        self.assertIn("every step done", r["toast"])

    def test_a_hidden_load_that_fails_comes_back_with_why(self):
        r = run(self, self.BODY + r"""
        running('j1', 30, 'downloading'); await __advance(1000);
        $('sh-cancel').click(); await __settle();
        const hidden = $('scrim').hidden;
        job(null, [{id: 'j1', action: 'switch', status: 'failed', rc: 1, elapsed: 31}]);
        await __advance(2000); await __settle();
        report({hidden, back: !$('scrim').hidden, status: txt('sh-status'), label: txt('sh-cancel'), go: txt('sh-go')});
        """)
        self.assertTrue(r["hidden"])
        self.assertTrue(r["back"], "a failure brings the window back")
        self.assertIn("Step 1 failed with exit code 1", r["status"])
        self.assertEqual((r["label"], r["go"]), ("Cancel", "Retry from here"))

    def test_between_two_steps_the_job_button_stays_and_brings_the_window_back(self):
        r = run(self, self.BODY + r"""
        running('j1', 30, 'downloading'); await __advance(1000);
        $('sh-cancel').click(); await __settle();
        job(null, [done('j1', 'switch')]);              // the next step is posted, its job not seen yet
        await __advance(600); await __settle();
        const between = {pip: !$('jobpip').hidden, what: txt('jobpip-what')};
        $('jobpip').click(); await __settle();
        report({between, back: !$('scrim').hidden, pipAfter: !$('jobpip').hidden});
        """)
        self.assertEqual(r["between"], {"pip": True, "what": "Load Qwen3.8 27B abliterated NVFP4 · edp1096: between steps"}, r)
        self.assertTrue(r["back"])
        self.assertFalse(r["pipAfter"], "the window is back and no job runs: the button has nothing left to bring back")

    def test_the_job_button_goes_once_a_hidden_load_has_ended(self):
        r = run(self, self.BODY + r"""
        running('j1', 30, 'downloading'); await __advance(1000);
        $('sh-cancel').click(); await __settle();
        job(null, [done('j1', 'switch')]); await __advance(2000); await __settle(); await __advance(1000);
        job(null, [done('j2', 'unit'), done('j1', 'switch')]); await __advance(2000); await __settle(); await __advance(2000);
        report({pip: !$('jobpip').hidden, sheet: SHEET});
        """)
        self.assertFalse(r["pip"], "nothing runs and no load waits: no job button")
        self.assertIsNone(r["sheet"])

    # The review of 2026-10-13 (681e25a), one test per finding.
    HIDE = r"""
    running('j1', 30, 'downloading'); await __advance(1000);
    $('sh-cancel').click(); await __settle();
    """

    def test_on_a_narrow_screen_the_way_back_still_shows(self):
        r = run(self, self.BODY + self.HIDE + r"""
        report({away: $('jobpip').classList.contains('away'), hidden: $('jobpip').hidden});
        """)
        self.assertEqual(r, {"away": True, "hidden": False})
        narrow = [(sel, decl) for sel, media, decl in css_rules() if any("max-width:1100px" in m.replace(" ", "") for m in media)]
        self.assertIn(".jobpip.away", [sel for sel, _ in narrow], "the head hides .jobpip under 1100 px: .away must undo it")
        self.assertIn("display:flex", dict(narrow)[".jobpip.away"].replace(" ", ""))
        phone = [(sel, decl) for sel, media, decl in css_rules() if any("max-width:720px" in m.replace(" ", "") for m in media)]
        self.assertIn("max-width:25vw", dict(phone).get(".jobpip.away", "").replace(" ", ""), "on a phone it must not widen the page")

    def test_no_generation_starts_while_a_load_runs(self):
        r = run(self, self.BODY + self.HIDE + r"""
        const buttons = {img: [$('img-run').disabled, txt('img-why')], vid: [$('vid-run').disabled, txt('vid-why')],
                         s1: [$('s1-run').disabled, txt('s1-why')]};
        const toasts = [];
        for (const f of [imgRun, imgSample, vidRun, s1Run]){ clear($('toasts')); await f(); await __settle(); toasts.push(toastText()); }
        report({buttons, toasts, posts: __fetches.map(x => x.url).filter(u => /generate|systemone/.test(u))});
        """)
        self.assertEqual(r["posts"], [], "nothing was sent to a lane")
        for name, (disabled, why) in r["buttons"].items():
            self.assertTrue(disabled, name)
            self.assertIn("A load is running (Qwen3.8 27B abliterated NVFP4 · edp1096)", why, name)
        for t in r["toasts"]:
            self.assertIn("A load is running", t)

    def test_every_control_says_it_waits_for_the_load(self):
        r = run(self, self.BODY + self.HIDE + r"""
        const b = $('oc-fitbtn');
        report({disabled: b.disabled, title: b.title});
        """)
        self.assertTrue(r["disabled"])
        self.assertIn("A load is running (Qwen3.8 27B abliterated NVFP4 · edp1096)", r["title"])

    def test_a_failure_brings_the_window_back_with_the_focus_on_its_status(self):
        r = run(self, self.BODY + self.HIDE + r"""
        job(null, [{id: 'j1', action: 'switch', status: 'failed', rc: 1, elapsed: 31}]);
        await __advance(2000); await __settle(); await __advance(10);
        report({back: !$('scrim').hidden, focus: document.activeElement && document.activeElement.id});
        """)
        self.assertTrue(r["back"])
        self.assertEqual(r["focus"], "sh-status", "on no button: a key pressed elsewhere neither runs the step again nor dismisses why")

    def test_brought_back_while_it_runs_the_focus_is_on_hide(self):
        r = run(self, self.BODY + self.HIDE + r"""
        $('jobpip').click(); await __settle(); await __advance(10);
        report({focus: document.activeElement && document.activeElement.id, label: txt('sh-cancel')});
        """)
        self.assertEqual(r, {"focus": "sh-cancel", "label": "Hide"})

    def test_a_failure_comes_back_over_the_menu_and_the_viewer(self):
        r = run(self, self.BODY + self.HIDE + r"""
        $('menu').hidden = false; $('viewer').hidden = false;
        job(null, [{id: 'j1', action: 'switch', status: 'failed', rc: 1, elapsed: 31}]);
        await __advance(2000); await __settle();
        report({menu: $('menu').hidden, viewer: $('viewer').hidden, back: !$('scrim').hidden, away: loadAway(),
                status: txt('sh-status'), go: txt('sh-go'), pip: $('jobpip').hidden});
        """)
        self.assertTrue(r["menu"] and r["viewer"], "what was open over the page is closed first")
        self.assertTrue(r["back"])
        self.assertFalse(r["away"], "a load is put away only while its steps run")
        self.assertIn("Step 1 failed with exit code 1", r["status"])
        self.assertEqual(r["go"], "Retry from here")
        self.assertTrue(r["pip"], "nothing runs and nothing waits: no job button")

    def test_a_failure_brought_back_keeps_its_first_second(self):
        r = run(self, self.BODY + self.HIDE + r"""
        $('viewer').hidden = false;
        job(null, [{id: 'j1', action: 'switch', status: 'failed', rc: 1, elapsed: 31}]);
        for (let k = 0; k < 80 && $('scrim').hidden; k++) await __advance(50);
        const back = !$('scrim').hidden;
        const esc = () => document.dispatchEvent({type: 'keydown', key: 'Escape'});
        esc(); $('scrim').click(); await __settle();
        const kept = !$('scrim').hidden && txt('sh-go') === 'Retry from here';
        await __advance(1000); esc(); await __settle();
        report({back, kept, closed: $('scrim').hidden});
        """)
        self.assertTrue(r["back"])
        self.assertTrue(r["kept"], "the Escape or the click aimed at what the window covered leaves the failure on screen")
        self.assertTrue(r["closed"], "a second later Escape closes it as any window")

    def test_another_job_on_the_button_opens_the_dock_not_the_load(self):
        r = run(self, self.BODY + self.HIDE + r"""
        job({id: 'auto1', action: 'unit', params: {verb: 'restart', unit: 'qwen38-keepalive.service'}, elapsed: 2, lines: []});
        await __advance(100);
        $('jobpip').click(); await __settle();
        report({scrim: $('scrim').hidden, dock: !$('dock').hidden});
        """)
        self.assertEqual(r, {"scrim": True, "dock": True})

    def test_leaving_the_page_asks_first_while_a_load_runs_away(self):
        r = run(self, self.BODY + r"""
        const leave = () => { const ev = {returnValue: undefined, prevented: false, preventDefault(){ this.prevented = true; }};
                              (window._listeners.beforeunload || []).forEach(f => f(ev)); return ev.prevented; };
        const shown = leave();
        """ + self.HIDE + r"""
        const away = leave();
        F.config.dry_run = true; const dry = leave(); F.config.dry_run = false;
        report({shown, away, dry});
        """)
        self.assertEqual(r, {"shown": False, "away": True, "dry": False})

    def test_a_wait_whose_check_throws_still_ends(self):
        r = run(self, r"""
        let n = 0, done = false;
        waitFor(() => { n++; if (n < 3) throw new Error('state not read yet'); return true; }, 90000).then(() => { done = true; });
        await __advance(2000); await __settle();
        report({done, n});
        """)
        self.assertTrue(r["done"], "a predicate that threw twice, then held, ended the wait")

    def test_a_refused_step_says_the_refusal_not_an_error(self):
        r = run(self, self.BODY + r"""
        // step 1 was posted by the body already: the next action, step 2, is refused by a gate
        __fetch = async url => url === '/api/csrf' ? __response(200, {token: 't'})
          : url === '/api/action' ? __response(409, {reasons: ['the gate says no']})
          : new Promise(() => {});
        """ + self.HIDE + r"""
        job(null, [done('j1', 'switch')]); await __advance(2000); await __settle(); await __advance(1000);
        report({status: txt('sh-status'), back: !$('scrim').hidden});
        """)
        self.assertEqual(r["status"], "Step 2 refused: the gate says no", r)
        self.assertTrue(r["back"])

    def test_going_to_the_login_does_not_ask(self):
        r = run(self, self.BODY + self.HIDE + r"""
        const leave = () => { const ev = {prevented: false, preventDefault(){ this.prevented = true; }};
                              (window._listeners.beforeunload || []).forEach(f => f(ev)); return ev.prevented; };
        const before = leave(); login(); const after = leave();
        report({before, after});
        """)
        self.assertEqual(r, {"before": True, "after": False}, "a session that ended cannot run the chain: no prompt on the way to the login")

    def test_the_job_button_brings_a_hidden_load_back(self):
        r = run(self, self.BODY + r"""
        running('j1', 30, 'downloading'); await __advance(1000);
        $('sh-cancel').click(); await __settle();
        const hidden = $('scrim').hidden;
        running('j1', 90, 'downloading'); await __advance(500);
        $('jobpip').click(); await __settle();
        report({hidden, back: !$('scrim').hidden, live: state().live[0]});
        """)
        self.assertTrue(r["hidden"])
        self.assertTrue(r["back"], "the job button reopens the load window")
        self.assertIn("1 min 30 so far: downloading", r["live"])


class ALoadBringsBackWhatALaneLacks(unittest.TestCase):
    """A lane that cannot start as installed (its checkpoint or a copy of its table missing) gets
    its switch, which fetches and copies them, even as the boot lane: its Load started it alone
    and it failed again on the same missing file (found in review, 2026-10-20)."""

    def steps(self, startable):
        return run(self, r"""
        const U = JSON.parse(JSON.stringify(UNITS));
        U.units['qwen38-sglang.service'] = {active: 'inactive', enabled: 'disabled'};
        U.units['qwen38-flash-local.service'] = {active: 'failed', enabled: 'enabled'};
        feed({units: U, lifecycle: life({'qwen38-flash-local.service': eng('failed', {target: 'flash-local'})})});
        feed({lanes_ready: {'flash-local': true}, lanes_startable: {'flash-local': %s}});
        report(laneJourneySteps('flash-local').map(s => s.name + (s.params.verb ? ':' + s.params.verb : '')));
        """ % json.dumps(startable))

    def test_a_lane_missing_a_file_gets_its_switch(self):
        self.assertEqual(self.steps(False), ["switch", "unit:start"])

    def test_its_step_says_it_fetches_and_copies_not_that_it_verifies(self):
        # the step read "Verifies the checkpoint ... Nothing restarts." where it may fetch 102 GB or
        # copy 32 GB (found in review, 2026-10-20)
        r = run(self, r"""
        const U = JSON.parse(JSON.stringify(UNITS));
        U.units['qwen38-sglang.service'] = {active: 'inactive', enabled: 'disabled'};
        U.units['qwen38-flash-local.service'] = {active: 'failed', enabled: 'enabled'};
        feed({units: U, lifecycle: life({'qwen38-flash-local.service': eng('failed', {target: 'flash-local'})})});
        feed({lanes_ready: {'flash-local': true}, lanes_startable: {'flash-local': false}});
        const s = laneJourneySteps('flash-local')[0];
        report({title: s.title, desc: s.desc});
        """)
        self.assertEqual(r["title"], "Bring back what Qwen3.8 Flash-Next FP4-Local lacks")
        self.assertIn("Fetches what its checkpoint lacks and copies its n-gram table again", r["desc"])
        self.assertNotIn("Verifies", r["desc"])

    def test_a_whole_boot_lane_is_only_started(self):
        self.assertEqual(self.steps(True), ["unit:start"])


class TheLastFiveMinutesSayTheEnginesCounters(unittest.TestCase):
    """The Traffic view's last 5 minutes come from the engine's own counters, which the
    cockpit reads and windows (engine_metrics.py): what each row says, and what the box says
    when there is nothing to count, a read failed, or a canary could not be taken out."""

    FULL = {"lane": "qwen38-flash.service", "read": True, "watched_s": 300.0, "requests": 4,
            "prompt_tokens": 200000, "cached_tokens": 190000, "generated_tokens": 1000,
            "reuse": 0.95, "prefill_tps": 1998.4, "queue_s": 0.42, "decode_tps": 49.83, "ttft_s": 1.0,
            "acc_len": 3.6082, "throughput_tps": 3.333, "idle_share": 0.4, "aborted": 2,
            "pool_max": 0.62, "running_max": 8, "queued_max": 3,
            "canaries_out": 1, "approximate": False}

    def box(self, metrics):
        return run(self, r"""
        feed({config: CONFIG, units: UNITS, metrics: %s});
        const dl = $('wm-facts'), facts = {};
        [...dl.children].forEach((n, k, a) => { if (n.tagName === 'DT') facts[n.textContent] = a[k + 1].textContent; });
        report({cap: txt('wm-cap'), note: txt('wm-note'), verdict: txt('wm-verdict'), facts});
        """ % json.dumps(metrics))

    def test_the_numbers_and_the_count(self):
        r = self.box(self.FULL)
        self.assertEqual(r["facts"], {"Requests finished": "4",
                                      "Prompt tokens from the cache": "95.0 % of 200,000",
                                      "Queued before starting": "0.42 s on average",
                                      "Prefill speed": "1,998 tok/s computed",
                                      "First token after": "1.00 s on average",
                                      "Decode speed": "49.8 tok/s per request",
                                      "Accepted per draft step": "3.61 tokens per verify step",
                                      "Generated, all clients": "3.3 tok/s, 1,000 tokens",
                                      "Busiest moment": "62 % of the pool, 8 requests at once, 3 waiting",
                                      "Idle in the window": "40 % of the time",
                                      "Abandons the engine accepted": "2"}, r)
        self.assertEqual((r["cap"], r["note"]), ("watching", ""), r)
        self.assertIn("each request counted once, when it finishes", r["verdict"])

    def test_one_request_at_once_is_said_in_the_singular(self):
        # the reference box's 27B, one prompt in the window (2026-10-11): "1 requests at once"
        r = self.box(dict(self.FULL, pool_max=0.09, running_max=1, queued_max=0))
        self.assertEqual(r["facts"]["Busiest moment"], "9 % of the pool, 1 request at once")

    def test_a_young_window_says_how_long_it_watched(self):
        self.assertEqual(self.box(dict(self.FULL, watched_s=70.0))["note"], "watched 70 s")

    def test_nothing_finished_is_said_not_shown_as_zero_percent(self):
        r = self.box(dict(self.FULL, requests=0, prompt_tokens=0, reuse=None, decode_tps=None,
                          ttft_s=None, throughput_tps=0.0, prefill_tps=None, queue_s=None, acc_len=None,
                          idle_share=1.0, aborted=0, pool_max=0.0, running_max=0, queued_max=0))
        self.assertEqual(r["cap"], "quiet")
        self.assertEqual(r["facts"]["Prompt tokens from the cache"], "no request finished")
        self.assertNotIn("Decode speed", r["facts"])
        self.assertNotIn("First token after", r["facts"])
        self.assertNotIn("Accepted per draft step", r["facts"])
        self.assertNotIn("Queued before starting", r["facts"])
        self.assertNotIn("Busiest moment", r["facts"], "nothing ran: no peaks to show")
        self.assertEqual(r["facts"]["Idle in the window"], "100 % of the time")
        self.assertEqual(r["facts"]["Abandons the engine accepted"], "none")

    def test_no_text_engine(self):
        r = self.box({"lane": None})
        self.assertEqual((r["cap"], r["facts"]), ("no text engine", {}), r)

    def test_a_failed_read_and_an_approximate_window_say_so(self):
        r = self.box(dict(self.FULL, read=False, approximate=True))
        self.assertEqual(r["cap"], "approximate")
        self.assertIn("The last read of the engine’s counters failed", r["verdict"])
        self.assertIn("A canary ran beside a client request", r["verdict"])

    def test_the_first_read_on_its_way(self):
        r = self.box({"lane": "qwen38-flash.service", "read": True, "watched_s": 0.0, "requests": None})
        self.assertEqual((r["cap"], r["facts"]), ("first read", {}), r)

    def test_an_engine_that_never_answered_its_counters_says_why_it_may_be(self):
        r = self.box({"lane": "qwen38-flash.service", "read": False, "watched_s": 0.0, "requests": None})
        self.assertEqual((r["cap"], r["facts"]), ("no counters", {}), r)
        self.assertIn("--enable-metrics", r["verdict"])
        self.assertIn("a busy one answers a later read", r["verdict"])

    def test_a_vllm_lanes_counters_say_no_canary_ran(self):
        # the collector reads the vLLM lanes too, where no canary runs: "the cockpit's canary
        # taken out" would say of them a thing that never happened (found in review, 2026-10-20)
        for unit in ("qwen38-flash-local.service", "qwen38-kolibri.service", "qwen38-nemotron.service",
                     "qwen38-nemotron-super.service"):
            with self.subTest(unit=unit):
                r = self.box(dict(self.FULL, lane=unit, canaries_out=0, aborted=None))
                # vLLM counts its tokens and latencies at every step, not once a request ends: a turn
                # still decoding at the window's end is in the tokens and not in the requests
                # (vllm/v1/metrics/stats.py, found in review, 2026-10-20)
                self.assertIn("From vLLM’s own counters: its tokens and latencies as it makes them, a request, "
                              "its wait and its prefill once it finishes", r["verdict"])
                self.assertNotIn("each request counted once", r["verdict"])
                self.assertNotIn("canary", r["verdict"])
                self.assertEqual(r["facts"]["Decode speed"], "49.8 tok/s per request")
                # vLLM counts no client that left: "none" would say none left
                self.assertEqual(r["facts"]["Abandons the engine accepted"], "not counted by vLLM")
                # no prompt read in the window says so: a request may well have finished in it
                r = self.box(dict(self.FULL, lane=unit, canaries_out=0, aborted=None, prompt_tokens=0, reuse=None))
                self.assertEqual(r["facts"]["Prompt tokens from the cache"], "no prompt read")



class EveryVersionOfALaneIsInSight(unittest.TestCase):
    """A lane's versions sat behind a select that read as one line, and nobody could tell
    there was a choice (2026-10-11). They are one button each now, a radio group, and the
    choice is the card's own value: no select stays behind them (v1.27.1)."""

    SERVE = TheCheckpointSelectorFollowsTheLaneAgain.SERVE

    def test_a_click_chooses_and_the_card_says_what(self):
        r = run(self, self.SERVE + r"""
        serve('qwen38-sglang.service', 'stock');
        const c = LCARDS.get('qwen38-sglang.service'), opt = t => c.opts.find(b => b.dataset.target === t);
        const seen = () => ({checked: c.opts.filter(b => b.getAttribute('aria-checked') === 'true').map(b => b.dataset.target),
                             tabbable: c.opts.filter(b => b.tabIndex === 0).map(b => b.dataset.target),
                             value: c.target, line: c.pickLine.textContent, load: c.load.hidden ? null : c.load.textContent});
        const before = seen(); opt('fp8').click(); const after = seen();
        // a lane that does not serve says which version its Load loads, when it has more than one
        const loads = ['qwen38-flash.service', 'qwen38-video.service'].map(u => LCARDS.get(u).load.textContent);
        serve('qwen38-sglang.service', 'stock');          // the lane still serves stock: the choice holds
        const held = seen();
        report({before, after, loads, held, names: c.opts.map(b => b.textContent), group: c.pickBox.querySelector('.pick').getAttribute('role'),
                roles: c.opts.map(b => b.getAttribute('role')), label: c.pickBox.querySelector('.pick-label').textContent,
                selects: c.root.querySelectorAll('select').length});
        """)
        self.assertEqual(r["names"], ["NVFP4 · RadixArk", "Abliterated NVFP4 · edp1096", "FP8 · Qwen", "Abliterated FP8 · edp1096"])
        self.assertEqual(r["label"], "Version (4)")
        self.assertEqual((r["group"], set(r["roles"])), ("radiogroup", {"radio"}))
        self.assertEqual(r["selects"], 0, "the buttons are the control: no select behind them")
        self.assertEqual((r["before"]["checked"], r["before"]["tabbable"], r["before"]["value"]), (["stock"], ["stock"], "stock"))
        self.assertTrue(r["before"]["line"].endswith("Serving now."), r["before"]["line"])
        self.assertIsNone(r["before"]["load"], "the version that serves is not offered again")
        self.assertEqual((r["after"]["checked"], r["after"]["tabbable"], r["after"]["value"]), (["fp8"], ["fp8"], "fp8"))
        self.assertEqual(r["after"]["line"], "Qwen3.8 27B FP8 · Qwen. Qwen's own weights: the most faithful, and heavier.")
        self.assertEqual(r["after"]["load"], "Load Qwen3.8 27B FP8 · Qwen")
        self.assertEqual(r["loads"], ["Load Qwen3.8 Flash-Next NVFP4 · RadixArk", "Load MiniMax-H3"])
        self.assertEqual((r["held"]["checked"], r["held"]["value"]), (["fp8"], "fp8"), "a click holds its choice")

    def test_the_arrow_keys_move_the_choice_and_the_focus(self):
        r = run(self, self.SERVE + r"""
        serve('qwen38-sglang.service', 'stock');
        const c = LCARDS.get('qwen38-sglang.service'), pick = c.pickBox.querySelector('.pick');
        const key = k => { let held = false; c.opts.find(b => b === document.activeElement).dispatchEvent(
                             {type: 'keydown', key: k, bubbles: true, preventDefault(){ held = true; }}); return held; };
        c.opts[0].focus();
        const steps = ['ArrowRight', 'ArrowRight', 'ArrowLeft', 'ArrowLeft', 'ArrowUp', 'ArrowDown'].map(k => {
          const held = key(k); return [c.target, document.activeElement.dataset.target, held]; });
        const modified = ['altKey', 'ctrlKey', 'metaKey'].map(m => { let held = false;
          document.activeElement.dispatchEvent({type: 'keydown', key: 'ArrowLeft', [m]: true, bubbles: true, preventDefault(){ held = true; }});
          return [held, c.target]; });
        report({steps, other: key('a'), after: c.target, modified});
        """)
        self.assertEqual([s[:2] for s in r["steps"]],
                         [["uncensored", "uncensored"], ["fp8", "fp8"], ["uncensored", "uncensored"],
                          ["stock", "stock"], ["uncensored-fp8", "uncensored-fp8"], ["stock", "stock"]])
        self.assertTrue(all(s[2] for s in r["steps"]), "an arrow moves the choice, not the page")
        self.assertFalse(r["other"], "any other key is left to the page")
        self.assertEqual(r["after"], "stock")
        # Alt+Left is Back in a browser: with a modifier the arrows are the browser's
        self.assertEqual(r["modified"], [[False, "stock"]] * 3)

    def test_the_served_version_is_marked_on_the_lane_that_serves_only(self):
        r = run(self, self.SERVE + r"""
        serve('qwen38-sglang.service', 'stock');
        const marked = unit => LCARDS.get(unit).opts.filter(b => b.classList.contains('served')).map(b => b.dataset.target);
        // the flash lane, stopped, still has a target of its own: it is not served
        feed({lifecycle: life({'qwen38-sglang.service': eng('ready', {target: 'stock'}), 'qwen38-flash.service': eng('stopped', {target: 'flash'})})});
        const ready = {c27: marked('qwen38-sglang.service'), line: LCARDS.get('qwen38-sglang.service').pickLine.textContent};
        // while the lane boots again (and the same for a stop or a wedge) it is not serving yet
        feed({lifecycle: life({'qwen38-sglang.service': eng('capturing-graphs', {target: 'stock'})})});
        const booting = {c27: marked('qwen38-sglang.service'), line: LCARDS.get('qwen38-sglang.service').pickLine.textContent};
        report({c27: ready.c27, line: ready.line, booting, flash: marked('qwen38-flash.service'),
                capsules: [...document.querySelectorAll('.serving')].map(e => e.id)});
        """)
        self.assertEqual(r["c27"], ["stock"])
        self.assertTrue(r["line"].endswith("Serving now."), r["line"])
        self.assertEqual(r["booting"]["c27"], [], "a boot is not serving: no green dot")
        self.assertNotIn("Serving now.", r["booting"]["line"])
        self.assertEqual(r["flash"], [], "a lane that does not serve marks no version")
        # .serving is the head's capsule: a version that wore it took the capsule's rules too,
        # and on a phone the served one stretched across its row (2026-10-11)
        self.assertEqual(r["capsules"], ["serving"])


    def test_every_version_has_its_name_and_its_line(self):
        missing = run(self, r"""
        report(LANE_UNITS.flatMap(u => LANE_TARGETS[u]).filter(t => !TARGET_SHORT_NAME[t] || !TARGET_LINE[t] || !TARGET_NAME[t]));
        """)
        self.assertEqual(missing, [], "a version declared halfway reads its long name on its segment and '. .' under it")


class ALaneToComeOpensTheRack(unittest.TestCase):
    """Hasan, 2026-10-25: a card says Qwen4 27B is coming. Since 2026-11-06 (Hasan: "en tout premier,
    avant qwen3.8 27b") it opens the rack, once, before the 27B it will follow however often the rack
    renders, says no date and no architecture (Qwen published neither), and loads nothing: a click only
    says it is not out."""

    def test_the_card_opens_the_rack_and_loads_nothing(self):
        r = run(self, r"""
        const mem = {MemTotal: 121.6 * GIB, MemAvailable: 16 * GIB, Cached: 12 * GIB, SwapTotal: 0, SwapFree: 0};
        feed({config: CONFIG, units: UNITS, machine: {mem, cpu_pct: {}, disks: {}}, lifecycle: life({})});
        feed({lifecycle: life({'qwen38-flash.service': eng('ready', {target: 'flash'})})});
        feed({lifecycle: life({})});
        const rack = [...$('rack').children], soon = document.querySelectorAll('#rack .bay.soon'), card = soon[0];
        const before = __fetches.length;
        clear($('toasts')); card.click(); await __settle();
        report({n: soon.length, at: rack.indexOf(card), next: rack[rack.indexOf(card) + 1].querySelector('h3').textContent,
                bays: rack.length, title: card.querySelector('h3').textContent, cap: card.querySelector('.cap').textContent,
                lamp: card.querySelectorAll('.cap .lamp').length, text: card.textContent, toast: toastText(),
                fetched: __fetches.slice(before).map(x => x.url), aria: card.getAttribute('aria-label'),
                room: [card.querySelector('.foot .act').style.visibility, card.querySelector('.foot .act').getAttribute('aria-hidden')]});
        """)
        self.assertEqual(r["n"], 1)
        self.assertEqual(r["at"], 0)
        self.assertTrue(r["next"].startswith("Qwen3.8 27B"), r["next"])
        self.assertEqual(r["bays"], 10)   # the nine lanes and this card
        self.assertEqual((r["title"], r["cap"], r["lamp"]), ("Qwen4 27B", "coming soon", 0))
        self.assertIn("not released yet", r["text"])
        self.assertIn("no date yet", r["text"])
        self.assertIsNone(re.search(r"\b(dense|MoE|experts|October|November|20\d\d)\b", r["text"]), r["text"])
        self.assertEqual(r["toast"], "Qwen4 27B is not released yet. It comes in an update.")
        self.assertEqual(r["fetched"], [])
        self.assertEqual(r["aria"], "Qwen4 27B: coming soon, not released yet.")
        # the foot keeps its neighbours' height with an action nobody sees or hears
        self.assertEqual(r["room"], ["hidden", "true"])


class EachLaneWearsItsMakersMark(unittest.TestCase):
    """Four bays that looked alike (2026-10-11): each lane carries its maker's mark on its bay
    and its card in Lanes, the head's capsule shows the mark of what serves, and the lane's
    own hue colours its share of the pool and the head's gauge."""

    def test_marks_and_hues_follow_the_lane_that_serves(self):
        r = run(self, r"""
        const mem = {MemTotal: 121.6 * GIB, MemAvailable: 16 * GIB, Cached: 12 * GIB, SwapTotal: 0, SwapFree: 0};
        feed({config: CONFIG, units: UNITS, machine: {mem, cpu_pct: {}, disks: {}}, lifecycle: life({})});
        const logo = img => img.getAttribute('src').replace('/static/img/logos/', '');
        const now = () => ({mark: $('serving-mark').hidden ? null : logo($('serving-mark').firstChild),
                            hue: $('seg-lane').style.getPropertyValue('--hue'), gauge: $('mp-used').style.background});
        const none = now();
        feed({lifecycle: life({'qwen38-flash.service': eng('ready', {target: 'flash'})})});
        const flash = now();
        feed({lifecycle: life({'qwen38-video.service': eng('ready', {target: 'video'})})});
        const video = now();
        report({bays: [...document.querySelectorAll('#rack .bay')].filter(b => !b.classList.contains('soon')).map(b => logo(b.querySelector('.mark img'))),
                cards: [...document.querySelectorAll('#lanes-list .lane-panel')].map(c => logo(c.querySelector('.mark img'))),
                alts: [...document.querySelectorAll('.mark img')].map(i => i.getAttribute('alt')),
                hidden: [...document.querySelectorAll('#rack .mark, #lanes-list .mark')].map(m => m.getAttribute('aria-hidden')),
                none, flash, video});
        """)
        lanes = ["qwen.svg", "qwen.svg", "nvidia.svg", "ornith.svg", "alephalpha.svg", "nvidia.svg", "nvidia.svg", "qwen.svg",
                 "minimax.svg"]   # 27B, flash, Flash FP4-Local, Ornith, Kolibri, the Nemotron lanes, image, video
        self.assertEqual(r["bays"], lanes)
        self.assertEqual(r["cards"], lanes)
        # the mark is decoration beside the lane's name: no alt text to read twice
        self.assertEqual(set(r["alts"]), {""})
        self.assertEqual(set(r["hidden"]), {"true"})
        self.assertEqual(r["none"], {"mark": None, "hue": "var(--brand)", "gauge": "var(--brand)"})
        self.assertEqual(r["flash"], {"mark": "qwen.svg", "hue": "var(--lane-flash)", "gauge": "var(--lane-flash)"})
        self.assertEqual(r["video"], {"mark": "minimax.svg", "hue": "var(--lane-video)", "gauge": "var(--lane-video)"})

    def test_every_mark_and_hue_the_page_names_exists(self):
        names = set()
        for f in (pagejs.STATIC / "js").glob("*.js"):
            names |= set(re.findall(r"maker: '([a-z]+)'", f.read_text()))
        self.assertEqual(names, {"qwen", "minimax", "alephalpha", "nvidia", "ornith"})
        for n in names:
            self.assertTrue((pagejs.STATIC / "img" / "logos" / f"{n}.svg").is_file(), n)
        css = (pagejs.STATIC / "css" / "cockpit.css").read_text()
        # the lanes whose name holds a dash too: read without it, Super VL's and FP4-Local's hues went
        # unchecked (found when the Ornith lane came, 2026-10-21)
        lanes = set(re.findall(r"lane: '([a-z0-9-]+)'", (pagejs.STATIC / "js" / "base.js").read_text()))
        self.assertEqual(lanes, {"27b", "flash", "kolibri", "nemotron", "nemotron-super", "flash-local", "ornith-122b",
                                 "image", "video"})
        for lane in lanes:
            self.assertRegex(css, r"--lane-%s:#[0-9a-f]{6};" % lane)


class TheBackdropStepsAsideWhereItCannotDraw(unittest.TestCase):
    """The animated ground is decoration: where its shader does not compile (here, a context
    that draws nothing) the canvas goes and the CSS light under it stays, with no error."""

    COUNT = r"""
    window.__contexts = 0;
    const cv = document.getElementById('backdrop'), getContext = cv.getContext.bind(cv);
    cv.getContext = (...a) => { window.__contexts++; return getContext(...a); };
    """

    def test_no_shader_no_canvas(self):
        r = run(self, r"""
        report({canvas: !!document.getElementById('backdrop'), lit: document.documentElement.classList.contains('has-backdrop'),
                contexts: window.__contexts});
        """, setup=self.COUNT)
        self.assertEqual(r, {"canvas": False, "lit": False, "contexts": 1})

    def test_forced_colours_never_paint_it(self):
        """Windows' high contrast forces the page's colours, never a canvas's pixels: the
        indigo stayed under panels gone transparent, their forced edges at 1.9:1."""
        r = run(self, r"""
        report({canvas: !!document.getElementById('backdrop'), contexts: window.__contexts});
        """, setup=self.COUNT, opts={"media": {"(forced-colors: active)": True}})
        self.assertEqual(r, {"canvas": False, "contexts": 0})
        hidden = {s.strip() for s, media, d in css_rules() if any("forced-colors" in m for m in media) and "display:none" in d.replace(" ", "")}
        self.assertTrue({"#backdrop", "body::before"} <= {x.strip() for s in hidden for x in s.split(",")}, hidden)



class DurationsRoundTheirMinutesFirst(unittest.TestCase):
    """fmtMin, the duration of a plan (a video's estimate, a boot, the time an install has run),
    rounded the minutes after it split the hours off: 7,170 s read "1 h 60", for 30 s of every
    hour, and 3,599 s "60 min" (found in review, 2026-10-21)."""

    def test_an_hour_is_never_sixty_minutes(self):
        r = run(self, "report({out: [null, 45, 89, 90, 3569, 3599, 3600, 5400, 7170, 7199, 7230].map(fmtMin)});")
        self.assertEqual(r["out"], ["?", "45 s", "89 s", "2 min", "59 min", "1 h 00", "1 h 00", "1 h 30", "2 h 00",
                                    "2 h 00", "2 h 01"])

class TheStillBackdropIsAlwaysDrawn(unittest.TestCase):
    """Under reduced motion the backdrop draws one frame. That frame went through the 8 fps
    gate, so a first frame earlier than 125 ms after the page started was skipped and never
    asked for again: the ground stayed black (3 loads in 4 in a browser, v1.27.0)."""

    GL = r"""
    window.__draws = 0;
    window.performance = window.performance || {now: () => 0};
    const cv = document.getElementById('backdrop');
    cv.getContext = () => new Proxy({}, {get: (t, k) => k === 'drawArrays' ? () => { window.__draws++; }
      : (k === 'getShaderParameter' || k === 'getProgramParameter') ? () => true : () => ({})});
    // a first frame 16 ms into the page, as a browser gives it
    window.requestAnimationFrame = cb => setTimeout(() => cb(16), 16);
    window.cancelAnimationFrame = id => clearTimeout(id);
    """

    def test_reduced_motion_draws_its_one_frame(self):
        r = run(self, r"""
        await __advance(1000);
        report({draws: window.__draws, lit: document.documentElement.classList.contains('has-backdrop')});
        """, setup=self.GL, opts={"media": {"(prefers-reduced-motion: reduce)": True}})
        self.assertEqual(r, {"draws": 1, "lit": True})

    def test_reduced_motion_draws_it_under_the_agents_fullscreen_too(self):
        # the Agent opens fullscreen on a phone: covered or not, the one frame is drawn, or the
        # ground is black when the fullscreen is left
        r = run(self, r"""
        document.body.classList.add('agentmax');
        await __advance(1000);
        report(window.__draws);
        """, setup=self.GL, opts={"media": {"(prefers-reduced-motion: reduce)": True}})
        self.assertEqual(r, 1)


class TheStylesheetKeepsWordsAndContrast(unittest.TestCase):
    """Two defects of the redesign found in review (2026-10-11), held by the stylesheet."""

    def rule(self, selector):
        return [d for s, media, d in css_rules() if s.strip() == selector and not media]

    def test_a_booting_lanes_share_ends_on_its_hue(self):
        # the label sits at the foot of the share: a gradient that darkened the hue there put
        # "27B 40 GiB" at 3.80:1 while the lane booted
        (d,) = self.rule(".seg-m.lane.filling")
        self.assertRegex(d.replace(" ", ""), r"var\(--hue\)\)$")

    def test_capsules_keep_the_words_the_page_writes(self):
        # upper case read "16.1 GIB AVAILABLE" and "SERVED BY QWEN3.8-27B", an identifier
        (d,) = self.rule(".cap")
        self.assertNotIn("uppercase", d)


class AReadyLaneIsTheTimelinesGreen(unittest.TestCase):
    """Green is ready and serving: a lane that came up is the green dot of the timeline, the
    other steps of a boot are plain news in the brand's colour (they all read green, 2026-10-11)."""

    def test_ready_is_green_and_a_step_is_not(self):
        r = run(self, r"""
        report([['state', 'sglang: warming-up to ready'], ['state', 'sglang: loading-weights to loading-draft'],
                ['state', 'sglang: ready to failed'], ['kernel', 'GPU driver refused 1 allocation at the edge of memory'],
                ['job', 'fit the opencode limits to this engine: done']].map(([kind, msg]) => evtDot({kind, msg})));
        """)
        self.assertEqual(r, ["ok", "gold", "clay", "ember", "cool"])



class TheBoxItIs(unittest.TestCase):
    """A 64 GB DGX Spark (2026-10-23) has the lanes that fit it and no other: where a lane left out
    named the command that installs it, one it cannot hold says it needs a 128 GB box, and the page
    says which box it is (boxmem.py, through the machine panel)."""
    BODY = r"""
    const units = JSON.parse(JSON.stringify(UNITS));
    const BIG = ['qwen38-flash.service', 'qwen38-image.service', 'qwen38-video.service', 'qwen38-kolibri.service',
                 'qwen38-nemotron-super.service', 'qwen38-flash-local.service', 'qwen38-ornith-122b.service'];
    for (const u of BIG) units.units[u].enabled = '';
    const fits = %s === '64' ? ['qwen38-nemotron.service', 'qwen38-sglang.service'] : Object.keys(units.units);
    const targets = fits.length === 2 ? ['nemotron', 'stock', 'uncensored'] : Object.keys(TARGET_NAME);
    feed({config: CONFIG, units, lifecycle: life({'qwen38-sglang.service': eng('ready')}),
          machine: {mem: {MemTotal: %s * GIB, MemAvailable: 20 * GIB, Cached: 2 * GIB, SwapTotal: 0, SwapFree: 0},
                    box: {cls: %s, name: 'ASUS Ascent GX10', fits, targets}, cpu_pct: {cpu: 1}, load: [0, 0, 0], disks: {}}});
    const notes = BIG.map(u => BAYS.get(u).note.textContent);
    showView('lanes');
    const install = [...document.querySelectorAll('#view-lanes dt')].filter(d => d.textContent === 'Install').map(d => d.nextSibling.textContent);
    BAYS.get('qwen38-flash.service').root.click();
    const toast = toastText();
    // the 27B card's versions, and the Image and Video views of a box without those lanes
    const versions = [...document.querySelectorAll('#view-lanes .opt')].filter(b => ['fp8', 'uncensored-fp8', 'stock'].includes(b.dataset.target))
      .map(b => `${b.dataset.target}:${b.disabled}`).filter((v, i, a) => a.indexOf(v) === i);
    showView('image'); imgRenderLane(); const image = txt('img-lane');
    showView('video'); vidRenderLane(); const video = txt('vid-lane');
    // Library's targets: a checkpoint not on the disk yet, its lane installed
    __fetch = async url => url.startsWith('/api/registry') ? __response(200, {models: [], targets: [
      {target: 'fp8', repo: 'Qwen/Qwen3.8-27B-FP8', state: 'missing', installed: true},
      {target: 'stock', repo: 'RadixArk/Qwen3.8-27B-NVFP4', state: 'missing', installed: true}]}) : new Promise(() => {});
    showView('library'); await scanRegistry(false); await __settle();
    const library = [...$('reg-targets').tBodies[0].rows].map(r => r.textContent);
    report({notes, install, pool: txt('h-pool'), box: txt('mm-box'), toast, versions, image, video, library});
    """

    def page(self, cls, gib):
        return run(self, self.BODY % (repr(cls), gib, repr(cls)))

    def test_a_64_gb_box_says_what_it_cannot_hold_and_which_box_it_is(self):
        r = self.page("64", 57.6)
        self.assertEqual(r["notes"], ["needs a 128 GB DGX Spark"] * 7, r)
        self.assertEqual(r["install"], ["needs a 128 GB DGX Spark"] * 7, r)
        self.assertEqual(r["pool"], "Unified memory of this ASUS Ascent GX10, 64 GB, shared by CPU and GPU")
        self.assertEqual(r["box"], "ASUS Ascent GX10, 64 GB: the lanes that fit it, the others say they need 128 GB")
        self.assertIn("is not installed on this box. It needs a 128 GB DGX Spark, and this box is a 64 GB one.", r["toast"])
        # the 27B's FP8 versions are shown and not to be picked, Image and Video say the box, and
        # Library downloads no FP8 checkpoint (found in review, 2026-10-23)
        self.assertEqual(sorted(r["versions"]), ["fp8:true", "stock:false", "uncensored-fp8:true"], r)
        self.assertIn("It needs a 128 GB DGX Spark, and this box is a 64 GB one.", r["image"])
        self.assertNotIn("--with-image", r["image"])
        self.assertIn("It needs a 128 GB DGX Spark, and this box is a 64 GB one.", r["video"])
        self.assertNotIn("--with-video", r["video"])
        fp8, stock = r["library"]
        self.assertIn("it needs a 128 GB DGX Spark", fp8)
        self.assertNotIn("Download", fp8)
        self.assertIn("Download", stock)

    MACHINE_64 = r"""{mem: {MemTotal: 57.6 * GIB, MemAvailable: 20 * GIB, Cached: 2 * GIB, SwapTotal: 0, SwapFree: 0},
        box: {cls: '64', name: 'ASUS Ascent GX10', fits: ['qwen38-nemotron.service', 'qwen38-sglang.service'],
              targets: ['nemotron', 'stock', 'uncensored']}, cpu_pct: {cpu: 1}, load: [0, 0, 0], disks: {}}"""

    def test_decide_offers_the_27b_on_a_64_gb_box(self):
        # it offered Load flash 176B whatever the box, and that ended on the toast that the flash lane
        # needs a 128 GB DGX Spark (found in review, 2026-10-23)
        r = run(self, r"""
        __fetch = async url => url === '/api/systemone' ? __response(200, {available: false, lane: '', reason: 'no text lane is serving'}) : new Promise(() => {});
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-nemotron.service': eng('ready')}), machine: %s});
        showView('decide'); await __settle();
        const btn = $('s1-note').querySelector('button');
        report({label: btn && btn.textContent, why: txt('s1-why')});
        btn.click(); await __settle();
        report({label: btn.textContent, why: txt('s1-why'), title: txt('sh-title'), toast: toastText()});
        """ % self.MACHINE_64)
        self.assertEqual(r["label"], "Load Qwen3.8 27B NVFP4 · RadixArk")
        self.assertIn("the 27B answers it", r["why"])
        self.assertNotIn("flash", r["why"])
        self.assertEqual(r["title"], "Load Qwen3.8 27B NVFP4 · RadixArk")
        self.assertNotIn("128 GB", r["toast"])

    def test_the_versions_follow_a_machine_panel_that_comes_after_the_card(self):
        # the card was built before the box's class was known, and its FP8 versions stayed to be
        # picked until a reload; the arrow keys stopped on a version that could not take the focus
        # (found in review, 2026-10-23)
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
        showView('lanes');
        const opt = t => [...document.querySelectorAll('#view-lanes .opt')].find(b => b.dataset.target === t);
        const before = opt('fp8').disabled;
        feed({machine: %s});
        const after = [opt('fp8').disabled, opt('uncensored-fp8').disabled, opt('stock').disabled];
        const unc = opt('uncensored'); unc.focus();
        unc.parentNode.dispatchEvent({type: 'keydown', key: 'ArrowRight', preventDefault(){}});
        report({before, after, focus: document.activeElement && document.activeElement.dataset.target,
                title: opt('fp8').title});
        """ % self.MACHINE_64)
        self.assertFalse(r["before"], "no class known is no refusal")
        self.assertEqual(r["after"], [True, True, False], r)
        self.assertEqual(r["focus"], "stock", "the arrow passes over the versions this box cannot hold")
        self.assertIn("needs a 128 GB DGX Spark", r["title"])

    def test_a_version_picked_before_the_machine_panel_gives_way_to_one_the_box_holds(self):
        # picked while the box's class was unknown, FP8 stayed the card's choice once disabled: its
        # Load still named it, and ended on the switch's refusal (found in review, 2026-10-24)
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-nemotron.service': eng('ready')})});
        showView('lanes');
        const c = laneCard('qwen38-sglang.service');
        const checked = () => c.opts.filter(b => b.getAttribute('aria-checked') === 'true').map(b => b.dataset.target);
        c.opts.find(b => b.dataset.target === 'fp8').click();
        const before = [checked(), c.load.textContent];
        feed({machine: %s});
        const after = [checked(), c.load.textContent, c.pickLine.textContent];
        c.load.click(); await __settle();
        report({before, after, title: txt('sh-title'), toast: toastText()});
        """ % self.MACHINE_64)
        self.assertEqual(r["before"][0], ["fp8"], r)
        self.assertIn("FP8", r["before"][1], r)
        self.assertEqual(r["after"][0], ["stock"], r)
        self.assertNotIn("FP8", r["after"][1] + r["after"][2], r)
        self.assertEqual(r["title"], "Load Qwen3.8 27B NVFP4 · RadixArk", r)
        self.assertNotIn("128 GB", r["toast"], r)

    def test_decide_follows_a_machine_panel_that_comes_after_its_probe(self):
        # the note's Load was chosen at the probe: one made before the box's class was known offered
        # the flash lane on a 64 GB box until the view was opened again (found in review, 2026-10-24)
        r = run(self, r"""
        __fetch = async url => url === '/api/systemone' ? __response(200, {available: false, lane: '', reason: 'no text lane is serving'}) : new Promise(() => {});
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-nemotron.service': eng('ready')})});
        showView('decide'); await __settle();
        const label = () => { const b = $('s1-note').querySelector('button'); return b && b.textContent; };
        const before = [label(), txt('s1-why')];
        feed({machine: %s}); await __settle();
        report({before, after: [label(), txt('s1-why')]});
        """ % self.MACHINE_64)
        self.assertEqual(r["before"][0], "Load Qwen3.8 Flash-Next NVFP4 · RadixArk", r)
        self.assertEqual(r["after"][0], "Load Qwen3.8 27B NVFP4 · RadixArk", r)
        self.assertIn("the 27B answers it", r["after"][1])
        self.assertNotIn("flash", r["after"][1])

    def test_a_128_gb_box_still_names_the_command_that_installs_a_lane_left_out(self):
        r = self.page("128", 121.6)
        self.assertIn("./install.sh --with-flash", r["notes"][0])
        self.assertTrue(all(i.startswith("./install.sh --with-") for i in r["install"]), r["install"])
        self.assertEqual(r["pool"], "Unified memory of this ASUS Ascent GX10, 128 GB, shared by CPU and GPU")
        self.assertEqual(r["box"], "ASUS Ascent GX10, 128 GB")
        self.assertIn("is not installed on this box. Install it on the box: ./install.sh --with-flash", r["toast"])
        self.assertEqual(sorted(r["versions"]), ["fp8:false", "stock:false", "uncensored-fp8:false"], r)
        self.assertIn("./install.sh --with-image", r["image"])
        self.assertIn("./install.sh --with-video", r["video"])
        self.assertTrue(all("Download" in row for row in r["library"]), r["library"])


class TheDeployedProxyIsSaidNotTheRepoCopyEvenWithoutItsVersion(unittest.TestCase):
    """The copy check compares the files themselves, and needs no version: the warning hid behind a version the cockpit
    could not read, from v1.40.7 to v1.42.0 (review, 2026-11-03)."""

    def test_the_warning_shows_with_or_without_a_version(self):
        for proxy, lanes, settings in (
                ({"version": None, "same_as_repo": False}, "version unknown, not the repo’s copy",
                 "version unknown, differs from the repo copy"),
                ({"version": "v6.51", "same_as_repo": False}, "v6.51, not the repo’s copy", "v6.51, differs from the repo copy"),
                ({"version": "v6.51", "same_as_repo": True}, "v6.51, the repo’s copy", "v6.51, the repo copy"),
                ({"version": None, "same_as_repo": None}, "version unknown", "version unknown")):
            with self.subTest(proxy=proxy):
                r = run(self, r"""
                feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')}),
                      repo: {head: 'abc', dirty: false, untracked: 0, proxy: %s}});
                report({lanes: txt('px-ver'), settings: txt('rp-proxy')});
                """ % json.dumps(proxy))
                self.assertEqual((r["lanes"], r["settings"]), (lanes, settings), r)


class AnEmptyTimelineReadsAsOneLine(unittest.TestCase):
    """The Timeline card of the Now view before any event: its one line of text was the only cell of an event's
    three-column grid, so it filled the time's 62 px column and read one word a line in a wide card (capture
    timeline-main.png, 2026-11-03)."""

    def test_the_line_is_not_laid_in_the_time_column(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
        const li = document.querySelector('#now-timeline li');
        report({cls: li ? li.className : null, text: li ? li.textContent : null,
                children: li ? li.children.length : null});
        """)
        self.assertEqual(r["text"], "No event yet in this cockpit session.", r)
        self.assertEqual(r["children"], 0, "its text is the grid's only cell")
        cls = r["cls"].split()
        grid = [d for sel, media, d in css_rules() if not media and ".timeline li" in [x.strip() for x in sel.split(",")]]
        self.assertTrue(any(re.search(r"display:\s*grid", d) for d in grid), grid)
        mine = [d for sel, media, d in css_rules() if not media and any(
            x.strip() in [".timeline li." + c for c in cls] for x in sel.split(","))]
        self.assertTrue(any(re.search(r"display:\s*block", d) for d in mine),
                        f"no rule takes the empty line out of the event grid: {mine}")


class TheOtherBoxOfAPairIsShownInNow(unittest.TestCase):
    """The tile of the other box of a pair (docs/pair.md, decision 975), as the captures of 2026-11-03 read
    it: hidden on a box alone, the other box's figures as this box says its own, its words as text only."""

    BODY = r"""
    feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')}), pair: %s});
    report({hidden: $('now-pair').hidden, title: txt('pair-title'),
            rows: [...document.querySelectorAll('#pair-facts dt')].map(dt => dt.textContent + ': ' + dt.nextElementSibling.textContent),
            elements: document.querySelectorAll('#pair-facts *').length});
    """
    GIB = 2 ** 30

    def test_a_box_alone_shows_no_tile(self):
        r = run(self, self.BODY % json.dumps({"node_id": "local", "boxes": {}}))
        self.assertTrue(r["hidden"], r)

    def test_the_other_box_is_said_with_the_figures_this_box_gives_its_own(self):
        state = {"name": "spark2", "version": "1.42.0", "models": ["qwen3.8-27b"], "mem_total": 121.6 * self.GIB,
                 "mem_available": 18.2 * self.GIB, "gpu_temp_c": 61, "gpu_power_w": 42,
                 "job": {"action": "switch to qwen3.8-flash-next-fp4", "status": "running", "elapsed": 95}}
        r = run(self, self.BODY % json.dumps({"node_id": "local", "boxes": {"spark2": {"state": state}}}))
        self.assertFalse(r["hidden"], r)
        self.assertEqual(r["title"], "The other box: spark2")
        self.assertEqual(r["rows"][:3], ["spark2: serves qwen3.8-27b", "memory: 103.4 GiB of 121.6 GiB in use",
                                         "GPU: 61 °C, 42 W"], r)
        self.assertIn("Alzeo: v1.42.0", r["rows"])
        self.assertTrue(any(x.startswith("job: switch to qwen3.8-flash-next-fp4, running, ") for x in r["rows"]), r)

    def test_a_box_that_does_not_answer_is_said_once(self):
        # the row read "does not answer: box spark9 does not answer: ..." (captures, 2026-11-03)
        why = "nothing listens on its pair port: its proxy is stopped, or older than this pair"
        r = run(self, self.BODY % json.dumps({"node_id": "local", "boxes": {
            "spark9": {"error": f"box spark9 does not answer: {why}"}}}))
        self.assertEqual(r["rows"], [f"spark9: does not answer: {why}"], r)
        r = run(self, self.BODY % json.dumps({"node_id": "local", "boxes": {"spark9": {"error": "HTTP 401"}}}))
        self.assertEqual(r["rows"], ["spark9: does not answer: HTTP 401"], r)

    def test_a_box_whose_cockpit_does_not_answer_still_says_what_it_serves(self):
        r = run(self, self.BODY % json.dumps({"node_id": "local", "boxes": {"spark2": {"state": {
            "models": ["qwen3.8-27b"], "cockpit": "Connection refused", "job": None}}}}))
        self.assertEqual(r["rows"], ["spark2: serves qwen3.8-27b", "cockpit: does not answer: Connection refused"], r)

    def test_what_this_box_sent_the_other_of_a_model_both_serve_is_said(self):
        state = {"models": ["qwen3.8-27b"], "sent": {"affinity": 9, "balance": 3, "failover": 1, "rescued": 2}}
        r = run(self, self.BODY % json.dumps({"node_id": "local", "boxes": {"spark2": {"state": state}}}))
        self.assertIn("shared: 12 sent there (9 to stay with their conversation, 3 to even the load), 1 served here "
                      "when it did not answer, 2 sent there when this box could not serve it", r["rows"], r)
        # none sent yet, or counts that are no counts: no row
        for sent in ({"affinity": 0, "balance": 0, "failover": 0}, {"affinity": "9", "balance": None}, "lots", None):
            r = run(self, self.BODY % json.dumps({"node_id": "local", "boxes": {"spark2": {"state": {
                "models": ["qwen3.8-27b"], "sent": sent}}}}))
            self.assertEqual(r["rows"], ["spark2: serves qwen3.8-27b"], (sent, r))

    def test_what_was_shared_is_said_while_the_other_box_does_not_answer(self):
        # what this box served while the other did not answer mattered most then, and the tile said none of it
        # (review, 2026-11-03); a prompt too long for its pool is said apart
        why = "box spark2 does not answer: Connection refused"
        r = run(self, self.BODY % json.dumps({"node_id": "local", "boxes": {"spark2": {"error": why, "sent": {
            "affinity": 2, "balance": 1, "failover": 4, "too_long": 1, "rescued": 0}}}}))
        self.assertEqual(r["rows"], ["spark2: does not answer: Connection refused",
                                     "shared: 3 sent there (2 to stay with their conversation, 1 to even the load), 4 "
                                     "served here when it did not answer, 1 served here as too long for its pool"], r)
        r = run(self, self.BODY % json.dumps({"node_id": "local", "boxes": {"spark2": {"error": why, "sent": {
            "failover": 0}}}}))
        self.assertEqual(r["rows"], ["spark2: does not answer: Connection refused"], r)
        # none went there: no "0 sent there"
        r = run(self, self.BODY % json.dumps({"node_id": "local", "boxes": {"spark2": {"state": {
            "models": ["qwen3.8-27b"], "sent": {"failover": 2}}}}}))
        self.assertEqual(r["rows"], ["spark2: serves qwen3.8-27b", "shared: 2 served here when it did not answer"], r)

    def test_what_the_other_box_says_is_text_never_markup(self):
        hostile = '<img src=x onerror="alert(1)">'
        r = run(self, self.BODY % json.dumps({"node_id": "local", "boxes": {"spark2": {"state": {
            "models": [hostile], "version": hostile, "job": {"action": hostile}}}}}))
        self.assertIn(f"spark2: serves {hostile}", r["rows"])
        self.assertEqual(r["elements"], 2 * len(r["rows"]), "a dt and a dd per row, nothing else")


class EachLaneSaysItsEngine(unittest.TestCase):
    """Decision 1178 (Hasan, 2026-11-07): the engine each lane runs, and its version, said everywhere a lane is: the
    spine's capsule, the rack's bays, the Lanes cards and the Library's recipes. The page named none of them."""

    BODY = r"""
    const PIN = 'lmsysorg/sglang@sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae';
    const engines = {'qwen38-sglang.service': {engine: 'SGLang', version: '0.5.22', label: 'SGLang 0.5.22', pin: PIN},
                     'qwen38-kolibri.service': {engine: 'vLLM', version: '0.29.0 + Aleph Alpha plugin 1.0.0',
                                                label: 'vLLM 0.29.0 + Aleph Alpha plugin 1.0.0', pin: 'vllm/vllm-openai@sha256:18'}};
    feed({config: CONFIG, engine_table: {table: engines}, units: UNITS,
          lifecycle: life({'qwen38-sglang.service': eng('ready')})});
    const tb = $('rcp-table').tBodies[0]; clear(tb);
    const recipe = image => ({recipe: {id: 'stock', lane: '27b', builtin: true, engine: {image},
                                       model: {repo: 'RadixArk/Qwen3.8-27B-NVFP4', revision: '0123456789abcdef'},
                                       drafter: {algorithm: 'none'}, serve: {}}, installed: true, presence: {}, drift: []});
    recipeRow(tb, recipe(PIN)); recipeRow(tb, recipe('example.org/my-sglang@sha256:' + 'f'.repeat(64)));
    report({sub: txt('serving-sub'), card27: LCARDS.get('qwen38-sglang.service').right.textContent,
            cardKol: LCARDS.get('qwen38-kolibri.service').right.textContent,
            bay27: BAYS.get('qwen38-sglang.service').line.textContent,
            bayKol: BAYS.get('qwen38-kolibri.service').line.textContent,
            bayImg: BAYS.get('qwen38-image.service').line.textContent,
            pinned: tb.rows[0].cells[1].textContent, theirs: tb.rows[1].cells[1].textContent});
    """

    def test_the_capsule_the_bays_the_cards_and_the_library_say_it(self):
        r = run(self, self.BODY)
        self.assertTrue(r["sub"].startswith("SGLang 0.5.22 · "), r["sub"])
        self.assertIn("EngineSGLang 0.5.22", r["card27"])
        self.assertIn("EnginevLLM 0.29.0 + Aleph Alpha plugin 1.0.0", r["cardKol"])
        self.assertTrue(r["bay27"].startswith("engine SGLang 0.5.22"), r["bay27"])
        self.assertTrue(r["bayKol"].startswith("engine vLLM 0.29.0 + Aleph Alpha plugin 1.0.0"), r["bayKol"])
        self.assertNotIn("engine", r["bayImg"], "a lane the table lacks says no engine rather than a wrong one")
        self.assertEqual(r["pinned"], "SGLang 0.5.22 lmsysorg/sglang@e3a4f06001a2…")
        # an image of the operator's is not the pin: named by itself, never given the pin's version
        self.assertEqual(r["theirs"], "example.org/my-sglang@ffffffffffff…")

    LIVE = r"""
    const engines = {'qwen38-sglang.service': {engine: 'SGLang', version: '0.5.22', label: 'SGLang 0.5.22', pin: 'p', reports: '0.5.22'},
                     'qwen38-flash.service': {engine: 'SGLang', version: 'nightly 53295c6', label: 'SGLang nightly 53295c6',
                                              pin: 'q', reports: '0.0.0.dev1+g53295c697'}};
    const U = JSON.parse(JSON.stringify(UNITS));
    U.units['qwen38-sglang.service'] = {active: 'inactive', enabled: 'disabled'};
    U.units['qwen38-flash.service'] = {active: 'active', enabled: 'enabled'};
    feed({config: CONFIG, engine_table: {table: engines}, units: U,
          lifecycle: life({'qwen38-flash.service': eng('ready', {target: 'flash'})}),
          engine_info: {served_target: 'flash', info: {version: %s}}});
    report({sub: txt('serving-sub'), ver: txt('eng-ver'), card: LCARDS.get('qwen38-flash.service').right.textContent,
            other: LCARDS.get('qwen38-sglang.service').right.textContent});
    """

    def test_the_lane_that_serves_says_the_version_its_engine_reports(self):
        # decision 1178: the serving lane's version is read on its engine. The pinned image's: the table's words
        # (the nightly's own string, "0.0.0.dev1+g53295c697", read "SGLang 0.0.0.dev1" in the engine facts)
        r = run(self, self.LIVE % "'0.0.0.dev1+g53295c697'")
        self.assertTrue(r["sub"].startswith("SGLang nightly 53295c6 · "), r["sub"])
        self.assertEqual(r["ver"], "SGLang nightly 53295c6")
        self.assertIn("EngineSGLang nightly 53295c6", r["card"])
        # an image of the operator's: what its engine says, never the pin's version
        r = run(self, self.LIVE % "'0.0.0.dev1+g043cfc4aa'")
        self.assertTrue(r["sub"].startswith("SGLang 0.0.0.dev1+g043cfc4aa · "), r["sub"])
        self.assertIn("EngineSGLang 0.0.0.dev1+g043cfc4aa", r["card"])
        # a lane that does not serve keeps its pin's version
        self.assertIn("EngineSGLang 0.5.22", r["other"])

    def test_a_phone_capsule_keeps_the_lanes_state_and_a_bay_title_breaks_before_its_build(self):
        # 375 to 430 px: "Qwen3.8 27B NV..." left "ready" out of the capsule (captures of 2026-11-08), and a bay's title
        # broke right after its " · ". The part after the family is its own element: a phone hides it in the capsule,
        # a bay keeps it whole
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
        const ver = el => { const v = el.querySelector('.ver'); return v ? v.textContent : null; };
        report({name: txt('serving-name'), ver: ver($('serving-name')), bay: BAYS.get('qwen38-flash.service').h.textContent,
                bayVer: ver(BAYS.get('qwen38-sglang.service').h)});
        """)
        self.assertEqual(r["name"], "Qwen3.8 27B NVFP4 · RadixArk, ready")
        self.assertEqual(r["ver"], " NVFP4 · RadixArk")
        self.assertEqual(r["bayVer"], "NVFP4 · RadixArk")
        # and a tablet upright (820 px read "Qwen3.8 27B NVFP…", captures of 2026-11-08): the drawer's width, 980 px
        phone = [d for s, media, d in css_rules() if s == ".serving .what .ver" and any("max-width:980px" in m for m in media)]
        self.assertTrue(phone and "display:none" in phone[0].replace(" ", ""), phone)
        whole = [d for s, media, d in css_rules() if s == ".bay h3 .ver" and not media]
        self.assertTrue(whole and "white-space:nowrap" in whole[0].replace(" ", ""), whole)
        # the second line's small grey type is its own: given to every span of the capsule, it shrank "NVFP4 ·
        # RadixArk" on a MacBook (captures of 2026-11-08)
        rules = css_rules()
        self.assertFalse([s for s, _, _ in rules if s == ".serving .what span"])
        self.assertTrue([s for s, _, _ in rules if s == ".serving .what > span"])
        # and at 375 px "Qwen3.8 27B, ready" lacked some 10 px: the name a size down there
        small = [d for s, media, d in rules if s == ".serving .what b" and any("max-width:380px" in m for m in media)]
        self.assertTrue(small and "--t-13" in small[0], small)

    def test_a_lane_on_an_image_of_the_operators_is_not_given_the_pins_version(self):
        # its unit starts another image (engine_table's pinned: false): the pin's "vLLM 0.29.0 + ..." would be false
        r = run(self, self.BODY.replace("pin: 'vllm/vllm-openai@sha256:18'}", "pin: 'vllm/vllm-openai@sha256:18', pinned: false}"))
        self.assertTrue(r["bayKol"].startswith("engine vLLM, another image"), r["bayKol"])
        self.assertIn("EnginevLLM, another image", r["cardKol"])
        self.assertIn("EngineSGLang 0.5.22", r["card27"])

    def test_the_table_read_again_wins_and_a_stopped_lane_drops_its_engines_word(self):
        # code review of v1.45.0: the config's table is read once at the cockpit's start (engine_table refreshes it
        # every 30 s), and the version an engine reported was kept after its lane stopped
        r = run(self, self.LIVE % "'0.0.0.dev1+g043cfc4aa'" + r"""
        feed({engine_table: {table: Object.assign({}, engines, {'qwen38-sglang.service': {engine: 'SGLang', version: '0.5.22',
              label: 'SGLang 0.5.22', pin: 'p', reports: '0.5.22', pinned: false}})}});
        const U2 = JSON.parse(JSON.stringify(U)); U2.units['qwen38-flash.service'] = {active: 'inactive', enabled: 'enabled'};
        feed({units: U2, lifecycle: life({'qwen38-flash.service': eng('stopped', {target: 'flash'})})});
        report({other: LCARDS.get('qwen38-sglang.service').right.textContent, stopped: engineLabel('qwen38-flash.service')});
        """)
        self.assertIn("EngineSGLang, another image", r["other"])
        # stopped, the flash lane is named by its table again, not by the version its engine reported while it served
        self.assertEqual(r["stopped"], "SGLang nightly 53295c6", r)

    def test_without_the_table_nothing_is_said(self):
        # a cockpit of an older release beside this page (the table came with v1.45.0): no line reads "undefined"
        r = run(self, self.BODY.replace("engine_table: {table: engines}, ", ""))
        self.assertFalse(r["sub"].startswith(" · "), r["sub"])
        self.assertNotIn("Engine", r["card27"])
        self.assertNotIn("undefined", r["card27"] + r["bay27"] + r["sub"])
        self.assertEqual(r["pinned"], "lmsysorg/sglang@e3a4f06001a2…")


if __name__ == "__main__":
    unittest.main()
