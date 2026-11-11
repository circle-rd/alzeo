# The Qwen3.8 Flash-Next FP4-Local lane: NVIDIA's FP4 build of Qwen3.8-Flash-Next, on vLLM

`nvidia/Qwen3.8-Flash-Next-FP4-Local` is NVIDIA's own build of Qwen3.8-Flash-Next for one local
GPU, released on Hugging Face on 2026-10-05: its experts in NVFP4 and its other layers in MXFP8,
refined by quantization-aware distillation (QAD), and its n-gram embedding (PLE) table in NVFP4 too,
stored inside seven of the checkpoint's own files. Its NVFP4 scales are in a new lossless format
that upstream vLLM does not read yet, so its model card serves it with community vLLM images that
carry NVIDIA's b12x kernels, `eugr/spark-vllm-b12x` on a DGX Spark. This lane serves it on the text
lanes' port, behind the same proxy and key, as model `qwen3.8-flash-next-fp4`, with its MTP head
drafting 4 tokens and, as on the other Qwen lanes, `lean` as the reasoning effort a request that
names none gets (`FLASH_LOCAL_EFFORT=xhigh` gives it its model card's `xhigh`, below). It is a lane of
its own beside the flash lane (`docs/flash-lane.md`), which serves the same model on SGLang from
RadixArk's export.

The lane comes from Dimitris Kontokostas's pull request #55, which brought the model, its image
and its card's recipe to this repository. Its integration as a lane, the n-gram table's placement,
the sandbox and the measurements below were done on the reference box.

Every number on this page was measured on the reference box (an ASUS Ascent GX10: GB10, 124 GiB of
unified memory, its Hugging Face cache on an external USB SSD, its home directory on the internal
NVMe) on 2026-10-19 and 20, with the scripts and their raw output kept beside them.

## The two flash lanes side by side

| | flash (SGLang, RadixArk's export) | Qwen3.8 Flash-Next FP4-Local (vLLM, NVIDIA's build) |
|---|---|---|
| checkpoint on disk | 135.3 GB | 102.3 GB |
| n-gram table | FP8, 47.7 GiB, written beside the home directory at every boot | NVFP4, 26.8 GiB, read in place from the checkpoint's files |
| weights in memory, once loaded | 81.31 GiB (SGLang's count) | 70.32 GiB (vLLM's count) |
| KV cache | 467,328 tokens, BF16 (the `concurrency` tier) | 921,090 to 984,833 tokens, FP8 |
| window, and the proxy's prompt ceiling | 262,144, 250,000 | 262,144, 250,000 |
| requests at a time, drafter | 8, MTP (3 steps) | 8, MTP (4 tokens) |
| a later boot, until it answers | 12 to 15 minutes (it writes its table first) | about 5 min 30 s (the first: 17 to 19 min, b12x tunes its kernels) |
| free memory in service, MemAvailable | 9.2 to 12.1 GiB | 16.6 to 19 GiB |
| alone, real text at the agents' sampling | 43.8 tok/s | 47.4 tok/s |
| 8 at a time, real text at the agents' sampling | 123.7 tok/s | 106.6 tok/s |
| 8 at a time, GSM8K's 1,314 questions at temperature 0 | 97.49 %, 132.1 tok/s, 2,297 s | 97.64 %, 114.6 tok/s, 2,765 s |
| HumanEval, 164 problems, pass@1 | 96.95 % (159) | 98.17 % (161) |
| random prompts at temperature 0, alone / 8 at a time | 35.3 / 103.4 tok/s | 23.3 / 77.0 tok/s |
| prefill, one prompt of 32K / 100K tokens | 1,617 / 1,638 tok/s | 2,141 / 2,079 tok/s |

Both quality runs used the same harness (`sglang.test.run_eval` from the SGLang image, temperature
0, and `evals/humaneval.py`, 32 at a time), the flash lane's on 2026-10-19 morning, this lane's that
night. On quality they tie: GSM8K differs by 2 questions out of 1,314 and HumanEval by 2 problems
out of 164, both inside the run-to-run spread of one engine, and NVIDIA's card gives this build 91.5 %
on GPQA Diamond and 88.7 % on ToolEvalBench against its BF16 teacher's 92.2 % and 89.4 %. "Real text
at the agents' sampling" is GSM8K's first 128 questions 8 at a time and 16 alone, at temperature 1.0,
top_p 0.95 and the checkpoint's top_k 20, as `real.sh` sends them, measured on both lanes on
2026-10-20. The random prompts are `vllm bench serve`'s, from a client in a container of its own. No
request named a reasoning effort, so each lane's template gave its default, `lean` on both, this
lane's default again since v1.46.4 (from v1.37.0 to v1.46.3 it was the card's `xhigh`). At `xhigh`,
on the same questions, answers ran longer (427 tokens on average against 237), the MTP head's tokens a
step fell from 3.7 to 2.85, and eight requests at a time decoded 99.4 tok/s (one run, 2026-10-20).

