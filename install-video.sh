#!/usr/bin/env bash
# Install the MiniMax-H3 video lane: text-to-video and video-and-audio generation,
# served by SGLang Diffusion on this box. Idempotent, and safe to run alone.
#
#   ./install-video.sh                 first install, or update in place
#   ./install-video.sh --no-smoke      skip the generation at the end
#   ./install-video.sh --uninstall     remove the unit and the venv (weights kept)
#   ./install-video.sh --units-only    the unit alone: no runtime, no weights (install.sh, on demand)
#   ./install-video.sh --prepare       the runtime and the Turbo adapter, no sudo: the cockpit's
#                                      first Load runs it (./prepare-lane.sh video)
#
# install.sh runs this on every plain install since v1.20: a box gets every lane. The
# checkpoint (the fl2va partition plus the shared components and the Turbo adapter, 145
# GB, 135 GiB in WEIGHTS_GB below), the runtime with its build (another 12, RUNTIME_GB's
# 11 GiB) and the runtime's own caches (17, 15.7 GiB measured) need about 174 GB of
# headroom; when the disk does not have it, this refuses before
# downloading anything, install.sh says so and goes on, and ./install.sh --no-video leaves
# the lane out of the run that passes it.
#
# WHY IT IS A VENV AND NOT DOCKER. Like the image lane, no published Docker image is
# verified for this model: the cookbook serves it from Python/source. The release wheel
# goes in first all the same, because it carries the prebuilt aarch64 native kernels
# that a source tree does not build.
#
# WHY MINIMAX-H3. The SGLang cookbook verifies this model on the DGX Spark itself:
# about 12.1 s per denoise step steady-state, about 40 s of decode, about 12 min per
# warm request at 480P, with no flags at all (adding the discrete-GPU offload flags
# measured 2.1x slower on the same box). So this lane passes no placement flags: the
# verified recipe is the whole recipe. Video and audio are denoised jointly in one
# pass and muxed into one MP4.
#
# ONE ENGINE AT A TIME. The video checkpoint does not fit beside any serving lane.
# The unit says so with Conflicts=, so starting this stops the other lanes and
# starting another lane stops this. Nothing here has to be remembered by the operator.
set -euo pipefail
# One spelling of the home, without a trailing slash, as install.sh says why: the cockpit's
# exact-argv sudoers is rendered from it and has to match the cockpit's own calls (2026-10-22).
while [ -n "${HOME:-}" ] && [ "$HOME" != / ] && [ "${HOME%/}" != "$HOME" ]; do HOME="${HOME%/}"; done; export HOME
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=qwen38-video.service
INSTALLED="/etc/systemd/system/$UNIT"
die(){ printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
step(){ printf '\n\033[1;36m── %s\033[0m\n' "$*"; }

# Under sudo every path below moves to /root: the venv, the weights and the key the
# unit reads. install.sh refuses the same way, for the same reason.
if [ "$(id -u)" = "0" ] && [ "${ALLOW_ROOT:-0}" != "1" ]; then
  die "run this as the user who will use the box, not as root (everything it installs is addressed from \$HOME)."
fi

SMOKE=1; ACTION=install; MODE=full
for a in "$@"; do case "$a" in
  --no-smoke) SMOKE=0 ;;
  --uninstall) ACTION=uninstall ;;
  # The two halves of a lane installed on demand, as for the image lane: --units-only puts
  # in what needs root (the unit, its start script) and nothing heavy, and --prepare builds
  # the runtime and fetches the Turbo adapter as the user, with no sudo, when the cockpit
  # first loads the lane. The checkpoint comes with that Load's switch, as for any Load.
  --units-only) [ "$MODE" = full ] || die "--units-only and --prepare are the two halves of an install: one at a time"; MODE=units ;;
  --prepare) [ "$MODE" = full ] || die "--units-only and --prepare are the two halves of an install: one at a time"; MODE=prepare ;;
  -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1' "$0"; exit 0 ;;
  *) die "unknown option: $a" ;;
esac; done

# What the installed unit says, so a re-run without variables changes nothing.
installed(){ { grep -m1 -E "^$1=" "$INSTALLED" 2>/dev/null || true; } | cut -d= -f2-; }
unit_flag(){ { grep -m1 -oE -- "$1 [^ \\\\]+" "$INSTALLED" 2>/dev/null || true; } | awk '{print $2}'; }
# A checkpoint read back from the unit, unless ./install.sh --recommended asks for the default
# one (v1.36.0); the port and the bind are read back either way.
kept_flag(){ [ "${QWEN38_RECOMMENDED:-0}" = 1 ] || unit_flag "$1"; }

