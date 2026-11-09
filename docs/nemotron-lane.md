# The Nemotron lane: NVIDIA's Nemotron 3.5 Bolt on one DGX Spark

Nemotron 3.5 Bolt is NVIDIA's open model released on 2026-10-15 (OpenMDW-1.1): a hybrid of
Mamba-2 and Transformer layers with a latent mixture of experts, 74B parameters with 9B active per
token, compressed from Nemotron 3.5 Super VL and quantized to fit in 24 GB. Its experts are in 2 bits
(IQ2_XXS, 2.0625 bits per weight), its Mamba layers in NVFP4, the rest in Q8_0. It reads text,
images and video (a C-RADIO v4-H encoder), writes text, reasons (on by default, with a shorter
"efficient" depth on request) and calls tools, in a window of 262,144 tokens. Its card names English,
Spanish, French, German, Italian and Japanese. This lane serves it on the same port and behind the
same proxy and key as the other text lanes, as model `nemotron-3.5-bolt`, with its LiLiCorr drafter.

Every number on this page was measured on the reference box (DGX Spark, GB10, the checkpoint read
from a USB SSD) on 2026-10-15, with the measurement scripts and their raw output kept beside it.

## Why vLLM, and which build

NVIDIA's model card serves Bolt on DGX Spark with one vLLM nightly, `nightly-77154ca6` (2026-10-14),
and the b12x kernel library, which runs the 2-bit experts, the NVFP4 Mamba layers and the Q8_0
layers on the GB10 (SM121); vLLM's own code calls it (`--linear-backend b12x --moe-backend b12x`).
`install-nemotron.sh` builds the lane's image on that nightly, pinned by the digest of its arm64
manifest, plus the b12x 1.5.1 wheel checked against the sha256 PyPI publishes, installed with
`--no-deps` (the nightly carries every dependency b12x names: `pip check` shows nothing new).
Nothing else is added. The card's other step, `vllm download-kernels`, installs FlashInfer's
prebuilt kernels: this image already holds exactly those (flashinfer-cubin and flashinfer-jit-cache
0.7.0.post1, sm121a among them), so the lane starts with no internet.

NVIDIA rewrote both repositories' history when it made them public ("initial commit" at 15:42 UTC):
the revisions measured first were gone the same day, and the pinned ones hold every one of the same
files bit for bit (their blob hashes compared), the model card and one note excepted.

b12x prints at each start that its FP6 plugin needs vLLM hooks this nightly lacks; Bolt uses no FP6,
and nothing else follows from it.

## The serving command

`qwen38-nemotron.service` runs the model card's command, with this box's port, key and memory share,
and what the box measured better:

```
vllm serve nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB --revision <pinned sha> \
  --served-model-name nemotron-3.5-bolt --trust-remote-code \
  --max-model-len 262144 --max-num-seqs 16 --kv-cache-dtype fp8 --gpu-memory-utilization 0.70 \
  --linear-backend b12x --moe-backend b12x --attention-backend FLASHINFER --mamba-backend flashinfer \
  --mamba-ssm-cache-dtype bfloat16 --mamba-cache-dtype bfloat16 --seed 42 \
  --block-size 64 --prefix-match-unit 64 --enable-mamba-shared-prefix-checkpoint \
  --limit-mm-per-prompt.image 16 --limit-mm-per-prompt.video 2 --video-pruning-rate 0.5 \
  --media-io-kwargs.video.fps 2 --media-io-kwargs.video.num_frames 256 \
  --allowed-media-domains media.invalid \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder \
  --reasoning-parser-plugin <snapshot>/ultra_v3_reasoning_parser.py --reasoning-parser ultra_v3 \
  --speculative-config.method dflash --speculative-config.model <its LiLiCorr drafter> \
  --speculative-config.revision <pinned sha> --speculative-config.num_speculative_tokens 7 ... \
  --config /out/engine-secrets.yaml --middleware vllm_engine_auth.RequireKey --host 127.0.0.1 --port 30000
```

The options that take a dict are given by key: vLLM's own parser builds the same dict (checked), and
no JSON goes through systemd and bash. What differs from the card, and why:

