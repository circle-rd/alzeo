# Security policy

What this box trusts, what it does not, and how to report a hole in that line.

## What this project is, security-wise

A serving stack one operator installs on a machine they own and administer:
a Docker engine behind a Python proxy, a cockpit web UI that reaches systemd
through an exact-argv sudo allowlist, and a relay, installed with the cockpit
when opencode is there, that puts opencode's web interface behind the cockpit
login. There is no telemetry and no
phone-home: every byte that leaves the box is a download the installer names
(Hugging Face, the image registry, and GitHub: the one-liner's clone and the pinned
opencode release) or a request you sent. One exception, added in v1.15.2 and named here
because it is one: the cockpit asks GitHub's public releases endpoint whether a newer
release exists, so that a box running an old version is told rather than left to find
out. Once it has an answer it asks again six hours later; a check that fails is retried
after one minute, then two, four, eight and so on up to six hours. It sends nothing but the
cockpit's version in a `User-Agent`, it reads a public URL that needs no credential, and
`COCKPIT_UPDATE_CHECK=0` turns it off, after which the cockpit says nothing about
newer releases at all (its Settings view keeps a link to the release notes on GitHub, which the page never
fetches: the user opens it or not). That includes the typed
decisions route (`POST /v1/systemone`, v6.19): it speaks the wire contract of a hosted
service, and it is answered by the engine on this box and nothing else; the proxy
calls no address but its upstream.

Two things the typed-decisions route is careful about, both found by a review of it on
2026-09-19 and both fixed before it shipped. The proxy decodes the request path once,
before any check reads it, because the engine routes on the decoded path: matching the raw
string let `/%76%31/chat/completions` walk past the per-client identity wall and past the
oversize guard with the engine's own key attached. And the `state` of a typed decision,
which is third-party text by design, is fenced with a token drawn at start-up: without it
a state could write the framing of the question itself and be answered instead of judged.

## Reporting

Report privately, not as a public issue (a public issue is a disclosure with
no fix window). Email `basbunarhasan@gmail.com` with as much of this as you
can: the version (`git rev-parse HEAD` or the release tag), the component
(proxy, cockpit, relay, installer, sudoers surface), what you sent, what came
back, and what you believe the boundary break is. Expected first response:
7 days, with patches landing as ordinary commits once the fix is public.
GitHub's private vulnerability reporting tab is an acceptable channel once
enabled for the repository.

## The trust boundaries, as they actually exist

**The keepalive proxy (`keepalive-proxy.py`, :30001)** is the only component
that reads bytes it does not control: every client request. Its guards are
its contract: bodies over `MAX_BODY_BYTES` (256 MiB) get 413 before a byte is
read, a peer on the network has 64 KiB for a request's line and headers
(`HEADER_BYTES_MAX`, 431 past it) and 30 s without a byte while its body comes
in (`BODY_STALL_S`), the routes open without a key take no body (both since
v1.40.1), a lying `Content-Length` cannot decide an allocation, tool schemas are
sanitized at bounded depth and only for patterns Python cannot compile,
image parts are priced from their own headers, and the corruption tripwire
aborts a decode that emits runs of token id 0. Fuzzed and state-machine
tested (see TESTING.md); a crash you can reproduce in the proxy is a bug
worth a private report.

