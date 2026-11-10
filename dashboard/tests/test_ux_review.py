#!/usr/bin/env python3
"""What the cockpit's ergonomics review found (2026-10-15), each on the page as it runs: Library
opening empty below its recipes, a median that was not one, "no engine" over a serving Kolibri
lane, the Kolibri engine's polls drowning its own journal, and Decide asking for a text lane
while one served; and what the test session of the whole cockpit found after it (Library's
tables and sizes). Same harness as test_page_behaviour.py."""
import json
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pagejs  # noqa: E402
from test_page_behaviour import css_rules, run  # noqa: E402

REG = {"images": [], "other_models": [], "caches": ["/c"], "models": [],
       "targets": [{"target": "stock", "repo": "RadixArk/Qwen3.8-27B-NVFP4", "rev": "a" * 40, "state": "complete", "installed": True},
                   {"target": "kolibri-nvfp4", "repo": "Quantinine/Kolibri-1-NVFP4A16-FP8", "rev": "b" * 40, "state": "absent", "installed": True}]}


class LibraryFillsItself(unittest.TestCase):
    """The checkpoints, what each lane can load (and its Download), the engine images and the
    other models waited for a click on Scan, while opening the view had just made the server
    read them (the recipes' load refreshes the registry): it opened empty below its recipes."""

    def open_library(self, times=1):
        return run(self, r"""
        __fetch = async (url, init) => url.startsWith('/api/recipes') ? __response(200, {builtin: [], custom: [], custom_dir: ''})
          : url.startsWith('/api/registry') ? __response(200, %s) : new Promise(() => {});
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
        for (let i = 0; i < %d; i++){ showView('library'); await __settle(); await __settle(); showView('now'); await __settle(); }
        showView('library'); await __settle();
        report({reads: __fetches.map(f => f.url).filter(u => u.startsWith('/api/registry')),
                targets: $('reg-targets').tBodies[0].rows.length});
        """ % (json.dumps(REG), times))

    def test_it_shows_what_the_server_read_as_it_opens(self):
        r = self.open_library()
        self.assertEqual(r["targets"], 2)
        self.assertEqual(r["reads"], ["/api/registry"])      # the server's read, not a second scan

    def test_it_reads_once_per_page_and_scan_still_scans(self):
        self.assertEqual(self.open_library(times=3)["reads"], ["/api/registry"])
        r = run(self, r"""
        __fetch = async (url, init) => url.startsWith('/api/registry') ? __response(200, %s) : new Promise(() => {});
        $('reg-btn').click(); await __settle();
        report(__fetches.map(f => f.url).filter(u => u.startsWith('/api/registry')));
        """ % json.dumps(REG))
        self.assertEqual(r, ["/api/registry?refresh=1"])


class LibraryReadsAgainWhatAJobChanged(unittest.TestCase):
    """Read once per page, the checkpoints stayed as they were after a Load or a Download made
    outside Library: what changes the caches has it read them again, at once when it is open,
    else at its next opening (found in review, 2026-10-15)."""

    def reads(self, action, library_open):
        return run(self, r"""
        __fetch = async (url, init) => url.startsWith('/api/recipes') ? __response(200, {builtin: [], custom: [], custom_dir: ''})
          : url.startsWith('/api/registry') ? __response(200, %s) : new Promise(() => {});
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
        showView('library'); await __settle(); await __settle();
        if (!%s){ showView('now'); await __settle(); }
        const before = __fetches.length;
        (HANDLERS.job_finished || []).forEach(f => f({action: %s, id: 'j1'}));
        await __settle(); await __settle();
        const now = __fetches.slice(before).map(f => f.url).filter(u => u.startsWith('/api/registry'));
        showView('library'); await __settle(); await __settle();
        report({now, then: __fetches.slice(before).map(f => f.url).filter(u => u.startsWith('/api/registry'))});
        """ % (json.dumps(REG), "true" if library_open else "false", json.dumps(action)))

    def test_a_download_or_a_load_has_it_read_again(self):
        for action in ("fetch", "switch", "prepare", "delete_strays"):
            with self.subTest(action=action, library="closed"):
                r = self.reads(action, False)
                self.assertEqual(r["now"], [])
                self.assertEqual(len(r["then"]), 1)                    # at its next opening
            with self.subTest(action=action, library="open"):
                self.assertEqual(len(self.reads(action, True)["now"]), 1)   # at once

    def test_a_start_or_a_stop_leaves_the_caches_read(self):
        self.assertEqual(self.reads("unit", False)["then"], [])


