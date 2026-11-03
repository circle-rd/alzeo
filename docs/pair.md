# Two boxes behind one endpoint

Two boxes that each run Alzeo can answer as one. Clients talk to either box, and each request goes to the
box that serves the model it names. One box can keep the 27B for your agents while the other loads Flash
or another lane, and the clients of either box see both models behind one address. This is the first
stage of [discussion #45](https://github.com/hasso5703/alzeo/discussions/45). Splitting one model across
both boxes over the ConnectX-7 link is a later stage.

## Before you start

- Both boxes run this version of Alzeo: `curl -fsSL https://get.alzeo.ai | bash` on each.
- SSH works from one box to the other without a password, as step 4 of NVIDIA's
  [connect-two-sparks playbook](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/connect-two-sparks)
  sets it up: `ssh <the other box> true` must work without a question.
- The two boxes reach each other on port 30002/tcp, with no firewall in between. Any link both boxes
  share will do: the ConnectX-7 cable, your LAN, or a tailnet.

## Pair the boxes

On either box, one command, with the other box's address as this box reaches it:

```bash
python3 ~/dgx-spark-qwen38/pair.py add 192.168.100.2
```

It pairs both boxes at once, checks that each box reaches the other, and leaves both boxes as they were
when a check fails (a pair they already had included), with what to fix. Both proxies take the pair within
a few seconds: nothing restarts. The two boxes' clocks need not agree.

```bash
python3 ~/dgx-spark-qwen38/pair.py status          # this box's pair, and what each box serves now
python3 ~/dgx-spark-qwen38/pair.py remove spark2   # unpair: both boxes alone again
python3 ~/dgx-spark-qwen38/pair.py name spark1     # the name this box takes in a pair, before pairing
python3 ~/dgx-spark-qwen38/pair.py identity        # this box's certificate and its fingerprint
```

A box takes its host name in a pair unless you name it first. When ssh reaches the other box by another
name or user, give it: `--ssh user@host`. When the other box reaches this one at another address than the
one its SSH server sees, give that one: `--back 192.168.100.1`.

## What a client sees

- A request that names a model of one of this repo's lanes (`qwen3.8-27b`, `qwen3.8-flash-next`,
  `qwen3.8-flash-next-fp4`, `ornith-1.5-122b`, `nemotron-3.5-bolt`, `nemotron-3.5-super-vl`,
  `kolibri-1`) goes to the box that serves it, streamed or not, in the OpenAI and Anthropic dialects and on
  the Ollama (`/api/chat`, `/api/generate`) and SageMaker (`/invocations`) routes.
