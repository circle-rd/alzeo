# The cockpit

The full tour. The README carries the short version: what the cockpit is, how it is installed, what it binds to, and the table of its views. This is everything else, starting with what it looks like.

## Installing it, and what it binds to

A local dashboard for this stack: what is served right now, whether it is
healthy for real, and the handful of actions you would otherwise type by hand.
A stdlib backend (cockpit.py and six modules beside it), no pip and no venv. Since v1.12 `install.sh`
installs it as step 10/10, once the engine has answered a real generation, and
prints its URL as the last thing it says. You do not run anything else:

```bash
curl -fsSL https://get.alzeo.ai | bash
# ... ends with:  ▶ OPEN THE COCKPIT:  http://<address>:30090
# the login is the API key from ~/.config/qwen38/api-key
```

On a first install it binds the box's **tailnet address** when there is one, so
the page opens from your laptop or your phone over a private network, and
`127.0.0.1` otherwise. A re-run never changes that: `install-dashboard.sh`
converges on the installed unit, so an upgrade cannot flip a reachable cockpit
back to loopback. Install it alone, or change the bind, with the same script:

```bash
dashboard/install-dashboard.sh          # DASH_PORT=30090 by default
```

To open a loopback-bound cockpit from another machine, set `DASH_BIND` and
re-run it:

```bash
DASH_BIND=0.0.0.0 dashboard/install-dashboard.sh        # every interface
DASH_BIND=$(tailscale ip -4) dashboard/install-dashboard.sh   # that interface only
```

Then browse `http://<the box's tailnet or LAN address>:30090`. The login is the
same API key, over plain HTTP: the session cookie is `HttpOnly` and
`SameSite=Strict` and every mutating POST carries a CSRF token, but there is no
TLS, so this belongs on a tailnet or a LAN you trust and never on the open
internet. On an already installed cockpit, change it in place:

```bash
sudo systemctl edit qwen38-dashboard    # [Service] Environment=COCKPIT_BIND=0.0.0.0
sudo systemctl restart qwen38-dashboard
```

That moves the page only. The Agent view's relay keeps its own address,
`COCKPIT_AGENT_BIND`, which a re-run of `dashboard/install-agent.sh` keeps too: set it in
the same drop-in (`Environment=COCKPIT_AGENT_BIND=tailscale`, or an address), or re-run
that script with `AGENT_BIND=`.

## What it looks like

![The cockpit's Now view: one pool of unified memory and what holds it, the lanes in their rack with their makers' marks and the serving one edged in green, and below them the requests and the timeline](img/cockpit-now.png)

*Now: one pool of unified memory, lanes that take turns in it, and which one holds the box. The timeline is the engines' own state transitions, including the kernel's GPU-allocation refusals that precede the memory edge on this hardware.*

