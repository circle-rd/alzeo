# Client compatibility

The stack serves two dialects through one hardened entry point. This page
says which, what each client must send, and what is measured on the
reference box versus what is merely how a client is configured.

The address to give a client, the model that serves and where the key is are in
the cockpit's Now view, ready to copy ("Connect a client", since v1.45.1), with
the box's other addresses when the cockpit is opened on the box itself.

## The surface

Agent clients should connect to the **keepalive proxy** (default
`:30001`), not the engine directly: it injects the SSE keepalives that stop
client watchdogs from killing long prefills, aborts decodes when clients
disappear, caps and refuses what cannot be served with a body a client can
act on, and aborts decodes that emit the corruption marker. The engine
port (`:30000`) speaks the same APIs without that protection, and **since
v1.17 it binds `127.0.0.1`, so it answers on the box only**. That is the
whole arrangement in one line: one door, and it is the one with the lock.
Since v1.22.13 the proxy checks that lock itself, with the engine's own rule, before it
reads a request's body: a request without the key gets the engine's own 401 at once, and
`/health`, `/ready` and `/metrics` stay open, as the engine keeps them.
`ENGINE_BIND=0.0.0.0 ./install.sh` puts the engine back on the network for
a box that wants it, and keeps it there across updates. An engine on the network
that only an install before v1.17.0 put there, and that nothing records as
chosen, moves to `127.0.0.1` at the first update of v1.36.0 or later, which says
so before its first step.