- Any other name (`claude-*` from Claude Code's background tasks, `gpt-*`, none) stays on the box it was
  sent to, as on a box alone.
- `GET /v1/models` lists the models of both boxes, each with a `box` field. It never waits for the other
  box: its list is read in the background and kept for a few seconds.
- A lane name that no box serves now is answered by the box it was sent to, by the lane it serves, as on
  a box alone: a client set up for a lane that is not loaded keeps working once the boxes are paired, and
  while the other box is off. When a box that may serve it does not answer (it served that model when last
  asked, or it was never reached), or comes back from a switch with another lane, the client gets a 503
  `engine_unavailable` with `Retry-After` and why, and the request sent again goes to the right box.
- A box never answers a request for the other box's model with its own. When the other box switched lanes
  since its list was read, it refuses the request and the entry reads every list again before it answers.
- While this box's own engine does not answer (a lane switch under way), `GET /v1/models` says so with a
  503, as on a box alone: tools that read its first row as this box's model, the cockpit's probe and
  `claude-local.py`, are never handed the other box's. Requests that name the other box's model still go
  there.

## A model both boxes serve

When both boxes load the same lane, they share its requests, and the box they were sent to decides where each
one goes:

- **A conversation stays on its box.** An agent (Claude Code, opencode, Hermes, pi) sends its whole
  conversation again at each turn, and the box that served the turn before holds all of it in its prefix cache
  but the new turn. Sent to the other box, all of it would be computed again: 100,000 tokens take tens of
  seconds on the 27B. So the entry keeps fingerprints of the first 4 KiB, 8 KiB, 16 KiB and so on of every
  request it sent to each box in the last 10 minutes, and a request goes to the box it shares its longest one
  with, when that covers at least 30 % of it (the `cache_threshold` of SGLang's own router) or 32 KiB. The
  32 KiB are for Claude Code, which writes its system prompt and its tools after the conversation: its turns
  share no more than their messages, a small part of a body of 120 KiB and more. New conversations that begin
  with one long text (a system prompt that covers 30 % of them) go where that text went first, as in SGLang's
  router, until the boxes are far apart (below).
- **A new conversation goes to the box with the fewest requests in flight**, the entry itself when they tie:
  two idle boxes, the entry serves it. Every generation counts, Claude Code's short background requests too.
  Each box says, in the list it gives the other one, how many generations it runs and how many of them the
  other box sent it, and the entry adds the requests it has in flight there now: a burst of requests is shared
  as it comes, not sent all to one box between two reads. A box that does not say it, one of a version before
  v1.43.0, gets no shared request: update both boxes.
- **Boxes far apart share even a conversation.** When the box a conversation is on runs more requests than
  the other by more than half of those its lane runs at once, and more than 1.5 times as many, its next turn
  goes to the other box: a long wait for one agent costs more than its prefix computed again. The 27B runs 8 at
  once, so 5 ahead moves a conversation, and a lane that runs 16 (Kolibri, Nemotron Bolt) moves it 9 ahead.
  These are the thresholds of SGLang's router (64 and 1.5), brought to each lane. Each box reads how many its
  lane runs at once in its unit (`--max-running-requests`, `--max-num-seqs`) and says it to the other. A box
  that says nothing of it counts as a lane of 8.
- **A box that cannot serve a request hands it on**, before a byte of its answer went out:
  - When the other box does not answer (it gets 3 s to take the connection), or answers 503 (its engine
    away, or its memory floor) or 529 (the floor, in the Anthropic dialect), the entry serves the request
    itself, the conversation stays with the entry from then on, and that box gets no request of a model
    both serve for 30 s.
  - When the other box refuses a prompt too long for its pool (a 64 GB box paired with a 128 GB one), the
    entry serves it, and the other box still gets the shorter ones.
  - When the entry cannot serve a request, the other box does: the entry's memory floor refuses new
    generations (a box held by its floor takes no shared request either), the prompt is too long for the
    entry's pool, its engine warms up and cannot measure a long prompt yet, or its engine does not answer at
    the first try (connection refused, or a 502, 503 or 504 while its lane starts or stops). The request goes
    at once, not after a wait for the lane.
  - A request whose answer began is never sent again, nor one an engine closed the connection on while it
    had it: the same request could take the other engine down too.
- A token count (`/v1/messages/count_tokens`) is answered by the box it was sent to: both boxes count the same
  way, and a count fills no cache, so it moves no conversation.
- A request with the header `X-Alzeo-Here: 1` is served by the box it was sent to. The cockpit's smoke test
  sends it, to test its own box.
- The tile of each cockpit says what it sent the other box, and why: to stay with a conversation, to even the
  load, what it served itself when that box did not answer or found the prompt too long for its pool, and what
  that box served when this one could not. A request counts once the other box's answer begins, and the counts
  stay on the tile while the other box does not answer.

## The other box in the cockpit

The Now view of each box's cockpit shows the other box of its pair: the models it serves, its memory in
use, its GPU's temperature and power, the job under way there, its version, or why it does not answer.
The other box's proxy reads its own cockpit with its own key and sends only that summary, over the pair's
TLS: its request log, its paths and its settings stay on it. To switch the other box's lane, use its own
cockpit for now.

## Security

- The two boxes talk over TLS 1.3 both ways, on port 30002. Each box has a certificate of its own, made
  once by `pair.py` (in `~/.config/qwen38/pair/identity`, its key readable by your user alone), and pins
  the other box's certificate by its SHA-256 in `pair.json`. A connection that does not present the
  pinned certificate is refused right after its handshake, before a byte of a request is read, and a box
  never sends a request to an address that does not present it. This is how the nodes of
  [NVIDIA PAIR](https://github.com/NVIDIA/Personal-AI-Router) trust each other.
- The certificate is the pair's only credential. No box ever holds the other's API key, which is also the
  login of its cockpit: a box of a pair can only send the other generations, read its list of models and
  read the summary its tile shows. Port 30002 refuses every other route, and the box that serves a request
  admits it to its own engine with its own key.
- `pair.py` swaps the certificates over SSH, with the host key checked whatever your SSH configuration
  says, and nothing secret on a command line, where `ps` shows it to every user, or on the screen.
- Port 30002 is open only while the box is paired, and only the paired box gets past it. Once you
  remove a box from the pair, its next connection is refused; nothing else needs to change, since it
  never held a key of this box.
- Your clients' keys and cookies never cross to the other box.

## Limits

- Every request a box sends on comes from one address, so the other box's limit of 64 connections per
  address is shared by all its clients together.
- Both boxes should keep the default `KEEPALIVE_S`: a stream from the other box that stays silent for
  four of its keepalives (60 s at least) is cut, as a box that went sends nothing more. A box that loses
  power or its cable while a request waits on it is seen within about 25 s (TCP keepalive between the
  boxes), and that request gets a 503. A new request of a model both serve waits 3 s for that box's
  connection, then the entry serves it, and that box gets none for 30 s.
- An update restarts the box's proxy, which ends the requests it serves, those the other box sent it too.
  While it restarts, the other box serves its own requests of a model both serve itself. Update both boxes:
  a box of a version before v1.43.0 gets no shared request.
- `bench-agent.py`, `needle.sh` and `tools-check.py` go through port 30001: on a pair that shares a model,
  they measure the pair. To measure one box, point them at its engine (`--port 30000`, or `PORT=30000` for
  `needle.sh`).
- A conversation is known to the box it was sent to: two clients that continue one conversation through two
  entries each have it placed by its own entry.
- Abort all, in a box's cockpit, stops the generations of that box's engine. The requests it sent the other
  box run there: they end when their client leaves, or from that box's cockpit.
- `GET /v1/models/<id>` answers for the box's own models only.
- To switch the other box's lane, use its own cockpit for now: the tile only shows it.

## When `pair.py add` says no

- *cannot run a command over SSH without a password*: set SSH up as step 4 of NVIDIA's playbook does,
  then `ssh <the other box> true` must work without a question.
- *does not know the SSH host key*: run `ssh <the other box>` once and answer yes once you have checked
  the key. `pair.py` never takes an unknown host key on its own, even when your SSH configuration would.
- *cannot reach the pair port*: both boxes must run this version of Alzeo, no firewall may stop port
  30002/tcp between them, and the address must be one both boxes share.
- *both boxes are named*: two boxes with the same host name; name one of them with `pair.py name`.