![The Lanes view: each lane with its maker's mark, every version it can load in one row of buttons with the one serving marked in green, its Load or Stop button, its facts and its last five boots](img/cockpit-lanes.png)

*Lanes: each lane with what it serves, what its boots took on this box and the KV pool each one won, and one button that loads it: switch, stop, start, each step asking first and showing its command.*

A lane installed on demand says so on its card (built at its first Load): its Load builds it first,
as you, with no sudo (`./prepare-lane.sh <lane>`, with the target it loads for the two Qwen lanes,
whose preparation fetches that checkpoint), then switches, stops and starts. Its Start, which starts
the unit as it is with no switch, appears once a Load has switched to it: before, the unit had no
checkpoint or chat template to run.

![The Traffic view: the running, waiting and KV counters, the requests the keepalive proxy relayed with client, route, size, duration and outcome, and the abandoned-request guard](img/cockpit-traffic.png)

*Traffic: both sides of the wire. The feed is what the proxy relayed; the guard is whether any client walked away from an answer the engine is still generating.*

![The Video view with the 27B lane holding the box: the button that loads MiniMax-H3, then the mode, examples, prompt, length, size and speed of a call, beside where the video appears](img/cockpit-video.png)

*Video: MiniMax-H3 on this box. While another lane holds the box, the view says which one and loads the video lane with one button; what a call costs here shows before it is made.*

Deep indigo, two halos of light drifting slowly behind translucent panels, and colour kept
for meaning: green is ready and serving, amber on its way, red broken, grey idle, blue memory
and information, violet the controls. Each lane carries its maker's mark on its bay and its
card in Lanes, and its own hue on its share of the pool and the gauge in the head, and a
lane's versions are all in sight, one button each. The backdrop is decoration and behaves like it:
the browser draws it at a quarter of its width and height and 8 frames a second at most,
pauses it in a hidden tab and under the Agent's fullscreen frame, keeps it still for a system
that asks for reduced motion, leaves it out under forced colours, and shows the same light in
CSS where WebGL is missing. Every text measured on every view meets WCAG AA contrast (4.5:1,
3:1 for large text), on a desktop and a phone over the backdrop's whole drift (v1.27.0). Fonts,
marks and scripts are all served by the box: the page fetches nothing from elsewhere.

## What it does that a terminal does not

- **Lane state that is not a lie.** A wedged SGLang still answers `/health`, so
  the cockpit runs a real generation canary and reports `ready`, `loading`,
  `wedged` or `stopped` from that, with the served checkpoint named from the
  unit rather than guessed.
- **Where a client sends its requests.** The Now view's "Connect a client" panel gives
  the OpenAI address (`http://<the address you opened the cockpit by>:30001/v1`) and the
  Anthropic one, the model that serves and where the key is, never the key itself, each
  ready to copy. Opened on the box, it lists the addresses another machine reaches it
  by, the tailnet's first; on a proxy that listens on the box alone (`PROXY_BIND=127.0.0.1`)
  it says so and how to open it, with a certificate it says https, and with client keys
  on it asks for one, as the proxy then refuses any other. Its test request reads the key
  from its file through a descriptor, never from curl's arguments, and `claude-local`
  sets Claude Code up for the lane that serves ([clients](clients.md)).
- **Belts.** A host `MemAvailable` floor that aborts generations before the box
  reaches the memory edge, and a counter of the kernel's `NVRM` allocation
  refusals, which is how the memory-edge behaviour in the flash section was
  found in the first place.
- **A long prompt, said while it is computed.** A prompt the prefix cache
  cannot serve is computed whole before its first token, 19 min for 494,089
  tokens on the Ornith lane, and vLLM counts no prompt token until it is done.
  Since v1.40.7 a request in flight shows how long it has been so in the
  Traffic view, and on the vLLM lanes the Now and Traffic views say how much
  of its prompt is computed, from the KV cache it fills, with a floor of the
  time left ("at least", never a promise: docs/ornith-lane.md). It follows a
  prompt only from an admission vLLM's counters prove to be one request, says
  it only while it runs alone, and says no floor once another request has
  shared the engine with it. A banner
  says when a client sent a long prompt again after leaving it without an
  answer, with what widens that client's timeout (docs/clients.md).
- **Actions, one at a time.** Unit start/stop/restart, lane switch (the same
  `switch-model.sh` you would run), cache flush, abort-all, smoke probe. Every
  action is audited to `~/.config/qwen38/cockpit-audit.log` with its exact argv.
- **Recipes and drift.** Every target as data, derived from `install.sh` and the lane
  templates so a recipe cannot drift from what the installer renders, compared flag by
  flag against the invocation actually running on the box. Since v1.8.5 that comparison
  covers value-less flags too (`switch.--sleep-on-idle: recipe true, installed false` is
  what the panel said the morning the flash lane was caught spinning a core).
- **Zombie guard.** A client that gives up leaves the engine decoding unless something
  stops it, so the Traffic view reads both sides of the wire: the engine's own flood
  lines grouped by request, worst first, with the span between a request's first and
  last line, which is the dead decode; what the proxy did about it over the same window
  (aborted, drained, the longest drain, aborts the engine never answered); the version
  of the **running** proxy from its startup banner; and whether the engine accepts the
  proxy's request id at all, because without that an abandoned answer can only be
  drained. See the v1.8.4 and v1.8.5 changelog entries.
- **Jobs.** Every action above, the opencode fit and the diagnostics bundle run
  as supervised one-at-a-time jobs with live output, instead of commands you
  type blind into a terminal. Benchmarks are not among them: `./bench.sh` and
  `./bench-matrix.sh` run in a terminal (BENCHMARKS.md).
- **Housekeeping.** Inventory of everything the repo ever put on the box and
  what each item costs in bytes (read-only, from `./uninstall.sh --list`; the
  reclaim commands are printed by `./uninstall.sh` once it has removed the
  services, and by the installer for images an earlier version left, never run
  from the page), the opencode integration state (limits per lane, default
  model, launcher) with the one action that writes there, fitting those limits
  to the pool the engine actually booted with, and the repo itself (version,
  branch, head, a dirty working tree, whether a newer release is out, and a link to the release notes). There
  is no panel for the API key, which is what you log in with; the diagnostics
  bundle masks it.
- **Checkpoints.** Library lists every lane's checkpoints, the text lanes', the image
  and video lanes', Kolibri's, the two Nemotron lanes' (their drafters too), Qwen3.8 Flash-Next FP4-Local's and Ornith's, in the caches the lanes read (each lane's unit says
  which), each revision pinned or stray, with what it holds on disk. A stray revision
  is one no pin, no installed unit and no ref names: the line says what deleting it
  frees, or why it stays (a ref names it, and a load by name reads it), and one button
  deletes the ones it offers, after a sheet that names each. Each goes with its
  snapshot and the blobs only it links to, never a blob another revision shares; the
  job scans again before it deletes, and the page sends no name and no path. It is the
  one cleanup the page runs; the rest of the reclaim stays a command. Below, each target a
  Load can switch to says whether its checkpoint is on disk (whole for what its lane
  serves), begun or not downloaded, and Download fetches it where its lane is installed and built:
  `./switch-model.sh <target> --fetch`, the switch's own fetch (the cache first, resumable,
  its room checked), which changes nothing else, no unit, no boot, no engine.

