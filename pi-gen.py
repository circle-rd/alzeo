#!/usr/bin/env python3
"""Generate the pi and omp configs from this repo's generated opencode.json.

Usage: pi-gen.py [--config <opencode.json>] [--out <dir>] [--dry-run]

pi (https://pi.dev) and omp (oh-my-pi, https://omp.sh) read no opencode.json,
so this writes the agents' side of this box's facts as ready files next to it
- never into ~/.pi or ~/.omp: copying them into the agent's own directory
stays the user's one cp:

  <out>/pi/models.json + settings.json    the canonical pair (pi's JSON)
  <out>/omp/models.yml + config.yml       the same values converted (omp YAML)

<config> defaults to ~/.config/qwen38/opencode.json, <out> to ~/.config/qwen38.
All read from the artifact: one provider per installed lane (the {file:...}
key as "!cat <path>": read at request time, no secret copied, rotation-proof),
per model name, modalities, contextWindow, maxTokens, a 1568x1568 /
512 KiB image resize limit and compat.supportsDeveloperRole false (the
system prompt as "system": the served template refuses "developer");
pi's settings.json adds the default model,
modelThinkingLevels "off" for the Qwen models (unset sends no effort field: the
installed template's default, lean, or xhigh on a box installed with LEAN_DEFAULT=0
and on the Qwen3.8 Flash-Next FP4-Local lane installed with FLASH_LOCAL_EFFORT=xhigh)
and "high" for Kolibri-1, the two Nemotron lanes and Ornith 1.5 122B, which are
sent their effort (EFFORT_FIELD),
keepRecentTokens from the artifact's compaction block and per-lane reserveTokens =
the lane's output cap. omp
gets the same values re-serialized: the same models array, thinking block
added, inputLimits and pi's compat dropped (its schema has no room for them);
default provider + model id -> modelRoles.default. The thinking block lists
the template's own tiers (the artifact's variants, lean included, what pi's
fixed enum cannot offer) with defaultLevel the installed template's default (lean, or xhigh on a
box installed with LEAN_DEFAULT=0 and on the FP4-Local lane with FLASH_LOCAL_EFFORT=xhigh); compat carries
thinkingFormat qwen-chat-template, qwenTemplateReasoningEffort and a
reasoningEffortMap, which route the pick onto
chat_template_kwargs.reasoning_effort alone, what the patched template reads
(SGLang refuses a top-level "lean").

Once this ran (its pi/ or omp/ directory exists), switch-model.sh (block 4f)
and an oc-fit-limits.py fit regenerate these files: re-copy them after a
switch or a fit. A file whose content would not change is not touched;
--dry-run prints what would change. The YAML comes
from a small stdlib writer: this repo's runtime imports nothing outside the
stdlib, and PyYAML is a measuring-gate package (tests/testpy.sh). Exit 0 when
done or with nothing to generate (no artifact, as on a --no-opencode box),
1 on a failed write, 2 on usage errors, 3 on an unreadable artifact.
"""
import argparse
import json
import os
import re
import shlex
import sys

OURS = ("qwen38", "flashnext", "kolibri", "nemotron", "nemotron-super", "flash-local", "ornith-122b")
RESIZE = {"maxWidth": 1568, "maxHeight": 1568, "maxBytes": 524288, "jpegQuality": 75}


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def write(path, text, dry):
    if read_text(path) == text:
        print(f"left as it is: {path}")
        return
    if dry:
        print(f"would write {path}")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print(f"could not write {path}: {e}")
        sys.exit(1)
    print(f"wrote {path}")


def yd(d):
    """Block-style YAML (mapping insertion order, two-space indents), the shape
    yaml.safe_dump(sort_keys=False) wrote before this repo's stdlib rule met
    this file. The values are only dicts, lists, strings, ints and bools; the
    keys are schema names and the two provider ids, every one plain-safe. All
    strings go through json.dumps, whose output is a valid YAML double-quoted
    scalar too: quoting all of them is how a leading-'!' apiKey (a YAML tag
    indicator when plain) survives without a special case."""
    def sc(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, str):
            return json.dumps(v)
        return str(v)
    def lines(v, ind):
        pad = "  " * ind
        if isinstance(v, dict):
            out = []
            for k, val in v.items():
                if val and isinstance(val, (dict, list)):
                    out.append(f"{pad}{k}:")
                    out += lines(val, ind + 1)
                elif isinstance(val, dict):
                    out.append(f"{pad}{k}: {{}}")
                elif isinstance(val, list):
                    out.append(f"{pad}{k}: []")
                else:
                    out.append(f"{pad}{k}: {sc(val)}")
            return out
        out = []
        for it in v:
            if isinstance(it, (dict, list)) and it:
                inner = lines(it, ind + 1)
                out.append(f"{pad}- {inner[0][len(pad) + 2:]}")
                out += inner[1:]
            elif isinstance(it, dict):
                out.append(f"{pad}- {{}}")
            elif isinstance(it, list):
                out.append(f"{pad}- []")
            else:
                out.append(f"{pad}- {sc(it)}")
        return out
    return "\n".join(lines(d, 0)) + "\n"


