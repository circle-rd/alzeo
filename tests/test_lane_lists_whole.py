#!/usr/bin/env python3
"""Every list of the lanes names them all.

Nine engine lanes share the box, one at a time, and each script that stops, starts, lists or
removes them carries its own list of them: some thirty lists in a dozen files, shell, Python and the
page's JavaScript. A lane added and forgotten in one of them was a box left with no engine: the image
and video installers' lists did not name the eighth lane (found in review, 2026-10-20). The lanes are
the unit templates that name the others in Conflicts=, and every list must name, exactly, all of
them, all but its own lane, the text lanes, the vLLM lanes, or one of five narrower families, each
read from what it is about: the lanes that keep a lane-before link, the units conformity.py reads,
the lanes whose unit mounts the Hugging Face cache, the lanes that are not SGLang's text lanes, and
the vLLM lanes that run b12x. A list names a lane by its unit, with or without ".service", or by the constant that holds it
(FLASH_LOCAL_UNIT, lc.KOLIBRI_UNIT): read by units alone, the lists made of names were never checked
(found in review, 2026-10-20). A tenth lane added to the templates and missed in one list fails here,
with the file and the line, and so does a list that lost a lane, unless what it kept is itself a
family: a list of the other lanes in the image installer that lost the video lane is the text
lanes, which the same file lists for good (found in review, 2026-10-20). Telling those apart takes
binding each list to its family, one by one, which this does by name for the vLLM and b12x lists."""
import ast
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
UNIT = re.compile(r"qwen38-[a-z0-9-]+\.service")
# a unit named without ".service" (a shell loop adds it): not a file, an image tag, a path or a longer name
BARE = re.compile(r"\bqwen38-[a-z0-9-]+(?![a-z0-9.:/-])")
# the lines whose bare names are no lanes: the repositories of the images this repo built or tagged,
# named like the lanes that built them (uninstall.sh's inventory)
NOT_LANES = re.compile(r"^\s*LOCAL_IMAGE_REPOS=")
# the two lanes that serve images and video rather than text
DIFFUSION = {"qwen38-image.service", "qwen38-video.service"}


def engine_lanes() -> set:
    """The units of the templates that conflict with the others (the 1M and the 64 GB templates are
    the 27B's unit)."""
    lanes = set()
    for t in REPO.glob("qwen38-*.service.template"):
        if re.search(r"^Conflicts=", t.read_text(), re.M):
            lanes.add(t.name[:-len(".template")].replace("qwen38-sglang-1m.", "qwen38-sglang.").replace("qwen38-sglang-64.", "qwen38-sglang."))
    return lanes


ENGINE = engine_lanes()
TEXT = ENGINE - DIFFUSION
# the lanes whose installer stands on vllm-lane.sh
VLLM = {f"qwen38-{p.name[len('install-'):-len('.sh')]}.service" for p in REPO.glob("install-*.sh")
        if re.search(r"^\s*(\.|source)\s+\S*vllm-lane\.sh", p.read_text(), re.M)}
# the lanes a switch away from them leaves a lane-before link for (lanes.sh walks those links)
LANE_BEFORE = {f"qwen38-{x}.service" for p in REPO.glob("*.sh") for x in re.findall(r"lane-before-([a-z0-9-]+)\b", p.read_text())}
# the units conformity.py reads to say whether a box is in its recommended state
CONFORMITY = set(UNIT.findall((REPO / "conformity.py").read_text())) & ENGINE
# the lanes that are not SGLang's text lanes: the cockpit's canary, pool guard and autoheal leave them out
NOT_SGLANG = DIFFUSION | VLLM
# the vLLM lanes whose engine runs b12x, as their installers say: the cockpit's table of that family
B12X = {u for u in VLLM if "b12x" in (REPO / f"install-{u[len('qwen38-'):-len('.service')]}.sh").read_text()}
# the lanes whose unit mounts the Hugging Face cache (the flash lane's launcher mounts it for its unit)
HF_UNITS = {("qwen38-flash.service" if t.name == "qwen38-flash-launch.sh.template"
             else t.name[:-len(".template")].replace("qwen38-sglang-1m.", "qwen38-sglang.").replace("qwen38-sglang-64.", "qwen38-sglang."))
            for t in REPO.glob("qwen38-*.template")
            if re.search(r"-v __HF_CACHE__:/root/\.cache/huggingface\b", t.read_text())}
