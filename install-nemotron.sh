#!/usr/bin/env bash
# Install the Nemotron lane: NVIDIA's Nemotron 3.5 Bolt, a mixture of experts of 74B parameters
# (9B active per token) compressed to 24 GB (its experts in 2 bits), with reasoning, tool calling
# and image and video input, served by vLLM on the text lanes' port behind the proxy and its key.
# Idempotent, and safe to run alone.
#
#   ./install-nemotron.sh                 first install, or update in place
#   ./install-nemotron.sh --no-smoke      skip the proof at the end
#   ./install-nemotron.sh --uninstall     remove the unit (weights kept, and the image while the
#                                         Nemotron Super lane uses it)
#   ./install-nemotron.sh --units-only    the unit alone: no image, no weights (install.sh, on demand)
#   ./install-nemotron.sh --prepare       its image, built with no sudo: the cockpit's first Load
#                                         runs it (./prepare-lane.sh nemotron)
#
# install.sh puts this lane's unit in on every plain install, like the other lanes, and its first
# Load from the cockpit builds it; ./install.sh --no-nemotron leaves it out of the run that passes it. It takes 26 GB of weights (the model and its drafter) and a 25 GB image, 35 at its
# pull's peak; when the disk does not have the room, this refuses before downloading anything.
#
# WHY THIS BUILD. NVIDIA's model card serves Bolt with one vLLM nightly, 77154ca6, plus the b12x
# kernel library (its 2-bit IQ2_XXS experts, NVFP4 Mamba layers and Q8_0 layers run on b12x on the
# GB10, SM121). That image is published for arm64; this builds on it, pinned by the digest of its
# arm64 manifest, plus b12x's wheel checked against the sha256 PyPI publishes, installed with
# --no-deps (the image already carries every dependency b12x names, checked with pip check). The
# card's other step, `vllm download-kernels`, installs FlashInfer's prebuilt kernels: this image
# already holds exactly those (flashinfer-cubin and flashinfer-jit-cache 0.7.0.post1, sm121a among
# them), so it is not run, and the lane starts with no internet.
#
# ONE ENGINE AT A TIME. The unit says so with Conflicts=: starting it stops whichever lane
# serves, and starting another lane stops it.
set -euo pipefail
# One spelling of the home, without a trailing slash, as install.sh says why: the cockpit's
# exact-argv sudoers is rendered from it and has to match the cockpit's own calls (2026-10-22).
while [ -n "${HOME:-}" ] && [ "$HOME" != / ] && [ "${HOME%/}" != "$HOME" ]; do HOME="${HOME%/}"; done; export HOME
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=qwen38-nemotron.service
# where systemd reads the units, the one variable lanes.sh and vllm-lane.sh read the other units by
INSTALLED="${LANES_UNIT_DIR:-/etc/systemd/system}/$UNIT"
CONTAINER=qwen38-nemotron
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
  # The two halves of a lane installed on demand, as for the image, video and Kolibri lanes:
  # --units-only puts in what needs root (the unit, its start scripts) and nothing heavy, and
  # --prepare builds the lane's image as the user, with no sudo, when the cockpit first loads
  # the lane. The checkpoints come with that Load's switch, as for any Load.
  --units-only) [ "$MODE" = full ] || die "--units-only and --prepare are the two halves of an install: one at a time"; MODE=units ;;
  --prepare) [ "$MODE" = full ] || die "--units-only and --prepare are the two halves of an install: one at a time"; MODE=prepare ;;
  -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1' "$0"; exit 0 ;;
  *) die "unknown option: $a" ;;
esac; done

CONFIG_DIR="$HOME/.config/qwen38"
# the text lane the box comes back to, one record shared with switch-model.sh (lanes.sh)
# shellcheck source=lanes.sh
. "$HERE/lanes.sh"
# A terminal opened before the account joined the docker group: the run goes on with it (lanes.sh).
docker_session bash "$HERE/${BASH_SOURCE[0]##*/}" "$@"
# what the three vLLM lanes' installers share: the engine's cache and port, the uninstall, the
# service, the preflight and the room, the fetch, the proof
# shellcheck source=vllm-lane.sh
. "$HERE/vllm-lane.sh"
VLANE=nemotron; VLANE_SHORT=Nemotron

