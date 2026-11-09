# The Kolibri lane: Aleph Alpha's Kolibri-1 on one DGX Spark

Kolibri-1 is Aleph Alpha's open model (Apache 2.0, released 2026-10-03): a mixture of experts of
78.1B parameters with 3.46B active per token (384 routed experts, 6 per token, and one shared
expert), 50 layers of which 40 use a 513-token sliding window and 10 full attention, its weights
in FP8 with 128x128 block scales, and a native context of 262,144 tokens. It is built for German
and English, with explicit reasoning (effort `low`, `medium`, `high` or `none`) and tool calling.
This lane serves it on the same port and behind the same proxy and key as the Qwen lanes, so every
client reaches it the same way, as model `kolibri-1`.

Every number on this page was measured on the reference box (DGX Spark, GB10, the checkpoint read
from a USB SSD) on 2026-10-14, with the measurement scripts and their raw output kept beside it.

## Why vLLM, and which build

SGLang cannot load Kolibri ([sgl-project/sglang#43256](https://github.com/sgl-project/sglang/issues/43256),
open). Aleph Alpha serves it with vLLM and their plugin,
[aleph-alpha-inference](https://github.com/Aleph-Alpha/aleph-alpha-inference) 1.0.0, which registers
the `Kolibri1ForCausalLM` architecture and the `kolibri1` reasoning and tool-call parsers, and pins
vLLM 0.29.x. Their container is published for amd64 only, so `install-kolibri.sh` builds the same
one here: their Dockerfile, on the image their `FROM` line names (`vllm/vllm-openai:v0.29.0`, whose
arm64 manifest is pinned by digest), plus the plugin's wheel, checked against the sha256 PyPI
publishes for it. Nothing else is added.

| build | what happened on the GB10 |
|---|---|
| `vllm/vllm-openai:v0.29.0` (CUDA 13.0), pinned | serves; the reference below |
| `vllm/vllm-openai:v0.29.0-aarch64-cu129` (CUDA 12.9) | dies at its first FP8 GEMM: `cutlass_gemm_caller ... Error Internal`. Its block-FP8 kernels are built for `sm_120a`, which the GB10 (`sm_121`) cannot run; the CUDA 13.0 image carries `sm_120` without the `a` |
| `vllm/vllm-openai:v0.31.0` with the same plugin | the token log-probabilities of 153,450 positions are identical to the bit, and the speed is the same (48.4, 161.7 and 201.6 tok/s at 1, 8 and 16 streams): nothing to gain, and outside the plugin's pin |
| vLLM's own Kolibri support ([vllm#60827](https://github.com/vllm-project/vllm/pull/60827), merged 2026-10-13) | needs a transformers release that does not exist yet (the model was added to transformers on 2026-10-13, after 5.19.0); its own measurements, on a B200, put it 6 % slower at one stream than a native implementation proposed beside it ([vllm#60026](https://github.com/vllm-project/vllm/pull/60026), closed) |

## The serving command

`qwen38-kolibri.service` runs the model card's command, with this box's port, key and memory share:

```
vllm serve <checkpoint> --revision <pinned sha> --served-model-name kolibri-1 \
  --max-model-len 262144 --kv-cache-dtype fp8 --gpu-memory-utilization <share> --max-num-seqs 16 \
  --reasoning-parser kolibri1 --tool-call-parser kolibri1 --enable-auto-tool-choice \
  --config /out/engine-secrets.yaml --host 127.0.0.1 --port 30000
```

- **Sampling**: Aleph Alpha recommends temperature 1.0, top_p 0.97 and top_k 128, and the
  checkpoint's `generation_config.json` carries exactly that; vLLM applies it to every request that
  does not set its own, and says so at boot (`Default vLLM sampling parameters have been overridden
  by the model's generation_config.json: {'temperature': 1.0, 'top_k': 128, 'top_p': 0.97}`).
- **Reasoning** is on by default, at the effort its template defaults to (high). A request sets it
  with `chat_template_kwargs: {"reasoning_effort": "low" | "medium" | "high" | "none"}`, or the
  top-level `reasoning_effort`, or `chat_template_kwargs: {"enable_thinking": false}`. Measured: the
  same question took 249 tokens at low and 634 at high; at none it answers at once.
- **The window** is the 262,144 tokens Aleph Alpha recommends. The model can extrapolate to
  1,048,576, which they advise against for complex tasks, and this lane does not serve.
- **The key** reaches vLLM the way it reaches SGLang: `engine-secrets.yaml`, read with `--config`,
  so it is on no command line; vLLM prints its arguments at boot with `api_key` masked. vLLM
  itself asks it only under `/v1`, `/v2`, `/inference` and `/cohere`: `/invocations` (its SageMaker
  route, which runs chat and completions), `/tokenize` and `/detokenize` answered without it. A
  middleware loaded with `--middleware` (`vllm_engine_auth.py`) asks it everywhere but `/health`
  and `/metrics`, as the SGLang lanes do.
- **Usage statistics**: vLLM reports anonymous usage to its authors by default; the unit turns that
  off (`VLLM_NO_USAGE_STATS=1`, `DO_NOT_TRACK=1`).

## The two checkpoints

| | `kolibri` (default) | `kolibri-nvfp4` |
|---|---|---|
| checkpoint | [Aleph-Alpha/Kolibri-1](https://huggingface.co/Aleph-Alpha/Kolibri-1), the official FP8 | [Quantinine/Kolibri-1-NVFP4A16-FP8](https://huggingface.co/Quantinine/Kolibri-1-NVFP4A16-FP8): the routed experts in NVFP4 (4-bit weights, 16-bit activations), attention, shared expert, router, embeddings and output layer the official FP8 bit for bit |
| download | 78.9 GB | 45.8 GB (at its first switch) |
| weights in memory | 73.55 GiB | 42.77 GiB |
| memory share | 0.80 | 0.55 |
| KV cache | 2,093,056 to 2,175,959 tokens (3 boots) | 1,997,166 and 2,014,877 tokens (2 boots) |
| boot from the USB SSD | 8 min 40 s to 10 min 03 s (weights 440 to 525 s, 3 boots) | 6 min 34 s and 7 min 10 s (weights 309 s and 338 s) |
| decode, 1 / 4 / 8 / 16 streams | 48.0 / 121.9 / 165.8 / 202.0 tok/s | 52.9 / 157.7 / 248.2 / 377.2 tok/s |
| prefill of 32K / 128K tokens | 7.7 s / 48.5 s | 6.7 s / 40.6 s |
| MemAvailable, lowest seen | 10.9 GiB | 48.1 GiB |

Each memory share keeps about the same cache, 2.0 to 2.2 million tokens, 7.6 to 8.3 requests at
the full window (it moves by a few percent from one boot to the next, as on SGLang): the NVFP4's
30.8 GiB of weights saved go back to the box rather than to a cache nobody fills. The lowest
MemAvailable came with the measurement scripts running on the box beside the engine (benchmark
client, log-probability analysis); the lane's own clients and a 96,252-token prompt left 15.5 GiB.

Speeds and prefills were measured with `vllm bench serve` on the same image and flags, before the
lane existed, the NVFP4 at memory share 0.80 (16 streams of these lengths fit either cache); the FP8
lane decodes one stream through its proxy at 47.9 to 48.2 tok/s, the same.

Long contexts stay fast on either, the sliding-window layers costing little past 513 tokens: the
FP8 decodes at 46, 41 and 36 tok/s with 32K, 128K and 250K tokens of context, and prefills 250,000
tokens in 94.5 s.

**Quality, token by token.** Both checkpoints scored the same 150 Wikipedia articles (60 English, 60
German, 30 French, the first 1,024 tokens of each, 153,450 positions), the log-probability of every
token and the 20 most probable at each position:

| pair | KL, nats per token | change in the text's likelihood, nats per token [95 %] | same most likely token |
|---|---|---|---|
| FP8 against itself, twice, one article at a time or 8 at once | 0.0000 | +0.0000 (identical to the bit) | 100 % |
| NVFP4 against itself, twice | 0.0379 | +0.0002 [-0.0032, +0.0035] | 92.1 % |
| NVFP4 against FP8, all | 0.0627 | +0.0111 [+0.0052, +0.0170] | 89.4 % |
| of which English | 0.0643 | +0.0059 [-0.0020, +0.0132] | 90.0 % |
| of which German | 0.0552 | +0.0034 [-0.0036, +0.0106] | 89.6 % |
| of which French | 0.0746 | +0.0369 [+0.0205, +0.0546] | 87.7 % |

The FP8 is deterministic here, even with several requests batched; the NVFP4 is not (its Marlin
kernel's reductions vary from run to run), and two thirds of its distance from the FP8 is that
noise. In English and German the text is as likely under either checkpoint, which agrees with
Quantinine's own evaluation, made on the official model's benchmarks with the same stack (21 of 24
rows level, AIME, MMLU-Pro and agentic suites included); in French, which Kolibri was not built for,
the NVFP4 is measurably weaker (about 3.8 % of perplexity). So the FP8 is the default, and the NVFP4
the lane's second checkpoint, for many requests at once.

**What was tried and not kept**, each measured:

- Reading the checkpoint with 8 threads (`enable_multithread_load`): 588 s instead of 440 from the
  USB SSD, slower.
- vLLM's GB10 table for its Triton MoE kernel (shipped for 512 experts, copied for 384): the
  log-probabilities identical to the bit, the speed within noise (48.5, 118.5, 164.6, 207.4 tok/s).

## Clients

Through the proxy on `:30001`, with the box's key, as model `kolibri-1`:

- **OpenAI** (`/v1/chat/completions`, `/v1/completions`) and **Anthropic** (`/v1/messages`,
  `/v1/messages/count_tokens`) are vLLM's own.
- **Tool calls** come back parsed (`tool_calls`), streamed or not, Hermes-style as Aleph Alpha
  documents. Measured: a weather tool called with `{"city": "Berlin", "unit": "celsius"}`, and the
  answer after its result.
- **Reasoning** comes in the `reasoning` field of the message (vLLM 0.29's name for it; the Qwen
  lanes say `reasoning_content`). opencode reads either.
- **Any model name works**, as on the Qwen lanes: vLLM refuses a model it does not serve, so the
  proxy gives such a request the served name (once per name, it says so in its journal).
- **opencode** gets a `kolibri` provider, text only (Kolibri has no vision), with the levels
  `none`, `low`, `medium` and `high`; loading the lane makes it opencode's default.
- **System One** needs a Qwen lane: its options are measured on the Qwen tokenizer. On this lane
  the proxy says so (400) and the cockpit's Decide view offers to load the flash lane.

A known upstream issue of the `kolibri1` reasoning parser, measured by Quantinine on both
checkpoints: when the model writes a literal `<tool_call>` inside its reasoning, the parser ends the
reasoning there and the tool parser then misses the call. On one English request at high effort it
hit 1.9 % of non-streamed replies with the FP8 and 3.5 % with the NVFP4; with the FP8, never on the
other requests they tested. The call the model wrote after its reasoning is then left in the content.

## Switching to it, and back

- The cockpit: **Kolibri-1** in the Lanes view, **FP8** or **NVFP4**, then **Load**.
- A terminal: `./prepare-lane.sh kolibri` the first time (it builds its image, no sudo), then
  `./switch-model.sh kolibri` (or `kolibri-nvfp4`) and the commands it prints.
- Installed on demand by every plain `./install.sh` on a 128 GB box (its units; its first Load builds it;
  a 64 GB DGX Spark leaves it out: README, [On a 64 GB DGX Spark](../README.md#on-a-64-gb-dgx-spark));
  `--with-kolibri` installs it whole now, and `--no-kolibri` leaves it out of the run that passes
  it (102 GB); `./install-kolibri.sh --uninstall` removes its unit and its local image (the weights stay, and so does the 23 GB vLLM image it was built on, which the script names for you to remove).