- **CUDA graphs** at vLLM's default. The card's first version turned them off
  (`cudagraph_mode: NONE`); on, the box decoded 43 to 55 % faster with the same answers (below), and
  NVIDIA dropped that line from the card at 17:55 UTC the same day.
- **16 requests at a time** (the card says 1): agents send several. Measured below, every reply
  whole and finished at every load.
- **A memory share of 0.70.** The card sets none, and vLLM's default of 0.92 would take the whole box.
  The drafter costs the cache dearly (at 0.45: 2.0 million tokens with it, 5.2 million without), so
  0.70 holds 6.5 million tokens with the prefix cache's settings below (6,523,301 and 6,536,475
  over two boots; 4,712,014 before them), 24 requests at the full window, and leaves the box 26 GiB
  at rest, and 26.4 at its least during a 254,091-token prompt's prefill (25.3 before them).
- **The prefix cache's fine settings**, which NVIDIA's own Super VL recipe sets and Bolt's card does
  not: `--block-size 64 --prefix-match-unit 64 --enable-mamba-shared-prefix-checkpoint`. With the
  card's, the drafter's sliding window made vLLM land every prefix-cache hit on a 17,984-token
  boundary, the Mamba state kept one such step before a prompt's end, and an agent's every turn
  recomputed most of its prompt. Measured on the lane (2026-10-16), first with the card's settings,
  then with these: a 40,000-token conversation growing 27 tokens a turn, 22,055 tokens recomputed a
  turn and 13.4 s before the first token, then 8,231 and 5.0 s; opencode, four turns with a tool
  each, 47 % of its prompts from the cache and 28 s a turn, then 83 % and 11 to 12 s; Claude Code,
  whose 18,600-token requests the card's settings never served from the cache (0 %, 23 s a turn),
  then 72 to 73 % and 4 to 9 s. The same greedy answers (12 of 12 identical), the same speed, and
  the larger cache above. The drafter's window still keeps hits on 4,544-token blocks (Super VL,
  whose drafter does not, recomputes about a hundred tokens): a copy of the drafter without its
  window started a turn in 0.2 s and decoded about five times slower at 40,000 tokens of context, so
  the lane keeps NVIDIA's drafter as it is.
- **16 images and 2 videos per conversation** (the card: 1 and 1). The limit counts the whole
  conversation a client sends again at each turn: with one, the second screenshot of a session was
  refused (400), and every request after it.
- **Media inside the request only.** The card opens `--allowed-local-media-path /`, which lets any
  client with the key make the engine read any file it can see; here no local path is allowed (vLLM
  refuses one, with a 500 and the reason), and the one domain allowed for URLs does not exist, so the
  engine fetches nothing for a client (a URL is refused, 400). Images and videos sent in the request
  (base64, as Claude Code and opencode send them) are read.