CONFIG_DIR="$HOME/.config/qwen38"
# the text lane the box comes back to, one record shared with switch-model.sh (lanes.sh)
# shellcheck source=lanes.sh
. "$HERE/lanes.sh"
# A 64 GB DGX Spark cannot hold this lane (boxmem.py): refused there before anything is fetched,
# and --uninstall still takes away what an older release put there (found in review, 2026-10-23).
[ "$ACTION" = uninstall ] || [ "$(box_mem_class)" != 64 ] || lane_fits video || die "The video lane (MiniMax-H3) needs a 128 GB DGX Spark, and this box is a 64 GB one."
# A terminal opened before the account joined the docker group: the run goes on with it (lanes.sh).
docker_session bash "$HERE/${BASH_SOURCE[0]##*/}" "$@"
# Read back one slash at a time, as unit_hf_cache reads the cache (lanes.sh): a unit rendered from a
# home with a trailing slash says "//", and its own spelling kept it as it was at every update.
LANE_DIR="${VIDEO_LANE_DIR:-$(installed WorkingDirectory | tr -s /)}"; LANE_DIR="${LANE_DIR:-$HOME/.local/share/qwen38-video}"
VENV="$LANE_DIR/venv"
SRC="$LANE_DIR/sglang"
WHEEL_NOTE="$LANE_DIR/sglang-wheel"   # the wheel release the venv's dependencies come from (step 3)
SOURCE_NOTE="$LANE_DIR/sglang-source" # the SGLang commit the venv runs from (step 3)
RUNTIME_NOTE="$LANE_DIR/runtime-changed"   # touched whenever this script changes the runtime the lane runs (step 3)
PORT="${VIDEO_PORT:-$(unit_flag --port)}"; PORT="${PORT:-30022}"
# The checkpoint and its revision, pinned like the rest: refs/main on 2026-09-25.
# The first smoke on the reference box confirms it; until then the pin-watch alarms
# on any upstream move. VIDEO_MODEL_REV overrides it; another VIDEO_MODEL is fetched
# at VIDEO_MODEL_REV, or main.
VIDEO_MODEL_PIN="MiniMaxAI/MiniMax-H3"
VIDEO_MODEL_PIN_REV="42ed227ee7df40d41602854ae760620d6eb651fe"
MODEL="${VIDEO_MODEL:-$(kept_flag --model-path)}"; MODEL="${MODEL:-MiniMaxAI/MiniMax-H3}"   # = VIDEO_MODEL_PIN
if [ "$MODEL" = "$VIDEO_MODEL_PIN" ]; then MODEL_REV="${VIDEO_MODEL_REV:-$VIDEO_MODEL_PIN_REV}"; else MODEL_REV="${VIDEO_MODEL_REV:-main}"; fi
# The Turbo adapter (the Video view's Turbo switch): the cookbook's recommended
# speed/quality recipe for the FL2VA weights, larryvrh's eight-evaluation LoRA: one file of
# 0.78 GB in a repository of 112 GB (its other versions and experimental checkpoints), so it
# is fetched by name, at the revision measured on the reference box on 2026-10-10.
VIDEO_TURBO_REPO="larryvrh/MiniMax-H3-Turbo-Lora"
VIDEO_TURBO_REV="43a74557ac3f6539db8e0f2a959d03feb7a81480"
VIDEO_TURBO_FILE="minimax_h3_turbo_v4_step600_ema.safetensors"
# fl2va serves text-only plus first/last-frame conditioning; ref2va serves reference
# image/audio/video conditioning instead. They are checkpoint partitions: the variant
# must match the weights fetched. VIDEO_VARIANT overrides it.
VARIANT="${VIDEO_VARIANT:-$(kept_flag --model-variant)}"; VARIANT="${VARIANT:-fl2va}"
case "$VARIANT" in fl2va|ref2va) ;; *) die "VIDEO_VARIANT takes fl2va or ref2va, got: $VARIANT" ;; esac
# The installed unit's cache, then the one the text lane mounts, then the default: an
# update that ignored the unit downloaded into ~/.cache and rewrote HF_HOME on a box
# whose lane lived on another disk (same bug as the image lane, 2026-09-24).
installed_env(){ { grep -m1 -E "^Environment=$1=" "$INSTALLED" 2>/dev/null || true; } | cut -d= -f3-; }
TEXT_UNITS="${TEXT_UNITS:-/etc/systemd/system/qwen38-sglang.service $CONFIG_DIR/launch-flash.sh}"
text_cache(){
  # shellcheck disable=SC2086  # a list of paths
  unit_hf_cache $TEXT_UNITS
}
HF_CACHE="${HF_CACHE:-$(installed_env HF_HOME | tr -s /)}"; HF_CACHE="${HF_CACHE:-$(text_cache)}"   # one slash at a time, as LANE_DIR
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
# The SGLang source this lane runs: the v0.5.21 release's commit, as the image lane does
# since v1.24 (the lane ran main's ddebc52 from 2026-09-25 until v1.25). SGLANG_DIFFUSION_PIN
# overrides it only for someone deliberately testing another one.
PIN="${SGLANG_DIFFUSION_PIN:-e00930c5489053f26d86b179cee0d087f846acbb}"
# The released wheel that carries the prebuilt aarch64 kernels the source tree reuses.
WHEEL="${SGLANG_DIFFUSION_WHEEL:-0.5.21}"
# Loopback, and not from ENGINE_BIND: that variable belongs to the LLM lane, which has
# a key. This one has none (the diffusion parser has no --api-key at all), so a
# non-loopback bind puts an unauthenticated video generator on that interface.
# VIDEO_BIND overrides it for someone who means to, and is told what it costs.
VIDEO_BIND="${VIDEO_BIND:-$(unit_flag --host)}"; VIDEO_BIND="${VIDEO_BIND:-127.0.0.1}"
case "$VIDEO_BIND" in
  127.*|localhost) ;;
  0.0.0.0) die "VIDEO_BIND=0.0.0.0 puts a keyless video generator on EVERY interface (LAN, tailnet, future NICs). This lane has no --api-key; bind loopback (the cockpit is the door) or name one specific address." ;;
  *) echo "WARNING: binding $VIDEO_BIND. This lane has no API key (the diffusion runtime has"
     echo "         no --api-key), so anyone who can reach that address can generate on your GPU."
     echo "         The scheduler's own channel stays on 127.0.0.1 whatever this bind (scheduler-loopback)."
     echo "         torch's rendezvous port, 30005, listens on all the box's addresses too, with no key,"
     echo "         and a bind past loopback leaves it unfiltered (SECURITY.md)."
     echo "         Loopback plus the cockpit is the intended shape." ;;
esac
case "$VIDEO_BIND" in
  *[!0-9.]*|""|*..*) [ "$VIDEO_BIND" = localhost ] || die "VIDEO_BIND takes an IPv4 address, got: $VIDEO_BIND" ;;
