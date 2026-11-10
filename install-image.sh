#!/usr/bin/env bash
# Install the Qwen-Image 2.1 lane: text-to-image, image editing and native RGBA,
# served by SGLang Diffusion on this box. Idempotent, and safe to run alone.
#
#   ./install-image.sh                 first install, or update in place
#   ./install-image.sh --no-smoke      skip the generation at the end
#   ./install-image.sh --uninstall     remove the unit and the venv (weights kept)
#   ./install-image.sh --units-only    the unit alone: no runtime, no weights (install.sh, on demand)
#   ./install-image.sh --prepare       the runtime alone, no sudo: the cockpit's first Load runs it
#
# install.sh runs this on every plain install since v1.20: a box gets every lane. The
# checkpoint takes 34 GB (33.1 measured, rounded up; 30.9 GiB) and the runtime another 12 with its
# build (WEIGHTS_GB and RUNTIME_GB below count them in GiB, as df -BG does: 31, the checkpoint
# rounded up, and 11: the venv, the checkout and the wheel take 8.6 to 8.8 GiB on the reference
# box, measured 2026-10-17, the rest is for the build); when the disk does not have the room, this refuses before
# downloading anything, install.sh says so and goes on, and ./install.sh --no-image
# leaves the lane out of the run that passes it.
#
# WHY IT IS A VENV AND NOT DOCKER. The cookbook is explicit for this model: "This
# integration currently uses the Python/source command; no published Docker image is
# verified." So the runtime is a pinned source checkout: the v0.5.21 release, the first
# that carries Qwen-Image 2.1, with local patches (step 3). The release wheel goes in
# first all the same, because it carries the prebuilt aarch64 native kernels that a source
# tree does not build.
#
# ONE ENGINE AT A TIME. 31 GiB of weights do not fit beside a serving LLM. The unit says
# so with Conflicts=, so starting this stops the LLM lane and starting an LLM lane stops
# this. Nothing here has to be remembered by the operator.
set -euo pipefail
# One spelling of the home, without a trailing slash, as install.sh says why: the cockpit's
# exact-argv sudoers is rendered from it and has to match the cockpit's own calls (2026-10-22).
while [ -n "${HOME:-}" ] && [ "$HOME" != / ] && [ "${HOME%/}" != "$HOME" ]; do HOME="${HOME%/}"; done; export HOME
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=qwen38-image.service
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
  # The two halves of a lane installed on demand: --units-only puts in what needs root (the
  # unit, its start script) and nothing heavy, and --prepare builds the runtime as the user,
  # with no sudo, when the cockpit first loads the lane. The checkpoint comes with that Load's
  # switch, as for any Load.
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
[ "$ACTION" = uninstall ] || [ "$(box_mem_class)" != 64 ] || lane_fits image || die "The image lane (Qwen-Image 2.1) needs a 128 GB DGX Spark, and this box is a 64 GB one."
# A terminal opened before the account joined the docker group: the run goes on with it (lanes.sh).
docker_session bash "$HERE/${BASH_SOURCE[0]##*/}" "$@"
# Read back one slash at a time, as unit_hf_cache reads the cache (lanes.sh): a unit rendered from a
# home with a trailing slash says "//", and its own spelling kept it as it was at every update.
LANE_DIR="${IMAGE_LANE_DIR:-$(installed WorkingDirectory | tr -s /)}"; LANE_DIR="${LANE_DIR:-$HOME/.local/share/qwen38-image}"
VENV="$LANE_DIR/venv"
SRC="$LANE_DIR/sglang"
WHEEL_NOTE="$LANE_DIR/sglang-wheel"   # the wheel release the venv's dependencies come from (step 3)
RUNTIME_NOTE="$LANE_DIR/runtime-changed"   # touched whenever this script changes the runtime the lane runs (step 3)
PORT="${IMAGE_PORT:-$(unit_flag --port)}"; PORT="${PORT:-30020}"
# The checkpoint and its revision, pinned like the rest: the revision this lane was
# measured with on 2026-09-22 (its main then, and on 2026-09-24). It was fetched at main,
# so a push upstream would have changed what a fresh install serves (found in review,
# 2026-09-24). IMAGE_MODEL_REV overrides it; another IMAGE_MODEL is fetched at
# IMAGE_MODEL_REV, or main.
IMAGE_MODEL_PIN="Qwen/Qwen-Image-2.1"
IMAGE_MODEL_PIN_REV="790c92633540aa0cb11d9abf19eb46d861714758"
# The Turbo variant (./switch-model.sh image-turbo): Qwen's eight-step distillation of the
# same model, at the revision measured on the reference box on 2026-10-10. A box that
# serves it keeps it, like any target of the unit.
IMAGE_TURBO_PIN="Qwen/Qwen-Image-2.1-Turbo"
IMAGE_TURBO_PIN_REV="d65dbc9a7e8f6b5479e33dee6030eaab2a906509"
MODEL="${IMAGE_MODEL:-$(kept_flag --model-path)}"; MODEL="${MODEL:-Qwen/Qwen-Image-2.1}"   # = IMAGE_MODEL_PIN
case "$MODEL" in
  "$IMAGE_MODEL_PIN") MODEL_REV="${IMAGE_MODEL_REV:-$IMAGE_MODEL_PIN_REV}" ;;
  "$IMAGE_TURBO_PIN") MODEL_REV="${IMAGE_MODEL_REV:-$IMAGE_TURBO_PIN_REV}" ;;
  *) MODEL_REV="${IMAGE_MODEL_REV:-main}" ;;