class TheMedianOfAnEvenCount(unittest.TestCase):
    """"Last 2 boots: 14 min 47, 15 min 37. Median 15 min 37": the upper of the two middle
    values stood for the median, and for every "answers in about" said from it."""

    def test_an_even_count_takes_the_mean_of_its_two_middles(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({
          'qwen38-flash.service': eng('stopped', {target: 'flash', boots: [937, 887]}),
          'qwen38-image.service': eng('stopped', {target: 'image', boots: [61, 75, 60]}),
          'qwen38-video.service': eng('stopped', {target: 'video', boots: [700, 720, 712, 735]})})});
        report({two: bootSeconds('qwen38-flash.service'), three: bootSeconds('qwen38-image.service'),
                four: bootSeconds('qwen38-video.service'), said: readyIn('qwen38-flash.service')});
        """)
        self.assertEqual(r["two"], 912)
        self.assertEqual(r["three"], 61)
        self.assertEqual(r["four"], 716)
        self.assertIn("15 min 12", r["said"])



class OneBootIsSaidInTheSingular(unittest.TestCase):
    """A lane seen booting once read "Last 1 boots: 5 min 22. Median 5 min 22.": its fix, planned for
    v1.35.1, was never made (found by an audit of the plans, 2026-10-20). One boot is "Last boot",
    with no median of one beside it."""

    def test_one_boot_and_two(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({
          'qwen38-flash.service': eng('stopped', {target: 'flash', boots: [322]}),
          'qwen38-image.service': eng('stopped', {target: 'image', boots: [61, 75]})})});
        const said = u => [...LCARDS.get(u).hist.querySelectorAll('p')].map(p => p.textContent).find(t => /boot/i.test(t));
        report({one: said('qwen38-flash.service'), two: said('qwen38-image.service')});
        """)
        self.assertEqual(r["one"], "Last boot: 5 min 22.")
        self.assertEqual(r["two"], "Last 2 boots: 61 s, 75 s. Median 68 s.")

class TheRequestIdOverrideOnKolibri(unittest.TestCase):
    """Traffic said "Request id override: no engine" while Kolibri served: the override is an
    SGLang setting the guard reads on the SGLang lanes only."""

    def test_a_serving_kolibri_lane_is_not_no_engine(self):
        r = run(self, r"""
        const guard = {lane: null, override: null, state: 'ok', verdict: 'no abandoned request in this window', window: '10m', guard: {}, zombies: {}};
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-kolibri.service': eng('ready', {target: 'kolibri'})})});
        feed({reqguard: guard});
        const kolibri = txt('zg-override');
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped')})});
        feed({reqguard: guard});
        report({kolibri, none: txt('zg-override')});
        """)
        self.assertIn("not on the Kolibri-1 lane", r["kolibri"])
        self.assertEqual(r["none"], "no engine")