On a phone the rail is a drawer behind the menu button, controls are 44 px
targets, and the Agent view opens fullscreen (see "On a phone" below): the whole
box is operable from a hand.

**The privileged surface, stated plainly.** The unit actions need root, so the
installer writes `/etc/sudoers.d/qwen38-cockpit`: an exact argv allowlist,
no wildcard or regular expression (a CI step refuses `*`, `?`, `[` and `^` on its lines), validated with `visudo -c`
from a temp file before it lands so a bad render can never brick sudo. It covers
start/stop/restart and enable/disable of this repo's units, `daemon-reload`, the
writes `switch-model.sh` performs, one read-only forensics wrapper, kernel
journal reads, and the GPU clock cap (`nvidia-smi -lgc 0,<MHz>` at its three caps,
and `nvidia-smi -rgc`). `./uninstall.sh` removes it along with the unit and the wrapper.
If that surface is more than you want, do not install the cockpit: nothing else
in this repo depends on it.

**Self-restart is off by default** (`COCKPIT_AUTOHEAL=0` in the unit). This repo
spends a whole section on how a GB10 box freezes, so an engine that restarts
itself is not something an install should decide for you. Arm it once you know
what a wedge looks like on your box:

```bash
sudo systemctl edit qwen38-dashboard    # [Service] Environment=COCKPIT_AUTOHEAL=1
                                        # and Environment=COCKPIT_AUTOHEAL_GRACE=600
                                        # to keep a wedge up for forensics first
```

Remove it with `./uninstall.sh` (which takes the whole stack) or on its own:

```bash
sudo systemctl disable --now qwen38-dashboard
# a GPU clock cap the cockpit keeps (Machine view) is lifted: it holds until the next reboot otherwise
[ -f ~/.config/qwen38/cockpit-gpu-clock.json ] && sudo /usr/bin/nvidia-smi -rgc
rm -f ~/.config/qwen38/cockpit-gpu-clock.json
sudo rm -f /etc/systemd/system/qwen38-dashboard.service \
           /etc/sudoers.d/qwen38-cockpit /usr/local/bin/qwen38-pyspy-scheduler
sudo systemctl daemon-reload
```

## The GPU clock cap

