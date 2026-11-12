# Spark Cockpit: design foundations

One web app that does everything this repo does from the shell, by clicks, with
everything visible in real time. Ships as `qwen38-dashboard.service` since v1.6,
and since v1.12 `install.sh` installs it by default as its last step
(`--no-cockpit` opts out, and the choice sticks).

## Non-negotiables (from the owner)

- Nothing ever breaks: the app must never be able to wedge the box, the serving
  stack, or itself. Every action that mutates state gets confirmation +
  guardrails; every failure is isolated and shown, the rest keeps working.
- No em/en dashes anywhere (house rule, CI-guarded already).
- Beautiful, professional, own palette; UX readable at a glance.
- DGX Spark only for now, but abstractions ready for multi-node later.
- Atomic self-update with automatic rollback.

## Stack decision

**Backend: single Python 3 process, stdlib only (http.server ThreadingHTTPServer).**
Rationale, challenged against FastAPI/uvicorn:
- Zero supply chain: stock DGX OS python3 runs it, no venv, no pip, no wheels
  to pin, nothing to break on update. That IS the reliability requirement.
- Our scale is one box, a handful of viewers: threads + SSE are ample.
- SSE proxy pitfalls (buffering middleboxes) do not apply: same-host or LAN,
  direct connection, no CDN. Long-poll fallback still provided.
- FastAPI would be nicer to write, worse to own: 30+ transitive deps on a box
  that must never break. Rejected.

**Frontend: one static HTML + vanilla JS + hand CSS, no CDN, no framework.**
Same supply-chain logic. Canvas-drawn sparklines (no chart lib).

**Transport: SSE (`/api/stream`) for push; every panel also fetchable by plain
GET for the fallback path. Actions are POST with JSON, CSRF-protected.**

## Security model (NASA mode)

- A first `install.sh` binds the box's tailnet address when it has one, so the
  page opens from a laptop or a phone over a private network, and 127.0.0.1
  otherwise; `dashboard/install-dashboard.sh` run alone binds 127.0.0.1. Any other
  address is an explicit choice (`DASH_BIND` at install, `COCKPIT_BIND` on the
  unit), and a re-run keeps the installed one. A bind other than loopback is
  announced in the journal at startup, and the API key is its only gate.
- Session auth: the app reuses the repo's api-key file as its bearer secret
  (cookie session after a login page; the key never appears in URLs).
- CSRF token on every mutating POST (no Origin check: the token is the guard).
- The backend never interpolates client input into shell commands: every
  action maps to a fixed allowlisted argv template; parameters are validated
  against closed enums (model in {stock,uncensored,flash}, unit in the fixed
  set, etc.). No free-form command execution, ever.
- Privileged actions (systemctl start/stop/enable, unit writes) go through a
  dedicated sudoers drop-in installed by the app installer:
  `hasan ALL=(root) NOPASSWD: /usr/bin/systemctl start qwen38-sglang.service, ...`
  listing EXACT argv lines only. The app runs unprivileged.
- Read paths are allowlisted absolute prefixes (repo dir, config dir, HF cache
  metadata); no path traversal possible (resolved + prefix-checked).
- One job at a time (a second action is refused with 409 while one runs) and a
  per-address limit on failed logins; there is no other rate limit, and no
  idempotency key.
- Agent relay (v1.7.0): opencode's web server stays on loopback with a generated
  Basic password (0600, never leaves the box). The relay binds one address (the
  tailnet address by default), requires the cockpit session cookie, refuses any
  foreign Origin, never forwards the cookie, adds the credentials, and sets
  `frame-ancestors` to the cockpit. One host for cockpit and relay: the frame
  carries the session, so there is no second login and no Basic prompt (which
  Chrome blocks inside cross-origin frames).

## Data sources (verified live on the box, 2026-08-28)

