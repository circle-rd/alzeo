#!/usr/bin/env python3
"""Every list and table of the lanes written by hand says what lane_registry.py says.

A lane is written by hand in some hundred places: install.sh's flags, cases and messages, the
switch's targets, the units, the proxy's names, opencode's providers, the cockpit's Python and its
page, the sudoers, the CI and the tests' own lists. Ornith 1.5 122B, the last lane added, touched
76 files in the six commits of its own (counted on 2026-11-10), and lists nobody compared drifted:
the offline suite's guard never learnt the video and speech lanes' ports, and install.sh said
"Kolibri", "Nemotron" and "Nemotron Super" a few lines from the official names its lane_title
gives (both found on 2026-11-10).
lane_registry.py says once what each lane is; this holds every place that writes it to that.

Each place is found by what it is called (a variable, a function, a case on a named variable, a
step of the CI), never by a line number, and one that cannot be found or reads empty fails: an
empty result proves nothing. A list that leaves lanes out on purpose is bound to the family it
keeps, with the reason its own code gives. A drift found here and not corrected yet is excused by
its exact value in TO_DECIDE, so that it fails again the day it changes, and the check that reads
an excuse fails once it excuses nothing. Nothing here starts a process, opens a port or reads the
network: the files are read as text, four modules are loaded for the tables they hold
(lane_registry.py, boxmem.py, dashboard/engines.py, tests/test_lane_lists_whole.py), and the few
functions read for what they compute run in this process from their own source (the cockpit's
target_lane and target_unit, the main of each oc-<lane>.py, which prints the provider block
opencode gets)."""
import __future__
import ast
import contextlib
import importlib.util
import io
import json
import pathlib
import re
import sys
import textwrap
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reg = load("lane_registry_under_test", REPO / "lane_registry.py")
boxmem = load("boxmem_for_lane_registry", REPO / "boxmem.py")
engines = load("engines_for_lane_registry", REPO / "dashboard" / "engines.py")
# the families tests/test_lane_lists_whole.py reads from the files themselves (ENGINE, TEXT, VLLM,
# B12X), taken from it rather than derived a second time
whole = load("lane_lists_whole_for_lane_registry", REPO / "tests" / "test_lane_lists_whole.py")

LANES = reg.LANES
UNIT = {lane: f["unit"] for lane, f in LANES.items()}
LANE_OF_UNIT = {u: lane for lane, u in UNIT.items()}
TARGET_LANE = reg.targets()
# the prefix install.sh, switch-model.sh and the CI give a lane's variables: WITH_ORNITH_122B, WANT_NEMOTRON_SUPER
VAR = {lane: lane.upper().replace("-", "_") for lane in LANES}
LANE_OF_VAR = {v: lane for lane, v in VAR.items()}


def family(**fields):
    """The lanes whose fields hold these values, in the registry's order (a list: a check reads its order)."""
    return list(reg.lanes(**fields))


def among(lanes, keep):
    return [x for x in lanes if x in keep]


# The lanes that take the GPU in turn, each unit in Conflicts= with the others: all but the speech lane,
# which runs on the CPU beside any of them (install-speech.sh, "WHY THE CPU"), and which the cockpit,
# the sudoers and the switch do not drive.
TURNS = [x for x in LANES if LANES[x]["kind"] != "speech"]
# the lanes install.sh's --with-<lane> and --no-<lane> name
FLAGGED = family(flags=True)
# among them the side lanes, each installed by its own installer, which install.sh runs (it installs
# the two SGLang text lanes itself: MODEL_CHOICE's)
SIDE = [x for x in FLAGGED if LANES[x]["installer"] != "install.sh"]
INSTALL_SH = family(installer="install.sh")
TEXT = family(kind="text")
VLLM = family(engine="vllm")
SGLANG = family(engine="sglang")
B12X = family(b12x=True)
NOT_64 = family(fits_64=False)
# the flags of install.sh that are no lane's: components and how a run goes
NOT_LANES = {"opencode", "cockpit", "start", "service", "claude-warmup"}


def targets_of(lanes):
    return [t for t, lane in TARGET_LANE.items() if lane in lanes]


def units_of(lanes):
    return [UNIT[x] for x in LANES if x in lanes]


# ---------------------------------------------------------------------------------------------------
# Drifts this test finds and step 1 of the lane factory leaves as they are (2026-11-10), each to
# decide: (file, construct, key) -> the exact value found there. Any other value there still fails.

TO_DECIDE = {
    # to decide (inventory, 14.2): the b12x lanes in a sentence, the two Nemotron lanes under their
    # family's name and Ornith under a short one, beside LANE_NAME's official names
    ("dashboard/cockpit.py", "B12X_LANE_NAME", "nemotron"): "the Nemotron lane",
    ("dashboard/cockpit.py", "B12X_LANE_NAME", "nemotron-super"): "the Nemotron lane",
    ("dashboard/cockpit.py", "B12X_LANE_NAME", "ornith-122b"): "the Ornith lane",
    ("dashboard/static/js/base.js", "b12xName", "nemotron"): "the Nemotron lane",
    ("dashboard/static/js/base.js", "b12xName", "nemotron-super"): "the Nemotron lane",
    ("dashboard/static/js/base.js", "b12xName", "ornith-122b"): "the Ornith lane",
}


def excuses(*where):
    """The TO_DECIDE keys of a file, or of a file's construct."""
    return [k for k in TO_DECIDE if k[:len(where)] == where]


class Problems(list):
    """What a check found wrong, one line each: the file and line, what is missing, what is extra."""

    def __init__(self):
        super().__init__()
        self.excused = set()

    def drift(self, key, found, line):
        """A drift at key, its value found: excused when TO_DECIDE holds that exact value, else a problem."""
        if TO_DECIDE.get(key) == found:
            self.excused.add(key)
        else:
            self.append(line)

    def same(self, where, got, want):
        """A list against what it must hold, as sets: what is missing and what is extra."""
        got, want = list(got), list(want)
        missing = [x for x in want if x not in got]
        extra = [x for x in got if x not in want]
        if missing or extra or len(set(got)) != len(got):
            self.append(f"{where}: missing {missing}, extra {extra}" + (", one twice" if len(set(got)) != len(got) else ""))

    def table(self, where, got: dict, want: dict):
        """A table against what it must hold: its keys as same() reads them, then each value that differs."""
        self.same(f"{where} (keys)", list(got), list(want))
        for k in want:
            if k in got and got[k] != want[k]:
                self.append(f"{where}[{k!r}]: {got[k]!r}, the registry says {want[k]!r}")

    def check(self, test, keys=()):
        """Fail with every problem at once, and with every excuse of these keys that excused nothing."""
        stale = [f"TO_DECIDE{list(k)} excuses {TO_DECIDE[k]!r}, which is not there any more: remove the excuse"
                 for k in keys if k not in self.excused]
        test.assertEqual(list(self) + stale, [])


class NotFound(AssertionError):
    """A place this test reads is not where it says: the file changed, and this test with it."""


def text_of(rel):
    try:
        return (REPO / rel).read_text()
    except OSError as e:
        raise NotFound(f"{rel}: {e}")


def line_at(text, pos):
    return text.count("\n", 0, pos) + 1


def floor(items, where, least=1):
    """What an extraction found, failing when it found fewer than it must: an empty list is no proof."""
    if len(items) < least:
        raise NotFound(f"{where}: found {len(items)}, at least {least} expected")
    return items


# ---------------------------------------------------------------------------------------------------
# Shell: assignments, functions and case statements, read as bash reads them (quotes, $(...), comments
# and here-documents skipped), so that a ";;", a "case" or an "esac" in a string or a help text never
# ends an arm.

HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def sh_assign(text, name, where):
    """(value, line) of NAME=... at the start of a line, its first time, its quotes removed."""
    m = re.search(rf"^{name}=(?:\"([^\"]*)\"|'([^']*)'|([^\s;]*))", text, re.M)
    if not m:
        raise NotFound(f"{where}: no {name}= at the start of a line")
    return next(g for g in m.groups() if g is not None), line_at(text, m.start())


def sh_function(text, name, where):
    """(line, body) of a shell function written name(){ ... }, its closing brace alone at the start of a line."""
    m = re.search(rf"^{re.escape(name)}\s*\(\)\s*\{{", text, re.M)
    if not m:
        raise NotFound(f"{where}: no function {name}()")
    end = re.compile(r"^\}", re.M).search(text, m.end())
    if not end:
        raise NotFound(f"{where}: {name}() never closes")
    return line_at(text, m.start()), text[m.end():end.start()]


def _skip_quoted(text, i):
    """Past the double-quoted string at i, its $(...) and `...` included."""
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1
        elif text.startswith("$(", i):
            i = _skip_substitution(text, i + 2)
        elif c == "`":
            j = text.find("`", i + 1)
            if j < 0:
                break
            i = j + 1
        else:
            i += 1
    raise ValueError(f"line {line_at(text, i)}: a string never ends")


def _skip_substitution(text, i):
    """Past the ")" that closes the $( or $(( opened just before i."""
    n, depth = len(text), 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                break
            i = j + 1
        elif c == '"':
            i = _skip_quoted(text, i)
        elif c == "(":
            depth, i = depth + 1, i + 1
        elif c == ")":
            depth, i = depth - 1, i + 1
            if depth == 0:
                return i
        else:
            i += 1
    raise ValueError(f"line {line_at(text, i)}: a $( never closes")


def _case_header(text, i):
    """Past the `in` of a case statement whose `case` starts at i, its subject read as one shell word
    ("$(flag '--max-running-requests')" has a space inside), or None when it is no case statement."""
    if not _word_at(text, i, "case"):
        return None
    j, n = i + 4, len(text)
    while j < n and text[j] in " \t":
        j += 1
    start = j
    while j < n and text[j] not in " \t\n;":
        if text[j] == '"':
            j = _skip_quoted(text, j)
        elif text[j] == "'":
            k = text.find("'", j + 1)
            if k < 0:
                return None
            j = k + 1
        elif text.startswith("$(", j):
            j = _skip_substitution(text, j + 2)
        else:
            j += 1
    m = re.compile(r"[ \t]+in(?=[ \t\n;])").match(text, j)
    return m.end() if m and j > start else None


def _word_at(text, i, word):
    """Whether a shell keyword starts at i, as a word of its own."""
    before = text[i - 1] if i else "\n"
    after = text[i + len(word)] if i + len(word) < len(text) else "\n"
    return text.startswith(word, i) and before in " \t\n;(&|" and after in " \t\n;)"