Several of those guards exist because the engine behind it does not survive
the request, and a denial of service that costs one HTTP call is worth naming
as such. A prompt past the KV pool is queued and never admitted, and from then on
the engine generates nothing for anyone until it is restarted (measured on this
box, 2026-08-29). A logprob request past the vocabulary raises
`selected index k out of range` inside the scheduler and the server is gone
for every client until it is restarted, about nine minutes here
(sglang#40076, open). A `stop_token_ids` or `input_ids` entry past the
vocabulary indexes a `scatter_add_` or the embedding out of bounds, which on
CUDA is a device-side assert with the same blast radius, and `n` expands a
list before scheduling with no bound at all (sglang#31597, whose two fixes
were closed without being merged). Every one of those fields is declared
unconstrained in the served release, checked in the image, and all of them
are reachable from an ordinary chat request. One more is reachable from
`/generate`: on a batch that mixes a request asking for `token_ids_logprob` with
one that does not, the 27B build served until v1.22 (`v0.5.19`) fills a bare list
for the one that did not ask and its scheduler dies on it (sglang#34719, open;
refused since v1.22.13). `v0.5.21` fills an empty tensor there, and the issue's
reproduction no longer kills its scheduler (20 rounds on the reference box,
2026-10-10); while the issue is open the field stays refused.

The proxy refuses all of them with a 400. **That protects the clients that go
through it, and nothing else.** Whatever reaches the engine's own port with the
serving API key reaches the scheduler with none of these guards. **Since v1.17
the engine binds `127.0.0.1` by default**, so on a box installed that way its
port answers on the box only and the proxy is the one door from the network.
One setting still puts the engine itself on the network: `ENGINE_BIND=0.0.0.0`,
given to `./install.sh`, or to `./run.sh`, the foreground path of `--no-service`,
which has no proxy at all (and serves `127.0.0.1` without it, like the units,
since v1.18.7). With it, anything on the LAN or the tailnet that holds the key can
reach the engine directly and end it, exactly as it could before these guards
existed: the key is the boundary there, not the proxy. Nothing is lost by the default: the proxy is a full pass-through of
every route the engine serves, so a client that used `:30000` from another
machine uses `:30001` and gets the guards with it. `ENGINE_BIND=0.0.0.0`
restores the old behaviour, `PROXY_BIND` does the same for the proxy, and an
installed choice is kept across updates in both directions. Since v1.36.0 the
engine bind that only an install before v1.17.0 put on every interface, and that
nothing records as chosen, moves to `127.0.0.1` at the next update instead: the
update says so before its first step, with `ENGINE_BIND=0.0.0.0 ./install.sh` to
keep it, and a box that set it since keeps it.

**The key is checked before a body is read** (since v1.22.13, proxy v6.34).
Without the identity wall, the engine's key is the proxy port's one gate, and the
proxy applies it itself, with the engine's own rule and the engine's own 401,
before it reads a byte of the body. Until then a caller with no key, or a wrong
one, had its body read whole (up to `MAX_BODY_BYTES`) and, when it was large,
counted by the engine on the proxy's own key before the engine refused it. `/health`,
`/ready` and `/metrics` stay open, as the engine keeps them (`/ready` since v1.26.1);
since v1.40.1 those three routes only, where the door opened every path under them as the
engine does: `/health_generate`, which runs a one-token generation on an idle engine,
needs the key, and the SGLang units turn off the generation `/health` itself ran.

**The image and video lanes take no key, and their scheduler's channel stays on
loopback** (since v1.40.1). Their HTTP API binds where `IMAGE_BIND` and `VIDEO_BIND` say,
loopback by default, and the installer says what another address costs: anyone who reaches
it generates on the GPU. Behind it, the diffusion runtime's scheduler takes its requests on a
ZMQ socket that unpickles what it is sent, and upstream derives that socket's address from
the same `--host`: until v1.40.1 a lane bound off loopback took requests on that channel from
the network too, which let anyone who reached it run code as the lane's user (found in a
security review, 2026-10-24). `image-sglang/scheduler-loopback.patch` keeps it on 127.0.0.1
whatever `--host` is. A video lane that serves during an update keeps the code it started with
until it restarts, and an image lane is restarted by the update (since v1.40.6, about 70 s).

**The cockpit (`dashboard/`, :30090)** runs as the user who installed the box,
and that user is already root-equivalent: `install.sh` requires it to be in the
`docker` group, and a member of that group can start a container with the host's
`/` mounted. Since v1.37.2 the installer adds it to that group when it is not in it,
which is how DGX OS's first-boot setup leaves the account it creates. It says so,
and why, before it runs sudo: where sudo asks for the password, a person can stop
there (sudo does not ask while an earlier ticket is still valid, nor for an account
that needs no password). With `--no-service`, which uses no sudo, it says how instead. The NOPASSWD sudoers lines rendered from
`dashboard/sudoers-cockpit.template` add no privilege to that, and they are no
boundary either: two of them `install` a file the user writes
(`~/.config/qwen38/*.switch-stage`) into `/etc/systemd/system`, and with
`daemon-reload` and `restart` allowed beside them, whoever writes that file runs
what it says as root. So the cockpit's user, its API key and a session on it are
root on this machine, and the defences that count are the ones in front of it:
the key, the bind, the session cookie. The allowlist is still exact argv with
no wildcards (a glob on `sed` arguments is root without any file to write), and
two CI steps hold it: no `*` on any allowlist line, and every privileged call in
`switch-model.sh` cross-checked against the template. The cockpit process
runs unprivileged; login is the serving API key; a per-session HMAC cookie
gates everything else; the diagnostic bundle masks the API key in every
member, and a test builds a real tarball and greps it rather than trusting
the masking code.