# The pins, at column 0 like install.sh's: the vLLM nightly NVIDIA's card names (its arm64
# manifest), the b12x wheel, the model and its drafter at the revisions measured on the reference
# box on 2026-10-15. NVIDIA rewrote both repos' history when it made them public ("initial commit",
# 15:42 UTC): the revisions measured, from before, are gone, and these hold every one of the same
# files bit for bit (blob hashes compared), the model card and one note excepted.
NEMOTRON_BASE_IMAGE="vllm/vllm-openai@sha256:1ee85ca80e0fc110017d79ea2081a0cbef9a208a05d7f22724801bcf6a956620"
NEMOTRON_VLLM_LABEL="nightly-77154ca6"
NEMOTRON_B12X_VERSION="1.5.1"
NEMOTRON_B12X_URL="https://files.pythonhosted.org/packages/0c/7f/31fbc8311188835a509d97c38f6400071ba5b01a5ca54cb33d7b22dec3fa/b12x-1.5.1-py3-none-any.whl"
NEMOTRON_B12X_SHA256="8731e23571d8d45db26d78e7699ec0b23e7ba20b52487adf8c1063c8d8b53a3d"
NEMOTRON_BOLT_REPO="nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB"
NEMOTRON_BOLT_REV="c6cc2e8ffa353403213d6b9b18aaf8eb684d705a"
NEMOTRON_BOLT_DRAFT_REPO="nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB-LiLiCorr"
NEMOTRON_BOLT_DRAFT_REV="4c6a5613b6d887194a3a5493e3a18a5b3930bb86"
# The share of the GPU memory and the download in GB (why: where they are used below).
# switch-model.sh reads them here, with the repos and revisions.
NEMOTRON_BOLT_GMU="0.70"
# The share on a 64 GB DGX Spark (boxmem.py), of its total as vLLM reads it. Measured on a 64 GB
# budget (2026-10-24: on the reference box at the same absolute memory, 0.284 of its 121.63 GiB,
# as vLLM asks total x share): 667,874 tokens of KV, 2.55 times the 262,144-token window, a
# 110.8 tok/s greedy median on bench.sh, and 7.1 GiB left at the peak under four 32,768-token
# requests. A 64 GB box serves this lane (install.sh).
NEMOTRON_BOLT_GMU_64="0.60"
NEMOTRON_BOLT_GB="26"
# The local image: the base and the wheel, nothing else. Its tag names both, so a change of
# either pin is a new tag, and a box never serves an image built from older pins. The wheel is
# named by its version, which PyPI never lets a file change under.
B12X_WHEEL="b12x-${NEMOTRON_B12X_VERSION}-py3-none-any.whl"
[ "${NEMOTRON_B12X_URL##*/}" = "$B12X_WHEEL" ] \
  || die "NEMOTRON_B12X_URL names ${NEMOTRON_B12X_URL##*/}, not the $B12X_WHEEL of NEMOTRON_B12X_VERSION (repo bug: please open an issue)"
LOCAL_IMAGE="qwen38-nemotron:vllm${NEMOTRON_VLLM_LABEL#nightly-}-b12x${NEMOTRON_B12X_VERSION}-${NEMOTRON_BASE_IMAGE##*:}"
LOCAL_IMAGE="${LOCAL_IMAGE:0:52}"