esac
# The runtime listens past its bind: torch's TCPStore on port 30005 of every interface, and gloo's
# and NCCL's bootstrap on the box's LAN address, none with a key (2026-10-30). The unit keeps that
# bootstrap on lo, and a loopback bind gets its IP filter (lane_ip_deny, lanes.sh).
VIDEO_IP_DENY="$(lane_ip_deny "$VIDEO_BIND")"; VIDEO_IP_ALLOW="${VIDEO_IP_DENY:+localhost}"
WEIGHTS_GB=135; RUNTIME_GB=11   # the fl2va partition plus the shared components and root
# metadata, measured 2026-09-25: 144.1 GB cached in 88 files, which is 134.2 GiB, and the
# room check below counts GiB (it said 145, and a box with the whole checkpoint cached was
# told "10 GB to download", 2026-10-01). 135 holds it with the Turbo adapter's 0.73 GiB (134.9
# in all). The check below measures what is cached of the checkpoint alone, in whole GiB down:
# a box with the checkpoint cached is told it has nothing to download, and the 0.8 GiB left
# over is the adapter's room (136 told such a box 1, 2026-10-10). The venv, the checkout and the
# wheel (8.6 to 8.8 GiB on the reference box, measured 2026-10-17) and the pip build tree make
# RUNTIME_GB.

if [ "$ACTION" = uninstall ]; then
  step "Removing the video lane"
  # what the box's links of v1.29 say, read while this lane is still the one at boot (lanes.sh)
  text_lane_migrate
  if [ -f "$INSTALLED" ]; then
    WAS_BOOT=0; systemctl is-enabled --quiet "$UNIT" 2>/dev/null && WAS_BOOT=1
    sudo systemctl disable --now "$UNIT" 2>/dev/null || true
    sudo rm -f "$INSTALLED"; sudo systemctl daemon-reload
    echo "unit removed"
    # When the video lane was the box's lane at boot, whatever it replaced was disabled
    # by that switch: removing it left no engine at all (same trap as the image lane).
    # The lane before video comes back.
    TEXT_ENABLED=0
    for u in qwen38-sglang.service qwen38-flash.service qwen38-image.service qwen38-kolibri.service qwen38-nemotron.service qwen38-nemotron-super.service qwen38-flash-local.service qwen38-ornith-122b.service; do
      systemctl is-enabled --quiet "$u" 2>/dev/null && TEXT_ENABLED=1
    done
    if [ "$WAS_BOOT" -eq 1 ] && [ "$TEXT_ENABLED" -eq 0 ]; then
      # The text lane noted (lanes.sh), or the one installed: it read a link of its own, and a
      # lane it named that was removed since left the box with no lane at boot (found in review,
      # 2026-10-14).
      text_lane_give_back video "the video lane"
    fi
  fi
  # Only what this script put there: LANE_DIR can be a directory shared with other work.
  if [ -d "$LANE_DIR" ]; then
    rm -rf "$VENV" "$SRC" "$WHEEL_NOTE" "$SOURCE_NOTE"
    echo "runtime removed: $VENV and $SRC"
    rmdir "$LANE_DIR" 2>/dev/null || echo "kept $LANE_DIR: it holds files the video lane did not put there"
  fi
  echo "the video checkpoint is left in $HF_CACHE; delete it yourself if you want the space:"
  echo "  rm -rf $HF_CACHE/hub/models--${MODEL//\//--}"
  exit 0
fi

# Step 5, the one part that needs root: the unit, its outputs folder and its start script.
# A function, since a lane installed on demand (--units-only) takes it after the preflight.
install_service(){
  step "5/6 Service"
  # The lane's own output directory: the download needs the file to still be there,
  # so the server writes every MP4 here instead of a temp dir it deletes. Old videos
  # stay until deleted by hand; uninstall.sh leaves them alone.
  mkdir -p "$LANE_DIR/outputs"
  # The unit runs engine-preflight.sh before every start: run on its own, this installer
  # must put it in place, or the lane would never start.
  mkdir -p "$CONFIG_DIR"
  cmp -s "$HERE/engine-preflight.sh" "$CONFIG_DIR/engine-preflight.sh" \
    || install -m 755 "$HERE/engine-preflight.sh" "$CONFIG_DIR/engine-preflight.sh"
  RENDER="$(mktemp)"; trap 'rm -f "$RENDER"' EXIT
  sed -e "s|__USER__|$(id -un)|g" -e "s|__GROUP__|$(id -gn "$(id -un)")|g" \
      -e "s|__VIDEO_LANE_DIR__|$LANE_DIR|g" -e "s|__VIDEO_VENV__|$VENV|g" \
      -e "s|__VIDEO_MODEL__|$MODEL|g" -e "s|__VIDEO_VARIANT__|$VARIANT|g" \
      -e "s|__VIDEO_PORT__|$PORT|g" \
      -e "s|__VIDEO_BIND__|$VIDEO_BIND|g" -e "s|__VIDEO_IP_DENY__|$VIDEO_IP_DENY|g" -e "s|__VIDEO_IP_ALLOW__|$VIDEO_IP_ALLOW|g" -e "s|__HF_CACHE__|$HF_CACHE|g" \
      -e "s|__HOME__|$HOME|g" \
      "$HERE/$UNIT.template" > "$RENDER"
  grep -q '__[A-Z][A-Z0-9_]*__' "$RENDER" && die "the unit template still holds an unsubstituted placeholder: $(grep -o '__[A-Z][A-Z0-9_]*__' "$RENDER" | sort -u | tr '\n' ' ')"
  # 0644 like every other unit, and set explicitly: mktemp creates 0600 and cp keeps the
  # mode, which left the image unit readable by root alone until this fix. Everything
  # that reads the unit (switch-model.sh, the cockpit) failed quietly on a 0600 unit.
  UNIT_PUT="$(unit_put "$RENDER" "$INSTALLED")"
  case "$UNIT_PUT" in
    same) echo "unit unchanged"
          sudo chmod 644 "$INSTALLED" ;;   # a unit installed before this fix is still 0600
    deps) sudo systemctl daemon-reload
          echo "unit updated: only the lines naming the other lanes changed, which the lane does not read" ;;
    *)    sudo systemctl daemon-reload
          echo "unit installed at $INSTALLED" ;;
  esac
  # A lane that serves goes on serving the unit, the runtime and the start scripts it started with:
  # said here when no proof follows to restart it, whether this run or an earlier update changed
  # them (an update leaves a serving lane serving, and said nothing for the image and video lanes:
  # found in review, 2026-10-14). The runtime's dates: runtime-changed, touched at each change
  # since v1.30, and the notes of the wheel and of the source, which said the changes before.
  LANE_FILES=("$INSTALLED" "$RUNTIME_NOTE" "$WHEEL_NOTE" "$SOURCE_NOTE" "$CONFIG_DIR/engine-preflight.sh")
  if [ "$SMOKE" -eq 0 ] && lane_serves_older "$UNIT" "${LANE_FILES[@]}"; then
    echo "NOTE: The video lane runs older files than the ones installed now (its unit, its runtime or its start scripts); it runs these after: sudo systemctl restart $UNIT   (or Load it again from the cockpit)"
  fi
  # Installed, not switched to: the lane this box serves stays the lane it serves. The
  # video lane becomes the boot lane the same way the other lanes do, by a switch, which
  # is also what makes it come back after a reboot.
  echo "installed as a lane of its own. Switch to it like any lane:"
  echo "  the cockpit: Load on MiniMax-H3 in the Lanes view (it switches, stops the serving lane, starts this one)"
  echo "  a terminal : ./switch-model.sh video, then the two commands it prints"
}

