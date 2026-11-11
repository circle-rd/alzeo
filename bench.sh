#!/usr/bin/env bash
# Measure your decode tok/s against the running qwen38-sglang service.
# Deterministic greedy probes (repeatable ±0.2) + one eval-style peak probe.
set -euo pipefail
PORT="${PORT:-30000}"
KEY_FILE="$HOME/.config/qwen38/api-key"
[ -s "$KEY_FILE" ] || { echo "API key not found at $KEY_FILE. Run ./install.sh first"; exit 1; }
for i in 1 2 3; do
  curl -s -m 5 "http://127.0.0.1:$PORT/health" >/dev/null && break
  [ "$i" = 3 ] && { echo "server not up on :$PORT after 3 tries (systemctl status qwen38-sglang)"; exit 1; }
  sleep 5
done

# Who holds a CPU core outside the engine before the run, from one window (3 s) while the engine
# waits for its first request: on a GB10 the CPU and the GPU share one power budget, and three
# command-line calls left behind by old sessions held a core each for days on another DGX Spark
# (October 2026), seen only in the load average (cpu-hogs.py).
HOGS_WINDOW="${HOGS_WINDOW:-3}"
# A cpu-hogs that died must never read as a quiet box (revue 627, 2026-10-26): the
# "2>/dev/null || true" used to swallow every failure (missing file, crash, a non-numeric
# window from the caller) and the report then claimed nothing outside the engine held a core.
# Its last line says why it failed (a window out of 0.5 to 60 s, a crash), which the report gives.
if HOGS="$(python3 "$(dirname "$0")/cpu-hogs.py" --window "$HOGS_WINDOW" 2>&1)"; then
  HOGS_FAILED=0; HOGS_ERR=""
else
  HOGS_FAILED=$?; HOGS_ERR="$(printf '%s\n' "$HOGS" | tail -n 1)"; HOGS=""
fi

KEY="$(cat "$KEY_FILE")" PORT="$PORT" HOGS="$HOGS" HOGS_FAILED="$HOGS_FAILED" HOGS_ERR="$HOGS_ERR" HOGS_WINDOW="$HOGS_WINDOW" python3 - <<'PYEOF'
import json, os, statistics, time, urllib.request

KEY, PORT = os.environ["KEY"], os.environ["PORT"]
BASE = f"http://127.0.0.1:{PORT}"

PROBES = [
    ("code (greedy, deterministic)", 0.0,
     "Write a Python class LRUCache with O(1) get/put using OrderedDict, plus a small test.", 1200),
    ("reasoning (greedy, deterministic)", 0.0,
     "A train leaves Paris at 14:00 at 160 km/h toward Lyon (465 km). Another leaves Lyon at 14:30 at 120 km/h toward Paris. When do they meet? Show your work.", 1200),
    ("math peak (temp 0.6, like SGLang's eval setting)", 0.6,
     "Natalia sold clips to 48 friends in April, half as many in May, and 3x May's amount in June. How many clips did she sell in total? Think step by step.", 2048),
    # Deliberate worst case, NOT in the median: free prose collapses draft
    # acceptance (~1.5-2.2 vs 3.3-5.6); see the README's content-dependence
    # section. If this one is slow but the others are fast, that is EXPECTED.
    ("free prose (greedy, worst-case, excluded from median)", 0.0,
     "Describe in detail a walk through an autumn forest: the colors of the leaves, the sounds, the smell after rain, and the thoughts that cross your mind.", 800),
]

