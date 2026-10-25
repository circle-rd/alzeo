# Qwen-Image 2.1 on a DGX Spark (text to image in 7.5 s)

`Qwen/Qwen-Image-2.1` served by SGLang Diffusion in its own venv: text to image, image editing and
native RGBA (transparent output) on the same box as your text lanes.

## One command

```bash
./switch-model.sh image          # or: image-turbo
```

The one-line install sets up its units and its first Load builds it (46 GB);
`./install.sh --with-image` builds it at install and proves it with one image.

## Measured on our box (ASUS Ascent GX10, 128 GB)

On 2026-10-10, on the v0.5.21 runtime, the server's own time and peak memory:

- **1024x1024 in 7.5 s with the Turbo**, Qwen's eight-step distillation of the same model, all
  eighteen Turbo requests of that day alike, and **34.3 s** with the base at its 40 steps (the
  median of its calibration runs, 34.4 s over all fifteen base requests of that day).
- A peak of **34.8 GiB** at 1024x1024 (45.4 GiB at 2048x2048). The cockpit starts no engine while
  another one is busy, and says which one to stop.
- The Turbo is close to the base at 40 steps on photographs and portraits in the quality battery of
  [docs/image-lane.md](../image-lane.md).

## What it is good at

Real image work on a box that is not a GPU server: product shots, portraits, legible text on signs,
transparent PNGs, edits of existing images. All local, no API, no per-image cost.

## In the repo

- [docs/image-lane.md](../image-lane.md), the lane page with the quality battery and the pins
- [BENCHMARKS.md](../../BENCHMARKS.md), every number with its method
