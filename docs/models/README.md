# Which model to run on your DGX Spark

One page per model Alzeo serves, each with the one command that loads it, the numbers measured on
the reference box, an ASUS Ascent GX10 (128 GB), and where their method lives. The numbers come
from [BENCHMARKS.md](../../BENCHMARKS.md) and the lane pages it links, nothing here is estimated.

| Model | Page | One command | Headline |
|---|---|---|---|
| Qwen3.8-27B (NVFP4, DFlash2) | [qwen38-27b.md](qwen38-27b.md) | `./switch-model.sh stock` | 44.0 tok/s alone and 80.6 at 4 on 8K-token prompts, 1M window |
| Qwen3.8-Flash-Next 176B on SGLang | [qwen38-flash-next.md](qwen38-flash-next.md) | `./switch-model.sh flash` | 47.9 tok/s on code, on one box |
| Flash FP4-Local, NVIDIA's build on vLLM | [qwen38-flash-next-fp4-local.md](qwen38-flash-next-fp4-local.md) | `./switch-model.sh flash-local` | 55.4 tok/s, twice the flash lane's KV cache |
| Nemotron 3.5 Bolt | [nemotron-35-bolt.md](nemotron-35-bolt.md) | `./switch-model.sh nemotron` | 140 tok/s on code alone, the default of a 64 GB box |
| Nemotron 3.5 Super VL | [nemotron-35-super-vl.md](nemotron-35-super-vl.md) | `./switch-model.sh nemotron-super` | 69.5 tok/s on code alone, a 1M window with images and video |
| Kolibri-1 (German) | [kolibri-1.md](kolibri-1.md) | `./switch-model.sh kolibri` | 48 tok/s alone, German-first |
| Ornith 1.5 122B | [ornith-122b.md](ornith-122b.md) | `./switch-model.sh ornith-122b` | 42.4 tok/s, vision, a 1M window |
| Qwen-Image 2.1 | [qwen-image-21.md](qwen-image-21.md) | `./switch-model.sh image` | 1024x1024 in 7.5 s with its Turbo |
| MiniMax-H3, video with sound | [minimax-h3.md](minimax-h3.md) | `./switch-model.sh video` | 4 s of 480P in about 127 s with its Turbo |

Speech (Qwen3-ASR, Kokoro) is opt-in and runs on the CPU, see
[docs/speech-lane.md](../speech-lane.md). The lanes share one pool of unified memory and take turns
in it, and the [cockpit](../cockpit.md) loads one with a button.
