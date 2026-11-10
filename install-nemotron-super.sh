#!/usr/bin/env bash
# Install the Nemotron Super lane: NVIDIA's Nemotron 3.5 Super VL, a mixture of experts of 120B
# parameters (12B active per token) in NVFP4, with reasoning, tool calling and image and video input
# over a window of 1,048,576 tokens, served by vLLM on the text lanes' port behind the proxy and its
# key. Idempotent, and safe to run alone.
#
#   ./install-nemotron-super.sh                 first install, or update in place
#   ./install-nemotron-super.sh --no-smoke      skip the proof at the end
#   ./install-nemotron-super.sh --uninstall     remove the unit (weights kept, and the image while
#                                               the Nemotron Bolt lane uses it)
#   ./install-nemotron-super.sh --units-only    the unit alone: no image, no weights (install.sh, on demand)
#   ./install-nemotron-super.sh --prepare       its image, built with no sudo: the cockpit's first Load
#                                               runs it (./prepare-lane.sh nemotron-super)
#
# install.sh puts this lane's unit in on every plain install, like the other lanes, and its first
# Load from the cockpit builds its image and fetches its weights; ./install.sh --no-nemotron-super
# leaves it out of the run that passes it. It takes 77 GB of weights (the model and its
# drafter) and the Nemotron Bolt lane's image, 25 GB, 35 at its build's peak, which the two lanes
# share: when the disk does not have the room, this refuses before downloading anything.
#
# WHY THIS IMAGE. NVIDIA's model card serves Super VL on a DGX Spark with vLLM nightly 3f1bcfd8,
# one request at a time, at a memory share of 0.75, over its whole window. With this box's 16 images
# and 2 videos per conversation, that image left 9.61 GiB to the KV cache at that share, short of
# the 10.04 GiB the full window needs, and the tuning of its MoE kernels took the box under 8 GiB of
# free memory (reference box, 2026-10-16). The Nemotron Bolt lane's image (vLLM nightly 77154ca6
# with b12x, install-nemotron.sh) serves the same checkpoint, right on every answer measured, with a
# cache of 1,275,937 tokens at the same share: the two lanes run it, built once by either lane's
# first Load, from its pins in install-nemotron.sh.
#
# ONE ENGINE AT A TIME. The unit says so with Conflicts=: starting it stops whichever lane
# serves, and starting another lane stops it.
set -euo pipefail
# One spelling of the home, without a trailing slash, as install.sh says why: the cockpit's
# exact-argv sudoers is rendered from it and has to match the cockpit's own calls (2026-10-22).
while [ -n "${HOME:-}" ] && [ "$HOME" != / ] && [ "${HOME%/}" != "$HOME" ]; do HOME="${HOME%/}"; done; export HOME
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=qwen38-nemotron-super.service
# where systemd reads the units, the one variable lanes.sh and vllm-lane.sh read the other units by
INSTALLED="${LANES_UNIT_DIR:-/etc/systemd/system}/$UNIT"
CONTAINER=qwen38-nemotron-super
die(){ printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
step(){ printf '\n\033[1;36m── %s\033[0m\n' "$*"; }

# Under sudo every path below moves to /root: the cache, the config and the key the unit
# reads. install.sh refuses the same way, for the same reason.
if [ "$(id -u)" = "0" ] && [ "${ALLOW_ROOT:-0}" != "1" ]; then
  die "run this as the user who will use the box, not as root (everything it installs is addressed from \$HOME)."
fi

SMOKE=1; ACTION=install; MODE=full
for a in "$@"; do case "$a" in
  --no-smoke) SMOKE=0 ;;
  --uninstall) ACTION=uninstall ;;
  # The two halves of a lane installed on demand, as for the other lanes: --units-only puts in
  # what needs root (the unit, its start scripts) and nothing heavy, and --prepare builds the
  # lane's image as the user, with no sudo, when the cockpit first loads the lane. The checkpoints
  # come with that Load's switch, as for any Load.
  --units-only) [ "$MODE" = full ] || die "--units-only and --prepare are the two halves of an install: one at a time"; MODE=units ;;
  --prepare) [ "$MODE" = full ] || die "--units-only and --prepare are the two halves of an install: one at a time"; MODE=prepare ;;
  -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1' "$0"; exit 0 ;;
  *) die "unknown option: $a" ;;
esac; done

CONFIG_DIR="$HOME/.config/qwen38"
# the text lane the box comes back to, one record shared with switch-model.sh (lanes.sh)
# shellcheck source=lanes.sh
. "$HERE/lanes.sh"
# A 64 GB DGX Spark cannot hold this lane (boxmem.py): refused there before anything is fetched,
# and --uninstall still takes away what an older release put there (found in review, 2026-10-23).
[ "$ACTION" = uninstall ] || [ "$(box_mem_class)" != 64 ] || lane_fits nemotron-super || die "The Nemotron 3.5 Super VL lane needs a 128 GB DGX Spark, and this box is a 64 GB one."
# A terminal opened before the account joined the docker group: the run goes on with it (lanes.sh).
docker_session bash "$HERE/${BASH_SOURCE[0]##*/}" "$@"
# what the three vLLM lanes' installers share: the engine's cache and port, the uninstall, the
# service, the preflight and the room, the fetch, the proof
# shellcheck source=vllm-lane.sh
. "$HERE/vllm-lane.sh"
VLANE=nemotron-super; VLANE_SHORT="Super VL"

