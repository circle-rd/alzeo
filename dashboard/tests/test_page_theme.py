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
         "--field-bg", "--code-bg", "--reservoir-bg", "--mark-tile", "--overlay-ink", "--lane-brand", "--stage-1", "--stage-2", "--stage-3", "--scrim",
         "--tick-ink", "--spark-grid", "--logo-2", "--r-panel")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
