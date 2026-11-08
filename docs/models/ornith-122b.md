# Ornith 1.5 122B on a DGX Spark

Ornith AI's `Ornith-1.5-122B-NVFP4`, a mixture of experts with a vision encoder, served at a window of
a million tokens on vLLM (b12x build 8244d1c) with NVIDIA's b12x kernels and its MTP head drafting 2 tokens, on one 128 GB
box.

## One command

```bash
./switch-model.sh ornith-122b
```

## Measured on our box (ASUS Ascent GX10, 128 GB)

At the lane's window of a million:

- **42.2 to 42.4 tok/s** greedy median on `./bench.sh`.
- **41.2 / 68.1 / 90.0 tok/s** in all at 1 / 4 / 8 requests on five real jobs.
- **GSM8K 98.8%** (250 questions) and **HumanEval 93.90%** (164), against 97.6% and 95.12% at its
  native window, the same box, the same drafting and the same evaluations.

The memory math of the lane and its boot proofs are in [docs/ornith-lane.md](../ornith-lane.md).

## What it is good at

A second strong generalist when the pool is free: long-context agents and vision work. The lanes
take turns in one pool, and the cockpit loads it with one button.

## In the repo

- [docs/ornith-lane.md](../ornith-lane.md), the lane page with its pin and its boot proofs
- [BENCHMARKS.md](../../BENCHMARKS.md), every number with its method
