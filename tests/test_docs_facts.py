#!/usr/bin/env python3
"""The documents say what the code does, where a check can tell.

Three drifts found in review (2026-09-24), each of which a reader could not see
from the page itself:
- a code block of README.md was never closed, so on GitHub the paragraph after
  it, the heading of the cockpit section and its introduction rendered as code;
- install.sh's closing summary sent people to a cockpit tab named Benchmarks,
  which never existed, and the README's tab table promised a bench, a canary
  battery and a key regeneration no tab has;
- the README's disk requirements were not the preflight's (84 GB announced for
  a 27B target where the preflight asks 45 on one disk and 40 on another, 225
  for a flash target where it asks 230 and 35), so a box sized from the README
  was refused.
"""
import math
import pathlib
import re
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".claude", ".venv", ".venv-test", ".hypothesis", "node_modules", "__pycache__"}


def tracked(*patterns):
    """The tracked files matching the patterns, or, outside a git checkout, the same
    walk with the working directories of tools left out."""
    r = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--", *patterns],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout:
        return [REPO / p for p in r.stdout.split("\0") if p]
    found = set()
    for pat in patterns:
        for p in REPO.rglob(pat):
            if not SKIP_DIRS & set(p.relative_to(REPO).parts):
                found.add(p)
    return sorted(found)


FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def fence_problems(text):
    """CommonMark's rule, as far as this repo's pages need it: a block opens on a
    fence of three or more backticks or tildes, and only a fence of the same
    character, at least as long and with nothing after it, closes it. So a fence
    with an info string inside an open block cannot close it, and is the mark of a
    block left open above (README.md's Operations block ran into the next
    ```bash that way)."""
    problems, open_at = [], None
    for n, line in enumerate(text.splitlines(), 1):
        m = FENCE.match(line)
        if not m:
            continue
        fence, rest = m.group(1), m.group(2).strip()
        if open_at is None:
            open_at = (n, fence)
        elif fence[0] == open_at[1][0] and len(fence) >= len(open_at[1]) and not rest:
            open_at = None
        elif rest:
            problems.append(f"line {n}: a fence with an info string inside the block opened at line {open_at[0]}")
    if open_at is not None:
        problems.append(f"line {open_at[0]}: a code block that never closes")
    return problems


class TheCodeBlocksClose(unittest.TestCase):
    def test_every_tracked_page(self):
        pages = tracked("*.md")
        self.assertGreater(len(pages), 20, "the page list came back nearly empty")
        bad = {str(p.relative_to(REPO)): fence_problems(p.read_text(encoding="utf-8")) for p in pages}
        bad = {k: v for k, v in bad.items() if v}
        self.assertEqual(bad, {})

    def test_the_rule_sees_the_block_that_swallowed_a_heading(self):
        # the shape README.md had: a block that runs into the next opening fence
        text = "## Operations\n\n```bash\nls\n\nprose\n\n## The cockpit\n\n```bash\nx\n```\n"
        self.assertEqual(len(fence_problems(text)), 1)
        self.assertEqual(fence_problems("```\na\n```\n\n~~~~\n```\n~~~~\n"), [])
        self.assertEqual(fence_problems("```python\nx\n"), ["line 1: a code block that never closes"])


def nav_labels():
    html = (REPO / "dashboard/static/index.html").read_text()
    labels = re.findall(r'<button class="nav" data-view="[a-z]+"[^>]*>.*?<span class="lab">([^<]+)</span>', html)
    return set(labels)


# "<Name> tab" in prose and messages; a leading word that only introduces the name
# ("The Agent tab", "Every tab") is not part of it
TAB_REF = re.compile(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+)?) tab\b")
NOT_A_NAME = {"The", "Every", "One", "Each", "This", "That", "A", "An", "Its", "Any", "Which"}


class TheTabsTheDocsNameExist(unittest.TestCase):
    def test_the_page_has_its_tabs(self):
        self.assertGreaterEqual(len(nav_labels()), 10, "the nav markup moved: fix the pattern")

    def test_every_tab_named_anywhere_is_one_of_the_page(self):
        labels = nav_labels()
        unknown = []
        # the changelog is history: a tab it names may have been renamed since
        files = [p for p in tracked("*.md", "*.sh", "*.py", "*.js", "*.html") if p.name != "CHANGELOG.md"]
        for p in files:
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for m in TAB_REF.finditer(line):
                    words = m.group(1).split()
                    while words and words[0] in NOT_A_NAME:
                        words.pop(0)
                    if words and " ".join(words) not in labels:
                        unknown.append(f"{p.relative_to(REPO)}:{n}: {m.group(0)}")
        self.assertEqual(unknown, [], f"tabs the page does not have (it has {sorted(labels)})")

    def test_the_readme_table_lists_the_tabs_of_the_page(self):
        readme = (REPO / "README.md").read_text()
        section = readme[readme.index("## The cockpit"):]
        section = section[:section.index("\n## ", 1)]
        rows = re.findall(r"^\| \*\*([^*]+)\*\* \|", section, re.M)
        self.assertEqual(sorted(rows), sorted(nav_labels()))