# switch-model.sh's targets, by the lane each block switches to
CHOICE_LANE = {"kolibri": "qwen38-kolibri.service", "kolibri-nvfp4": "qwen38-kolibri.service",
               "nemotron": "qwen38-nemotron.service", "nemotron-super": "qwen38-nemotron-super.service",
               "flash-local": "qwen38-flash-local.service", "ornith-122b": "qwen38-ornith-122b.service",
               "image": "qwen38-image.service",
               "image-turbo": "qwen38-image.service", "video": "qwen38-video.service"}


# The two families one lane apart (the vLLM lanes less Kolibri are the b12x lanes) are told by the
# name of the list: a list of the vLLM lanes that lost Kolibri passed as the b12x family, and the
# Traffic view would have stopped reading Kolibri with no test failing (found in review, 2026-10-20).
# A list named for one of them must be that family whatever its size (a b12x table of three that lost
# one was two, below the check's threshold), and the b12x family is taken from a list so named only:
# the cockpit's NEMOTRON_LANES is that family's table, named before FP4-Local joined it.
NAMED_FAMILIES = (("b12x", "B12X"), ("nemotron_lanes", "B12X"), ("vllm", "VLLM"))


def family_of(name):
    """The family a list's name says it holds (B12X_SPEC, VLLM_UNITS), or None."""
    for key, fam in NAMED_FAMILIES:
        if name and key in name.lower():
            return globals()[fam]
    return None


def whole(named: set, own: set, name=None) -> bool:
    """A list is whole when it names all the lanes, all but one of its own, the text lanes (all but
    one of its own among them), the vLLM lanes, or one of the narrower families whole; a list named
    for a family is that family."""
    named = named & ENGINE
    fam = family_of(name)
    if fam is not None:
        return named == fam
    if named in (ENGINE, TEXT, VLLM, NOT_SGLANG, LANE_BEFORE, CONFORMITY, HF_UNITS):
        return True
    return any(named == ENGINE - {o} or named == TEXT - {o} for o in own)


def missing(named: set, name=None, own=frozenset()) -> list:
    """What a list that is not whole misses: of its family when its name says one, else of the
    smallest family it falls short of (the vLLM lanes for one that lost Kolibri, not all nine),
    all but its own lane counted among them: a list of the other lanes that lost one named its
    own lane too, which it must leave out (found in review, 2026-10-20)."""
    named = named & ENGINE
    fam = family_of(name)
    if fam is None:
        fams = [ENGINE, TEXT, VLLM, NOT_SGLANG, LANE_BEFORE, CONFORMITY, HF_UNITS]
        fams += [ENGINE - {o} for o in own] + [TEXT - {o} for o in own]
        fam = min((f for f in fams if named <= f), key=len, default=ENGINE)
    return sorted(u[7:-8] for u in fam - named) or [f"no {sorted(u[7:-8] for u in named - fam)}"]