esac
# The installed unit's cache, then the one the text lane mounts, then the default: an
# update that ignored the unit downloaded 33 GB again into ~/.cache and rewrote HF_HOME on
# a box whose lane lived on another disk, and a first install beside a text lane on a
# custom cache put the checkpoint on the system disk (found in review, 2026-09-24).
installed_env(){ { grep -m1 -E "^Environment=$1=" "$INSTALLED" 2>/dev/null || true; } | cut -d= -f3-; }
TEXT_UNITS="${TEXT_UNITS:-/etc/systemd/system/qwen38-sglang.service $CONFIG_DIR/launch-flash.sh}"
text_cache(){
  # shellcheck disable=SC2086  # a list of paths
  unit_hf_cache $TEXT_UNITS
}
HF_CACHE="${HF_CACHE:-$(installed_env HF_HOME | tr -s /)}"; HF_CACHE="${HF_CACHE:-$(text_cache)}"   # one slash at a time, as LANE_DIR
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
# The v0.5.21 release (its tag's commit), the first that carries Qwen-Image 2.1, measured
# end to end on the reference box on 2026-10-10. From 2026-09-22 until then the lane ran
# the main commit ddebc52, which no release held yet. SGLANG_DIFFUSION_PIN overrides it
# only for someone deliberately testing another one.
PIN="${SGLANG_DIFFUSION_PIN:-e00930c5489053f26d86b179cee0d087f846acbb}"
# The released wheel that carries the prebuilt aarch64 kernels the source tree reuses, and
# the versions of everything else the runtime imports.
WHEEL="${SGLANG_DIFFUSION_WHEEL:-0.5.21}"
# Loopback, and not from ENGINE_BIND: that variable belongs to the LLM lane, which has a
# key. This one has none (the diffusion parser has no --api-key at all), so a non-loopback
# bind puts an unauthenticated image generator on that interface. IMAGE_BIND overrides it
# for someone who means to, and is told what it costs.
IMAGE_BIND="${IMAGE_BIND:-$(unit_flag --host)}"; IMAGE_BIND="${IMAGE_BIND:-127.0.0.1}"
case "$IMAGE_BIND" in
  127.*|localhost) ;;
  *) echo "WARNING: binding $IMAGE_BIND. This lane has no API key (the diffusion runtime has"
     echo "         no --api-key), so anyone who can reach that address can generate on your GPU."
     if [ "$IMAGE_BIND" = 0.0.0.0 ]; then
       echo "         0.0.0.0 is every interface: the LAN, the tailnet and any added later."
     fi
     echo "         The scheduler's own channel stays on 127.0.0.1 whatever this bind (scheduler-loopback)."
     echo "         torch's rendezvous port, 30005, listens on all the box's addresses too, with no key,"
     echo "         and a bind past loopback leaves it unfiltered (SECURITY.md)."
     echo "         Loopback plus the cockpit is the intended shape." ;;
esac
case "$IMAGE_BIND" in
  *[!0-9.]*|""|*..*) [ "$IMAGE_BIND" = localhost ] || die "IMAGE_BIND takes an IPv4 address, got: $IMAGE_BIND" ;;
esac
# The runtime listens past its bind: torch's TCPStore on port 30005 of every interface, and gloo's
# and NCCL's bootstrap on the box's LAN address, none with a key (2026-10-30). The unit keeps that
# bootstrap on lo, and a loopback bind gets its IP filter (lane_ip_deny, lanes.sh).
IMAGE_IP_DENY="$(lane_ip_deny "$IMAGE_BIND")"; IMAGE_IP_ALLOW="${IMAGE_IP_DENY:+localhost}"
WEIGHTS_GB=31; RUNTIME_GB=11   # the checkpoint; the venv, checkout and wheel (8.6 to 8.8) and the pip build tree