def gb(gib):
    """A size of the preflight's in the README's unit. install.sh counts GiB, as df -BG gives
    them; the README says GB of 10^9 bytes, as disks are sold, rounded up, and a sum of the
    parts it names (its 93 GiB were 93 GB there, 99.9 GB of disk; found in review, 2026-10-16)."""
    return math.ceil(int(gib) * 2 ** 30 / 10 ** 9)


class TheDiskNeedsAreThePreflights(unittest.TestCase):
    def test_the_readme_states_what_the_installer_checks(self):
        sh = (REPO / "install.sh").read_text()
        pairs = [(gb(h), gb(d)) for h, d in re.findall(r"NEED_GB=(\d+); DOCKER_NEED_GB=(\d+); IMG_LABEL=", sh)]
        self.assertEqual(len(pairs), 2, "the preflight's two needs moved: fix the pattern")
        (b_hf, b_docker), (f_hf, f_docker) = pairs
        ple = re.findall(r"NEED_GB=\$\(\(NEED_GB \+ (\d+)\)\)", sh)
        self.assertEqual(len(ple), 1, "the PLE table's share moved: fix the pattern")
        f_hf += gb(ple[0])
        labels = [gb(n) for n in re.findall(r'IMG_LABEL="(\d+) GB Docker image"', sh)]
        readme = (REPO / "README.md").read_text()
        req = next(line for line in readme.splitlines() if line.startswith("Requirements:"))
        for n in (b_hf, b_docker, f_hf, f_docker, b_hf + b_docker, f_hf + f_docker):
            self.assertIn(f"**{n} GB**", req, f"install.sh checks {n} GB somewhere the README does not say")
        for n in labels:
            self.assertIn(f"its {n} GB image", req, f"install.sh names a {n} GB image the README does not")

    def test_the_help_gives_each_lane_one_size(self):
        # install.sh's help gave the Nemotron lane 60 GB in its overview, and 51 at its
        # --no-nemotron line, in the README and in the CHANGELOG (found in review, 2026-10-16)
        sh = (REPO / "install.sh").read_text()
        overview = sh[sh.index("A plain run installs the box"):sh.index("  --no-start ")]
        # a lane's size is the last number in its parenthesis, before what a ";" adds (the Flash
        # FP4-Local lane's copy of its table, on a cache on another disk)
        said = {re.sub(r"\s+", "-", name.lower()).replace("flash-fp4-local", "flash-local"):
                re.findall(r"\d+", inside.split(";")[0])[-1] for name, inside in
                re.findall(r"the (image|video|Kolibri|Nemotron Super|Nemotron|Flash\s+FP4-Local|Ornith\s+122B) lane \(([^)]*)\)",
                           overview)}
        flags = dict(re.findall(r"--no-(image|video|kolibri|nemotron-super|nemotron|flash-local|ornith-122b)\s+leave the .*?\((?:vLLM, )?(\d+) GB\)", sh))
        self.assertEqual(set(said), {"image", "video", "kolibri", "nemotron", "nemotron-super", "flash-local", "ornith-122b"},
                         overview)
        self.assertEqual(said, flags)



class TheGsm8kCountIsTheOneRunEvalAsks(unittest.TestCase):
    def test_our_scores_name_the_questions_they_were_asked(self):
        """run_eval's GSM8K asks the test set's 1,319 questions less the five it shows as examples in
        every prompt: 1,314. The README and BENCHMARKS.md gave scores "over all 1,319" that only
        1,314 can give: 97.41% is 1,280 of 1,314, and no count out of 1,319 rounds to it (found
        2026-10-21)."""
        self.assertEqual(round(100 * 1280 / 1314, 2), 97.41)
        self.assertFalse([k for k in range(1300) if round(100 * k / 1319, 2) == 97.41])
        for name in ("README.md", "BENCHMARKS.md"):
            self.assertNotRegex((REPO / name).read_text(), r"(?i)gsm8k[^|\n]*all 1,319", name)


NUMBERS = {w: n for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}


def present_pages():
    """The tracked pages a reader takes as the present: CHANGELOG.md is history, and keeps what
    each release said."""
    return [p for p in tracked("*.md") if p.name != "CHANGELOG.md"]


