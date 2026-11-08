# A second ASUS Ascent GX10, from one command

The project is developed and measured on an ASUS Ascent GX10, the reference box. In October 2026 a
second GX10 was set up the way a new owner sets one up: a fresh DGX OS, its own update from the DGX
Dashboard, then the one command of the README. This page is what it measured, on 2026-10-20 and 21.

## At a glance

| | The second GX10, new | The reference GX10 |
|---|---|---|
| One command on a new box, to the first token of Qwen3.8-27B | **97 min**, downloads included (v1.37.2) | |
| Qwen3.8-27B, one stream (`bench.sh`, greedy median) | **62 to 64 tok/s** | 72 to 73.5 tok/s |
| Qwen3.8 Flash-Next FP4-Local (176B), one stream (`bench.sh`, greedy median) | **52 to 53 tok/s** | 53 tok/s, the same day |
| GSM8K (1,314) / MMLU (500) / HumanEval (164), on the 27B | **97.7% / 88.2% / 95.7%** | 97.6% / 87.6% / 97.6%, the same morning |
| One prompt of 400,025 tokens, a passphrase hidden in it | **returned exactly**, in 821 s | |
| A coding agent alone (opencode on the 27B), from one paragraph | **a command-line app and 21 passing tests, in 117 s** | |
| An image, 1024×1024 at 40 steps (Qwen-Image 2.1) | **38 to 43 s** | 34 s |
| A 4-second video with sound at 480p (MiniMax-H3) | **146 s** with its Turbo, 621 s without | 127 s, 623 s |
| SoC temperature | 44 to 47 °C at rest, up to 95 °C under long prefills and video | |

The one-command figure is the whole first install of a new box: the checks, the engine image and
the checkpoints (about 41 GB, on a line that lost up to 22.5% of its packets), and the first boot of the
engine. The other lanes were installed ready to load, each fetching its weights at its first Load
from the cockpit.

## What a new box taught the installer

A GX10 in daily use hides what a new one lacks. The second GX10 showed three things, each fixed and
released within the day:

- Its first account is not in the docker group, and DGX OS 7.6.0 has neither python3-dev nor ffmpeg.
  The installer now adds the account, goes on in the same terminal, and installs both
  ([v1.37.2](../CHANGELOG.md)).
- The image and video lanes compile a kernel at its first use with `ninja`, which their venv has but
  their units left off PATH: the first image of a new box failed ([v1.37.4](../CHANGELOG.md)).
- A long update ran under the cockpit's banner asking to run the installer, and the Copy buttons
  failed on a cockpit opened by the box's address. The banner now says an install is running, and
  every Copy button works over plain HTTP ([v1.37.5](../CHANGELOG.md)).

## The two GX10s, side by side

They run the same software and the same memory (LPDDR5 at 8533 MT/s), at the same GPU clocks. They
differ in kernel (7.0.0-1019, which the new box's own update installed, against 6.17.0-1032) and in
firmware (BIOS 0106 against 0105). Their answers are the same. On Qwen3.8 Flash-Next FP4-Local their speeds are
within 2%; on the 27B lane the new box decodes 13% slower, and its GPU copies memory 10% slower (212
against 237 GB/s). Which of the two differences causes that was not isolated: it takes a reboot into
the other kernel or firmware.

## How it was measured

- Speed: `./bench.sh`, three runs or more on each box, the median of each run.
- Quality: GSM8K with SGLang's `run_eval`, MMLU and HumanEval with the drivers in [evals/](../evals/),
  the same scripts and engine image on both boxes the same morning.
- The long prompt: [needle.sh](../needle.sh), sent through the proxy as a client sends it.
- Methods and older measurements: [BENCHMARKS.md](../BENCHMARKS.md).
