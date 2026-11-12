#!/usr/bin/env python3
"""The cockpit's three themes: chosen in this browser, and never half applied.

The page had one palette, fixed in the stylesheet, the meta theme-color and the WebGL
backdrop alike. A selector that changed a class but left one of them behind would be a
page that says one theme and paints another, so these tests read the three places a
theme lives: the stored preference, the data-theme attribute and the meta tag.
"""
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pagejs  # noqa: E402

DASH = pathlib.Path(__file__).resolve().parents[1]
CSS = (DASH / "static" / "css" / "cockpit.css").read_text()
THEMES = {
    "indigo": {"scheme": "dark"},
    "graphite": {"scheme": "dark"},
    "paper": {"scheme": "light"},
}
ROLES = ("--ink-0", "--rule-1", "--fg-1", "--brand", "--ok", "--warn", "--bad",
         "--surface", "--surface-border", "--primary-bg", "--primary-ink", "--primary-ink-soft",
         "--field-bg", "--code-bg", "--reservoir-bg", "--mark-tile", "--overlay-ink", "--brand-lane", "--seg-ink", "--stage-1", "--stage-2", "--stage-3", "--scrim",
         "--backdrop-ground", "--backdrop-a", "--backdrop-b", "--bootbar-top", "--bootbar-bottom",
         "--screen-bg", "--badge-sel-ink", "--badge-sel-border", "--badge-sel-edge",
         "--tick-ink", "--spark-grid", "--r-panel")


def run(test, body, **kw):
    return pagejs.run(test, body, **kw)


def block(theme):
    if theme == "indigo":
        return re.search(r"(?m)^:root\{(.*?)^\}", CSS, re.S).group(1)
    return re.search(r'(?m)^:root\[data-theme="%s"\]\{(.*?)^\}' % theme, CSS, re.S).group(1)


def token(theme, name):
    m = re.search(r"%s:\s*([^;]+);" % re.escape(name), block(theme))
    return m.group(1).strip() if m else None


def hex_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def luminance(value):
    def channel(c):
        return c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in hex_rgb(value))
    return .2126 * r + .7152 * g + .0722 * b


