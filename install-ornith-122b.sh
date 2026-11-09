#!/usr/bin/env bash
# Install the Ornith 1.5 122B lane: Ornith AI's Ornith 1.5 122B (MIT), a mixture of experts of the
# Qwen3.5 family with vision (122B parameters, 8 of 256 experts active per token), in the NVFP4
# build its authors published, served by vLLM with b12x on the text lanes' port behind the proxy
# and its key. Idempotent, and safe to run alone.
#
#   ./install-ornith-122b.sh                 first install, or update in place
#   ./install-ornith-122b.sh --no-smoke      skip the proof at the end
#   ./install-ornith-122b.sh --uninstall     remove the unit (the checkpoint and the image kept, said)
#   ./install-ornith-122b.sh --units-only    the unit alone: no image, no checkpoint (install.sh, on demand)
#   ./install-ornith-122b.sh --prepare       its image, pulled with no sudo: the cockpit's first Load
#                                            runs it (./prepare-lane.sh ornith-122b)
#   ./install-ornith-122b.sh --stage FILE    the unit for the checkpoint in the cache, written to FILE
#                                            with no sudo: switch-model.sh ornith-122b installs FILE
#
# install.sh puts this lane's unit in on every plain install, like the other lanes, and its first
# Load from the cockpit pulls its image and fetches its checkpoint; ./install.sh --no-ornith-122b
# leaves it out of the run that passes it. It takes a checkpoint of 78 GB and the
# Qwen3.8 Flash-Next FP4-Local lane's image, 18 GB (10 to download), which the two lanes share: when the disk
# does not have the room, this refuses before downloading anything.
#
# WHY THIS IMAGE. The checkpoint mixes NVFP4 experts read as W4A16 and FP8 projections, which
# ModelOpt calls MIXED_PRECISION: the community b12x vLLM build NVIDIA's Qwen3.8 Flash-Next FP4-Local card names
# for a DGX Spark reads it with the b12x kernels and loader it serves FP4-Local with, and served this
# model right on every test (docs/ornith-lane.md says what was measured). The two lanes pull it once.
#
# ONE ENGINE AT A TIME. The unit says so with Conflicts=: starting it stops whichever lane
# serves, and starting another lane stops it.
set -euo pipefail
# One spelling of the home, without a trailing slash, as install.sh says why: the cockpit's
# exact-argv sudoers is rendered from it and has to match the cockpit's own calls (2026-10-22).
while [ -n "${HOME:-}" ] && [ "$HOME" != / ] && [ "${HOME%/}" != "$HOME" ]; do HOME="${HOME%/}"; done; export HOME
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=qwen38-ornith-122b.service
# where systemd reads the units, the one variable lanes.sh and vllm-lane.sh read the other units by
INSTALLED="${LANES_UNIT_DIR:-/etc/systemd/system}/$UNIT"
CONTAINER=qwen38-ornith-122b
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
  # what needs root (the unit, its start scripts) and nothing heavy, and --prepare pulls the
  # lane's image as the user, with no sudo, when the cockpit first loads the lane. The checkpoint
  # comes with that Load's switch, as for any Load.
  --units-only) [ "$MODE" = full ] || die "--units-only and --prepare are the two halves of an install: one at a time"; MODE=units ;;
  --prepare) [ "$MODE" = full ] || die "--units-only and --prepare are the two halves of an install: one at a time"; MODE=prepare ;;
  --stage) [ "$MODE" = full ] || die "--stage is a mode of its own"; MODE=stage ;;
  -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1' "$0"; exit 0 ;;
  *) if [ "$MODE" = stage ] && [ -z "${STAGE_OUT:-}" ]; then STAGE_OUT="$a"; else die "unknown option: $a"; fi ;;
esac; done
if [ "$MODE" = stage ] && [ -z "${STAGE_OUT:-}" ]; then die "--stage takes the file to write the unit to"; fi

