# Qwen3.8-27B on a DGX Spark (and any GB10 box)

Qwen's 27B in NVFP4 (`RadixArk/Qwen3.8-27B-NVFP4`), the `stock` target of [Alzeo](../../README.md)'s
27B lane: served by SGLang with a DFlash2 speculative drafter, a calibrated NVFP4 head 16 deep, on one
DGX Spark or one ASUS Ascent GX10, with a window of 1,010,000 tokens.

## One command

```bash
./switch-model.sh stock        # or: uncensored | fp8 | uncensored-fp8
```

The one-line install of the [README](../../README.md) sets it up, and the [cockpit](../cockpit.md)
loads it with a button.

## Measured on our box (ASUS Ascent GX10, 128 GB)

On 2026-11-05, the lane as v1.44.0 serves it (SGLang v0.5.22, without torch.compile):

- **44.0 tok/s alone and 80.6 in all at 4 requests** on SGLang's official benchmark (8,192 tokens in,
  1,024 out, the median of three runs).
- **65.2 and 68.5 tok/s** greedy median on `./bench.sh`, two runs.
- **GSM8K 99.2%** on 250 questions.
- Ready to answer **208 s** after its start.

The A/B against torch.compile that gave these numbers is in the
[v1.44.0 notes](../../CHANGELOG.md), the older campaigns in [BENCHMARKS.md](../../BENCHMARKS.md).

The window's limits are fitted to the KV pool each boot gets, 832,993 to 922,094 tokens over sixteen
boots here: [docs/context-1m.md](../context-1m.md). Speculation is lossless by construction, the
target verifies every drafted token, so speed costs no quality
([the losslessness study](../../BENCHMARKS.md#the-losslessness-study-2026-08-20)).

## What it is good at

General text work with long context: agent loops in Claude Code or opencode, code with files open,
summaries of whole repositories. The four targets share one serving path: `stock` for general use,
the two abliterated targets for fewer refusals, and Qwen's own FP8 release as the quantization
reference. Free prose is the slow case on any drafter, [BENCHMARKS.md](../../BENCHMARKS.md) says
by how much.

## In the repo

- [BENCHMARKS.md](../../BENCHMARKS.md), every number with its method and its failed experiments
- [docs/context-1m.md](../context-1m.md) for the 1M window
- [docs/clients.md](../clients.md) for the clients the proxy is tested with