if [ "$ACTION" = uninstall ]; then
  step "Removing the image lane"
  # what the box's links of v1.29 say, read while this lane is still the one at boot (lanes.sh)
  text_lane_migrate
  if [ -f "$INSTALLED" ]; then
    WAS_BOOT=0; systemctl is-enabled --quiet "$UNIT" 2>/dev/null && WAS_BOOT=1
    sudo systemctl disable --now "$UNIT" 2>/dev/null || true
    sudo rm -f "$INSTALLED"; sudo systemctl daemon-reload
    echo "unit removed"
    # When the image lane was the box's lane at boot, the text lane it replaced was
    # disabled by that switch: removing it left no engine at all, the next boot included,
    # and said nothing (found in review, 2026-09-24). The lane before images comes back.
    TEXT_ENABLED=0
    for u in qwen38-sglang.service qwen38-flash.service qwen38-kolibri.service qwen38-nemotron.service qwen38-nemotron-super.service qwen38-flash-local.service qwen38-ornith-122b.service; do
      systemctl is-enabled --quiet "$u" 2>/dev/null && TEXT_ENABLED=1
    done
    if [ "$WAS_BOOT" -eq 1 ] && [ "$TEXT_ENABLED" -eq 0 ]; then
      # The text lane noted (lanes.sh), or the one installed: it read a link of its own, and a
      # lane it named that was removed since left the box with no lane at boot (found in review,
      # 2026-10-14).
      text_lane_give_back image "the image lane"
    fi
  fi
  # Only what this script put there: LANE_DIR can be a directory shared with other work,
  # and removing it whole took the rest with it (found in review, 2026-09-24).
  if [ -d "$LANE_DIR" ]; then
    rm -rf "$VENV" "$SRC" "$WHEEL_NOTE"
    echo "runtime removed: $VENV and $SRC"
    rmdir "$LANE_DIR" 2>/dev/null || echo "kept $LANE_DIR: it holds files the image lane did not put there"
  fi
  # ./switch-model.sh's note of a checkpoint to come back to from the Turbo goes with the
  # lane, read first: the checkpoint it names is the lane's too
  BEFORE_TURBO="$(cat "$CONFIG_DIR/image-model-before-turbo" 2>/dev/null || true)"
  rm -f "$CONFIG_DIR/image-model-before-turbo"
  # every checkpoint of the lane the cache holds: the unit's, the other one a switch fetched,
  # and the one the note named
  KEPT=()
  for R in "$MODEL" "$IMAGE_MODEL_PIN" "$IMAGE_TURBO_PIN" ${BEFORE_TURBO:+"$BEFORE_TURBO"}; do
    D="$HF_CACHE/hub/models--${R//\//--}"
    [ -d "$D" ] && [[ " ${KEPT[*]} " != *" $D "* ]] && KEPT+=("$D")
  done
  if [ "${#KEPT[@]}" -gt 0 ]; then
    echo "the lane's checkpoints are left in $HF_CACHE (about 34 GB each); delete them yourself if you want the space:"
    for D in "${KEPT[@]}"; do echo "  rm -rf $D"; done
  fi
  exit 0
fi