def sh_case(text, at):
    """([(patterns, body, line)] of each arm, end) of the case statement starting at `at`. Raises
    ValueError with the line where it lost its place: it never reads half a case."""
    i, n = _case_header(text, at), len(text)
    if i is None:
        raise ValueError(f"line {line_at(text, at)}: no case statement here")
    depth, chunks, start, pending = 0, [], i, []
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                raise ValueError(f"line {line_at(text, i)}: a quote never ends")
            i = j + 1
        elif c == '"':
            i = _skip_quoted(text, i)
        elif text.startswith("$(", i):
            i = _skip_substitution(text, i + 2)
        elif c == "#" and text[i - 1] in " \t\n;":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("<<<", i):
            i += 3
        elif HEREDOC.match(text, i):
            h = HEREDOC.match(text, i)
            pending.append((h.group(3), h.group(1) == "-"))
            i = h.end()
        elif c == "\n" and pending:
            # the here-documents opened on this line: their bodies are text, up to their delimiters
            i += 1
            for word, tabs in pending:
                while True:
                    j = text.find("\n", i)
                    line = text[i:n if j < 0 else j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if tabs else line) == word:
                        break
                    if j < 0:
                        raise ValueError(f"a here-document never meets its {word}")
            pending = []
        elif _case_header(text, i):
            depth, i = depth + 1, _case_header(text, i)
        elif _word_at(text, i, "esac"):
            if depth:
                depth, i = depth - 1, i + 4
                continue
            chunks.append((start, i))
            arms = []
            for s, e in chunks:
                body = text[s:e]
                lead = re.match(r"(?:\s|#[^\n]*)*", body).end()
                if lead == len(body):
                    continue
                k = body.find(")", lead)
                if k < 0:
                    raise ValueError(f"line {line_at(text, s + lead)}: an arm with no pattern")
                pats = tuple(p.strip().strip('"') for p in body[lead:k].lstrip("(").split("|"))
                arms.append((pats, body[k + 1:], line_at(text, s + lead)))
            return arms, i + 4
        elif text.startswith(";;", i) and depth == 0:
            chunks.append((start, i))
            i += 2
            start = i
        else:
            i += 1
    raise ValueError(f"line {line_at(text, at)}: this case never meets its esac")


def sh_case_with(text, subject, needle, where, offset=0):
    """(arms, line) of the one `case <subject> in` whose arms say `needle` (a regex): the case is known by
    what it does, the way a reader finds it, whatever moves around it."""
    found = []
    for m in re.finditer(rf"(?<![\w$]){re.escape('case ' + subject + ' in')}(?![\w-])", text):
        arms = sh_case(text, m.start())[0]
        if any(re.search(needle, body) for _, body, _ in arms):
            found.append((arms, offset + line_at(text, m.start())))
    if len(found) != 1:
        raise NotFound(f"{where}: {len(found)} case {subject} in whose arms say {needle!r}, one expected")
    return floor(found[0][0], f"{where}: the arms of case {subject} in"), found[0][1]


def patterns(arms):
    """Each arm's patterns, but the catch-all and the ones that are variables ("$FLASH_REPO")."""
    return [p for pats, _, _ in arms for p in pats if p != "*" and not p.startswith("$")]


# ---------------------------------------------------------------------------------------------------
# Python: module-level assignments and the literals of named functions, read by ast.

TREES = {}


def tree_of(rel):
    if rel not in TREES:
        TREES[rel] = ast.parse(text_of(rel))
    return TREES[rel]


def py_assign(rel, name):
    """The value node of the module-level assignment of name."""
    for node in tree_of(rel).body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets) and node.value is not None:
            return node.value
    raise NotFound(f"{rel}: no module-level {name} =")