| panel | source | notes |
|---|---|---|
| GPU power/temp | `nvidia-smi --query-gpu=power.draw,temperature.gpu` | memory.used/total are [N/A] on GB10; utilization.gpu can freeze stale (documented trap) |
| GPU processes | `nvidia-smi --query-compute-apps=pid,used_memory` | works |
| Unified memory | `/proc/meminfo` (MemTotal/MemAvailable) | the real GB10 gauge |
| CPU | `/proc/stat` deltas, per-core | |
| Disk | `df -B1` on $HOME + docker root + statvfs; NVMe IO from `/proc/diskstats` | |
| Serving engine | SGLang `GET /server_info` (100+ fields: model, revision, quant, memfrac, mrr, ctx), `GET /v1/loads?include=core`, `GET /health`; the old `/get_server_info` and `/get_load` only on an engine that answers 404 to the new ones | Bearer key from config; works on both lanes |
| Live decode telemetry | `docker logs --since` parsing of scheduler lines (`#running-req`, `#full token`, `accept len`, tok/s) | robust regex, degrade gracefully |
| Requests in flight | keepalive proxy log lines (journald via `docker`/file) + `/v1/loads` | proxy already logs one line per request with outcome |
| systemd | `systemctl show -p ...` (read, no sudo) / actions via sudoers allowlist | |
| Containers | `docker ps/inspect/stats --no-stream` | user is in docker group |
| Repo state | `git -C repo` describe/status/log, pins parsed from install.sh | |
| Inventory | `./uninstall.sh --list` (read-only by design) | |

## Update engine (designed, not built)

None of this section exists at v1.18.7. The cockpit runs from the repo checkout
and imports its code at start, so an update is the one-liner (`get.sh` updates the
checkout, then `install.sh` restarts the cockpit); updating the serving stack is a terminal
command the Settings view prints, because the installer needs an interactive sudo
(the `ACTIONS` table in `cockpit.py` says why there is no `update_stack`). Only
the last point below is real: one job at a time. The design, as written:

- Releases = git tags on the repo. The app runs from `releases/<sha>/` with a
  `current` symlink; `previous` kept.
- Update flow: fetch, checkout new worktree, run its self-checks, atomically
  swap `current`, systemd restart of the dashboard only, health-check within
  N seconds by the NEW process writing a beacon; the OLD version's watchdog
  (systemd `ExecStartPre` guard + `previous` symlink) restores on failure.
- The dashboard updating the SERVING stack = running the repo's own
  `get.sh`/`install.sh` in a supervised job with live log streaming; those
  scripts already converge and never reset choices (validated through v1.5).
- Never two mutating jobs at once (single job-lock with owner and TTL).

## Extensibility groundwork (do not implement now)

- Every data collector implements one interface: `collect() -> dict` with a
  `node_id` field; the store is keyed by node. Today node_id is always
  `local`. Remote nodes later = same schema over HTTP.
- Actions declare `{id, title, argv_template, params_schema, danger_level}` in
  a registry; the UI renders from the registry. New engines/models = registry
  entries, not new UI code.

## Palette (three themes, one set of roles)

The stylesheet names roles (`--surface`, `--surface-border`, `--primary-bg`,
`--field-bg`, `--code-bg`, `--reservoir-bg`, state colours, lane hues) and three
theme blocks fill them. `data-theme` on `<html>` is the only switch. The lane
hues are the lanes' identity, not a theme role: they are the same in every
theme.

- **Deep indigo**, the default: `#170b38` ground, translucent white panels, violet
  accent `#9a6bff`, and the two WebGL halos. This is the palette the cockpit
  shipped with and the one an unset preference gets.
- **Graphite**, dark: `#09090b` ground, `#111113` surfaces, cyan accent `#22d3ee`,
  emerald, amber and red states. Flat surfaces, tighter panel radii, and the
  indigo's two moving halos in neutral greys (black into dark grey).
- **Paper**, light: `#f6f7fb` ground, white surfaces, cyan and teal accent
  `#0e7490`, darker state colours for contrast on white. The same geometry as
  Graphite, with quieter shadows and the indigo's two moving halos in neutral
  greys (white into light grey).

The choice is stored in this browser as `cockpit.theme`, applied before the page
renders, and reflected in the `theme-color` meta tag. Text and state pairs meet
WCAG AA on their surface in every theme; `dashboard/tests/test_page_theme.py`
holds the roles and the measured pairs. Typography stays Geist and IBM Plex
Mono, served by the box. Motion stays 150 to 250 ms ease-out, with no
gratuitous animation.

## Phases