# One checkpoint today, Bolt with its LiLiCorr drafter. Its share of the GPU memory, 0.70, holds a
# KV cache of 6.5 million tokens with the prefix cache's settings in its unit (6,523,301 and
# 6,536,475 over two boots, 2026-10-16; 4,712,014 before them): 24 requests at the full 262,144 window,
# for the 16 the unit lets in at once, with 26 GiB of MemAvailable left to the box at rest (25.3 at
# its least, a 252,000-token prompt's prefill, before them). The drafter
# costs that cache dearly (2.0 million tokens at 0.45 with it, 5.2 without) and pays it back at
# every load measured (72.7 against 30.3 tok/s for one stream, 213 against 178 for sixteen).
MODEL="$NEMOTRON_BOLT_REPO"; MODEL_REV="$NEMOTRON_BOLT_REV"
DRAFT="$NEMOTRON_BOLT_DRAFT_REPO"; DRAFT_REV="$NEMOTRON_BOLT_DRAFT_REV"
GMU="$NEMOTRON_BOLT_GMU"; WEIGHTS_GB="$NEMOTRON_BOLT_GB"
[ "$(box_mem_class)" != 64 ] || GMU="$NEMOTRON_BOLT_GMU_64"
# The base image's pull at its peak: 24.46 GB unpacked with the wheel's layer (27 MB) and 10.30 GB
# to download (the registry's arm64 manifest), which the containerd image store (Docker 29's
# default on a new install) keeps beside it, 32.4 GiB; ceil(x 1.02) + 1 as df -BG rounds up
# (install.sh counts the text lanes' images so). It asked for 34, from rounded sizes (found in
# review, 2026-10-15).
IMAGE_GB=35

# The text lanes' cache and port: Nemotron is one of them for the proxy (vllm-lane.sh).
vlane_engine_env "${NEMOTRON_PORT:-}"

if [ "$ACTION" = uninstall ]; then
  step "Removing the Nemotron 3.5 Bolt lane"
  vlane_uninstall_unit
  vlane_uninstall_clients
  # The image is the Nemotron Super lane's too (install-nemotron-super.sh): a build stays while an
  # installed unit names it (lanes.sh).
  vlane_uninstall_runtime qwen38-nemotron "$NEMOTRON_BASE_IMAGE" "$NEMOTRON_BOLT_REPO" "$NEMOTRON_BOLT_DRAFT_REPO"
  exit 0
fi

# Step 4, the one part that needs root: the unit and its start scripts. A function, since a
# lane installed on demand (--units-only) takes it straight after the preflight.
install_service(){
  step "4/5 Service"
  # vLLM's compile cache, FlashInfer's kernels built at their first use and b12x's
  vlane_service_files "$CONFIG_DIR/nemotron-cache/vllm" "$CONFIG_DIR/nemotron-cache/flashinfer" "$CONFIG_DIR/nemotron-cache/b12x"
  RENDER="$(mktemp)"; trap 'rm -f "$RENDER"' EXIT
  sed -e "s|__USER__|$(id -un)|g" -e "s|__GROUP__|$(id -gn "$(id -un)")|g" -e "s|__HOME__|$HOME|g" \
      -e "s|__IMAGE__|$LOCAL_IMAGE|g" -e "s|__MODEL_DIR__|${MODEL//\//--}|g" -e "s|__MODEL__|$MODEL|g" \
      -e "s|__REV__|$MODEL_REV|g" -e "s|__DRAFT__|$DRAFT|g" -e "s|__DREV__|$DRAFT_REV|g" \
      -e "s|__GMU__|$GMU|g" -e "s|__HF_CACHE__|$HF_CACHE|g" \
      -e "s|__ENGINE_BIND__|$ENGINE_BIND|g" -e "s|__PORT__|$PORT|g" \
      "$HERE/$UNIT.template" > "$RENDER"
  vlane_put_unit "$RENDER"
  # Builds of this image from older pins, once the unit names this one, and none the Nemotron
  # Super lane's unit still names (the two lanes run one image: lanes.sh says why that matters).
  lane_prune_images qwen38-nemotron "$LOCAL_IMAGE"
  # Installed, not switched to: the lane this box serves stays the lane it serves.
  echo "installed as a lane of its own. Switch to it like any lane:"
  echo "  the cockpit: Load on Nemotron 3.5 Bolt in the Lanes view"
  echo "  a terminal : ./switch-model.sh nemotron, then the commands it prints"
}