step "1/6 Preflight"
case "$(uname -m)" in aarch64|arm64) : ;; *) die "this lane is verified on the DGX Spark's ARM64 GB10 only (this box is $(uname -m))." ;; esac
command -v nvidia-smi >/dev/null || die "nvidia-smi not found: this needs the NVIDIA driver."
command -v git >/dev/null || die "git is required (stock on DGX OS)."
# Run from a terminal, this installs what the lane needs and the box lacks (lanes.sh
# host_packages). The checks below, as before, say what could not be put in: the cockpit's
# runs have no terminal for sudo's password, and install.sh puts these in for them.
HOST_PKGS=()
{ command -v ffmpeg && command -v ffprobe; } >/dev/null 2>&1 || HOST_PKGS+=(ffmpeg)
python3 -c 'import venv, ensurepip' 2>/dev/null || HOST_PKGS+=(python3-venv)
[ -f "$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])' 2>/dev/null)/Python.h" ] || HOST_PKGS+=(python3-dev)
[ "${#HOST_PKGS[@]}" -eq 0 ] || host_packages "the video lane" "${HOST_PKGS[@]}" || true
# The MiniMax-H3 pipeline requires ffmpeg and ffprobe for media processing and
# validated output delivery, and refuses to start without them (its own words in
# minimax_h3_pipeline.py). Installed from the OS package, once, before anything else.
command -v ffmpeg >/dev/null || die "ffmpeg is missing, and the MiniMax-H3 pipeline refuses to start without it (with ffprobe). Fix: sudo apt-get install -y ffmpeg"
command -v ffprobe >/dev/null || die "ffprobe is missing, and the MiniMax-H3 pipeline refuses to start without it (with ffmpeg). Fix: sudo apt-get install -y ffmpeg"
# ensurepip, not just venv: a box without python3-venv makes a venv with no pip.
python3 -c 'import venv, ensurepip' 2>/dev/null || die "python3-venv is missing (a venv made without it has no pip). Fix: sudo apt-get install -y python3-venv"
# python3-dev too: xatlas, which sglang[diffusion] asks for, is built here against Python's
# headers (PyPI has no aarch64 wheel of it), and DGX OS installs python3-pip without them.
[ -f "$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')/Python.h" ] || die "python3-dev is missing (xatlas, which this lane needs, is built here against Python's headers). Fix: sudo apt-get install -y python3-dev"
[ -s "$CONFIG_DIR/api-key" ] || die "no API key at $CONFIG_DIR/api-key. Run ./install.sh first: the cockpit is the authenticated door in front of this lane, and it reads that file."
if [ "$MODE" = units ]; then
  echo "OK: $(uname -m), key present; the runtime and the checkpoint come with the lane's first Load"
  install_service
  step "Done: the video lane is installed; its first Load prepares it"
  echo "  the cockpit: Load on MiniMax-H3 in the Lanes view builds its runtime (about 12 GB),"
  echo "               fetches its checkpoint (about 145 GB), then starts it"
  echo "  a terminal : ./install-video.sh --prepare, then ./switch-model.sh video and the commands it prints"
  exit 0