def api_key(options):
    """{file:<path>} -> "!cat <path>", which both agents run in a shell at request time:
    the path is quoted for it (a home with a space in it)."""
    v = (options or {}).get("apiKey", "")
    if isinstance(v, str) and v.startswith("{file:") and v.endswith("}"):
        return "!cat " + shlex.quote(v[6:-1])
    return v if isinstance(v, str) and v else "qwen38"


# pi sends its system prompt as the "developer" role to a reasoning model on a provider it
# does not know, and the served chat template refuses that role: every request got
# 400 "Unexpected message role." (pi 0.99.2 through the proxy to the flash lane,
# 2026-10-01). With the role off, the prompt goes out as "system".
PI_COMPAT = {"supportsDeveloperRole": False}
# Kolibri's template reads reasoning_effort none, low, medium or high, and vLLM 0.29 hands it
# a request's top-level field (and none turns thinking off). With no field it reasons at high:
# pi's "off" sent nothing, so a pi user saw thinking off on a lane reasoning at high on every
# request (found in review, 2026-10-14). pi sends the field for these models, its "off" as
# none and its other levels as the nearest of the four, and starts at high, what Kolibri does
# unasked. omp gets none as its "minimal" (OMP_LEVEL).
# Nemotron's template reads none of those levels (enable_thinking and medium_effort instead), and
# the proxy translates the field for it (keepalive-proxy.py, nemotron_effort): pi sends it the same
# way, and Bolt reasons at its full depth unasked, so pi starts at high there too. Super VL's
# template reads the same two, with the same three levels (its model card): the same, again.
# Ornith's template reads enable_thinking alone, and the proxy translates the field for it too
# (keepalive-proxy.py, ornith_effort): none and minimal turn it off, every other level keeps it whole,
# and it reasons at its full depth unasked, so pi starts at high. pi sends its levels as they are, its
# off as none: Kolibri's map sent its minimal as low, which the proxy keeps whole (found in review,
# 2026-10-22).
EFFORT_FIELD = {"kolibri": ({"off": "none", "minimal": "low", "xhigh": "high"}, "high"),
                "nemotron": ({"off": "none", "minimal": "low", "xhigh": "high"}, "high"),
                "nemotron-super": ({"off": "none", "minimal": "low", "xhigh": "high"}, "high"),
                "ornith-122b": ({"off": "none"}, "high")}


def model_meta(mid, m):
    lim = m.get("limit") or {}
    modal = (m.get("modalities") or {}).get("input") or ["text"]
    e = {"id": mid, "reasoning": True, "input": modal,
         "contextWindow": lim.get("context"), "maxTokens": lim.get("output"),
         "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
         "compat": dict(PI_COMPAT)}
    if m.get("name"):
        e["name"] = m["name"]
    if "image" in modal:
        e["inputLimits"] = {"images": {"resize": dict(RESIZE)}}
    return e


