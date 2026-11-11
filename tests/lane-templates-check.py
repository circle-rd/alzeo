#!/usr/bin/env python3
"""Every text lane's chat template, as its installer serves it, takes what the real clients send.

opencode 1.19.4 sends two system messages before the user's, and the published template of the
Ornith 1.5 122B lane refused the second ("System message must be at the beginning"): every
opencode request on that lane got a 400 from v1.38.6 to v1.40.1, and nothing saw it, as each
release proved the clients on the lane that happened to serve (found on 2026-10-24).

This takes each text lane's chat template at its pinned revision on Hugging Face (chat_template.jinja,
or the chat_template of tokenizer_config.json, the one named tool_use when tools are sent and it has
one, as transformers picks), patches it as its installer does (patch-template.py with the lane's
TEMPLATE_FIXES, from chat_template.jinja only, as the installer reads it), and renders, as
transformers renders it for vLLM and SGLang, every message shape the real clients send
(tests/fixtures/client-shapes.json), with tools and without, sent as strings and as lists of parts,
each handed to the template as its engine hands it (the engines' own reading of the template and
conversion of the messages, ported below). A template that raises fails the run, naming the lane and
the client. So do a text target of
switch-model.sh this check does not know (a variant is a target: Kolibri's FP8, its default, and its
NVFP4 each have their pin) and a lane whose installer patches its template otherwise than the table
below says.

Network: the templates come from huggingface.co. Usage: lane-templates-check.py (no argument).
"""
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHAPES = REPO / "tests" / "fixtures" / "client-shapes.json"

# How each vLLM target's installer serves its template: None as published, else how its installer
# runs patch-template.py ("patches": its --patches option, the rest its environment). lane_specs()
# checks this against the installers themselves.
# A target, not a lane: the Kolibri lane's two checkpoints each have their pin, and the check read only
# the NVFP4's while the FP8 is the lane's default (found in review, 2026-10-24). The Qwen3.8 Flash-Next
# FP4-Local lane's line is its default, lean (v1.46.4); its option FLASH_LOCAL_EFFORT=xhigh gives the
# same template Qwen's xhigh as its default, the one this check rendered until then.
VLLM_LANES = {
    "flash-local": ("install-flash-local.sh", "FLASH_LOCAL", {"LEAN_DEFAULT": "1"}),
    "ornith-122b": ("install-ornith-122b.sh", "ORNITH_122B", {"patches": "system-reminder,tool-reference"}),
    "kolibri": ("install-kolibri.sh", "KOLIBRI_FP8", None),
    "kolibri-nvfp4": ("install-kolibri.sh", "KOLIBRI_NVFP4", None),
    "nemotron": ("install-nemotron.sh", "NEMOTRON_BOLT", None),
    "nemotron-super": ("install-nemotron-super.sh", "NEMOTRON_SUPER", None),
}
# switch-model.sh's targets that serve no chat template
NOT_TEXT = {"image", "image-turbo", "video"}


def pin(text, prefix):
    """The (repo, revision) an installer pins under PREFIX_REPO and PREFIX_REV."""
    repo = re.search(rf'^{prefix}_REPO="([^"]+)"', text, re.M)
    rev = re.search(rf'^{prefix}_REV="([0-9a-f]{{40}})"', text, re.M)
    if not (repo and rev):
        raise SystemExit(f"{prefix}_REPO and {prefix}_REV are not where this check reads them")
    return repo.group(1), rev.group(1)


