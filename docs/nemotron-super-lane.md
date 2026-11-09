# The Nemotron Super lane: NVIDIA's Nemotron 3.5 Super VL on one DGX Spark

Nemotron 3.5 Super VL is NVIDIA's open vision-language model released on 2026-10-15
(OpenMDW-1.1): a hybrid of Mamba-2, Transformer and mixture-of-experts layers (512 routed experts
per MoE layer) with a C-RADIO vision encoder, 120B parameters with 12B active per token. Its NVFP4
release, quantized with NVIDIA's Model Optimizer and refined by quantization-aware distillation,
takes 76 GB. It reads text, images and video, writes text, reasons (on by default, with a shorter
"efficient" depth on request) and calls tools, in a window of 1,048,576 tokens. This lane serves it
on the same port and behind the same proxy and key as the other text lanes, as model
`nemotron-3.5-super-vl`, with its LiLiCorr drafter. It is the Nemotron lane's big sibling: the
Nemotron lane (`docs/nemotron-lane.md`) serves Bolt, the 74B compressed from it into 24 GB.

Every number on this page was measured on the reference box (DGX Spark, GB10, the checkpoint read
from a USB SSD) on 2026-10-15 and 16, with the measurement scripts and their raw output kept beside
it.

## Which build, and why the Nemotron lane's image

NVIDIA's model card serves Super VL on a DGX Spark with the vLLM nightly `nightly-3f1bcfd8`, one
request at a time, at a memory share of 0.75, with its LiLiCorr drafter drafting 7 tokens, over the
model's whole window. The box measured that build first, then the Nemotron lane's (the vLLM nightly
`nightly-77154ca6` with NVIDIA's b12x, which install-nemotron.sh builds), each with this box's 16
images and 2 videos per conversation:

| at a share of 0.75 | KV cache | the full window fits | MemAvailable, lowest in that run |
|---|---|---|---|
| the card's image, LiLiCorr, 1 at a time | 9.61 GiB | no: 10.04 GiB needed (vLLM refuses to start) | |
| the card's image, its native MTP (3 tokens), 1 at a time | 10.63 GiB, 2.2 million tokens | yes | 7.6 GiB: the box's guard stopped it while vLLM tuned its kernels |
| the Nemotron lane's image, LiLiCorr, 1 at a time | 12.22 GiB, 1,275,937 tokens | yes, 1.22 times | 9.6 GiB |
| the Nemotron lane's image, LiLiCorr, 4 at a time | 11.18 to 13.24 GiB, 1,166,215 to 1,382,265 tokens over two boots | yes, 1.11 to 1.32 times | 9.5 GiB |
| the Nemotron lane's image, its native MTP (3 tokens), 4 at a time | 12.14 GiB, 2.5 million tokens | yes | 6.4 GiB at 0.75, 4.1 GiB at 0.70: stopped by the guard both times |

The card's image does not hold the window at the card's share once the media limits are set (they
reserve the vision encoder's room), and its kernels' tuning took the box under 8 GiB of free memory.
On the Nemotron lane's image, vLLM runs Super VL's experts with FlashInfer's CUTLASS kernels (b12x
is there, and unused by this checkpoint), with room for the whole window. Its answers are those of
the card's image within the engine's own noise (below). So the lane runs the Nemotron lane's image:
one image for the two lanes, built once by either lane's first Load, from its pins in
install-nemotron.sh, which install-nemotron-super.sh reads.

The native MTP head was measured too, as the card's other recipes use it (MTP on H100, H200 and
GB200; LiLiCorr, the card says, "for DGX Spark deployments in all scenarios"): both times its
start took the box under 8 GiB, at 0.75 and at 0.70, and the guard stopped it before a token was
served. LiLiCorr stays.

NVIDIA's model repository moved after the box measured it, by edits of its model card only
(checked on 2026-10-16): the pinned revision, of 2026-10-15 21:36 UTC, holds every file of the
revision measured as the same blob but the model card.

## The serving command

`qwen38-nemotron-super.service` runs the model card's command for a DGX Spark in the Nemotron lane's
image, with this box's port, key and memory share, and what the box measured:

```
vllm serve nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4 --revision <pinned sha> \
  --served-model-name nemotron-3.5-super-vl --trust-remote-code \
  --max-model-len 1048576 --max-num-seqs 4 --gpu-memory-utilization 0.75 --max-num-batched-tokens 8192 \
  --speculative-config.method dflash --speculative-config.model <its LiLiCorr drafter> \
  --speculative-config.revision <pinned sha> --speculative-config.num_speculative_tokens 7 \
  --mamba-backend flashinfer --mamba-ssm-cache-dtype float16 \
  --enable-mamba-cache-stochastic-rounding --mamba-cache-philox-rounds 5 --enable-mamba-shared-prefix-checkpoint \
  --block-size 64 --prefix-match-unit 64 \
  --limit-mm-per-prompt.image 16 --limit-mm-per-prompt.video 2 --video-pruning-rate 0.5 \
  --media-io-kwargs.video.fps 2 --media-io-kwargs.video.num_frames 256 \
  --allowed-media-domains media.invalid \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser nemotron_v3 \
  --config /out/engine-secrets.yaml --middleware vllm_engine_auth.RequireKey --host 127.0.0.1 --port 30000
```

What differs from the card, and why:

- **4 requests at a time** (the card says 1): agents send several. The four share the cache, which
  holds the whole window more than once; one request alone goes 1 to 4 % slower than with one at a
  time, and four make 81.7 tok/s in all against 45.6 for one.
- **16 images and 2 videos per conversation** (vLLM's default without the option is one of each in
  the card's build, and the limit counts the whole conversation a client sends again at each turn),
  and **media inside the request only**: no local file of the box, and no URL fetched (the one
  domain allowed does not exist), as on the Nemotron lane.
- **The Nemotron lane's image**, for the cache (above).
- **Its own caches** (vLLM's compile cache and FlashInfer's kernels built at their first use), in
  folders of their own, so that a boot after the first builds nothing again.
- **`PYTHONDONTWRITEBYTECODE` and `HF_MODULES_CACHE`**, as on the Nemotron lane: nothing root-owned
  left in the user's Hugging Face cache.

The KV cache is in FP8, the checkpoint's own choice (its `kv_cache_quant_algo`): against a KV cache
in BF16 (at a window of 262,144 tokens, which the BF16 cache holds at this share), the difference is
the engine's own noise (below), and FP8 holds twice the tokens. The sampling defaults (temperature
1.0, top_p 0.95) are the checkpoint's `generation_config.json`, which vLLM applies to any request
that does not set them (the card advises 0.6 for vision work: a client sets it per request). The
key reaches vLLM through `engine-secrets.yaml` and the middleware asks it everywhere but `/health`
and `/metrics`, as on the other vLLM lanes.

## Measured

| | |
|---|---|
| download | 77 GB (the model 75.1, its drafter 1.3); the image is the Nemotron lane's (10.3 GB to download, 24.4 unpacked) |
| weights in memory | 70.0 GiB |
| KV cache at 0.75, 4 at a time | 1,166,215 to 1,382,265 tokens over two boots: the whole window 1.11 to 1.32 times |
| boot from the USB SSD | 8 min 51 s to 11 min 02 s over four boots (the weights alone 5 min 44 s to 6 min 27 s) |
| MemAvailable, lowest seen | 9.1 GiB at the end of the installed lane's first boot, while vLLM builds its kernels, a browser open on the cockpit (9.5 at the end of the measurement runs' first boot; 14.1 at the end of a boot that finds them built); in service 9.3 GiB during a 1,011,142-token prefill, and 9.2 while scoring the prompt of 61,440-token documents token by token |

**Speed, one request**, real work and the reasoning off, median of 3, four requests let in at a time
(the drafter's mean acceptance 4.4 tokens per pass): 69.5 tok/s rewriting code, 35.7 summarising it,
30.7 writing prose (70.0, 37.0 and 31.8 with one request let in at a time: 1 to 4 % slower with four).

**Several requests at once**, the same three kinds of work in turn, two requests per stream: 45.6,
51.9 and 81.7 tok/s in all at 1, 2 and 4 streams.

**Long contexts.** A secret code hidden at 10, 50 and 90 % of 32,000, 128,000, 500,000 and
1,000,000 tokens (the last 1,010,603 to 1,011,142, inside the 1,048,576 of the window) was found every
time, 12 of 12. The first token came after 11 s at 32,000 tokens, 56 to 58 s at 128,000, 6 min 11 s
to 6 min 20 s at 500,000 and 19 min 22 s to 19 min 28 s at a million; and after 2.3, 4.2 to 4.5,
7.5 to 8.7 and 15.3 to 16.3 s when the same prompt came back (the prefix cache).

**An agent's turns.** A 40,000-token conversation growing 27 tokens a turn, as an agent's does, the
reasoning off and on: from the third turn all but 76 to 168 tokens came from the prefix cache, the
first token after 0.45 to 0.71 s. opencode and Claude Code, four turns with a tool each, through the
proxy: 98 to 99 % of opencode's prompts from the cache from its third turn, and 96 to 98 % of Claude
Code's from its second (3 to 7 s a turn). The second request of the first conversation after a boot
recomputes its prompt whole (13.5 s at 40,000 tokens); every later one resumes from the cache.

**Answers.** Every one of these right, through the engine's own API: the reasoning at its full and
its efficient depth and off, streamed or not; tool calls, 20 of 20 with the reasoning on and 20 of 20
with it off; German, French and English; an image; a video sent in the request; 16 images in one
conversation (the 17th refused, 400). Greedy answers to 12 fixed prompts: all right, 9 and 10 of
them word for word the card's image's.

**Quality, token by token.** The same 150 Wikipedia articles as the other lanes' (1,024 tokens each,
153,450 positions) and 4 documents of 61,440 tokens whose last 4,096 positions were scored:

| pair | KL, nats per token | change in the text's likelihood, nats per token [95 %] | same most likely token |
|---|---|---|---|
| the card's image, FP8 cache, against itself, twice (1,024 tokens) | 0.0218 | +0.0003 [-0.0008, +0.0016] | 93.0 % |
| the card's image, FP8 cache against BF16 (1,024 tokens) | 0.0216 | +0.0005 [-0.0008, +0.0017] | 93.0 % |
| the card's image, FP8 cache, against itself, twice (61,440 tokens) | 0.0207 | -0.0027 [-0.0070, +0.0006] | 92.8 % |
| the card's image, FP8 cache against BF16 (61,440 tokens) | 0.0207 | -0.0030 [-0.0064, -0.0007] | 92.7 % |
| the lane (the Nemotron lane's image, 4 at a time) against the card's image's two passes (1,024 tokens) | 0.0219 and 0.0220 | +0.0001 [-0.0011, +0.0014] and -0.0002 [-0.0015, +0.0011] | 93.1 and 93.0 % |
| the lane against the card's image's two passes (61,440 tokens) | 0.0204 and 0.0207 | -0.0049 [-0.0083, -0.0019] and -0.0023 [-0.0044, -0.0001] | 92.7 % |

The engine is far from deterministic here (a KL of 0.02 between two passes of the same build, eight
times Bolt's): its Mamba state in float16 is rounded stochastically, as the card sets it. Against
that noise the BF16 cache changes nothing measurable, and neither does the Nemotron lane's image:
the lane scores against the card's image as the card's image scores against itself (on the long
documents, the text a little more likely under the lane, by as much as two passes of the card
differ).

## Clients

Through the proxy on `:30001`, with the box's key, as model `nemotron-3.5-super-vl`, as on the
Nemotron lane: OpenAI and Anthropic dialects, tool calls parsed (`qwen3_coder`), the reasoning in the
message's `reasoning` field, and its levels in its template's words (`enable_thinking` and
`medium_effort`: the card's "off", "on (medium)" and "on"), which the proxy says for a
`reasoning_effort` (v6.40). opencode gets a `nemotron-super` provider that may attach images, with
the levels `none`, `low`, `medium` and `high` and a window of 990,000 tokens with 32,000 of output
(compaction late, and still inside the 1,048,576 the engine takes); loading the lane makes it
opencode's default. pi and omp send it their level, starting at `high`. System One needs a Qwen lane.

## Switching to it, and back

- The cockpit: **Nemotron 3.5 Super VL** in the Lanes view, then **Load**.
- A terminal: `./prepare-lane.sh nemotron-super` the first time (it builds the image both Nemotron
  lanes run, no sudo, when the Nemotron lane has not), then `./switch-model.sh nemotron-super` and
  the commands it prints.
- Installed on demand by every plain `./install.sh` on a 128 GB box (a 64 GB DGX Spark leaves it out:
  README, [On a 64 GB DGX Spark](../README.md#on-a-64-gb-dgx-spark); its units; its first Load builds the image if
  needed and fetches its weights); `--with-nemotron-super` installs it whole now
  (102 GB: 77 of checkpoint and drafter, 25 of image, 77 when the Nemotron lane is there), and
  `--no-nemotron-super` leaves it out of that run; `./install-nemotron-super.sh --uninstall` removes its
  unit, and its image unless the Nemotron lane runs it (the weights stay). The key's middleware
  stays while another vLLM lane uses it.
