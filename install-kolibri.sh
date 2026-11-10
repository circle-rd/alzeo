#!/usr/bin/env bash
# Install the Kolibri-1 lane: Aleph Alpha's open 78B mixture-of-experts (3.5B active per
# token), German and English, with reasoning and tool calling, served by vLLM on the text
# lanes' port behind the proxy and its key. Idempotent, and safe to run alone.
#
#   ./install-kolibri.sh                 first install, or update in place
#   ./install-kolibri.sh --no-smoke      skip the proof at the end
#   ./install-kolibri.sh --uninstall     remove the unit and the local image (weights kept)
#   ./install-kolibri.sh --units-only    the unit alone: no image, no weights (install.sh, on demand)
#   ./install-kolibri.sh --prepare       its image, built with no sudo: the cockpit's first Load
#                                        runs it (./prepare-lane.sh kolibri)
#
# install.sh runs this on every plain install, like the other lanes; ./install.sh
# --no-kolibri leaves it out of the run that passes it. It takes 79 GB of
# weights (the official FP8 release) and a 23 GB image; when the disk does not have the
# room, this refuses before downloading anything and install.sh goes on without it.
#
# WHY vLLM AND NOT SGLANG. SGLang cannot load Kolibri (sgl-project/sglang#43256, open on
# 2026-10-14). Aleph Alpha serves it with vLLM and their plugin, aleph-alpha-inference 1.0.0,
# which pins vLLM 0.29.x. Their container is published for amd64 only, so this builds the
# same one here: their Dockerfile, on the very image their FROM line names (vLLM v0.29.0,
# whose arm64 manifest is pinned below by digest), plus the plugin's wheel, checked
# against the sha256 PyPI publishes. The -cu129 variant of that image cannot run on the
# GB10: its block-FP8 kernels are built for sm_120a, and the first GEMM dies with
# "cutlass_gemm_caller ... Error Internal" (measured 2026-10-14); this one is CUDA 13.0.
#
# TWO CHECKPOINTS, ONE LANE. The official FP8 is the default and the reference. A 4-bit
# NVFP4 variant (Quantinine/Kolibri-1-NVFP4A16-FP8: the routed experts in NVFP4, attention
# and everything else the official FP8 bit for bit) is the other one, switched to with
# ./switch-model.sh kolibri-nvfp4 and downloaded on that first switch. Measured on the
# reference box, 2026-10-14, against the FP8 over 153,450 positions of English, German and
# French Wikipedia: the likelihood of English and German text is level (+0.006 and +0.003
# nats per token, neither interval excludes zero), French drops measurably (+0.037 nats per
# token, about 3.8 % of perplexity; French is not one of Kolibri's two languages). It
# decodes 10 % faster alone and 50 % faster at 8 streams, in 42.8 GiB instead of 73.6.
#
# ONE ENGINE AT A TIME. The unit says so with Conflicts=: starting it stops whichever lane
# serves, and starting another lane stops it.
set -euo pipefail
# One spelling of the home, without a trailing slash, as install.sh says why: the cockpit's
# exact-argv sudoers is rendered from it and has to match the cockpit's own calls (2026-10-22).
while [ -n "${HOME:-}" ] && [ "$HOME" != / ] && [ "${HOME%/}" != "$HOME" ]; do HOME="${HOME%/}"; done; export HOME
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=qwen38-kolibri.service
# where systemd reads the units, the one variable lanes.sh and vllm-lane.sh read the other units by
INSTALLED="${LANES_UNIT_DIR:-/etc/systemd/system}/$UNIT"
CONTAINER=qwen38-kolibri
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
  # The two halves of a lane installed on demand, as for the image and video lanes:
  # --units-only puts in what needs root (the unit, its start scripts) and nothing heavy, and
  # --prepare builds the lane's image as the user, with no sudo, when the cockpit first loads
  # the lane. The checkpoint comes with that Load's switch, as for any Load.
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
[ "$ACTION" = uninstall ] || [ "$(box_mem_class)" != 64 ] || lane_fits kolibri || die "The Kolibri-1 lane needs a 128 GB DGX Spark, and this box is a 64 GB one."
# A terminal opened before the account joined the docker group: the run goes on with it (lanes.sh).
docker_session bash "$HERE/${BASH_SOURCE[0]##*/}" "$@"
# what the three vLLM lanes' installers share: the engine's cache and port, the uninstall, the
# service, the preflight and the room, the fetch, the proof
# shellcheck source=vllm-lane.sh
. "$HERE/vllm-lane.sh"
VLANE=kolibri; VLANE_SHORT=Kolibri

