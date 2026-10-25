# Qwen3.8-Flash-Next 176B on a single DGX Spark

A 176B hybrid MoE in NVFP4 on **one** box, no second Spark, served by SGLang with its NEXTN drafter.
Its 47.7 GiB FP8 n-gram (PLE) table lives in a sparse file on NVMe while the unified pool goes to
the compute weights and a real KV cache. Three builds share the lane: `flash`, RadixArk's export and
the default, `flash-nvda`, NVIDIA's mixed-precision export, and `flash-uncensored`, an abliterated
build.

## One command

```bash
./switch-model.sh flash          # or: flash-nvda | flash-uncensored
```

NVIDIA's FP4-Local build of the same model has a lane of its own, on vLLM:
[its page](qwen38-flash-next-fp4-local.md).

## Measured on our box (ASUS Ascent GX10, 128 GB)

- **47.9 tok/s on code** and **47.1 on math** single stream at the `context` tier, 29 to 31 on
  prose, **27.0 ms/tok** on an agent loop. [Method](../../BENCHMARKS.md).
- **GSM8K 97.41%** (1,314 questions), **MMLU 90.8%** (500), **HumanEval 95.73%** (164).
- A **262,144-token window** on one box, prefill about **2,250 tok/s** cold at 27K tokens.
- **Prefix caching**: 27K tokens served again in 2.5 s against 12.0 s cold.
- Image input works, large prompts included.
- The speculative `--speculative-token-map` (upstream
  [sglang#37068](https://github.com/sgl-project/sglang/pull/37068)) is worth 14 to 25% of decode
  without changing what the model can say.

## What it is good at

Reasoning-heavy agents, code, questions on real screenshots, long documents inside the 262K window.
`flash-nvda` ties `flash` on every quality probe with its own calibration, and `flash-uncensored`
refused 0 of 5 deliberately blunt probes
([its validation](../../BENCHMARKS.md#the-abliterated-flash-target-validated-on-this-box)).

## In the repo

- [docs/flash-lane.md](../flash-lane.md), the lane page with its engine pin and its A/B history
- [BENCHMARKS.md](../../BENCHMARKS.md), every number with its method