def build(artifact):
    """artifact -> (pi pair, {lane: the template's effort tiers}); pi can only
    offer its fixed level enum, so the lean-aware tier list (the artifact's own
    variants: what the patched template natively accepts) goes to omp alone."""
    provs, thinking, overrides, efforts, default = {}, {}, {}, {}, artifact.get("model")
    for prov, pcfg in (artifact.get("provider") or {}).items():
        if prov not in OURS or not isinstance(pcfg, dict):
            continue
        models = pcfg.get("models") or {}
        opts = pcfg.get("options") or {}
        base = opts.get("baseURL")
        if not base or not models:
            continue
        mid = next(iter(models))
        meta = model_meta(mid, models[mid] if isinstance(models[mid], dict) else {})
        provs[prov] = {"baseUrl": base, "api": "openai-completions",
                       "apiKey": api_key(opts), "models": [meta]}
        thinking[f"{prov}/{mid}"] = "off"
        if prov in EFFORT_FIELD:
            level_map, thinking[f"{prov}/{mid}"] = EFFORT_FIELD[prov]
            meta["compat"]["supportsReasoningEffort"] = True
            meta["thinkingLevelMap"] = dict(level_map)
        overrides[f"{prov}/{mid}"] = {"reserveTokens": meta["maxTokens"]}
        tiers = list((models[mid] or {}).get("variants") or {})
        if tiers:
            efforts[f"{prov}/{mid}"] = tiers
    models_doc = {"providers": provs}
    settings = {}
    if isinstance(default, str) and "/" in default:
        settings["defaultProvider"], settings["defaultModel"] = default.split("/", 1)
    settings["modelThinkingLevels"] = thinking
    comp = {"enabled": True}
    preserve = (artifact.get("compaction") or {}).get("preserve_recent_tokens")
    if preserve:
        comp["keepRecentTokens"] = preserve
    comp["modelOverrides"] = overrides
    settings["compaction"] = comp
    return models_doc, settings, efforts


# omp's thinking levels are a fixed enum (like pi's); the template's own tiers
# are reached through compat.reasoningEffortMap, which maps the internal level
# onto the provider string, and compat.qwenTemplateReasoningEffort, which
# routes the mapped pick onto chat_template_kwargs.reasoning_effort, what the
# patched template reads. The catalog ships that flag on by default for Qwen
# 3.8+ ids on LM Studio, llama.cpp discovery and vLLM only, and this proxy's
# provider id is none of those: without it the picker moved and no effort
# field was sent (captured against omp 18.4.6, 2026-10-01). So the generator
# sets it beside every thinking block. Its default dialect also copies the
# effort into a top-level reasoning_effort, and SGLang validates that field
# against none/minimal/low/medium/high/xhigh/max: "lean" there was a 400 on
# every request at the default level (omp 18.4.9, both lanes' images,
# 2026-10-01). thinkingFormat "qwen-chat-template", omp's own dialect for
# vLLM and SGLang, sends the kwargs alone. omp validates models.yml itself:
# these spellings were fixed against its error output, not only the docs.
OMP_LEVEL = {"lean": "minimal", "low": "low", "medium": "medium", "minimal": "minimal",
             "high": "high", "xhigh": "xhigh", "max": "max", "none": "minimal"}


# The patched template each Qwen lane reads, beside the configs this writes (patch-template.py). Its
# default is the effort a request that names none gets: lean on a box installed as is, Qwen's xhigh on
# one installed with LEAN_DEFAULT=0, and on the FP4-Local lane installed with FLASH_LOCAL_EFFORT=xhigh.
# omp sends its default level on every request, so it starts there: it took lean for every patched
# template, and a LEAN_DEFAULT=0 box got lean in omp alone (found in review, 2026-10-20).
LANE_TEMPLATE = {"qwen38": "chat-template-sglang.jinja", "flashnext": "chat-template-flashnext.jinja",
                 "flash-local": "chat-template-flash-local.jinja"}
TEMPLATE_DEFAULT = re.compile(r"reasoning_effort\s*\|\s*default\(\s*['\"]([a-z]+)['\"]\s*\)")


# The FP4-Local lane's default is its installer's to choose (FLASH_LOCAL_EFFORT, install-flash-local.sh): lean, or the
# xhigh it keeps in a file beside these configs. Read there and not in its template: an update runs this before the
# lane's installer writes the template again, and a lane that serves reads its template at its next start only, so omp
# stayed at the xhigh of the template of v1.46.3 (found in review of v1.46.4).
EFFORT_CHOICE = {"flash-local": "flash-local-effort"}


def template_default(out_dir, prov):
    """The default effort of a provider's lane: its installer's kept choice where it keeps one, else the installed
    template's; None when unknown."""
    choice = EFFORT_CHOICE.get(prov)
    if choice:
        kept = (read_text(os.path.join(out_dir, choice)) or "").split("\n", 1)[0]     # as the installer's head -1
        return "xhigh" if kept == "xhigh" else "lean"
    name = LANE_TEMPLATE.get(prov)
    m = TEMPLATE_DEFAULT.search((read_text(os.path.join(out_dir, name)) if name else None) or "")
    return m.group(1) if m else None


