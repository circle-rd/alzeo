# opencode integration

What the installer writes for [opencode](https://opencode.ai), why each piece is there, and how to opt out.

The installer writes a complete, ready-to-use [opencode](https://opencode.ai) config at `~/.config/qwen38/opencode.json` (the API key is referenced via `{file:...}`, no secret inside). It contains one provider per installed lane (`qwen38` for the 27B, `flashnext` for flash, `flash-local`, `kolibri`, `nemotron`, `nemotron-super` and `ornith-122b`), the three Qwen ones with `lean` / `low` / `medium` / `xhigh` reasoning-effort variants (no variant = the patched template's default, `lean`, or Qwen's `xhigh` on a box installed with `LEAN_DEFAULT=0`, and always `xhigh` on the Qwen3.8 Flash-Next FP4-Local lane, its model card's default; the vLLM ones offer `none` / `low` / `medium` / `high`, but Ornith's `none` / `high`, as its template turns reasoning on or off and nothing between), and its default model follows the installed target (`./switch-model.sh` re-points it on every switch):

A box with no opencode config of its own gets this one as `~/.config/opencode/opencode.json`
(since v1.18.4: before, it was printed as a `cp` command, and a first install left opencode,
`oc` and the Agent view with no provider for the box's own model, "Provider not found: qwen38").
A config you already have is kept: the installer merges the served lane's limits into it,
and adds the provider of a lane installed since (the 27B first, the flash lane later) when
it already has one of this repo's providers. When it has none of them, the installer says
so and leaves it alone: merge this repo's provider blocks (`qwen38`, `flashnext`, `flash-local`, `kolibri`, `nemotron`, `nemotron-super`, `ornith-122b`) from
`~/.config/qwen38/opencode.json` into it yourself.

```bash
oc        # opencode on this box's model, with the output cap lifted (see below)
```

`oc` and the cockpit's Agent view also load that generated config through
`OPENCODE_CONFIG`, which opencode reads over your global one: their default is the model
this box serves even when your own config names another, and opencode can never fall back
to its free hosted model there. That fallback is what a fresh box got before v1.18.4 (with
no provider for the box, opencode answered with `big-pickle`, a cloud model). Plain
`opencode` reads only your own config.

## Which opencode: one pinned version, on every box

Four things this repo does were read out of one opencode binary and checked against its
behaviour: the compaction threshold `oc-fit-limits.py` sizes the limits for, the hidden
32,000-token output cap the `oc` launcher lifts, the overflow phrases the proxy answers
with so that a refusal makes opencode compact instead of failing, and `--yolo` /
`OPENCODE_PERMISSION` for the Agent view. So `install.sh` pins the opencode it runs:
**1.19.5** (`OPENCODE_VERSION`, with the sha256 GitHub publishes for
`opencode-linux-arm64.tar.gz` in `OPENCODE_SHA256`). CI runs the Agent view against that release
on its arm64 runners, with that digest; `dashboard/tests/agent-bench.mjs` also carries the digest of
`opencode-linux-x64.tar.gz` for the same release (`X64_SHA256`), for a run on an x86_64 machine: a new pin needs both. 1.18.32 was checked against the
1.18.27 the repo was measured on, on 2026-09-23: the same 21 overflow phrases and 3
exclusions verbatim, `max_tokens` 182,000 with the launcher's variable and 32,000 without
it on both (recorded on a fake endpoint), the same compaction and permission code once the
bundler's chunk names are set aside, `serve --hostname` and the hidden `--yolo` on both.
1.18.35 was checked against 1.18.32 the same way on 2026-10-10: the list of phrases and
exclusions is the same block of the binary byte for byte, `max_tokens` 32,000 and 182,000
on the fake endpoint again, `serve --hostname` and `--yolo` there, and the release diff
touches neither the compaction nor the permission code. What changed for this box: its
model requests carry one more header, `x-opencode-session-id`, with the session id
`x-session-id` already carried (the proxy and the engine pass it by), and `opencode debug
config` prints a provider's key as `***`, where 1.18.32 printed this box's key in clear.
1.19.4 was checked against 1.18.35 on 2026-10-22, the binaries side by side: the same 21
phrases and 3 exclusions, `max_tokens` 32,000 and 182,000 on the fake endpoint with the same
headers, the release diff leaves the compaction, overflow, permission, flag and request code
as it was, keys still masked, and the sessions one wrote the other lists and reads, both ways.
Behind a copy of the cockpit's relay, reached by a name that is not loopback as the tailnet
address is, both answered a prompt from the interface and ran a command in its terminal, and
the phone layer found the same parts of the interface. One thing changed for the relay: 1.19.4
sends its index chunked, where 1.18.35 sent a length, and the relay injected the phone layer
only into a document with a length (it reads a chunked one too since v1.38.6). It runs on
Bun 1.4.4 (read in its binary), whose Brotli carries the fix for CVE-2026-111497, an
out-of-bounds write in the decoder: 1.18.35 runs on Bun 1.3.14, without it, and the agent's
web fetch decodes whatever a page sends.
1.19.5 was checked against 1.19.4 on 2026-10-30, the binaries side by side again: the list of
phrases and exclusions is the same block byte for byte, `max_tokens` 32,000 and 182,000 on the
fake endpoint with the same headers, the same default permissions, `--yolo` still read as
`--auto`, keys masked, and the sessions one wrote the other lists and reads, both ways. Behind
a copy of the cockpit's relay, both answered a prompt, ran a command in the terminal and passed
the phone layer, and an endpoint that asked for a shell command naming an aliyuncs.com host had
it refused by both, under `--yolo` and under `AGENT_AUTO`'s permission. The release diff lets a
terminal's ticket stand for the password only on the route the server dispatches the request
to, no longer on a pattern of its path, answers a request of the TUI's worker that fails
instead of leaving it waiting, updates the package of GitLab's provider, which this repo does
not use, and otherwise removes unused code: the compaction, overflow, permission, flag and request
code is as it was. It runs on the same Bun 1.4.4.

What a run does with the opencode it finds:

| found | what `install.sh` does |
|---|---|
| none | downloads the pinned release, checks its sha256, installs it where opencode's own installer does (`~/.opencode/bin`) and adds that to `PATH` in `~/.bashrc` |
| the pinned version | nothing |
| an older one in `~/.opencode/bin` | replaces it the same way, checked |
| an older one elsewhere (npm, bun) | `opencode upgrade <pinned>`, opencode's own upgrader, which knows how it was installed |
| an older one from opencode's own Homebrew tap (`anomalyco/tap`, `sst/tap` before the project moved, as its keg's install receipt says) | the same upgrader, which runs `brew upgrade` and so moves it to the tap's own version, a 1.x (the pinned one on 2026-10-22) |
| an older one from Homebrew Core (or a keg whose install receipt names no tap) | keeps it and says how to get the pinned one: `brew upgrade` installs the formula's own version whatever version it is asked for, Homebrew Core's formula is OpenCode 2 now (2.1.2 on 2026-10-22), whose server is not the 1.x `opencode serve` the Agent view relays, and up to 1.19.1 the upgrader took it (`brew uninstall opencode` and a new run of the installer, or `brew install anomalyco/tap/opencode`, opencode's own 1.x formula) |
| a newer one | keeps it and says so: going back a version can leave sessions a newer opencode wrote unreadable |

Up to 1.18, left alone, opencode installed its own patch releases (read out of its binary:
only `"autoupdate": false`, `"notify"` or a minor/major release stopped it), which is how
two boxes installed a week apart ended up on two versions. 1.19 installs nothing by itself,
but with any value other than `false` its TUI checks every 10 minutes and, once it is on
the newest 1.x, shows "/update to upgrade to OpenCode 2.0" on its home screen; 1.18.35
announces 2.x as soon as GitHub's latest release is one. OpenCode 2's server is not the
1.x `opencode serve` the Agent view relays. So, with the pin, the generated config and
yours get `"autoupdate": false`: no check and no notice (`OPENCODE_DISABLE_AUTOUPDATE=1`
does the same for one run). The Agent view says when the opencode it serves is not the
pinned one. An update that installs the cockpit restarts the Agent view's server on a new
opencode, after the cockpit that relays it, whatever lane the box boots on and however that
opencode came (since v1.38.6: before, only an update with a text lane at boot did, and only
for a binary dated after the server's start, which npm and Homebrew do not give). An update
that installs no cockpit (`--no-cockpit`, or `--no-start` on a box booting a text lane) leaves
the server on the opencode it runs until it restarts.
`dashboard/install-agent.sh` never points it at OpenCode 2, whose server the Agent view is
not written for yet: it says so and keeps the opencode it runs.

`OPENCODE_PIN=0 ./install.sh` keeps whatever opencode you have and leaves `autoupdate`
alone. `OPENCODE_VERSION=<x> OPENCODE_SHA256=<its digest>` pins another release (a
version without its checksum is refused: it would not be a pin). A download that does
not match its sha256 is not installed, and nothing about opencode fails the rest of the
install.

## Opting out

Do not want any of it? `./install.sh --no-opencode` (one-liner: `| bash -s -- --no-opencode`) installs the API only: no generated config, no `oc` launcher, and `switch-model.sh` never touches your opencode default model. The choice is remembered by later runs (marker `~/.config/qwen38/opencode.off`); `./install.sh --with-opencode` turns it back on. Your own `~/.config/opencode/opencode.json` is never rewritten in either mode: when the integration is on, the installer only merges into it the served lane's limits, its compaction block, the provider of a lane installed since, the default model when it is unset or already one of this box's (a default you pointed at another provider is yours, and stays) and, with the version pinned, `"autoupdate": false`, plus the shell deny rules (a deny holds under `--yolo`) and a `lean` variant for the 27B and flash models, each by a targeted edit that keeps your comments (in every form opencode accepts: `//`, `/* */`, trailing commas), your other providers and your line endings, with a dated backup first (the ten newest of each file are kept) and in one step, so a full disk leaves the file as it was.

What the shipped config gets right for you:

1. **The hidden 32K output cap**: opencode sends `max_tokens = min(limit.output, OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX or 32000)`. Without that env var, a long thinking phase hits 32000 tokens, the turn ends silently (`finish_reason: length`, no text, no tool call) and you have to re-prompt. The installer ships an **`oc` launcher** (`~/.local/bin/oc`, skipped if an unrelated `oc` binary exists) that exports the right value and execs `opencode --yolo`: launch with `oc` instead of `opencode` and the cap sits above every output limit this repo declares, on every target and in either context mode. It is deliberately a ceiling rather than the installed target's own number, because `limit.output` is what follows a model switch: a ceiling copied from the installed target survives the switch and cuts the next lane's turn in the same silence (`./oc-limits.sh --max-out` is where both installers read it). Note that opencode's defaults already run every tool, the shell included, without asking, and `--yolo` also approves what would ask (how the reference box runs): for a prompt before each shell command, put `"*": "ask"` in the `"bash"` object of your config and start plain `opencode` ([agents and the network](agents-and-network.md)).
2. **Limits that can never 400**: the server rejects any request where `input + max_tokens` exceeds the window (no clamping), and the prompt opencode sends can reach its compaction point (`limit.input` minus 20,000) plus one agent step (up to 43,863 tokens measured on this box). So the config ships `context/input 173000, output 64000` in native mode (173,000 - 20,000 + 43,863 + 64,000 = 260,863, under 262,144, whether the 32K cap is lifted or not), `205000/32000` on the flash lane, and `700000/200000` in 1m mode as a starting pair. Until v1.18.7 the native pair was 194048/64000 and the flash one 225000/32000: both fitted a prompt of exactly `context`, not the one a large step brings past compaction, so a request in that band got a 400 that opencode then compacted on. The 1m pair is a starting pair, which the installer then fits to the pool the boot actually got (`oc-fit-limits.py`, after step 9): the pool changes from boot to boot (832,993 to 922,094 tokens over sixteen 27B boots on the reference box), and that pair does not fit the smaller ones. Since v1.18.2 the cockpit does the same fit by itself after a switch and a Start. Both fits hold the same window rule, and on a 262,144 window they never go above the lane's pair above (v1.18.7).
3. **Reasoning-effort variants**: the generated config declares `lean`, `low`, `medium` and `xhigh` variants (ctrl+t in the TUI), `lean` first because it is what a turn that picks none gets: the patched template's default since v1.13, measured in [LEAN.md](../LEAN.md) (`LEAN_DEFAULT=0 ./install.sh` keeps Qwen's `xhigh` as the default and still offers `lean`). This works because the patched template accepts `lean` and maps other clients' effort tiers (`max`/`high` → `xhigh`, `minimal` → `low`, [contributed by helge](https://forums.developer.nvidia.com/t/380257/10)); any client sending an unmapped tier would get a 500 on the stock template.
4. **Mid-conversation system messages**: some agent clients inject system messages after turn 1, and opencode 1.19.4 sends two before the first user message; the stock template raises `System message must be at the beginning`. Patched to render them as `<system-reminder>` blocks, on every lane whose template raises it: the 27B, the two flash lanes and, since v1.40.2, Ornith.
5. **Vision declared**: `attachment` + `modalities` are set, so image attachments and on-disk image reads work end to end (the model is natively multimodal).

On service installs the generated config points at the **keepalive proxy port** (`PORT+1`), not the server directly, and that is deliberate: SGLang buffers tool-call arguments while they stream (127 s of measured silence on one 400-line file write, at native context), and opencode drops a stream after roughly 140-180 s without a real chunk. The proxy (`qwen38-keepalive.service`, vendored `keepalive-proxy.py`) fills those silences with protocol-correct keepalives, at SSE event boundaries only, and makes sure a client that gives up does not leave a generation running (v6.14: it names every request with `x-override-rid` so it can abort one that has not produced anything yet, aborts before closing the socket, and drains the answer where the engine offers no rid to abort with). Since v1.22.5 (proxy v6.31) it also keeps a stream the engine has not started: SGLang sends a stream's headers with its first chunk, after the queue and the prefill, and opencode 1.18.32 gives up on a response whose headers have not come in 300 s (`headerTimeout`, its default) and sends the request again, while the engine keeps the one it gave up on queued. The proxy opens such a stream itself after 10 s and keeps it alive until the engine answers, and a caller that leaves before then has its request ended in the engine. Several agents at once (opencode's sub-agents, or several sessions) share the lane's KV pool, and past it every turn computes its whole prompt again: keep the sum of their contexts under the pool ([flash-lane.md](flash-lane.md#several-agents-at-once-their-contexts-share-one-pool) has the measurements). With `./install.sh --no-service` there is no proxy: the config then points at the server directly, and huge single-file writes may abort. One more caveat, measured: SGLang's `--api-key` only accepts `Authorization: Bearer`, **not** `x-api-key`.

[Back to the README](../README.md)
