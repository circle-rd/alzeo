# Nemotron 3.5 Bolt on a DGX Spark (64 GB boxes too)

NVIDIA's Nemotron 3.5 Bolt, which reads images and video and reasons, served on the
[Nemotron lane](../nemotron-lane.md) on vLLM with NVIDIA's b12x kernels and its drafter. A new 64 GB
Spark ends its install on it.

## One command

```bash
./switch-model.sh nemotron
```

## Measured on our box (ASUS Ascent GX10, 128 GB)

- **140 tok/s** rewriting code, one request, reasoning off.
- **74.8 / 113.4 / 163.6 / 201.2 tok/s** in all at 1 / 4 / 8 / 16 streams.
- Reads **images and video** on the same endpoint.
- At the memory a 64 GB box gives it (measured on the reference box on 2026-10-24, real 64 GB
  owners' numbers welcome): **110.8 tok/s** greedy median on `./bench.sh`, a KV cache of
  **667,874 to 721,883 tokens** over two boots, 2.55 to 2.75 times its 262,144-token window. The
  64 GB story is in the
  [v1.40.0 release notes](https://github.com/hasso5703/alzeo/releases/tag/v1.40.0).

## What it is good at

Agentic speed: tool calling loops, quick vision questions, anything where the time of each step
matters more than depth. It is the lane that makes a 64 GB Spark useful out of the box.

## In the repo

- [docs/nemotron-lane.md](../nemotron-lane.md), the lane page with its engine pin
- [BENCHMARKS.md](../../BENCHMARKS.md), every number with its method
