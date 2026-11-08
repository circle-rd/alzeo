# Which model to run on your DGX Spark

One page per model Alzeo serves, each with the one command that loads it, the numbers measured on
the reference box, an ASUS Ascent GX10 (128 GB), and where their method lives. The numbers come
from [BENCHMARKS.md](../../BENCHMARKS.md) and the lane pages it links, nothing here is estimated.

| Model, as the cockpit names it | Engine | Page | One command | Headline |
|---|---|---|---|---|
| Qwen3.8 27B NVFP4 · RadixArk, with its DFlash2 draft | SGLang 0.5.22 | [qwen38-27b.md](qwen38-27b.md) | `./switch-model.sh stock` | 44.0 tok/s alone and 80.6 at 4 on 8K-token prompts, 1M window |
| Qwen3.8 Flash-Next NVFP4 · RadixArk (176B) | SGLang nightly 53295c6 | [qwen38-flash-next.md](qwen38-flash-next.md) | `./switch-model.sh flash` | 47.9 tok/s on code, on one box |
| Qwen3.8 Flash-Next FP4-Local · NVIDIA | vLLM b12x build 8244d1c | [qwen38-flash-next-fp4-local.md](qwen38-flash-next-fp4-local.md) | `./switch-model.sh flash-local` | 55.4 tok/s, twice the flash lane's KV cache |
| Nemotron 3.5 Bolt 24GB · NVIDIA | vLLM nightly 77154ca6 + b12x 1.5.1 | [nemotron-35-bolt.md](nemotron-35-bolt.md) | `./switch-model.sh nemotron` | 140 tok/s on code alone, the default of a 64 GB box |
| Nemotron 3.5 Super VL NVFP4 · NVIDIA | vLLM nightly 77154ca6 + b12x 1.5.1 | [nemotron-35-super-vl.md](nemotron-35-super-vl.md) | `./switch-model.sh nemotron-super` | 69.5 tok/s on code alone, a 1M window with images and video |
| Kolibri-1 FP8 · Aleph Alpha (German) | vLLM 0.29.0 + Aleph Alpha plugin 1.0.0 | [kolibri-1.md](kolibri-1.md) | `./switch-model.sh kolibri` | 48 tok/s alone, German-first |
| Ornith 1.5 122B NVFP4 · Ornith AI | vLLM b12x build 8244d1c | [ornith-122b.md](ornith-122b.md) | `./switch-model.sh ornith-122b` | 42.4 tok/s, vision, a 1M window |
| Qwen-Image 2.1 · Qwen | SGLang Diffusion 0.5.21 | [qwen-image-21.md](qwen-image-21.md) | `./switch-model.sh image` | 1024x1024 in 7.5 s with its Turbo |
| MiniMax-H3 · MiniMax, video with sound | SGLang Diffusion 0.5.21 | [minimax-h3.md](minimax-h3.md) | `./switch-model.sh video` | 4 s of 480P in about 127 s with its Turbo |

Each engine is the version the lane's pinned image reads on the reference box (the cockpit says the same, and the
version its engine reports for the lane that serves).

Speech (Qwen3-ASR, Kokoro) is opt-in and runs on the CPU, see
[docs/speech-lane.md](../speech-lane.md). The lanes share one pool of unified memory and take turns
in it, and the [cockpit](../cockpit.md) loads one with a button.