1. Foundations: this doc, CAPABILITIES.md, skeleton server + unit + palette tokens.
2. Read-only cockpit: machine + engine + systemd + repo panels, SSE live.
3. Actions: switches, service control, proxy, jobs with streamed logs.
4. Update engine + inventory + opencode shortcut + polish.
5. Hardening pass: failure injection, load, security review, docs.
Each phase ends with a browser-validated visual check and a written test log.

## Absorbed from the parallel session's webui/SPEC.md (now merged here)

Ideas adopted on top of the plan above; `webui/` is removed, `dashboard/` is
the single home:
- Every mutating action shows THE EXACT shell command in its confirm modal,
  with a dry-run first wherever the underlying script offers one.
- Append-only audit log file of every action (who, when, argv, outcome).
- During a lane boot, the lane card streams the unit journal live with a
  boot-time counter (boots are 7-15 min; silence is the enemy).
- Pins vs upstream drift detection stays READ-ONLY (show, never auto-bump).
- Log views are virtualized (bounded DOM), streams are bounded server-side.
- Health actions: one-click smoke (canary via proxy), quick needle test,
  template validation, auth check expecting 401.
- HostAdapter naming for the future multi-node layer (matches the collector
  node_id groundwork).


## Lifecycle engine (etape 1, 2026-08-29)