Since v1.47.0 the Machine view caps the GPU's clock: **None**, the factory setting and the
default, or 2,400, 2,200 or 2,000 MHz. A cap is a ceiling with no floor
(`nvidia-smi -lgc 0,<MHz>`), and None lifts it (`nvidia-smi -rgc`); each goes through its exact
line of the sudoers allowlist, after the sheet that shows the command. nvidia-smi applies it at
once, "regardless if an app is running" in its own words, so a generation in flight goes on at
the new clock. The view shows the clock nvidia-smi reads and, for each cap, what it cost and gave here:

| Cap | Clock read | tok/s, 4 streams | GPU watts | Hottest zone | tok/s, 1 stream | Prefill, tok/s |
|---|---|---|---|---|---|---|
| None | 2,502 MHz | 105.74 | 46.6 W | 92.6 °C | 56.20 | 2,078 |
| 2,400 MHz | 2,387 MHz | 105.87 | 35.9 W | 90.2 °C | 56.22 | 2,073 |
| 2,200 MHz | 2,190 MHz | 103.77 | 28.1 W | 86.6 °C | 55.50 | 2,027 |
| 2,000 MHz | 1,989 MHz | 103.15 | 22.8 W | 84.4 °C | 54.62 | 2,004 |
| None, again at the end | 2,494 MHz | 105.02 | 47.7 W | 93.2 °C | 56.38 | 2,080 |

*The reference box, 2026-11-11: the Qwen3.8 Flash-Next FP4-Local lane at 4 streams, the same
work at each cap, 48 answers of 1,200 tokens.* At 2,400 MHz the lane answered as fast as with no
cap (105.87 tok/s, against 105.74 and 105.02 for the two runs without one) for 10.7 W less on the
GPU and 2.4 °C less at the box's hottest zone; at 2,000 MHz it gave up 2.59 tok/s at 4 streams (2.4 %)
for less than half the GPU's watts and 8.2 °C less.

The clock nvidia-smi reads can lag behind a change of cap (still the old one 2 s after it on the reference box), then
follows the cap within a minute, the GPU idle included. On the reference box an idle GPU still read 2,502 MHz two
seconds after a cap of 2,000 MHz was set, and still 1,989 MHz just after that cap was lifted; after 51 to 111 s of
rest, idle at 7 to 12 W, it read 2,392 MHz under a cap of 2,400, 2,197 under 2,200, 1,989 under 2,000, and 2,411 MHz
with no cap (51 s after the cap was lifted). So a reading above the cap just after it is set is not a cap that failed.

What this does not say yet: one lane was measured, a text one, with one run per cap (None twice,
first and last), and the image, video and 27B lanes are not measured. The watts are the GPU's
alone, as nvidia-smi reads them, not the whole box's. nvidia-smi warns, at each cap it sets, that
the persistence mode is disabled: on a box where nothing holds the GPU any more, the driver may
lose the cap, and the view would still say it is set. Not verified: the reference box keeps its
GPU active with its desktop. The cap set again at the cockpit's start was run from a start of the
cockpit, not from a cold boot of the box: if nvidia-smi cannot answer yet when the cockpit starts,
that try fails, the view says the kept cap is not set, and a click sets it.

An image model under study, MiniMax-H3-Image, which is not a lane yet, was measured the same day,
one image at a time (10 at 2048x2048 and 3 at 4736x3552 per cap, no cap again at the end): 2,400
MHz changed nothing, and 2,000 MHz cost 8 % more time per image (16.86 s against 15.58 s, and 89.15
s against 82.87 s) for 28 to 34 % fewer GPU watts and a box's hottest zone at 87.7 °C against 97.0
°C at 2048x2048. Diffusion pays the cap more than text does.

A cap does not survive a reboot of the machine. The cockpit keeps the one you chose in
`~/.config/qwen38/cockpit-gpu-clock.json` and sets it again at its start, which is at boot, as a
job like any other (the dock and the Jobs list say "again at the cockpit's start"). Until that
job has run, or when it fails, the view says the cap is kept and not set, never that it holds.
When nvidia-smi does not answer within 20 s, at the start or at a click, the view says the cap
in effect is not known: the cockpit stops waiting, but nvidia-smi runs as root, out of its
reach, and may still set or lift the cap. None removes the file. The cockpit knows only the caps
it set: nvidia-smi's clock query gives the clock and the GPU's maximum (3,003 MHz on the
reference box), not a cap, so a cap set by hand outside the cockpit is not read back, and with
none set from the cockpit the view says just that.