Alone on real text this lane decodes faster, 47.4 tok/s against 43.8. Eight requests at a time, the
flash lane does, by 16 % at the agents' sampling and by 15 % on GSM8K at temperature 0. Random prompts,
which no agent sends, favour the flash lane alone and at eight: a measure of the engines on
predictable text says more than one on noise. This lane also prefills about 30 % faster, takes 102 GB
less disk (121 against 223: a smaller checkpoint and image, and no table written beside it), writes
nothing at boot, starts in less than half the time, holds twice the KV cache and leaves the box some 7 GiB
more of free memory in service. Which to load is a choice of use, and the cockpit loads either.

## The n-gram table is read at every token: keep it on the internal disk

The engine reads the table's rows at random over io_uring at every token, from the checkpoint's own
files (`VLLM_PLE_TABLE_MEMORY=disk`, `B12X_DISK_BACKEND=io_uring`, the card's settings). Where those
files are decides the speed:

| where the seven files are | alone | eight at a time, in all | first token, alone |
|---|---|---|---|
| on the USB SSD (the cache's disk) | 15.7 tok/s | 15.6 tok/s | 1.49 s |
| on the internal NVMe | 23.4 tok/s | 77.7 tok/s | 0.75 s |

Random prompts of 1,024 tokens, 512 out, `vllm bench serve` at temperature 0 (6 prompts alone, 32
at eight, and on the SSD the eight read off the engine's own token counter over 101 s, the run cut
there). On the SSD, eight requests decode no faster than one: every token waits on the disk.
So the lane reads the table where the cache is only when the cache is on the disk of the home
directory, a new box's default. When the cache is on another disk, the Load copies the seven files
(32 GB) into `~/flash-local-ple`, checks each copy against its sha256, and the unit mounts them over
the cache's own, file by file, and the weights the engine reads once at boot stay where they are.
`FLASH_LOCAL_PLE_DIR=<folder>` puts the copies elsewhere, `FLASH_LOCAL_PLE_DIR=none` reads the table
where the cache is, and the choice holds for every run after (`ple-table.py` says how,
`docs/external-disk.md` what else an external disk changes).

## The sandbox: Docker's own filter, plus io_uring

io_uring's three system calls are refused by Docker's default seccomp profile, so the model card
starts the engine with seccomp and AppArmor turned off. This lane keeps both: its unit passes
`seccomp-io-uring.json`, Docker 29.6.2's own default profile (moby's
`profiles/seccomp/default.json`, unchanged) with one rule more that allows `io_uring_setup`,
`io_uring_enter` and `io_uring_register`, and nothing else (a test holds the file to that). The
engine runs as root in its container, on the host's network: with the filter off it could, for
one, make a user namespace, which this profile still refuses (checked in the container both ways).
Measured with the filter and without it, everything else equal: 23.34 against 23.39 tok/s alone,
76.69 against 77.65 at eight, 2,141 against 2,136 tok/s of prefill, all inside the noise.

## The serving command, and how it was chosen

The unit runs the model card's command for a DGX Spark, flag for flag and variable for variable
(`qwen38-flash-local.service.template` lists them), with this box's port, key, cache and sandbox,
the template patched as the other Qwen lanes' are (`patch-template.py`, with their default effort,
`lean`, since v1.46.4: below), its own caches, so that only the first boot compiles and tunes, and one setting
of PyTorch's allocator that the card does not give, for its boot (Memory, below). Then each
setting that could do better was measured against it, one at a time, on the same boot recipe
(`~/qa-tools/pr55/recipe/`: a drop-in per variant, the engine restarted, the same probes in the
same order, the SoC under 70 °C before a start and under 75 °C before a run):