def omp_thinking(tiers, default_effort=None):
    """template tiers -> (thinking block, compat), enum-clean. lean rides on
    minimal, high/max collapse onto xhigh, exactly as the patched template
    maps. compat always carries qwenTemplateReasoningEffort; the map only
    when a tier needs another spelling. default_effort is the installed
    template's own default (template_default), which omp's default level follows."""
    levels, rmap = [], {}
    for t in tiers:
        lvl = OMP_LEVEL.get(t)
        if not lvl:
            continue
        if lvl not in levels:
            levels.append(lvl)
        if lvl != t:
            rmap[lvl] = t
    compat = {"thinkingFormat": "qwen-chat-template"}
    if not levels:
        return None, compat
    # The lane's default decides, never the order of the tiers: the FP4-Local lane's provider lists xhigh first as it
    # has since v1.37.0, and its default is lean since v1.46.4 (template_default reads its installer's choice).
    if default_effort == "xhigh" and "xhigh" in levels:
        default = "xhigh"             # Qwen's own default, kept by the template (LEAN_DEFAULT=0) or chosen for the lane
    elif "lean" in tiers and "minimal" in levels:
        default = "minimal"           # the patched Qwen template's own default
    elif "none" in tiers and "high" in levels:
        default = "high"              # Kolibri reasons at high unasked (EFFORT_FIELD)
    else:
        default = levels[0]
    thinking = {"mode": "effort", "efforts": levels, "defaultLevel": default}
    compat["qwenTemplateReasoningEffort"] = True
    if rmap:
        compat["reasoningEffortMap"] = rmap
    return thinking, compat


def omp_convert(models_doc, settings, efforts, defaults=None):
    """The pi pair, re-serialized into omp's YAML shapes: same values, no rebuild.
    The thinking block carries omp's enum levels; reasoningEffortMap turns the
    selection onto the template's tiers (minimal -> lean) and, with
    qwenTemplateReasoningEffort set, omp routes it onto
    chat_template_kwargs.reasoning_effort."""
    provs = {}
    for prov, b in models_doc["providers"].items():
        entries = []
        for m in b["models"]:
            # inputLimits and pi's compat are pi's shapes; omp's models.yml schema
            # does not carry them, and a schema error makes omp skip the whole
            # file. models is an ARRAY in omp too (verified against omp's own
            # validation error).
            e = {k: v for k, v in m.items() if k not in ("inputLimits", "compat", "thinkingLevelMap")}
            thinking, compat = omp_thinking(efforts.get(f"{prov}/{m['id']}", []), (defaults or {}).get(prov))
            if thinking:
                e["thinking"] = thinking
            if compat:
                e["compat"] = compat
            entries.append(e)
        provs[prov] = {"baseUrl": b["baseUrl"], "api": b["api"], "apiKey": b["apiKey"],
                       "models": entries}
    cfg = {}
    if "defaultProvider" in settings:
        cfg["modelRoles"] = {"default": f"{settings['defaultProvider']}/{settings['defaultModel']}"}
    return {"providers": provs}, cfg


def main():
    ap = argparse.ArgumentParser(description="Generate the pi and omp configs from opencode.json")
    ap.add_argument("--config", default=os.path.join(os.environ.get("HOME", ""), ".config", "qwen38", "opencode.json"))
    ap.add_argument("--out", default=os.path.join(os.environ.get("HOME", ""), ".config", "qwen38"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        text = read_text(a.config)
        if text is None:
            print(f"{a.config} not found (a --no-opencode box?); nothing to generate.")
            return
        artifact = json.loads(text)
        if not isinstance(artifact, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        print(f"could not read {a.config}: {e}")
        sys.exit(3)
    models_doc, settings, efforts = build(artifact)
    if not models_doc["providers"]:
        print(f"NOTE: {a.config} has none of this repo's providers ({'/'.join(OURS)}): nothing to generate.")
        return
    defaults = {prov: template_default(a.out, prov) for prov in models_doc["providers"]}
    omp_models, omp_cfg = omp_convert(models_doc, settings, efforts, defaults)
    jd = lambda d: json.dumps(d, indent=2) + "\n"
    write(os.path.join(a.out, "pi", "models.json"), jd(models_doc), a.dry_run)
    write(os.path.join(a.out, "pi", "settings.json"), jd(settings), a.dry_run)
    write(os.path.join(a.out, "omp", "models.yml"), yd(omp_models), a.dry_run)
    write(os.path.join(a.out, "omp", "config.yml"), yd(omp_cfg), a.dry_run)


if __name__ == "__main__":
    main()
