# The Ornith 1.5 122B lane: Ornith AI's mixture of experts, on vLLM

`ornith-ai/Ornith-1.5-122B-NVFP4` is the NVFP4 build of Ornith 1.5 122B that its authors, Ornith AI,
published on Hugging Face on 2026-10-20 under the MIT licence: a mixture of experts of the Qwen3.5
family (`Qwen3_5MoeForConditionalGeneration`), 122B parameters with 8 of its 256 experts active per
token, 48 layers of which 12 are full attention and the rest linear attention, a vision encoder, and
one MTP layer. ModelOpt quantized it in mixed precision: the routed and shared experts and the output
head in NVFP4, read as W4A16 (groups of 16), the attention and linear-attention projections in FP8,
the MTP layer left as it was, and a KV cache in FP8. This lane serves it on the text lanes' port,
behind the same proxy and key, as model `ornith-1.5-122b`, on vLLM with NVIDIA's b12x kernels in the
Qwen3.8 Flash-Next FP4-Local lane's image, which reads that mixed precision with the same kernels and the same
loader it serves FP4-Local with.

Every number on this page was measured on the reference box (an ASUS Ascent GX10: GB10, 124 GiB of
unified memory, its Hugging Face cache on an external USB SSD) between 2026-10-21 and 22, and the
ones marked so on a second ASUS Ascent GX10, with the scripts and their raw output kept beside them.

## The checkpoint

The lane pins the authors' own build at its revision of 2026-10-21 08:31 UTC. Its 23 files are those
of the revision measured here (dde0b1af, 2026-10-20 17:37 UTC) but its model card, whose introduction
was rewritten four times since: checked file by file against Hugging Face's own sha256 of each. Its
chat template is served as published but for one line: reasoning between `<think>` tags, on by default
and turned off by `enable_thinking: false`, tool calls in Qwen's XML form, and no reasoning effort. It
takes two fixes. A system message past the first one made the published template raise "System message
must be at the beginning", and opencode 1.19.4 sends two system messages before the user's, so every
opencode request on this lane got a 400 until v1.40.2: `patch-template.py` renders such a message as a
`<system-reminder>` block, as on the 27B and the two flash lanes. And its `render_content` loop raised
"Unexpected item type in content." on the `tool_reference` blocks of a Claude Code ToolSearch result until
v1.40.3: each now names its tool, as on the Qwen lanes (`patch-template.py
--patches=system-reminder,tool-reference`, the template having no effort of its own). A conversation with
one system message renders byte for byte as before. Its sampling
defaults, which vLLM applies to a request that sets none, are its `generation_config.json`'s:
temperature 0.6, top_k 20, top_p 0.95. Its tokenizer counts text as Qwen3.8's does (the same
vocabulary, merges and pre-tokenizer, seven audio tokens fewer), so the limits measured for the Qwen
lanes hold for it.

A second build of the same weights, `Sherlock-Comms/Ornith-1.5-122B-NVFP4-DFlash2-RTX-Spark`, keeps the
output head in BF16, which vLLM asks for before it runs the DFlash2 drafter of this model. It was
checked here before it was measured: its first seven files are the authors' own (the same sha256), its
eighth holds the same 4,307 tensors byte for byte but the head, and the head is, bit for bit on all
248,320 rows, the authors' NVFP4 head dequantized (each FP4 value times its FP8 scale of 16 values times
the global FP32 scale, the low half-byte first), done here without its author's script. Its config
differs in one thing, the head named among the layers left unquantized.

## The window: 262,144 tokens, and a million with YaRN

The model's own window is 262,144 tokens. Its model card stretches it to a million with YaRN (factor 4
over the native 262,144), and gives it as `--hf-overrides '{"rope_scaling": {...}}'`. In this build of
vLLM that form does nothing: it sets a field at the top of the model's config, while the layers read
`text_config.rope_parameters`, and the engine serves the native rotary embedding at every position.
Measured on 150 Wikipedia articles of 1,024 tokens (English, German, French), the probabilities it gives
each token with the card's form against the native window: a KL divergence of 0.0117 nats per token,
the same as the engine against itself (two passes, one request at a time and four, 0.0137). Only the
form that rewrites `text_config.rope_parameters` whole, YaRN in it, scales the embedding:

| against the native window, the same 150 articles | KL, nats per token | change of the log-likelihood | same first token |
|---|---|---|---|
| the native window, a second pass (the engine's own noise) | 0.0137 | +0.0010 (from -0.0002 to +0.0023) | 94.72 % |
| the card's form | 0.0117 | +0.0001 (from -0.0010 to +0.0012) | 95.02 % |
| YaRN, factor 2 (524,288 tokens) | 0.0196 | +0.0018 (from +0.0005 to +0.0032) | 93.46 % |
| YaRN, factor 4 (1,048,576 tokens) | 0.0271 | +0.0040 (from +0.0022 to +0.0057) | 92.39 % |

The intervals are 95 % bootstrap intervals over the articles. YaRN at factor 4 costs short texts 0.4 %
of perplexity (0.66 % in German), and a code hidden at 900,055 tokens was found exactly, the prompt
prefilled in 55 minutes at about 270 tokens a second, the box's free memory at 19.5 GiB at its lowest.
A code at 480,118 tokens was found with YaRN at factor 2, and one at 400,025 tokens with the card's form,
which, scaling nothing, found it all the same: one retrieval says little of what a model does past its
window. GSM8K and HumanEval said more: with YaRN at factor 4, 98.8 % and 93.90 %, against 97.6 % and
95.12 % on the native window, the same box, the same drafting and the same evaluations (below).
The lane serves the million. Its MTP head gets the same YaRN (vLLM copies the overrides of
`text_config` into the head's own config), and with it drafting 2 tokens a code hidden at 499,974
tokens was found exactly, its prompt prefilled in 19 min 27 s, the box's free memory at 15.5 GiB at its
lowest.

vLLM derives the window from `max_position_embeddings` without reading that YaRN, and refuses a longer
one: the unit sets `VLLM_ALLOW_LONG_MAX_MODEL_LEN=1`, as the card does. It fixes the KV cache at 17 GiB
(`--kv-cache-memory-bytes`), which holds 1,301,680 tokens, the window 1.24 times over. Left to a share of
the memory (0.8), the KV cache gave from 829,030 to 1,149,938 tokens from one boot to the next, with
what the rest of the box held at each start: less than the window at some.

## The engine: vLLM with the model's own MTP head

Two engines serve this checkpoint on a GB10, each with a drafter that guesses tokens for the model to
check: vLLM with b12x and the MTP layer the model carries, and SGLang with DFlash2, a drafter trained
apart for this model (`incoai/Ornith-1.5-122B-DFlash2`) that proposes a block of tokens at once. Both
have the model check every drafted token, so neither changes what it says. Measured on the reference
box, one engine after the other, the same night (2026-10-22), at the native window:

| | vLLM, b12x, MTP drafting 2 | SGLang (nightly), DFlash2 drafting 8 |
|---|---|---|
| one request, `./bench.sh`, greedy: code / reasoning / math / free prose | 42.2 / 42.5-43.3 / 41.1-42.7 / 30.8-32.2 tok/s | 53.7-53.8 / 65.8-67.6 / 69.8-74.5 / 25.2-25.3 tok/s |
| five real jobs at the model's sampling, in all, 4 / 8 at a time | 64.2 / 88.3 tok/s | 63.0 / 93.2 tok/s |
| random tokens (1,024 in, 512 out), in all, 1 / 4 / 8 at a time | 37.3-42.1 / 79.1 / 104.5 tok/s | 11.2-11.5 / 24.7 / 37.2 tok/s |
| a prompt of 40,013 tokens, first token, cold / when it comes back with a new turn | 26.8 s / 5.3 to 5.6 s (0.54 to 0.58 s on the second GX10 since v1.38.3, below) | 19.3 s / 0.2 to 0.3 s |
| KV cache at a window of a million | 1,301,680 tokens | 566,943 tokens |
| free memory at its lowest, booting / serving up to 8 at a time | 17.1 / 15.8 GiB | 12.7 / 10.7 GiB |
| a boot, its caches made | 2 min 46 s to 3 min 46 s | 11 min 30 s to 11 min 51 s |

SGLang's drafter wins where text is predictable and one request runs alone: by a quarter on code, by
half or more on reasoning and math. It loses where text is not: a fifth on free prose, more than two thirds
on random tokens, as eight drafted tokens are paid for at every step and thrown away. At four and eight
requests the two engines are within 6 % of each other. And SGLang holds half the window: at a window of a million its KV
cache is 566,943 tokens, and it refused a prompt of 900,000. The lane serves vLLM, for the whole
window, a speed that holds on any text, a boot four times shorter, more of the box's memory left free,
and the image the Qwen3.8 Flash-Next FP4-Local lane already pulls. SGLang's turns of an agent come back from its
cache in 0.2 to 0.3 s, the one thing it does better here: an option of this vLLM build brought this
lane's from 5.3 to 5.6 s down to 0.54 to 0.58 s on the second GX10 (v1.38.3, below), which narrows the
gap without closing it.

DFlash2 runs on vLLM too, in principle, and the build of the same weights with a BF16 output head
(above) was made for it. In this image it does not start, with prefix caching or without: "The resolved
KV cache layout (BLNHC) does not store blocks as dense, unpadded pages ... so a manager block cannot be
split into 271 kernel blocks of 16 tokens" (the model's linear attention sets the block at 4,336 tokens,
and DFlash refuses the other layout). The lane keeps the authors' build.

The MTP head drafts 2 tokens: 42.4 to 42.6 tok/s on `./bench.sh`, against 41.3 to 41.7 with 3 and 40.7
to 41.1 with 4. One more drafted token is accepted less often than it costs.

## Speed

On the reference box, at the lane's window of a million: 42.2 to 42.4 tok/s greedy median on
`./bench.sh` (41.0 to 43.4 on code, 42.4 to 43.7 on reasoning, 41.7 to 43.3 on math, 31.9 to 33.1 on
free prose), and 41.2 / 68.1 / 90.0 tok/s in all at 1 / 4 / 8 requests on five real jobs (a program's
type annotations, a function and its tests, an agent's plan, an essay in English, a summary in French),
against 40.9 / 64.2 / 88.3 on the native window the same night: YaRN costs no speed. On the second ASUS
Ascent GX10, at the native window: 43.5 to 43.6 tok/s on `./bench.sh`, and 41.1 / 66.7 / 91.2 tok/s in
all at 1 / 4 / 8 requests on the five real jobs. A request that names no sampling gets the
checkpoint's (temperature 0.6, top_k 20, top_p 0.95), reasoning included, and reasoning makes most of
an answer's tokens.

## Quality

| | the native window, MTP drafting 4, reference box | the native window, MTP drafting 2, second GX10 | the lane's window (YaRN at factor 4), MTP drafting 2, second GX10 |
|---|---|---|---|
| GSM8K, its first 250 problems | 98.0 % | 97.6 % | 98.8 % |
| HumanEval, 164 problems | 95.73 % | 95.12 % | 93.90 % |

Both evaluations greedy, 8 problems at a time, 8,192 tokens at most, through the engine's own API. A
drafted token is kept only when the model picks it too, so between two columns what differs is the
window and the engine's own noise: it does not compute a batch the same way twice, and a greedy answer
can part from one run to the next. On the same box with the same drafting, the million lost 2 of
HumanEval's 164 problems and gained 3 of GSM8K's 250: as much as the native window moved from one box
and one drafting depth to the other, so no cost these evaluations can see.

## An agent's turns: what comes back from the cache

An agent sends its whole conversation again at every request, and makes two or more a turn: the tool
call, then its answer once the tool has run. On a hybrid model, a prefix cache serves only what it kept
a recurrent state for. vLLM keeps one at the boundaries of its blocks, 4,224 tokens here, and with MTP
it leaves the last of them out: by default a request found in the cache the last whole block before the
end of what it shared with the one before, less one. Since v1.38.3 the lane passes
`--prefix-match-unit 16`: the cache keys a prompt every 16 tokens and also keeps a state at each
prompt's last 16-token boundary, so a request that extends an earlier one, as an agent's next request
does, starts from the cache at that boundary (16 tokens earlier with MTP). A request that parts from an
earlier prompt inside it still starts at the last whole block before the point where they part.

Measured through the proxy with a conversation as an agent holds it (`prefix_probe.py`: a 40,000-token
system prompt, then turns, each the whole history and a new question whose answer is checked), on the
second GX10 (2026-10-22): every turn after the first found 99.9 % of its 40,042 to 40,126 tokens in the
cache and computed 46 to 58, its first token after 0.54 to 0.58 s, where without the option, on the
same box, it found 33,792 (84 %) and its first token came after 5.1 s (5.0 to 5.6 s on the reference
box). Then with the agents themselves, on the second GX10: four turns of opencode and four of Claude
Code, each in one session, every turn with a tool (read a file, write one, read it again, list the
folder), three times over; the medians of the turns after the first:

| | v1.38.0 to v1.38.2 (MTP 2) | MTP off | MTP off, with the option | v1.38.3 (MTP 2, with the option) |
|---|---|---|---|---|
| opencode: from the cache / tokens computed / a turn | 84 % / 11,399 / 14.1 to 15.8 s | 95 % / 3,601 / 10.4 to 10.7 s | 100 % / 214 / 5.6 to 8.6 s | 100 % / 249 / 5.6 to 7.9 s |
| Claude Code | 36 % / 14,810 / 11.7 to 13.3 s | 78 % / 6,369 / 7.2 to 10.7 s | 99 % / 234 / 5.4 to 7.8 s | 99 % / 268 / 3.7 to 6.4 s |

Each column's 24 answers were right. An agent's request writes few tokens, 59 to 76 on average here
(tool calls and short answers), so a turn's time is mostly the prompt it computes: without the option,
turning MTP off made a turn about 30 % shorter, its drafting saving less than the block it cost. With
the option, MTP costs a request 16 tokens of cache instead of a block, and the lane keeps it: a turn is
as short with MTP as without, often shorter, and the option leaves the writing speed as it was
(`./bench.sh` gave 40.0 and 40.4 tok/s without it and 39.9 to 40.3 with it, in the same session on the
second GX10), where turning MTP off writes 29.2 tok/s on any text instead of 41 to 43 on code,
reasoning and math and 31 to 33 on free prose (the reference box).

What v1.38.0 said here was wrong on two counts, found by measuring (2026-10-22). vLLM's fix for MTP's
last block (vllm#58368), put into this image, changed nothing on this model (the probe, on the
reference box); nor did the checkpoints at each request's end that this build keeps for three other
model families, turned on for this one. And Claude Code's prompt did not change from one turn to the
next: the 8,448 tokens it found were one block of 4,224 for each of the two requests of a turn, of about
11,000 to 12,000 tokens each, not two blocks of one prompt. `--enable-mamba-fine-grained-prefix-cache`,
which this build also has, keeps a state where a request with MTP was seen to leave a prefix it shares
with another: beside `--prefix-match-unit 16` it changed nothing that came from the cache (99 to 100 %
either way), the turns' times moved both ways (Claude Code 3.9 to 5.7 s with it against 3.7 to 6.4
without, opencode 7.3 to 8.9 s against 5.6 to 7.9, its fourth turns writing longer answers in that
series) and the probe's first token was 0.55 to 0.59 s against 0.54 to 0.58, so the lane leaves it out.
On the Nemotron lanes, whose template is another, Claude Code's turns found 72 to 98 %.

The correctness of those turns was checked, not only their speed: the fixes vLLM made for MTP and the
prefix cache of hybrid models (vllm#46384, #51113) are in this image, and the misread prefill that gave
NaN after a turn from the cache on other builds (vllm#49918, #55766) does not reach the model runner
this lane uses (its second version, which never takes a batch with a prompt in it for a decode).

## A prompt the cache cannot serve

A turn whose prompt the prefix cache serves comes back in seconds, whatever its length: the next turn of
a chat of 494,120 tokens gave its first token in 1.8 s, all but 56 of its tokens from the cache, and
Claude Code's next turn of 500,266 tokens took 4.3 s (2026-10-30). A prompt the cache cannot serve is
computed whole before its first token, and each of its tokens attends to all those before it, so the
time grows about with the square of its length: on the reference box, about 3 min for the first 200,000
tokens, 6 for 300,000, 11 for 400,000 and 19 for 494,089, at 2,900 tokens a second at its start and 270
at its end. A turn is computed whole after the lane starts or restarts, when an earlier message is
edited or regenerated, when a client changes something near the start of its prompt (a date or a time in
a system prompt, a resumed session whose first lines changed), or when other long requests have pushed
it out of the cache, which holds 1,301,680 tokens.

The client has to wait that long. Claude Code with its own default timeouts left such a turn after 10 min,
twice, then sent it without streaming and left after 5 min, twice, and the engine kept what it had
computed only every other time: its answer came after 35 min (issue #61). docs/clients.md
says what widens each client's timeout. Since v1.40.7 the cockpit says while it lasts how long the
request has been in flight and how much of its prompt the engine has computed, with a floor of the time
left, and the proxy says when a client sends a long prompt again after leaving it.

## Other requests while a long prompt is computed

Each engine step carries up to 4,096 tokens of a prompt, and once a prompt is long those take seconds. With
vLLM's default scheduler the other requests waited (issue #61): while a cold prompt of 362,694 tokens was
computed, 10 clients each sending requests of about 20 KB in a loop got their first token after 101 s at the
median and their 64 tokens after 265 s, 484 s at worst, where ten such requests at once take 11.5 and 20 s
on an engine with nothing else to do. A client whose timeout is shorter gives up and sends its request
again, as the user who reported it saw.

Since v1.40.7 the lane starts with four of vLLM's scheduler settings (vllm/config/scheduler.py says what each
does). `--max-parallel-prefills auto` lets up to four prompts share a step, round-robin, a prompt alone
keeping the whole budget. `--prefill-policy decode-aware` keeps one of those places for the prompt nearest its
answer. `--prefill-compute-share 0.25` gives the prompts a quarter of the time the engine is shared, and the
answers being written the rest. `--max-num-prefill-tokens-per-step 1024` caps a shared step's prompt tokens,
a step the prompt has alone keeping its 4,096. Measured on the reference box on 2026-10-31, each setting
started by the lane's own command, the same long prompt and the same 10 clients:

| Settings | 64 tokens, median | worst | first token, median | requests served a minute | the long prompt | a prompt alone, 277,608 tokens |
|---|---|---|---|---|---|---|
| vLLM's defaults (v1.40.6) | 265 s | 484 s | 101 s | 2.4 | 698 s | 416 s |
| the four, a share of "auto" | 86 s | 161 s | 40 s | 7.1 | 1,113 s | 415 s |
| the four, a share of 0.25 (v1.40.7) | 34 s | 62 s | 30 s | 16.7 | 1,653 s, its last 388 s alone | 418 s |
| the four with "auto", steps of 8,192 | 22 s | 780 s | 21 s | 6.2 | 791 s | 393 s |

The clients stop after 20 min: on v1.40.7's settings the long prompt outlived them and ran its last 388 s alone,
the others ended while they still sent.

The long prompt takes longer while it shares the engine, and the time it takes alone does not change: a step
it has alone is scheduled as before. Steps of 8,192 tokens made a prompt alone 5 % faster, but 8 small
requests of 80 waited 668 to 777 s for their first token, so the lane keeps 4,096. Answers changed no more than the engine's
own noise: two runs of the same ten prompts at temperature 0 on one engine gave the same text for two to five
of them, and v1.40.7's settings against v1.40.6's for three, the largest gap of a token's log-probability 0.50
against 0.54 between two runs of v1.40.6's own.

## Memory

vLLM loads 72.63 GiB of weights, and the KV cache takes its fixed 17 GiB. At the lane's window,
MemAvailable was 16.3 GiB at its lowest while it booted, 16.4 GiB while it served up to eight requests
at once, and 15.6 GiB while it prefilled a prompt of 499,974 tokens. A prompt of 900,055 tokens, with
MTP off, left 19.5 GiB free at its lowest.

## Clients

Through the proxy on `:30001`, with the box's key, as model `ornith-1.5-122b`: the OpenAI and the
Anthropic dialects alike, images and videos inside the request (a picture and a short film were read
right, in both dialects, on the second GX10), tool calls parsed in the checkpoint's own format
(`qwen3_xml`), the reasoning in the message's `reasoning` field. Its template has two levels, reasoning
on or off (`enable_thinking`), and the proxy (v6.45) says a chat request's `reasoning_effort` in its
words: `none` and `minimal` off whatever else the request says, as on the Qwen lanes, every other level
on unless the request names `enable_thinking` itself. The Anthropic dialect's `thinking: {"type":
"disabled"}` and an effort sent on `/v1/responses` go as they came, and the model reasons all the same
(checked for the first on the second GX10): there, `enable_thinking: false` in `chat_template_kwargs`,
where a client lets one set it, is the way to turn it off. opencode gets an `ornith-122b` provider that may attach
images, with the levels `high` and `none` and a window of 990,000 tokens with 32,000 of output
(compaction late, and still inside the 1,048,576 the engine takes); loading the lane makes it
opencode's default. pi and omp send it their level, starting at `high`, `off` turning it off. Claude
Code: `claude-local` sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1013000` and `max_tokens` 32,000 here
(opencode's 990,000 plus 23,000), at worst 1,022,000 tokens inside the window; 1000000 set by hand
with its default output fits too, at worst 1,009,000 (the KV cache, 1,301,680 tokens, is not what binds here). System One needs a Qwen
lane.

## What it does not have

- The SGLang lanes' routes: no `/flush_cache`, no route that aborts every generation, no `/server_info`
  pool. The cockpit says so in its name (the Flush and Abort buttons, the opencode fit), and stopping
  the lane stops its generations.
- System One (`/v1/systemone`) answers on the SGLang lanes only: this lane serves on vLLM, and the
  endpoint says to load one of them.
- A reasoning effort: the chat template reads `enable_thinking` and nothing else. The proxy turns a
  chat request's effort into it (above).

## Installing, loading, removing

A plain install on a 128 GB box puts its unit in (a 64 GB DGX Spark leaves it out: README, [On a 64 GB DGX Spark](../README.md#on-a-64-gb-dgx-spark)),
and its first Load from the cockpit pulls the Qwen3.8 Flash-Next FP4-Local
lane's image when that lane has not (18.2 GB on disk), fetches the checkpoint at its pinned revision
(77.7 GB) and starts it: 5 min 31 s for the first boot on the second GX10, b12x tuning its kernels
for this model, then 1 min 52 s there, its checkpoint on the internal disk, and 2 min 46 s to 3 min
46 s on the reference box, whose cache is on a USB SSD. From a terminal: `./prepare-lane.sh ornith-122b` the first time, then
`./switch-model.sh ornith-122b`. `--with-ornith-122b` installs it whole at install time and proves it,
`--no-ornith-122b` leaves it out of that run, and `./install-ornith-122b.sh --uninstall` removes its unit,
its chat template, its opencode and pi providers, its caches, its image's pin tag and, when the Qwen3.8 Flash-Next FP4-Local lane is not
installed, the io_uring filter the two lanes share. The image (which the Qwen3.8 Flash-Next FP4-Local lane's own tag
keeps, when that lane is installed) and the checkpoint stay, and it prints how to delete them.
