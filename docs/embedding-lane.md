# The embedding lane: PPLX Embed v1 0.6B, on the GPU or the CPU, behind `/v1/embeddings`

Perplexity's `pplx-embed-v1-0.6b`, behind the keepalive proxy's OpenAI-compatible
`/v1/embeddings` and the same API key. Two engines serve it:

- **llama.cpp on the GPU**, the default: 18,000 to 21,000 tokens/s for 512-token
  chunks, an 8K context, 2.6 GB of device memory, measured beside Flash-Next.
- **Text Embeddings Inference on the CPU**, the fallback: 382 tokens/s, 2.5 GiB of host
  memory, no GPU.

Only one runs at a time: they serve the same checkpoint, and the CPU lane costs 2.9 GiB of
host memory for nothing when the GPU one is there. Opt-in, and not in the one command yet:
`./install-embed.sh` installs it, from a clone, on a box where `./install.sh` ran.

```bash
./install-embed.sh                    # llama.cpp on the GPU, the default
EMBED_BOOT=1 ./install-embed.sh       # start it now, and with the box
./install-embed.sh --engine tei       # the CPU fallback
./install-embed.sh --uninstall        # the unit and the wiring; the model stays
```

The GPU mode needs Docker, a GPU and about 4 GiB free: the 1.2 GB F16 GGUF and the
llama.cpp image. The CPU mode needs about 6 GiB: TEI's 2.3 GB checkpoint and its image. The
installer proves the lane twice: directly on `127.0.0.1:30026`, then through the proxy on
its own port, both with the box's key, and checks two 1024-dimension vectors. In GPU mode
it also verifies the GGUF's pinned sha256 before the first start.

## The default: llama.cpp on the GPU

Measured on 2026-11-17 with the Flash-Next lane running, the pinned llama.cpp image
(`ghcr.io/ggml-org/llama.cpp@sha256:a5600a59...`, b12316) and the F16 GGUF of
`bowang0911/pplx-embed-v1-0.6b-gguf` (sha256 `08ba3d20...`), all 29 layers on the GB10:

```bash
llama-server -m pplx-embed-v1-0.6b-f16.gguf \
  --embedding --pooling mean \
  --override-kv qwen3.attention.causal=bool:false \
  -c 8192 -b 8192 -ub 8192 -ngl 999 -fa on
```

Two flags are mandatory, and one of them is not in `llama-server`'s help:

- `--pooling mean`: the GGUF's own pooling is `-1`; without it the vector is wrong.
- `--override-kv qwen3.attention.causal=bool:false`: the GGUF records causal attention
  (`causal attn = 1` in llama.cpp's log), but PPLX is a bidirectional encoder. `llama-server`
  has no `--attention` option; that flag belongs to `llama-embedding` only. With the GGUF as
  published, causal attention gives cosine 0.58 against TEI; with the override, raw float
  gives 0.9994, and after the model's own int8 step (`round(tanh(x) * 127)`, then L2) gives
  **0.99996**. The vectors match the reference.

The launcher adds `--embd-normalize 2`, `--api-key-file` (the box's key), `-t 4` and the
small cores. The 8K context is the co-residency setting: it keeps the GPU footprint at
2.6 GB, and a request may carry up to 8192 tokens, for example 16 chunks of 512.

## The CPU fallback: TEI, and why 2048-token batches

The fallback exists for a box whose GPU is busy or absent. TEI's defaults were measured on
the reference box on 2026-11-17, with Flash-Next loading:

| settings | memory | note |
|---|---|---|
| defaults: `--max-batch-tokens 16384`, 512 concurrent requests, 19 tokenizers | **22.4 GiB**, 33.8 GiB peak | enough to take the flash lane's load down |
| `--max-batch-tokens 2048`, 64 concurrent requests, 2 tokenizers | **2.5 GiB** | 59 ms for one short request, 1.9 s for a 64-input batch, ready in 9 s |

TEI clamps `max_input_length` to the batch budget, so 2048 tokens is also the longest text
this lane takes. That suits codebase chunks of 400 to 1000 tokens; a longer text is cut by
TEI, silently. To index longer files, raise `EMBED_BATCH_TOKENS` and `EMBED_MEM_MAX`
together, and measure: 16384 tokens costs the 22 GiB above.

`max_concurrent_requests` counts **inputs**, not requests: TEI takes one permit per input
of a batch. A 64-input request with the old default of 8 permits answered 429, "Model is
overloaded", on the first index run (2026-11-17). The installer sets 64, so one client can
send a full 64-input batch; several clients sending batches at once want a multiple of
that. `max_client_batch_size` is 64 to match.

The CPU lane keeps to the ten Cortex-A725 cores (CPUs 0-4 and 10-14), as the speech lane
does, with Docker's `--memory` cap at 4 GiB by default. A container's cgroup is Docker's,
not the systemd unit's, so the cap is on `docker run`; a unit `MemoryMax` would watch the
client alone.

## Using it

The route is the proxy's, on its usual port (30001 by default), with the box's API key.
The OpenAI SDKs work as they are:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:30001/v1", api_key="<the box's key>")
r = client.embeddings.create(model="pplx-embed-v1-0.6b", input=["def main():", "fn main() {"])
print(len(r.data), len(r.data[0].embedding))     # 2 1024
```

- **1024 dimensions**, always. The model supports Matryoshka truncation, but neither
  engine exposes it: do not send `dimensions`.
- **Compare with cosine similarity.** The CPU/TEI lane returns the model's int8 step,
  L2-normalized; the GPU lane returns an L2-normalized float vector, 0.9994 cosine from
  TEI. Both are unnormalized in the model's own terms; do not use inner product.
- **No instruction prefix** is needed or added.
- **`model` is ignored by both engines**; name `pplx-embed-v1-0.6b` for the reader's sake.
- The plural `/v1/embeddings` is OpenAI's spelling. `/v1/embedding` is kept as an alias
  for a client that wrote it the other way.

The checkpoint revision is pinned in `install-embed.sh`
(`2c4d510dd4a732063c31a0f70193e35067b51fd8`); the GPU GGUF adds its repository revision
(`ab95a3d1...`) and file sha256 (`08ba3d20...`). Store them beside an index: a moving
`main` changes the vectors, and vectors from two revisions do not compare. Keep one engine
per index, or re-embed the whole corpus when switching.

## The wiring

```text
client :30001
   |
keepalive-proxy
   |-- /v1/chat/completions, /v1/messages... -> 127.0.0.1:30000  (the text lane)
   `-- /v1/embeddings, /v1/embedding         -> 127.0.0.1:30026  (llama.cpp or TEI)
```

`install-embed.sh` writes `qwen38-embed.service`, its launcher, and a systemd drop-in for
the proxy:

```ini
# /etc/systemd/system/qwen38-keepalive.service.d/embed.conf
[Service]
Environment=EMBED_UPSTREAM=http://127.0.0.1:30026
Environment=EMBED_TIMEOUT_S=900
EnvironmentFile=-/home/<user>/.config/qwen38/embed.env
```

The proxy reads `EMBED_UPSTREAM` at start, so the installer restarts it once. Its
embedding timeout is 900 s: a 32,000-token batch takes about 86 s on the small cores, and
the default 60 s cut the proxy's connection while TEI kept working, which then answered
later requests 429 until its permits returned (seen on 2026-11-17). The box runs
the copy `install.sh` put in `~/.config/qwen38/keepalive-proxy.py`, not the clone's, so
`install-embed.sh` also installs this branch's proxy when the deployed copy has no
`EMBED_UPSTREAM` at all. Without that, a git update alone left the old proxy in memory and
`/v1/embeddings` reached the text engine, which answered 400 with SGLang's
"does not appear to be an embedding model" (seen on the reference box, 2026-11-17).

The lane has no `Conflicts=` with the text lanes: it is meant to run beside whichever one
serves, and the co-residency measurement is below. It is not started at boot unless
`EMBED_BOOT=1`, as the speech lane is not.

The same key opens both doors. `embed.env` holds `EMBED_API_KEY`, read from the box's own
`~/.config/qwen38/api-key` (0600). The TEI launcher passes it in the environment; the
llama.cpp launcher mounts the key file read-only for `--api-key-file`. Neither puts it on a
command line. The proxy validates the client's key as it always does, then signs its call
to the lane with the same value.

## Throughput, measured on the reference box

Measured on 2026-11-17, Flash-Next running and idle, through TEI on `127.0.0.1:30026`.
The inputs were 512-token chunks of this repository's `keepalive-proxy.py`, tokenized with
the checkpoint's own tokenizer, sent as one request of 8, 16, 32 or 64 inputs. One run per
row after a warmup. The lane as `install-embed.sh` starts it, on the ten Cortex-A725 cores:

| inputs | tokens | time | chunks/s | tokens/s |
|---:|---:|---:|---:|---:|
| 2 short texts | 11 | 0.06 s | 33 | 180 |
| 64 tiny code lines | 812 | 2.17 s | 29 | 374 |
| 8 x 512 | 4,096 | 10.7 s | 0.75 | 382 |
| 16 x 512 | 8,192 | 21.5 s | 0.74 | 381 |
| 32 x 512 | 16,384 | 42.8 s | 0.75 | 383 |
| 64 x 512 | 32,765 | 85.8 s | 0.75 | 382 |
| 8 x 1024 | 8,192 | 24.8 s | 0.32 | 330 |
| 8 x 2048 | 16,384 | 62.2 s | 0.13 | 264 |

The same binary on all twenty cores, for comparison, with `EMBED_CPUS` and
`EMBED_THREADS` raised:

| inputs | tokens | time | tokens/s |
|---:|---:|---:|---:|
| 8 x 512 | 4,096 | 5.9 s | 697 |
| 16 x 512 | 8,192 | 11.3 s | 725 |
| 32 x 512 | 16,384 | 22.2 s | 740 |
| 8 x 2048 | 16,384 | 32.5 s | 505 |

The small cores reach about 380 tokens/s, the whole box about 740: 1.9 times, not 2, because
the model is memory-bandwidth bound. A 0.6B model is 1.2 GFLOP per token, so 380 tokens/s is
about 460 GFLOP/s, close to what ten A725 cores can do in f32. Chunks of 512 tokens are the
fastest per token; 2048-token chunks lose a third because attention grows with the square of
the input.

For a query, the lane is fast: 60 ms for two short texts. Bulk indexing is the slow part.
At 382 tokens/s, 10M tokens take 7.3 hours, 100M take 73 hours, and 1B take 30 days; on all
twenty cores, 3.8 hours, 37.5 hours and 15.6 days.

### GPU throughput

| context | inputs | tokens | time | tokens/s | device memory (llama.cpp's projection) |
|---:|---:|---:|---:|---:|---:|
| 8K | 8 x 512 | 4,096 | 0.21 s | 19,858 | 2.6 GB |
| 8K | 16 x 512 | 8,192 | 0.39 s | 21,176 | 2.6 GB |
| 8K | 2 requests x 8 x 512 | 8,192 | 0.38 s | 21,802 | 2.6 GB |
| 32K | 64 x 512 | 32,765 | 1.53 s | 21,407 | 8.4 GB |
| 32K | 8 x 2048 | 16,384 | 0.97 s | 16,835 | 8.4 GB |

That is **47 to 56 times** the CPU lane for 512-token chunks (18,153 to 21,176 against 382
tokens/s), and a 64-chunk batch in 1.5 s instead of 86 s. At 18,000 tokens/s, 10M tokens
take 9.3 minutes, 100M take 93 minutes. The 8K context keeps the GPU footprint to 2.6 GB,
small enough to sit beside a text lane; the 32K context costs 8.4 GB and fits only while
the text lane leaves room.

The 21,176 figure is the test server's 20-thread run; the lane's launcher bounds tokenisation
to 4 threads on the small cores to leave Flash-Next's CPU work alone, and measured **18,153
tokens/s** for 16x512 beside Flash-Next. Eight threads did not improve it (17,721), so the
GPU, not tokenisation, is the limit.

The GGUF is a community conversion, not an official one: llama.cpp's converter still
rejects `PPLXQwen3Model` (`ggml-org/llama.cpp#20055`, closed as not planned). The lane pins
the file, its revision and its sha256; a conversion from the official checkpoint with a
patched converter that writes `causal_attention=false` would be preferable when it exists.

### Both at once: Flash-Next and llama.cpp embeddings

Measured on 2026-11-17 with the SGLang Flash-Next lane (`qwen38-flash.service`) and the
llama.cpp embedding server above, the latter at an 8K context and 2.6 GB of device memory.

| load order | Flash ready | available at ready | chat alone | chat + 1 x 16x512 | chat + 4 x 8x512 | embeddings alone |
|---|---:|---:|---:|---:|---:|---:|
| Flash first, then embeddings | 840 s | 12.4 GiB | 36.7 tok/s | 29.7 tok/s | 22.5 tok/s | 21.3k tok/s |
| embeddings first, then Flash | 780 s | 12.1 GiB | 28.4 tok/s | 30.9 tok/s | 21.7 tok/s | 21.2k tok/s |

Both orders work at 8K and 2.6 GB; the order is not the deciding factor. The safe default is
still **Flash first**: its load is the sensitive one (the box's 6.9 GiB floor), and starting
the small lane after `/health` leaves the most headroom. In the reverse order the embedding
server added about 3.7 GiB of used memory before the load, and Flash still reached ready.

The cost is in decode when both are busy: a single 16x512 embedding request took chat from
36.7 to 29.7 tok/s (-19%), and four concurrent 8x512 requests took it to 22.5 tok/s (-39%).
Embedding aggregate throughput fell from 21.3k to 7.0-9.5k tok/s while chat was decoding.
That is acceptable for background indexing and not for latency-sensitive chat: index in
bursts, or stop indexing while an agent is talking.

A 32K embedding context costs 8.4 GB and would leave 110 + 8.4 = 118.4 GB of the 124 GB
unified pool, with the Flash load peak close to the floor. Do not use 32K beside a loading
text lane; the 8K context is the co-residency setting.

### Idle memory, and what the 5.7 GB is

Measured with Flash stopped and the CPU/TEI embedding lane running: `free -m` showed
**used 4.9 GiB, available 119.6 GiB**. The resident processes were:

| process | RSS |
|---|---:|
| `text-embeddings` (the TEI lane) | 2.9 GiB |
| `nvidia-persistenced` | 0.6 GiB |
| cockpit, opencode, docker, containerd, other services | about 1.4 GiB |

The 11 GiB of `buff/cache` is reclaimable and is already counted in `available`; it is not
pressure. The useful lever is the TEI lane: it and the GPU llama.cpp lane serve the same
checkpoint, so running both duplicates 2.9 GiB for nothing. With it stopped the idle used
memory is about 2 GiB. `nvidia-persistenced` can be disabled for another 0.6 GiB, at the
cost of slower GPU initialisation; the rest is the OS and the cockpit. During a Flash load
the floor is 6.9 GiB, so the 2.9 GiB from TEI is exactly the kind of headroom that matters.

## Against Perplexity's cloud

Perplexity's Embeddings API publishes, for `pplx-embed-v1-0.6b`:

- **price**: $0.004 per million input tokens, so 10M tokens cost $0.04 and 1B costs $4.00
- **request shape**: up to 512 texts per request, 120,000 tokens per request, 32K per text
- **rate limits**: 85 QPS on Tier 0, 170 on Tiers 1-3, 335 on Tiers 4-5
- **latency**: OpenRouter measures Perplexity at 0.18 s P50 and 0.23 s average for a small request

The rate limit alone allows 85 x 120,000 = 10.2M tokens/s at Tier 0 if every request were
full; that is a ceiling, not a promise, but it is the scale of the service. The local lane
does 382 tokens/s, or about 4.4 orders of magnitude less; even at a conservative 100,000
tokens/s from the cloud, it is 260 times less. For 10M tokens of code, the local small cores
take 7.3 hours where the cloud bills $0.04 and answers in seconds to a few minutes.

That is the whole difference: the cloud runs a 0.6B model on GPUs with continuous batching
across customers, at $0.004 per million tokens. The box runs it on ten ARM cores in f32,
with no batching across clients, and the CPU is the limit.

## What follows

- **Queries belong on the box.** 20 ms on the GPU lane, 60 ms on the CPU one, against the
  cloud's 180 ms P50, and the vectors never leave the network.
- **Bulk indexing does not, unless privacy or offline use decides it.** Use the cloud for a
  one-off index of a large codebase, or llama.cpp on the GPU if it can have the GPU: 10M
  tokens in 8 minutes, against 7.3 hours on the CPU lane. The cloud and both local paths
  serve the same checkpoint family, but pin the revision and compare one batch before mixing
  vectors from them.
- **Chunk at 512 tokens, not 2048**, when the content allows: 45% more tokens per second.
- **Do not expect CPU tuning to close the gap.** int8 weights or an ARM64 ONNX Runtime image
  could give a factor of 2 to 4; the cloud's gap is three to four orders of magnitude.

## Why not another server

- **SGLang `--is-embedding`** would need a second full CUDA context beside the text lane.
  llama.cpp's 2.6 GB is the measured co-residency path; SGLang's mode is for a box where
  the embedding model is the only GPU tenant.
- **vLLM on the CPU** adds its scheduler and image for no measured gain on an embedding
  model; TEI is the server the model card names.
- **llama.cpp GGUF** is the lane's default: F16, 2.6 GB of GPU at 8K context, 21,000
  tokens/s, and 0.99996 cosine against TEI once the causal-attention metadata is
  overridden. It needs a pinned community GGUF, or a patched official conversion.
- **ONNX Runtime int8** would be leaner, but the official TEI image for it is amd64 only,
  and this box is arm64.
