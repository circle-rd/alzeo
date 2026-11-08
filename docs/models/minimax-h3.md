# MiniMax-H3 on a DGX Spark (video with sound)

`MiniMaxAI/MiniMax-H3`, text to video **with sound**, served by SGLang Diffusion 0.5.21 in its own venv on
the same box as your text lanes, with none of the offload flags.

## One command

```bash
./switch-model.sh video
```

The one-line install sets up its units and its first Load builds it (about 174 GB of headroom);
`./install.sh --with-video` builds it at install and proves it, in about 23 min.

## Measured on our box (ASUS Ascent GX10, 128 GB)

On 2026-10-10, on the v0.5.21 runtime, 4 s at 480P, three prompts, the server's time end to end:

- **622.8 to 632.5 s** with the base at 50 steps.
- **126.2 to 127.9 s** with the Turbo adapter, which the Video view's Speed choice puts on.

The SGLang cookbook verifies MiniMax-H3 on the DGX Spark too, with no flags at all. The lane page
carries the A/B of the offload flags and why we serve none: [docs/video-lane.md](../video-lane.md).

## What it is good at

Short video with audio, generated locally: demos, social clips, prototypes. It is the slowest lane
by nature and it says so: the lane page says what a request costs, and the cockpit keeps the pool
safe around it.

## In the repo

- [docs/video-lane.md](../video-lane.md), the lane page with its flags study, the Turbo and its pins
- [BENCHMARKS.md](../../BENCHMARKS.md), every number with its method
