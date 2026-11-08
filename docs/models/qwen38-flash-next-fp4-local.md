# Qwen3.8 Flash-Next FP4-Local on a DGX Spark (NVIDIA's FP4 build)

`nvidia/Qwen3.8-Flash-Next-FP4-Local`, NVIDIA's own build of Qwen3.8-Flash-Next for one local GPU:
its experts in NVFP4 and its other layers in MXFP8, refined by quantization-aware distillation, its
n-gram (PLE) table in NVFP4 inside the checkpoint's own files. This stack serves it on vLLM (b12x build 8244d1c) with
NVIDIA's b12x kernels and its MTP head drafting 4 tokens, as a lane of its own beside the
[flash lane](qwen38-flash-next.md), which serves the same model on SGLang. The lane comes from
Dimitris Kontokostas's pull request #55.

## One command

```bash
./switch-model.sh flash-local
```

## Measured on our box (ASUS Ascent GX10, 128 GB)

- **55.4 tok/s** greedy median on `./bench.sh`, at its default reasoning effort, `xhigh`.
- At the `lean` effort: **47.4 tok/s alone** and **106.6 in all at 8 requests** on real text at the
  agents' sampling, **GSM8K 97.64%** (1,314 questions) and **HumanEval 98.17%** (164).
- A KV cache of **921,090 to 984,833 tokens** in FP8, three and a half times its 262,144-token window.
- **Reads images**: a solid red and a solid blue test image, each named right (2026-11-06).
- Against the flash lane: 102.3 GB of checkpoint on disk against 135.3, nothing written at boot (its
  n-gram table is read in place), a later boot answering in about 5 min 30 s against 12 to 15 min.

## What it is good at

The same model as the flash lane, with more room: twice its KV cache, three and a half whole windows
at once, some 7 GiB more of free memory for the box in service, and a prefill about 30% faster. The
flash lane is ahead when eight requests run at once on real text (123.7 tok/s in all against 106.6).

## In the repo

- [docs/flash-local-lane.md](../flash-local-lane.md), the lane page with the two flash lanes side by
  side, its pins and its measurements
- [BENCHMARKS.md](../../BENCHMARKS.md), every number with its method