# Step 5, the one part that needs root: the unit and its start script. A function, since a lane
# installed on demand (--units-only) takes it straight after the preflight.
install_service(){
  step "5/6 Service"
  # The unit runs engine-preflight.sh before every start: run on its own, this installer
  # must put it in place, or the lane would never start.
  mkdir -p "$CONFIG_DIR"
  cmp -s "$HERE/engine-preflight.sh" "$CONFIG_DIR/engine-preflight.sh" \
    || install -m 755 "$HERE/engine-preflight.sh" "$CONFIG_DIR/engine-preflight.sh"
  RENDER="$(mktemp)"; trap 'rm -f "$RENDER"' EXIT
  sed -e "s|__USER__|$(id -un)|g" -e "s|__GROUP__|$(id -gn "$(id -un)")|g" \
      -e "s|__IMAGE_LANE_DIR__|$LANE_DIR|g" -e "s|__IMAGE_VENV__|$VENV|g" \
      -e "s|__IMAGE_MODEL__|$MODEL|g" -e "s|__IMAGE_PORT__|$PORT|g" \
      -e "s|__IMAGE_BIND__|$IMAGE_BIND|g" -e "s|__IMAGE_IP_DENY__|$IMAGE_IP_DENY|g" -e "s|__IMAGE_IP_ALLOW__|$IMAGE_IP_ALLOW|g" -e "s|__HF_CACHE__|$HF_CACHE|g" \
      -e "s|__HOME__|$HOME|g" \
      "$HERE/$UNIT.template" > "$RENDER"
  grep -q '__[A-Z][A-Z0-9_]*__' "$RENDER" && die "the unit template still holds an unsubstituted placeholder: $(grep -o '__[A-Z][A-Z0-9_]*__' "$RENDER" | sort -u | tr '\n' ' ')"
  # 0644 like every other unit, and set explicitly: mktemp creates 0600 and cp keeps the
  # mode, which left this unit readable by root alone. systemd did not mind; everything
  # else that reads the unit did, and failed quietly. switch-model.sh could not find the
  # runtime, and the cockpit's reads of the port, the bind and the model all fell back to
  # their defaults, which happened to be right on the reference box and would not have been
  # on one installed with IMAGE_PORT= or IMAGE_BIND=.
  # A unit installed for another checkpoint than it named (IMAGE_MODEL=) is not the one
  # ./switch-model.sh image-turbo left: the note of the way back from the Turbo goes, or a
  # later `./switch-model.sh image` would bring back a checkpoint from before this install.
  INSTALLED_MODEL="$(unit_flag --model-path)"
  if [ -n "$INSTALLED_MODEL" ] && [ "$INSTALLED_MODEL" != "$MODEL" ]; then
    rm -f "$CONFIG_DIR/image-model-before-turbo"
  fi
  UNIT_PUT="$(unit_put "$RENDER" "$INSTALLED")"
  case "$UNIT_PUT" in
    same) echo "unit unchanged"
          sudo chmod 644 "$INSTALLED" ;;   # a unit installed before this fix is still 0600
    deps) sudo systemctl daemon-reload
          echo "unit updated: only the lines naming the other lanes changed, which the lane does not read" ;;
    *)    sudo systemctl daemon-reload
          echo "unit installed at $INSTALLED" ;;
  esac
  # A lane that serves runs the unit, the runtime and the start scripts it started with, whether this run
  # or an earlier update changed them (said nothing for the image and video lanes until found in review,
  # 2026-10-14). The runtime's dates: runtime-changed, touched at each change since v1.30, and the notes
  # of the wheel, which said the changes before.
  LANE_FILES=("$INSTALLED" "$RUNTIME_NOTE" "$WHEEL_NOTE" "$CONFIG_DIR/engine-preflight.sh")
  # A serving image lane restarts to run them (Hasan, 2026-10-30, v1.40.6): about 70 s, and an image being made
  # then is lost. Its own sockets' filter came in with a unit, which a lane that kept serving never read. The
  # video lane, 12 to 15 min to boot, keeps its NOTE.
  if [ "$SMOKE" -eq 0 ] && lane_serves_older "$UNIT" "${LANE_FILES[@]}"; then
    echo "the image lane serves older files than the ones installed now (its unit, its runtime or its start scripts): restarting it (about 70 s)"
    sudo systemctl restart "$UNIT" \
      || echo "NOTE: the image lane did not restart; it runs the older files until: sudo systemctl restart $UNIT"
  fi
  # Installed, not switched to: the lane this box serves stays the lane it serves. The image
  # lane becomes the boot lane the same way the other two do, by a switch, which is also
  # what makes it come back after a reboot.
  echo "installed as a lane of its own. Switch to it like any lane:"
  echo "  the cockpit: Load on Qwen-Image 2.1 in the Lanes view (it switches, stops the serving lane, starts this one)"
  echo "  a terminal : ./switch-model.sh image, then the two commands it prints"
}