def fixes_of(installer_text, name="the installer"):
    """What an installer tells patch-template.py, from its one TEMPLATE_FIXES line (vlane_template in
    vllm-lane.sh runs it with them): None when it patches nothing, else {"patches": its --patches, and
    each VAR=value of its environment}. It stops on an installer that runs patch-template.py itself, one
    whose call of vlane_template leaves its fixes out, and an option this check cannot pass on (each
    would have rendered a template the lane does not serve: code review, 2026-10-24)."""
    # patch-template.py named anywhere but in the list of files its preflight checks, however the call
    # is written (${HERE}, python3 -I, the file run itself, a line continued): code review, 2026-10-24
    code = [re.sub(r"(^|\s)#.*$", "", line).strip() for line in re.sub(r"\\\n", " ", installer_text).splitlines()]
    for line in code:
        if "patch-template.py" in line and not line.startswith("vlane_preflight "):
            raise SystemExit(f"{name} runs patch-template.py itself: give it a TEMPLATE_FIXES line, run through vlane_template")
    calls = [c.group(1) for c in (re.search(r"\bvlane_template(?:_if_cached)? (\S.*?)(?:; *\}|$)", line) for line in code) if c]
    if sum(bool(re.match(r"TEMPLATE_FIXES\+?=", line)) for line in code) > 1:
        raise SystemExit(f"{name} sets TEMPLATE_FIXES more than once: this check reads one line")
    m = re.search(r"^TEMPLATE_FIXES=\(([^)]*)\)$", installer_text, re.M)
    if not m:
        if calls:
            raise SystemExit(f"{name} calls vlane_template without a TEMPLATE_FIXES line this check reads")
        return None
    if not calls or any(c.strip() != '"$TEMPLATE" "${TEMPLATE_FIXES[@]}"' for c in calls):
        raise SystemExit(f'{name} declares TEMPLATE_FIXES and does not pass them all: vlane_template "$TEMPLATE" "${{TEMPLATE_FIXES[@]}}"')
    spec = {}
    for item in m.group(1).split():
        if item.startswith("--patches="):
            spec["patches"] = item[len("--patches="):]
        elif re.fullmatch(r"[A-Z][A-Z0-9_]*=\S*", item):
            key, _, value = item.partition("=")
            spec[key] = value
        else:
            raise SystemExit(f"{name}: TEMPLATE_FIXES holds {item}, which this check cannot pass to patch-template.py")
    return spec


def lane_specs(repo_dir=REPO):
    """Every text target: (name, repo, revision, patch environment or None). The 27B and flash targets
    are install.sh's (its case of MODEL_REPO lines), patched with the five fixes at every install and
    switch; the vLLM targets are VLLM_LANES, checked against what each installer does. Together they
    are every target of switch-model.sh but NOT_TEXT, or this stops."""
    install = (repo_dir / "install.sh").read_text()
    targets = re.findall(r'^\s+([a-z0-9-]+)\)\s+MODEL_REPO="\$([A-Z0-9_]+)_REPO"', install, re.M)
    if not targets:
        raise SystemExit("install.sh no longer maps its targets the way this check reads them")
    # the five fixes there: install.sh and switch-model.sh run patch-template.py with no --patches and no
    # environment of their own, or this check would render a template they do not write (code review)
    for script in ("install.sh", "switch-model.sh"):
        calls = [ln.strip() for ln in re.sub(r"\\\n", " ", (repo_dir / script).read_text()).splitlines()
                 if "patch-template.py" in re.sub(r"(^|\s)#.*$", "", ln)]
        if not calls or any("--patches" in c or not c.startswith('python3 "$REPO_DIR/patch-template.py"') for c in calls):
            raise SystemExit(f"{script} runs patch-template.py otherwise than with its five fixes: {calls}")
    specs = [(t, *pin(install, p), {}) for t, p in targets]
    installers = sorted(f.name for f in repo_dir.glob("install-*.sh") if "vlane_put_unit" in f.read_text())
    known = sorted({f for f, _, _ in VLLM_LANES.values()})
    if installers != known:
        raise SystemExit(f"the vLLM lanes are {installers}, this check knows {known}: add the new lane to VLLM_LANES")
    for name, (installer, prefix, env) in VLLM_LANES.items():
        text = (repo_dir / installer).read_text()
        if fixes_of(text, installer) != env:
            raise SystemExit(f"{installer} serves its template otherwise than VLLM_LANES says for {name}")
        specs.append((name, *pin(text, prefix), env))
    switch = re.search(r'^case "\$CHOICE" in ([a-z0-9|-]+)\) ;;', (repo_dir / "switch-model.sh").read_text(), re.M)
    if not switch:
        raise SystemExit("switch-model.sh no longer lists its targets the way this check reads them")
    served, checked = set(switch.group(1).split("|")) - NOT_TEXT, {name for name, _, _, _ in specs}
    if served != checked:
        raise SystemExit(f"switch-model.sh serves the text targets {sorted(served - checked)} this check does not"
                         f" know, and it knows {sorted(checked - served)} the switch does not serve: add them")
    return specs


