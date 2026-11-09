# Roadmap

Where this is going, written so that a contributor does not have to ask, and
so that "on the roadmap" means a checkbox with a definition of done, not a
mood. Statuses: shipped, in progress, planned, considered (considered items
have a reason they are not yet planned, stated here or in an issue).

## Shipped, current: v1.40 to v1.45

- **The 64 GB DGX Spark** (v1.40.0): the lanes that fit it, Bolt served by default; a `--no-<lane>`
  counts for its run only.
- **Security** (v1.40.1, v1.40.6, v1.42.1): the image and video lanes' scheduler channel on loopback,
  `GET /health` that generates nothing, a network peer's headers and body bounded; the engines'
  coordination ports out of the network's reach (a file rendezvous for the SGLang text lanes, the
  image and video lanes held to loopback by the kernel, none opened by the vLLM lanes); no client
  can hide a field from the proxy's guards in UTF-16.
- **Every lane says what its prefix cache served** (v1.40.3, #49), and Claude Code's ToolSearch works
  on the Qwen lanes.
- **opencode refuses a shell command that names an Alibaba Cloud host or a cloud metadata address**
  (v1.40.4).
- **On the Ornith lane a long prompt no longer holds up the other requests** (v1.40.7, #61).
- **The project is Alzeo** (v1.41.0): github.com/hasso5703/alzeo, `get.alzeo.ai`, the Alzeo cockpit;
  its site at alzeo.ai (v1.45.3).
- **Two boxes, one model** (v1.42.0, v1.43.0): paired in one command behind one endpoint, the other
  box in the cockpit, a conversation staying on the box that holds it.
- **The 27B starts without torch.compile** (v1.44.0), and an answer's end is read as its engine
  sends it; one page per model (v1.44.1).
- **The cockpit names each lane's engine and version by its official name** (v1.45.0) and says where
  a client sends its requests (v1.45.1).
- **A window of 1,048,576 tokens on the Flash-Next FP4-Local lane**, as an option (v1.45.2).

## Shipped, v1.39

- **SGLang v0.5.22 for the 27B lane** (v1.39.0): faster at one and four streams
  in an A/B on the reference box, its draft accepted alike on every boot, with
  the scheduler and DFlash2 selector fixes `v0.5.21` lacks.
- **The Agent tab against a real opencode, in CI** (v1.39.0): the pinned release,
  a cockpit of its own and a browser, the terminal and the phone layer included.

## Shipped, v1.38

- **Ornith 1.5 122B, a ninth lane** (v1.38.0): Ornith AI's mixture of experts
  with vision, on vLLM in the Flash FP4-Local lane's image with its MTP head,
  and a window of a million tokens with YaRN, written where the model reads it.
- **opencode 1.19.4** (v1.38.6): on Bun 1.4.4 and its fix for CVE-2026-111497 in
  Brotli's decoder, with the Agent tab's phone layer on the page it sends chunked.

## Shipped, v1.37

- **Flash FP4-Local, an eighth lane** (v1.37.0): NVIDIA's FP4 build of the
  flash model on vLLM, with the flash lane's quality in about half its disk
  and twice its KV cache, its n-gram table kept on the internal disk.

## Shipped, v1.36

- **Nobody runs behind in silence** (v1.36.0): what an update keeps and
  moves, said before it starts and in the cockpit.
- **The cockpit's engine actions as tiles** (v1.36.1), and the README opens
  on the box at work.
- **A CI twenty minutes shorter** (v1.36.2), and the repository says how to
  take part.
- **A cloud review's findings, proved on the box** (v1.36.3): the proxy in a
  systemd sandbox, the engines reading their config directory read-only, the
  27B lane without checkpoint code, the speech lane reading an upload as a
  sound file; the CI's offline step runs four test files at a time.
- **The flash lane says when its GPU cannot start it** (v1.36.4): outside its
  ATS addressing mode it says so, and fetches nothing for it (issue #54).

## Shipped, v1.26 to v1.35

- **Nemotron 3.5 Super VL, a seventh lane** (v1.35.0): NVIDIA's 120B with
  vision and a 1M-token window, on vLLM in the Nemotron lane's image,
  installed on demand like the others; the proxy bounds what the network
  can hold, and the image and video lanes see no secret of the box (v1.35.1).
- **Nemotron 3.5 Bolt, a sixth lane** (v1.34.0): NVIDIA's 74B that reads
  images and video, in 24 GB.
- **Every lane but the one that serves installed on demand** (v1.33.0), and
  built at its first Load; the cockpit's ergonomics review (v1.33.1).
- **A speech lane on the CPU** (v1.32.0), opt-in, and a cockpit update
  command that updates.
- **One fetch for every lane's checkpoint** (v1.31.0), and a text Load that
  needs no network.
- **Each lane proved on what is installed** (v1.30.0), and Kolibri compacting
  at 185,000 tokens.
- **Kolibri-1, a fifth lane** (v1.29.0): Aleph Alpha's German and English
  model, on vLLM.
- **The flash lane on `nightly-cu134-20261011-53295c6`** (v1.28.0), measured
  equal or better.
- **The cockpit in deep indigo** (v1.27.0): each lane with its maker's mark
  and its colour, every version in sight.
- **The Traffic view says the prefill speed too** (v1.26.0), the queue, the
  idleness and the peaks.

## Shipped, v1.25

- **A Turbo switch for videos, and the video lane on a release** (v1.25.0): the cookbook's
  speed adapter for MiniMax-H3 put on for one video and taken off after (4 s at 480P in 127 s
  against 623 s, the base's next video unchanged byte for byte), admitted at 480P and 4 s,
  the sizes measured with it; the lane runs the v0.5.21 release, its videos unchanged byte
  for byte. opencode 1.18.35, which masks the keys `opencode debug config` prints.
- **Results in their own shape** (v1.25.1): the Image and Video views show a result in its own
  proportions, and an image opens at full size, fitted or at its own pixels.

## Shipped, v1.24

- **Qwen-Image 2.1 Turbo, and the image lane on a release** (v1.24.0): Qwen's eight-step
  distillation as a second checkpoint of the image lane (a 1024x1024 image in 7.5 s against
  34.3 s), loaded like any target; the lane runs the v0.5.21 release with the Turbo's sigma
  grid backported, the base's images unchanged byte for byte.

## Shipped, v1.23

- **The 27B lane on SGLang v0.5.21** (v1.23.0), whose prefix cache no longer resumes a
  conversation from a stale linear state (sgl-project/sglang#37817, fixed by #37818).

## Shipped, v1.22

- **pi and omp configs** (v1.22.0), generated from the opencode artifact.
- **The proxy hardened** (v1.22.1 to v1.22.14): a stream the engine has not started kept
  open and its caller watched, the engine key masked in every `/server_info`, the key
  checked before a body is read, the prompts that could wedge the 27B counted, and the
  engine's facts dropped at once when a lane switches.
- **The cockpit says what the engine says** (v1.22.3 to v1.22.12): download progress with
  bytes, rate and time left, the last 5 minutes of traffic from the engine's own counters.
- **An idle video lane holds no CPU core** (v1.22.10).

## Shipped, v1.21

- **An engine restart no longer ends a session** (v1.21.0), and no engine starts on memory
  a dead one still holds.

## Shipped, v1.20

- **Every lane by default** (v1.20.0): the one-liner installs both text lanes (the 27B
  serves, Flash-Next 176B beside it), the image lane and the video lane, each proved
  the first time; `--no-<lane>` leaves one out, remembered like `--no-cockpit`.

## Shipped, v1.19

- **A first install works again** (v1.19.0): v1.18.7 ended every install on a box that
  never had this repo at step 7. Found by installing the reference box from nothing, its
  own install moved aside, and checked the same way with the update from v1.18.7.
- **MiniMax-H3, a fourth lane** (v1.19.0, opt-in with `--with-video`): text to video
  with sound made in the same pass, and first/last-frame conditioning, served from the
  SGLang cookbook's recipe with no local patch, driven like the other three lanes
  from the cockpit's Video view.
- **The cockpit rebuilt** (v1.19.0): one pool, four lanes, and a lane change as one
  journey (switch, stop, start), now checked under a finger on five touch screens by
  a browser test that taps where a finger lands.
- **The flash lane at eight requests** (v1.19.0): the concurrency tier with two mamba
  states per path and the nightly engine the reference box validated on 2026-09-29
  are the default.

## Shipped, v1.18

Since v1.14 the stack gained a third lane and closed its engine port:

- **Typed decisions** (v1.15.0): `POST /v1/systemone`, the wire contract of
  TypeSafe's Jev answered by the lane on this box, with a cockpit view of its
  own and its measured comparison in BENCHMARKS.md.
- **The release check** (v1.15.3), on by default: the cockpit says when a
  newer release is out, and `COCKPIT_UPDATE_CHECK=0` turns it off.
- **The engine on loopback** (v1.16.0 as a choice, v1.17.0 by default): the
  proxy on `:30001` is the one door from the network, and `ENGINE_BIND=0.0.0.0`
  reopens the engine port for a box that wants it.
- **Qwen-Image 2.1, a third lane** (v1.18.0, opt-in with `--with-image`),
  switched and started like the text lanes, on loopback with the cockpit as its
  gate, since the runtime has no API key.
- **One pinned opencode on every box** (v1.18.3): 1.18.32, checked by sha256,
  installed or upgraded by `install.sh` unless `OPENCODE_PIN=0`.
- v1.18.4 to v1.18.7: a fresh box's opencode stays on the box's own model, the
  image lane installs without Rust, an update restarts only what it changed,
  and a line-by-line review of v1.18.6 fixed with a test per defect.

## The stack at v1.14.0

Two lanes (27B NVFP4/FP8 with DFlash2 drafting; 176B flash-next with NEXTN
and the PLE table on NVMe), seven switchable targets, the cockpit, the
keepalive proxy with its byte guards and the corruption tripwire, 1M and
native context presets, four reasoning-effort levels with `lean` as the
default, opencode out of the box, and the frozen benchmark battery (v1) that
keeps every number in this repo comparable across years. **Both lanes now
serve an official upstream image and this repo builds none by default**, which
is the end of a long habit of carrying vendored engine files. The flash lane's
old overlay (`OVERLAY_FLASH=1`) was retired in v1.18.7: the launcher of v1.8 on
did not fit its image and nothing had booted the pair since, so its rollback is
the release that shipped it whole, v1.7.2.

## Shipped in v1.13.0 and v1.14.0

`lean`, a fourth reasoning-effort level measured on 7,008 runs and made the
default: 0.71x the thinking tokens of `medium` with no detectable quality
change, and 0.047x of the shipped `xhigh` on the underspecified requests that
make this model spiral.

Then the 27B lane moved to the official `v0.5.19` release and its eight-file
overlay was deleted. Both reasons it had stayed behind were retired by
measurement rather than by restating them: the overlay carried nothing
upstream lacks (diffed function by function), and the sm_121 objection turned
out not to depart the two images at all, since the one this lane served
shipped the same sm90 and sm100 kernels, byte for byte. The memory fraction
moved to 0.76 because the official image claims a smaller static budget for
the same number.

## Shipped, v1.11

Proxy v6.16 (optional TLS, optional per-client identity), `--enable-metrics`
on every lane (a re-run and a restart carry it onto existing boxes), the
governance layer (SECURITY, CONTRIBUTING, ARCHITECTURE, ROADMAP, the
pin-watch workflow, the issue templates) and the v6.16 client docs. The
metrics flag is live on a box once it has been re-installed and restarted;
the cockpit drift panel says so, per lane, until it is.

## Planned

- **Beyond one box, in that order: more GB10 machines, then more hardware,
  then more models.** What this repo is, today, is a production serving stack
  for one specific machine, and every number in it says "reference box" for
  that reason. The order matters because each step buys the next one: a
  GB10 matrix with community rows proves the recipes survive other people's
  OEMs and kernels; hardware beyond GB10 is only honest once the recipes are
  parameterised by what the card can do, not by what this one does (the
  sm_121 story in v1.14 is the shape of that work: an objection that turned
  out to be about neither image). More model families, last in that order, came
  first: Aleph Alpha's Kolibri-1 (v1.29), NVIDIA's Nemotron 3.5 Bolt (v1.34),
  Nemotron 3.5 Super VL (v1.35) and Ornith AI's Ornith 1.5 122B (v1.38) each
  have a lane, measured by the method
  its lane's page states. None of this is a rewrite, it is the same discipline
  applied to a wider set of pins, and nothing ships as "supported" before a
  measured row exists for it.

- **A supported-GB10 matrix beyond the reference box**: every performance
  claim carries "reference box", and the box-report protocol plus
  `bench-matrix.sh` battery v1 exist to widen that with community numbers
  that name their conditions. Planned shape: a machine table in
  BENCHMARKS.md with OEM, OS and battery JSON per row, one row per
  verifiable report.
- **Podman and quadlet path**: the units are plain systemd; a quadlet
  render of the same pinned commands (same digests, same flags, CI-rendered
  and byte-checked like the units today) so containers-first fleets get the
  same pins without a Docker install. Considered first on a box that is not
  the reference one, because it must not be able to drift from the Docker
  path.
- **Per-client limits on the identity layer**: the v6.16 key map names who;
  the honest next step is per-label ceilings and a journal-derived quota,
  designed as an issue with measured motivation, not bolted on.
- **The speech lane, next steps** (asked for in
  [discussion #38](https://github.com/hasso5703/alzeo/discussions/38)). It is
  built, opt-in: `./install-speech.sh` serves Qwen3-ASR and Kokoro-82M on the CPU, beside
  any lane, behind OpenAI's audio routes ([docs/speech-lane.md](docs/speech-lane.md)), from
  a script of its own. Possible next steps, none of them planned: a cockpit view (record and
  play in the browser, Load and proof like the other lanes'), the lane in the one command,
  and Qwen3-TTS as a second voice once llama.cpp's server endpoint lands and runs faster
  than real time.
- **Mirrors of every pin** (not started): the checkpoints, the draft and
  the base images mirrored into an org this project controls, and
  `MIRROR.md` as the runbook, so a deleted upstream revision is an
  inconvenience instead of the end of an installation path. The pin-watch
  workflow is the alarm; this is the extinguisher. Needs infrastructure
  decisions (which org, where the images live, what it costs) and they are
  not made on vibes: the first mirror is the one this repo would use on a
  fresh install.

## Considered (and why not yet)

- **vLLM for the Qwen lanes**: vLLM serves five lanes: since v1.29 Kolibri,
  then Bolt and Super VL, the models SGLang did not serve on this box, NVIDIA's
  FP4-Local build of the flash model as its card serves it (v1.37), and Ornith
  (v1.38), whose window SGLang held half of here; for
  the two Qwen lanes, third-party vLLM numbers on this hardware tie or lose
  to the SGLang config here (issue #2, BENCHMARKS.md), and moving a lane
  that serves well needs a measured win, not a second flavor of parity.
- **A GUI for anything the cockpit does not already do**: the cockpit is
  what an operator needs (state, switch, logs, bundles); new surfaces have
  to earn their maintenance cost the way features do.
- **Hosted or telemetry components**: none, by design (SECURITY.md). The one
  request the stack makes that nobody typed is the release check above, which
  sends nothing but the cockpit's version and can be turned off; the telemetry
  question is answered by the Prometheus endpoint, which the box serves and
  never sends anywhere.
- **GGUF fine-tune serving**: `extras/gguf/README.md` carries the
  measured llama.cpp numbers and the sharp edges; a full lane would compete
  with SGLang on the battery before earning a lane, per issue #12.

## Maintenance promises, in the open

- Pins are contracts; `./check-pins.sh` is the daily alarm; a dead pin is
  fixed by a measured re-pin, with the story in the CHANGELOG.
- A contributor's measured number in a box-report issue is a contribution
  to BENCHMARKS.md as soon as its conditions are pinned, credited as such.
- Breaking changes to the serving contract (ports, ceiling chain, the
  closed outcome vocabulary, the recipes format) are issues first, PRs
  second, releases third; the CI gates are the tiebreaker, not taste.