step "1/6 Preflight"
case "$(uname -m)" in aarch64|arm64) : ;; *) die "this lane is verified on the DGX Spark's ARM64 GB10 only (this box is $(uname -m))." ;; esac
command -v nvidia-smi >/dev/null || die "nvidia-smi not found: this needs the NVIDIA driver."
command -v git >/dev/null || die "git is required (stock on DGX OS)."
# Run from a terminal, this installs what the lane needs and the box lacks (lanes.sh
# host_packages). The checks below, as before, say what could not be put in: the cockpit's
# runs have no terminal for sudo's password, and install.sh puts these in for them.
HOST_PKGS=()
python3 -c 'import venv, ensurepip' 2>/dev/null || HOST_PKGS+=(python3-venv)
[ -f "$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])' 2>/dev/null)/Python.h" ] || HOST_PKGS+=(python3-dev)
[ "${#HOST_PKGS[@]}" -eq 0 ] || host_packages "the image lane" "${HOST_PKGS[@]}" || true
# ensurepip, not just venv: venv is in python3's standard library either way, and it is
# python3-venv that ships ensurepip, which gives a venv its pip. Checked on venv alone,
# a box without the package made a venv with no pip (found in review, 2026-09-24).
python3 -c 'import venv, ensurepip' 2>/dev/null || die "python3-venv is missing (a venv made without it has no pip). Fix: sudo apt-get install -y python3-venv"
# python3-dev too: sglang[diffusion] asks for xatlas, which PyPI ships for no aarch64
# Python, so pip builds it here, against Python's headers. DGX OS installs python3-pip
# without its recommends, python3-dev among them, and without the headers that build stops
# on a missing /usr/include/python3.12 (a user's box, 2026-10-05; proved in a clean Ubuntu
# 24.04 container, the reference box having had them from an install by hand).
[ -f "$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')/Python.h" ] || die "python3-dev is missing (xatlas, which this lane needs, is built here against Python's headers). Fix: sudo apt-get install -y python3-dev"
[ -s "$CONFIG_DIR/api-key" ] || die "no API key at $CONFIG_DIR/api-key. Run ./install.sh first: the cockpit is the authenticated door in front of this lane, and it reads that file."
# Measured where each part lands, not under $HOME (install.sh had fixed the same bug):
# the checkpoint goes to HF_CACHE, the runtime and its build to the lane's folder. What the
# cache already holds comes off the checkpoint's share, by bytes: the folder alone said
# nothing, since huggingface_hub creates it before the first byte (found in review,
# 2026-09-24).
# existing: lanes.sh's (the nearest folder of a path that exists)
free_gb(){ { df -BG --output=avail "$(existing "$1")" 2>/dev/null || true; } | tail -1 | tr -dc '0-9'; }
HAVE_B="$({ du -s --apparent-size -B1 "$HF_CACHE/hub/models--${MODEL//\//--}/blobs" 2>/dev/null || true; } | cut -f1)"
# what is left, in whole GB (a complete 30.86 GiB checkpoint is 0 left, not 1)
WEIGHTS_NEED=$(( (WEIGHTS_GB * 1073741824 - ${HAVE_B:-0}) / 1073741824 )); [ "$WEIGHTS_NEED" -ge 0 ] || WEIGHTS_NEED=0
# --prepare builds the runtime only: the checkpoint's room is the switch's to check, as it
# fetches it. --units-only downloads nothing.
[ "$MODE" = prepare ] && WEIGHTS_NEED=0
FREE_W="$(free_gb "$HF_CACHE")"; FREE_R="$(free_gb "$LANE_DIR")"
if [ "$MODE" = units ]; then
  :
elif [ "$(stat -L -c %d "$(existing "$HF_CACHE")")" = "$(stat -L -c %d "$(existing "$LANE_DIR")")" ]; then
  WANT=$((WEIGHTS_NEED + RUNTIME_GB))
  [ "${FREE_W:-0}" -ge "$WANT" ] \
    || die "${FREE_W:-?} GB free on the disk of $HF_CACHE and $LANE_DIR, this needs about $WANT GB ($WEIGHTS_NEED for the checkpoint, $RUNTIME_GB for the runtime and its build). Free some space, or point HF_CACHE or IMAGE_LANE_DIR at a bigger disk."
else
  [ "${FREE_W:-0}" -ge "$WEIGHTS_NEED" ] \
    || die "${FREE_W:-?} GB free under HF_CACHE=$HF_CACHE, the checkpoint needs about $WEIGHTS_NEED GB more. Free some space, or point HF_CACHE at a bigger disk."
  [ "${FREE_R:-0}" -ge "$RUNTIME_GB" ] \
    || die "${FREE_R:-?} GB free under $LANE_DIR, the runtime and its build need about $RUNTIME_GB GB. Free some space, or point IMAGE_LANE_DIR at a bigger disk."
fi
if [ "$MODE" = units ]; then
  echo "OK: $(uname -m), key present; the runtime and the checkpoint come with the lane's first Load"
  install_service
  step "Done: the image lane is installed; its first Load prepares it"
  echo "  the cockpit: Load on Qwen-Image 2.1 in the Lanes view builds its runtime (about 12 GB),"
  echo "               fetches its checkpoint (about 34 GB), then starts it"
  echo "  a terminal : ./install-image.sh --prepare, then ./switch-model.sh image and the commands it prints"
  exit 0
fi
echo "OK: $(uname -m), key present, checkpoint $([ "$MODE" = prepare ] && echo "left to the Load's switch" || { [ "$WEIGHTS_NEED" -eq 0 ] && echo 'already cached'; } || echo "$WEIGHTS_NEED GB to download") into $HF_CACHE"

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
# that moves to a new release takes that release's wheel too: a venv that only checked
# whether sglang imported kept the previous release's dependencies under the new source
# (cache-dit 1.3.0 under v0.5.21, which asks for 1.5.1). The version installed is written
# down once the whole runtime is in (below, after the registry check); a venv from before
# that note, or one a run left half built, has none and takes the wheel again.
if ! "$VENV/bin/python" -c 'import sglang' 2>/dev/null || [ "$(cat "$WHEEL_NOTE" 2>/dev/null)" != "$WHEEL" ]; then
  echo "installing sglang[diffusion]==$WHEEL (~10 min on a new venv: torch and the kernels are large; an existing one only takes what changed)"
  "$VENV/bin/pip" install --quiet --pre "sglang[diffusion]==$WHEEL" \
    || die "the released wheel would not install. Re-run: pip resumes. If it keeps failing, SGLANG_DIFFUSION_WHEEL=<version> picks another."
  touch "$RUNTIME_NOTE"
fi
if [ ! -d "$SRC/.git" ]; then
  git clone --quiet https://github.com/sgl-project/sglang "$SRC" || die "could not clone SGLang."
fi
# Local changes to the pinned source, each measured on the reference box (docs/image-lane.md,
# "The local changes to the pinned source"). The first two are not about images:
#   scheduler-idle-poll: the diffusion scheduler's loop never waits. recv_reqs() polls its
#     socket without blocking and nothing else in the loop sleeps, so a lane with nothing
#     to do held one CPU core at 100% (1.047 cores, against 0.029 for the 27B lane, which
#     parks itself with --sleep-on-idle; this runtime has no such flag). The patch waits on
#     the request socket for up to a second, as the LLM scheduler's own IdleSleeper does.
#     The same seed gives the same pixels, byte for byte, with and without it.
#   http-graceful-timeout: on shutdown its HTTP server waited for every open connection,
#     and a generation holds one for as long as it runs. A Stop during a 2048x2048 request
#     sat 60 s in "stopping" until systemd killed the lane and marked the unit failed
#     (2026-09-23). Now 5 s, then the requests in flight are cancelled and it stops clean.
#   scheduler-loopback: the scheduler takes its requests on a ZMQ socket that unpickles what it
#     is sent, and upstream derives that socket's address from --host: a lane bound to an
#     address other machines reach took requests on that channel from them too, with no key
#     (found in a security review, 2026-10-24). It stays on 127.0.0.1 now, whatever --host is:
#     here the HTTP server and the scheduler always run on the same machine. Upstream's main
#     pins only 0.0.0.0 and IPv6 to loopback and keeps an explicit address.
# The other three are sgl-project/sglang#43391, merged upstream on 2026-10-10 and in SGLang
# v0.5.22 (2026-10-16), the release after this pin, one file each: the Turbo checkpoint's eight-step sigma grid, which its
# model_index.json carries, reaches the sampler. Without them the Turbo weights load and
# sample on a uniform schedule from num_inference_steps, which is not how it was trained;
# with them the base checkpoint, which has no grid, samples exactly as before.
# One line: CI reads this list with a one-line pattern.
PATCHES=(scheduler-idle-poll http-graceful-timeout scheduler-loopback qwen-image21-turbo-sigma-config qwen-image21-turbo-sigma-pipeline qwen-image21-turbo-sigma-stage)
declare -A PATCH_FIXES=(
  [scheduler-idle-poll]="an idle lane no longer holds a CPU core"
  [http-graceful-timeout]="a stop during a generation takes 5 s instead of timing out"
  [scheduler-loopback]="the scheduler's channel stays on 127.0.0.1 whatever --host"
  [qwen-image21-turbo-sigma-config]="the Turbo's sigma grid has a place in the pipeline config"
  [qwen-image21-turbo-sigma-pipeline]="the Turbo's sigma grid is read from its model_index.json"
  [qwen-image21-turbo-sigma-stage]="a Turbo request samples on that grid, eight steps"
)
CURRENT="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || true)"
if [ "$CURRENT" != "$PIN" ]; then
  # they are the only local edits in this tree: take them off, or the checkout trips on them
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
  echo "source checked out at ${PIN:0:12}"; touch "$RUNTIME_NOTE"
else
  echo "source already at ${PIN:0:12}"
fi
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
# again pulls a transformers that breaks the encoder this model needs.
# SGLANG_BUILD_RUST_EXTS=none: the pinned source declares five Rust extensions (gRPC, the
# Rust server, the radix tree, two multimodal processors), all of the LLM runtime; none
# is imported by the diffusion server. Building them needs cargo, which DGX OS does not
# ship: a box without a Rust toolchain failed here ("cargo is required ...", reference
# box in a clean login, 2026-09-23), and one with it spent the build on unused code.
if ! "$VENV/bin/python" -c 'import sglang, pathlib, sys; sys.exit(0 if str(pathlib.Path(sglang.__file__).parent).startswith("'"$SRC"'") else 1)' 2>/dev/null; then
  echo "overlaying the pinned source (editable, no dependency resolution, no Rust extensions)"
  SGLANG_BUILD_RUST_EXTS=none "$VENV/bin/pip" install --quiet --no-deps -e "$SRC/python" \
    || die "the editable overlay failed. The venv still holds the released wheel; re-run to retry."
fi
# Asked of the registry itself, not of its source text: v0.5.21 registers the model from its
# pipeline config module, and the text check that held at ddebc52 failed there although the
# runtime served it (2026-10-10). The exact id must map to Qwen-Image 2.1's own config; a
# fuzzy match would also take an unknown "Qwen/Qwen-Image-9.9" for an older Qwen-Image.
"$VENV/bin/python" - <<'PY' || die "the runtime does not know Qwen-Image 2.1. The pin may be wrong for this checkout."
import pathlib, sglang
from sglang.multimodal_gen import registry
from sglang.multimodal_gen.configs.pipeline_configs.qwen_image21 import QwenImage21PipelineConfig
getattr(registry, "_ensure_registry_initialized", lambda: None)()
info = registry._CONFIG_REGISTRY.get(registry._MODEL_HF_PATH_TO_NAME.get("Qwen/Qwen-Image-2.1", ""))
if info is None or info.pipeline_config_cls is not QwenImage21PipelineConfig:
    raise SystemExit("Qwen/Qwen-Image-2.1 does not map to Qwen-Image 2.1's own config in the model registry")
src = pathlib.Path(sglang.__file__).parent
print(f"   runtime: {src}")
PY
# A Turbo the runtime cannot read the grid of would load, answer 200 and sample on a
# uniform schedule it was not trained for: refused here rather than served degraded. The
# three parts are checked one by one (image-sglang/turbo-grid-check.py).
if [ "$MODEL" = "$IMAGE_TURBO_PIN" ]; then
  TURBO_WHY="$("$VENV/bin/python" "$HERE/image-sglang/turbo-grid-check.py" 2>&1)" \
    || die "this lane serves $IMAGE_TURBO_PIN, and the runtime at ${PIN:0:12} cannot sample on its eight-step grid: ${TURBO_WHY:-no reason given} (a qwen-image21-turbo-sigma patch above did not apply?). Serve the base with ./switch-model.sh image, or pin a commit that carries sgl-project/sglang#43391."
  echo "   the runtime samples on the Turbo's eight-step sigma grid"
fi
# The runtime is whole: its note says so, and it is what the cockpit and install.sh read as
# this lane prepared. Written last, so a run that stops on the way (a clone that fails, a
# patch, the overlay, the registry) leaves a lane its next Load prepares again, not one it
# takes for built and serves on the bare wheel (found in review, 2026-10-15). And only when it
# changes: its date is one of the runtime's (LANE_FILES, the cockpit's stale-runtime check), and
# rewritten at every run it told a serving lane it ran older files, at each update (found in
# review of that fix, the same day).
[ "$(cat "$WHEEL_NOTE" 2>/dev/null)" = "$WHEEL" ] || printf '%s\n' "$WHEEL" > "$WHEEL_NOTE"

if [ "$MODE" = prepare ]; then
  step "Done: the image lane's runtime is ready; its Load fetches the checkpoint and starts it"
  exit 0
fi

step "4/6 Checkpoint ($MODEL at ${MODEL_REV:0:12}, ~34 GB, one-time, resumable)"
# The same two lessons the LLM lane learned the hard way: the Hub's Xet backend stalls
# silently on this box (0-8 MB/s against 89 on the classic CDN), and an unauthenticated
# pull gets throttled, so HF_TOKEN is passed through when it is set.
# ckpt-fetch.py is the one fetch of every lane's checkpoint (resumed attempts, the room, the
# byte count). The unit serves the repo by name with HF_HUB_OFFLINE=1, which resolves
# refs/main, and a download by commit writes no ref: --refs-main pin points main at the
# pinned commit, which is what is served.
HF_HOME="$HF_CACHE" HF_HUB_DOWNLOAD_TIMEOUT=30 HF_HUB_DISABLE_XET=1 \
  "$VENV/bin/python" "$HERE/ckpt-fetch.py" "$MODEL" "$MODEL_REV" --refs-main pin \
  || die "checkpoint download failed. Causes: no internet, HuggingFace throttling an unauthenticated download (set HF_TOKEN=<your token>), a pinned revision removed upstream (IMAGE_MODEL_REV=main serves the current one), or a permission error in the cache. Re-running resumes."

install_service

if [ "$SMOKE" -eq 0 ]; then step "Done (smoke test skipped)"; exit 0; fi

step "6/6 Proving it serves (one image, then the box goes back to the lane it was serving)"
# The restore must mirror this unit's Conflicts= set: starting the image lane stops every
# engine lane, so whatever was serving (text, flash, the video lane or a vLLM lane) has
# to be started back. Checking only the two text lanes left a box that
# was serving on video with no engine at all after the smoke test (found in review,
# 2026-09-26).
WAS_LLM=""
for u in qwen38-sglang.service qwen38-flash.service qwen38-video.service qwen38-kolibri.service qwen38-nemotron.service qwen38-nemotron-super.service qwen38-flash-local.service qwen38-ornith-122b.service; do
  systemctl is-active --quiet "$u" 2>/dev/null && WAS_LLM="$u"
done
# If this lane was already serving, the test leaves it serving: stopping it on the way out
# would turn "re-run the installer" into "take the image lane down".
WAS_IMAGE=0
systemctl is-active --quiet "$UNIT" 2>/dev/null && WAS_IMAGE=1
[ -n "$WAS_LLM" ] && echo "note: $WAS_LLM is serving and will be stopped for this test, then started again."
# From here on the serving lane is DOWN, so putting it back cannot live on the happy path:
# every die() below would leave the box serving nothing, with the image unit holding
# 31 GiB, until somebody noticed. The trap runs on success and on every failure alike.
restore_text_lane() {
  [ "$WAS_IMAGE" -eq 1 ] || sudo systemctl stop "$UNIT" 2>/dev/null || true
  if [ -n "$WAS_LLM" ]; then
    echo "starting $WAS_LLM again"
    sudo systemctl start "$WAS_LLM" || echo "WARNING: could not restart $WAS_LLM. Do it by hand: sudo systemctl start $WAS_LLM"
  fi
  rm -f "$RENDER" "${OUT:-}"
}
trap restore_text_lane EXIT
# A lane that runs older files than the ones installed now restarts for the proof: the engine that
# runs proved nothing about what this run installed (found in review, 2026-10-14).
if [ "$WAS_IMAGE" -eq 1 ] && lane_serves_older "$UNIT" "${LANE_FILES[@]}"; then
  echo "The image lane runs older files than the ones installed now (its unit, its runtime or its start scripts): it restarts to prove these"
  sudo systemctl restart "$UNIT"
else
  sudo systemctl start "$UNIT"
fi
READY=0
for i in $(seq 1 90); do
  curl -fsS -m 5 "http://$IMAGE_BIND:$PORT/health" >/dev/null 2>&1 && { READY=1; break; }
  sleep 10
done
[ "$READY" -eq 1 ] || { sudo journalctl -u "$UNIT" -n 40 --no-pager || true; die "the lane did not answer /health within 15 min. The journal above says why."; }
echo "up after ~$(( (i - 1) * 10 )) s; generating a 512x512 image"   # the first probe comes before any wait
OUT="$(mktemp)"
# `|| true`: a transport failure (the lane dying mid-request, the 300 s passing) made curl
# exit non-zero inside the assignment, and set -e ended the script before the message and
# the journal below (found in review, 2026-09-24); curl still writes 000 for the code.
CODE=$(curl -sS -o "$OUT" -w '%{http_code}' -m 300 "http://$IMAGE_BIND:$PORT/v1/images/generations" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"A capybara reading a book by candlelight","size":"512x512","num_inference_steps":20,"output_format":"png","response_format":"b64_json","generator_device":"cpu","seed":42}' || true)
if [ "$CODE" != 200 ]; then
  echo "HTTP ${CODE:-000}: $(head -c 300 "$OUT" 2>/dev/null)"
  sudo journalctl -u "$UNIT" -n 40 --no-pager || true
  die "the lane started but did not make the smoke image (HTTP ${CODE:-000}); the journal above says why."
fi
"$VENV/bin/python" - "$OUT" <<'PY' || die "the lane answered 200 with something that is not a 512x512 image."
import base64, json, struct, sys
d = json.load(open(sys.argv[1]))["data"][0]
raw = base64.b64decode(d["b64_json"])
if raw[:8] != b"\x89PNG\r\n\x1a\n":
    raise SystemExit("not a PNG")
w, h = struct.unpack(">II", raw[16:24])
if (w, h) != (512, 512):
    raise SystemExit(f"got {w}x{h}")
print(f"   {w}x{h} {'RGBA' if raw[25] == 6 else raw[25]}, {len(raw)/1e6:.1f} MB")
PY
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
    && echo "NOTE: the lane holds a CPU core while idle; the idle-loop fix is not in effect (see step 3)."
fi
# the trap stops the lane and brings the text lane back, whichever way this ends

step "Done: the image lane is installed and proved it serves on $IMAGE_BIND:$PORT"
echo "  switch to it : Load on Qwen-Image 2.1 (or its Turbo) in the cockpit's Lanes view, or ./switch-model.sh image (image-turbo)"
echo "  back to text : Load on a text lane there, or ./switch-model.sh stock"
echo "  generate     : the cockpit's Image view, or the API on :$PORT (loopback, no key)"