# Hugging Face answers a runner's anonymous requests from shared addresses: a 429 or a brief 5xx
# is waited out, not taken for a broken template (2026-10-24). Its quotas count over fixed windows of
# five minutes, and its 429 tells when the window ends in its RateLimit header, `"resolvers";r=0;t=55`
# (https://huggingface.co/docs/hub/en/rate-limits): that wait, or a Retry-After's, up to WAIT_MAX.
RETRY = (408, 425, 429, 500, 502, 503, 504)
WAIT_MAX = 300


def server_wait(headers):
    """The seconds the server asks for before the next try: RateLimit's t=, else Retry-After, else None."""
    headers = headers or {}
    left = re.search(r";\s*t=(\d+)", headers.get("RateLimit") or "")
    after = (headers.get("Retry-After") or "").strip()
    if left:
        return min(int(left.group(1)) + 1, WAIT_MAX)
    return min(int(after), WAIT_MAX) if after.isdigit() else None


# transformers reads a repo's named templates there too, beside chat_template.jinja (its
# list_repo_templates and _from_pretrained, 5.12.1): none of the targets has one on 2026-10-24
EXTRA_TEMPLATES = "additional_chat_templates"


def fetch(repo, rev, tries=5, sleep=time.sleep):
    """(the chat template the engine reads at that revision, the file it comes from), as transformers
    assembles it: chat_template.jinja as the template named default, and the named templates of
    additional_chat_templates/, a dict of them by name when there are more than the default; without
    either, the chat_template of tokenizer_config.json, a dict by name when it names several."""
    def resolve(path):
        return get(f"https://huggingface.co/{repo}/resolve/{rev}/{path}")

    def get(url):
        for attempt in range(tries):
            wait = 2 ** (attempt + 1)
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    return r.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code not in RETRY or attempt == tries - 1:
                    raise
                asked = server_wait(e.headers)
                wait = wait if asked is None else asked
            except (OSError, http.client.HTTPException):    # a reset, a timeout, a body cut short
                if attempt == tries - 1:
                    raise
            sleep(wait)
    templates = {}
    default = resolve("chat_template.jinja")
    if default is not None:
        templates["default"] = default
    listing = get(f"https://huggingface.co/api/models/{repo}/tree/{rev}/{EXTRA_TEMPLATES}")
    for entry in json.loads(listing) if listing else []:
        path = entry.get("path") or ""
        if entry.get("type") == "file" and path.endswith(".jinja"):
            templates[path.rsplit("/", 1)[-1][:-len(".jinja")]] = resolve(path)
    if templates:
        source = "chat_template.jinja" if default is not None else EXTRA_TEMPLATES
        return (templates["default"] if list(templates) == ["default"] else templates), source
    t = json.loads(resolve("tokenizer_config.json") or "{}").get("chat_template")
    if isinstance(t, list):
        t = {x.get("name"): x.get("template") for x in t if isinstance(x, dict) and x.get("template")}
    if not t:
        raise SystemExit(f"{repo}@{rev[:12]} has no chat template where the engines look")
    return t, "tokenizer_config.json"


def pick(templates, tools):
    """The template transformers renders: the one named tool_use when tools are sent and it has one,
    else the one named default; a single template is itself."""
    if isinstance(templates, str):
        return templates
    name = "tool_use" if tools and "tool_use" in templates else "default"
    if name not in templates:
        raise SystemExit(f"named chat templates without a {name!r} one: {sorted(templates)}")
    return templates[name]