**The agent relay (`dashboard/agent_relay.py`, :30091 by default)** is the
only thing that reaches `opencode serve`: it requires a valid cockpit session
cookie, refuses foreign origins, strips caller headers the relay owns, and
forwards over HTTP with Basic credentials that never leave the machine
(opencode binds 127.0.0.1). Its boundary properties (origin gate, cookie
names, header hygiene) are property-tested.

**The pair port (`keepalive-proxy.py`, :30002, only while the box is paired)**
is the proxy itself, for the other box of a pair (`pair.py`, docs/pair.md). It
speaks TLS 1.3 only and demands a client certificate: the chain must end at a
certificate pinned in `pair.json`, and the certificate itself must have a pinned
SHA-256, or the connection ends right after its handshake, before a byte of a
request is read. The certificate is the pair's only credential: no box holds the
other's API key, which is also its cockpit's login (root on a box, as above), and
the pair port takes the generation routes and `GET /v1/models` only (403 for any
other), admitted to the engine with this box's own key, and `GET /pair/cockpit/state`:
a summary of this box (its name in the pair, what serves, its lanes' units, memory, CPU,
GPU, the job under way, the version, why its cockpit does not answer when it does not,
the time), which the proxy reads from its own cockpit with its own key, never relayed
whole, and keeps 2 s, so the other box's reads cost this one a cockpit read every 2 s
at most. What a box takes from the other (its list of models, its summary) is rebuilt
field by field, each of its type: nothing nested, no NaN, comes through. A request the
other box sends on never names the engine's request id, nor does a client of a listed
key (below), in a header (`x-override-rid`, vLLM's `X-Request-Id`) or in any form an
engine reads one from a body: "rid" (written with escapes too), "request_id", the
Vertex route's "parameters". A body that may hold one and cannot be read whole gets a
400. SGLang aborts every request whose id starts with one it aborts, and ids "0" to
"f" could end every generation of the box. Every request body is JSON in UTF-8, or a
415: in UTF-16 or UTF-32, which the vLLM lanes read too (the SGLang lanes refuse it), the
guards saw none of its fields.
The list a box gives the other on this port carries `x_alzeo_running`, the number of
generations it runs, and `x_alzeo_running_yours`, how many of them the other box sent
it, which the other weighs to share a model both serve (docs/pair.md): counts from 0 to
100,000, the second no more than the first, are taken, anything else is not, and the
box then gets no shared request. It also carries `x_alzeo_slots`, how many requests its
lane runs at once, a count from 1 to 4,096 or nothing (a lane of 8 is then assumed), and
`x_alzeo_revision`, the checkpoint it serves, 40 hexadecimal characters or nothing: a box
that serves another checkpoint, or says none, gets no shared request. A
box that lies about them can only draw more of the other's requests of that model, or
send it fewer, which it could do by answering them slowly or not at all.
This box's cockpit reads the other's through `/proxy/pair/<box>/state`, a route of
this machine only, as `/proxy/stop`. A request that gets past
it is a hop from the box of that certificate: served there or refused, never sent
on, and it meets every guard of :30001 but the client keys. No exemption this
machine's own tools get on loopback reaches it, whatever its address. The port
opens within seconds of `pair.json` naming a peer and closes once it names none;
a box taken out of the pair is refused at its next connection. Each box's
certificate is self-signed (EC P-256), made once by `pair.py`, its key mode 600
in a directory mode 700, the model of NVIDIA PAIR's nodes; its dates are not
checked, the pin being the certificate itself. The boxes swap certificates over
SSH, host keys checked whatever the user's SSH configuration says, nothing secret
on a command line. Outbound, a box sends to a peer's pair port only, with its own
certificate, once the peer's is the pinned one, never through a proxy of its
environment, and never with its client's key or cookies; an answer it relays is
bounded (`MAX_BODY_BYTES`) and stripped of cookies.

**The installer (`install.sh`, `dashboard/install-*.sh`)** runs things you
sudo. Every pinned byte is hash-verified: images by digest, checkpoints by
pinned revision, the opencode release by its sha256. The image and video lanes
(`--with-image`, `--with-video`) are the parts with unpinned bytes: their SGLang wheel, source
commit and checkpoint revision are pinned, but pip resolves the wheel's dependency tree
from PyPI at install time, with no hash checked. The speech lane (`./install-speech.sh`,
opt-in) pins llama.cpp's commit, its models by sha256, and every package of its venv by
exact version, wheels only, also without hashes. The system packages the image and video
lanes need on a new box (`python3-dev`, `ffmpeg`, and `python3-venv` where it is missing), and
the speech lane's build tools when its installer finds one missing, come from the box's own apt
sources, Ubuntu's signed archive, installed by sudo at the versions its package lists name. The engine enforces its key, read from a file created
0600 and handed to it through SGLang's `--config`, so it is in no process's command line and the chat template and launcher are regenerated from the
repo on every install.

**The one-liner (`get.sh`)** is fetched, not pinned: what runs is main's get.sh at that moment.
Since v1.41.0 the README's command asks `get.alzeo.ai`, a redirect the project keeps at Cloudflare,
which sends curl on to `https://raw.githubusercontent.com/hasso5703/alzeo/main/get.sh`. Whoever
controls that domain controls what that command runs, as whoever controls the repository does. The
update command the cockpit shows, and the long form the README gives next to the short one, fetch
the same file from GitHub alone: a box already installed trusts no new party for its updates.

## Assumptions that are design choices, not vulnerabilities

Read these before reporting; a report against one of them is a design
discussion, not a disclosure.

- **Plain HTTP by default.** The cockpit and the relay serve HTTP only, and so
  does the proxy until it is given a certificate (`QWEN38_TLS_CERT`, proxy
  v6.16, see docs/clients.md): all three are meant for loopback or a tailnet.
  The cockpit says in its journal when it binds anything but loopback, and with
  no key file no login succeeds. TLS in front of the cockpit and the relay is
  the operator's reverse proxy. If you need to expose any of it to a LAN you do
  not control, that is the design's stated edge, not a hole.
- **One API key, one trust realm, by default.** Every client that holds the key
  is the same principal: same quota, same ceiling, same lane. The proxy can
  name clients instead (`QWEN38_CLIENT_KEYS_FILE`, proxy v6.16: one bearer per
  client, a 401 for an unlisted one on every route but `/health`, a 403 for a
  listed one on the engine's operator routes since 2026-10-16, the wall signing
  what it relays with the engine's own key: a client key generates, counts and
  reads, the label on
  every journal line of its requests, and since v1.42.1 the engine's request id
  is never the client's to name), and naming is all it does: there are no
  per-client quotas or ceilings, and the engine behind it still has one key.
- **`AGENT_AUTO=1` is deliberate escalation.** It sets opencode to allow
  every tool call in the Agent view, behind the cockpit login, on this
  machine, by the operator's own choice. opencode merges it after every
  rule of its config files, so its `"*": "allow"` comes after the rules of
  your own `opencode.json`, `deny` included, and overrides them, unless your
  config has a `"*"` of its own: the unit's then takes that key's place, and
  the rules after it still hold. The unit repeats only this repo's shell
  rules for Alibaba Cloud's hosts and the cloud metadata addresses after it. Without it,
  opencode's own defaults already run shell commands unasked
  ([docs/agents-and-network.md](docs/agents-and-network.md)).
- **The abliterated targets are a policy choice.** `uncensored`,
  `uncensored-fp8` and `flash-uncensored` exist as pinned checkpoints
  because the operator asked for them; nothing in the serving stack filters
  or vets what they answer.
- **`/metrics` needs no key.** Every text lane passes the Prometheus flag, and
  the engine serves `/metrics` without the API key (checked on the reference
  box: 200 with no key, where `/v1/models` answers 401). Since v1.17 the engine
  port is on loopback, so from another machine the counters are read through
  the proxy on `:30001`, which relays that route like any other, with no key
  either; with the per-client identity file set, it needs a listed key like
  every route but `/health`. Request rates and queue depths are therefore
  readable by anything that can reach the proxy port. That is the port's
  existing trust model (a trusted network by design, see the plain HTTP edge
  above); the metrics endpoint adds counters to it, not a new surface to
  authenticate against. The image and video lanes export no metrics.
- **The speech lane's routes take no key.** Like the image and video lanes, the speech
  lane (opt-in, `:30024`) serves its routes with no key on loopback, and neither the
  cockpit nor the proxy relays them: programs on the box reach them, another machine
  through an SSH tunnel. `SPEECH_BIND` puts them on another address, and the installer
  says what that costs: anyone who reaches it can transcribe and speak with the box's
  CPU. Its recognizer (`:30025`) binds loopback whatever is set.
- **The engines' coordination ports stay off the network** (since v1.40.6, for lanes bound to
  loopback, the default). An engine of one process still runs PyTorch's distributed store, which torch
  binds to all interfaces whatever address it is given, and gloo's and NCCL's bootstrap took the box's
  LAN address, though its name resolves to 127.0.0.1. Measured on the reference box, which has no
  firewall: the flash lane's scheduler listened on `*:45385` (a random port, new at each engine start,
  2026-09-30), and a TCP connection to it went through from the LAN and the tailnet addresses. The video
  lane listened on `*:30005` with four ports on the LAN address (2026-09-29 and 2026-09-30), the image
  lane the same, where a connection to `30005` went through from both addresses (2026-10-30), the 27B
  lane on `*:23906`, and under the real clients the 27B and flash lanes on four ports of the LAN address
  as well (2026-10-30). The ports take no key and serve no model and no data, but PyTorch states that
  its distributed features are not built for untrusted networks: a hostile connection can disturb the
  engine, a denial of service. This page said until v1.40.6 that no setting narrowed them. Two do. The
  SGLang text lanes take their rendezvous through a file in their container
  (`SGLANG_DISTRIBUTED_INIT_METHOD_OVERRIDE`), so no port opens for it, and pin gloo and NCCL to `lo`:
  under the same eight real clients, the 27B and flash lanes started with them listened on no TCP port
  past loopback, where the 27B without them had its store and four ports on the LAN address
  (2026-10-30). The image and video units pin gloo and NCCL to `lo` and, with a loopback bind, let the
  lane talk to loopback alone (`IPAddressDeny=any`, `IPAddressAllow=localhost`): the store still
  listens on `*:30005`, and what another machine sends it is dropped, unanswered. The same filter stops
  the lane fetching an image by its URL. A diffusion lane bound past loopback has no filter, and its
  port 30005 is then open to the network: the installer says so. A video lane that served through the
  update takes its filter at its next start. The vLLM lanes opened no such port (2026-10-30). It all
  sits inside the trusted-network edge above anyway, like the plain HTTP ports. To see the listeners
  while a lane serves: `sudo ss -ltnp | grep -v 127.0.0.1` (a filtered diffusion lane still shows
  `*:30005` there, and `systemctl show qwen38-image.service -p IPAddressDeny` its filter). On a LAN you
  do not control, firewall the box to
  the ports you use before anything else; with ufw (inactive on the reference box):

  ```bash
  sudo ufw allow in on lo
  sudo ufw allow in on tailscale0      # the cockpit, the proxy and the relay over the tailnet
  sudo ufw allow 22/tcp                # SSH from the LAN, if you use it
  sudo ufw default deny incoming
  sudo ufw enable
  ```

  Docker does not bypass this here: every lane runs on the host network or natively, so
  no port of theirs goes through Docker's own rules. Allow `30001/tcp` too if LAN
  clients use the proxy.
- **The cockpit assumes one admin user.** The sudo allowlist covers this
  repo's argv, but a hostile local user with your shell can do what you can
  do: this box is yours, and it is not a multi-tenant host.

## Out of scope

SGLang and the serving images themselves (report upstream, tell us too if it
affects a pinned digest), model behavior and prompt injection into agents
(opencode's permission model is the boundary), and anything reachable only
with root or physical access.