# The pins, at column 0 like install.sh's: the model at its revision of 2026-10-15 21:36 UTC, 8a115b9d,
# which holds every file of the revision the reference box measured (82d93699, 2026-10-15 and 16) as
# the same blob but its model card (the revisions since edit the card only, checked on 2026-10-16); the
# drafter at the revision measured; the memory share and the download in GB, the requests at a time.
# switch-model.sh reads them here.
NEMOTRON_SUPER_REPO="nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4"
NEMOTRON_SUPER_REV="8a115b9d966adce8c059a7bcf47f49a1e57957d8"
NEMOTRON_SUPER_DRAFT_REPO="nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4-LiLiCorr"
NEMOTRON_SUPER_DRAFT_REV="7c7221aeb4cb3139307aa457f7dce11a8fb6ceb2"
NEMOTRON_SUPER_GMU="0.75"
NEMOTRON_SUPER_GB="77"
NEMOTRON_SUPER_SEQS="4"
# The image is the Nemotron Bolt lane's, named from its pins in install-nemotron.sh, the one place
# they are declared, as that installer names it (a test holds the two names equal).
eval "$(grep -E '^NEMOTRON_(BASE_IMAGE|VLLM_LABEL|B12X_VERSION)=' "$HERE/install-nemotron.sh" || true)"
for _v in NEMOTRON_BASE_IMAGE NEMOTRON_VLLM_LABEL NEMOTRON_B12X_VERSION; do
  eval "[ -n \"\${$_v:-}\" ]" || die "install-nemotron.sh no longer defines $_v (repo layout changed?)"
done
unset _v
LOCAL_IMAGE="qwen38-nemotron:vllm${NEMOTRON_VLLM_LABEL#nightly-}-b12x${NEMOTRON_B12X_VERSION}-${NEMOTRON_BASE_IMAGE##*:}"
LOCAL_IMAGE="${LOCAL_IMAGE:0:52}"

# One checkpoint, Super VL with its LiLiCorr drafter. Its share of the GPU memory, 0.75 (the card's),
# holds a KV cache of 1,166,215 to 1,382,265 tokens with four requests at a time over two boots
# (1,275,937 with one, measured 2026-10-16): the whole window 1.11 to 1.32 times over, which the four
# share, and 9.1 GiB of MemAvailable left to the box at its least (the end of the installed lane's
# first boot, a browser open on the cockpit; 9.2 in service, 9.3 during a million-token prefill).
# Four at a time make 81.7 tok/s in all against 45.6 for one, and one alone goes 1 to 4 % slower
# than with one at a time. The drafter (the card's choice for a DGX Spark) has the model's tokens
# accepted 4.4 at a time on real work.
MODEL="$NEMOTRON_SUPER_REPO"; MODEL_REV="$NEMOTRON_SUPER_REV"
DRAFT="$NEMOTRON_SUPER_DRAFT_REPO"; DRAFT_REV="$NEMOTRON_SUPER_DRAFT_REV"
GMU="$NEMOTRON_SUPER_GMU"; WEIGHTS_GB="$NEMOTRON_SUPER_GB"; SEQS="$NEMOTRON_SUPER_SEQS"
# The image's build at its peak, as install-nemotron.sh counts it (its IMAGE_GB, read there).
IMAGE_GB="$({ grep -m1 -oE '^IMAGE_GB=[0-9]+' "$HERE/install-nemotron.sh" || true; } | cut -d= -f2)"
case "$IMAGE_GB" in ''|*[!0-9]*) die "install-nemotron.sh no longer defines IMAGE_GB (repo layout changed?)" ;; esac

# The text lanes' cache and port: Super VL is one of them for the proxy (vllm-lane.sh).
vlane_engine_env "${NEMOTRON_SUPER_PORT:-}"

if [ "$ACTION" = uninstall ]; then
  step "Removing the Nemotron 3.5 Super VL lane"
  vlane_uninstall_unit
  vlane_uninstall_clients
  # The image is the Nemotron Bolt lane's too: a build stays while an installed unit names it
  # (lanes.sh).
  vlane_uninstall_runtime qwen38-nemotron "$NEMOTRON_BASE_IMAGE" "$NEMOTRON_SUPER_REPO" "$NEMOTRON_SUPER_DRAFT_REPO"
  exit 0
fi