def py_function(rel, name):
    found = [n for n in ast.walk(tree_of(rel)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if len(found) != 1:
        raise NotFound(f"{rel}: {len(found)} functions {name}(), one expected")
    return found[0]


def py_find(node, what, where, test):
    """The one node under `node` of the type `what` that `test` says is it."""
    found = [n for n in ast.walk(node) if isinstance(n, what) and test(n)]
    if len(found) != 1:
        raise NotFound(f"{where}: {len(found)} found, one expected")
    return found[0]


def py_strings(rel):
    """{name: value} of the module's string constants (KOLIBRI_UNIT = "qwen38-kolibri.service")."""
    out = {}
    for node in tree_of(rel).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.setdefault(t.id, node.value.value)
    return out


def py_value(node, rel):
    """A literal of the module, its names resolved to the module's own string constants and literals (lc.X
    to lifecycle.py's): tuples, lists, sets, dicts, frozenset(...) and a + of tuples. Anything else is
    NotFound: half a table is never read."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        names = py_strings(rel)
        return names[node.id] if node.id in names else py_value(py_assign(rel, node.id), rel)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "lc":
        return py_strings("dashboard/lifecycle.py")[node.attr]
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(py_value(e, rel) for e in node.elts)
    if isinstance(node, ast.Set):
        return frozenset(py_value(e, rel) for e in node.elts)
    if isinstance(node, ast.Dict) and None not in node.keys:
        return {py_value(k, rel): py_value(v, rel) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("frozenset", "set") \
            and len(node.args) == 1:
        return frozenset(py_value(node.args[0], rel))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return py_value(node.left, rel) + py_value(node.right, rel)
    raise NotFound(f"{rel}:{getattr(node, 'lineno', '?')}: {ast.dump(node)[:80]} is not a literal this reads")


def py_run(rel, name, namespace):
    """The function named, compiled from its own source as its module compiles it (annotations not
    evaluated) and run in the namespace given: the module is never imported (the cockpit's import
    starts its own state)."""
    code = compile(ast.Module(body=[py_function(rel, name)], type_ignores=[]), str(REPO / rel), "exec",
                   flags=__future__.annotations.compiler_flag, dont_inherit=True)
    exec(code, namespace)
    return namespace[name]


def run_main(rel, argv):
    """What a script's main() prints, run in this process with its own argv: an oc-<lane>.py prints the
    provider block install.sh and switch-model.sh merge into opencode's config."""
    mod = load(f"main_of_{pathlib.Path(rel).stem.replace('-', '_')}", REPO / rel)
    out, saved = io.StringIO(), sys.argv
    sys.argv = [rel, *argv]
    try:
        with contextlib.redirect_stdout(out):
            mod.main()
    finally:
        sys.argv = saved
    return out.getvalue()


# ---------------------------------------------------------------------------------------------------
# JavaScript: the page's tables, read as values (strings, numbers, names, arrays, objects with computed
# keys), and the one arrow that maps a target to its unit.

JS_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


class JsName(str):
    """A name in a JavaScript value: a constant (KOLIBRI_UNIT), resolved by the caller."""


def _js_skip(text, i):
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i)
            if j < 0:
                raise ValueError(f"line {line_at(text, i)}: a comment never ends")
            i = j + 2
        else:
            break
    return i


def _js_at(text, i, what):
    if i >= len(text):
        raise ValueError(f"the text ends where {what} belongs")
    return text[i]


def js_value(text, i):
    """(value, end) of the JavaScript value at i. Anything but a string, a number, a name, an array or an
    object of those raises ValueError with its line."""
    i = _js_skip(text, i)
    c = _js_at(text, i, "a value")
    if c in ("'", '"'):
        j, out = i + 1, []
        while _js_at(text, j, "a string's end") != c:
            if text[j] == "\n":
                raise ValueError(f"line {line_at(text, i)}: a string runs past its line")
            if text[j] == "\\":
                out.append(_js_at(text, j + 1, "an escape"))
                j += 2
            else:
                out.append(text[j])
                j += 1
        return "".join(out), j + 1
    if c in "[{":
        close, items, keys = "]" if c == "[" else "}", [], []
        i += 1
        while True:
            i = _js_skip(text, i)
            if _js_at(text, i, f"a '{close}'") == close:
                return (items if c == "[" else dict(zip(keys, items))), i + 1
            if c == "{":
                if text[i] == "[":
                    m = JS_IDENT.match(text, _js_skip(text, i + 1))
                    k = _js_skip(text, m.end()) if m else i
                    if not m or _js_at(text, k, "a ']'") != "]":
                        raise ValueError(f"line {line_at(text, i)}: a computed key this does not read")
                    key, i = JsName(m.group(0)), k + 1
                elif text[i] in "'\"":
                    key, i = js_value(text, i)
                else:
                    m = JS_IDENT.match(text, i)
                    if not m:
                        raise ValueError(f"line {line_at(text, i)}: a key this does not read")
                    key, i = m.group(0), m.end()
                i = _js_skip(text, i)
                if _js_at(text, i, "a ':'") != ":":
                    raise ValueError(f"line {line_at(text, i)}: no ':' after the key {key!r}")
                keys.append(key)
                i += 1
            value, i = js_value(text, i)
            items.append(value)
            i = _js_skip(text, i)
            if _js_at(text, i, f"a ',' or '{close}'") == ",":
                i += 1
            elif text[i] != close:
                raise ValueError(f"line {line_at(text, i)}: {text[i:i + 20]!r} where a ',' or '{close}' belongs")
    m = re.compile(r"-?\d+(?:\.\d+)?").match(text, i)
    if m:
        return float(m.group(0)) if "." in m.group(0) else int(m.group(0)), m.end()
    m = JS_IDENT.match(text, i)
    if m and text[_js_skip(text, m.end()):][:1] not in ("(", ".", "["):
        return JsName(m.group(0)), m.end()
    raise ValueError(f"line {line_at(text, i)}: {text[i:i + 30]!r} is not a value this reads")


def js_const(rel, name):
    """(value, line) of `const NAME = <value>` in a page file."""
    text = text_of(rel)
    m = re.search(rf"\bconst {name}\s*=\s*", text)
    if not m:
        raise NotFound(f"{rel}: no const {name}")
    return js_value(text, m.end())[0], line_at(text, m.start())


def js_units(rel="dashboard/static/js/base.js"):
    """{constant: unit} of the page's unit constants (const U27 = 'qwen38-sglang.service', ...)."""
    return floor(dict(re.findall(r"\b([A-Z][A-Z0-9_]*)\s*=\s*'(qwen38-[a-z0-9-]+\.service)'", text_of(rel))),
                 f"{rel}: the units' constants", len(TURNS))


def js_resolve(value, consts, where):
    """A table or list with its names (computed keys, items) made the units they hold."""
    def one(x):
        if isinstance(x, JsName):
            if x not in consts:
                raise NotFound(f"{where}: {x} is no unit constant")
            return consts[x]
        return x
    if isinstance(value, dict):
        return {one(k): v for k, v in value.items()}
    return [one(x) for x in value]


def js_target_unit(rel="dashboard/static/js/base.js"):
    """(function, line) of TARGET_UNIT as the page runs it: its chain of t === 'x' and
    String(t).startsWith('x') tests, in their order. A test of another shape fails here, never skipped."""
    text = text_of(rel)
    m = re.search(r"\bconst TARGET_UNIT = t => (.*?);\n", text, re.S)
    if not m:
        raise NotFound(f"{rel}: no const TARGET_UNIT = t => ...;")
    parts = re.split(r"\s*\?\s*([A-Z][A-Z0-9_]*)\s*:\s*", m.group(1).strip())
    floor(parts, f"{rel}: TARGET_UNIT's tests", 3)
    rules = []
    for cond, name in zip(parts[0:-1:2], parts[1:-1:2]):
        tests = []
        for atom in cond.split("||"):
            eq = re.fullmatch(r"t === '([^']+)'", atom.strip())
            pre = re.fullmatch(r"String\(t\)\.startsWith\('([^']+)'\)", atom.strip())
            if not (eq or pre):
                raise NotFound(f"{rel}: TARGET_UNIT tests {atom.strip()!r}, which this does not read")
            tests.append(("eq", eq.group(1)) if eq else ("pre", pre.group(1)))
        rules.append((tests, name))
    default = parts[-1].strip()
    consts = js_units(rel)

    def unit_of(t):
        for tests, name in rules:
            if any(t == v if kind == "eq" else t.startswith(v) for kind, v in tests):
                return consts[name]
        return consts[default]
    return unit_of, line_at(text, m.start())


def ci_literal(step, name):
    """(node, line) of the literal `name = ...` in the embedded script of a CI step, its lines dedented."""
    ci = text_of(".github/workflows/ci.yml")
    s = ci.find(f"- name: {step}\n")
    if s < 0:
        raise NotFound(f".github/workflows/ci.yml: no step {step!r}")
    m = re.compile(rf"^([ ]*){re.escape(name)} = ", re.M).search(ci, s)
    nxt = ci.find("\n      - name: ", s + 1)
    if not m or (nxt >= 0 and m.start() > nxt):
        raise NotFound(f".github/workflows/ci.yml: no {name} = in the step {step!r}")
    # its lines, one more each time, until they make a statement: a literal ends where it closes
    lines = ci[m.start():].splitlines()
    for k in range(1, min(len(lines), 80) + 1):
        try:
            node = ast.parse(textwrap.dedent("\n".join(lines[:k]))).body[0]
        except SyntaxError:
            continue
        return node.value, line_at(ci, m.start())
    raise NotFound(f".github/workflows/ci.yml: {name} = in the step {step!r} never closes")


def finds(rel, text, pattern, flags=re.M):
    """(what re.findall finds, "file:first-last"): a list a pattern reads in a file, and where it is."""
    rx = re.compile(pattern, flags)
    ms = list(rx.finditer(text))
    lines = [line_at(text, m.start()) for m in ms]
    where = f"{rel}:{lines[0]}" + (f"-{lines[-1]}" if lines[-1] != lines[0] else "") if lines else f"{rel}:?"
    return [m.group(1) if rx.groups == 1 else m.groups() for m in ms], where


def one(rel, text, pattern, flags=re.M):
    """(the match, "file:line") of a pattern that must be in a file."""
    m = re.search(pattern, text, flags)
    if not m:
        raise NotFound(f"{rel}: no {pattern!r}")
    return m, f"{rel}:{line_at(text, m.start())}"


def py_at(rel, name):
    """(the literal value of a module-level name, "file:line")."""
    node = py_assign(rel, name)
    return py_value(node, rel), f"{rel}:{node.lineno}"


def py_line(rel, name):
    """"file:line" of a module-level name, for a module this test imports (boxmem.py, engines.py)."""
    return f"{rel}:{py_assign(rel, name).lineno}"


# ---------------------------------------------------------------------------------------------------

class TheRegistryIsWellFormed(unittest.TestCase):
    def test_each_lane_has_every_field_and_values_of_their_kind(self):
        fields = {"unit", "installer", "flags", "kind", "engine", "b12x", "targets", "heavy_64", "served",
                  "provider", "ports", "fits_64", "title"}
        self.assertGreaterEqual(len(LANES), 10)
        for lane, f in LANES.items():
            with self.subTest(lane):
                self.assertEqual(set(f), fields)
                self.assertIn(f["kind"], ("text", "image", "video", "speech"))
                self.assertIn(f["engine"], ("sglang", "vllm", "diffusion", "own"))
                self.assertTrue(set(f["heavy_64"]) <= set(f["targets"]))
                self.assertTrue(f["fits_64"] or not f["heavy_64"], "a lane that does not fit has no heavy target")
                self.assertTrue(f["engine"] == "vllm" or not f["b12x"], "b12x is vLLM's")
                # a text lane listens behind the proxy, on the one engine port, under its own names
                self.assertEqual(f["ports"] == (reg.TEXT_PORT,), f["kind"] == "text")
                self.assertEqual(f["served"] is not None, f["kind"] == "text")
                self.assertEqual(f["provider"] is not None, f["kind"] == "text")
        self.assertEqual(len(TARGET_LANE), sum(len(f["targets"]) for f in LANES.values()), "a target in two lanes")
        for field in ("unit", "installer", "served", "provider", "title"):
            values = [f[field] for f in LANES.values() if f[field] not in (None, "install.sh")]
            self.assertEqual(len(values), len(set(values)), f"two lanes share a {field}")
        ports = [p for f in LANES.values() if f["ports"] != (reg.TEXT_PORT,) for p in f["ports"]]
        self.assertEqual(len(ports), len(set(ports)), "two lanes share a port of their own")
        self.assertNotIn(reg.TEXT_PORT, ports)


class ThePartialSourcesSayTheSame(unittest.TestCase):
    """The tables the repo keeps of its lanes already: boxmem.py, the units and installers on disk, the
    families test_lane_lists_whole.py reads from them, and the cockpit's table of engines."""

    def test_boxmem(self):
        p, rel = Problems(), "boxmem.py"
        p.same(f"{py_line(rel, 'LANES')} LANES", boxmem.LANES, LANES)
        if tuple(boxmem.LANES) != tuple(LANES):
            p.append(f"{py_line(rel, 'LANES')} LANES in the order {boxmem.LANES}, the registry's is {tuple(LANES)}")
        p.table(f"{py_line(rel, 'TARGETS')} TARGETS", boxmem.TARGETS, TARGET_LANE)
        if list(boxmem.TARGETS) != list(TARGET_LANE):
            p.append(f"{py_line(rel, 'TARGETS')} TARGETS in the order {list(boxmem.TARGETS)}, the registry's is {list(TARGET_LANE)}")
        p.same(f"{py_line(rel, 'FITS_64')} FITS_64", boxmem.FITS_64, family(fits_64=True))
        p.same(f"{py_line(rel, 'HEAVY_64')} HEAVY_64", boxmem.HEAVY_64, [t for f in LANES.values() for t in f["heavy_64"]])
        p.check(self)

    def test_the_units_and_installers_on_disk(self):
        p = Problems()
        templates = floor(sorted(REPO.glob("qwen38-*.service.template")), "the unit templates", len(LANES))
        units = {t.name[:-len(".template")].replace("qwen38-sglang-1m.", "qwen38-sglang.")
                 .replace("qwen38-sglang-64.", "qwen38-sglang.") for t in templates}
        # the proxy's unit is no lane: it stands in front of the text lanes
        p.same("the unit templates qwen38-*.service.template on disk", sorted(units - {"qwen38-keepalive.service"}), UNIT.values())
        installers = floor(sorted(x.name for x in REPO.glob("install-*.sh")), "install-*.sh", len(SIDE))
        p.same("the installers install-*.sh on disk", installers, [f["installer"] for f in LANES.values() if f["installer"] != "install.sh"])
        for f in LANES.values():
            if f["installer"] != "install.sh":
                unit, line = sh_assign(text_of(f["installer"]), "UNIT", f["installer"])
                if unit != f["unit"]:
                    p.append(f"{f['installer']}:{line} UNIT={unit}, the registry says {f['unit']}")
        # the unit of the two lanes install.sh installs itself, by MODEL_CHOICE: the default's, then the case's
        sh = text_of("install.sh")
        default, _ = sh_assign(sh, "MODEL_CHOICE", "install.sh")
        default = re.fullmatch(r"\$\{MODEL_CHOICE:-([a-z0-9-]+)\}", default)
        m, where = one("install.sh", sh, r'^LANE=([a-z0-9-]+); UNIT_NAME="([^"]+)"$')
        if not default:
            raise NotFound("install.sh: no MODEL_CHOICE=\"${MODEL_CHOICE:-<target>}\"")
        default = default.group(1)
        got = {default: m.groups()}
        arms, line = sh_case_with(sh, '"$MODEL_CHOICE"', r"UNIT_NAME=", "install.sh")
        for pats, body, _ in arms:
            lane = re.search(r'LANE=([a-z0-9-]+); UNIT_NAME="([^"]+)"', body)
            got.update({t: lane.groups() if lane else None for t in pats})
        p.table(f"{where}, install.sh:{line} LANE and UNIT_NAME of MODEL_CHOICE's default and of its case", got,
                {t: (TARGET_LANE[t], UNIT[TARGET_LANE[t]]) for t in [default] + targets_of(INSTALL_SH)
                 if t == default or TARGET_LANE[t] != TARGET_LANE[default]})
        p.check(self)

    def test_the_families_test_lane_lists_whole_reads_from_the_files(self):
        p, rel = Problems(), "tests/test_lane_lists_whole.py"
        p.same(f"{py_line(rel, 'ENGINE')} ENGINE, the units of the templates with Conflicts=", whole.ENGINE, units_of(TURNS))
        p.same(f"{py_line(rel, 'TEXT')} TEXT", whole.TEXT, units_of(TEXT))
        p.same(f"{py_line(rel, 'VLLM')} VLLM, the installers that source vllm-lane.sh", whole.VLLM, units_of(VLLM))
        p.same(f"{py_line(rel, 'B12X')} B12X, the vLLM installers that say b12x", whole.B12X, units_of(B12X))
        p.same(f"{py_line(rel, 'DIFFUSION')} DIFFUSION", whole.DIFFUSION, units_of(family(engine="diffusion")))
        p.check(self)

    def test_the_cockpits_table_of_engines(self):
        p, rel = Problems(), "dashboard/engines.py"
        where = f"{py_line(rel, 'ENGINES')} ENGINES"
        # every engine the cockpit names, the speech lane's (its own server) not among them
        p.same(where, engines.ENGINES, [UNIT[x] for x in LANES if LANES[x]["engine"] != "own"])
        label = {"sglang": "SGLang", "vllm": "vLLM", "diffusion": "SGLang Diffusion"}
        for unit, e in engines.ENGINES.items():
            f = LANES.get(LANE_OF_UNIT.get(unit))
            if f and e["engine"] != label.get(f["engine"]):
                p.append(f"{where}[{unit!r}] engine {e['engine']!r}, the registry says {f['engine']}")
            if f and ("b12x" in e["version"]) != f["b12x"]:
                p.append(f"{where}[{unit!r}] version {e['version']!r}, the registry says b12x {f['b12x']}")
        p.check(self)


class InstallShSaysTheSame(unittest.TestCase):
    """install.sh: its flags, its tables of lanes and of targets, its blocks per side lane, its opencode."""

    SH = "install.sh"

    def setUp(self):
        self.text = text_of(self.SH)

    def test_its_flags(self):
        p, sh, rel = Problems(), self.text, self.SH
        arms, line = sh_case_with(sh, '"$arg"', r"WITH_[A-Z0-9_]+=1", rel)
        for word in ("with", "no"):
            got = [x[len(f"--{word}-"):] for x in patterns(arms) if x.startswith(f"--{word}-")]
            p.same(f"{rel}:{line} the case of its arguments, --{word}-<lane>", [g for g in got if g not in NOT_LANES], FLAGGED)
        # and each sets its lane's variable, the one the rest of the run reads
        for pats, body, n in arms:
            for flag in pats:
                m = re.fullmatch(r"--(with|no)-([a-z0-9-]+)", flag)
                if m and m.group(2) in LANES and body.strip() != f"{m.group(1).upper()}_{VAR[m.group(2)]}=1":
                    p.append(f"{rel}:{n} {flag}) {body.strip()}, the registry says {m.group(1).upper()}_{VAR[m.group(2)]}=1")
        for word in ("WITH", "NO"):
            got, where = finds(rel, sh, rf"^{word}_([A-Z0-9_]+)=0$")
            p.same(f"{where} its {word}_<LANE>=0", floor([LANE_OF_VAR.get(v, v) for v in got if v.lower() not in NOT_LANES],
                                                       f"{rel} {word}_<LANE>=0"), FLAGGED)
        # its help, the here-document its -h|--help arm prints, alone, its lines still the file's
        m, _ = one(rel, sh, r"cat <<'HLP'\n(.*?)\nHLP\n", re.S)
        helps = re.sub(r"[^\n]", " ", sh[:m.start(1)]) + m.group(1)
        usage, where = one(rel, helps, r"^Usage: \./install\.sh (.*?)\n\n", re.M | re.S)
        pairs = re.findall(r"\[--no-([a-z0-9-]+) \| --with-([a-z0-9-]+)\]", usage.group(1))
        p.same(f"{where} its help's Usage", [a for a, b in pairs if a == b and a not in NOT_LANES], FLAGGED)
        for word in ("no", "with"):
            got, where = finds(rel, helps, rf"^  --{word}-([a-z0-9-]+) ")
            p.same(f"{where} its help's --{word}-<lane> lines", [g for g in got if g not in NOT_LANES], FLAGGED)
        got, where = finds(rel, sh, r"--no-([a-z0-9-]+) and --with-\1 contradict each other")
        p.same(f"{where} its checks that --no-<lane> and --with-<lane> contradict each other", [g for g in got if g not in NOT_LANES], FLAGGED)
        # a side lane asked for by name needs the full install: a unit, and a proof that it serves
        got, where = finds(rel, sh, r"--with-([a-z0-9-]+) needs the full install")
        p.same(f"{where} its refusals of --with-<lane> without the full install", got, SIDE)
        calls, where = finds(rel, sh, r'^\s*lane_wanted ([a-z0-9-]+) "\$NO_([A-Z0-9_]+)" "\$WITH_([A-Z0-9_]+)"; WANT_([A-Z0-9_]+)="\$WANT"$')
        p.same(f"{where} its lane_wanted calls", [c[0] for c in calls], FLAGGED)
        for c in calls:
            if not c[1] == c[2] == c[3] == VAR.get(c[0]):
                p.append(f"{where} lane_wanted {c[0]}: NO_{c[1]}, WITH_{c[2]}, WANT_{c[3]}, the registry says {VAR.get(c[0])}")
        m, where = one(rel, sh, r"^WANT_[A-Z0-9_]+=0(?:; WANT_[A-Z0-9_]+=0)*$")
        p.same(f"{where} its WANT_<LANE>=0", [LANE_OF_VAR.get(v, v) for v in re.findall(r"WANT_([A-Z0-9_]+)=0", m.group(0))], FLAGGED)
        p.check(self)

    def test_its_tables_of_the_lanes(self):
        p, sh, rel = Problems(), self.text, self.SH
        # the unit of each lane by the variable that holds its path (KOLIBRI_UNIT_PATH)
        paths = dict(re.findall(r'^([A-Z0-9_]+_UNIT_PATH)="/etc/systemd/system/([^"]+)"$', sh, re.M))
        for fn, want in (("lane_title", FLAGGED), ("side_begun", SIDE)):
            line, body = sh_function(sh, fn, rel)
            arms, at = sh_case_with(body, '"$1"', r".", f"{rel} {fn}()", line - 1)
            p.same(f"{rel}:{at} {fn}()", patterns(arms), want)
        # the side lanes' installs, in two copies: the path of a box that boots one, and the text path
        blocks = floor(list(re.finditer(r'^(\s*)if \[ "\$WANT_([A-Z0-9_]+)" -eq 1 \]; then\n(.*?)^\1fi\b', sh, re.M | re.S)),
                       f"{rel} its if [ \"$WANT_<LANE>\" -eq 1 ] blocks", 2 * len(SIDE))
        copies = {}
        for b in blocks:
            lane = LANE_OF_VAR.get(b.group(2), b.group(2))
            inst = re.findall(r'"\$REPO_DIR/(install-[a-z0-9-]+\.sh)"', b.group(3))
            copies.setdefault(len(b.group(1)), []).append((lane, line_at(sh, b.start())))
            if inst != [LANES[lane]["installer"] if lane in LANES else None]:
                p.append(f"{rel}:{line_at(sh, b.start())} WANT_{b.group(2)} runs {inst}, the registry says "
                         f"{LANES[lane]['installer'] if lane in LANES else 'no such lane'}")
        floor(copies, f"{rel} the copies of the side lanes' installs", 2)
        for lanes in copies.values():
            p.same(f"{rel}:{lanes[0][1]}-{lanes[-1][1]} the side lanes' installs", [x for x, _ in lanes], SIDE)
        got, where = finds(rel, sh, r"^([A-Z0-9_]+)_BOOT=0$")
        p.same(f"{where} its <LANE>_BOOT=0", [LANE_OF_VAR.get(v, v) for v in got], SIDE)
        m, where = one(rel, sh, r"^SIDE_BOOT=\$\(\((.*)\)\)$")
        p.same(f"{where} SIDE_BOOT", [LANE_OF_VAR.get(v, v) for v in re.findall(r"([A-Z0-9_]+)_BOOT", m.group(1))], SIDE)
        m, where = one(rel, sh, r'^if \[ -z "\$ATS_CANNOT" \] (?:&& \[ "\$[A-Z0-9_]+_BOOT" -eq 0 \] ?)+; then$')
        p.same(f"{where} the text path's [ \"$<LANE>_BOOT\" -eq 0 ]",
               [LANE_OF_VAR.get(v, v) for v in re.findall(r'"\$([A-Z0-9_]+)_BOOT"', m.group(0))], SIDE)
        got, where = finds(rel, sh, r'^\s*\[ "\$([A-Z0-9_]+)_BOOT" -eq 1 \] && SERVING_LANE="[^"]*"$')
        p.same(f"{where} its SERVING_LANE of each side lane", [LANE_OF_VAR.get(v, v) for v in got], SIDE)
        # the text path's lines of the side lanes, one variable each: new, what its install did, to prove
        for name, pattern in (("<LANE>_FRESH=0", r"([A-Z0-9_]+)_FRESH=0"), ("<LANE>_STATE=off", r"([A-Z0-9_]+)_STATE=off"),
                              ("PROVE_<LANE>=0", r"PROVE_([A-Z0-9_]+)=0")):
            m, where = one(rel, sh, rf"^\s*{pattern}(?:; {pattern})+$")
            got = [LANE_OF_VAR.get(v, v) for v in re.findall(pattern, m.group(0))]
            p.same(f"{where} its {name}", [x for x in got if x != "SECOND"], SIDE)
        probes, where = finds(rel, sh, r'^\s*\{ \[ -f "?([^" ]+)"? \] && side_begun ([a-z0-9-]+); \} \|\| ([A-Z0-9_]+)_FRESH=1$')
        p.same(f"{where} its side_begun probes", [lane for _, lane, _ in probes], SIDE)
        for path, lane, var in probes:
            unit = paths.get(path.lstrip("$").strip("{}"), path.rsplit("/", 1)[-1])
            if lane in LANES and (unit != UNIT[lane] or LANE_OF_VAR.get(var) != lane):
                p.append(f"{where} side_begun {lane}: probes {unit}, sets {var}_FRESH, the registry says {UNIT[lane]}, {VAR[lane]}")
        # its summary, a line per lane: its flag, a target of its own, its unit
        lines, where = finds(rel, sh, r'^\s*lane_line "[^"]*" "\$[A-Z0-9_]+" ([a-z0-9-]+) ([a-z0-9-]+) "\d+ GB" (\S+)$')
        p.same(f"{where} its lane_line calls, their flags", [x[0] for x in floor(lines, f"{rel} lane_line", len(FLAGGED))], FLAGGED)
        for flag, target, unit in lines:
            unit = paths.get(unit.strip('"').lstrip("$").strip("{}"), unit.rsplit("/", 1)[-1])
            if flag in LANES and (TARGET_LANE.get(target) != flag or unit != UNIT[flag]):
                p.append(f"{where} lane_line {flag} {target} {unit}: {target} is a target of {TARGET_LANE.get(target)}, "
                         f"{flag}'s unit is {UNIT[flag]}")
        p.check(self)

    def test_its_targets(self):
        p, sh, rel = Problems(), self.text, self.SH
        arms, line = sh_case_with(sh, '"$MODEL_CHOICE"', r"MODEL_REPO=", rel)
        p.same(f"{rel}:{line} the case of MODEL_CHOICE", patterns(arms), targets_of(INSTALL_SH))
        m, where = one(rel, sh, r"ERROR: MODEL_CHOICE must be (.*?) \(got")
        p.same(f"{where} its MODEL_CHOICE error", re.findall(r'"([a-z0-9-]+)"', m.group(1)), targets_of(INSTALL_SH))
        line, body = sh_function(sh, "resolve_flash_checkpoint_args", rel)
        arms, at = sh_case_with(body, '"$MODEL_CHOICE"', r"FLASH_QUANT_ARGS=", rel, line - 1)
        p.same(f"{rel}:{at} resolve_flash_checkpoint_args()", patterns(arms), targets_of(["flash"]))
        p.check(self)

    def test_its_opencode_providers(self):
        p, sh, rel = Problems(), self.text, self.SH
        # the two providers its generator writes itself, then each text lane's provider behind its OC_<LANE>
        own, where = finds(rel, sh, r'^\s*providers\["([a-z0-9-]+)"\] = prov\("[^"]*", "([^"]+)"')
        p.table(f"{where} providers[...] = prov(...) (provider: served)", dict(own), {LANES[x]["provider"]: LANES[x]["served"] for x in INSTALL_SH})
        gates, where = finds(rel, sh, r'^if os\.environ(?:\.get\("OC_([A-Z0-9_]+)"\)|\["OC_([A-Z0-9_]+)"\]) == "1":\n((?:[ #][^\n]*\n)*?)'
                                      r'\s*providers\["([a-z0-9-]+)"\] =')
        got = {LANE_OF_VAR.get(a or b, a or b): prov for a, b, _, prov in floor(gates, f"{rel} OC_<LANE>", len(TEXT))}
        p.table(f"{where} if os.environ[\"OC_<LANE>\"]: providers[...] (lane: provider)", got, {x: LANES[x]["provider"] for x in TEXT})
        defaults, where = finds(rel, sh, r'^if os\.environ\.get\("OC_([A-Z0-9_]+)_BOOT"\) == "1" and "([a-z0-9-]+)" in providers:\n'
                                         r'\s*default = "([a-z0-9-]+/[^"]+)"')
        p.table(f"{where} opencode's default on a box that boots a side lane", {LANE_OF_VAR.get(v, v): (pr, md) for v, pr, md in defaults},
                {x: (LANES[x]["provider"], f"{LANES[x]['provider']}/{LANES[x]['served']}") for x in among(SIDE, TEXT)})
        m, where = one(rel, sh, r"grep -qsE '\"\(([a-z0-9|-]+)\)\"' \"\$OC_USER_CFG\"")
        p.same(f"{where} its grep of this repo's providers", m.group(1).split("|"), [LANES[x]["provider"] for x in TEXT])
        p.check(self)


class TheSwitchSaysTheSame(unittest.TestCase):
    SH = "switch-model.sh"

    def setUp(self):
        self.text = text_of(self.SH)

    def test_its_targets(self):
        p, sh, rel = Problems(), self.text, self.SH
        arms, line = sh_case_with(sh, '"$CHOICE"', r"printf 'usage: \./switch-model\.sh <", rel)
        p.same(f"{rel}:{line} its guard", patterns(arms), TARGET_LANE)
        usage = re.search(r"usage: \./switch-model\.sh <([a-z0-9|-]+)>", "".join(body for _, body, _ in arms))
        p.same(f"{rel}:{line} its usage", usage.group(1).split("|") if usage else [], TARGET_LANE)
        # a block per side lane, or per family of them, each with whole lanes
        blocks = floor(list(re.finditer(r'^if \[ "\$CHOICE" = "[a-z0-9-]+" \][^\n]*; then\n.*?^fi\b', sh, re.M | re.S)),
                       f"{rel} its if [ \"$CHOICE\" = ... ] blocks", 2)
        seen = []
        for b in blocks:
            ts = re.findall(r'"\$CHOICE" = "([a-z0-9-]+)"', b.group(0).split("\n", 1)[0])
            lanes = [x for x in LANES if x in {TARGET_LANE.get(t) for t in ts}]
            seen += ts
            where = f"{rel}:{line_at(sh, b.start())} the block for {ts}"
            if sorted(ts) != sorted(targets_of(lanes)):
                p.append(f"{where}: its lanes {lanes} have the targets {targets_of(lanes)}")
            # the name each lane serves under, said in its own block and in no other
            for lane, f in LANES.items():
                said = f["served"] and re.search(rf"(?<![\w.-]){re.escape(f['served'])}(?![\w-]|\.\w)", b.group(0))
                if said and lane not in lanes:
                    p.append(f"{where} names {f['served']}, {lane}'s")
                if not said and f["served"] and lane in lanes:
                    p.append(f"{where} never names {f['served']}, the name its lane serves under")
        p.same(f"{rel}:{line_at(sh, blocks[0].start())}-{line_at(sh, blocks[-1].start())} the targets of its blocks", seen, targets_of(SIDE))
        # the lane each target of install.sh's lanes switches to
        arms, line = sh_case_with(sh, '"$CHOICE"', r"TARGET_LANE=", rel)
        got = {}
        for pats, body, _ in arms:
            m = re.search(r"TARGET_LANE=([a-z0-9-]+)", body)
            got.update({t: m.group(1) if m else None for t in pats})
        p.table(f"{rel}:{line} the case that sets TARGET_LANE", got, {t: TARGET_LANE[t] for t in targets_of(INSTALL_SH)})
        arms, line = sh_case_with(sh, '"$CHOICE"', r"Switch back:", rel)
        p.same(f"{rel}:{line} its Switch back hints", patterns(arms), targets_of(INSTALL_SH))
        p.check(self)

    def test_its_units_names_and_providers(self):
        p, sh, rel = Problems(), self.text, self.SH
        for lane in SIDE:
            for var, want in ((f"{VAR[lane]}_UNIT_NAME", UNIT[lane]), (f"{VAR[lane]}_UNIT", f"/etc/systemd/system/{UNIT[lane]}")):
                got, line = sh_assign(sh, var, rel)
                if got != want:
                    p.append(f"{rel}:{line} {var}={got}, the registry says {want}")
        names, where = finds(rel, sh, r'^([A-Z0-9_]+)_UNIT_NAME="qwen38-[a-z0-9-]+\.service"$')
        p.same(f"{where} its <LANE>_UNIT_NAME", [LANE_OF_VAR.get(v, v) for v in names], SIDE)
        # the Nemotron lanes' block: each lane's installer, name and served model, by its branch
        m, where = one(rel, sh, r'^if \[ "\$CHOICE" = "nemotron" \] \|\| \[ "\$CHOICE" = "nemotron-super" \]; then\n(.*?)^fi\b', re.M | re.S)
        got = {}
        for branch in re.findall(r"NEM_INSTALLER=(\S+); NEM_PIN=\S+; NEM_TITLE=\"([^\"]+)\"\n\s*NEM_SERVED=(\S+);", m.group(1)):
            got[next((x for x, f in LANES.items() if f["installer"] == branch[0]), branch[0])] = branch[1:]
        p.table(f"{where} NEM_INSTALLER, NEM_TITLE and NEM_SERVED", got,
                {x: (LANES[x]["title"], LANES[x]["served"]) for x in LANES if x in {TARGET_LANE["nemotron"], TARGET_LANE["nemotron-super"]}})
        m, where = one(rel, sh, r'if \[ "\$TARGET_LANE" = "([a-z0-9-]+)" \]; then SW_PROV=(\S+); SW_MODEL=(\S+)\n'
                                r'\s*else SW_PROV=(\S+); SW_MODEL=(\S+); fi')
        other = [x for x in INSTALL_SH if x != m.group(1)]
        p.table(f"{where} SW_PROV and SW_MODEL", {m.group(1): m.group(2, 3), "the other": m.group(4, 5)},
                {m.group(1): (LANES.get(m.group(1), {}).get("provider"), LANES.get(m.group(1), {}).get("served")),
                 "the other": (LANES[other[0]]["provider"], LANES[other[0]]["served"]) if len(other) == 1 else other})
        p.check(self)


class TheOtherScriptsSayTheSame(unittest.TestCase):
    def test_prepare_lane_and_uninstall(self):
        p, rel = Problems(), "prepare-lane.sh"
        sh = text_of(rel)
        m, where = one(rel, sh, r"^usage\(\)\{ echo \"usage: \./prepare-lane\.sh <([a-z0-9|-]+)> \| <([a-z0-9|-]+)> \[<target>\]\"")
        p.same(f"{where} usage()", m.group(1).split("|"), SIDE)
        p.same(f"{where} usage(), the lanes with a target", m.group(2).split("|"), INSTALL_SH)
        arms, line = sh_case_with(sh, '"${1:-}"', r"usage", rel)
        p.same(f"{rel}:{line} the case of its lane", patterns(arms), SIDE + INSTALL_SH)
        arms, line = sh_case_with(sh, '"$1"', r'install-\$1\.sh" --prepare', rel)
        p.same(f"{rel}:{line} the case that prepares each lane", patterns(arms), SIDE + INSTALL_SH)
        for lane in INSTALL_SH:
            line, body = sh_function(sh, f"prepare_{lane}", rel)
            arms, at = sh_case_with(body, '"${TARGET:-$model}"', r"target=", rel, line - 1)
            p.same(f"{rel}:{at} prepare_{lane}()'s targets", patterns(arms), targets_of([lane]))
        rel = "uninstall.sh"
        sh = text_of(rel)
        m, where = one(rel, sh, r"^for u in (qwen38-[^;]*); do$")
        units = [u for u in m.group(1).split() if u not in ("qwen38-keepalive.service", "qwen38-dashboard.service", "opencode-web.service")]
        p.same(f"{where} its inventory of units", units, UNIT.values())
        m, where = one(rel, sh, r"^\s*for _lane in ([a-z0-9 -]+); do\n\s*\[ -f \"\$CONFIG_DIR/\$_lane\.off\" \]")
        p.same(f"{where} the lanes a --no-<lane> marker names", m.group(1).split(), FLAGGED)
        p.check(self)

    def test_the_units_names_and_the_proxy(self):
        p, rel = Problems(), "qwen38-keepalive.service.template"
        ka = text_of(rel)
        m, where = one(rel, ka, r"^After=(.*)$")
        p.same(f"{where} After=", [u for u in m.group(1).split() if u.startswith("qwen38-")], units_of(TEXT))
        m, where = one(rel, ka, r'^Environment="PROXY_HOLD_UNITS=([^"]*)"$')
        p.same(f"{where} PROXY_HOLD_UNITS", m.group(1).split(), units_of(TEXT))
        # each text lane serves under its name, on the engine port, and EACH of its templates says both once (the
        # 27B has three, and two of them hid a third that had lost its name: found in review, 2026-11-10). A unit
        # that runs its lane's launcher says neither: the launcher does.
        launched = {t.name.replace("-launch.sh.template", ".service.template") for t in REPO.glob("qwen38-*-launch.sh.template")}
        says = []
        for t in floor(sorted(REPO.glob("qwen38-*.template")), "the templates qwen38-*.template", len(LANES)):
            unit = ("qwen38-flash.service" if t.name == "qwen38-flash-launch.sh.template" else
                    t.name[:-len(".template")].replace("qwen38-sglang-1m.", "qwen38-sglang.").replace("qwen38-sglang-64.", "qwen38-sglang."))
            if unit not in LANE_OF_UNIT:
                continue
            lane, body = LANE_OF_UNIT[unit], t.read_text()
            f = LANES[lane]
            served, at = finds(t.name, body, r"--served-model-name (\S+)")
            ports, at_port = finds(t.name, body, r"--port ([^\s'\"]+)")
            if f["kind"] != "text":
                if served or "__PORT__" in ports:
                    p.append(f"{t.name}: {lane} serves no text, and says {served} {ports}")
            elif t.name in launched:
                if served or ports:
                    p.append(f"{t.name}: its launcher says the name and the port, and this unit says {served} {ports} too")
            else:
                says.append(lane)
                if served != [f["served"]]:
                    p.append(f"{at} --served-model-name {served}, the registry says {f['served']}, once")
                if ports != ["__PORT__"]:
                    p.append(f"{at_port} --port {ports}: a text lane listens on the engine port, __PORT__, said once")
        p.same("the text lanes with a template that says a name and a port", sorted(set(says)), sorted(TEXT))
        m, where = one("run.sh", text_of("run.sh"), r"--served-model-name (\S+)")
        p.same(f"{where} --served-model-name (the 27B in the foreground)", [m.group(1)], [LANES["27b"]["served"]])
        for lane in VLLM:
            f = LANES[lane]
            for var, want in (("VLANE", lane), ("VLANE_SERVED", f["served"])):
                got, line = sh_assign(text_of(f["installer"]), var, f["installer"])
                if got != want:
                    p.append(f"{f['installer']}:{line} {var}={got}, the registry says {want}")
        proxy = "keepalive-proxy.py"
        got, where = py_at(proxy, "LANE_NAMES")
        p.same(f"{where} LANE_NAMES", got, [LANES[x]["served"] for x in TEXT])
        # the flash model's names, the flash lane's and its FP4 build's: the names that begin with the flash lane's
        flash = [f["served"] for f in LANES.values() if f["served"] and f["served"].startswith(LANES["flash"]["served"])]
        for rel in (proxy, "oc-fit-limits.py"):
            got, where = py_at(rel, "FLASH_SERVED_NAMES")
            p.same(f"{where} FLASH_SERVED_NAMES", got, flash)
        # the lanes the proxy asks systemd about by their unit, to say the box serves images or video
        for kind in ("image", "video"):
            got, where = py_at(proxy, f"{kind.upper()}_UNIT")
            p.same(f"{where} {kind.upper()}_UNIT", [got], units_of(family(kind=kind)))
        # "The Qwen lanes vLLM serves (v6.43)", named one by one
        got, where = py_at(proxy, "QWEN_VLLM_NAMES")
        p.same(f"{where} QWEN_VLLM_NAMES", got, [LANES[x]["served"] for x in VLLM if LANES[x]["title"].startswith("Qwen")])
        p.check(self)

    def test_opencode_and_pi(self):
        p = Problems()
        providers = [LANES[x]["provider"] for x in TEXT]
        for rel in ("pi-gen.py", "oc-merge-limits.py"):
            got, where = py_at(rel, "OURS")
            p.same(f"{where} OURS", got, providers)
        # each vLLM lane's provider block, as its oc-<lane>.py prints it for install.sh and the switch
        got, scripts = {}, floor([s for s in sorted(REPO.glob("oc-*.py")) if "\ndef provider(" in s.read_text()], "oc-<lane>.py")
        for script in scripts:
            lanes = py_at(script.name, "LANES")[0] if re.search(r"^LANES = ", script.read_text(), re.M) else [None]
            for lane in lanes:
                out = json.loads(run_main(script.name, ["http://127.0.0.1:1/v1", "/k", "1000", "100", "n"] + ([lane] if lane else [])))
                for prov, block in out["provider"].items():
                    got[prov] = list(block["models"])
        p.table(f"{', '.join(s.name for s in scripts)}: the provider each prints, and its model", got,
                {LANES[x]["provider"]: [LANES[x]["served"]] for x in VLLM})
        rel = "oc-point-default.py"
        got, where = py_at(rel, "LABEL")
        p.same(f"{where} LABEL", got, targets_of(TEXT))
        want = py_find(py_function(rel, "main"), ast.Call, f"{rel}: {{lane: provider/model}}.get(lane, default) in main()",
                       lambda n: isinstance(n.func, ast.Attribute) and n.func.attr == "get" and isinstance(n.func.value, ast.Dict))
        table = dict(py_value(want.func.value, rel))
        rest = [x for x in TEXT if x not in table]
        table.update({x: py_value(want.args[1], rel) for x in rest[:1]})      # the default, the one lane it leaves out
        p.table(f"{rel}:{want.lineno} the default it points opencode at (lane: provider/model)", table,
                {x: f"{LANES[x]['provider']}/{LANES[x]['served']}" for x in TEXT})
        m = re.search(r"<lane: ([a-z0-9|-]+)>", ast.get_docstring(tree_of(rel)) or "")
        p.same(f"{rel}:3 its usage's lanes", m.group(1).split("|") if m else [], TEXT)
        # the pool fit and the recipes are SGLang's: the two text lanes install.sh serves
        got, where = py_at("oc-fit-limits.py", "LANE_MODEL")
        p.table(f"{where} LANE_MODEL (served: provider), the SGLang lanes", got, {LANES[x]["served"]: LANES[x]["provider"] for x in SGLANG})
        got, where = py_at("dashboard/recipes.py", "LANES")
        p.same(f"{where} LANES, recipes are SGLang's", got, SGLANG)
        got, where = py_at("dashboard/recipes.py", "BUILTIN_IDS")
        p.same(f"{where} BUILTIN_IDS", got, targets_of(SGLANG))
        # oc-limits.sh's pair per target: every text target, and custom (the 27B's kept model)
        arms, line = sh_case_with(text_of("oc-limits.sh"), '"$CHOICE"', r"unknown target", "oc-limits.sh")
        p.same(f"oc-limits.sh:{line} its case of targets", [t for t in patterns(arms) if t != "custom"], targets_of(TEXT))
        # bench.sh's lanes with a measured reference, by the name they serve under ("other" for the rest, on
        # purpose, its own comment says why): each pair it names is right
        m, where = one("bench.sh", text_of("bench.sh"), r'^LANE = (\{.*?\})\.get\(MODEL, "other"\)$', re.M | re.S)
        bench = floor(ast.literal_eval(m.group(1)), "bench.sh LANE", 2)
        p.table(f"{where} LANE (served: lane), the lanes with a reference", bench, {LANES[x]["served"]: x for x in TEXT if LANES[x]["served"] in bench})
        p.check(self)

    def test_the_ports(self):
        p = Problems()
        m, where = one("install.sh", text_of("install.sh"), r'^PORT="\$\{PORT:-(\d+)\}"$')
        p.same(f"{where} PORT, the engine port", [int(m.group(1))], [reg.TEXT_PORT])
        line, body = sh_function(text_of("vllm-lane.sh"), "vlane_engine_env", "vllm-lane.sh")
        m, where = one("vllm-lane.sh", body, r'PORT="\$\{PORT:-(\d+)\}"')
        p.same(f"vllm-lane.sh:{line + int(where.split(':')[1]) - 1} vlane_engine_env()'s PORT", [int(m.group(1))], [reg.TEXT_PORT])
        for lane, f in LANES.items():
            if f["ports"] == (reg.TEXT_PORT,):
                continue
            rel, src = f["installer"], text_of(f["installer"])
            m, where = one(rel, src, r'^PORT="\$\{[A-Z]+_PORT:-\$\([a-z_]+ --port\)\}"; PORT="\$\{PORT:-(\d+)\}"$')
            p.same(f"{where} its PORT", [int(m.group(1))], f["ports"][:1])
            got, where = finds(rel, src, r"^ASR_PORT=\$\(\(PORT \+ (\d+)\)\)$")
            p.same(f"{where} its ASR_PORT", [f["ports"][0] + int(x) for x in got], f["ports"][1:])
        m, where = one("speech-server.py", text_of("speech-server.py"),
                       r'add_argument\("--port", type=int, default=(\d+)\)\n\s*ap\.add_argument\("--asr-url", default="http://127\.0\.0\.1:(\d+)"\)')
        p.same(f"{where} its --port and --asr-url", [int(x) for x in m.groups()], LANES["speech"]["ports"])
        cockpit = text_of("dashboard/cockpit.py")
        for kind, fn in (("image", "image_port"), ("video", "video_port")):
            node = py_function("dashboard/cockpit.py", fn)
            got = sorted({int(x) for x in re.findall(r"\b(\d{4,5})\b", ast.get_source_segment(cockpit, node) or "")})
            p.same(f"dashboard/cockpit.py:{node.lineno} {fn}()", floor(got, f"dashboard/cockpit.py {fn}()"), LANES[family(kind=kind)[0]]["ports"])
        for rel, pattern, kind in (("dashboard/static/js/image.js", r"\bconst IS = \{[^;]*?\bport: (\d+)", "image"),
                                   ("dashboard/static/js/image.js", r"\bIS\.port = d\.port \|\| (\d+);", "image"),
                                   ("dashboard/static/js/video.js", r"\bconst VS = \{[^;]*?\bport: (\d+)", "video"),
                                   ("dashboard/static/js/video.js", r"\bVS\.port = d\.port \|\| (\d+);", "video"),
                                   ("dashboard/static/js/now.js", r"\bconst VID_NOW = \{[^;]*?\bport: (\d+)", "video"),
                                   ("dashboard/static/js/now.js", r"\bVID_NOW\.port \|\| (\d+)", "video"),
                                   ("dashboard/static/js/now.js", r"'Listens on', '127\.0\.0\.1:(\d+), behind this cockpit'", "image")):
            m, where = one(rel, text_of(rel), pattern)
            p.same(f"{where} {pattern}", [int(m.group(1))], LANES[family(kind=kind)[0]]["ports"][:1])
        # the offline suite's guard: no test may connect to a port the box serves on
        rel = ".github/workflows/ci.yml"
        m, where = one(rel, text_of(rel), r"QWEN38_SERVING_PORTS=([0-9,]+)")
        guarded = {int(x) for x in m.group(1).split(",")}
        for lane, f in LANES.items():
            lacks = [x for x in f["ports"] if x not in guarded]
            if lacks:
                p.drift((rel, "QWEN38_SERVING_PORTS", lane), f"lacks {', '.join(map(str, lacks))}",
                        f"{where} QWEN38_SERVING_PORTS lacks {lacks}, the {lane} lane's")
        p.check(self, excuses(rel, "QWEN38_SERVING_PORTS"))

    def test_the_64_gb_guards(self):
        p = Problems()
        # every installer of its own refuses a 64 GB box for a lane that does not fit, and only for it
        for lane, f in LANES.items():
            if f["installer"] != "install.sh":
                guards, where = finds(f["installer"], text_of(f["installer"]), r"\blane_fits ([a-z0-9-]+) \|\| die ")
                p.same(f"{where} its lane_fits guard", guards, [] if f["fits_64"] else [lane])
        p.check(self)


class TheCockpitSaysTheSame(unittest.TestCase):
    PY = "dashboard/cockpit.py"
    JS = "dashboard/static/js/base.js"

    def test_its_python(self):
        p, rel = Problems(), self.PY
        got, where = py_at(rel, "UNIT_OF_LANE")
        p.table(f"{where} UNIT_OF_LANE", got, {x: UNIT[x] for x in TURNS})
        for fn in ("collect_lanes_ready", "collect_lanes_startable"):
            comp = py_find(py_function(rel, fn), ast.DictComp, f"{rel}: {fn}()'s {{lane: ... for lane in (...)}}", lambda n: True)
            p.same(f"{rel}:{comp.lineno} {fn}()", py_value(comp.generators[0].iter, rel), TURNS)
        d = py_find(py_function(rel, "lane_prepared"), ast.Subscript, f"{rel}: lane_prepared()'s {{lane: unit}}[lane]",
                    lambda n: isinstance(n.value, ast.Dict))
        p.same(f"{rel}:{d.lineno} lane_prepared()'s units", [k.value for k in d.value.keys], TURNS)
        switch = py_find(py_assign(rel, "ACTIONS"), ast.Dict, f"{rel}: ACTIONS[\"switch\"][\"params\"]",
                         lambda n: [getattr(k, "value", None) for k in n.keys] == ["target"])
        p.same(f"{rel}:{switch.lineno} ACTIONS[\"switch\"]'s targets", py_value(switch, rel)["target"], TARGET_LANE)
        got, where = py_at(rel, "TARGET_PIN")
        p.same(f"{where} TARGET_PIN", got, TARGET_LANE)
        # the pins of the targets install.sh serves itself, which the cockpit reads from its unit
        got, where = py_at(rel, "VAR2TARGET")
        p.same(f"{where} VAR2TARGET (its targets)", got.values(), targets_of(INSTALL_SH))
        target_lane = py_run(rel, "target_lane", {})
        p.table(f"{rel}:{py_function(rel, 'target_lane').lineno} target_lane()", {t: target_lane(t) for t in TARGET_LANE}, TARGET_LANE)
        # target_unit() run on the unit each of its names holds: X_UNIT_PATH = Path("/etc/systemd/system/<unit>"),
        # UNIT_PATHS by its keys
        names = {"UNIT_PATHS": {k.value: k.value for k in py_assign(rel, "UNIT_PATHS").keys}}
        for node in tree_of(rel).body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "Path" \
                    and node.value.args and isinstance(node.value.args[0], ast.Constant) and isinstance(node.targets[0], ast.Name):
                names[node.targets[0].id] = str(node.value.args[0].value).rsplit("/", 1)[-1]
        target_unit = py_run(rel, "target_unit", names)
        p.table(f"{rel}:{py_function(rel, 'target_unit').lineno} target_unit()", {t: target_unit(t) for t in TARGET_LANE},
                {t: UNIT[x] for t, x in TARGET_LANE.items()})
        got, where = py_at(rel, "SERVING_UNITS")
        p.same(f"{where} SERVING_UNITS, its lanes", [u for u in got if u.startswith("qwen38-") and u != "qwen38-keepalive.service"], units_of(TURNS))
        got, where = py_at(rel, "NEMOTRON_LANES")
        p.table(f"{where} NEMOTRON_LANES (unit: container, lane, served), the b12x lanes", {u: v[:3] for u, v in got.items()},
                {UNIT[x]: (UNIT[x][:-len(".service")], x, LANES[x]["served"]) for x in B12X})
        names, where = py_at(rel, "B12X_LANE_NAME")
        p.same(f"{where} B12X_LANE_NAME, its lanes", [LANE_OF_UNIT.get(u, u) for u in names], B12X)
        for unit, said in names.items():
            lane = LANE_OF_UNIT.get(unit)
            if lane and said != f"the {LANES[lane]['title']} lane":
                p.drift((rel, "B12X_LANE_NAME", lane), said, f"{where} B12X_LANE_NAME[{unit!r}] {said!r}, "
                                                             f"the registry says 'the {LANES[lane]['title']} lane'")
        # its opencode panel: this repo's providers, and the provider of each name an engine serves under
        fn = py_function(rel, "collect_opencode")
        provs = py_find(fn, ast.Compare, f"{rel}: collect_opencode()'s `prov not in (...)`",
                        lambda n: isinstance(n.ops[0], ast.NotIn) and isinstance(n.left, ast.Name) and n.left.id == "prov")
        p.same(f"{rel}:{provs.lineno} collect_opencode()'s providers", py_value(provs.comparators[0], rel), [LANES[x]["provider"] for x in TEXT])
        served = py_find(fn, ast.Call, f"{rel}: collect_opencode()'s {{served: provider}}.get(served)",
                         lambda n: isinstance(n.func, ast.Attribute) and n.func.attr == "get" and isinstance(n.func.value, ast.Dict))
        p.table(f"{rel}:{served.lineno} collect_opencode() (served: provider)", py_value(served.func.value, rel),
                {LANES[x]["served"]: LANES[x]["provider"] for x in TEXT})
        got, where = py_at("dashboard/lifecycle.py", "LANE_PROVIDER")
        p.table(f"{where} LANE_PROVIDER", got, {UNIT[x]: LANES[x]["provider"] for x in TEXT})
        p.check(self, excuses(rel, "B12X_LANE_NAME"))

    def test_its_page(self):
        p, rel = Problems(), self.JS
        consts = js_units(rel)
        p.same(f"{rel} its units' constants (const U27 = ..., ...)", set(consts.values()) - {"qwen38-keepalive.service"}, units_of(TURNS))
        value, line = js_const(rel, "LANE_UNITS")
        p.same(f"{rel}:{line} LANE_UNITS", js_resolve(value, consts, rel), units_of(TURNS))
        value, line = js_const(rel, "LANE_NAME")
        p.table(f"{rel}:{line} LANE_NAME", js_resolve(value, consts, rel), {UNIT[x]: LANES[x]["title"] for x in TURNS})
        value, line = js_const(rel, "LANE_META")
        p.table(f"{rel}:{line} LANE_META (title, lane)", {u: (m.get("title"), m.get("lane")) for u, m in js_resolve(value, consts, rel).items()},
                {UNIT[x]: (LANES[x]["title"], x) for x in TURNS})
        value, line = js_const(rel, "SIDE_LANE")
        p.table(f"{rel}:{line} SIDE_LANE", js_resolve(value, consts, rel), {UNIT[x]: x for x in TURNS})
        value, line = js_const(rel, "READY_DEFAULT")
        p.same(f"{rel}:{line} READY_DEFAULT", js_resolve(value, consts, rel), units_of(TURNS))
        value, line = js_const(rel, "LANE_TARGETS")
        p.table(f"{rel}:{line} LANE_TARGETS", {u: sorted(t) for u, t in js_resolve(value, consts, rel).items()},
                {UNIT[x]: sorted(LANES[x]["targets"]) for x in TURNS})
        value, line = js_const(rel, "LANE_INSTALL")
        p.table(f"{rel}:{line} LANE_INSTALL", js_resolve(value, consts, rel), {UNIT[x]: f"./install.sh --with-{x}" for x in TURNS})
        value, line = js_const(rel, "LANE_RUNTIME")
        p.same(f"{rel}:{line} LANE_RUNTIME", value, TURNS)
        for name in ("TARGET_NAME", "TARGET_SHORT_NAME", "TARGET_LINE", "TARGET_NOTE"):
            value, line = js_const(rel, name)
            p.same(f"{rel}:{line} {name}", value, TARGET_LANE)
        unit_of, line = js_target_unit(rel)
        p.table(f"{rel}:{line} TARGET_UNIT", {t: unit_of(t) for t in TARGET_LANE}, {t: UNIT[x] for t, x in TARGET_LANE.items()})
        # the b12x lanes, as the page tells them: isB12x, the two Nemotron lanes by isNemotron and the others
        text = text_of(rel)
        m, where = one(rel, text, r"^const isNemotron = u => (u === [A-Z0-9_]+(?: \|\| u === [A-Z0-9_]+)*);$")
        nemotron = [consts.get(c, c) for c in re.findall(r"u === ([A-Z0-9_]+)", m.group(1))]
        m, where = one(rel, text, r"^const isB12x = u => isNemotron\(u\)((?: \|\| u === [A-Z0-9_]+)*);$")
        p.same(f"{where} isB12x (with isNemotron's)", nemotron + [consts.get(c, c) for c in re.findall(r"u === ([A-Z0-9_]+)", m.group(1))],
               units_of(B12X))
        # the b12x lanes in a sentence: b12xName's chain of names, the last for the lanes it does not test
        m, where = one(rel, text, r"^const b12xName = u => (.*);$")
        said = {consts[c]: s for c, s in re.findall(r"u === ([A-Z0-9_]+) \? '([^']+)'", m.group(1))}
        rest = re.search(r":\s*'([^']+)'$", m.group(1))
        said.update({u: rest.group(1) if rest else None for u in units_of(B12X) if u not in said})
        for unit, s in said.items():
            lane = LANE_OF_UNIT.get(unit)
            if lane and s != f"the {LANES[lane]['title']} lane":
                p.drift((rel, "b12xName", lane), s, f"{where} b12xName({unit}) {s!r}, the registry says 'the {LANES[lane]['title']} lane'")
        # the sources of the Logs view: the containers of the lanes that run in one (SGLang's and vLLM's,
        # not the venvs of the diffusion lanes), and the journals of every lane's unit, each by its name
        html = "dashboard/static/index.html"
        for group, what, lanes in (("Serving containers", "container", [x for x in TURNS if LANES[x]["engine"] in ("sglang", "vllm")]),
                                   ("Unit journals", "journal", TURNS)):
            m, where = one(html, text_of(html), rf'<optgroup label="{group}">(.*?)</optgroup>')
            options = dict(re.findall(r'<option value="([^"]+)">([^<]*)</option>', m.group(1)))
            want = {UNIT[x][:-len(".service")] if what == "container" else UNIT[x]: f"{LANES[x]['title']} {what}" for x in lanes}
            p.table(f"{where} {group}", {v: label for v, label in options.items() if v != "qwen38-keepalive.service"}, want)
        css = "dashboard/static/css/cockpit.css"
        got, where = finds(css, text_of(css), r"--lane-([a-z0-9-]+):")
        p.same(f"{where} its --lane-<lane> hues", floor(got, f"{css} --lane-<lane>"), TURNS)
        p.check(self, excuses(rel, "b12xName"))

    def test_its_sudoers(self):
        p, rel = Problems(), "dashboard/sudoers-cockpit.template"
        text = text_of(rel)
        for verb in ("start", "stop", "restart", "enable", "disable"):
            units, where = finds(rel, text, rf"^__USER__ ALL=\(root\) NOPASSWD: /usr/bin/systemctl {verb} (qwen38-[a-z0-9-]+\.service)$")
            p.same(f"{where} systemctl {verb}", [u for u in floor(units, f"{rel} systemctl {verb}") if u != "qwen38-keepalive.service"],
                   units_of(TURNS))
        p.check(self)


class TheCiAndTheTestsSayTheSame(unittest.TestCase):
    """The lists the CI and the other tests keep of the lanes to name what they check."""

    def test_the_ci(self):
        p, ci = Problems(), ".github/workflows/ci.yml"
        node, line = ci_literal("One list of targets, in all four places that must agree", "FLAG_LANES")
        p.same(f"{ci}:{line} FLAG_LANES (the targets that are no MODEL_CHOICE)", ast.literal_eval(node),
               [t for t in TARGET_LANE if TARGET_LANE[t] not in INSTALL_SH])
        step = "Every privileged call of switch-model.sh is in the cockpit allowlist"
        node, line = ci_literal(step, "units")
        p.same(f"{ci}:{line} units (the units of install.sh's lanes)", ast.literal_eval(node), units_of(INSTALL_SH))
        node, line = ci_literal(step, "engines")
        if not (isinstance(node, ast.BinOp) and isinstance(node.left, ast.Name) and node.left.id == "units" and isinstance(node.right, ast.Set)):
            raise NotFound(f"{ci}:{line} engines is not units | {{...}}")
        p.same(f"{ci}:{line} engines", units_of(INSTALL_SH) + list(ast.literal_eval(node.right)), units_of(TURNS))
        node, line = ci_literal(step, "values")
        got = {LANE_OF_VAR.get(k.value[:-len("_UNIT_NAME")], k.value): sorted(ast.literal_eval(v)) for k, v in zip(node.keys, node.values)
               if isinstance(k, ast.Constant) and k.value.endswith("_UNIT_NAME") and isinstance(v, ast.Set)}
        p.table(f"{ci}:{line} values[\"<LANE>_UNIT_NAME\"]", got, {x: [UNIT[x]] for x in SIDE})
        p.check(self)

    def test_the_other_tests(self):
        p, rel = Problems(), "tests/test_lane_lists_whole.py"
        p.table(f"{rel}:{py_assign(rel, 'CHOICE_LANE').lineno} CHOICE_LANE", whole.CHOICE_LANE, {t: UNIT[x] for t, x in TARGET_LANE.items() if x in SIDE})
        vllm = py_find(py_function(rel, "test_the_lanes_and_their_families_are_found"), ast.Call, f"{rel}: assertEqual(VLLM, {{...}})",
                       lambda n: getattr(n.func, "attr", "") == "assertEqual" and isinstance(n.args[0], ast.Name) and n.args[0].id == "VLLM")
        p.same(f"{rel}:{vllm.lineno} its VLLM", ast.literal_eval(vllm.args[1]), units_of(VLLM))
        need = py_find(py_function(rel, "test_the_cockpits_python"), ast.Call, f"{rel}: assertLessEqual({{...constants}}, ...)",
                       lambda n: getattr(n.func, "attr", "") == "assertLessEqual" and isinstance(n.args[0], ast.Set))
        held = {**py_strings("dashboard/lifecycle.py"), **py_strings("dashboard/cockpit.py")}
        named = [LANE_OF_UNIT.get(held.get(c), c) for c in ast.literal_eval(need.args[0])]
        p.same(f"{rel}:{need.lineno} the cockpit's lane constants it requires, their lanes", [x for x in named if x not in LANES], [])
        for lane in SIDE:
            if lane not in named:
                p.drift((rel, "the cockpit's lane constants it requires", lane), "absent",
                        f"{rel}:{need.lineno} the cockpit's lane constants it requires lack {lane}'s")
        rel = "tests/test_spark_64.py"
        got, where = py_at(rel, "SIDE")
        p.table(f"{where} SIDE (the installer of each side lane a 64 GB box does not hold)", got,
                {LANES[x]["installer"]: x for x in among(SIDE, NOT_64)})
        loop = py_find(py_function(rel, "test_prepare_lane_says_a_side_lane_needs_128_gb_not_an_install"), ast.For,
                       f"{rel}: for lane in (...)", lambda n: isinstance(n.target, ast.Name) and n.target.id == "lane")
        p.same(f"{rel}:{loop.lineno} for lane in (...)", ast.literal_eval(loop.iter), among(SIDE, NOT_64))
        rel = "tests/test_boxmem.py"
        for fn, want in (("test_the_64_gb_class_takes_the_27b_bolt_and_speech_only", family(fits_64=True)),
                         ("test_a_64_gb_box_takes_the_27b_in_nvfp4_only",
                          [t for t, x in TARGET_LANE.items() if LANES[x]["fits_64"] and t not in LANES[x]["heavy_64"]])):
            lit = py_find(py_function(rel, fn), ast.Call, f"{rel}: {fn}'s assertEqual(got, {{...}})",
                          lambda n: getattr(n.func, "attr", "") == "assertEqual" and isinstance(n.args[1], ast.Set))
            p.same(f"{rel}:{lit.lineno} {fn}", ast.literal_eval(lit.args[1]), want)
        got, where = py_at("tests/test_one_engine_units.py", "LANES")
        p.same(f"{where} LANES", got, units_of(TURNS))
        rel = "tests/lane-templates-check.py"
        got, where = py_at(rel, "VLLM_LANES")
        p.table(f"{where} VLLM_LANES (target: installer)", {t: v[0] for t, v in got.items()},
                {t: LANES[x]["installer"] for t, x in TARGET_LANE.items() if x in VLLM})
        got, where = py_at(rel, "NOT_TEXT")
        p.same(f"{where} NOT_TEXT", got, [t for t, x in TARGET_LANE.items() if x not in TEXT])
        rel = "tests/test_lane_templates_check.py"
        lit = py_find(py_function(rel, "test_every_text_lane_with_how_its_installer_serves_its_template"), ast.Call,
                      f"{rel}: sorted([...the text targets])",
                      lambda n: getattr(n.func, "id", "") == "sorted" and n.args and isinstance(n.args[0], ast.List))
        p.same(f"{rel}:{lit.lineno} the text targets", ast.literal_eval(lit.args[0]), targets_of(TEXT))
        rel = "tests/test_docs_facts.py"
        lit = py_find(py_function(rel, "test_the_help_gives_each_lane_one_size"), ast.Call, f"{rel}: assertEqual(set(said), {{...}})",
                      lambda n: getattr(n.func, "attr", "") == "assertEqual" and isinstance(n.args[1], ast.Set))
        p.same(f"{rel}:{lit.lineno} the side lanes of install.sh's help", ast.literal_eval(lit.args[1]), SIDE)
        rel = "tests/test_proxy_hold.py"
        copy = py_find(py_function(rel, "test_the_installed_proxy_names_every_text_lane"), ast.JoinedStr,
                       f"{rel}: its f-string of Environment=\"PROXY_HOLD_UNITS=...\"",
                       lambda n: any(isinstance(v, ast.Constant) and "PROXY_HOLD_UNITS=" in v.value for v in n.values))
        said = []
        for v in copy.values:
            if isinstance(v, ast.Constant):
                said += whole.UNIT.findall(v.value)
            elif isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name):
                said += whole.UNIT.findall(py_value(v.value, rel))
            else:
                raise NotFound(f"{rel}:{copy.lineno}: a part of its copy of PROXY_HOLD_UNITS this does not read")
        p.same(f"{rel}:{copy.lineno} its copy of PROXY_HOLD_UNITS", said, units_of(TEXT))
        p.check(self, excuses("tests/test_lane_lists_whole.py"))