fi
# Measured where each part lands, by bytes in the blobs: the folder alone says nothing,
# since huggingface_hub creates it before the first byte.
# existing: lanes.sh's (the nearest folder of a path that exists)
free_gb(){ { df -BG --output=avail "$(existing "$1")" 2>/dev/null || true; } | tail -1 | tr -dc '0-9'; }
HAVE_B="$({ du -s --apparent-size -B1 "$HF_CACHE/hub/models--${MODEL//\//--}/blobs" 2>/dev/null || true; } | cut -f1)"
# what is left, in whole GiB (a complete checkpoint is 0 left, not 1)
WEIGHTS_NEED=$(( (WEIGHTS_GB * 1073741824 - ${HAVE_B:-0}) / 1073741824 )); [ "$WEIGHTS_NEED" -ge 0 ] || WEIGHTS_NEED=0
# --prepare: the checkpoint is the Load's switch's to fetch, and to measure
[ "$MODE" = prepare ] && WEIGHTS_NEED=0
# The two caches the runtime writes at the lane's first start (lanes.sh), counted nowhere before
# this check (2026-10-10). Summed per disk: on a stock box all of it lands on one.
declare -A NEED_ON=() FREE_ON=() WHAT_ON=()
PARTS=("$HF_CACHE|$WEIGHTS_NEED|the checkpoint" "$LANE_DIR|$RUNTIME_GB|the runtime and its build")
while IFS= read -r PART; do PARTS+=("$PART"); done < <(video_first_start_caches)
for PART in "${PARTS[@]}"; do
  IFS='|' read -r WHERE GB WHAT <<< "$PART"
  DEV="$(stat -L -c %d "$(existing "$WHERE")")"
  NEED_ON[$DEV]=$(( ${NEED_ON[$DEV]:-0} + GB ))
  FREE_ON[$DEV]="$(free_gb "$WHERE")"
  WHAT_ON[$DEV]="${WHAT_ON[$DEV]:+${WHAT_ON[$DEV]}, }$GB for $WHAT ($WHERE)"
done
for DEV in "${!NEED_ON[@]}"; do
  [ "${FREE_ON[$DEV]:-0}" -ge "${NEED_ON[$DEV]}" ] \
    || die "${FREE_ON[$DEV]:-?} GiB free on one disk, and this lane needs about ${NEED_ON[$DEV]} GiB there: ${WHAT_ON[$DEV]}. Free some space, or point HF_CACHE or VIDEO_LANE_DIR at a bigger disk."
done
echo "OK: $(uname -m), key present, checkpoint $([ "$MODE" = prepare ] && echo "left to the Load's switch" || { [ "$WEIGHTS_NEED" -eq 0 ] && echo 'already cached'; } || echo "$WEIGHTS_NEED GiB to download") into $HF_CACHE"

step "2/6 Runtime ($LANE_DIR)"
mkdir -p "$LANE_DIR"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" || die "could not create the venv at $VENV"
  echo "venv created on $("$VENV/bin/python" -V)"
elif ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
  # A venv made while python3-venv was missing has its python and no pip, and every
  # later run took it for a finished one, then died on its first pip call.
  echo "the venv at $VENV has no pip: making it again"
  python3 -m venv --clear "$VENV" || die "could not make the venv at $VENV again"
else
  echo "venv already at $VENV ($("$VENV/bin/python" -V))"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null

step "3/6 SGLang Diffusion (released wheel first, then the pinned source over it)"
# Order matters and is not cosmetic. The wheel resolves the whole dependency tree AND
# ships sglang-kernel built for aarch64; installing the source tree first leaves the
# runtime without those kernels and the server dies on the first request.
# The wheel is also what sets the versions of everything the runtime imports, so a pin
# that moves to a new release takes that release's wheel too, as in install-image.sh: the
# version installed is written down once the whole runtime is in (below, after the registry
# check), and a venv from before that note, or one a run left half built, takes the wheel again.
if ! "$VENV/bin/python" -c 'import sglang' 2>/dev/null || [ "$(cat "$WHEEL_NOTE" 2>/dev/null)" != "$WHEEL" ]; then
  echo "installing sglang[diffusion]==$WHEEL (~10 min on a new venv: torch and the kernels are large; an existing one only takes what changed)"
  "$VENV/bin/pip" install --quiet --pre "sglang[diffusion]==$WHEEL" \
    || die "the released wheel would not install. Re-run: pip resumes. If it keeps failing, SGLANG_DIFFUSION_WHEEL=<version> picks another."
  touch "$RUNTIME_NOTE"
fi
if [ ! -d "$SRC/.git" ]; then
  git clone --quiet https://github.com/sgl-project/sglang "$SRC" || die "could not clone SGLang."
fi
# Two local changes to the pinned source, the image lane's scheduler-idle-poll and
# scheduler-loopback (one file each in image-sglang/ for both lanes, which applies to the
# pin of each). scheduler-loopback keeps the scheduler's own channel, a socket that
# unpickles what it is sent, on 127.0.0.1 whatever --host is (install-image.sh says why,
# 2026-10-24). For scheduler-idle-poll: the diffusion scheduler's loop never waits: recv_reqs() polls its socket without blocking and nothing
# else in the loop sleeps, so a lane with nothing to do held one CPU core at 100%, kept
# the box's hottest zone near 61 C at rest (43 C with no lane loaded) and its fans
# loud. The patch waits on the request socket for up to a second, as the LLM scheduler's
# own IdleSleeper does. Measured on the reference box (2026-10-06), 3 min after the lane
# was ready: 1.05 cores without it, 0.05 with it, the hottest zone 67 C against 54 C; a
# 4 s 480P request was picked up in the second it arrived and ran in 691 s (592 to 760
# measured before), and a stop at rest took 0.24 s. The rest is the cookbook's recipe
# as upstream wrote it at the pinned commit; the honest consequences live in the unit
# template (a stop cancels the generation in flight, measured under a second on all
# seven logged stops, with TimeoutStopSec as a ceiling, not a wait) and in
# docs/video-lane.md.
PATCHES=(scheduler-idle-poll scheduler-loopback)
declare -A PATCH_FIXES=(
  [scheduler-idle-poll]="an idle lane no longer holds a CPU core"
  [scheduler-loopback]="the scheduler's channel stays on 127.0.0.1 whatever --host"
)
CURRENT="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || true)"
if [ "$CURRENT" != "$PIN" ]; then
  # it is the only local edit in this tree: take it off, or the checkout trips on it
  # Only from a file that has local edits: a fresh clone stands on upstream's main, where a
  # patch merged upstream reverses cleanly too, and taking it off there wrote the very edit
  # that then blocked the checkout (a first install, 2026-10-10, once main carried #43391).
  for P in "${PATCHES[@]}"; do
    PF="$HERE/image-sglang/$P.patch"
    git -C "$SRC" diff --quiet -- "$(grep -m1 '^+++ b/' "$PF" | cut -c7-)" 2>/dev/null && continue
    git -C "$SRC" apply --reverse "$PF" >/dev/null 2>&1 || true
  done
  git -C "$SRC" fetch --quiet origin "$PIN" 2>/dev/null || git -C "$SRC" fetch --quiet origin
  git -C "$SRC" checkout --quiet "$PIN" \
    || die "could not check out $PIN: not in the SGLang repository, or local edits in $SRC block it (git -C $SRC status says which)."
  echo "source checked out at ${PIN:0:12}"
else
  echo "source already at ${PIN:0:12}"
fi
# The commit the tree holds, noted beside the runtime only when it changes, as the wheel is:
# the cockpit puts the Turbo adapter on only for the commit it was measured on, and only on a
# lane started after both notes (dashboard/cockpit.py video_runtime_stale). A lane that runs
# ddebc52, this lane's runtime before v1.25, loads the whole DiT to put an adapter on.
HEAD_NOW="$(git -C "$SRC" rev-parse HEAD)"
[ "$(cat "$SOURCE_NOTE" 2>/dev/null)" = "$HEAD_NOW" ] || { printf '%s\n' "$HEAD_NOW" > "$SOURCE_NOTE"; touch "$RUNTIME_NOTE"; }
for P in "${PATCHES[@]}"; do
  PF="$HERE/image-sglang/$P.patch"
  if git -C "$SRC" apply --reverse --check "$PF" >/dev/null 2>&1; then
    echo "$P: already applied"
  elif git -C "$SRC" apply --check "$PF" >/dev/null 2>&1; then
    git -C "$SRC" apply "$PF" || die "$P passed its check and then failed to apply to $SRC."
    touch "$RUNTIME_NOTE"
    echo "$P: applied, ${PATCH_FIXES[$P]} (effective at the lane's next start)"
  else
    # A newer pin may have changed that code, or fixed it upstream. The lane works either
    # way; the smoke test below measures what an idle lane costs and says so.
    echo "NOTE: $P does not apply to ${PIN:0:12}; that part runs as upstream wrote it."
  fi
done
# --no-deps: the wheel above already resolved them, and letting the source tree resolve
# again pulls a transformers that breaks the encoders this model needs.
# SGLANG_BUILD_RUST_EXTS=none: the pinned source declares five Rust extensions, all of
# the LLM runtime; none is imported by the diffusion server. Building them needs cargo,
# which DGX OS does not ship.
if ! "$VENV/bin/python" -c 'import sglang, pathlib, sys; sys.exit(0 if str(pathlib.Path(sglang.__file__).parent).startswith("'"$SRC"'") else 1)' 2>/dev/null; then
  echo "overlaying the pinned source (editable, no dependency resolution, no Rust extensions)"
  SGLANG_BUILD_RUST_EXTS=none "$VENV/bin/pip" install --quiet --no-deps -e "$SRC/python" \
    || die "the editable overlay failed. The venv still holds the released wheel; re-run to retry."
fi
# Asked of the registry itself, not of its source text: v0.5.21 names the model in a table
# of non-diffusers pipelines, lower-cased, and the text check that held at ddebc52 failed
# there although the runtime served it (2026-10-10). The id must resolve to MiniMax-H3's
# own pipeline, registered under that name.
"$VENV/bin/python" - <<'PY' || die "the runtime does not know MiniMax-H3. The pin may be wrong for this checkout."
import pathlib, sglang
from sglang.multimodal_gen import registry
from sglang.multimodal_gen.runtime.pipelines.minimax_h3_pipeline import MiniMaxH3Pipeline
getattr(registry, "_ensure_registry_initialized", lambda: None)()
name = registry.get_non_diffusers_pipeline_name("MiniMaxAI/MiniMax-H3")
if name != "MiniMaxH3Pipeline" or registry.get_pipeline_class(name) is not MiniMaxH3Pipeline:
    raise SystemExit("MiniMaxAI/MiniMax-H3 does not resolve to MiniMax-H3's own pipeline in the model registry")
src = pathlib.Path(sglang.__file__).parent
print(f"   runtime: {src}")
PY
# The runtime is whole: its note says so, and it is what the cockpit and install.sh read as
# this lane prepared. Written last, so a run that stops on the way (a clone that fails, a
# patch, the overlay, the registry) leaves a lane its next Load prepares again, not one it
# takes for built and serves on the bare wheel (found in review, 2026-10-15). And only when it
# changes: its date is one of the runtime's (LANE_FILES, the cockpit's stale-runtime check), and
# rewritten at every run it told a serving lane it ran older files, at each update (found in
# review of that fix, the same day).
[ "$(cat "$WHEEL_NOTE" 2>/dev/null)" = "$WHEEL" ] || printf '%s\n' "$WHEEL" > "$WHEEL_NOTE"

step "4/6 Checkpoint ($MODEL at ${MODEL_REV:0:12}, one-time, resumable)"
# Only the partition this lane serves is fetched, by allowlist. The repo holds both
# weight partitions (FL2VA/ and Ref2VA/, 81 files each) plus a root-level native
# layout, and --model-variant fl2va roots the pipeline in the FL2VA subfolder alone
# (default_model_subfolder in minimax_h3_pipeline.py): fetching the rest is dead
# weight on a box whose disk is the constraint (seen on the reference box, 2026-09-25:
# the full pull filled the disk at 98% and died). A lane installed on the ref2va
# weights fetches those instead.
if [ "$MODEL" = "$VIDEO_MODEL_PIN" ]; then
  if [ "$VARIANT" = "fl2va" ]; then ALLOW="FL2VA/* model_index.json modular_model_index.json scheduler/* audio_scheduler/* *.md LICENSE*"
  else ALLOW="Ref2VA/* model_index.json modular_model_index.json scheduler/* audio_scheduler/* *.md LICENSE*"; fi
else ALLOW="";
fi
# Said out loud, because a silent empty here refetches the other partition.
echo "fetching with allow: ${ALLOW:-(whole repo)} (variant $VARIANT, model $MODEL)"
# The same two lessons the LLM lane learned the hard way: the Hub's Xet backend stalls
# silently on this box (0-8 MB/s against 89 on the classic CDN), and an unauthenticated
# pull gets throttled, so HF_TOKEN is passed through when it is set.
# ckpt-fetch.py is the one fetch of every lane's checkpoint (resumed attempts, the room, the
# byte count). The unit serves the repo by name with HF_HUB_OFFLINE=1, which resolves
# refs/main, and a download by commit writes no ref: --refs-main pin points main at the
# pinned commit, which is what is served.
[ "$MODE" = prepare ] && echo "the checkpoint is left to the Load's switch, which fetches it; the Turbo adapter only, here"
[ "$MODE" = prepare ] || HF_HOME="$HF_CACHE" HF_HUB_DOWNLOAD_TIMEOUT=30 HF_HUB_DISABLE_XET=1 \
  "$VENV/bin/python" "$HERE/ckpt-fetch.py" "$MODEL" "$MODEL_REV" --allow "$ALLOW" --refs-main pin \
  || die "checkpoint download failed. Causes: no internet, HuggingFace throttling an unauthenticated download (set HF_TOKEN=<your token>), a pinned revision removed upstream (VIDEO_MODEL_REV=main serves the current one), a disk too small for the partition served, or a permission error in the cache. Re-running resumes."
# The Turbo adapter, for the FL2VA weights it was trained on; a ref2va lane has no Turbo.
# The lane serves without it, so a failed fetch is a note: the Video view keeps its Turbo
# switch off until a re-run of this script fetches it.
if [ "$VARIANT" = "fl2va" ]; then
  HF_HOME="$HF_CACHE" HF_HUB_DOWNLOAD_TIMEOUT=30 HF_HUB_DISABLE_XET=1 \
    "$VENV/bin/python" - "$VIDEO_TURBO_REPO" "$VIDEO_TURBO_REV" "$VIDEO_TURBO_FILE" <<'PY' \
    || echo "NOTE: the Turbo adapter ($VIDEO_TURBO_REPO, 0.78 GB) could not be fetched; the lane serves without it, and a re-run fetches it."
import sys
from huggingface_hub import hf_hub_download
repo, rev, name = sys.argv[1:4]
print("   Turbo adapter: " + hf_hub_download(repo, name, revision=rev), flush=True)
PY
fi
if [ "$MODE" = prepare ]; then
  step "Done: the video lane's runtime is ready; its Load fetches the checkpoint and starts it"
  exit 0
fi

install_service

# The smoke body is t2va on MiniMax-H3's fl2va weights: the ref2va weights take no
# unconditioned call, and another model's id the smoke does not name. Asking for
# either and booting fifteen minutes for a body declared wrong here is a 400 on a
# 15-minute bill; skip the smoke and say why (the same exit as --no-smoke).
if [ "$SMOKE" -eq 1 ] && { [ "$VARIANT" != "fl2va" ] || [ "$MODEL" != "MiniMaxAI/MiniMax-H3" ]; }; then
  echo "smoke skipped: its t2va body fits MiniMax-H3 on the fl2va weights only (this install: model $MODEL, variant $VARIANT)"
  SMOKE=0
fi
if [ "$SMOKE" -eq 0 ]; then step "Done (smoke test skipped)"; exit 0; fi

step "6/6 Proving it serves (one short video, then the box goes back to the lane it was serving)"
WAS_OTHER=""
for u in qwen38-sglang.service qwen38-flash.service qwen38-image.service qwen38-kolibri.service qwen38-nemotron.service qwen38-nemotron-super.service qwen38-flash-local.service qwen38-ornith-122b.service; do
  systemctl is-active --quiet "$u" 2>/dev/null && WAS_OTHER="$u"
done
# If this lane was already serving, the test leaves it serving: stopping it on the way
# out would turn "re-run the installer" into "take the video lane down".
WAS_VIDEO=0
systemctl is-active --quiet "$UNIT" 2>/dev/null && WAS_VIDEO=1
[ -n "$WAS_OTHER" ] && echo "note: $WAS_OTHER is serving and will be stopped for this test, then started again."
# From here on the other lane is DOWN, so putting it back cannot live on the happy
# path: every die() below would leave the box serving nothing. The trap runs on
# success and on every failure alike.
restore_other_lane() {
  [ "$WAS_VIDEO" -eq 1 ] || sudo systemctl stop "$UNIT" 2>/dev/null || true
  if [ -n "$WAS_OTHER" ]; then
    echo "starting $WAS_OTHER again"
    sudo systemctl start "$WAS_OTHER" || echo "WARNING: could not restart $WAS_OTHER. Do it by hand: sudo systemctl start $WAS_OTHER"
  fi
  rm -f "$RENDER" "${OUT:-}"
}
trap restore_other_lane EXIT
# A lane that runs older files than the ones installed now restarts for the proof: the engine that
# runs proved nothing about what this run installed (found in review, 2026-10-14).
if [ "$WAS_VIDEO" -eq 1 ] && lane_serves_older "$UNIT" "${LANE_FILES[@]}"; then
  echo "The video lane runs older files than the ones installed now (its unit, its runtime or its start scripts): it restarts to prove these"
  sudo systemctl restart "$UNIT"
else
  sudo systemctl start "$UNIT"
fi
READY=0
for i in $(seq 1 90); do
  curl -fsS -m 5 "http://$VIDEO_BIND:$PORT/health" >/dev/null 2>&1 && { READY=1; break; }
  sleep 10
done
[ "$READY" -eq 1 ] || { sudo journalctl -u "$UNIT" -n 40 --no-pager || true; die "the lane did not answer /health within 15 min. The journal above says why."; }
echo "up after ~$(( (i - 1) * 10 )) s; generating a 4 s video at 480P (about 12 min on this box)"
OUT="$(mktemp)"
# Video creation is asynchronous: POST returns an id with a queued status, then the
# content is downloaded once the poll says completed. `|| true`: a transport failure
# made curl exit non-zero inside the assignment, and set -e ended the script before
# the message below (same trap as the image lane).
# The body carries task and target because the lane 400s without each (measured
# 2026-09-25, docs/video-lane.md): a 400 here throws away a 15-minute boot. t2va
# is the smoke of the default fl2va variant; a ref2va install would need a
# conditioned body, which no default install path uses.
CODE=$(curl -sS -o "$OUT" -w '%{http_code}' -m 60 "http://$VIDEO_BIND:$PORT/v1/videos" \
  -H 'Content-Type: application/json' \
  -d '{"model":"MiniMax-H3","prompt":"A cat walks across a sunlit room, dust in the air","seconds":4,"task":"t2va","target":{"short_edge":480,"aspect_ratio":"16:9","duration_seconds":4}}' || true)
if [ "$CODE" != 200 ]; then
  echo "HTTP ${CODE:-000}: $(head -c 300 "$OUT" 2>/dev/null)"
  sudo journalctl -u "$UNIT" -n 40 --no-pager || true
  die "the lane started but did not take the smoke video (HTTP ${CODE:-000}); the journal above says why."
fi
VID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id",""))' "$OUT")"
[ -n "$VID" ] || die "the lane answered 200 with no video id: $(head -c 300 "$OUT")"
echo "   video $VID queued; polling to completed (up to 40 min)"
DONE=0
for i in $(seq 1 240); do
  ST="$(curl -sS -m 20 "http://$VIDEO_BIND:$PORT/v1/videos" -H 'Content-Type: application/json' 2>/dev/null | python3 -c '
import json,sys
try:
    items = json.load(sys.stdin)["data"]
except Exception:
    print("unknown"); raise SystemExit
print(next((v.get("status","unknown") for v in items if v.get("id") == sys.argv[1]), "missing"))' "$VID" 2>/dev/null || echo unknown)"
  case "$ST" in
    completed) DONE=1; break ;;
    failed|error) sudo journalctl -u "$UNIT" -n 40 --no-pager || true
                  die "the smoke video $VID ended in $ST; the journal above says why." ;;
  esac
  sleep 10