A machine without `/usr/bin/nvidia-smi` offers no cap, and says why; None stays on while the
cockpit keeps a cap there, and forgets it without running anything. On a box whose sudoers
allowlist predates the cap, sudo refuses the call: the view says so, and the cockpit's installer
(`./dashboard/install-dashboard.sh` in the checkout) writes the four lines. `./install.sh` runs
it too, unless it leaves the cockpit out (`--no-cockpit`, `--no-service`, `--no-start`).
`./uninstall.sh` lifts the cap the cockpit keeps and removes its file, once the cockpit is
stopped and before its sudoers lines go.

## The Decide view: the typed-decisions endpoint, from a browser

`POST /v1/systemone` answers probabilities instead of text, and until this view the only way
to see one was curl. The view is the console for it, and it exercises the whole contract:

- **five prefilled examples**, one per shape the endpoint is actually used for: routing a
  support ticket, choosing an agent's next tool, moderating a comment, extracting a field,
  and an eight-option choice, the likely cause of an engine crash (labelled A to H: the
  labels only turn to two letters past 26 options)
- **an editor for all three question types**, with options and levels you can add and remove,
  so a question you are about to put in production can be tried before it is
- **the answer drawn as the distribution it is**: a bar per option, the pick and its
  confidence, and for a score the level the number lands on
- **the headers the contract has no room for**: how many branches the call fanned out to, how
  much of the model's first-token probability landed on a label, and how many tokens the
  radix cache served
- **the same call as a curl** that updates as you type and copies in one click, because the
  point of the view is the request, not the view
- **the measured comparison against the hosted Jev**, task by task, from BENCHMARKS.md

The serving key never reaches the page. The browser sends the state and the questions to the
cockpit, and the cockpit calls the proxy with the key it already holds; a test asserts that a
body naming its own path or upstream changes neither. Availability is probed rather than
assumed, so a box whose proxy predates v6.19 is told why instead of looking broken.

**Image** is next to it: since v1.18.0 it drives Qwen-Image 2.1, the third lane, when that lane
serves (generation, editing with up to ten references, native RGBA; see
[docs/image-lane.md](image-lane.md)). **Video** drives MiniMax-H3, the fourth lane, when that lane
serves (text to video with joint video-and-audio, first/last-frame conditioning; see
[docs/video-lane.md](video-lane.md)).

## Being told there is a newer version

A box that runs an old release does not know it. The answer existed from v1.6 in the
Library view, printed after a button press, which means it reached whoever already
suspected there was news. Since v1.15.2 the cockpit volunteers it: every six hours it
asks GitHub whether a newer release is published, and if there is one the banner strip
says so with the command that installs it, and a button that copies it. The Settings view
carries the same line permanently, next to the repo's own version, and the command with its
button: the one command of the README in its long form, get.sh from GitHub itself (get.alzeo.ai
redirects to that file, and a box already installed trusts no new party for its updates), run
from the checkout so that it updates that clone
(`./install.sh` alone fetches nothing: it installs the release already there again).

Two details decide whether this is useful or irritating. It compares version **numbers**,
not strings, so `v1.15.0` is correctly newer than `v1.9.0`, and it only speaks when the
published version is **strictly newer** than the installed one, because a box that
develops this repo is regularly ahead of the newest tag and would otherwise be nagged
forever. The installed release is the one the last `./install.sh` that ended well wrote
down in `~/.config/qwen38/installed.json` (since v1.36.0), not what the git tags or the
checkout's CHANGELOG.md say: the one-liner fetched main and no tag until v1.20.2, so a box it
updated described itself as the release it was first cloned at, and a `git pull` with no
install moved the CHANGELOG while the services ran the release before. A box no run of
v1.36.0 or later recorded says so ("before v1.36.0"), and a clone that moved on since the
last run is said to be ahead of what its services run. A box with no network says "latest
release unknown" rather than raising an alarm, and stops asking for a while instead of
spending a request every collection.