The first series, at temperature 0 (`vllm bench serve` with random prompts of 1,024 tokens and 512
out, 6 alone and 32 at eight, then the prefill of one 32K prompt, then the first 200 questions of
GSM8K, 8 at a time):

| against the card's recipe | alone | eight at a time | prefill | GSM8K 200 | KV cache | kept |
|---|---|---|---|---|---|---|
| the card's recipe | 23.27 tok/s | 76.99 tok/s | 2,120 tok/s | 99.0 %, 111.9 tok/s | 954,184 | yes |
| `--async-scheduling` said | 23.10 | 75.44 | 2,092 | 99.0 %, 112.7 | 951,250 | already on: vLLM turns it on with MTP |
| MTP drafting 3 tokens | 22.90 | 84.76 | 2,086 | 99.0 %, 109.9 | 949,279 | no |
| 8,192 tokens a step | 22.98 | 74.68 | 2,158 | 98.5 %, 112.5 | 786,105 | no |

The second series, at the sampling the agents get (temperature 1.0, top_p 0.95, and the top_k 20 of
the checkpoint's `generation_config.json`, which vLLM applies to a request that sets none): GSM8K's
first 128 questions 8 at a time, then 16 alone, with the tokens each step of the MTP head's drafts
produced, read off vLLM's counters:

| draft and verification | 8 at a time | its tokens a step | alone | its tokens a step | kept |
|---|---|---|---|---|---|
| the card's: greedy drafts, standard verification | 99.2 %, 106.6 tok/s | 3.66 | 47.4 tok/s | 3.71 | yes |
| `draft_sample_method: probabilistic` | 96.1 %, 105.1 tok/s | 3.72 | 48.5 tok/s | 3.83 | no |
| the same, with `rejection_sample_method: block` | 98.4 %, 105.6 tok/s | 3.78 | 48.5 tok/s | 3.79 | no |

Random prompts flatter shorter drafts: what the model writes after random tokens is hard to
predict, so a draft of 3 tokens wastes less than one of 4, and led by 10 % at eight requests there.
On real text the card's 4 tokens led. Eight thousand tokens a step bought 2 % of prefill and cost 17 %
of the KV cache and 3 minutes 37 more at boot. Drafting by sampling, which NVIDIA's recipe for an RTX
PRO 6000 sets, and verifying drafts as a block raised the tokens a step by 1.5 to 3 % at the agents'
sampling, and nothing of the throughput: inside the run-to-run spread (the scores too, on 128
questions). The card's recipe for a DGX Spark stays, unchanged. Not tried, and why: a schedule of
draft lengths by batch size, which this build has, turns off the fused multi-step draft and the
prefix cache's request-boundary checkpoints (below) in its source. More requests at a time than the
card's 8, and a larger KV cache than its memory share gives, were not tried either: the card's share
already gives the KV cache 921,090 to 984,833 tokens, three and a half requests of the whole window
or more.

## An agent's turns: its prompt comes back from the cache

An agent sends its whole conversation again at every turn, and on a hybrid model like this one a
prefix cache can only serve what it kept a recurrent state for. This build keeps those states at a
request's boundaries for this model (its log says so at boot: "Request-boundary recurrent checkpoint
caching is enabled"): the leading instructions, the whole prompt and the end of the answer. Measured
through the proxy with a conversation as an agent holds it (`prefix_probe.py`: a 40,000-token system
prompt, then six turns, each the whole history and a new question): the first turn computed its
40,013 tokens in 17.5 s, and every turn after found 100 % of its prompt in the cache but its last 22
to 25 tokens, its first token after 0.31 to 0.34 s.

## Memory

At the card's share of 0.8, vLLM counted 114.4 GiB free of 121.63 at its start (the box's other
services, the cockpit and the speech lane among them, held the rest), loaded 70.32 GiB of weights,
profiled 2.01 GiB of activations and 0.34 GiB of CUDA graphs, and gave the KV cache 17.85 GiB: between
921,090 and 984,833 tokens over the boots measured, with what the rest of the box held at each start.
In service it leaves the box room: MemAvailable was 18 to 19 GiB once it served, and 16.6 GiB at its
lowest under eight requests, where the flash lane on SGLang served at 9.2 to 12.1 GiB under the same
measures the same night.