done
[ "$DONE" -eq 1 ] || { sudo journalctl -u "$UNIT" -n 40 --no-pager || true
  die "the smoke video $VID did not complete in 40 min (last status: $ST); the journal above says why."; }
curl -fsS -m 300 "http://$VIDEO_BIND:$PORT/v1/videos/$VID/content" -o "$OUT.mp4" \
  || die "the video completed but its content did not download."
SZ="$(stat -c %s "$OUT.mp4" 2>/dev/null || echo 0)"
[ "$SZ" -gt 100000 ] || die "the downloaded video is only $SZ bytes, not a real video."
echo "   $(numfmt --to=iec "$SZ") of MP4; keeping it at $OUT.mp4 is left to you (trap removes only \$OUT)"
# What the lane costs with nothing to do, from its own cgroup: the CPU time systemd
# accounts to it over 10 s, after 5 s for the request's tail to finish. About 0 with the
# idle-loop fix above, about 1 core without it. A note, not a failure: it serves either way.
CG="/sys/fs/cgroup$(systemctl show -p ControlGroup --value "$UNIT" 2>/dev/null)"
if [ -r "$CG/cpu.stat" ]; then
  sleep 5
  U0=$(awk '/^usage_usec/{print $2}' "$CG/cpu.stat"); sleep 10
  U1=$(awk '/^usage_usec/{print $2}' "$CG/cpu.stat")
  IDLE_CORES=$(awk -v a="$U0" -v b="$U1" 'BEGIN{printf "%.2f", (b-a)/1e7}')
  echo "   at rest: $IDLE_CORES CPU cores"
  awk -v c="$IDLE_CORES" 'BEGIN{exit !(c > 0.5)}' \
    && echo "NOTE: the lane holds a CPU core while idle; the idle-loop fix is not in effect (see step 3; a lane started before it went in keeps the old loop until its next start)."
fi
# the trap stops the lane and brings the other lane back, whichever way this ends

step "Done: the video lane is installed and proved it serves on $VIDEO_BIND:$PORT"
echo "  switch to it : Load on MiniMax-H3 in the cockpit's Lanes view, or ./switch-model.sh video"
echo "  back         : Load on any other lane there, or ./switch-model.sh stock"
echo "  generate     : the cockpit's Video view, or the API on :$PORT (loopback, no key)"