def assigned_names(tree) -> dict:
    """id of each literal of a module -> the name it is assigned to (VLLM_UNITS = (...)), through a
    call that wraps it too (B12X = frozenset({...}))."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(t, (ast.Name, ast.Attribute)):
                    name = t.id if isinstance(t, ast.Name) else t.attr
                    out[id(node.value)] = name
                    if isinstance(node.value, ast.Call) and node.value.args:
                        out[id(node.value.args[0])] = name
    return out


def checked(named: set, name=None) -> bool:
    """Whether a list is one of the lanes: three lanes or more, or any list named for a family."""
    return len(named & ENGINE) >= 3 or (family_of(name) is not None and bool(named & ENGINE))


def shell_files():
    return sorted(p for p in REPO.glob("*.sh") if p.is_file())


# words after which a slash starts a regex literal, not a division
JS_REGEX_AFTER = {"return", "typeof", "case", "in", "of", "new", "delete", "void", "throw", "else", "do",
                  "instanceof", "yield", "await"}
JS_PUNCT = sorted(("===", "!==", "**=", "...", "=>", "==", "!=", "<=", ">=", "&&", "||", "??", "?.", "++", "--",
                   "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<", ">>", "**"), key=len, reverse=True)


def js_literals(text: str):
    """(line, names, name) of every array and object literal of a JavaScript file: what it holds at its
    own level (identifiers and strings, an object's keys, computed ones too: {[KOLIBRI_UNIT]: ...}),
    and the name it is assigned to (const SIDE_LANE = {...}), or None. A regex over one pair of
    brackets read each [CONST] of such an object as a list of one, so the page's tables of lanes were
    never checked (found in review, 2026-10-20). Strings, comments, regex literals and template
    literals are read as JavaScript reads them, the code inside a template's ${...} included: a regex
    with a quote in it, inside a template's ${...}, made a first version take the rest of image.js for
    a string, and its last 265 lines went unchecked (found in review, 2026-10-20). A text this cannot
    follow to its end, every bracket closed, raises ValueError with the line where it lost its place,
    so it can never pass a file it did not read. Two limits: an array right after a closing
    parenthesis is read as an index (f(x)[0]), so one that opens a statement after an if (...) is not
    a list here, and none does in the page (compared with acorn on its ten files, 2026-10-20); a name
    reaches a literal written right after its "=", or passed to a callee there (new Set([...]))."""
    out, stack = [], []
    i, n = 0, len(text)
    prev = ""            # the last token: "id", "num", "str", a punctuator, or a word a regex may follow
    last_id = assigned = None
    # an "=" followed only by a callee (new Set, Object.freeze): the literal passed to it takes the name
    callee = False
    IDENT = re.compile(r"[A-Za-z_$][\w$]*")

    def line_at(k):
        return text.count("\n", 0, k) + 1

    def lost(k, why):
        raise ValueError(f"line {line_at(k)}: {why}")

    def into_template(k):
        """From inside a template literal at k: past its closing backtick (True), or past the ${
        that opens an expression, which is pushed (False)."""
        while k < n:
            if text[k] == "\\":
                k += 2
            elif text[k] == "`":
                return k + 1, True
            elif text.startswith("${", k):
                stack.append({"open": "${", "start": k, "literal": False, "names": set(), "name": None})
                return k + 2, False
            else:
                k += 1
        lost(k, "a template literal never ends")

    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                lost(i, "a comment never ends")
            i = j + 2
        elif c in "'\"":
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\n":
                    lost(i, "a string runs past its line")
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                lost(i, "a string never ends")
            if stack and stack[-1]["literal"]:
                stack[-1]["names"].add(text[i + 1:j])
            prev, i = "str", j + 1
        elif c == "`":
            i, ended = into_template(i + 1)
            prev = "str" if ended else "("
        elif c == "/" and prev not in ("id", "num", "str", ")", "]", "}"):
            j, in_class = i + 1, False           # a regex literal, and its flags
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "\n":
                    lost(i, "a regex runs past its line")
                if text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                elif text[j] == "/" and not in_class:
                    break
                j += 1
            j += 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            prev, i = "str", j
        elif IDENT.match(text, i):
            m = IDENT.match(text, i)
            word = m.group(0)
            if stack and stack[-1]["literal"]:
                stack[-1]["names"].add(word)
            last_id = word
            prev = word if word in JS_REGEX_AFTER else "id"
            # a word keeps a callee after an "=" (new Set, Object.freeze): callee is left as it is
            i = m.end()
        elif c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "._"):
                j += 1
            prev, i = "num", j
        elif c in "([{":
            name = assigned if prev == "=" else None
            if c != "(" and prev == "(" and stack and stack[-1]["open"] == "(":
                name = stack[-1]["name"]
            if c == "(":
                literal = False
                name = assigned if callee else None
            elif c == "[":                       # an index follows a value, a literal does not
                literal = prev not in ("id", "num", "str", ")", "]", "}", "?.")
            else:                                # an object follows an operator, a block follows the rest
                literal = prev in ("=", ":", "(", ",", "[", "?", "||", "&&", "??", "...", "return")
            stack.append({"open": c, "start": i, "literal": literal, "names": set(), "name": name})
            callee = False
            prev, i = c, i + 1
        elif c in ")]}":
            if not stack:
                lost(i, f"a {c} closes nothing")
            frame = stack.pop()
            if c == "}" and frame["open"] == "${":
                i, ended = into_template(i + 1)
                prev = "str" if ended else "("
                continue
            if frame["open"] != {")": "(", "]": "[", "}": "{"}[c]:
                lost(i, f"a {c} closes the {frame['open']} of line {line_at(frame['start'])}")
            if frame["literal"]:
                out.append((line_at(frame["start"]), frame["names"], frame["name"]))
            # a computed key, [CONST]: of the object around it
            k = i + 1
            while k < n and text[k] in " \t":
                k += 1
            if c == "]" and k < n and text[k] == ":" and stack and stack[-1]["open"] == "{" and stack[-1]["literal"]:
                stack[-1]["names"] |= frame["names"]
            # a block's brace ends no value: a slash after it starts a regex
            prev, i = (c if c != "}" or frame["literal"] else ";"), i + 1
        else:
            p = next((p for p in JS_PUNCT if text.startswith(p, i)), c)
            if p == "=":
                assigned = last_id
            callee = p == "=" or (callee and p == ".")
            prev, i = p, i + len(p)
    if stack:
        lost(stack[-1]["start"], f"the {stack[-1]['open']} here never closes")
    return out


class TheLanesAreWhatTheTemplatesSay(unittest.TestCase):
    def test_the_lanes_and_their_families_are_found(self):
        self.assertGreaterEqual(len(ENGINE), 9, ENGINE)
        for family in (LANE_BEFORE, CONFORMITY, HF_UNITS):
            self.assertGreaterEqual(len(family), 3, family)
            self.assertTrue(family < ENGINE, family)
        self.assertTrue(DIFFUSION <= ENGINE)
        self.assertEqual(VLLM, {"qwen38-kolibri.service", "qwen38-nemotron.service",
                                "qwen38-nemotron-super.service", "qwen38-flash-local.service",
                                "qwen38-ornith-122b.service"})
        self.assertEqual(B12X, VLLM - {"qwen38-kolibri.service"})

    def test_each_lane_conflicts_with_every_other_and_starts_after_them(self):
        for t in sorted(REPO.glob("qwen38-*.service.template")):
            text = t.read_text()
            m = re.search(r"^Conflicts=(.*)$", text, re.M)
            if not m:
                continue
            own = t.name[:-len(".template")].replace("qwen38-sglang-1m.", "qwen38-sglang.").replace("qwen38-sglang-64.", "qwen38-sglang.")
            with self.subTest(template=t.name):
                self.assertEqual(set(UNIT.findall(m.group(1))) & ENGINE, ENGINE - {own})
                after = re.search(r"^After=(.*)$", text, re.M)
                self.assertIsNotNone(after)
                self.assertTrue(ENGINE - {own} <= set(UNIT.findall(after.group(1))))


class EveryListNamesThemAll(unittest.TestCase):
    def test_the_shell_scripts(self):
        bad = []
        for p in shell_files():
            own_file = set()
            m = re.match(r"install-(.+)\.sh$", p.name)
            if m and f"qwen38-{m.group(1)}.service" in ENGINE:
                own_file = {f"qwen38-{m.group(1)}.service"}
            block = set()
            # a list continued over several lines (a trailing backslash) is one list
            lines, held = [], ""
            for n, raw in enumerate(p.read_text().splitlines(), 1):
                if raw.endswith("\\"):
                    held += raw[:-1] + " "
                    continue
                lines.append((n, held + raw)); held = ""
            for n, line in lines:
                b = re.match(r'^if \[ "\$CHOICE" = "([a-z0-9-]+)" \]', line)
                if b:
                    block = {CHOICE_LANE[c] for c in re.findall(r'"\$CHOICE" = "([a-z0-9-]+)"', line) if c in CHOICE_LANE}
                elif re.match(r"^fi\b", line):
                    block = set()
                bare = set() if NOT_LANES.match(line) else {b + ".service" for b in BARE.findall(line)}
                named = (set(UNIT.findall(line)) | bare) & ENGINE
                if "launch-flash.sh" in line:
                    named.add("qwen38-flash.service")   # the flash lane's mounts are in its launcher
                if len(named) >= 3 and not whole(named, own_file | block):
                    bad.append(f"{p.name}:{n} misses {missing(named, own=own_file | block)}")
        self.assertEqual(bad, [])

    def test_the_cockpits_python(self):
        files = sorted((REPO / "dashboard").glob("*.py"))
        trees = {p: ast.parse(p.read_text()) for p in files}
        # the constants that hold a lane's unit, module-level in any of the cockpit's files
        consts = {}
        for tree in trees.values():
            for node in tree.body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and node.value.value in ENGINE:
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            self.assertEqual(consts.setdefault(t.id, node.value.value), node.value.value, t.id)
        self.assertLessEqual({"IMAGE_UNIT", "VIDEO_UNIT", "KOLIBRI_UNIT", "NEMOTRON_UNIT", "NEMOTRON_SUPER_UNIT",
                              "FLASH_LOCAL_UNIT", "ORNITH_UNIT"}, set(consts), consts)

        def unit_of(e):
            if isinstance(e, ast.Constant):
                return e.value if isinstance(e.value, str) else None
            return consts.get(e.id if isinstance(e, ast.Name) else e.attr if isinstance(e, ast.Attribute) else None)
        bad, read = [], {}
        for p, tree in trees.items():
            # the name each literal is assigned to (VLLM_UNITS = (...)), which may say its family
            assigned = assigned_names(tree)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                    items = node.elts
                elif isinstance(node, ast.Dict):
                    items = [k for k in node.keys if k is not None]
                else:
                    continue
                named = {unit_of(e) for e in items} & ENGINE
                name = assigned.get(id(node))
                if checked(named, name):
                    read[f"{p.name}:{node.lineno}"] = named
                    if not whole(named, set(), name):
                        bad.append(f"{p.name}:{node.lineno} {name or ''} misses {missing(named, name)}")
        self.assertEqual(bad, [])
        # a witness: the lanes TEXT_UNITS leaves out are a list of names, and it is read
        line = next(n for n, l in enumerate((REPO / "dashboard" / "lifecycle.py").read_text().splitlines(), 1)
                    if l.lstrip().startswith("if u not in (IMAGE_UNIT"))
        self.assertEqual(read.get(f"lifecycle.py:{line}"), NOT_SGLANG)

    def test_the_pages_javascript(self):
        # base.js names each lane's unit once, as a constant, and every list is made of those names
        js = (REPO / "dashboard" / "static" / "js" / "base.js").read_text()
        consts = dict(re.findall(r"\b([A-Z][A-Z0-9_]*)\s*=\s*'(qwen38-[a-z0-9-]+\.service)'", js))
        names = {c for c, u in consts.items() if u in ENGINE}
        self.assertEqual({consts[c] for c in names}, ENGINE, "a lane with no constant in base.js")
        bad, read = [], {}
        for path in sorted((REPO / "dashboard" / "static" / "js").glob("*.js")):
            for line, held, name in js_literals(path.read_text()):
                named = {consts[c] for c in held if c in names} | ({h for h in held if h in ENGINE})
                if checked(named, name):
                    read[f"{path.name}:{line}"] = named
                    if not whole(named, set(), name):
                        bad.append(f"{path.name}:{line} {name or ''} misses {missing(named, name)}")
        self.assertEqual(bad, [])
        # witnesses: two of the page's tables keyed by the lanes' constants are read whole
        base = (REPO / "dashboard" / "static" / "js" / "base.js").read_text().splitlines()
        for table in ("const SIDE_LANE = {", "const READY_DEFAULT = {"):
            line = next(k for k, l in enumerate(base, 1) if l.startswith(table))
            self.assertIn(f"base.js:{line}", read, f"{table} is not read")
        # and the list of the vLLM lanes the Traffic view names them by, held to that family by its name
        ops = (REPO / "dashboard" / "static" / "js" / "ops.js").read_text().splitlines()
        line = next(k for k, l in enumerate(ops, 1) if l.startswith("const VLLM_UNITS = ["))
        self.assertEqual(read.get(f"ops.js:{line}"), VLLM)


class TheScannerReadsJavaScriptAsJavaScriptDoes(unittest.TestCase):
    """js_literals on what made a first version lose its place, each followed by a list it must find."""

    LIST = "const AFTER = [U27, UFLASH, KOLIBRI_UNIT];"

    def found(self, js):
        return [(names, name) for _, names, name in js_literals(js + "\n" + self.LIST + "\n")
                if {"U27", "UFLASH", "KOLIBRI_UNIT"} <= names]

    def test_what_once_lost_it(self):
        cases = {
            "a regex with a quote, in a template's ${...}": "const t = `-F ${shq('a\"' + n.replace(/[\\\"]/g, '\\\\$&'))}`;",
            "apostrophes in a template inside a template": "const t = `a ${x ? `opencode's ${y}` : 'z'} b`;",
            "a division after a call and an index": "const r = f(a) / 2 + b[0] / c;",
            "a regex after a return": "function f(s) { return /'/.test(s); }",
            "an arrow's block, then an arrow's object": "const f = x => { return 1; }; const g = x => ({a: [x]});",
            "a comment with a quote": "// opencode's own\n/* it's */ const q = 1;",
            "a computed key": "const M = {[KOLIBRI_UNIT]: 'k', [U27]: '2'};",
        }
        for what, js in cases.items():
            with self.subTest(what):
                self.assertEqual(self.found(js), [({"U27", "UFLASH", "KOLIBRI_UNIT"}, "AFTER")])

    def test_a_name_reaches_a_literal_passed_to_a_callee(self):
        lits = {name: names for _, names, name in js_literals(
            "const VLLM_SET = new Set([KOLIBRI_UNIT, U27]); const F = Object.freeze({[U27]: 1}); const N = f(x)[0];")}
        self.assertEqual(lits.get("VLLM_SET"), {"KOLIBRI_UNIT", "U27"})
        self.assertEqual(lits.get("F"), {"U27"})
        self.assertNotIn("N", lits)
        tree = ast.parse("B12X_SET = frozenset({'qwen38-nemotron.service'})\nVLLM_T = ('a', 'b')\n")
        names = sorted(assigned_names(tree).values())
        self.assertEqual(names.count("B12X_SET"), 2)       # the call and the set it wraps
        self.assertIn("VLLM_T", names)

    def test_a_miss_names_what_the_list_lacks_not_its_own_lane(self):
        others = ENGINE - {"qwen38-image.service", "qwen38-flash-local.service"}
        self.assertEqual(missing(others, own={"qwen38-image.service"}), ["flash-local"])

    def test_a_text_it_cannot_follow_fails_rather_than_passes(self):
        for js in ("const a = [1, 2;", "const s = 'never ends;", "const t = `never ${ends};", "f(a));"):
            with self.subTest(js):
                with self.assertRaises(ValueError):
                    js_literals(js)

    def test_every_file_of_the_page_is_read_to_its_end(self):
        for path in sorted((REPO / "dashboard" / "static" / "js").glob("*.js")):
            with self.subTest(path.name):
                js_literals(path.read_text())   # raises with the line where it lost its place


if __name__ == "__main__":
    unittest.main()