class TheEnginesOwnPollsAreDimmed(unittest.TestCase):
    """The Kolibri engine's journal was the box's own polls, line after line (its metrics every
    few seconds, its version, its models): the page dimmed the SGLang lanes' polls only."""

    def test_kolibris_polls_dim_like_the_others_and_a_request_does_not(self):
        r = run(self, r"""
        logLines = ['(APIServer pid=1) INFO:     127.0.0.1:1 - "GET /metrics HTTP/1.1" 200 OK',
                    '(APIServer pid=1) INFO:     127.0.0.1:2 - "GET /version HTTP/1.1" 200 OK',
                    '(APIServer pid=1) INFO:     127.0.0.1:3 - "GET /v1/models HTTP/1.1" 200 OK',
                    '(APIServer pid=1) INFO:     127.0.0.1:4 - "GET /health HTTP/1.1" 200 OK',
                    '(APIServer pid=1) INFO:     127.0.0.1:5 - "POST /v1/chat/completions HTTP/1.1" 200 OK'];
        drawLog();
        report([...$('log-view').querySelectorAll('span')].map(s => s.className || ''));
        """)
        self.assertEqual(r, ["l-dim", "l-dim", "l-dim", "l-dim", ""])

    def test_a_poll_refused_stands_out(self):
        # the proxy's journal is its clients' requests too: a 401 without the key, a 503 with no
        # engine, were dimmed among the polls it answered (found in review, 2026-10-15)
        r = run(self, r"""
        logLines = ['[proxy] 10.0.0.2:5 GET /v1/models ok get in 0.0s',
                    '[proxy] 10.0.0.2:6 GET /v1/models 401 without the serving key in 0.0s',
                    '[proxy] 10.0.0.2:7 GET /v1/models 503 engine unreachable in 0.0s',
                    '[proxy] 10.0.0.2:8 GET /metrics ok get in 0.0s',
                    '(APIServer pid=1) INFO:     127.0.0.1:9 - "GET /v1/models HTTP/1.1" 401 Unauthorized'];
        drawLog();
        report([...$('log-view').querySelectorAll('span')].map(s => s.className || ''));
        """)
        self.assertEqual(r, ["l-dim", "", "", "l-dim", ""])


class SystemOneNamesTheLanesThatServeIt(unittest.TestCase):
    """Under Ask, Decide said System One "needs a text lane" while the Kolibri text lane served,
    over a note saying the 27B or the flash lane answers it."""

    def test_the_line_under_ask_names_them(self):
        r = run(self, r"""
        __fetch = async url => url === '/api/systemone' ? __response(200, {available: false, lane: '',
          reason: 'The Kolibri-1 lane is serving: System One is answered by the 27B or the flash lane'}) : new Promise(() => {});
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-kolibri.service': eng('ready', {target: 'kolibri'})})});
        showView('decide'); await __settle();
        report(txt('s1-why'));
        """)
        self.assertIn("the 27B or the flash lane", r)
        self.assertNotIn("needs a text lane", r)

    def test_with_a_text_lane_serving_the_line_defers_to_the_note(self):
        # the 27B serves and the proxy does not answer: the line said the 27B or the flash lane
        # answers it, over the 27B serving (found in review, 2026-10-15)
        r = run(self, r"""
        __fetch = async url => url === '/api/systemone' ? __response(200, {available: false, lane: '',
          reason: 'the proxy in front of this box does not answer /v1/systemone'}) : new Promise(() => {});
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
        showView('decide'); await __settle();
        report({why: txt('s1-why'), note: txt('s1-note')});
        """)
        self.assertEqual(r["why"], "System One does not answer right now: the note says why.")
        self.assertIn("does not answer /v1/systemone", r["note"])