Its boot did not, until one setting of PyTorch's allocator. vLLM loads the weights with that allocator
splitting no block above 20 MiB, then b12x lays out the experts' NVFP4 scales, layer by layer: the
blocks it frees on the way are not used again, and the allocator's cache grows until the GPU's driver
refuses it memory, then drops it at once and asks again. On a discrete GPU that cache sits in the GPU's
own memory. On a GB10 it is the machine's. Four to six and a half minutes into each of the twelve
boots this build had on the reference box (2026-10-19 and 20), the driver refused 44 to 149 allocations
(`NV_ERR_NO_MEMORY` in the kernel log) and MemAvailable fell to between 0.5 and 3.5 GiB, around and
under the cockpit's floor of 3 GiB, where this box has met crashed engines and livelocks before
([gb10-memory.md](gb10-memory.md)). Read every 0.2 s at the last of them, it fell from 37 to 1.05 GiB in 8 seconds and
was back at 42 GiB within one, as the driver refused, and `py-spy` found the engine inside b12x's
`prepare_w4a16_modelopt_nvfp4_weights`, under vLLM's `process_weights_after_loading`.

PyTorch collects the blocks it keeps, before it asks the driver for more, only under a ceiling below
the whole GPU (`per_process_memory_fraction`), once it holds the share of that ceiling its threshold
says (`garbage_collection_threshold`). The unit gives both: a ceiling of 0.95 of the GPU's 121.63 GiB,
which nothing reaches, and the collection past 0.8 of that ceiling, 92.4 GiB, above the 90.4 GiB the engine holds
in service (its weights, KV cache, activations and CUDA graphs). A test holds the two to the share the
unit gives vLLM. Two boots with it, on the same box and checkpoint, the second from the unit as an
update puts it in and restarted from the cockpit: MemAvailable at 19.1 and 18.7 GiB at their lowest,
no refusal from the driver, ready in 322 s both times against 329 to 336 before, and the same KV cache
(932,991 and 933,806 tokens). The engine serves as fast with it: `bench.sh`, at `xhigh`, the lane's
default effort then, measured a greedy median of 55.4 tok/s with it and 55.1 without it (2026-10-20).

Five more boots with it on 2026-10-20 say what it does not do. The driver still refused allocations
in three of them, 233, 130 and 5 times, every one from `_memdescAllocInternal`: each time from about
33 s after the model's weights were read, where b12x lays out the scales and where the dip was (4 min
03 to 5 min 07 into the boot, one of them until its KV cache was allocated). MemAvailable, read every
0.2 or 0.5 s at four of the five, stayed at 11.7 GiB or more, and at 14.1 GiB or more while the driver
refused. So the setting ends the dip, not the refusals, which do not come from the box running out of
memory. Their cause is not known yet. Each of those boots came up and served, and the cockpit's
Machine view counts the refusals of the last hour.