An install that runs is said as one (since v1.37.5). `./install.sh` keeps a file in
`~/.config/qwen38` while it lasts (`install-running.<pid>.json`: its process, the time that
process started, and the release it installs), and the cockpit believes it while that very
process lives, so a pid the system gives again later is not taken for it. The strip then says
"Installing v1.37.5." and since when, and holds back, while the run lasts, the banners that ask
for an update, for a run of the installer or for a restart of the cockpit; once it ends, the page
says what is left. An update of 1 h 45 on a new box, 144 GB of video to fetch, had run under the
orange banner that asked to run the installer. The next run removes the file of a run killed
outright.

Every command a banner asks to run sits apart from its sentence, with a button that copies it
(since v1.37.5). The Copy buttons work over plain HTTP too: a box opened by its LAN or Tailscale
address is no secure context for the browser, which gives such a page no clipboard API, and
every Copy button there said the browser refused. They copy through a selected text field there.

The same strip carries the other staleness a running cockpit can have: a `git pull`
under a live process leaves it serving new HTML against the Python it imported at start,
so its controls and its checks disagree. The banner names the files that changed and the
one command that fixes it.

And the staleness of the engine that serves. An update rewrites a lane's unit (what it runs
and how), a script the unit runs or the image it names, and leaves that engine serving, so a
box keeps answering through an update: what the update brought applies at the lane's next
start. The cockpit asks `engine-inputs.py`, the rule the installers go by, whether the engine
started after every input it reads was written (every 30 seconds, each `systemctl` or `docker`
call bounded at 5 s; for the vLLM lanes, also the auth middleware their installer counts too),
and when it did not, the strip says so and what changed, and the lane's card offers "Restart
<lane> to apply the update", a Load of one step: the restart alone, which leaves the lane enabled
at boot as it is. Nothing is said while a switch waits for its restart (the strip says what the
next start loads, and that Load applies both) or while the engine boots. The image and video
lanes' runtime, which their installer rebuilds, is not in this check yet: their installer says
when its restart is owed. The `[Unit]` lines that name the other lanes, which an added lane
rewrites in every unit, are not an input: the installers keep the unit's date when only they
change, and no restart is asked for them.