class LibrarysEngineImagesFitTheirPanel(unittest.TestCase):
    """An image named by its digest (vllm/vllm-openai@sha256:..., 88 characters with no break)
    widened Library's first column past its panel on a desk screen: Size and ID went out of view,
    behind a scroll nothing showed (found in the cockpit's test session, 2026-10-15, at 636 px
    for a 584 px panel). Its name breaks anywhere, at every width."""

    def test_the_name_of_an_image_breaks_anywhere_at_every_width(self):
        ref = "vllm/vllm-openai@sha256:" + "a7" * 32
        reg = dict(REG, images=[{"ref": ref, "size": "24.4GB", "id": "bc7762f87a5e"}])
        r = run(self, r"""
        __fetch = async (url, init) => url.startsWith('/api/recipes') ? __response(200, {builtin: [], custom: [], custom_dir: ''})
          : url.startsWith('/api/registry') ? __response(200, %s) : new Promise(() => {});
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
        showView('library'); await __settle(); await __settle();
        report([...$('reg-images').tBodies[0].rows[0].cells].map(c => ({text: c.textContent, cls: c.className})));
        """ % json.dumps(reg))
        self.assertEqual([c["text"] for c in r], [ref, "24.4GB", "bc7762f87a5e"])
        name = r[0]["cls"].split()
        # a rule under no @media: the phone's stacked rows already break anywhere, the desk did not
        breaks = [sel for sel, media, decl in css_rules() if not media and re.search(r"overflow-wrap:\s*anywhere", decl)
                  and any(re.fullmatch(r"\.tbl\s+(td)?\.%s" % re.escape(c), s.strip()) for c in name for s in sel.split(","))]
        self.assertTrue(breaks, "no rule at every width lets the image's name break anywhere")


class LibrarysSizesKeepTheirUnit(unittest.TestCase):
    """Library's Size and On disk columns, squeezed on a desk screen, laid "73.4" over "GiB": a
    size read as two values (found in the cockpit's test session, 2026-10-15, in the checkpoints
    and the other models at 1024 to 1600 px). Every size in its tables keeps its unit on its line."""

    def test_every_size_in_librarys_tables_stays_on_one_line(self):
        reg = dict(REG, models=[{"repo_id": "RadixArk/Qwen3.8-27B-NVFP4", "managed": True, "disk_bytes": 21_900_000_000,
                                 "revisions": [{"rev": "a" * 40, "bytes": 21_900_000_000, "status": "pinned", "pin": "STOCK_REV"}]}],
                   other_models=[{"repo_id": "x/y", "disk_bytes": 75_000_000_000}],
                   images=[{"ref": "lmsysorg/sglang:v0.5.21", "size": "33.7GB", "id": "bc7762f87a5e"}])
        r = run(self, r"""
        __fetch = async (url, init) => url.startsWith('/api/recipes') ? __response(200, {builtin: [], custom: [], custom_dir: ''})
          : url.startsWith('/api/registry') ? __response(200, %s) : new Promise(() => {});
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
        showView('library'); await __settle(); await __settle();
        report(['reg-models', 'reg-images', 'reg-other'].flatMap(id => [...$(id).tBodies[0].rows].flatMap(row =>
          [...row.cells].filter(c => /^[0-9.]+ ?(GiB|GB)$/.test(c.textContent)).map(c => ({table: id, text: c.textContent, cls: c.className})))));
        """ % json.dumps(reg))
        self.assertEqual(sorted((c["table"], c["text"]) for c in r),
                         [("reg-images", "33.7GB"), ("reg-models", "20.4 GiB"), ("reg-models", "20.4 GiB"), ("reg-other", "69.8 GiB")])
        # a rule under no @media: the squeeze is a desk screen's
        nowrap = {s.strip() for sel, media, decl in css_rules() if not media and re.search(r"white-space:\s*nowrap", decl)
                  for s in sel.split(",")}
        for c in r:
            with self.subTest(**c):
                self.assertTrue(any(re.fullmatch(r"\.tbl\s+(td)?\.%s" % re.escape(k), s) for k in c["cls"].split() for s in nowrap),
                                "this size can be laid over two lines")