def served(repo, rev, fetched, spec):
    """The template as the lane's installer serves it: as published, or patch-template.py on a cache of
    one snapshot, which reads chat_template.jinja only: a patched lane without one would pass here and
    stop its installer (found in review, 2026-10-24)."""
    template, source = fetched
    if spec is None:
        return template
    if source != "chat_template.jinja":
        raise SystemExit(f"{repo}@{rev[:12]}: its installer patches chat_template.jinja, which it does not have")
    spec = dict(spec)
    patches = spec.pop("patches", None)
    named = template if isinstance(template, dict) else {"default": template}
    with tempfile.TemporaryDirectory(prefix="lane-tpl-") as d:
        snap = Path(d, "hub", f"models--{repo.replace('/', '--')}", "snapshots", rev)
        snap.mkdir(parents=True)
        (snap / "chat_template.jinja").write_text(named["default"])
        out = Path(d, "served.jinja")
        argv = [sys.executable, str(REPO / "patch-template.py")] + ([f"--patches={patches}"] if patches else [])
        r = subprocess.run(argv + [d, str(out), rev, repo], capture_output=True, text=True, env={**os.environ, **spec})
        if r.returncode != 0:
            raise SystemExit(f"patch-template.py refused {repo}@{rev[:12]}:\n{r.stdout}{r.stderr}")
        # the installer patches chat_template.jinja alone and hands that file to the engine with
        # --chat-template, which serves it to every request: a named template of the repo goes unused
        # (second review, 2026-10-24)
        return out.read_text()


def environment():
    """A Jinja environment as transformers compiles chat templates with: sandboxed, blocks trimmed,
    loop controls, its {% generation %} tag, raise_exception, tojson and strftime_now."""
    import jinja2
    from jinja2.ext import Extension, loopcontrols
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    class Generation(Extension):
        """transformers' AssistantTracker without its tracking: {% generation %} renders its body. A
        template that marks its assistant turns so read as a refusal here (code review, 2026-10-24)."""
        tags = {"generation"}

        def parse(self, parser):
            lineno = next(parser.stream).lineno
            body = parser.parse_statements(["name:endgeneration"], drop_needle=True)
            return jinja2.nodes.CallBlock(self.call_method("_body"), [], [], body).set_lineno(lineno)

        def _body(self, caller):
            return caller()

    def raise_exception(message):
        raise jinja2.exceptions.TemplateError(message)

    def tojson(x, ensure_ascii=False, indent=None, separators=None, sort_keys=False):
        return json.dumps(x, ensure_ascii=ensure_ascii, indent=indent, separators=separators, sort_keys=sort_keys)

    env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True, extensions=[loopcontrols, Generation])
    env.filters["tojson"] = tojson
    env.globals["raise_exception"] = raise_exception
    env.globals["strftime_now"] = lambda fmt: datetime.now().strftime(fmt)
    return env


# How each engine hands a template a message's content, read in their sources on 2026-10-24: vLLM at
# the commits its lanes pin (8244d1c4 for Flash FP4-Local and Ornith, 77154ca6 for the two Nemotron
# lanes, v0.29.0 for Kolibri), SGLang at v0.5.22 (the 27B) and at the flash lane's nightly 53295c6,
# whose code for it is the same. Both read the template first. vLLM's _detect_content_format gives
# "openai" to one that loops over a message's content, and every content then reaches it as a list of
# text parts, a string included; any other gets "string", the parts joined by a newline. SGLang's
# detect_jinja_template_content_format reads it more simply, and leaves a string a string: a list
# reaches the template only when the client sent one and the template takes "openai", else its text
# joined by a space. Both make a tool's text parts one string. The detectors and the conversions below
# are theirs, ported (Apache-2.0): the check renders what a client sends, strings and lists of parts,
# as its engine hands it on (the check rendered a tool's answer as a list, which neither engine does,
# and vLLM's empty assistant content as a string: code review, 2026-10-24).
def _var(node, name):
    import jinja2.nodes as n
    return isinstance(node, n.Name) and node.ctx == "load" and node.name == name


def _attr(node, name, key):
    import jinja2.nodes as n
    if isinstance(node, n.Getitem):
        return _var(node.node, name) and isinstance(node.arg, n.Const) and node.arg.value == key
    if isinstance(node, n.Getattr):
        return _var(node.node, name) and node.attr == key
    return False


def _elems(node, name, key=None):
    import jinja2.nodes as n
    if isinstance(node, n.Filter):
        return node.node is not None and _elems(node.node, name, key)
    if isinstance(node, n.Test):
        return _elems(node.node, name, key)
    if isinstance(node, n.Getitem) and isinstance(node.arg, n.Slice):
        return _elems(node.node, name, key)
    return _attr(node, name, key) if key else _var(node, name)


class _NotAName(Exception):
    """vLLM asserts that what takes the messages, or a loop's item over them, is a plain variable: its
    detector stops there, and the template gets its default, "string"."""