## What it does not have

- The SGLang lanes' routes: no `/flush_cache`, no route that aborts every generation, no
  `/server_info` pool. The cockpit says so in its name (the Flush and Abort buttons, the opencode
  fit), and stopping the lane stops its generations.
- System One (`/v1/systemone`) answers on the SGLang lanes only: this lane serves on vLLM, and the
  endpoint says to load one of them.
- No 1M window by default: the model's own window is 262,144 tokens, which this lane serves whole,
  as the card's command for a DGX Spark does. A window of 1,048,576 is an option, below.

## A window of 1,048,576 tokens: FLASH_LOCAL_CONTEXT=1m

The card says the model extends to 1,000,000 tokens and gives no recipe for it (discussion #63
asked). Measured on the reference box on 2026-11-09, with this lane's own image, flags and memory
share, plus YaRN, a factor of 4 over the native 262,144 with the model's multimodal RoPE kept
(`mrope_section` and `mrope_interleaved` as the checkpoint has them), and `--max-model-len 1048576`:

| | measured |
|---|---|
| ready in | 406 s |
| KV pool | 1,061,819 tokens, room for one request of 1,048,576 |
| a passphrase hidden in filler, two trials at each depth | 8 of 8 retrieved: at 131K, 262K, 524K and 1,036,576 tokens |
| time to the first token | 58 s at 131K, 122 s at 262K, 268 s at 524K, 608 s at 1.04M (about 1,700 tokens a second) |
| memory available, lowest during the longest prompt | 15.1 GiB |
| GSM8K, 250 questions, 8 at a time (short prompts) | 97.6 % with YaRN, 96.8 % at the native window: the same within the noise, at the same speed |

```bash
cd ~/dgx-spark-qwen38 && FLASH_LOCAL_CONTEXT=1m ./install-flash-local.sh --units-only      # then Load the lane
cd ~/dgx-spark-qwen38 && FLASH_LOCAL_CONTEXT=native ./install-flash-local.sh --units-only  # back to 262,144
```

`--units-only` writes the choice and the lane's unit, and downloads and starts nothing: the next Load
of the lane, from the cockpit or `./switch-model.sh flash-local`, starts it with the window chosen and
sets the clients' limits for it. An update keeps the window chosen: `./install.sh` does not read
`FLASH_LOCAL_CONTEXT`, and says so.

The choice is kept for the Loads after (a Load from the cockpit stages the unit with none of your
environment). The unit reads its overrides from `~/.config/qwen38/flash-local-yarn.json` at its
start, written from the pinned checkpoint's own RoPE, and the installer refuses the option when
that RoPE is not the one measured, and keeps nothing of it then. A choice kept from before that a
checkpoint refuses so serves the native window, and the Load says why. Before the checkpoint is in
the cache, the unit keeps the native window: the Load that fetches it sets the 1M one.

At 1M, the proxy relays a prompt up to 92 % of the pool (about 976,000 tokens): the 250,000-token
ceiling it keeps on this lane at its native window is for that window. The Load gives opencode, pi
and `claude-local` a context of 840,000 tokens and 32,000 for the answer, which fit a pool 10 %
smaller than the one measured, and the cockpit's Lanes and Now views say 1M tokens of context.
It is not the default: its makers ship 262,144, and the 1M window is measured here on retrieval and on GSM8K only.

## The reasoning effort: lean by default, FLASH_LOCAL_EFFORT=xhigh

A request that names no reasoning effort gets `lean` since v1.46.4, as on the 27B and flash lanes
([LEAN.md](../LEAN.md)); from v1.37.0 to v1.46.3 it got `xhigh`, the default the model card ships. A
request still names any of `lean`, `low`, `medium` and `xhigh` (`reasoning_effort`, top-level or in
`chat_template_kwargs`). Measured on the reference box on 2026-11-11, through the proxy, 4 requests at a
time, at the card's sampling (temperature 1.0, top_p 0.95, top_k 20):