# Which lane is on this port, asked of the engine rather than assumed. bench.sh
# printed the 27B header and the 27B reference line on a box serving the flash
# lane, so a perfectly normal 41.9 read as a regression against a baseline that
# belongs to another model (and the hardcoded model name only worked because
# SGLang does not enforce it).
def served_model():
    req = urllib.request.Request(BASE + "/v1/models",
                                 headers={"Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return (json.loads(r.read().decode())["data"] or [{}])[0].get("id") or ""
    except Exception:
        return ""


MODEL = served_model()
# Each lane by the name its engine serves under. "flash" in the name made the FP4-Local lane the
# SGLang flash lane, its header, reference and warm-up note above its own numbers, and any other
# name the 27B lane, whose 71.4 headed Kolibri's and the Nemotron lanes' (found in review, 2026-10-20).
LANE = {"qwen3.8-27b": "27b", "qwen3.8-flash-next": "flash",
        "qwen3.8-flash-next-fp4": "flash-local", "ornith-1.5-122b": "ornith-122b"}.get(MODEL, "other")
HEADERS = {
    # The median is the v1.14 campaign on the official release image (README, CHANGELOG
    # v1.14.0); the per-probe figures are the last ones published, v1.9's, whose median was
    # ~65. This line said ~65 after the README moved to 71.4 (found in review, 2026-09-24).
    "27b": ("Qwen3.8-27B NVFP4+DFlash2 benchmark (batch 1 decode)",
            "reference box (official release image, v1.14+): 71.4 greedy median\n"
            "  per probe as last published (v1.9, median ~65): code 64-66 / reasoning 65-66 /\n"
            "  math peak 57-71 / free prose ~23"),
    # Flash-Next, NEXTN + the reduced draft vocabulary. BENCHMARKS.md, "The
    # reduced draft vocabulary": code 47.9, math 47.1, prose EN 29.3; the
    # abliterated target measures 45.4-46.4 / 43.7-46.6 / 27.3-27.7.
    "flash": ("Qwen3.8-Flash-Next NVFP4+NEXTN benchmark (batch 1 decode)",
              "reference box (v1.8, token map 65536): code ~47.9 / math ~47.1 / prose ~29\n"
              "  the abliterated target measures 45.4-46.4 / 43.7-46.6 / 27.3-27.7\n"
              "  DISCARD THE FIRST BATCH on this lane: one batch right after a boot came in\n"
              "  at 38.8 / 39.1 / 24.8 and the next three at 47.7-49.1 (BENCHMARKS.md)"),
    # NVIDIA's FP4-Local build on vLLM with b12x and its MTP head, at xhigh, the lane's default effort
    # then: two runs on the reference box, 2026-10-20 (docs/flash-local-lane.md). This run sends no
    # effort, so it measures the lane's default, lean since v1.46.4, with no reference of its own yet
    "flash-local": ("Qwen3.8-Flash-Next FP4-Local (NVIDIA, vLLM b12x, MTP) benchmark (batch 1 decode)",
                    "reference box (2026-10-20, at xhigh, the lane's default effort then): greedy median 55.1-55.4\n"
                    "  code 56.2-57.4 / reasoning 52.8-54.3 / math peak 46.5-49.2 / free prose 33.4-35.5\n"
                    "  this run is at the lane's default effort, lean since v1.46.4: it compares with these\n"
                    "  figures only on a box that keeps xhigh (FLASH_LOCAL_EFFORT=xhigh)"),
    # Ornith AI's NVFP4 build on vLLM with b12x and its MTP head drafting 2 tokens, at the lane's
    # window of a million: two runs on the reference box, 2026-10-22 (docs/ornith-lane.md), the
    # native window's speed (42.0-42.6 there), and 43.6 / 43.5 on a second GX10 at the native window
    "ornith-122b": ("Ornith 1.5 122B NVFP4 (vLLM b12x, MTP 2, window 1M) benchmark (batch 1 decode)",
                    "reference box (2026-10-22): greedy median 42.2-42.4\n"
                    "  code 41.0-43.4 / reasoning 42.4-43.7 / math peak 41.7-43.3 / free prose 31.9-33.1"),
}


def stream(prompt, temp, max_tokens):
    body = {"model": MODEL or "qwen3.8-27b",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": temp,
            "stream": True, "stream_options": {"include_usage": True}}
    req = urllib.request.Request(BASE + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    t_first = t_last = None; n = 0; usage = {}
    try:
        resp = urllib.request.urlopen(req, timeout=900)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SystemExit("401/403: the key in ~/.config/qwen38/api-key does not match the "
                             "server's (was the service installed with a different key? re-run ./install.sh)")
        raise SystemExit(f"HTTP {e.code} from the server: {e.read()[:300]!r}")
    with resp as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            d = json.loads(line[6:])
            if d.get("usage"):
                usage = d["usage"]
            if not d.get("choices"):
                continue
            delta = d["choices"][0].get("delta", {})
            # thinking streams as reasoning_content (SGLang) or reasoning (vLLM >= 0.27);
            # missing a field name starts the clock after the thinking phase while
            # usage still counts it, inflating tok/s several-fold (issue #2)
            if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning"):
                now = time.time()
                if t_first is None:
                    t_first = now
                t_last = now; n += 1
    out = usage.get("completion_tokens") or n
    return (out - 1) / (t_last - t_first) if t_first and t_last > t_first else -1

title, reference = HEADERS.get(LANE) or (f"{MODEL or 'unknown model'} benchmark (batch 1 decode)",
                                         "no reference measured with bench.sh for this lane")
print(title)
print(f"served model: {MODEL or 'unknown (the engine did not name it)'}")
print(f"{reference}\n")
# pid, share of a core, name and command line, tab-separated, from cpu-hogs.py
HOGS = [line.split("\t") for line in os.environ.get("HOGS", "").splitlines() if line.count("\t") == 3]
if os.environ.get("HOGS_FAILED", "0") != "0":
    why = "".join(c if c.isprintable() else " " for c in os.environ.get("HOGS_ERR", ""))[:200]
    print(f"busy before the run: cpu-hogs failed (exit {os.environ['HOGS_FAILED']}{': ' + why if why else ''}), the quiet outside the engine is NOT proven, do not publish these numbers as clean\n")
elif HOGS:
    print(f"busy before the run, outside the engine (half a core or more over {os.environ['HOGS_WINDOW']} s):")
    # its pid and name, never its command line: this report gets pasted in public, and a command line can
    # hold a token (verification, 2026-10-26). cpu-hogs.py prints them, on this box, for its operator.
    for pid, share, name, cmd in HOGS:
        print(f"  {pid} {name}: {float(share):.0%} of a core")
    print("  their command lines: python3 cpu-hogs.py, on this box. The CPU and the GPU share one power budget:")
    print("  the numbers below can come out lower\n")
else:
    print("busy before the run: nothing outside the engine held half a core\n")
all_greedy = []
untimed = 0
for name, temp, prompt, mt in PROBES:
    speeds = [stream(prompt, temp, mt) for _ in range(2)]
    if temp == 0.0 and "worst-case" not in name:
        # -1 is a run the stream could not time (no token, or one): not a speed, and it went
        # into the median as one, dragging it down (found in review, 2026-09-24)
        all_greedy += [x for x in speeds if x > 0]
        untimed += sum(1 for x in speeds if x <= 0)
    print(f"  {name}: {' / '.join(f'{x:.1f}' if x > 0 else 'not timed' for x in speeds)} tok/s")
    if max(speeds) > 90:
        print("    ^ above the block-8 speculative physical ceiling for this box (~9x the")
        print("      ~11 tok/s AR floor), almost certainly a measurement artifact, not")
        print("      real speed. Cross-check with bench-matrix.sh (wall-clock method).")
if not all_greedy:
    print("\n  greedy median: none, no greedy run could be timed")
    raise SystemExit(1)
print(f"\n  greedy median: {statistics.median(all_greedy):.1f} tok/s"
      + (f" ({untimed} run(s) could not be timed and are left out)" if untimed else "")
      + (f" ({len(HOGS)} process(es) held a core before the run, see above)" if HOGS else ""))
print("  (numbers vary with content: acceptance length drives everything;")
if LANE == "27b":
    print("   math/code accept ~3.3-5.6 tokens/step, free prose ~1.5-2.2 in any")
    print("   language, see BENCHMARKS.md)")
elif LANE == "other":
    print("   no profile of this lane is measured here)")
else:
    print("   free prose costs this lane about a third of its code rate, on any")
    print("   engine and drafter measured here, see BENCHMARKS.md)")
PYEOF