CONFIG_DIR="$HOME/.config/qwen38"
# the text lane the box comes back to, one record shared with switch-model.sh (lanes.sh)
# shellcheck source=lanes.sh
. "$HERE/lanes.sh"
# A 64 GB DGX Spark cannot hold this lane (boxmem.py): refused there before anything is fetched,
# and --uninstall still takes away what an older release put there (found in review, 2026-10-23).
[ "$ACTION" = uninstall ] || [ "$(box_mem_class)" != 64 ] || lane_fits ornith-122b || die "The Ornith 1.5 122B lane needs a 128 GB DGX Spark, and this box is a 64 GB one."
# A terminal opened before the account joined the docker group: the run goes on with it (lanes.sh).
docker_session bash "$HERE/${BASH_SOURCE[0]##*/}" "$@"
# what the vLLM lanes' installers share: the engine's cache and port, the uninstall, the service,
# the preflight and the room, the fetch, the proof
# shellcheck source=vllm-lane.sh
. "$HERE/vllm-lane.sh"
VLANE=ornith-122b; VLANE_SHORT="Ornith 1.5 122B"

# The pins, at column 0 like install.sh's: the checkpoint at its revision of 2026-10-21 08:31 UTC, the
# last one, whose files are those of the revision measured on the reference box (dde0b1af, 2026-10-20
# 17:37 UTC) but its model card, rewritten four times since in its introduction alone (checked file by
# file on 2026-10-22); the memory share and the requests at a time measured with it, the tokens MTP
# drafts, the download in GB. switch-model.sh reads them here.
ORNITH_122B_REPO="ornith-ai/Ornith-1.5-122B-NVFP4"
ORNITH_122B_REV="f049036f794622db10bb69855bf9819207334626"
ORNITH_122B_GMU="0.8"
ORNITH_122B_KV_BYTES="18253611008"
ORNITH_122B_GB="78"
ORNITH_122B_SEQS="8"
ORNITH_122B_SPEC="2"
MODEL="$ORNITH_122B_REPO"; MODEL_REV="$ORNITH_122B_REV"
GMU="$ORNITH_122B_GMU"; WEIGHTS_GB="$ORNITH_122B_GB"; SEQS="$ORNITH_122B_SEQS"
# The image is the Qwen3.8 Flash-Next FP4-Local lane's, by its pin in install-flash-local.sh, the one place it is
# declared, pulled as published, by digest: nothing is built on the box. Its pull at its peak, as
# that installer counts it (its IMAGE_GB, read there).
eval "$(grep -E '^FLASH_LOCAL_IMAGE=' "$HERE/install-flash-local.sh" || true)"
[ -n "${FLASH_LOCAL_IMAGE:-}" ] || die "install-flash-local.sh no longer defines FLASH_LOCAL_IMAGE (repo layout changed?)"
LOCAL_IMAGE="$FLASH_LOCAL_IMAGE"
IMAGE_GB="$({ grep -m1 -oE '^IMAGE_GB=[0-9]+' "$HERE/install-flash-local.sh" || true; } | cut -d= -f2)"
case "$IMAGE_GB" in ''|*[!0-9]*) die "install-flash-local.sh no longer defines IMAGE_GB (repo layout changed?)" ;; esac

# The text lanes' cache and port: Ornith is one of them for the proxy (vllm-lane.sh).
vlane_engine_env "${ORNITH_122B_PORT:-}"

if [ "$ACTION" = uninstall ]; then
  step "Removing the Ornith 1.5 122B lane"
  vlane_uninstall_unit
  vlane_uninstall_clients
  # The image is pulled, not built: no build of it to prune (qwen38-ornith-122b names none), and
  # its local tag goes alone (lanes.sh), the Qwen3.8 Flash-Next FP4-Local lane's own staying.
  vlane_uninstall_runtime qwen38-ornith-122b "$FLASH_LOCAL_IMAGE" "$ORNITH_122B_REPO"
  pin_untag ornith-122b
  rm -f "$CONFIG_DIR/chat-template-ornith-122b.jinja"
  # The seccomp profile is the Qwen3.8 Flash-Next FP4-Local lane's too: it stays while that lane's unit reads it.
  if [ -f "$LANES_UNIT_DIR/qwen38-flash-local.service" ]; then
    echo "seccomp-io-uring.json kept: the Qwen3.8 Flash-Next FP4-Local lane reads it too"
  else
    rm -f "$CONFIG_DIR/seccomp-io-uring.json"
  fi
  exit 0
