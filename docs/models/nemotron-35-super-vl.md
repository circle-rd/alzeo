# Nemotron 3.5 Super VL on a DGX Spark

NVIDIA's big vision-language model, `nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4`, with a
**1M-token window**, its LiLiCorr drafter, and NVIDIA's b12x on the same vLLM image as the Bolt
lane. It holds the whole window with four requests at a time on one 128 GB box.

## One command

```bash
./switch-model.sh nemotron-super
```

## Measured on our box (ASUS Ascent GX10, 128 GB)

- **69.5 tok/s** rewriting code, **35.7 tok/s** summarising it, 30.7 writing prose, one request,
  reasoning off, median of 3, four requests let in at a time (the drafter's mean acceptance 4.4
  tokens per pass).
- **45.6, 51.9 and 81.7 tok/s in all** at 1, 2 and 4 streams (the same three kinds of work in turn,
  two requests per stream).
- A KV cache of **1,166,215 to 1,382,265 tokens** over two boots with four requests at a time, 1.11
  to 1.32 times the model's 1M window. **16 images and 2 videos per conversation.**
  The memory math and the failed first attempt are written up in
  [docs/nemotron-super-lane.md](../nemotron-super-lane.md).

## What it is good at

Vision at length: documents and screenshots inside a million-token context, video understanding,
OCR-heavy agent work. When one model must see a lot and still answer fast, this is the lane.

## In the repo

- [docs/nemotron-super-lane.md](../nemotron-super-lane.md), the lane page with its pin and its
  media limits
- [BENCHMARKS.md](../../BENCHMARKS.md), every number with its method
