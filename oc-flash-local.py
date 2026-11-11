#!/usr/bin/env python3
"""The Flash FP4-Local lane's opencode provider, in one place.

install.sh's generator writes it into the opencode config once the lane is installed, and
switch-model.sh adds it to a config written before that, so both take it from here and the two
cannot drift.

Usage: oc-flash-local.py <base_url> <api-key file> <ctx> <out> <name>
prints {"provider": {"flash-local": ...}}, the shape oc-merge-limits.py --add-providers reads.
"""
import json
import sys

# The checkpoint's chat template is the Qwen3.8 one the flash lane serves, byte for byte, patched
# the same way (patch-template.py), so the same four levels. Their order is the one this lane's
# provider has had since v1.37.0, xhigh first, its default then: the default is lean since v1.46.4
# (install-flash-local.sh), and the order stays, as opencode sends no level when none is picked, so
# it decides no request, and another one would have rewritten the generated config and restarted
# the Agent view's server on every box at its update.
LEVELS = ("xhigh", "lean", "low", "medium")


def provider(base_url, key_file, ctx, out, name):
    """The provider block. The model reads images, as the flash lane's does."""
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Qwen3.8-Flash-Next FP4-Local (DGX Spark)",
        "options": {"baseURL": base_url, "apiKey": f"{{file:{key_file}}}"},
        "models": {"qwen3.8-flash-next-fp4": {
            "name": name,
            "limit": {"context": ctx, "input": ctx, "output": out},
            "variants": {lvl: {"chat_template_kwargs": {"reasoning_effort": lvl}} for lvl in LEVELS},
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
