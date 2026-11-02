# Two boxes behind one endpoint

Two boxes that each run this repo can answer as one: clients talk to one of them (the entry), and each
request goes to the box that serves the model it names. One box can keep the 27B for your agents while
the other loads Flash or another lane, and the clients see both models behind one address. This is the
first stage of [discussion #45](https://github.com/hasso5703/alzeo/discussions/45). Splitting one model
across both boxes over the ConnectX-7 link is a later stage.

## Set it up

On the entry, name the other box and hand it the other box's key on standard input, never on the
command line, where `ps` shows it to every user:

```bash
ssh spark2 cat .config/qwen38/api-key | python3 ~/dgx-spark-qwen38/pair.py add spark2 http://spark2:30001
python3 ~/dgx-spark-qwen38/pair.py status          # this box's pair, and what each peer serves now
python3 ~/dgx-spark-qwen38/pair.py name spark1     # the name this box gives itself (its host name by default)
python3 ~/dgx-spark-qwen38/pair.py remove spark2   # this box alone again, as it was
```

`add` checks that the other box answers with that key before it writes anything (`--no-check` writes
it anyway). The key goes to `~/.config/qwen38/pair/<name>.key`, mode 600, and only to that box's proxy.
The proxy reads `~/.config/qwen38/pair.json` again within 2 s: nothing restarts. The other box needs
nothing. Pairing each box with the other works too, and either one can then be the entry.

## What a client sees

- A request that names a model of one of this repo's lanes (`qwen3.8-27b`, `qwen3.8-flash-next`,
  `qwen3.8-flash-next-fp4`, `ornith-1.5-122b`, `nemotron-3.5-bolt`, `nemotron-3.5-super-vl`,
  `kolibri-1`) goes to the box that serves it, streamed or not, OpenAI or Anthropic dialect.
- Any other name (`claude-*` from Claude Code's background tasks, `gpt-*`, none) stays on the entry, as
  on a box alone.
- `GET /v1/models` lists the models of both boxes, each with a `box` field. It never waits for the other
  box: its list is read in the background and kept for a few seconds.
- A lane name that no box serves now gets a 404 `model_not_found`, in the client's dialect, with what
  each box serves. When a box does not answer, or comes back from a switch with another lane, the client
  gets a 503 `engine_unavailable` with `Retry-After`, and the request sent again goes to the right box.
- A box never answers a request for one model with another. When the other box switched lanes since its
  list was read, it refuses the request and the entry reads every list again before it answers.

## Security

The traffic between the two boxes is plain HTTP, as on port 30001 today. Put the other box's address on
a link of your own: the ConnectX-7 cable between the two boxes, or a tailnet. The entry never sends on a
request it could not check against its own key, and it never sends the other box's key through a proxy
of its environment.

## Limits

- Every request the entry sends on comes from one address, so the other box's limit of 64 connections
  per address is shared by all the entry's clients together.
- Both boxes should keep the default `KEEPALIVE_S`: a stream from the other box that stays silent for
  four of its keepalives (60 s at least) is cut, as a box that went sends nothing more.
- `GET /v1/models/<id>` answers for the entry's own models only.
- To switch the other box's lane, use its own cockpit for now.