class TheNamesAreTheOfficialOnes(unittest.TestCase):
    """A lane's name in install.sh's messages is its official one (decision 1180), or for the image and
    video lanes their kind, the way install.sh names them ("the image lane"), their model after it: in
    lane_title, SERVING_LANE, the notes and the steps of the side lanes' installs, the 64 GB guards, and
    in every message that names a lane by a proper name. The help, the comments, and the other scripts'
    messages are not read here."""

    SH = "install.sh"

    def said(self, lane, name):
        f = LANES[lane]
        if f["kind"] in ("image", "video"):
            return name in (f["title"], f["kind"], f"{f['kind']} ({f['title']})")
        return name == f["title"]

    def test_install_sh(self):
        p, sh, rel = Problems(), text_of(self.SH), self.SH
        line, body = sh_function(sh, "lane_title", rel)
        arms, _ = sh_case_with(body, '"$1"', r".", f"{rel} lane_title()", line - 1)
        for pats, arm, n in arms:
            m = re.search(r'echo "([^"]*)"', arm)
            for lane in pats:
                if lane in LANES and not (m and self.said(lane, m.group(1))):
                    p.append(f"{rel}:{line + n - 1} lane_title {lane}: {m.group(1) if m else None!r}, the registry says {LANES[lane]['title']!r}")
        for m in floor(list(re.finditer(r'^\s*\[ "\$([A-Z0-9_]+)_BOOT" -eq 1 \] && SERVING_LANE="([^"]*)"$', sh, re.M)),
                       f"{rel} SERVING_LANE", len(SIDE)):
            lane = LANE_OF_VAR.get(m.group(1))
            if lane and not self.said(lane, m.group(2)):
                p.append(f"{rel}:{line_at(sh, m.start())} SERVING_LANE of {lane}: {m.group(2)!r}, the registry says {LANES[lane]['title']!r}")
        # each side lane's install, in its two copies, and what it says when the lane did not install
        notes = {}
        for b in re.finditer(r'^(\s*)if \[ "\$WANT_([A-Z0-9_]+)" -eq 1 \]; then\n(.*?)^\1fi\b', sh, re.M | re.S):
            m = re.search(r"NOTE: the (.+?) lane did not install", b.group(3))
            if LANE_OF_VAR.get(b.group(2)):
                notes.setdefault(LANE_OF_VAR[b.group(2)], []).append((m.group(1) if m else None,
                                                                      line_at(sh, b.start(3) + m.start()) if m else line_at(sh, b.start())))
        floor(notes, f"{rel} NOTE: the <name> lane did not install", len(SIDE))
        # and the step that starts each install on the text path: step "<name> lane (what it does)"
        steps = {}
        for b in re.finditer(r'^(\s*)if \[ "\$WANT_([A-Z0-9_]+)" -eq 1 \]; then\n\s*step "(.+?) lane \(', sh, re.M):
            steps[LANE_OF_VAR.get(b.group(2), b.group(2))] = (b.group(3), line_at(sh, b.end()))
        p.same(f"{rel} the steps of the side lanes' installs, their lanes", floor(steps, f"{rel} step \"<name> lane (\"", len(SIDE)), SIDE)
        for lane, (name, n) in steps.items():
            if lane in LANES and name != LANES[lane]["title"]:
                p.append(f"{rel}:{n} step \"{name} lane (...)\", the registry says {LANES[lane]['title']!r}")
        for lane, said in notes.items():
            names = [s for s, _ in said]
            if not all(s and self.said(lane, s) for s in names):
                p.drift((rel, "NOTE: the <name> lane did not install", lane), names,
                        f"{rel}:{','.join(str(n) for _, n in said)} NOTE: the <name> lane did not install, {lane}: {names}, "
                        f"the registry says {LANES[lane]['title']!r}")
        # and every message of install.sh that names a lane by a proper name ("the Kolibri-1 lane") names it by
        # its official one: fifteen of them still said the short names (found in review, 2026-11-10). The 27B and
        # the image and video lanes are named by what they are, in lower case or by a figure, which this leaves.
        titles = {f["title"] for f in LANES.values() if f["title"]}
        named = []
        for m in re.finditer(r'^[ \t]*(?:\|\| )?(?:echo|step|die|printf)\b.*$', sh, re.M):
            for n in re.finditer(r"\b[Tt]he ([A-Z][\w.-]*(?: [A-Z0-9][\w.-]*)*) lanes?\b", m.group(0)):
                named.append(n.group(1))
                if n.group(1) not in titles:
                    p.append(f"{rel}:{line_at(sh, m.start())} says \"the {n.group(1)} lane\", which is no lane's official name")
        floor(named, f"{rel} its messages that name a lane by a proper name", 2 * len(SIDE))
        # the 64 GB guard of each installer of its own names its lane
        for lane, f in LANES.items():
            if f["installer"] == rel or f["fits_64"]:
                continue
            m, where = one(f["installer"], text_of(f["installer"]),
                           rf'lane_fits {re.escape(lane)} \|\| die "The (.+?) lane(?: \((.+?)\))? needs a 128 GB DGX Spark')
            name = m.group(1) + (f" ({m.group(2)})" if m.group(2) else "")
            if not self.said(lane, name):
                p.append(f"{where} its 64 GB guard names {name!r}, the registry says {f['title']!r}")
        p.check(self, excuses(rel))