- **Three caches kept** (vLLM's compile cache, FlashInfer's kernels built at first use, b12x's),
  each in its own folder of `nemotron-cache`, so that a boot after the first builds nothing again.
- **`PYTHONDONTWRITEBYTECODE` and `HF_MODULES_CACHE`**: the reasoning parser is imported from the
  checkpoint's snapshot, and transformers copies the checkpoint's code into a modules folder of the
  HF cache; vLLM, root in the container, left its `__pycache__` in the snapshot and that copy in the
  user's cache, both root-owned. The copy is made inside the container now, at each start.
- **`--trust-remote-code`**, as the card: the checkpoint's configuration and processing code, at
  the pinned revision.
- **The card's `--mamba-cache-mode align`, `--enable-chunked-prefill`, `--load-format auto` and
  `--tensor-parallel-size 1` left out**: each is this vLLM's default, and the first is deprecated
  with no effect (its own warning at every start: Mamba's prefix cache follows
  `--enable-prefix-caching`, on by default, and takes its align mode then, its log says so too).

The sampling defaults (temperature 1.0, top_p 0.95) are the checkpoint's `generation_config.json`,
which vLLM applies to any request that does not set them. The key reaches vLLM through
`engine-secrets.yaml` and the middleware asks it everywhere but `/health` and `/metrics`, as on the
Kolibri lane (`docs/kolibri-lane.md`).

## Measured

| | |
|---|---|
| download | 26 GB (the model 24.1, its drafter 1.0), and a 10.3 GB image (24.4 unpacked) |
| weights in memory | 24.1 GiB |
| KV cache at 0.70 | 6,523,301 and 6,536,475 tokens over two boots, 24 requests at the full window (4,712,014 and 18 before the prefix cache's fine settings) |
| an agent's next turn, 40,000 tokens | 31,808 from the prefix cache, 8,231 recomputed, 5.0 s before the first token (17,984, 22,055 and 13.4 s before) |
| boot from the USB SSD | 7 min 26 s to 7 min 36 s (weights 124 to 132 s, CUDA graphs for 16 requests 128 s) |
| MemAvailable, lowest seen | 26.4 GiB, a 254,091-token prompt's prefill (199.7 s), with the cockpit, opencode's web server and a test suite beside it (25.3 before the prefix cache's fine settings); 26 GiB at rest |
| GPU, hottest | 88 °C, up to 95 W, long prefills back to back |

**Speed, one request**, real work and the reasoning off, median of 3 (the drafter's mean acceptance
4.0 tokens per pass): 140 tok/s rewriting code, 63 summarising it, 50 writing prose. Without the
drafter, 31 tok/s whatever the text.

**Several requests at once**, the same three kinds of work in turn, two requests per stream, in
tok/s in all:

| streams | 1 | 4 | 8 | 16 |
|---|---|---|---|---|
| with the drafter, at the lane's 0.70 (median of 3 passes) | 74.8 | 113.4 | 163.6 | 201.2 |
| with the drafter, at 0.45 (one pass) | 72.7 | 131.8 | 158.1 | 213.2 |
| without it, at 0.45 (one pass) | 30.3 | 80.3 | 124.8 | 178.0 |

A pass varies by about a tenth at 4 streams and more (108.7 to 136.2 at 4, 192.4 to 211.1 at 16, the
three at 0.70). The drafter helps at every load measured, each of its passes above the pass without
it. On random tokens (`vllm bench serve`, 1,024 in, 512 out), which it cannot guess, it costs: 19,
41, 61 and 77 tok/s at 1, 4, 8 and 16 streams, against 30 and 203 at 1 and 16 without it.

**Long contexts.** A secret code hidden at 10, 50 and 90 % of 32,000, 128,000 and 252,000 tokens was
found every time. The first token comes after 18 s at 32,000 tokens and 192 s at 252,000 (about
1,300 to 1,800 tokens per second of prefill). The prefix cache keeps the state of the Mamba layers
only at block boundaries near the end of a prompt (4,496 tokens a block, one block back with a
drafter): an agent's conversation, sent again at each turn with a little more at its end, answered
its later turns in 5 s at 40,000 tokens against 25 s cold; the same prompt sent twice at 32,000
tokens gained nothing, and at 252,000 tokens it came back in 19 s.

**Answers.** Every one of these right, through the engine's own API: the reasoning at its full and
its efficient depth and off, streamed or not; tool calls, 20 of 20 with the reasoning on and 20 of 20
with it off, streamed or not, and the answer after a tool's result; German, French and English; an
image (a capybara reading a book by candlelight); a video sent in the request (a red fox walking
through a snowy forest at sunset); 16 images in one conversation (the 17th refused).

**Quality, token by token.** The same 150 Wikipedia articles as the Kolibri lane's (60 English, 60
German, 30 French, 1,024 tokens each, 153,450 positions), and 4 documents of 61,440 tokens whose last
4,096 positions were scored (16,384 positions, where an error in the Mamba state would have had time
to grow), with the card's precision (KV cache in FP8, Mamba state in BF16) against the full one (KV
in BF16, Mamba state in FP32, the precision the checkpoint's config names):

| pair | KL, nats per token | change in the text's likelihood, nats per token [95 %] | same most likely token |
|---|---|---|---|
| the card's precision against itself, twice (1,024 tokens) | 0.0028 | -0.0000 [-0.0005, +0.0004] | 97.3 % |
| the card's precision against the full one (1,024 tokens) | 0.0037 | +0.0003 [-0.0003, +0.0009] | 96.7 % |
| the card's precision against itself, twice (61,440 tokens) | 0.0050 | +0.0058 [+0.0005, +0.0128] | 96.4 % |
| the card's precision against the full one (61,440 tokens) | 0.0061 | -0.0090 [-0.0181, -0.0011] | 96.0 % |

The engine is not deterministic (the same precision scored twice differs as much), and the full
precision is no closer to the text than the card's, at either length. The full one also decodes 7
to 10 % slower and holds half the cache, so the card's precision stays.

**What was tried and not kept**, each measured:

- A prefill of 16,384 tokens a step instead of 2,048 (vLLM warns under 8,192): the prefill ran 1.5
  times faster, but the KV cache fell from 2.18 to 0.79 million tokens at the same memory share (the
  Mamba state needs a slot per block of each step), and the first token at 32,000 tokens came no
  sooner while its kernels compiled.
- The full precision above.
- No drafter: 2.6 times the cache, slower at every load of real work measured.
- A draft that shortens as the load grows (vLLM's `num_speculative_tokens_per_batch_size`: 7 tokens
  up to 4 requests, 5 up to 8, 3 beyond). With the CUDA graphs whole the engine did not start: its
  Mamba layers capture a graph for the full draft length only (an assertion in vLLM's
  `mamba_attn.py`). With them piecewise it ran, 7.5 % faster at 16 streams (221.2 against 205.8 tok/s,
  each pass above each of the lane's) and 6 % at 8, the same at 4, and 2.4 % slower for one request
  (136.6 against 140.0 tok/s rewriting code), the answers the same; slower alone, it is not kept.

## Clients

Through the proxy on `:30001`, with the box's key, as model `nemotron-3.5-bolt`:

- **OpenAI** (`/v1/chat/completions`, `/v1/completions`) and **Anthropic** (`/v1/messages`,
  `/v1/messages/count_tokens`) are vLLM's own.
- **Tool calls** come back parsed (`tool_calls`), streamed or not (the `qwen3_coder` parser, the
  card's).
- **Reasoning** comes in the `reasoning` field of the message. Sent back in a conversation, the
  template replays the reasoning of the turn in progress from that field (this vLLM ignores a
  `reasoning_content` there); the reasoning of earlier turns is left out, as the template does.
- **The effort.** Bolt's template reads `chat_template_kwargs: {"enable_thinking": false}` to turn
  the reasoning off and `{"medium_effort": true}` for its efficient depth, and no reasoning effort: a
  `reasoning_effort` sent as it came left the answer inside the reasoning, its content empty. The
  proxy says the levels in the template's words (v6.40): `none` and `minimal` turn the reasoning off,
  `low` and `medium` ask the efficient depth, `high` and above the full one; a request that names
  `enable_thinking` itself keeps its choice.
- **Any model name works**, as on the other lanes: vLLM refuses a model it does not serve, so the
  proxy gives such a request the served name.
- **opencode** gets a `nemotron` provider that may attach images, with the levels `none`, `low`,
  `medium` and `high`; loading the lane makes it opencode's default. **pi** sends it its effort and
  starts at `high`.
- **System One** needs a Qwen lane: its options are measured on the Qwen tokenizer. On this lane
  the proxy says so (400) and the cockpit's Decide view offers to load the flash lane (the 27B on a
  64 GB box, which holds no flash lane).

## Switching to it, and back

- The cockpit: **Nemotron 3.5 Bolt** in the Lanes view, then **Load**.
- A terminal: `./prepare-lane.sh nemotron` the first time (it builds its image, no sudo), then
  `./switch-model.sh nemotron` and the commands it prints.
- Installed on demand by every plain `./install.sh` (its units; its first Load builds it and fetches
  its weights); `--with-nemotron` installs it whole now (51 GB: 26 of checkpoint and drafter, 25 of
  image), and `--no-nemotron` leaves it out of that run;
  `./install-nemotron.sh --uninstall` removes its unit, and its local image unless the Nemotron Super
  lane runs it too (the weights stay, and so does the vLLM image it was built on, which the script
  names for you to remove). The key's middleware stays while another vLLM lane (Kolibri, Nemotron
  Super, Qwen3.8 Flash-Next FP4-Local, Ornith) uses it.