class TheKeepAsItIsCommandNamesEveryLane(unittest.TestCase):
    def test_operations_gives_a_no_flag_for_every_lane_the_installer_adds(self):
        """docs/operations.md tells how to keep a box as it is: a --no-<lane> for every lane on
        every run, since a --no-<lane> counts for the run that passes it only. Its command was
        `--no-flash --no-image --no-video`, written before the five vLLM lanes, and it still
        brought their units (audit, 2026-11-09)."""
        lanes = set(re.findall(r'lane_wanted ([a-z0-9-]+) "\$NO_', (REPO / "install.sh").read_text()))
        self.assertGreaterEqual(len(lanes), 5, lanes)
        text = (REPO / "docs/operations.md").read_text()
        m = re.search(r"To keep a box as it is,.*?`(\./install\.sh[^`]*)`", text, re.S)
        self.assertIsNotNone(m, "the keep-as-is command is no longer where this check reads it")
        self.assertEqual(lanes - {"27b"}, set(re.findall(r"--no-([a-z0-9-]+)", m.group(1))), m.group(1))


class TheCountsOfTextUnitsAreTheProxys(unittest.TestCase):
    def test_every_count_of_text_units_and_vllm_lanes(self):
        """The proxy holds requests while any text lane's unit starts (PROXY_HOLD_UNITS). The README
        said five text units share the engine's port, from before the sixth and seventh lanes
        (audit, 2026-11-09). A count a page gives is checked against that list, and a count of vLLM
        lanes against the units whose template runs vLLM."""
        units = re.search(r'PROXY_HOLD_UNITS=([^"]+)"',
                          (REPO / "qwen38-keepalive.service.template").read_text()).group(1).split()
        self.assertGreaterEqual(len(units), 2, units)
        vllm = [u for u in units if "vllm" in (REPO / f"{u}.template").read_text().lower()]
        expect = {"text": len(units), "vllm": len(vllm)}
        seen = 0
        for page in present_pages():
            for m in re.finditer(r"\b(" + "|".join(NUMBERS) + r") (text|vLLM) (?:units|lanes)\b",
                                 page.read_text(), re.I):
                seen += 1
                self.assertEqual(NUMBERS[m.group(1).lower()], expect[m.group(2).lower()],
                                 f"{page.relative_to(REPO)}: {m.group(0)!r}, the units say {expect}")
        self.assertGreaterEqual(seen, 1, "no page counts the text units: this check reads nothing")


class TheCockpitBackendIsItsModules(unittest.TestCase):
    def test_no_page_calls_the_backend_a_single_file(self):
        """The README and docs/cockpit.md called the cockpit's backend a single file, and cockpit.py
        imports six modules that sit beside it (audit, 2026-11-09). A page that counts them gives
        their number."""
        mods = [m for m in re.findall(r"^import ([a-z_]+) as [a-z]+$",
                                      (REPO / "dashboard/cockpit.py").read_text(), re.M)
                if (REPO / "dashboard" / f"{m}.py").exists()]
        self.assertTrue(mods, "cockpit.py imports no module of its own: this check reads nothing")
        counted = 0
        for page in present_pages():
            text = page.read_text()
            if re.search(r"(?i)single-file stdlib backend", text):
                self.fail(f"{page.relative_to(REPO)} calls the backend a single file; cockpit.py imports {mods}")
            for m in re.finditer(r"cockpit\.py and (\w+) modules beside it", text):
                counted += 1
                self.assertEqual(NUMBERS.get(m.group(1)), len(mods),
                                 f"{page.relative_to(REPO)}: {m.group(0)!r}, cockpit.py imports {mods}")
        self.assertGreaterEqual(counted, 1, "no page counts the backend's modules: this check reads nothing")


class ThePrivilegedPathNamesWhatSudoRuns(unittest.TestCase):
    def test_flush_and_abort_are_not_said_to_go_through_sudo(self):
        """The README and ARCHITECTURE.md said the cockpit's flush and abort go through its sudoers
        allowlist: they are calls to the engine and the proxy with the box's key, and the allowlist
        has no line for them (audit and review, 2026-11-09)."""
        sudoers = (REPO / "dashboard/sudoers-cockpit.template").read_text().lower()
        m = re.search(r"Everything privileged the cockpit can do \(([^)]*)\)", (REPO / "README.md").read_text())
        self.assertIsNotNone(m, "the README's sentence on the cockpit's privileges is no longer where this check reads it")
        said = [("README.md", m.group(1))]
        arch = [ln for ln in (REPO / "ARCHITECTURE.md").read_text().splitlines() if "sudo exact argv" in ln]
        self.assertTrue(arch, "ARCHITECTURE.md's sudo line is no longer where this check reads it")
        said += [("ARCHITECTURE.md", ln) for ln in arch]
        for word in ("flush", "abort"):
            if word in sudoers:
                continue
            for where, text in said:
                self.assertNotIn(word, text.lower(),
                                 f"{where}: {text.strip()!r} names {word}, which sudoers-cockpit.template does not run")


if __name__ == "__main__":
    unittest.main(verbosity=2)