And the settings a box runs behind on (since v1.36.0). A value that only an older release's
default put on the box (the engine on every interface before v1.17.0, the native window before
v1.12.1, the flash lane's context tier before v1.19.0, the BF16 drafter before v1.9.0) stayed
there for good, said once in an update's output; the next update moves it to today's default,
unless the operator chose it. The cockpit reads the box against what its clone recommends, the
list every run prints before its first step (`./install.sh --plan` prints it alone and changes
nothing), and when the next update moves something the strip says so in orange, with what it
moves. Settings lists each difference, why it stays (recorded as a choice, never a default, a
setting of the site) or the variable that keeps it. Neither a box as recommended nor one that
differs by its operator's choices alone gets a banner: a choice is not a delay, and Settings caps
it in plain. While a setting differs that is not the site's, Settings also gives the command that
brings the box to the recommended state, choices included: the update's one command with
`-s -- --recommended`.

`COCKPIT_UPDATE_CHECK=0` turns the outbound check off. SECURITY.md names it as the one
request this stack makes that the operator did not type.

## On a phone

The cockpit is built for a hand as well as for a desk, and that is checked with a finger
rather than assumed. Below 980 px the rail is a drawer behind the menu button at the top
left: a tap opens it, a tap on a view shows that view and closes it, and a tap beside it or
Escape closes it. The head keeps what is serving and the engine actions, and the cards
stack. On a phone the request log shows one card per request, outcome beside time, and the
Library's wide tables stack into cards whose lines carry their column's name. Controls are
at least 44x44 CSS px, every form control is 16 px or larger (iOS Safari zooms the whole
page when a smaller one takes focus and never zooms back), heights use `dvh`, and the page
keeps clear of what an iPhone reserves: the notch or island in landscape, the home
indicator at the foot.

![The cockpit on a phone, with the drawer open over the Now view: the rail's eleven views in three groups, Operate, Create and Maintain](img/cockpit-phone.png)

The Agent view opens **fullscreen on a phone**, because there the view is the frame:
opencode gets the whole screen, held to the safe area, and **Back to the cockpit** brings
the rest back. That choice is remembered per device, so leaving fullscreen once makes the
embedded frame the default from then on; embedded, the frame fills what is left under the
view's buttons, or takes a screen and scrolls under the head when too little is. It never
opens fullscreen when the panel cannot load (a relay bound to another address, a stopped
server): covering the explanation with a blank frame would leave nothing to act on.

Two browser checks hold this. `dashboard/tests/touch-check.mjs` runs in CI against a
cockpit of its own: real touch events on five touch screens (four iPhone sizes and an iPad)
and a laptop, every rail item tapped where a finger lands, every control of every view
audited for anything covering it, and the safe areas emulated. It exists because every
earlier check switched views with `click()` from script, which ignores what covers an
element: on a phone the drawer's veil covered every rail item while every check stayed
green (found on the reference box's phone, 2026-09-30). `dashboard/tests/mobile-check.mjs`
runs against a live box, taps the same way, and measures the layout on four iPhone
geometries, the Agent view in both of its modes included. Run it against the address the
phone uses, so the Agent view behaves the way it does in a hand:

```bash
node dashboard/tests/touch-check.mjs                                          # a cockpit of its own
node dashboard/tests/mobile-check.mjs http://<the box's tailnet address>:30090
```

## The Agent view: opencode in the browser, behind the cockpit login

Since v1.7.0 the cockpit can hold opencode's own web interface, so a session on the
box runs from the laptop without a terminal: sessions, the project picker, file
diffs, the terminal panel, the same config, plugins, skills and MCP servers as the
`oc` command. Since v1.12 `install.sh` installs it with the cockpit whenever
opencode is on your PATH, and since v1.18.3 the installer puts the pinned opencode there
when there is none (docs/opencode.md); when it still is not, the installer says so
and skips that one view rather than failing an install that is otherwise up. Run
it by hand after installing opencode, or to retune it:

```bash
dashboard/install-agent.sh
```

Two pieces land, and the shape is the security model:

- **`opencode-web.service`** runs `opencode serve` as you on `127.0.0.1:4096`
  (`OPENCODE_PORT=`), with Basic credentials generated once into
  `~/.config/qwen38/opencode-web.env` (mode 0600). Nothing else ever reaches it,
  and the password never leaves the box. The unit carries your PATH and the same
  output-token cap as the `oc` launcher, so long thinking is not cut at 32,000.
- **The relay** inside the cockpit process listens on ONE address (port
  `AGENT_PORT=30091`). Installed without `AGENT_BIND=`, that is the tailnet address
  when the cockpit binds every interface or the tailnet, loopback when the cockpit
  is on loopback, and the cockpit's own address otherwise, so a cockpit bound to a
  LAN address puts the relay on the LAN too; `AGENT_BIND=` sets any other.
  It answers a request only with a valid cockpit session cookie, refuses any
  foreign `Origin` and shows opencode its own in place of the browser's (opencode
  gives its terminal only to a page of its own host or of loopback), strips the
  cookie before forwarding, adds the credentials, and streams everything back:
  plain answers, the event stream, the WebSocket of the terminal panel. Its
  responses carry `frame-ancestors` naming the cockpit, so no other page can frame
  the interface.

The browser therefore sees one host for the cockpit and the relay (cookies ignore
ports): the Agent view frames the interface with no second login and no Basic-auth
prompt, which Chrome would block inside a cross-origin frame anyway. That is also
the one rule: open the cockpit through the address the relay binds. With the
cockpit on `0.0.0.0` or on the tailnet address, that is
`http://<tailnet address>:30090/#agent`; the view says so when you arrive by
another name. A cockpit bound to `127.0.0.1` gets a loopback relay, usable on the
box itself.

What the view shows: the state of the server and the relay, the served opencode
version and, after `opencode upgrade`, that a newer binary is installed with a
**Restart server** button (systemd restart, through the same exact-argv sudoers
allowlist as the other units, three more lines). **Fullscreen** makes the
interface cover the whole browser window; **Back to the cockpit** (drag it wherever
it is least in the way) or Escape brings the cockpit back, and a reload comes back the
way it was left. Prefer it to opening more browser tabs: every browser tab of the interface holds one permanent
event stream, browsers allow six connections per origin, and a sixth tab freezes
them all (the interface handles any number of sessions in one tab). **Open in a
new tab** still exists for a second screen. The Logs view reads the server's journal.

Permissions are opencode's own. Its defaults (opencode 1.18, 1.19) allow most tool
calls and ask before a tool touches a path outside the session's project and
when the same call repeats three times; the interface shows those prompts. For
the autonomy of the `oc` launcher's `--yolo` (a flag `opencode serve` rejects),
install with `AGENT_AUTO=1`: the unit then carries `OPENCODE_PERMISSION` set to
allow everything, which opencode honours, followed by this repo's shell rules for
Alibaba Cloud's hosts and the cloud metadata addresses (v1.40.4). opencode merges it after the rules of your
config, `deny` included, so its `"*"` overrides them, unless your config has a
`"*"` of its own (SECURITY.md). Your `opencode.json` gets those shell rules when it
is this repo's copy or lists its providers ([agents-and-network.md](agents-and-network.md)).
The view says which mode the running server applies. Re-runs remember the choice;
`AGENT_AUTO=0` turns it back off.

```bash
AGENT_AUTO=1 dashboard/install-agent.sh
```

Variables: `OPENCODE_PORT` (4096), `AGENT_PORT` (30091), `AGENT_BIND` (an address,
or `tailscale`), `AGENT_AUTO` (0 or 1), `AGENT_OUTPUT_TOKEN_MAX` (the output
ceiling; by default `./oc-limits.sh --max-out`, the largest limit any target asks for), `AGENT_PATH` (the PATH the service gets; yours by
default, plus the directories of the installed one, less what an editor's session puts on
its terminal's PATH, such as VS Code's remote CLI, since v1.22.7; an `AGENT_PATH` you set
is taken as given on that run). A re-run of either script keeps
what it finds installed unless a variable says otherwise: `install-agent.sh` both
ports, the relay's address, `AGENT_AUTO` and the service's PATH (since v1.18.7;
before, every `./install.sh` put them back on the defaults), and
`dashboard/install-dashboard.sh` the relay settings, the bind and the port. Remove
with:

```bash
sudo systemctl disable --now opencode-web.service
sudo rm -f /etc/systemd/system/opencode-web.service
DASH_AGENT_PORT=0 dashboard/install-dashboard.sh       # the cockpit without the relay
```

A browser check holds the view against a real opencode, in CI: `dashboard/tests/agent-bench.mjs`
fetches the opencode release `install.sh` pins (checked against its sha256), runs it on loopback with
a HOME, a config and a password of its own, and drives it through a dry-run cockpit whose relay answers
on 127.0.0.2, as a box's relay answers on its tailnet address: the relay's gate, the Agent tab, the
phone layer in the page opencode sends, and a terminal with a command run in it. A browser counts
127.0.0.2 as a secure context and an http:// tailnet address not: the page asked as from the tailnet
(no Sec-Fetch headers) is checked from node, but an API a browser keeps to secure contexts is not. opencode starts with
the command line of the unit `install-agent.sh` writes, so a flag a release refuses fails there first. It
needs Chrome; `AGENT_BENCH_TARBALL` lets it use a release already downloaded, and
`AGENT_BENCH_OPENCODE=<binary>` tries another release before it is pinned (OpenCode 2.1.3 does not start
with the unit's `--log-level INFO`, found that way on 2026-10-23). `dashboard/tests/agent-check.mjs` runs the same kind of checks against a live box:

```bash
node dashboard/tests/agent-bench.mjs                                          # its own opencode and cockpit
node dashboard/tests/agent-check.mjs http://<the box's tailnet address>:30090
```

[Back to the README](../README.md)