def rgba(value):
    r, g, b, a = re.match(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", value).groups()
    return int(r), int(g), int(b), float(a)


def gradient_peak(theme):
    """The ground under the text where both gradients lie at full strength, as one hex colour."""
    base = [int(c * 255) for c in hex_rgb(token(theme, "--ground"))]
    for name in ("--ground-a", "--ground-b"):
        r, g, b, a = rgba(token(theme, name))
        base = [round(c * a + d * (1 - a)) for c, d in zip((r, g, b), base)]
    return "#%02x%02x%02x" % tuple(base)


def contrast(a, b):
    x, y = sorted((luminance(a), luminance(b)), reverse=True)
    return (x + .05) / (y + .05)


class TheDefaultIsTheThemeThatWasAlwaysThere(unittest.TestCase):
    def test_no_preference_means_indigo(self):
        r = run(self, r"""
        report({theme: document.documentElement.dataset.theme,
                meta: document.querySelector('meta[name="theme-color"]').getAttribute('content'),
                picker: $('theme-select').value,
                stored: localStorage.getItem('cockpit.theme')});
        """)
        self.assertEqual(r, {"theme": "indigo", "meta": token("indigo", "--ground"), "picker": "indigo", "stored": None})

    def test_an_unknown_preference_is_no_preference(self):
        r = run(self, r"""
        report({theme: document.documentElement.dataset.theme,
                meta: document.querySelector('meta[name="theme-color"]').getAttribute('content'),
                picker: $('theme-select').value});
        """, setup="localStorage.setItem('cockpit.theme', 'constructor');")
        self.assertEqual(r, {"theme": "indigo", "meta": token("indigo", "--ground"), "picker": "indigo"})


class AStoredThemeIsAppliedBeforeThePageRuns(unittest.TestCase):
    def test_graphite_and_paper_are_restored(self):
        for name in ("graphite", "paper"):
            with self.subTest(theme=name):
                r = run(self, r"""
                report({theme: document.documentElement.dataset.theme,
                        meta: document.querySelector('meta[name="theme-color"]').getAttribute('content'),
                        picker: $('theme-select').value});
                """, setup="localStorage.setItem('cockpit.theme', '%s');" % name)
                self.assertEqual(r, {"theme": name, "meta": token(name, "--ground"), "picker": name})


class TheSettingsPickerChangesThePageAndRemembersIt(unittest.TestCase):
    def test_a_choice_reaches_the_attribute_the_meta_and_the_store(self):
        r = run(self, r"""
        const s = $('theme-select');
        s.value = 'paper';
        s.dispatchEvent({type: 'change'});
        report({theme: document.documentElement.dataset.theme,
                meta: document.querySelector('meta[name="theme-color"]').getAttribute('content'),
                stored: localStorage.getItem('cockpit.theme'), picker: s.value});
        """)
        self.assertEqual(r, {"theme": "paper", "meta": token("paper", "--ground"), "stored": "paper", "picker": "paper"})

    def test_a_choice_holds_without_storage(self):
        r = run(self, r"""
        const s = $('theme-select');
        s.value = 'graphite';
        s.dispatchEvent({type: 'change'});
        report({theme: document.documentElement.dataset.theme,
                meta: document.querySelector('meta[name="theme-color"]').getAttribute('content'),
                picker: s.value});
        """, opts={"noStorage": True})
        self.assertEqual(r, {"theme": "graphite", "meta": token("graphite", "--ground"), "picker": "graphite"})


class EveryThemeAnswersWithItsWholePalette(unittest.TestCase):
    def test_the_static_meta_starts_on_the_indigo_ground(self):
        for page in ("index.html", "login.html"):
            with self.subTest(page=page):
                html = (DASH / "static" / page).read_text()
                meta = re.search(r'<meta name="theme-color" content="([^"]+)">', html)
                self.assertEqual(meta.group(1), token("indigo", "--ground"))

    def test_the_pool_ticks_keep_spacing_and_ink_apart(self):
        ticks = re.search(r"\.reservoir \.ticks\{.*?background:([^}]+)\}", CSS, re.S).group(1)
        self.assertIn("var(--tick-ink)", ticks)
        self.assertIn("calc(var(--tick) - 1px)", ticks)
        self.assertNotIn("var(--tick) calc", ticks)

    def test_each_block_declares_the_roles_a_component_reads(self):
        for name in THEMES:
            with self.subTest(theme=name):
                body = block(name)
                for role in ROLES:
                    self.assertRegex(body, r"(?<![\w-])%s\s*:" % re.escape(role),
                                     "%s does not define %s" % (name, role))

    def test_each_block_names_its_colour_scheme(self):
        for name, want in THEMES.items():
            with self.subTest(theme=name):
                self.assertIn("color-scheme:%s" % want["scheme"], block(name).replace(" ", ""))

    def test_text_meets_aa_on_the_ground_and_the_panels(self):
        pairs = [("indigo", "--fg-1", "--ground"),
                 ("graphite", "--fg-1", "--ground"),
                 ("graphite", "--fg-2", "--surface"),
                 ("graphite", "--fg-3", "--surface"),
                 ("paper", "--fg-1", "--ground"),
                 ("paper", "--fg-2", "--surface"),
                 ("paper", "--fg-3", "--surface")]
        for theme, fg, bg in pairs:
            with self.subTest(theme=theme, fg=fg):
                self.assertGreaterEqual(contrast(token(theme, fg), token(theme, bg)), 4.5,
                                        "%s %s on %s" % (theme, fg, bg))

    def test_text_meets_aa_on_the_gradient_peak(self):
        for theme in ("graphite", "paper"):
            peak = gradient_peak(theme)
            for fg in ("--fg-1", "--fg-2", "--fg-3"):
                with self.subTest(theme=theme, fg=fg):
                    self.assertGreaterEqual(contrast(token(theme, fg), peak), 4.5,
                                            "%s %s on the gradient peak %s" % (theme, fg, peak))

    def test_the_ground_gradient_is_the_indigo_one_in_every_theme(self):
        shape = re.search(r"body::before\{.*?\n.*?\}", CSS, re.S).group(0)
        self.assertIn("var(--ground-a)", shape)
        self.assertIn("var(--ground-b)", shape)
        self.assertNotIn("rgba(122,62,232", shape)

    def test_state_text_meets_aa_on_its_panel(self):
        for theme in ("graphite", "paper"):
            for state in ("--ok-hi", "--warn-hi", "--bad-hi"):
                with self.subTest(theme=theme, state=state):
                    self.assertGreaterEqual(contrast(token(theme, state), token(theme, "--surface")), 4.5,
                                            "%s %s" % (theme, state))

    def test_the_primary_button_text_meets_aa_on_every_stop(self):
        for theme in ("graphite", "paper"):
            for name in ("--primary-ink", "--primary-ink-soft"):
                ink = token(theme, name)
                self.assertRegex(ink, r"^#[0-9a-f]{6}$", "%s %s" % (theme, name))
                stops = re.findall(r"#[0-9a-fA-F]{6}", token(theme, "--primary-bg"))
                self.assertTrue(stops, theme)
                for stop in stops:
                    with self.subTest(theme=theme, ink=name, stop=stop):
                        self.assertGreaterEqual(contrast(ink, stop), 4.5, "%s on %s" % (ink, stop))


class TheLogoIsIndigoInEveryTheme(unittest.TestCase):
    def test_no_theme_recolours_the_mark(self):
        self.assertNotIn("--logo", CSS)
        for theme in THEMES:
            with self.subTest(theme=theme):
                self.assertNotIn("--logo", block(theme))


class TheThemeIsAnEntryOfTheCockpitPanel(unittest.TestCase):
    def test_the_picker_sits_in_the_cockpit_facts_and_no_card_of_its_own(self):
        html = (DASH / "static" / "index.html").read_text()
        panel = re.search(r"<header><h2>Cockpit</h2></header>\s*<dl class=\"facts\">(.*?)</dl>", html, re.S)
        self.assertIsNotNone(panel)
        self.assertIn('id="theme-select"', panel.group(1))
        self.assertTrue(panel.group(1).rstrip().endswith("</dd>"))
        self.assertEqual(re.findall(r"<dt>(?:<label[^>]*>)?([^<]+)", panel.group(1))[-1], "Theme")
        self.assertNotIn("Appearance", html)


class TheThemesDidNotMoveTheDefault(unittest.TestCase):
    """The theming branch changed four indigo rules, resolved rule by rule against
    main: the empty screen's stroke and word, the gallery's caption and the ring's
    figure. The default theme is the page the cockpit shipped with, so those four
    stay the rules they were."""

    def test_the_four_rules_the_branch_had_moved(self):
        rules = {
            r".screen \.empty svg": "stroke:var(--rule-3)",
            r".screen \.empty b": "color:var(--fg-2)",
            r".gallery button span": "background:rgba(0,0,0,.65)",
            r".ring \.in b": "font-weight:620",
        }
        for rule, want in rules.items():
            with self.subTest(rule=rule):
                body = re.search(rule + r"\{([^}]*)\}", CSS).group(1)
                self.assertIn(want, body)
        for rule in (r".gallery button span", r".ring \.in b"):
            with self.subTest(rule=rule):
                self.assertNotIn("color:", re.search(rule + r"\{([^}]*)\}", CSS).group(1))


class TheSoonCardKeepsItsWordsOnTheArt(unittest.TestCase):
    """Qwen4's card draws a dark picture under its words. In Paper the veil is
    light, so the chip, which sits over the art and not under the veil, needed its
    own dark ground, and the veil had to hold over the width its words use."""

    def test_the_chip_has_its_own_ground_in_every_theme(self):
        self.assertIn("background:var(--art-chip-bg)", CSS)
        self.assertIn("color:var(--art-chip-ink)", CSS)
        for theme in THEMES:
            with self.subTest(theme=theme):
                self.assertIsNotNone(token(theme, "--art-chip-bg"))
                self.assertIsNotNone(token(theme, "--art-chip-ink"))
        r, g, b, a = rgba(token("paper", "--art-chip-bg"))
        self.assertGreaterEqual(a, .6, "paper's chip ground is too thin over the picture")
        self.assertEqual(token("paper", "--art-chip-ink"), "#ffffff")

    def test_paper_holds_its_veil_over_the_words(self):
        paper = re.search(r':root\[data-theme="paper"\] \.bay\.soon[^}]*\{([^}]*)\}', CSS, re.S).group(1)
        self.assertIn("var(--bay-veil-b) 62%", paper)
        self.assertIn("var(--bay-veil-c) 88%", paper)
        self.assertGreaterEqual(rgba(token("paper", "--bay-veil-b"))[3], .8)
        self.assertGreaterEqual(rgba(token("paper", "--bay-veil-c"))[3], .3)


class TheOtherThemesKeepTheIndigoGradientInTheirOwnGreys(unittest.TestCase):
    """Graphite and Paper take the indigo ground's radial gradient, not bubbles
    of another colour: black into dark grey, white into light grey."""

    def test_their_gradient_is_a_neutral_grey(self):
        for theme in ("graphite", "paper"):
            for name in ("--ground-a", "--ground-b", "--backdrop-a", "--backdrop-b"):
                with self.subTest(theme=theme, name=name):
                    r, g, b, a = rgba(token(theme, name))
                    self.assertLessEqual(max(r, g, b) - min(r, g, b), 4,
                                         "%s %s is not a neutral grey" % (theme, name))
            # the CSS light under the canvas is the same light the shader draws
            for name in ("--backdrop-a", "--backdrop-b"):
                with self.subTest(theme=theme, name=name, fallback=True):
                    self.assertEqual(token(theme, name), token(theme, name.replace("--backdrop", "--ground")))


class TheBackdropRunsInEveryTheme(unittest.TestCase):
    """The moving halos are the cockpit's ground, not an indigo decoration: each
    theme gives the shader its own ground and two halo colours, and no theme hides
    the canvas and leaves a still gradient behind."""

    def test_every_theme_feeds_the_shader(self):
        for theme in THEMES:
            for name in ("--backdrop-ground", "--backdrop-a", "--backdrop-b"):
                with self.subTest(theme=theme, name=name):
                    self.assertIsNotNone(token(theme, name))
        js = (DASH / "static" / "js" / "backdrop.js").read_text()
        for name in ("--backdrop-ground", "--backdrop-a", "--backdrop-b"):
            self.assertIn(name, js)
        self.assertNotIn(':root[data-theme="graphite"] #backdrop', CSS)
        self.assertNotIn(':root[data-theme="paper"] #backdrop', CSS)


class TheBootBarsFollowTheTheme(unittest.TestCase):
    """The boot history is a small gradient chart in each lane card. Indigo takes
    its violet brand, while graphite and paper take the dominant neutral of their
    room, not their cyan or teal accent."""

    def resolved(self, theme, name):
        value = token(theme, name)
        m = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
        return token(theme, m.group(1)) if m else value

    def test_the_boot_history_takes_the_theme_gradient(self):
        js = (DASH / "static" / "js" / "lanes.js").read_text()
        self.assertIn("var(--bootbar-top)", js)
        self.assertIn("var(--bootbar-bottom)", js)
        for theme in THEMES:
            for name in ("--bootbar-top", "--bootbar-bottom"):
                with self.subTest(theme=theme, name=name):
                    self.assertIsNotNone(token(theme, name))

    def test_graphite_and_paper_take_their_dominant_grey(self):
        for theme in ("graphite", "paper"):
            for name in ("--bootbar-top", "--bootbar-bottom"):
                with self.subTest(theme=theme, name=name):
                    self.assertNotIn("--brand", token(theme, name))
                    value = self.resolved(theme, name)
                    self.assertRegex(value, r"^#[0-9a-f]{6}$")
                    r, g, b = hex_rgb(value)
                    self.assertLessEqual(max(r, g, b) - min(r, g, b), 4,
                                         "%s %s is not a neutral grey" % (theme, name))
        self.assertIn("--brand", token("indigo", "--bootbar-top"))
        self.assertIn("--brand", token("indigo", "--bootbar-bottom"))


class TheStudioFollowsTheTheme(unittest.TestCase):
    """Image and Video: the result well used to keep an indigo background, and the
    selected example/reference badges were outlined in the accent. The badges keep
    the outline, but graphite and paper draw it in their inverse (the primary)
    colour, as the solid primary buttons already do."""

    def test_the_result_well_is_a_theme_role(self):
        self.assertIn("background:var(--screen-bg)", CSS)
        self.assertNotIn("background:#0b0720", CSS)
        self.assertEqual(token("indigo", "--screen-bg"), "#0b0720")
        for theme in THEMES:
            with self.subTest(theme=theme):
                self.assertIsNotNone(token(theme, "--screen-bg"))

    def test_the_selected_badges_keep_an_outline_in_the_inverse_colour(self):
        self.assertIn("border-color:var(--badge-sel-border)", CSS)
        self.assertIn("color:var(--badge-sel-ink)", CSS)
        self.assertIn("border-color:var(--badge-sel-edge)", CSS)
        for theme in THEMES:
            for name in ("--badge-sel-ink", "--badge-sel-border", "--badge-sel-edge"):
                with self.subTest(theme=theme, name=name):
                    self.assertIsNotNone(token(theme, name))
        for theme in ("graphite", "paper"):
            for name in ("--badge-sel-ink", "--badge-sel-border", "--badge-sel-edge"):
                with self.subTest(theme=theme, name=name, inverse=True):
                    self.assertIn("--primary-bg", token(theme, name))
        for name in ("--badge-sel-ink", "--badge-sel-border", "--badge-sel-edge"):
            with self.subTest(theme="indigo", name=name):
                self.assertIn("--brand", token("indigo", name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
