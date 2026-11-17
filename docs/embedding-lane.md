# The embedding lane: PPLX Embed v1 0.6B on the CPU, behind `/v1/embeddings`

Perplexity's `pplx-embed-v1-0.6b` in Text Embeddings Inference (TEI), on the CPU beside
whichever text lane serves, behind the keepalive proxy's OpenAI-compatible
`/v1/embeddings` and the same API key. Opt-in, and not in the one command yet:
`./install-embed.sh` installs it, from a clone, on a box where `./install.sh` ran.

```bash
./install-embed.sh                  # TEI, the model, the unit and the proxy route, proved before it ends
EMBED_BOOT=1 ./install-embed.sh     # start it now, and with the box
./install-embed.sh --uninstall      # the unit and the wiring; the model cache stays
```

It needs Docker and about 6 GiB free. The image is a 117 MB pull, the model 2.3 GB. The
installer proves the lane twice: directly on `127.0.0.1:30026`, then through the proxy on
its own port, both with the box's key, and checks two 1024-dimension vectors.

## Why the CPU, and why 2048-token batches

The box serves one GPU lane at a time, and Flash-Next holds almost all of the unified
memory. On 2026-11-17, with Flash-Next loading, TEI's defaults were measured on the
reference box:

| settings | memory | note |
|---|---|---|
| defaults: `--max-batch-tokens 16384`, 512 concurrent requests, 19 tokenizers | **22.4 GiB**, 33.8 GiB peak | enough to take the flash lane's load down |
| `--max-batch-tokens 2048`, 8 concurrent requests, 2 tokenizers | **2.5 GiB** | 59 ms for one short request, ready in 9 s |

TEI clamps `max_input_length` to the batch budget, so 2048 tokens is also the longest text
this lane takes. That suits codebase chunks of 400 to 1000 tokens; a longer text is cut by
TEI, silently. To index longer files, raise `EMBED_BATCH_TOKENS` and `EMBED_MEM_MAX`
together, and measure: 16384 tokens costs the 22 GiB above.

The lane keeps to the ten Cortex-A725 cores (CPUs 0-4 and 10-14), as the speech lane does,
with Docker's `--memory` cap at 4 GiB by default. A container's cgroup is Docker's, not the
systemd unit's, so the cap is on `docker run`; a unit `MemoryMax` would watch the client
alone.

## Using it

The route is the proxy's, on its usual port (30001 by default), with the box's API key.
The OpenAI SDKs work as they are:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:30001/v1", api_key="<the box's key>")
r = client.embeddings.create(model="pplx-embed-v1-0.6b", input=["def main():", "fn main() {"])
print(len(r.data), len(r.data[0].embedding))     # 2 1024
```

- **1024 dimensions**, always. The model supports Matryoshka truncation, but TEI exposes
  the full vector only: do not send `dimensions`.
- **Unnormalized, int8-quantized vectors**, as Perplexity trained them. Compare with
  cosine similarity, not inner product.
- **No instruction prefix** is needed or added.
- **`model` is ignored by TEI**; name `pplx-embed-v1-0.6b` for the reader's sake.
- The plural `/v1/embeddings` is OpenAI's spelling. `/v1/embedding` is kept as an alias
  for a client that wrote it the other way.

The model revision is pinned in `install-embed.sh`
(`2c4d510dd4a732063c31a0f70193e35067b51fd8`). Store it beside an index: a moving `main`
changes the vectors, and vectors from two revisions do not compare.

## The wiring

```text
client :30001
   |
keepalive-proxy
   |-- /v1/chat/completions, /v1/messages... -> 127.0.0.1:30000  (the text lane)
   `-- /v1/embeddings, /v1/embedding         -> 127.0.0.1:30026  (TEI)
```

`install-embed.sh` writes `qwen38-embed.service`, its launcher, and a systemd drop-in for
the proxy:

```ini
# /etc/systemd/system/qwen38-keepalive.service.d/embed.conf
[Service]
Environment=EMBED_UPSTREAM=http://127.0.0.1:30026
EnvironmentFile=-/home/<user>/.config/qwen38/embed.env
```

The proxy reads `EMBED_UPSTREAM` at start, so the installer restarts it once. The box runs
the copy `install.sh` put in `~/.config/qwen38/keepalive-proxy.py`, not the clone's, so
`install-embed.sh` also installs this branch's proxy when the deployed copy has no
`EMBED_UPSTREAM` at all. Without that, a git update alone left the old proxy in memory and
`/v1/embeddings` reached the text engine, which answered 400 with SGLang's
"does not appear to be an embedding model" (seen on the reference box, 2026-11-17).

The lane itself has no `Conflicts=` with the text lanes: it never touches the GPU. It is
not started at boot unless `EMBED_BOOT=1`, as the speech lane is not: at rest it holds
2.5 GiB of host memory that a flash lane's long prompts also want.

The same key opens both doors. `embed.env` holds `EMBED_API_KEY`, read from the box's own
`~/.config/qwen38/api-key` (0600), and the launcher gives TEI that key in its environment,
never on its command line. The proxy validates the client's key as it always does, then
signs its call to TEI with the same value.

## Measured, and not

Measured on the reference box on 2026-11-17: the two memory rows above, one short request
at 59 ms, 1024 dimensions, `/v1/embeddings` on TEI 1.9.4, and the proxy's relay with the
box's key. Not measured: throughput on a real codebase, batches of 32 to 64 chunks, long
inputs at 4096 or 8192 tokens, and what a concurrent indexing run costs the text lane.
Those need a run on the box, and the answer to the second decides whether 2048 is the
right ceiling.

## Why not another server

- **SGLang `--is-embedding`** is the fastest on a GPU, but the GPU is the text lane's.
  Beside Flash-Next, a new CUDA context can fail to start, and a busy GPU side lane took
  45% of the flash lane's decode in the speech measurement. It would be right on a second
  box, or with the text lane stopped.
- **vLLM on the CPU** adds its scheduler and image for no measured gain on an embedding
  model; TEI is the server the model card names.
- **llama.cpp GGUF** is small (0.6 GB at Q8) but the PPLX architecture is not officially
  convertible (`ggml-org/llama.cpp#20055`); community GGUFs rewrite it as Qwen3 and
  compare at cosine 0.99998, which is not a promise for an index.
- **ONNX Runtime int8** would be leaner, but the official TEI image for it is amd64 only,
  and this box is arm64.