- **A short story of about 800 characters**, asked in English and in French, within 5,000 tokens: at
  `xhigh` no answer 24 times of 32 (12 of 16 at the lane's default, `xhigh` then, and 12 of 16 asked by
  name), every token spent reasoning, the model counting the letters one by one; at `lean`, `medium` and
  `low`, an answer 24 times of 24.
- **Exactly 300 characters**: no answer at every level that reasons, 13 times of 16 at `xhigh` and 11 of
  12 at `lean`, `medium` and `low`. A limit of the model that no level lifts.
- **A long JSON answer** (a drug's interactions): at the lane's default, `xhigh` then, 4 answers of 4, but
  21,260 tokens out in median, 19,145 of them reasoning, in 1,007 s; at `lean`, 8 of 8 in readable JSON,
  1,427 tokens, 819 of them reasoning, in 59 s; at `medium` 97 s, at `low` 87 s.
- **What the level does not change**: listing the release headings of the first 6,000 to 48,000
  characters of this repository's CHANGELOG (1,828 to 13,681 tokens of prompt), right 24 times of 24, 16
  at `xhigh` and 8 at `medium`, with 129 to 345 tokens of reasoning: the runaway comes with the kind of
  request (counting, enumerating), not with the length of the prompt.

```bash
cd ~/dgx-spark-qwen38 && FLASH_LOCAL_EFFORT=xhigh ./install-flash-local.sh --units-only   # then Load the lane
cd ~/dgx-spark-qwen38 && FLASH_LOCAL_EFFORT=lean ./install-flash-local.sh --units-only    # back to lean
```

`--units-only` writes the choice, and the lane's chat template when its checkpoint is in the cache, and
downloads and starts nothing: a lane that serves answers at its new default once it starts again, at its
next Load. The choice is kept for the Loads and the updates after (a Load from the cockpit runs with none
of your environment), in `~/.config/qwen38/flash-local-effort`. opencode's generated config keeps the
order of the lane's four levels (`xhigh` first), which decides no request: a turn that picks no level names
none. omp, which names its level with every request, starts at the level chosen (`pi-gen.py`, which an update
and a Load run again). `LEAN_DEFAULT`, the 27B and flash lanes' setting, does not reach this lane.

## Installing, loading, removing

A plain install on a 128 GB box puts its unit in (a 64 GB DGX Spark leaves it out: README, [On a 64 GB DGX Spark](../README.md#on-a-64-gb-dgx-spark)),
and its first Load from the cockpit pulls its image (18.2 GB on
disk, 9.7 GB to download, by digest), fetches its checkpoint at its pinned revision, makes the
table's copies when they are needed, and starts it: 17 to 19 minutes for the first boot, b12x tuning its
kernels for this GPU, about 5 min 30 s after. From a terminal: `./prepare-lane.sh flash-local` the
first time, then `./switch-model.sh flash-local`. `--with-flash-local` installs it whole at install
time and proves it, `--no-flash-local` leaves it out of that run, and `./install-flash-local.sh
--uninstall` removes its unit, its caches, its patched template, its filter (kept while the Ornith 1.5 122B lane, which reads it too, is installed), its check of its files,
its image's pin tag and the table's copies. The image and the checkpoint stay, and it prints how to
delete them.

A start with its checkpoint or a copy of its table missing or not whole (a file the checkpoint's index
names and the cache lacks, a copy empty or cut short) stays failed: its unit's check of its files
(`flash-local-files.sh`, the head of its `ExecStart`, after the memory guard) waits 90 s
for a disk mounted late at boot, then says which file is missing and exits 78, which systemd does not
start the lane again on, and the cockpit shows **cannot start: a file is missing**. With the disk of
the cache mounted, a Load from the cockpit, or `./switch-model.sh flash-local`, brings the files back
([engine-restarts.md](engine-restarts.md)); with it away, the Load would fetch the checkpoint onto the
system's disk.
