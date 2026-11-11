#!/usr/bin/env python3
"""The Flash FP4-Local lane's opencode provider, in one place.

install.sh's generator writes it into the opencode config once the lane is installed, and
switch-model.sh adds it to a config written before that, so both take it from here and the two
cannot drift.

Usage: oc-flash-local.py <base_url> <api-key file> <ctx> <out> <name>
prints {"provider": {"flash-local": ...}}, the shape oc-merge-limits.py --add-providers reads.
"""
import json
import os
import sys

# The checkpoint's chat template is the Qwen3.8 one the flash lane serves, byte for byte, patched
# the same way (patch-template.py), so the same four levels, in the order install.sh lists the other
# Qwen lanes' but with the level a request that names none gets first, as a picker lists it: lean, as
# on the other Qwen lanes since v1.46.4, or xhigh, its model card's default, on a box whose installer
# was run with FLASH_LOCAL_EFFORT=xhigh. Fixed at xhigh until v1.46.3, the lane's default then.
LEVELS = ("lean", "low", "medium", "xhigh")
# The choice the lane's installer keeps beside the box's key (install-flash-local.sh), read here and
# not from the template: install.sh writes the opencode config before it runs the lane's installer,
# which writes the template again, and that template takes the choice.
EFFORT_FILE = "flash-local-effort"


def default_effort(config_dir):
    """The level the lane's template defaults to, read as its installer reads its kept choice."""
    try:
        with open(os.path.join(config_dir, EFFORT_FILE), encoding="utf-8", errors="replace") as f:
            return "xhigh" if f.readline().rstrip("\n") == "xhigh" else "lean"
    except OSError:
        return "lean"


def provider(base_url, key_file, ctx, out, name):
    """The provider block. The model reads images, as the flash lane's does. Both callers hand the
    box's key in the config directory, where the lane's installer keeps its level."""
    first = default_effort(os.path.dirname(key_file))
    levels = (first,) + tuple(lvl for lvl in LEVELS if lvl != first)
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Qwen3.8-Flash-Next FP4-Local (DGX Spark)",
        "options": {"baseURL": base_url, "apiKey": f"{{file:{key_file}}}"},
        "models": {"qwen3.8-flash-next-fp4": {
            "name": name,
            "limit": {"context": ctx, "input": ctx, "output": out},
            "variants": {lvl: {"chat_template_kwargs": {"reasoning_effort": lvl}} for lvl in levels},
            "attachment": True,
            "modalities": {"input": ["text", "image"], "output": ["text"]},
        }},
    }


def main():
    if len(sys.argv) != 6:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)
    base_url, key_file, ctx, out, name = sys.argv[1:6]
    try:
        ctx, out = int(ctx), int(out)
    except ValueError:
        print("ctx and out are token counts", file=sys.stderr)
        sys.exit(2)
    print(json.dumps({"provider": {"flash-local": provider(base_url, key_file, ctx, out, name)}}, indent=2))


if __name__ == "__main__":
    main()