# The pins, at column 0 like install.sh's: the vLLM image Aleph Alpha's Dockerfile builds on
# (v0.29.0, its arm64 manifest), the plugin's wheel, and the two checkpoints at the
# revisions measured on the reference box on 2026-10-14.
KOLIBRI_BASE_IMAGE="vllm/vllm-openai@sha256:18372a7224938643461b846fb64c5c9d3d6e9727e82caf2dc3043e620c9d4d7a"
KOLIBRI_PLUGIN_VERSION="1.0.0"
KOLIBRI_PLUGIN_URL="https://files.pythonhosted.org/packages/b0/d7/1eda35b6ee0293f2a52d7a653cf5a4d54c93f21e2e359fe70a7e7b87a851/aleph_alpha_inference-1.0.0-py3-none-any.whl"
KOLIBRI_PLUGIN_SHA256="5a0ca118e67924f10c4f9c04dd64bef117007a17dd820233a8841b9cb8d6f211"
KOLIBRI_FP8_REPO="Aleph-Alpha/Kolibri-1"
KOLIBRI_FP8_REV="258cae8ffffc8c12db670bdbc6ac01bcf8826c87"
KOLIBRI_NVFP4_REPO="Quantinine/Kolibri-1-NVFP4A16-FP8"
KOLIBRI_NVFP4_REV="4a8406455c474fd0fb2bd523c352cc99803eb492"
# Each checkpoint's share of the GPU memory and its download in GB (why: where the checkpoint is
# chosen below). switch-model.sh reads them here, with the repos and revisions.
KOLIBRI_FP8_GMU="0.80"
KOLIBRI_FP8_GB="79"
KOLIBRI_NVFP4_GMU="0.55"
KOLIBRI_NVFP4_GB="46"
# The local image: the base and the wheel, nothing else. Its tag names both, so a change of
# either pin is a new tag, and a box never serves an image built from older pins. The wheel
# is named by its version, which PyPI never lets a file change under (the tag said 1.0.0
# whatever the URL pinned, and a box that had it built kept it: found in review, 2026-10-14).
PLUGIN_WHEEL="aleph_alpha_inference-${KOLIBRI_PLUGIN_VERSION}-py3-none-any.whl"
[ "${KOLIBRI_PLUGIN_URL##*/}" = "$PLUGIN_WHEEL" ] \
  || die "KOLIBRI_PLUGIN_URL names ${KOLIBRI_PLUGIN_URL##*/}, not the $PLUGIN_WHEEL of KOLIBRI_PLUGIN_VERSION (repo bug: please open an issue)"
LOCAL_IMAGE="qwen38-kolibri:vllm0.29.0-aai${KOLIBRI_PLUGIN_VERSION}-${KOLIBRI_BASE_IMAGE##*:}"
LOCAL_IMAGE="${LOCAL_IMAGE:0:52}"

# The checkpoint: the installed unit's, or KOLIBRI_VARIANT (fp8, the default, or nvfp4).
INSTALLED_MODEL="$(grep -m1 -oE -- "(${KOLIBRI_FP8_REPO}|${KOLIBRI_NVFP4_REPO}) --revision" "$INSTALLED" 2>/dev/null | awk '{print $1}' || true)"
# ./install.sh --recommended installs the default checkpoint whatever the unit names (v1.36.0)
if [ "${QWEN38_RECOMMENDED:-0}" = 1 ]; then INSTALLED_MODEL=""; fi
case "${KOLIBRI_VARIANT:-}" in
  "") VARIANT=fp8; [ "$INSTALLED_MODEL" = "$KOLIBRI_NVFP4_REPO" ] && VARIANT=nvfp4 ;;
  fp8|nvfp4) VARIANT="$KOLIBRI_VARIANT" ;;
  *) die "KOLIBRI_VARIANT takes fp8 or nvfp4, got: $KOLIBRI_VARIANT" ;;