step "1/5 Preflight (Bolt: $MODEL @ ${MODEL_REV:0:12}, drafter $DRAFT @ ${DRAFT_REV:0:12})"
vlane_preflight engine-preflight.sh engine-secrets.sh vllm_engine_auth.py ckpt-fetch.py
vlane_room "$NEMOTRON_BASE_IMAGE" "$MODEL" "$DRAFT"
if [ "$MODE" = units ]; then
  echo "OK: $(uname -m), key present; the image and the checkpoints come with the lane's first Load"
  install_service
  step "Done: the Nemotron 3.5 Bolt lane is installed; its first Load prepares it"
  echo "  the cockpit: Load on Nemotron 3.5 Bolt in the Lanes view builds its image (about 25 GB, 11 to download),"
  echo "               fetches its checkpoints (about $WEIGHTS_GB GB), then starts it"
  echo "  a terminal : ./install-nemotron.sh --prepare, then ./switch-model.sh nemotron and the commands it prints"
  exit 0
fi

step "2/5 Image ($LOCAL_IMAGE)"
if docker image inspect "$LOCAL_IMAGE" >/dev/null 2>&1; then
  echo "already built"
else
  docker image inspect "$NEMOTRON_BASE_IMAGE" >/dev/null 2>&1 || docker pull "$NEMOTRON_BASE_IMAGE" \
    || die "docker pull of $NEMOTRON_BASE_IMAGE failed. Causes: no internet, a Docker Hub rate limit (retry in a few minutes, or docker login), or the digest removed upstream."
  BUILD="$(mktemp -d)"
  curl -fsSL --retry 3 -o "$BUILD/$B12X_WHEEL" "$NEMOTRON_B12X_URL" \
    || { rm -rf "$BUILD"; die "could not download the b12x wheel from PyPI ($NEMOTRON_B12X_URL)"; }
  echo "$NEMOTRON_B12X_SHA256  $BUILD/$B12X_WHEEL" | sha256sum -c --quiet - \
    || { rm -rf "$BUILD"; die "the b12x wheel does not match the sha256 PyPI published for $NEMOTRON_B12X_VERSION; refusing to build."; }
  # The base image plus the wheel with --no-deps, so pip never replaces the vLLM, torch, CUTLASS
  # DSL and transformers the base carries; the last line proves the version installed.
  cat > "$BUILD/Dockerfile" <<'DEOF'
ARG BASE_IMAGE
ARG B12X_VERSION
FROM ${BASE_IMAGE}
ARG B12X_VERSION
COPY *.whl /tmp/
RUN pip install --no-deps /tmp/*.whl && rm /tmp/*.whl \
 && python3 -c "import sys; from importlib.metadata import version; v = version('b12x'); assert v == sys.argv[1], v" "${B12X_VERSION}"
DEOF
  docker build -q --build-arg BASE_IMAGE="$NEMOTRON_BASE_IMAGE" --build-arg B12X_VERSION="$NEMOTRON_B12X_VERSION" \
    -t "$LOCAL_IMAGE" "$BUILD" >/dev/null \
    || { rm -rf "$BUILD"; die "the image build failed (the lines above say why)"; }
  rm -rf "$BUILD"
  echo "built from $NEMOTRON_BASE_IMAGE and the b12x wheel"
fi

if [ "$MODE" = prepare ]; then
  step "Done: the Nemotron 3.5 Bolt lane's image is ready; its Load fetches the checkpoints and starts it"
  exit 0
fi

step "3/5 Checkpoints ($MODEL @ ${MODEL_REV:0:12} and its drafter, resumable)"
vlane_fetch "$MODEL" "$MODEL_REV"
vlane_fetch "$DRAFT" "$DRAFT_REV"

install_service

if [ "$SMOKE" -eq 0 ]; then step "Done (proof skipped)"; exit 0; fi

step "5/5 Proving it serves (a reply and a tool call, then the box goes back to the lane it was serving)"
VLANE_SERVED=nemotron-3.5-bolt; VLANE_KWARGS='{"enable_thinking":false}'
VLANE_PARSERS="the qwen3_coder parser is"; VLANE_BOOT="about 8 min"; VLANE_WAIT_MIN=20
vlane_prove

step "Done: the Nemotron 3.5 Bolt lane is installed and proved it serves on $ENGINE_BIND:$PORT"
echo "  switch to it : Load on Nemotron 3.5 Bolt in the cockpit, or ./switch-model.sh nemotron"
echo "  clients      : the proxy on :30001 with the box's key, model nemotron-3.5-bolt (OpenAI and Anthropic APIs)"