fi

# The unit, rendered for this box: written to $1.
render_unit(){
  sed -e "s|__USER__|$(id -un)|g" -e "s|__GROUP__|$(id -gn "$(id -un)")|g" -e "s|__HOME__|$HOME|g" \
      -e "s|__IMAGE__|$LOCAL_IMAGE|g" -e "s|__MODEL__|$MODEL|g" -e "s|__REV__|$MODEL_REV|g" \
      -e "s|__GMU__|$GMU|g" -e "s|__SEQS__|$SEQS|g" -e "s|__SPEC_TOKENS__|$ORNITH_122B_SPEC|g" \
      -e "s|__KV_BYTES__|$ORNITH_122B_KV_BYTES|g" \
      -e "s|__HF_CACHE__|$HF_CACHE|g" -e "s|__ENGINE_BIND__|$ENGINE_BIND|g" -e "s|__PORT__|$PORT|g" \
      "$HERE/$UNIT.template" > "$1"
}

# The checkpoint's chat template with the two fixes it needs (vlane_template, TEMPLATE_FIXES): it
# refused a system message past the first one, which opencode 1.19.4 sends, and every opencode request
# on this lane got a 400 (2026-10-24); that message becomes a <system-reminder>, as on the 27B and the
# two flash lanes. And its render_content loop is the Qwen one, which raised on the tool_reference blocks
# of a Claude Code ToolSearch result (a 400 "Unexpected item type in content." on the FP4-Local lane,
# 2026-10-26): each block now names its tool, as on the Qwen lanes. The template has no reasoning effort
# of its own (the proxy gives it one through enable_thinking), so those are its only fixes. Written again at every install and every switch, and left
# as it is, mtime included, when nothing changed.
TEMPLATE="$CONFIG_DIR/chat-template-ornith-122b.jinja"
# shellcheck disable=SC2054  # one argument: patch-template.py takes its fixes' names comma-separated
TEMPLATE_FIXES=(--patches=system-reminder,tool-reference)
lane_template(){ vlane_template "$TEMPLATE" "${TEMPLATE_FIXES[@]}"; }

# This lane's own folders: vLLM's compile cache, FlashInfer's and Triton's kernels and b12x's
# kernel choices, built at the first boot.
CACHES=("$CONFIG_DIR/ornith-122b-cache/vllm" "$CONFIG_DIR/ornith-122b-cache/flashinfer"
        "$CONFIG_DIR/ornith-122b-cache/b12x" "$CONFIG_DIR/ornith-122b-cache/triton")

# Step 4, the one part that needs root: the unit and its start scripts. A function, since a lane
# installed on demand (--units-only) takes it straight after the preflight.
install_service(){
  step "4/5 Service"
  vlane_service_files "${CACHES[@]}"
  # Docker's default seccomp profile and the three io_uring calls b12x's loader reads the checkpoint
  # with (the unit says why), the Qwen3.8 Flash-Next FP4-Local lane's file; the docker client reads it at every
  # start, as the user
  cmp -s "$HERE/seccomp-io-uring.json" "$CONFIG_DIR/seccomp-io-uring.json" \
    || install -m 644 "$HERE/seccomp-io-uring.json" "$CONFIG_DIR/seccomp-io-uring.json"
  RENDER="$(mktemp)"; trap 'rm -f "$RENDER"' EXIT
  render_unit "$RENDER"
  # with what the engine reads at its start besides the unit: a lane started before they changed
  # serves the old ones (vllm-lane.sh)
  vlane_put_unit "$RENDER" "$CONFIG_DIR/seccomp-io-uring.json" "$TEMPLATE"
  # Installed, not switched to: the lane this box serves stays the lane it serves.
  echo "installed as a lane of its own. Switch to it like any lane:"
  echo "  the cockpit: Load on Ornith 1.5 122B in the Lanes view"
  echo "  a terminal : ./switch-model.sh ornith-122b, then the commands it prints"
}