**The proxy also refuses the requests that take the engine down instead of being
refused by it.** One is a prompt past the KV pool (measured here on 2026-08-29: queued, never
admitted, and nothing generated for anyone until a restart),
which is why sending a monster straight to `:30000` wedges the scheduler and sending it
through `:30001` gets a clean 400. Since v6.20 the other is an oversized logprob request:
OpenAI documents `top_logprobs` as "an integer between 0 and 20" and SGLang declares it
`Optional[int]` with no bound at all, so the number travels unexamined to
`logprobs.topk(max_k)` and, the moment it passes the vocabulary, raises `selected index k
out of range` **inside the scheduler**. One request from one client ends the engine for
everybody, and this box needs about nine minutes to boot again
([sglang#40076](https://github.com/sgl-project/sglang/issues/40076), open since
2026-09-18; the field is still unbounded in `v0.5.19` and in `v0.5.21`, checked in both images).
The proxy refuses anything above `TOP_LOGPROBS_CEILING` (1,024) on the three routes that
carry the number under three different names: `top_logprobs` on `/v1/chat/completions`,
`logprobs` on `/v1/completions`, and `top_logprobs_num` on `/generate`, the last one
element by element when a batch sends a list. It refuses rather than quietly lowering the
number, because a narrowed top-k answers a different question than the one that was
asked. The ceiling is not the vocabulary, which the engine publishes nowhere: no vocabulary
in use is smaller than 32k, OpenAI's own maximum is 20, and the System One readout asks 261
with its shipped caps (594 with the widest an operator can set), so nothing above 1,024 can
be a client asking for logprobs. `TOP_LOGPROBS_CEILING=0` turns the refusal off for an
operator who knows their build is patched; on this one it is not, so leave it alone.

Three more fields of that family were catalogued upstream in July with reproductions
([sglang#31597](https://github.com/sgl-project/sglang/issues/31597)) and are still
unbounded, because **both PRs that bounded them were closed without being merged**, which
is also why the logprob one had to be reported again in September. All three are reachable
from an ordinary chat request, and the proxy refuses them too: a `stop_token_ids` entry
past the vocabulary indexes a `scatter_add_` out of bounds whenever `min_new_tokens > 0`,
which on CUDA is a device-side assert that takes every in-flight request with it; an
`input_ids` entry does the same to the embedding (`_validate_input_ids_in_vocab` exists in
the image and has zero callers); and `n` becomes `parallel_sample_num` with no bound at
all, expanding a list before anything is scheduled, so it is a memory exhaustion rather
than a crash. `n` is held at `MAX_PARALLEL_SAMPLES` (128, which is OpenAI's own maximum).
The two id fields need the vocabulary, and since the engine publishes it nowhere the proxy
asks for it the only way it is offered: `logit_bias` is the one field SGLang does validate,
and it is refused with "logit_bias must has keys in [0, 248319]". One probe an hour, built
to be refused, so it never reaches the scheduler; it matched this checkpoint's own
`config.json` to the digit. **A negative id is refused whatever happens, and when the probe
cannot run the rest of the guard stands down rather than refuse traffic it cannot judge.**

One more is reachable from `/generate`, and from the `parameters` of `/vertex_generate`:
`token_ids_logprob`. On a batch that mixes a request asking for it with one that does not,
the 27B build served until v1.22 (`v0.5.19`) fills a bare list for the one that did not ask
and then calls `.tolist()` on every entry, so the scheduler dies on the first mixed batch
([sglang#34719](https://github.com/sgl-project/sglang/issues/34719), still open). `v0.5.21`
and the flash build fill an empty tensor there instead (their logprob files are identical,
checked on 2026-10-09), and the issue's two-request reproduction, run 20 times on a `v0.5.21`
boot in batches that mixed the two kinds of request, no longer killed its scheduler
(2026-10-10). While the issue is open the proxy keeps refusing the field: since v1.22.13 it
refuses any value the engine would act on; an empty one, which the engine itself treats as
absent, passes. `top_logprobs` is still relayed.

| | path | dialect |
|---|---|---|
| chat | `POST /v1/chat/completions` | OpenAI |
| models | `GET /v1/models` | OpenAI |
| messages | `POST /v1/messages` | Anthropic-compatible (Claude Code, Claude-family SDKs) |
| health | `GET /health` | plain text; stays open even when client keys are on |

Authentication on every path except `/health`: `Authorization: Bearer
<key>`, the key in `~/.config/qwen38/api-key` (0600). The Anthropic
dialect is Bearer-only: an `x-api-key` header is not read. The engine
enforces the key; the proxy forwards it verbatim. A client's `x-override-*`
headers stay at the proxy, `x-override-rid` aside: the SGLang lanes start their
engine with the header overrides on, for the request id the proxy names, and the
others set fields of its scheduler no client of this port has to set (proxy v6.42).
A client of a listed key never names the engine's request id (proxy v6.51): its
`x-override-rid` and `X-Request-Id` headers stay at the proxy, and so does any id in
its body. Bodies are JSON in UTF-8: another encoding gets a 415.

Served model names: `qwen3.8-27b` (27B lane, both context modes),
`qwen3.8-flash-next` (flash lane), `qwen3.8-flash-next-fp4` (Qwen3.8 Flash-Next FP4-Local lane),
`kolibri-1` (Kolibri lane), `nemotron-3.5-bolt` (Nemotron lane), `nemotron-3.5-super-vl`
(Nemotron Super lane) and `ornith-1.5-122b` (Ornith lane). Send the name, not the checkpoint path. A client set up for one lane
keeps working on another: SGLang answers any name, and on the vLLM lanes, whose vLLM
refuses a model it does not serve, the proxy gives such a request the served name
(proxy v6.37).

The Kolibri lane (vLLM, [docs/kolibri-lane.md](kolibri-lane.md)) differs from the
Qwen lanes in three things a client sees: its reasoning comes in the `reasoning` field
of the message (vLLM's name; the Qwen lanes say `reasoning_content`), its levels are
its own (`low`, `medium`, `high`, which its template defaults to, and `none`, in
`chat_template_kwargs.reasoning_effort` or the top-level `reasoning_effort`), and it
has no vision. `/v1/systemone` needs one of the SGLang Qwen lanes and says so there.

The Qwen3.8 Flash-Next FP4-Local lane (vLLM, [docs/flash-local-lane.md](flash-local-lane.md)) serves the flash
model with the flash lane's chat template, so a client sees the Qwen levels below, `lean`, `low`,
`medium` and `xhigh`, with one difference: a request that names no level gets `xhigh`, its model
card's default. `none` turns the reasoning off there too (the proxy says it in the template's words,
v6.43). Its reasoning comes in the `reasoning` field (vLLM's name), it reads images sent inside the
request (a data URL) and fetches no URL, and its window is 262,144 tokens (1,048,576 with `FLASH_LOCAL_CONTEXT=1m`, docs/flash-local-lane.md). `/v1/systemone` is not
served on it: the endpoint says which lanes serve it.

The Nemotron lanes (vLLM, [docs/nemotron-lane.md](nemotron-lane.md) and
[docs/nemotron-super-lane.md](nemotron-super-lane.md)) also say their
reasoning in the `reasoning` field. Their templates read no effort level: each reasons at its
full depth unasked, `{"enable_thinking": false}` in `chat_template_kwargs` turns that off and
`{"medium_effort": true}` asks for its shorter reasoning. A `reasoning_effort`, top-level or in
`chat_template_kwargs`, is said in those words by the proxy (v6.40): `none` and `minimal` turn
the reasoning off, `low` and `medium` ask the shorter one, `high` and above the default; sent
straight to the engine, `none` left the answer inside the reasoning. Each reads images and
videos sent inside the request (a data URL), 16 images and 2 videos per conversation; neither
fetches a URL or reads a file of the box for a client. Bolt's window is 262,144 tokens, Super
VL's 1,048,576.

Reasoning effort is a first-class field in both dialects: `lean`, which a
request that names no level gets, and Qwen's own `low`, `medium` and `xhigh`
(the template maps `max`/`high` to `xhigh` and `minimal` to `low`; the levels
and what each costs are in [LEAN.md](../LEAN.md), and a box installed with
`LEAN_DEFAULT=0` keeps Qwen's `xhigh` as the default). Long turns cost time at the
engine's speed, not yours: an `xhigh` turn can stream for minutes on a
correct answer, and several clients have wall-clock watchdogs that read
that as a hang. Prefer `medium` for interactive editing, `low` for
mechanical edits, and keep `xhigh` for the hard turn.

Oversize behavior is the same on every lane: nothing is truncated. No unit or
launcher passes `--allow-auto-truncate` (a CI gate refuses it), so a prompt a
lane cannot serve comes back refused rather than silently cut. The proxy
refuses a prompt past 92% of the lane's KV pool (`OVERSIZE_MARGIN_FRAC`, 0.08)
with a 400 `context_too_long` (`code: context_length_exceeded`, the code
opencode compacts on), whose message names the count, the limit and the pool;
while a flash lane serves at its 262K window (Flash-Next, or FP4-Local without its 1M option), the limit is also held under its one-prompt
ceiling (`FLASH_PROMPT_CEILING_TOKENS`, 250,000 by default), which keeps a long
flash prefill off the memory edge. The engine itself refuses a request whose
prompt plus `max_tokens` passes its window (262,144 tokens on most lanes;
1,010,000 on the 27B in 1M mode; 1,048,576 on Super VL, Ornith, and FP4-Local
with its 1M option). A 413 means a body over `MAX_BODY_BYTES` (256 MiB), refused before a
byte of it is read.

The network's share of the port is bounded (proxy v6.41). Another machine holds
at most 64 connections open at a time (`PROXY_MAX_CONN_PER_ADDRESS`) and the
network 256 in all (`PROXY_MAX_REMOTE_CONN`): past either, a new connection is
closed before it is read, and the proxy's journal says why, once a minute. Such
a connection has 30 s to send its request line and headers (`CLIENT_HEADER_S`),
64 KiB of them in all (`HEADER_BYTES_MAX`, a 431 past it: the largest the box's
clients send is under 1 KiB), and once they are in, 30 s at most without a byte
while its body comes in (`BODY_STALL_S`; the prefill and the answer come after,
and nothing waits on the client then). The clients on the box itself (127.0.0.1)
are not counted. `/health`, `/ready` and `/metrics` need no key and take no body:
a body there gets a 413 before it is read, and any other path under them, such
as `/health_generate`, needs the key. The request bodies in
flight share 384 MiB (`BODY_BUDGET_BYTES`), taken as their bytes arrive: one
that finds no room gets a 503 with `Retry-After` (the oldest one being read waits
up to 30 s for room first), and a body of 1 MiB or less always comes in. A compressed body (`Content-Encoding: gzip` and the like) gets a 415:
every guard reads the body as it is sent. A gateway that relays many users from
one machine may need a higher `PROXY_MAX_CONN_PER_ADDRESS`, set in a drop-in as
below (`0` turns a ceiling off).

## Measured on the reference box

- **opencode**: the primary client; the installer writes its provider
  config (effort variants, default model follows the target). Everything
  in README applies through the proxy.
- **Claude Code**: works through the proxy's Anthropic route
  (`/v1/messages`). **`claude-local`** runs it on the lane the box serves (the
  installer puts it in `~/.local/bin`, beside `oc`): it sets, for the one
  process it starts, the proxy, the key (read from its file at each launch,
  in the environment only), the model that serves, the timeouts below, and the
  context and answer budget that fit that lane, then runs `claude` with your
  arguments. Your `~/.claude` is left alone. The context, the answer budget, the
  model and the timeouts you set yourself are kept (an extra body of yours without
  `max_tokens` gets the lane's, and an `ANTHROPIC_MODEL` naming a Claude model is
  said, as Claude Code then sizes the context for that model); the endpoint and the
  key are always the box's, unless `CLAUDE_LOCAL_URL` names another proxy. It
  reaches the box directly, never through an HTTP proxy your shell names, and tells
  Claude Code to do the same. The installer leaves any other `claude-local` where it is, but
  for the one the reply to discussion #59 gave to copy there (2026-10-22): that
  one is kept beside it as `claude-local.from-59`, and replaced. The pair comes
  from the lane's opencode limits, which the installer and the cockpit fit to
  its pool, its window and the proxy's ceiling:
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` is opencode's context plus 23,000, so Claude
  Code's longest prompt is opencode's context, and `max_tokens` is opencode's
  answer budget (228,000 and 32,000 on Qwen3.8 Flash-Next FP4-Local, 578,000 and 185,000 on
  the 27B in its 1M mode). opencode may reach one worst step past its context,
  and that step is the margin left for what Claude Code adds to a prompt and
  only estimates. A lane opencode has no limits for gets 205,000 and 32,000,
  which fit every lane's window, every pool measured on a 128 GB box and Bolt's
  on a 64 GB one; the 27B's pool on a 64 GB box is smaller (opencode's fit gives
  it 37,000 and 12,000 there), and with opencode off it is the proxy's refusal of
  a prompt past the pool that makes Claude Code compact. It does not set `CLAUDE_CODE_ATTRIBUTION_HEADER=0`:
  on the Ornith lane Claude Code reused 36 to 38% of its prompts from the cache
  with it and without it (2026-10-22).

  Since v1.40.2 a `claude-local` session starts in Claude Code's normal permission
  mode, which asks before each command, unless you pass a mode of your own
  (`--permission-mode auto`, or `--dangerously-skip-permissions`): an account set to
  auto mode had the box's own model judge which commands ran unasked. Claude Code's
  own commands, their aliases (`kill`, `plugins`, `upgrade`) and what it reads as its
  first argument but its help does not list (`daemon`, `remote-control`, `rc`, `remote`,
  `sync`, `bridge`, `self-hosted-runner`) get none of a session's flags, nor `--update`
  alone, so `claude-local kill <name>` stops a session. `agents`, which starts sessions
  of its own, gets them and hands them to those sessions. And nothing of the session goes to
  Anthropic, or to another provider. The model's requests, auto mode's classifier among
  them, go to the box, and `claude-local` cuts the rest, whatever your shell, your own
  settings or a project's say (an organization's managed settings come after them): Claude
  Code's nonessential traffic (telemetry, error reports, `/feedback`, updates, feature
  flags), the claude.ai connectors of a signed-in account
  (`ENABLE_CLAUDEAI_MCP_SERVERS=false`), the official marketplace's registration, auto
  mode's wait for the server's checks (`CLAUDE_CODE_AUTO_MODE_SERVER=0`), WebFetch's
  hostname check at api.anthropic.com (`skipWebFetchPreflight`), and the other providers
  Claude Code reads before the endpoint (`CLAUDE_CODE_USE_BEDROCK` and its siblings,
  emptied). The settings it passes with `--settings` carry these variables and the
  endpoint, never the key, as Claude Code applies a settings file's env over its own:
  with settings of your own, it passes none, and these are yours to set. A settings
  file of yours that sets its own `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_MODEL` still wins
  (the key never rides on the command line): leave them out of it. Your own MCP
  servers stay. Claude Code's built-in WebSearch runs at Anthropic and does not work on
  a local model: an MCP server of your own does the searching instead.

  By hand, the env block that makes it behave (the last two lines keep it off
  Anthropic, with `"skipWebFetchPreflight": true` in your settings, the same variables
  in their `env` if a settings file of yours sets them, and `CLAUDE_CODE_USE_BEDROCK`
  and its siblings left unset or empty):

  ```bash
  ANTHROPIC_BASE_URL=http://<host>:30001   ANTHROPIC_AUTH_TOKEN="<key>"
  CLAUDE_CODE_MAX_CONTEXT_TOKENS=228000    # a 262,144 window; with CONTEXT_MODE=1m, see below
  CLAUDE_CODE_EXTRA_BODY='{"max_tokens":32000}'
  API_TIMEOUT_MS=3600000
  CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS=1800000
  CLAUDE_STREAM_IDLE_TIMEOUT_MS=1800000
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1   ENABLE_CLAUDEAI_MCP_SERVERS=false
  CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL=1   CLAUDE_CODE_AUTO_MODE_SERVER=0
  ```

  With `CONTEXT_MODE=1m` the limit is the KV pool, not the 1M window.
  Claude Code (measured on 2.1.239, and again on 2.1.304 on 2026-10-23 against a
  fake server) compacts at that variable minus 33,000 input tokens, refuses to
  send past it minus 23,000, and asks for its `max_tokens` on top. On 2.1.304,
  for a model whose name is not a Claude one, it reaches the first of these
  through its reactive path: it sends the turn, and compacts if the server says
  the prompt is too long, as the proxy does. It compacts before sending at the
  second. The proxy refuses a prompt past 92% of the pool,
  prompt and answer together must fit in it, and the pool is decided at
  boot (832,993 to 922,094 tokens over sixteen 27B boots here; the
  cockpit's Lanes view shows yours). Set to the window, a session grows past
  what the proxy takes and is refused mid-conversation instead of
  compacting. Sized on the smallest pool: `700000` with the
  default output (worst case 709,000), or `529000` with
  `CLAUDE_CODE_EXTRA_BODY='{"max_tokens":250000}'` (worst case 756,000).

  Neither pair fits the flash lane, whose window is 262,144 tokens. With
  `max_tokens` at 250,000 every request passed it (15,563 prompt tokens plus
  250,000), the engine refused it inside its stream, and Claude Code (2.1.286)
  sent it again without a stream, which it gave up on after about 6 minutes:
  a long answer never came, and the proxy's hold during a restart could not
  keep such a request alive (measured on the reference box, 2026-10-02);
  `700000` lets a session grow past the window. On the flash lane: `228000`
  (opencode's 205,000 plus the 23,000 Claude Code keeps below its setting) with
  `CLAUDE_CODE_EXTRA_BODY='{"max_tokens":32000}'`, so that Claude Code's prompts stop where opencode's do there.
  `claude-local` already takes the pair of the lane that serves from that lane's opencode limits (above); a
  launcher of your own can do the same.

  The idle-timeout variables are the difference between "hangs" and "slow";
  they are named in issue #2 and in the failure family diagnosed in
  issue #1 (clients cap total turn duration, not idle time). Quality and
  tool calls were validated here.

  Measured again on the Ornith lane on 2026-10-30 with Claude Code 2.1.314
  and none of these set (issue #61): a prompt of 494,089 tokens the cache
  could not serve takes 19 min before its first token there, and Claude
  Code left one of 500,161 tokens after 620 s, twice, then sent it without
  streaming and left
  after 300 s, twice. The engine kept part of what it had computed only
  every other time (nothing after the first 10 min, 346,368 tokens after
  the next), so the answer came at the fifth attempt, after 35 min. Since
  v1.40.7 the proxy writes `RESENT` in its journal when a client sends a
  long prompt again after leaving it without an answer, with the client's
  name and what widens its timeout, and the cockpit shows it as a banner.
- **VS Code Copilot BYOK**: reports the same total-duration watchdog in
  issue #1 (VSCode Copilot gives no timeout env to widen it). The fix that
  worked for the reporter is the config shape in that issue: chat-completions
  apiType, the proxy URL, effort fields per the client's own schema; keep
  effort at `medium` or `low` while streaming turns are long.

## Standard OpenAI-compatible clients (configured here, not measured)

Anything that takes `base_url` + API key points at
`http://<host>:30001/v1` with the API key as the token, and may send
any model name: the lane that serves answers it (`qwen3.8-27b` on the
27B). Two examples because they are the ones asked for
most; the configurations are their vendors' standard OpenAI fields, and
the claims below about the engine side are the ones this repo has verified:

- **Open WebUI**: `OPENAI_API_BASE_URL=http://<host>:30001/v1`, key as
  above; model discovery via `GET /v1/models` answers through the proxy.
- **Cursor / Continue / Zed assistant**: OpenAI provider, same base and
  key. Vision requests pass the proxy's media pricing (each image is priced
  from its own header with the vision tower's geometry, measured at twelve sizes: CHANGELOG
  v1.10.1, "an image costs what it costs").

If you run one of these in anger and it disagrees with something here, a
box-report issue with the client's exact request body is the contribution
that moves this section.

## Optional TLS (v6.16)

For a client that will only speak TLS, or a network segment that requires
it, the proxy speaks TLS when you name the certificate, and only then. The
drop-in survives re-installs (`systemctl edit` writes a drop-in directory
that `install.sh` does not touch):

```bash
sudo systemctl edit qwen38-keepalive   # creates the override; content:
[Service]
Environment=QWEN38_TLS_CERT=/etc/ssl/qwen38/cert.pem
Environment=QWEN38_TLS_KEY=/etc/ssl/qwen38/key.pem
sudo systemctl restart qwen38-keepalive
```

Bring your own certificate: a tailnet CA cert, a LAN-trust one, whatever
your clients already trust. TLS 1.2 is the floor; a cert that cannot be
read or parsed makes the unit refuse to start rather than come up plain
(verified by `tests/test_proxy_tls.py`, including that no plaintext socket
survives beside the TLS one).

## Optional per-client identity (v6.16)

One key, one trust realm is the default, and it is fine for one operator.
To know which client sent what (and to stop sharing a single secret):

```bash
sudo mkdir -p /etc/qwen38
# the proxy runs as you and reads this file itself: it must be yours
sudo install -m 0600 -o "$(id -un)" -g "$(id -gn)" keys.json /etc/qwen38/client-keys.json
# the engine key goes in a file systemd reads, never in a unit line anyone can list
printf 'QWEN38_UPSTREAM_API_KEY=%s\n' "$(cat ~/.config/qwen38/api-key)" \
  | sudo install -m 0600 /dev/stdin /etc/qwen38/upstream.env
sudo systemctl edit qwen38-keepalive
[Service]
Environment=QWEN38_CLIENT_KEYS_FILE=/etc/qwen38/client-keys.json
EnvironmentFile=/etc/qwen38/upstream.env
sudo systemctl restart qwen38-keepalive
```

Until v1.18.7 this page installed the keys file owned by root, which the proxy (it runs
as your user) cannot read: it refused to start and every client of :30001 lost it. It
also put the engine key on an `Environment=` line, which `systemctl show` prints to any
local user; an `EnvironmentFile=` is read by systemd as root and is not printed.

`keys.json` is `{"<bearer-token>": "<label>", ...}`; each client sends its
own token as the Bearer value, the scheme in any case, as the engine reads it
(proxy v6.42). Identity is two keys, not one, and the two halves live in
different places on purpose: the client's bearer names them
(the guard checks the map and labels the journal line), and
`QWEN38_UPSTREAM_API_KEY` admits them (the proxy sends the engine's key
upstream on relays and abort calls alike). Without the upstream key the
client's token goes through verbatim and meets the engine's own key check
there, which is correct only for an engine with no check of its own: the
proxy warns about the combination at startup. A request whose token is not
listed gets a 401 in its own dialect and never reaches the engine, on every
route and every method; `/health` alone stays open, for monitoring. Until
v1.18.7 the wall only looked at POST requests under `/v1/`, while the proxy
attached the engine's key to everything it relayed: GET `/server_info` (which
carries the engine key in clear), `/generate`, `/flush_cache` and
`/abort_request` went through with no key at all, and so did a path escaped
twice (`/%2576%2531/...`), which SGLang decodes once more before routing. Since
v1.18.7 a path still escaped after one decode is refused, and `/server_info` is
relayed without the engine's key fields in either mode; since v1.22.6 (proxy v6.32)
without the key's value anywhere else in it either, where SGLang's `launch_command`
repeated it with the rest of the engine's command line. The label appears on
every journal line of the request (`journalctl -u qwen38-keepalive`), the first
one included since v1.18.7, as one word (a space or a control character in it is
written `_`), so per-client throughput and refusals become readable without a
telemetry component, and the cockpit's request feed reads those requests to
their end. A missing, empty, malformed or unreadable keys file stops the unit
at start: the wall is present or the unit is down, never silently absent.

## Typed decisions: TypeSafe SDK and HTTP (v6.19)

`POST /v1/systemone` speaks the wire contract of TypeSafe's Jev, served by the lane behind the
proxy (README, "Typed decisions"). The official SDK works unchanged against this box:

```bash
pip install typesafe-sdk
export TYPESAFE_BASE_URL=http://127.0.0.1:30001          # or the tailnet address, or https:// with the optional TLS
export TYPESAFE_API_KEY=$(cat ~/.config/qwen38/api-key)  # the serving key; a client key from the identity map works too
```

```python
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

with TypeSafeClient() as client:
    r = client.system_one(
        state={"document": "I was charged twice. Please fix this ASAP."},
        questions={
            "billing": Noul(instructions="Is this ticket about billing?"),
            "tone": Choice(instructions="What is the customer's tone?", criteria={"calm": None, "frustrated": None, "angry": None}),
            "urgency": Score(instructions="How urgent is this ticket?", criteria=["can wait", "this week", "today"]),
        })
print(r.nouls["billing"].noul, r.choices["tone"].choice, r.scores["urgency"].score, r.model)
```

Verified with `typesafe-sdk` 0.7.0 (`tests/test_proxy_systemone.py` runs the SDK round trip when
the package is importable). The SDK's default timeout is 10 s; a very large state prefilled
cold can take longer on this box, so pass `timeout=` for those. `model` may be any of Jev's
aliases or the lane's own name; the response's `model` is what the lane served. The one SDK
call that does not translate is `client.models.list()`: `GET /v1/models` keeps the OpenAI shape
that opencode and `bench.sh` read. `model` takes Jev's three
aliases (`jev-latest`, `jev-preview`, `jev-1.13.0`) or the lane's own name, and any other name
is refused the way the hosted API refuses it.

Errors are the hosted API's, checked case by case against it (BENCHMARKS.md, "The same bad
request, the same refusal"): **422** with a `detail` list whose entries name the path that
failed, for a request that violates the schema; **400** with a `detail` that is a message or an
object, for a request that parses and cannot be served (an unknown model, a Noul with neither
instructions nor criteria, more than 255 options, a state longer than this lane's prompt
ceiling); **529** with `Retry-After` when `SYSTEMONE_MAX_CALLS` calls are already in progress,
which the SDK retries on its own; **503** with `Retry-After` while the engine restarts, in the
relay path's shape; **502** when the engine answered a branch with no usable distribution. The
SDK reads the message out of every one of those shapes.

Two behaviours worth knowing before you point a crowd at it. **Admission**: eight typed
decisions run at once and eight engine requests behind them, so the ninth caller gets 529
with `Retry-After` and the SDK sleeps it and sends the same call again; both numbers are
env vars (`SYSTEMONE_MAX_CALLS`, `SYSTEMONE_MAX_INFLIGHT`) and both came from a load curve
(BENCHMARKS.md, "Under load"). **A call you abandon stops**: the SDK's default timeout is
10 s, a cold fan-out on a large state takes longer, and when your socket closes the proxy
stops the fan-out and tells the engine to drop the branches it still holds. Pass a bigger
`timeout=` rather than relying on a retry to be cheaper than the first attempt.