esac
# Each checkpoint's share of the GPU memory, chosen so that both keep about the same KV cache,
# 2.0 to 2.2 million tokens (about 8 requests at the full 262,144 window): the FP8's 0.80 left
# 2,093,056 to 2,175,959 tokens over three boots, and MemAvailable never under 10.9 GiB
# (measured, the benchmark client on the box beside it); the NVFP4 holds 30.8 GiB less of
# weights, and 0.55 gives that back to the box rather than to a cache nobody fills.
if [ "$VARIANT" = nvfp4 ]; then
  MODEL="$KOLIBRI_NVFP4_REPO"; MODEL_REV="$KOLIBRI_NVFP4_REV"; GMU="$KOLIBRI_NVFP4_GMU"; WEIGHTS_GB="$KOLIBRI_NVFP4_GB"
else
  MODEL="$KOLIBRI_FP8_REPO"; MODEL_REV="$KOLIBRI_FP8_REV"; GMU="$KOLIBRI_FP8_GMU"; WEIGHTS_GB="$KOLIBRI_FP8_GB"
fi
# The base image's pull at its peak and the plugin's layer: 22.1 GB unpacked and 9.6 GB to
# download, which the containerd image store (Docker 29's default on a new install) keeps beside
# it, 29.5 GiB; ceil(x 1.02) + 1 as df -BG rounds up (install.sh counts the text lanes' images so).
# It asked for 23, the image alone (found in review, 2026-10-15).
IMAGE_GB=32

# The text lanes' cache and port: Kolibri is one of them for the proxy (vllm-lane.sh).
vlane_engine_env "${KOLIBRI_PORT:-}"

if [ "$ACTION" = uninstall ]; then
  step "Removing the Kolibri-1 lane"
  vlane_uninstall_unit
  vlane_uninstall_clients
  # v1.29's link to the lane before it, if this box still has one (lanes.sh keeps one record now)
  rm -f "$CONFIG_DIR/lane-before-kolibri"
  vlane_uninstall_runtime qwen38-kolibri "$KOLIBRI_BASE_IMAGE" "$KOLIBRI_FP8_REPO" "$KOLIBRI_NVFP4_REPO"
  exit 0
fi

# Step 4, the one part that needs root: the unit and its start scripts. A function, since a
# lane installed on demand (--units-only) takes it straight after the preflight.
install_service(){
  step "4/5 Service"
  # vLLM's compile cache
  vlane_service_files "$CONFIG_DIR/kolibri-cache"
  RENDER="$(mktemp)"; trap 'rm -f "$RENDER"' EXIT
  sed -e "s|__USER__|$(id -un)|g" -e "s|__GROUP__|$(id -gn "$(id -un)")|g" -e "s|__HOME__|$HOME|g" \
      -e "s|__IMAGE__|$LOCAL_IMAGE|g" -e "s|__MODEL__|$MODEL|g" -e "s|__REV__|$MODEL_REV|g" \
      -e "s|__GMU__|$GMU|g" -e "s|__HF_CACHE__|$HF_CACHE|g" \
      -e "s|__ENGINE_BIND__|$ENGINE_BIND|g" -e "s|__PORT__|$PORT|g" \
      "$HERE/$UNIT.template" > "$RENDER"
  vlane_put_unit "$RENDER"
  # Images of this lane built from older pins, once the unit names this one: removed before
  # the checkpoint came, a failed download left the unit naming an image that was gone.
  lane_prune_images qwen38-kolibri "$LOCAL_IMAGE"
  # Installed, not switched to: the lane this box serves stays the lane it serves.
  echo "installed as a lane of its own. Switch to it like any lane:"
  echo "  the cockpit: Load on Kolibri-1 in the Lanes view"
  echo "  a terminal : ./switch-model.sh kolibri (or kolibri-nvfp4), then the commands it prints"
}

step "1/5 Preflight ($VARIANT: $MODEL @ ${MODEL_REV:0:12})"
vlane_preflight engine-preflight.sh engine-secrets.sh vllm_engine_auth.py ckpt-fetch.py
vlane_room "$KOLIBRI_BASE_IMAGE" "$MODEL"
if [ "$MODE" = units ]; then
  echo "OK: $(uname -m), key present; the image and the checkpoint come with the lane's first Load"
  install_service
  step "Done: the Kolibri-1 lane is installed; its first Load prepares it"
  echo "  the cockpit: Load on Kolibri-1 in the Lanes view builds its image (about 23 GB, 10 to download),"
  echo "               fetches its checkpoint (about $WEIGHTS_GB GB), then starts it"
  echo "  a terminal : ./install-kolibri.sh --prepare, then ./switch-model.sh kolibri and the commands it prints"
  exit 0