# --stage: the unit for the checkpoint in the cache, for switch-model.sh, with no sudo. This lane's
# own files only, as the other lanes' switches touch nothing but their unit: the start scripts every
# lane runs (vlane_service_files) are ./install.sh's to put in (the Qwen3.8 Flash-Next FP4-Local lane's rule). Its
# caches made as the user, so that docker does not make them as root, and its filter only when it
# is missing.
if [ "$MODE" = stage ]; then
  lane_template >&2
  mkdir -p "${CACHES[@]}"
  [ -f "$CONFIG_DIR/seccomp-io-uring.json" ] || install -m 644 "$HERE/seccomp-io-uring.json" "$CONFIG_DIR/seccomp-io-uring.json"
  render_unit "$STAGE_OUT"
  grep -q '__[A-Z][A-Z0-9_]*__' "$STAGE_OUT" && die "the unit template still holds an unsubstituted placeholder"
  exit 0
fi

step "1/5 Preflight (Ornith 1.5 122B: $MODEL @ ${MODEL_REV:0:12})"
vlane_preflight engine-preflight.sh engine-secrets.sh vllm_engine_auth.py ckpt-fetch.py seccomp-io-uring.json \
  install-flash-local.sh patch-template.py
vlane_room "$FLASH_LOCAL_IMAGE" "$MODEL"
if [ "$MODE" = units ]; then
  echo "OK: $(uname -m), key present; the image and the checkpoint come with the lane's first Load"
  vlane_template_if_cached "$TEMPLATE" "${TEMPLATE_FIXES[@]}"
  install_service
  step "Done: the Ornith 1.5 122B lane is installed; its first Load prepares it"
  echo "  the cockpit: Load on Ornith 1.5 122B in the Lanes view pulls its image (the Qwen3.8 Flash-Next FP4-Local lane's,"
  echo "               about 18 GB, 10 to download, once for the two), fetches its checkpoint (about $WEIGHTS_GB GB), then starts it"
  echo "  a terminal : ./install-ornith-122b.sh --prepare, then ./switch-model.sh ornith-122b and the commands it prints"
  exit 0
fi

step "2/5 Image (${FLASH_LOCAL_IMAGE:0:40}..., the Qwen3.8 Flash-Next FP4-Local lane's)"
if docker image inspect "$FLASH_LOCAL_IMAGE" >/dev/null 2>&1; then
  echo "already here"
else
  docker pull "$FLASH_LOCAL_IMAGE" \
    || die "docker pull of $FLASH_LOCAL_IMAGE failed. Causes: no internet, a Docker Hub rate limit (retry in a few minutes, or docker login), or the digest removed upstream."
fi
# its local tag, so that a docker image prune leaves it alone while this lane is installed, and the
# tags of the images it was pinned to before go (lanes.sh); the Qwen3.8 Flash-Next FP4-Local lane keeps its own
pin_tag ornith-122b "$FLASH_LOCAL_IMAGE"
unpin_stale ornith-122b "$FLASH_LOCAL_IMAGE" || true

if [ "$MODE" = prepare ]; then
  step "Done: the Ornith 1.5 122B lane's image is ready; its Load fetches the checkpoint and starts it"
  exit 0
fi

step "3/5 Checkpoint ($MODEL @ ${MODEL_REV:0:12}, resumable) and its chat template"
vlane_fetch "$MODEL" "$MODEL_REV"
lane_template

install_service

if [ "$SMOKE" -eq 0 ]; then step "Done (proof skipped)"; exit 0; fi

step "5/5 Proving it serves (a reply and a tool call, then the box goes back to the lane it was serving)"
VLANE_SERVED=ornith-1.5-122b; VLANE_KWARGS='{"enable_thinking":false}'
VLANE_PARSERS="the qwen3_xml parser is"; VLANE_BOOT="about 7 min"; VLANE_WAIT_MIN=40
vlane_prove

step "Done: the Ornith 1.5 122B lane is installed and proved it serves on $ENGINE_BIND:$PORT"
echo "  switch to it : Load on Ornith 1.5 122B in the cockpit, or ./switch-model.sh ornith-122b"
echo "  clients      : the proxy on :30001 with the box's key, model ornith-1.5-122b (OpenAI and Anthropic APIs)"