def _aliases_of(root, name):
    """vLLM's _iter_nodes_assign_var_or_elems: the variable and every variable assigned from it."""
    import jinja2.nodes as n
    out, seen, todo = [name], {name}, [name]
    while todo:
        cur = todo.pop(0)
        for a in root.find_all(n.Assign):
            if _elems(a.node, cur):
                if not isinstance(a.target, n.Name):
                    raise _NotAName(cur)
                out.append(a.target.name)
                if a.target.name not in seen:
                    seen.add(a.target.name)
                    todo.append(a.target.name)
    return out


def vllm_takes_parts(template, env, macros=True):
    """vLLM's _detect_content_format: True for "openai", False for "string". macros=True as at 8244d1c4
    and 77154ca6, which follow a message's content into a macro's parameter and leave a macro's other
    loops over a variable named content alone; False as at v0.29.0, where any loop over such a variable
    counts. A template it cannot parse, or one that puts its messages into anything but a plain
    variable, gets the default, "string"."""
    import jinja2.nodes as n
    try:
        root = env.parse(template)
        messages = _aliases_of(root, "messages")
    except Exception:  # noqa: BLE001: vLLM falls back to its default here
        return False
    message_vars = []
    for loop in root.find_all(n.For):
        if any(_elems(loop.iter, v) for v in messages):
            if not isinstance(loop.target, n.Name):
                return False
            message_vars.append(loop.target.name)
    params_by_loop, in_macros = {}, set()
    for macro in (root.find_all(n.Macro) if macros else ()):
        names, params = {a.name for a in macro.args}, set()
        for call in root.find_all(n.Call):
            if isinstance(call.node, n.Name) and call.node.name == macro.name:
                for i, arg in enumerate(call.args):
                    if i < len(macro.args) and any(_elems(arg, v, "content") for v in message_vars):
                        params.add(macro.args[i].name)
                for kw in call.kwargs:
                    if kw.key in names and any(_elems(kw.value, v, "content") for v in message_vars):
                        params.add(kw.key)
        for loop in macro.find_all(n.For):
            in_macros.add(id(loop))
            if params:
                params_by_loop[id(loop)] = params
    for loop in root.find_all(n.For):
        if (any(_elems(loop.iter, v, "content") for v in message_vars)
                or isinstance(loop.iter, n.Name) and loop.iter.name in params_by_loop.get(id(loop), ())
                or id(loop) not in in_macros and isinstance(loop.iter, n.Name) and loop.iter.name == "content"):
            return isinstance(loop.target, n.Name)
    return False


def sglang_takes_parts(template, env):
    """SGLang's detect_jinja_template_content_format: True for "openai"."""
    import jinja2.nodes as n
    if any(word in template for word in ("image", "audio", "video", "vision")):
        return True
    try:
        root = env.parse(template)
    except Exception:  # noqa: BLE001
        return False
    return any(_elems(loop.iter, v, "content") for loop in root.find_all(n.For) for v in ("message", "msg", "m"))


def vllm_hands(msgs, openai):
    """The messages as vLLM hands them to a template (_parse_chat_message_content): for one that takes
    "openai" content, each content a list of text parts, a string made one part and none none, and a
    tool's text parts joined by a newline; for any other, the text parts joined by a newline, empty ones
    left out."""
    out = []
    for m in msgs:
        c = m.get("content")
        texts = [] if c is None else [c] if isinstance(c, str) else [p["text"] for p in c]
        if not openai:
            content = "\n".join(t for t in texts if t)
        elif m["role"] == "tool":
            content = "\n".join(texts)
        else:
            content = [{"type": "text", "text": t} for t in texts]
        out.append({**m, "content": content})
    return out


def sglang_hands(msgs, openai):
    """The messages as SGLang hands them to a template (serving_chat.py): no content is "", a string
    stays a string, a list stays a list for a template that takes "openai" content, else its text
    joined by a space (process_content_for_template_format), and a tool's text parts are one string,
    joined by a space (normalize_tool_content)."""
    out = []
    for m in msgs:
        c = "" if m.get("content") is None else m["content"]
        if isinstance(c, list) and (not openai or m["role"] == "tool"):
            c = " ".join(p["text"] for p in c)
        out.append({k: v for k, v in {**m, "content": c}.items() if v is not None})
    return out