fi

step "2/5 Image ($LOCAL_IMAGE)"
if docker image inspect "$LOCAL_IMAGE" >/dev/null 2>&1; then
  echo "already built"
else
  docker image inspect "$KOLIBRI_BASE_IMAGE" >/dev/null 2>&1 || docker pull "$KOLIBRI_BASE_IMAGE" \
    || die "docker pull of $KOLIBRI_BASE_IMAGE failed. Causes: no internet, a Docker Hub rate limit (retry in a few minutes, or docker login), or the digest removed upstream."
  BUILD="$(mktemp -d)"
  curl -fsSL --retry 3 -o "$BUILD/$PLUGIN_WHEEL" "$KOLIBRI_PLUGIN_URL" \
    || { rm -rf "$BUILD"; die "could not download the aleph-alpha-inference wheel from PyPI ($KOLIBRI_PLUGIN_URL)"; }
  echo "$KOLIBRI_PLUGIN_SHA256  $BUILD/$PLUGIN_WHEEL" | sha256sum -c --quiet - \
    || { rm -rf "$BUILD"; die "the aleph-alpha-inference wheel does not match the sha256 PyPI published for $KOLIBRI_PLUGIN_VERSION; refusing to build."; }
  # Aleph Alpha's own Dockerfile (aleph-alpha-inference, 1.0.0): the base image plus the
  # wheel with --no-deps, so pip never replaces the vLLM, torch and transformers the base
  # carries. The last line proves vLLM will see the plugin: its entry point is how vLLM
  # finds it in every process it starts.
  cat > "$BUILD/Dockerfile" <<'DEOF'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY *.whl /tmp/
RUN pip install --no-deps /tmp/*.whl && rm /tmp/*.whl \
 && python3 -c "from importlib.metadata import entry_points; assert [e for e in entry_points(group='vllm.general_plugins') if e.name.startswith('aleph')], 'the plugin entry point is missing'"
DEOF
  docker build -q --build-arg BASE_IMAGE="$KOLIBRI_BASE_IMAGE" -t "$LOCAL_IMAGE" "$BUILD" >/dev/null \
    || { rm -rf "$BUILD"; die "the image build failed (the lines above say why)"; }
  rm -rf "$BUILD"
  echo "built from $KOLIBRI_BASE_IMAGE and the plugin's wheel"
fi

if [ "$MODE" = prepare ]; then
  step "Done: the Kolibri-1 lane's image is ready; its Load fetches the checkpoint and starts it"
  exit 0
fi

step "3/5 Checkpoint ($MODEL @ ${MODEL_REV:0:12}, resumable)"
vlane_fetch "$MODEL" "$MODEL_REV"

install_service

if [ "$SMOKE" -eq 0 ]; then step "Done (proof skipped)"; exit 0; fi

step "5/5 Proving it serves (a reply and a tool call, then the box goes back to the lane it was serving)"
# At effort none: at high (the template's default), the kolibri1 reasoning parser cuts the
# reasoning at a <tool_call> the model writes inside it, and the call is missed, on 1.9 to
# 3.5 % of the replies to an English weather request like the proof's (Quantinine, both
# checkpoints); at none they measured every call parsed. A first install must not fail by chance.
VLANE_SERVED=kolibri-1; VLANE_KWARGS='{"reasoning_effort":"none"}'
VLANE_PARSERS="the kolibri1 parsers are"; VLANE_BOOT="about 10 min"; VLANE_WAIT_MIN=20
vlane_prove

step "Done: the Kolibri-1 lane is installed and proved it serves on $ENGINE_BIND:$PORT"
echo "  switch to it : Load on Kolibri-1 in the cockpit, or ./switch-model.sh kolibri (kolibri-nvfp4)"
echo "  clients      : the proxy on :30001 with the box's key, model kolibri-1 (OpenAI and Anthropic APIs)"