class TheReadersReadWhatTheyClaim(unittest.TestCase):
    """The readers above, on what would make them read the wrong thing."""

    def test_a_case_reads_past_what_hides_its_words(self):
        sh = ("case \"$a\" in\n"
              "  -h|--help)\n    cat <<'HLP'\nit's ;; a case in esac ) help\nHLP\n    ;;\n"
              "  x) echo \"$(printf '%s' \"in ;; esac\")\" ;; # it's a comment ;; esac\n"
              "  y) case \"$b\" in z) : ;; esac ;;\n"
              "  u) case \"$(flag '--x y')\" in 8) T=1 ;; *) T=2 ;; esac ;;\n"
              "  w) cat <<< 'esac' ;; v) echo one ;;\n"
              "esac\nafter\n")
        arms, end = sh_case(sh, 0)
        self.assertEqual([a[0] for a in arms], [("-h", "--help"), ("x",), ("y",), ("u",), ("w",), ("v",)])
        self.assertEqual(sh[end:], "\nafter\n")

    def test_a_case_it_cannot_follow_fails_rather_than_passes(self):
        for sh in ("case x in a) echo 'never ;; esac\n", "case x in a) cat <<EOF\n;; esac\n", "case x in a) : ;;\n"):
            with self.subTest(sh):
                with self.assertRaises(ValueError):
                    sh_case(sh, 0)

    def test_a_case_is_found_by_what_it_does_and_only_one(self):
        sh = 'case "$c" in a) X=1 ;; esac\ncase "$c" in b) Y=1 ;; esac\n'
        self.assertEqual(patterns(sh_case_with(sh, '"$c"', r"Y=", "t")[0]), ["b"])
        for needle in (r"=1", r"Z="):
            with self.subTest(needle), self.assertRaises(NotFound):
                sh_case_with(sh, '"$c"', needle, "t")

    def test_a_javascript_table_is_read_whole_or_not_at_all(self):
        js = "const T = {[U27]: 'a', b: [1, \"c's\"], 'd-e': {f: X}, // note\n};"
        self.assertEqual(js_value(js, js.index("{"))[0], {"U27": "a", "b": [1, "c's"], "d-e": {"f": "X"}})
        for bad in ("{a: f(x)}", "{a: 'b'", "{a 'b'}", "[1, 2", "{a: b.c}"):
            with self.subTest(bad), self.assertRaises(ValueError):
                js_value(bad, 0)

    def test_python_literals_resolve_their_names_or_fail(self):
        TREES["<test>"] = ast.parse("K = 'qwen38-kolibri.service'\nT = {K: ('a', 'b')}\nC = f(K)\n")
        try:
            self.assertEqual(py_value(py_assign("<test>", "T"), "<test>"), {"qwen38-kolibri.service": ("a", "b")})
            with self.assertRaises(NotFound):
                py_value(py_assign("<test>", "C"), "<test>")
            with self.assertRaises(NotFound):
                py_assign("<test>", "MISSING")
        finally:
            del TREES["<test>"]

    def test_a_failure_says_the_file_the_line_what_is_missing_and_what_is_extra(self):
        rows, where = finds("f.sh", "a\nX=1\nb\nX=2\n", r"^X=(\d)$")
        self.assertEqual((rows, where), (["1", "2"], "f.sh:2-4"))
        p = Problems()
        p.same(where, ["1", "3"], ["1", "2"])
        self.assertEqual(list(p), ["f.sh:2-4: missing ['2'], extra ['3']"])

    def test_an_excuse_holds_one_value_and_an_unused_one_fails(self):
        key = next(iter(TO_DECIDE))
        p = Problems()
        p.drift(key, TO_DECIDE[key], "excused")
        p.drift(key, "another value", "not excused")
        self.assertEqual(list(p), ["not excused"])
        with self.assertRaises(AssertionError):
            Problems().check(self, [key])


if __name__ == "__main__":
    unittest.main()