class LibrarysColumnsStackBeforeATableRunsPastItsPanel(unittest.TestCase):
    """Library's two columns hold tables of five columns, and at 1280 px each column was 468 px:
    the checkpoints ran 56 px past their panel, behind a scroll nothing showed, and 131 px once
    their sizes kept their unit and the Nemotron lane's pins came in (measured in the browser,
    2026-10-15). They stand side by side only while each gets 600 px, else one under the other:
    nothing past its panel at 1600, 1440, 1280 and 1024 px (the same session)."""

    def test_the_two_columns_wrap_below_six_hundred_pixels_each(self):
        html = (pagejs.STATIC / "index.html").read_text()
        lib = html[html.index('id="view-library"'):html.index("<!-- ══ LOGS")]
        grids = re.findall(r'<div class="(grid [^"]*)">', lib)
        self.assertEqual(len(grids), 1, grids)
        cls = set(grids[0].split())
        wraps = [sel for sel, media, decl in css_rules() if not media
                 and re.search(r"grid-template-columns:\s*repeat\(auto-fit,\s*minmax\(min\(100%,\s*600px\),\s*1fr\)\)", decl)
                 and any(set(re.findall(r"\.([\w-]+)", s)) <= cls for s in sel.split(","))]
        self.assertTrue(wraps, f"no rule at every width lets Library's columns ({sorted(cls)}) wrap below 600 px")


class ALoadThatEndedOffersToCloseItsWindow(unittest.TestCase):
    """Every step done, the load window said "Done. The lane is booting" over a Cancel button, for
    a chain with nothing left to cancel (found in the cockpit's test session, 2026-10-15, at the end
    of each lane's Load). Its button closes the window, and says so; the window still closes itself."""

    def test_the_button_of_a_finished_load_says_close(self):
        from test_page_behaviour import TheLoadWindowFollowsAStepToItsEnd as T
        r = run(self, T.BODY + r"""
        const done = (id, name) => ({id, action: name, status: 'done', rc: 0, elapsed: 31});
        job(null, [done('j1', 'switch')]); await __advance(2000); await __settle(); await __advance(1000);
        job(null, [done('j2', 'unit'), done('j1', 'switch')]); await __advance(500); await __settle();
        const end = {status: txt('sh-status'), label: txt('sh-cancel'), open: !$('scrim').hidden, actions: state().actions};
        await __advance(1500); await __settle();
        report({end, after: {open: !$('scrim').hidden, view: activeView}});
        """)
        self.assertEqual(r["end"]["actions"], ["switch", "unit"], r)
        self.assertIn("Done. The lane is booting", r["end"]["status"])
        self.assertTrue(r["end"]["open"], "seen before it closes itself")
        self.assertEqual(r["end"]["label"], "Close")
        self.assertEqual(r["after"], {"open": False, "view": "now"})


class TheTablesThatStackOnAPhoneStayTablesOnADesk(unittest.TestCase):
    """.stack is the layout's flex column, and the tables whose class says they stack on a phone
    carried it too: on a desk screen each was a flex column, its header and its body laid out
    apart, the headings bunched at the left and the columns out of line under them (Library's
    recipes, checkpoints, targets, engine images and pins; found in the cockpit's test session,
    2026-10-15). Wider than a phone they are tables, and on a phone they still stack."""

    def selectors(self, sel):
        return [s.strip() for s in sel.split(",")]

    def test_a_stacking_table_is_a_table_wider_than_a_phone(self):
        page = (pagejs.STATIC / "index.html").read_text()
        stacking = [c.split() for c in re.findall(r'<table class="([^"]*)"', page) if "stack" in c.split()]
        self.assertTrue(stacking)
        for classes in stacking:
            self.assertIn("tbl", classes)       # the rule below reads .tbl.stack
        rules = css_rules()
        self.assertTrue([1 for sel, media, decl in rules if not media and ".stack" in self.selectors(sel)
                         and re.search(r"display:\s*flex", decl)], "the layout's .stack this guards against")
        self.assertTrue([1 for sel, media, decl in rules if not media and ".tbl.stack" in self.selectors(sel)
                         and re.search(r"display:\s*table\s*(;|$)", decl)], "no rule makes them tables again")
        self.assertTrue([1 for sel, media, decl in rules if any("max-width" in m for m in media)
                         and ".tbl.stack" in self.selectors(sel) and re.search(r"display:\s*block", decl)],
                        "on a phone they stack")


if __name__ == "__main__":
    unittest.main()