# vLLM's detector as at each vLLM lane's pin: the Kolibri lane's image is vLLM v0.29.0 (install-kolibri.sh,
# its LOCAL_IMAGE), which reads no macro's parameter; the others are later.
VLLM_WITHOUT_MACROS = {"kolibri", "kolibri-nvfp4"}


def engine_reads(name, template, env):
    """(the engine, True when it takes "openai" content) for this target's template."""
    if name in VLLM_LANES:
        return "vllm", vllm_takes_parts(template, env, macros=name not in VLLM_WITHOUT_MACROS)
    return "sglang", sglang_takes_parts(template, env)


TOOLS = [{"type": "function", "function": {
    "name": "read", "description": "Read a file of the project.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


def messages(roles, parts=False):
    """A conversation of these roles as a client sends it, with a tool call where the shape has one:
    each content a string, or with parts, a list of text parts, and a tool call's own content null, as
    the OpenAI API returns it and an agent on its SDK sends it back."""
    text = (lambda t: [{"type": "text", "text": t}]) if parts else (lambda t: t)
    out = []
    for i, role in enumerate(roles):
        if role == "assistant_tool_call":
            out.append({"role": "assistant", "content": None if parts else "", "tool_calls": [{
                "id": f"call_{i}", "type": "function",
                "function": {"name": "read", "arguments": {"path": "hello.txt"}}}]})
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": f"call_{i - 1}", "content": text("7351")})
        else:
            out.append({"role": role, "content": text(f"A {role} message, number {i}.")})
    return out


def render(env, template, roles, tools, parts=False, hands=None):
    """None when the template renders this conversation, as the engine hands it on, else why it does not."""
    msgs = messages(roles, parts)
    msgs = hands(msgs) if hands else msgs    # outside the try: a fault of the check is not the template's refusal
    try:
        env.from_string(pick(template, tools)).render(messages=msgs, tools=tools,
                                                      add_generation_prompt=True, bos_token="", eos_token="")
        return None
    except Exception as e:  # noqa: BLE001: any refusal of the template is the finding
        return f"{type(e).__name__}: {e}"


# A template that refuses a system message past the first one, as Ornith's published one did: the
# renderer must see its refusal, or this check would pass on anything.
CANARY = ("{%- for m in messages %}{%- if m.role == 'system' and not loop.first %}"
          "{{- raise_exception('System message must be at the beginning.') }}{%- endif %}{%- endfor %}")


def main():
    shapes = json.loads(SHAPES.read_text())["shapes"]
    env = environment()
    # through each engine's conversion of the messages too, in each of its readings: a conversion that lost
    # them would pass anything (code review, 2026-10-24)
    for hands in (None, *[lambda m, h=h, o=o: h(m, o) for h in (vllm_hands, sglang_hands) for o in (False, True)]):
        for parts in (False, True):
            if render(env, CANARY, ["system", "system", "user"], None, parts, hands) is None:
                raise SystemExit("the renderer did not see the canary's refusal: this check would pass on anything")
    failed = renders = 0
    for name, repo, rev, patch in lane_specs():
        template = served(repo, rev, fetch(repo, rev), patch)
        how = "as published" if patch is None else f"patched ({patch.get('patches', 'all five fixes')})"
        reads = {tools is not None: engine_reads(name, pick(template, tools), env) for tools in (None, TOOLS)}
        engine = reads[False][0]
        said = {("vllm", True): "lists of parts", ("sglang", True): "what the client sends"}
        takes = " and ".join(sorted({said.get(r, "strings") for r in reads.values()}))
        print(f"{name}: {repo}@{rev[:12]}, {how}, {engine}, which hands it {takes}")
        hands_of = vllm_hands if engine == "vllm" else sglang_hands
        for shape in shapes:
            for tools in (None, TOOLS):
                openai = reads[tools is not None][1]
                for parts in (False, True):
                    renders += 1
                    why = render(env, template, shape["roles"], tools, parts, lambda m: hands_of(m, openai))
                    if why:
                        failed += 1
                        print(f"  REFUSED {shape['client']}{' with tools' if tools else ''}"
                              f"{', sent as parts' if parts else ''}: {why}")
    print(f"{renders} renders, {failed} refusal(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