One pure module (lifecycle.py) turns raw facts into a single explicit state
per engine: stopped, failed, starting, loading-weights, loading-draft,
allocating-kv, capturing-graphs, warming-up, ready, degraded, stopping.
Facts in: systemd ActiveState/SubState + ActiveEnterTimestampMonotonic,
container presence, the container log tail (markers are SGLang's own lines),
and /health. Boot durations are learned per unit (median of the last 12) and
drive the ETA; every flash boot writes its PLE table whole, so there is no
separate "rebuild" boot to learn apart (there was until v1.8);
a boot is only recorded if the cockpit witnessed the activation change,
so a cockpit restart facing a warm engine never records its uptime.

Action gates live server-side in the same module: starting one engine while
the other occupies the pool is refused with the reason (409), switch and
update wait for transitional states to settle, and mid-boot flash stops are
allowed with a truthful warning (the boot under way is lost, and every flash boot
writes its PLE table from scratch since v1.8). The UI
renders the same rules: disabled buttons print their reason, the confirm
modal carries the warnings, and the per-stage progress bar animates from
stage pills plus elapsed-vs-learned-ETA.

## Research notes for the modernization pass (29/08)

Sources read: Grafana dashboard best practices (official docs), dark-first
dashboard guidance (2026), SSE architecture references.

Principles adopted for the cockpit:
- A dashboard answers ONE question first; general to specific, top-left is the
  answer (engine state and the pool), details below. One row per service,
  ordered by data flow: client, proxy, engine, machine.
- Color has meaning and never carries it alone: every state chip pairs a color
  with a word; thresholds drive color; icons or position cues for CVD users.
- Dark is designed, not inverted: WCAG AA contrast, thin bright lines with
  translucent fills for charts, borders and shadows redesigned for dark.
- Refresh proportional to change rate (1 s vitals, 2 s lifecycle, 5 s units,
  30 s engine info, 90 s probes, 300 s registry): already the tier model.
- Skeleton and empty states for every panel; both themes shipped from day one.
- Normalize axes (percent of pool, not raw tokens) where comparison matters.
- Version the layout as code (this repo), never browser-only edits.
- Alerts lead browsing: the event feed and wedged/degraded chips are the entry
  points, panels are the drill-down.


## Identity decision (29/08, modernization pass)

Grounded in the object: the DGX Spark is a graphite chassis with champagne-gold
anodizing and NVIDIA green status light. Tokens: graphite paper and cards, warm
white text, spark gold as the single accent, NVIDIA green for good, lane colors
kept (violet flash, cyan 27B) because they already carry meaning. Light theme
is warm paper, not inverted dark.

Signature: the reservoir. One full-width strip showing tokens held in the KV
pool against the pool's real capacity, the tick where a single prompt tops out,
and, hatched in red, the part of the model window that never fits at these
memory settings. It is the picture of every incident of the day (scheduler
wedge on two giant contexts, a user's OOM at 230K, opencode limits). The card
cascade animation was removed: one accessory less; motion is kept only where it
carries information (boot shimmer, level transition, live pulse).

## Information architecture (2026-08-30, built)

One question per surface, and the answer never needs scrolling.

| surface | always shows | why |
|---|---|---|
| top bar | the serving lane and its state, the switch selector, start/stop for that lane, flush, abort, smoke, bundle, the connection | the owner asked never to scroll to the bottom to switch; the lane state is the first question the app answers |
| job strip | the running action, its elapsed time, its last log line, its log on demand | it comes from the server, so a reload or a second tab never loses a job |
| banners | lost connection, wedged, failed, degraded, a memory floor that fired, dry run | alarms lead browsing; panels are the drill-down |
| rail | seven sections with a badge when one needs eyes, collapsible to icons, remembered | tabs separate concerns without hiding what is wrong |

Sections: Overview (reservoir, serving lane, right now, unified memory, events),
Engines (the two lanes and the proxy with their buttons, served engine facts, probes and
containers), Requests (the proxy feed, pool and decode), Machine (memory, GPU, CPU, safety
belts), Models (recipes, registry, upstream watch, inventory), Logs (live logs, events,
recent jobs), Setup (repo, opencode, the terminal command for the installer, the cockpit's
own settings).

Rules the panels follow: every panel declares its collectors and shows their age; a panel
whose data is older than three refresh periods dims and says how old it is; a field never
shimmers unless it has never been loaded, otherwise it names its state; an action that
cannot work right now is disabled and says why in one sentence.



## Design pass on the built app (2026-09-03)

The first build shipped the information architecture; this pass fixed how it reads.
Reviewed by screenshotting all seven tabs in both themes at 1512 px (a MacBook Air's
default width) and at 1440, 1280, 1180 and 1024, then measuring the top bar's children
against the header box.

Rules the app follows from here:

- **One spacing scale** (`--s1`..`--s6`, 4 to 30 px). No component invents padding.
- **No tracked-out capitals.** Titles and labels are sentence case at reading weight;
  a hairline separates a panel title from its content.
- **The value column holds values.** Anything that is a sentence is shortened to a
  phrase with the full text in the tooltip, so a definition list never wraps over four
  ragged right-aligned lines.
- **One primary action per surface.** In the action bar that is the switch; red is
  reserved for stopping and aborting; everything else is neutral.
- **Empty states are written.** A table that can be empty names what would fill it; a
  panel with no data says why, once.
- **The interface never prints its wire format.** Actions read as sentences from one
  vocabulary (`ACTION_PHRASE` in js/base.js, `job_phrase` in cockpit.py), never as the
  parameter dict that happens to carry them.
- **Prose stops at 78 characters** whatever the panel width.
- **The top bar is one row by construction**, and `--top` is measured, not assumed, so
  a bar that wraps anyway takes the sticky rail and job strip with it.


## The rebuild (2026-09-29/30)

The page was rebuilt around one pool and four lanes. The top bar and seven tabs became a
head and a rail of eleven views in three groups: Operate (Now, Lanes, Traffic, Machine),
Create (Agent, Decide, Image, Video) and Maintain (Library, Logs, Settings). The head
carries what serves (a tap opens Lanes), the pool at a glance, the running job, the
connection and the engine actions. Changing lanes left the bar's selector for one journey
per lane, switch, stop and start, from the Lanes view or from Now's rack. Below 980 px the
rail is a drawer behind the menu button. The sections above describe the page before
this, and their rules (one spacing scale, written empty states, no wire format in the
interface) carried over.

What the phone taught the day after (2026-09-30), found with the reference box's phone:

- **Nothing takes a z-index unless it is meant to be a stacking context.** One on `.shell`
  trapped the drawer under the veil its own opening draws, so a tap on a view only closed
  the drawer, and held the Agent's fullscreen frame under the head. An animation left
  filling after it ends is a stacking context too.
- **A fixed box inside a grid keeps the grid's alignment.** The desktop rail's
  `align-self: start` made the fixed phone drawer as tall as its items, so on a short
  screen it never scrolled and its last items were out of reach.
- **The whole screen, asked for, has to be kept.** With `viewport-fit=cover` the head, the
  dock, the toasts, the sheets and the fullscreen frame read the safe-area insets.
- **A check that clicks from script proves nothing about a finger.** `element.click()`
  ignores what covers the element; `touch-check.mjs` sends real touch events where a finger
  lands and audits every control of every view for anything covering it.