# Step 4, the one part that needs root: the unit and its start scripts. A function, since a
# lane installed on demand (--units-only) takes it straight after the preflight.
install_service(){
  step "4/5 Service"
  # vLLM's compile cache and FlashInfer's kernels built at their first use
  vlane_service_files "$CONFIG_DIR/nemotron-super-cache/vllm" "$CONFIG_DIR/nemotron-super-cache/flashinfer"
  RENDER="$(mktemp)"; trap 'rm -f "$RENDER"' EXIT
  sed -e "s|__USER__|$(id -un)|g" -e "s|__GROUP__|$(id -gn "$(id -un)")|g" -e "s|__HOME__|$HOME|g" \
      -e "s|__IMAGE__|$LOCAL_IMAGE|g" -e "s|__MODEL__|$MODEL|g" \
      -e "s|__REV__|$MODEL_REV|g" -e "s|__DRAFT__|$DRAFT|g" -e "s|__DREV__|$DRAFT_REV|g" \
      -e "s|__GMU__|$GMU|g" -e "s|__SEQS__|$SEQS|g" -e "s|__HF_CACHE__|$HF_CACHE|g" \
      -e "s|__ENGINE_BIND__|$ENGINE_BIND|g" -e "s|__PORT__|$PORT|g" \
      "$HERE/$UNIT.template" > "$RENDER"
  vlane_put_unit "$RENDER"
  # Builds of the image from older pins, once the unit names this one, and none the Nemotron
  # lane's unit still names (lanes.sh): a box running Super VL alone kept every older build.
  lane_prune_images qwen38-nemotron "$LOCAL_IMAGE"
  # Installed, not switched to: the lane this box serves stays the lane it serves.
  echo "installed as a lane of its own. Switch to it like any lane:"
  echo "  the cockpit: Load on Nemotron 3.5 Super VL in the Lanes view"
  echo "  a terminal : ./switch-model.sh nemotron-super, then the commands it prints"
}

step "1/5 Preflight (Super VL: $MODEL @ ${MODEL_REV:0:12}, drafter $DRAFT @ ${DRAFT_REV:0:12})"
vlane_preflight engine-preflight.sh engine-secrets.sh vllm_engine_auth.py ckpt-fetch.py install-nemotron.sh
vlane_room "$NEMOTRON_BASE_IMAGE" "$MODEL" "$DRAFT"
if [ "$MODE" = units ]; then
  echo "OK: $(uname -m), key present; the image and the checkpoints come with the lane's first Load"
  install_service
  step "Done: the Nemotron 3.5 Super VL lane is installed; its first Load prepares it"
  echo "  the cockpit: Load on Nemotron 3.5 Super VL in the Lanes view builds its image (the Nemotron 3.5 Bolt lane's,"
  echo "               about 25 GB, 11 to download, once for the two), fetches its checkpoints (about $WEIGHTS_GB GB), then starts it"
  echo "  a terminal : ./install-nemotron-super.sh --prepare, then ./switch-model.sh nemotron-super and the commands it prints"
  exit 0
fi

step "2/5 Image ($LOCAL_IMAGE, the Nemotron 3.5 Bolt lane's)"
if docker image inspect "$LOCAL_IMAGE" >/dev/null 2>&1; then
  echo "already built"
else
  # built as the Bolt lane builds it, from its pins: the base image by digest, b12x's wheel
  "$HERE/install-nemotron.sh" --prepare || die "the image build failed (the lines above say why)"
  docker image inspect "$LOCAL_IMAGE" >/dev/null 2>&1 \
    || die "install-nemotron.sh --prepare built another image than $LOCAL_IMAGE (repo bug: please open an issue)"
fi

if [ "$MODE" = prepare ]; then
  step "Done: the Nemotron 3.5 Super VL lane's image is ready; its Load fetches the checkpoints and starts it"
  exit 0
fi

step "3/5 Checkpoints ($MODEL @ ${MODEL_REV:0:12} and its drafter, resumable)"
vlane_fetch "$MODEL" "$MODEL_REV"
vlane_fetch "$DRAFT" "$DRAFT_REV"

install_service

if [ "$SMOKE" -eq 0 ]; then step "Done (proof skipped)"; exit 0; fi

step "5/5 Proving it serves (a reply and a tool call, then the box goes back to the lane it was serving)"
VLANE_SERVED=nemotron-3.5-super-vl; VLANE_KWARGS='{"enable_thinking":false}'
VLANE_PARSERS="the qwen3_coder parser is"; VLANE_BOOT="about 11 min"; VLANE_WAIT_MIN=30
vlane_prove

step "Done: the Nemotron 3.5 Super VL lane is installed and proved it serves on $ENGINE_BIND:$PORT"
echo "  switch to it : Load on Nemotron 3.5 Super VL in the cockpit, or ./switch-model.sh nemotron-super"
echo "  clients      : the proxy on :30001 with the box's key, model nemotron-3.5-super-vl (OpenAI and Anthropic APIs)"
