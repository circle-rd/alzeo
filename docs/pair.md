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

It pairs both boxes at once, checks that each box reaches the other, and keeps nothing on either box
when a check fails, with what to fix. Both proxies take the pair within a few seconds: nothing restarts.
The two boxes' clocks need not agree.

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
  `kolibri-1`) goes to the box that serves it, streamed or not, OpenAI or Anthropic dialect.
- Any other name (`claude-*` from Claude Code's background tasks, `gpt-*`, none) stays on the box it was
  sent to, as on a box alone.
- `GET /v1/models` lists the models of both boxes, each with a `box` field. It never waits for the other
  box: its list is read in the background and kept for a few seconds.
- A lane name that no box serves now gets a 404 `model_not_found`, in the client's dialect, with what
  each box serves. When a box does not answer, or comes back from a switch with another lane, the client
  gets a 503 `engine_unavailable` with `Retry-After`, and the request sent again goes to the right box.
- A box never answers a request for one model with another. When the other box switched lanes since its
  list was read, it refuses the request and the entry reads every list again before it answers.

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
  four of its keepalives (60 s at least) is cut, as a box that went sends nothing more.
- When both boxes serve the same model, its requests stay on the box they were sent to for now.
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
