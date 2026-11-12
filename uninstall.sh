#!/usr/bin/env bash
# Removes the qwen38 serving services (27B SGLang and/or Flash-Next) and their config,
# and knows every artifact ANY past version of this repo may have left on the box
# (v1.0 through v1.5): units, drop-ins, backups, local and base docker images
# (tag or digest form), checkpoints, the PLE mmap file, the oc and claude-local launchers.
#
#   ./uninstall.sh --list    inventory only: show what is present and how big it
#                            is, decide for yourself; changes NOTHING, needs no sudo
#   ./uninstall.sh           remove services + (after a prompt) the config;
#                            data (images, checkpoints, PLE file) is never deleted,
#                            the exact reclaim command for each present item is printed
#   ./uninstall.sh --yes     same, config removed without asking
set -euo pipefail
# One spelling of the home, without a trailing slash, as install.sh says why: the cockpit's
# exact-argv sudoers is rendered from it and has to match the cockpit's own calls (2026-10-22).
while [ -n "${HOME:-}" ] && [ "$HOME" != / ] && [ "${HOME%/}" != "$HOME" ]; do HOME="${HOME%/}"; done; export HOME
CONFIG_DIR="$HOME/.config/qwen38"
SYSTEMD_DIR=/etc/systemd/system
SUDOERS_FILE=/etc/sudoers.d/qwen38-cockpit
PYSPY_WRAPPER=/usr/local/bin/qwen38-pyspy-scheduler
# The GPU clock cap the cockpit sets from its Machine view (v1.47.0), and the file it keeps it in to set it
# again at each of its starts: lifted with the cockpit, by the nvidia-smi its sudoers lines name.
GPU_CAP_FILE="$CONFIG_DIR/cockpit-gpu-clock.json"
NVIDIA_SMI=/usr/bin/nvidia-smi
# Every cache and PLE folder the installed lanes use, not just the environment's: a box
# installed with HF_CACHE=/data/hf had 22 to 126 GB of weights and the 48 GB PLE file
# neither listed nor given a reclaim command (found in review, 2026-09-24).
unit_mount(){  # $1 = the container path; prints the host side of every -v mounting it
  grep -hoE -- "-v [^ :]+:$1( |\$|:)" "$SYSTEMD_DIR/qwen38-sglang.service" "$CONFIG_DIR/launch-flash.sh" "$SYSTEMD_DIR/qwen38-kolibri.service" "$SYSTEMD_DIR/qwen38-nemotron.service" "$SYSTEMD_DIR/qwen38-nemotron-super.service" "$SYSTEMD_DIR/qwen38-flash-local.service" "$SYSTEMD_DIR/qwen38-ornith-122b.service" 2>/dev/null \
    | sed -e 's/^-v //' -e "s|:$1.*\$||" | tr -s / || true
}
# Each list one slash at a time before it keeps each once: what a unit wrote from a home with a trailing
# slash, and a folder given from a shell on such a home (PLE_DIR=~/...), say "//".
HF_CACHES="$( { printf '%s\n' "${HF_CACHE:-}"; unit_mount /root/.cache/huggingface
               { grep -m1 -E '^Environment=HF_HOME=' "$SYSTEMD_DIR/qwen38-image.service" 2>/dev/null || true; } | cut -d= -f3-
               { grep -m1 -E '^Environment=HF_HOME=' "$SYSTEMD_DIR/qwen38-video.service" 2>/dev/null || true; } | cut -d= -f3-
               printf '%s\n' "$HOME/.cache/huggingface"; } | tr -s / | awk 'NF && !seen[$0]++')"
PLE_DIRS="$( { printf '%s\n' "${PLE_DIR:-}"; unit_mount /ple; printf '%s\n' "$HOME/flashnext-ple"; } | tr -s / | awk 'NF && !seen[$0]++')"
# The Qwen3.8 Flash-Next FP4-Local lane's copies of its n-gram table (ple-table.py), where its unit mounts them
# from, where FLASH_LOCAL_PLE_DIR puts them (given now, or kept by its installer), and their
# default folder.
FL_TABLE_DIRS="$( { [ "${FLASH_LOCAL_PLE_DIR:-}" = none ] || printf '%s\n' "${FLASH_LOCAL_PLE_DIR:-}"
                    { head -1 "$CONFIG_DIR/flash-local-ple-dir" 2>/dev/null || true; } | grep -vx none
                    { grep -hoE -- "-v [^ :]+:/root/\.cache/huggingface/hub/[^ :]+/blobs/[0-9a-f]{64}:ro" "$SYSTEMD_DIR/qwen38-flash-local.service" 2>/dev/null || true; } \
                      | sed -e 's/^-v //' -e 's|:.*$||' | xargs -r -n1 dirname
                    printf '%s\n' "$HOME/flash-local-ple"; } | tr -s / | awk 'NF && !seen[$0]++')"
# The diffusion runtime's own caches, written at a lane's first start and reused after,
# outside every lane's folder: copies of transformed weights (12 GB for the video lane's
# FL2VA weights) and its cache root (the video decoder's converted weights, 4.6 GB, and the
# compile caches of both lanes).
SPILL_DIR="$HOME/.cache/sglang/diffusion/host_spill"
DIFF_CACHE="$HOME/.cache/sgl_diffusion"
# opencode reads all three global names, and creates opencode.jsonc itself on its first
# start: a provider block pasted into any of them reads the key as much as ours does.
OC_USER_CFGS=("$HOME/.config/opencode/config.json" "$HOME/.config/opencode/opencode.json" "$HOME/.config/opencode/opencode.jsonc")
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LIST_ONLY=0
PURGE_CONFIG=0
for arg in "${@:-}"; do
  case "$arg" in
    --list|-l) LIST_ONLY=1 ;;
    --yes|-y) PURGE_CONFIG=1 ;;
    "") ;;
    -h|--help) echo "Usage: ./uninstall.sh [--list] [--yes]   (--list = inventory only; --yes also deletes ~/.config/qwen38 without asking)"; exit 0 ;;
    *) echo "Unknown flag: $arg (see --help)" >&2; exit 1 ;;
  esac
done
# A terminal opened before the account joined the docker group sees none of its images: the
# inventory below skipped them all (docker info failed) and left them on the disk. It goes on
# with the group (lanes.sh).
die(){ printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
# shellcheck source=lanes.sh
. "$REPO_DIR/lanes.sh"
docker_session bash "$REPO_DIR/${BASH_SOURCE[0]##*/}" "$@"

# ── Inventory: every name this repo has ever created, shown only if present ──
# Local serving images built by install.sh across versions (any tag: v1.2,
# v1.2.2, v1.4, v1.5, ...), plus the pinned base images, matched by tag AND by
# digest: a digest pull leaves no tag behind.
LOCAL_IMAGE_REPOS="qwen38-dflash2 qwen38-flash qwen38-pinned qwen38-kolibri qwen38-nemotron"  # qwen38-dflash2 is the pre-v1.14 27B overlay, retired but still deletable; qwen38-pinned (v1.18.6) tags the digest pulls; qwen38-kolibri is the Kolibri lane's vLLM image with Aleph Alpha's plugin (install-kolibri.sh); qwen38-nemotron is the Nemotron lane's vLLM nightly with b12x (install-nemotron.sh)
BASE_IMAGES="lmsysorg/sglang:v0.5.22 lmsysorg/sglang@sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae lmsysorg/sglang:v0.5.21 lmsysorg/sglang@sha256:b1259f3ea3275f66237c498ea388919729018bc9f01c3d638391e06e2cf3f469 lmsysorg/sglang:v0.5.19 lmsysorg/sglang@sha256:d6e7288627be8b02be88e4bba38e73f6d50e2826869f753c13a4c4385ab3eda9 lmsysorg/sglang:qwen38-27b lmsysorg/sglang@sha256:febfb971c7352570fc445c466ebd6ffc9d896024958e544a60f2137fd85856b1 lmsysorg/sglang:qwen38flashnext lmsysorg/sglang@sha256:12d3392bdc8be8d35e9a95f191df6aef99c5114bdbefd41bfdc7e760e6d25ec1 lmsysorg/sglang@sha256:9d2a843c706c74bc259c0d9abf360551eb2734e1e7d255ab012a6965f10480b6 lmsysorg/sglang@sha256:cb6ed363800a5bea98ac90b4c6447e9ba66cef7db881ac1f108046b6622d22b8 lmsysorg/sglang@sha256:3e79541d1e2a6818979c4aa9ca1fa63d6961d7bc50120867953ea6baaf1d8f62 lmsysorg/sglang@sha256:616a3e97f45191af975896cfa644279096cb31bd408a071c2e99ca7209c3cafe vllm/vllm-openai:qwen38-flash-next vllm/vllm-openai@sha256:fc120ece0a388cc0aa1caad4a9f1cd92113484ab7ec2fd0efadd62585be05bf8 vllm/vllm-openai@sha256:18372a7224938643461b846fb64c5c9d3d6e9727e82caf2dc3043e620c9d4d7a vllm/vllm-openai@sha256:1ee85ca80e0fc110017d79ea2081a0cbef9a208a05d7f22724801bcf6a956620 eugr/spark-vllm-b12x@sha256:03a956544e9f9b4b7ba91690469fbe7848bdbef29a7d2e0f5788368467bb3595"
HF_REPOS="RadixArk/Qwen3.8-27B-NVFP4 edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4 Qwen/Qwen3.8-27B-FP8 edp1096/Huihui-Qwen3.8-27B-abliterated-FP8 RadixArk/Qwen3.8-27B-DSpark z-lab/Qwen3.8-27B-DFlash2 maurienne-ai/Qwen3.8-27B-DFlash2-NVFP4-RTNcal RadixArk/Qwen3.8-Flash-Next-NVFP4 nvidia/Qwen3.8-Flash-Next-NVFP4 dealignai/Qwen3.8-Flash-Next-ABLITERATED-NVFP4 Qwen/Qwen-Image-2.1 Qwen/Qwen-Image-2.1-Turbo MiniMaxAI/MiniMax-H3 larryvrh/MiniMax-H3-Turbo-Lora Aleph-Alpha/Kolibri-1 Quantinine/Kolibri-1-NVFP4A16-FP8 nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB-LiLiCorr nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4 nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4-LiLiCorr nvidia/Qwen3.8-Flash-Next-FP4-Local ornith-ai/Ornith-1.5-122B-NVFP4"

FOUND_IMAGES=()   # "ref|size|every ref", deduplicated by image ID (a tag and its digest are one image)
# Every reference Docker keeps for one image. A digest-pulled image carries its digest
# and, since v1.18.6, a qwen38-pinned tag: removing one of the two only untags it.
image_refs() {
  docker image inspect "$1" --format '{{range .RepoTags}}{{.}} {{end}}{{range .RepoDigests}}{{.}} {{end}}' 2>/dev/null || true
}
inventory_images() {
  command -v docker >/dev/null 2>&1 || return 0
  docker info >/dev/null 2>&1 || return 0
  local seen_ids=" " ref id size
  for repo in $LOCAL_IMAGE_REPOS; do
    while IFS='|' read -r ref id size; do
      [ -n "$ref" ] || continue
      case "$seen_ids" in *" $id "*) continue ;; esac
      seen_ids="$seen_ids$id "
      FOUND_IMAGES+=("$ref|$size|$(image_refs "$ref")")
    done < <(docker images --format '{{.Repository}}:{{.Tag}}|{{.ID}}|{{.Size}}' "$repo" 2>/dev/null || true)
  done
  for ref in $BASE_IMAGES; do
    if docker image inspect "$ref" >/dev/null 2>&1; then
      id="$(docker image inspect "$ref" --format '{{.Id}}' | cut -c8-19)"
      case "$seen_ids" in *" $id "*) continue ;; esac
      seen_ids="$seen_ids$id "
      size="$(docker image inspect "$ref" --format '{{.Size}}' | awk '{printf "%.1fGB", $1/1e9}')"
      FOUND_IMAGES+=("$ref|$size|$(image_refs "$ref")")
    fi
  done
}

dir_size() { du -sh "$1" 2>/dev/null | cut -f1; }
# The Qwen3.8 Flash-Next FP4-Local lane's copies in a folder: the files its list names (ple-table.py keeps it), one
# path a line. Never the folder's other files: FLASH_LOCAL_PLE_DIR can be a folder of the user's own.
fl_copies() { awk -v d="$1" '/^[0-9a-f]{64}$/ {print d "/" $0}' "$1/.qwen38-ple-copies" 2>/dev/null; }
# The reclaim command for a directory: the user's rm, or sudo when a directory in it is not
# theirs to empty. Engine containers of past versions ran as root on the mounted HF cache
# and left root-owned .no_exist and refs entries behind (reference box: three checkpoints,
# 2026-08-28 to 2026-09-12), and a plain rm -rf printed for them stopped on "Permission denied".
rm_cmd() {
  if [ -n "$(find "$1" -type d ! -writable -print -quit 2>/dev/null)" ]; then echo "sudo rm -rf"; else echo "rm -rf"; fi
}

echo "── Inventory (everything any version of this repo may have left here) ──"
for u in qwen38-sglang.service qwen38-flash.service qwen38-keepalive.service qwen38-dashboard.service qwen38-image.service qwen38-video.service qwen38-kolibri.service qwen38-nemotron.service qwen38-nemotron-super.service qwen38-flash-local.service qwen38-ornith-122b.service qwen38-speech.service opencode-web.service; do
  if [ -f "$SYSTEMD_DIR/$u" ]; then
    STATE="$(systemctl is-enabled "$u" 2>/dev/null || true)/$(systemctl is-active "$u" 2>/dev/null || true)"
    echo "  unit      $SYSTEMD_DIR/$u ($STATE)"
  fi
done
[ -d "$SYSTEMD_DIR/qwen38-sglang.service.d" ] && echo "  drop-ins  $SYSTEMD_DIR/qwen38-sglang.service.d (pre-v1.3 warmup lived here)"
[ -d "$SYSTEMD_DIR/qwen38-keepalive.service.d" ] && echo "  drop-ins  $SYSTEMD_DIR/qwen38-keepalive.service.d (switch-model.sh ceiling override)"
[ -d "$SYSTEMD_DIR/qwen38-dashboard.service.d" ] && echo "  drop-ins  $SYSTEMD_DIR/qwen38-dashboard.service.d (cockpit overrides)"
[ -f "$SUDOERS_FILE" ] && echo "  sudoers   $SUDOERS_FILE (cockpit argv allowlist, NOPASSWD)"
[ -f "$PYSPY_WRAPPER" ] && echo "  wrapper   $PYSPY_WRAPPER (cockpit forensics helper)"
# Read from the unit, not assumed: install-image.sh takes IMAGE_LANE_DIR, so a lane
# installed elsewhere would be reported clean and left on disk.
IMAGE_LANE_DIR="${IMAGE_LANE_DIR:-$({ grep -m1 -E '^WorkingDirectory=' "$SYSTEMD_DIR/qwen38-image.service" 2>/dev/null || true; } | cut -d= -f2- | tr -s /)}"
IMAGE_LANE_DIR="${IMAGE_LANE_DIR:-$HOME/.local/share/qwen38-image}"
[ -d "$IMAGE_LANE_DIR" ] && echo "  runtime   $IMAGE_LANE_DIR ($(dir_size "$IMAGE_LANE_DIR"), image lane venv + pinned SGLang checkout)"
# Read from the unit, not assumed: install-video.sh takes VIDEO_LANE_DIR, so a lane
# installed elsewhere would be reported clean and left on disk.
VIDEO_LANE_DIR="${VIDEO_LANE_DIR:-$({ grep -m1 -E '^WorkingDirectory=' "$SYSTEMD_DIR/qwen38-video.service" 2>/dev/null || true; } | cut -d= -f2- | tr -s /)}"
VIDEO_LANE_DIR="${VIDEO_LANE_DIR:-$HOME/.local/share/qwen38-video}"
[ -d "$VIDEO_LANE_DIR" ] && echo "  runtime   $VIDEO_LANE_DIR ($(dir_size "$VIDEO_LANE_DIR"), video lane venv + pinned SGLang checkout)"
# The speech lane (install-speech.sh, SPEECH_LANE_DIR) keeps its models in its own folder,
# not in the HF cache: listed apart, since they stay like every other checkpoint.
SPEECH_LANE_DIR="${SPEECH_LANE_DIR:-$({ grep -m1 -E '^WorkingDirectory=' "$SYSTEMD_DIR/qwen38-speech.service" 2>/dev/null || true; } | cut -d= -f2- | tr -s /)}"
SPEECH_LANE_DIR="${SPEECH_LANE_DIR:-$HOME/.local/share/qwen38-speech}"
if [ -d "$SPEECH_LANE_DIR/venv" ] || [ -d "$SPEECH_LANE_DIR/llama.cpp" ]; then
  echo "  runtime   $SPEECH_LANE_DIR (speech lane venv + llama.cpp CPU build)"
fi
[ -d "$SPEECH_LANE_DIR/models" ] && echo "  weights   $SPEECH_LANE_DIR/models ($(dir_size "$SPEECH_LANE_DIR/models"), speech lane: Qwen3-ASR and Kokoro)"
for f in "$CONFIG_DIR"/*.bak-preupdate; do
  [ -f "$f" ] && echo "  backup    $f (pre-update unit backup)"
done
if [ -d "$CONFIG_DIR" ]; then
  echo "  config    $CONFIG_DIR ($(dir_size "$CONFIG_DIR")): api-key and the engine's copy of it, patched templates, opencode.json, pi/omp agent configs, launch scripts, compile cache"
  [ -f "$CONFIG_DIR/opencode-web.env" ] && echo "  config    $CONFIG_DIR/opencode-web.env (Agent view: credentials of the opencode web server)"
  # since v1.36.0 (2026-10-17): it goes with the folder; kept, a new install reads the box's age in it
  [ -f "$CONFIG_DIR/installed.json" ] && echo "  record    $CONFIG_DIR/installed.json (the release the last ./install.sh installed, and the choices it recorded)"
  [ -f "$CONFIG_DIR/claude-code.env" ] && echo "  legacy    $CONFIG_DIR/claude-code.env (pre-v1.3 client config, unmaintained)"
  # 2026-11-02: a pair (docs/pair.md): the other box keeps this one until it is told. A peer of pair.json
  # pins a certificate (sha256); pair/ alone is this box's certificate, kept for a next pair
  if [ -f "$CONFIG_DIR/pair.json" ] && grep -q '"sha256"' "$CONFIG_DIR/pair.json" 2>/dev/null; then
    echo "  pair      $CONFIG_DIR/pair.json (this box is paired: run pair.py remove first, so that the other box forgets this one)"
  elif [ -d "$CONFIG_DIR/pair" ]; then
    echo "  pair      $CONFIG_DIR/pair (this box's certificate for a pair, made by pair.py)"
  fi
  [ -f "$CONFIG_DIR/opencode.off" ] && echo "  marker    $CONFIG_DIR/opencode.off (opencode integration disabled with --no-opencode)"
  [ -f "$CONFIG_DIR/cockpit.off" ] && echo "  marker    $CONFIG_DIR/cockpit.off (cockpit disabled with --no-cockpit)"
  for _lane in image video flash 27b kolibri nemotron nemotron-super flash-local ornith-122b; do
    [ -f "$CONFIG_DIR/$_lane.off" ] && echo "  marker    $CONFIG_DIR/$_lane.off ($_lane lane left out with --no-$_lane)"
  done
  # named from CONFIG_DIR alone, as every line of this block (GPU_CAP_FILE above is the same file)
  if [ -f "$CONFIG_DIR/cockpit-gpu-clock.json" ]; then
    _cap="$({ sed -n 's/.*"cap": *"\([0-9]*\)".*/\1/p' "$CONFIG_DIR/cockpit-gpu-clock.json" 2>/dev/null || true; } | head -1)"
    echo "  setting   $CONFIG_DIR/cockpit-gpu-clock.json (the cockpit's GPU clock cap${_cap:+, $_cap MHz}, set again at each of its starts: lifted with the cockpit)"
  fi
fi
if grep -q 'dgx-spark-qwen38' "$HOME/.local/bin/oc" 2>/dev/null; then
  echo "  launcher  $HOME/.local/bin/oc (this repo's opencode launcher)"
fi
if grep -q 'dgx-spark-qwen38' "$HOME/.local/bin/claude-local" 2>/dev/null; then
  echo "  launcher  $HOME/.local/bin/claude-local (this repo's Claude Code launcher)"
fi
# the launcher #59's reply gave, which an install moved aside: the user's, listed, never removed
# (found in review, 2026-10-24)
if [ -e "$HOME/.local/bin/claude-local.from-59" ]; then
  echo "  kept      $HOME/.local/bin/claude-local.from-59 (your copy of the launcher #59's reply gave, which an install moved aside: yours to delete)"
fi
# install.sh installs opencode itself only when a box had none; it marks that in the
# PATH line it adds to ~/.bashrc. Listed, never removed: from then on it is the user's
# opencode as much as this repo's, with its own sessions under ~/.local/share/opencode.
if [ -x "$HOME/.opencode/bin/opencode" ] && grep -qs 'opencode, installed by dgx-spark-qwen38' "$HOME/.bashrc"; then
  echo "  opencode  $HOME/.opencode (installed by this repo; kept. Yours to remove: rm -rf ~/.opencode, and its PATH line in ~/.bashrc)"
fi
for f in "${OC_USER_CFGS[@]}"; do
  if grep -qsF '.config/qwen38/api-key' "$f"; then
    echo "  opencode  $f (opencode's own config: lists providers that read this box's API key)"
  fi
done
inventory_images
for entry in ${FOUND_IMAGES[@]+"${FOUND_IMAGES[@]}"}; do
  IFS='|' read -r ref size _ <<< "$entry"
  echo "  image     $ref ($size)"
done
while IFS= read -r cache; do
  for repo in $HF_REPOS; do
    d="$cache/hub/models--${repo//\//--}"
    if [ -d "$d" ]; then echo "  weights   $d ($(dir_size "$d"))"; fi
  done
done <<< "$HF_CACHES"
while IFS= read -r p; do
  if [ -n "$p" ] && [ -d "$p" ]; then echo "  ple-file  $p ($(dir_size "$p"), flash mmap backing store)"; fi
done <<< "$PLE_DIRS"
while IFS= read -r p; do
  if [ -n "$p" ] && [ -f "$p/.qwen38-ple-copies" ]; then
    echo "  table     $p ($(fl_copies "$p" | xargs -r du -ch 2>/dev/null | tail -1 | cut -f1), the Qwen3.8 Flash-Next FP4-Local lane's copies of its n-gram table)"
  fi
done <<< "$FL_TABLE_DIRS"
if [ -d "$SPILL_DIR" ]; then echo "  spill     $SPILL_DIR ($(dir_size "$SPILL_DIR"), the diffusion runtime's transformed weights)"; fi
if [ -d "$DIFF_CACHE" ]; then echo "  cache     $DIFF_CACHE ($(dir_size "$DIFF_CACHE"), the diffusion runtime's converted weights and compile caches)"; fi
echo "──"

if [ "$LIST_ONLY" -eq 1 ]; then
  echo "Inventory only: nothing was changed. Remove services+config with ./uninstall.sh;"
  echo "data (images, weights, PLE file) always stays until you run the printed commands."
  exit 0
fi

PRIV=0
for f in "$SYSTEMD_DIR/qwen38-sglang.service" "$SYSTEMD_DIR/qwen38-flash.service" \
         "$SYSTEMD_DIR/qwen38-keepalive.service" "$SYSTEMD_DIR/qwen38-dashboard.service" \
         "$SYSTEMD_DIR/qwen38-image.service" "$SYSTEMD_DIR/qwen38-video.service" "$SYSTEMD_DIR/qwen38-kolibri.service" \
         "$SYSTEMD_DIR/qwen38-nemotron.service" "$SYSTEMD_DIR/qwen38-nemotron-super.service" "$SYSTEMD_DIR/qwen38-flash-local.service" "$SYSTEMD_DIR/qwen38-ornith-122b.service" "$SYSTEMD_DIR/qwen38-speech.service" "$SYSTEMD_DIR/opencode-web.service" \
         "$SYSTEMD_DIR/qwen38-sglang.service.d" "$SYSTEMD_DIR/qwen38-dashboard.service.d" \
         "$SYSTEMD_DIR/qwen38-keepalive.service.d" "$SUDOERS_FILE" "$PYSPY_WRAPPER" "$GPU_CAP_FILE"; do
  [ -e "$f" ] && PRIV=1
done
# What only root can remove, asked for only when some of it is there. A --no-service box has
# none of it and is the one made for boxes without sudo: sudo was called anyway, and the
# first refusal ended this script under set -e before it removed any of the user's own files
# (found in review, 2026-09-24). Refused, the services are left running and untouched, since
# a container removed under a unit that keeps running is started again by systemd.
PRIV_DONE=0
if [ "$PRIV" -eq 1 ]; then
  if sudo -v; then
    for u in qwen38-sglang qwen38-flash qwen38-keepalive qwen38-dashboard qwen38-image qwen38-video qwen38-kolibri qwen38-nemotron qwen38-nemotron-super qwen38-flash-local qwen38-ornith-122b qwen38-speech opencode-web; do
      sudo systemctl disable --now "$u.service" 2>/dev/null || true
    done
    docker rm -f qwen38-sglang qwen38-sglang-run qwen38-flash qwen38-kolibri qwen38-nemotron qwen38-nemotron-super qwen38-flash-local qwen38-ornith-122b 2>/dev/null || true
    # The GPU clock cap the cockpit set, lifted now that the cockpit is stopped (it can no longer set it
    # again) and before its sudoers lines go: left, it held until the next reboot with nothing to say it.
    # Only a cap the cockpit keeps: one set by hand outside it is the operator's.
    if [ -f "$GPU_CAP_FILE" ]; then
      if sudo "$NVIDIA_SMI" -rgc >/dev/null 2>&1; then
        echo "GPU clock cap lifted."
      else
        echo "NOTE: nvidia-smi did not lift the GPU clock cap: the next reboot does, or sudo $NVIDIA_SMI -rgc now."
      fi
      rm -f "$GPU_CAP_FILE"
    fi
    sudo rm -f "$SYSTEMD_DIR/qwen38-sglang.service" "$SYSTEMD_DIR/qwen38-flash.service" "$SYSTEMD_DIR/qwen38-keepalive.service" "$SYSTEMD_DIR/qwen38-dashboard.service" "$SYSTEMD_DIR/qwen38-image.service" "$SYSTEMD_DIR/qwen38-video.service" "$SYSTEMD_DIR/qwen38-kolibri.service" "$SYSTEMD_DIR/qwen38-nemotron.service" "$SYSTEMD_DIR/qwen38-nemotron-super.service" "$SYSTEMD_DIR/qwen38-flash-local.service" "$SYSTEMD_DIR/qwen38-ornith-122b.service" "$SYSTEMD_DIR/qwen38-speech.service" "$SYSTEMD_DIR/opencode-web.service"
    sudo rm -rf "$SYSTEMD_DIR/qwen38-sglang.service.d" "$SYSTEMD_DIR/qwen38-dashboard.service.d" "$SYSTEMD_DIR/qwen38-keepalive.service.d"
    # The cockpit's privileged surface goes with it: a NOPASSWD allowlist left behind
    # after an uninstall is the one leftover that is not merely clutter.
    sudo rm -f "$SUDOERS_FILE" "$PYSPY_WRAPPER"
    sudo systemctl daemon-reload
    PRIV_DONE=1
  else
    echo "NOTE: sudo was refused, so the services, their units and the cockpit's sudoers entry stay,"
    echo "      running as they were. Re-run ./uninstall.sh where sudo works to remove them."
  fi
else
  docker rm -f qwen38-sglang-run 2>/dev/null || true      # ./run.sh's foreground engine, if one was left
fi
# The image lane's runtime is the user's, not root's: no sudo, and the 31 GiB checkpoint
# stays in the HF cache with every other checkpoint, which this script reports separately.
# Only what install-image.sh put there (venv/ and sglang/): IMAGE_LANE_DIR can be a
# directory shared with other work, and removing it whole took the rest with it (found in
# review, 2026-09-24). The directory itself goes only once it is empty.
# Left alone when sudo was refused: the image lane may still be running from it.
if [ -d "$IMAGE_LANE_DIR" ] && { [ "$PRIV" -eq 0 ] || [ "$PRIV_DONE" -eq 1 ]; }; then
  rm -rf "$IMAGE_LANE_DIR/venv" "$IMAGE_LANE_DIR/sglang" "$IMAGE_LANE_DIR/sglang-wheel"
  rmdir "$IMAGE_LANE_DIR" 2>/dev/null || echo "kept $IMAGE_LANE_DIR: it holds files the image lane did not put there"
fi
# The video lane's runtime, under the same rule: only venv/, sglang/ and the notes of the wheel
# and the commit they hold, and the directory itself only once it is empty. Its outputs/ folder
# goes when it is empty: every install makes it, a lane installed on demand and never loaded
# included, and it kept the directory with a line that called it someone else's (found by the
# install matrix, 2026-10-17). The videos in it stay, as they always did, and are said to.
if [ -d "$VIDEO_LANE_DIR" ] && { [ "$PRIV" -eq 0 ] || [ "$PRIV_DONE" -eq 1 ]; }; then
  rm -rf "$VIDEO_LANE_DIR/venv" "$VIDEO_LANE_DIR/sglang" "$VIDEO_LANE_DIR/sglang-wheel" "$VIDEO_LANE_DIR/sglang-source"
  rmdir "$VIDEO_LANE_DIR/outputs" 2>/dev/null || true
  if [ -d "$VIDEO_LANE_DIR/outputs" ]; then
    echo "kept $VIDEO_LANE_DIR/outputs: the videos the lane generated (delete them by hand: rm -r $VIDEO_LANE_DIR/outputs)"
  fi
  rmdir "$VIDEO_LANE_DIR" 2>/dev/null || [ -d "$VIDEO_LANE_DIR/outputs" ] \
    || echo "kept $VIDEO_LANE_DIR: it holds files the video lane did not put there"
fi
# The speech lane's runtime, under the same rule: venv/, llama.cpp/ and the routes' script.
# Its models/ stay, with a reclaim command below, as every other checkpoint does.
if [ -d "$SPEECH_LANE_DIR" ] && { [ "$PRIV" -eq 0 ] || [ "$PRIV_DONE" -eq 1 ]; }; then
  rm -rf "$SPEECH_LANE_DIR/venv" "$SPEECH_LANE_DIR/llama.cpp"
  rm -f "$SPEECH_LANE_DIR/speech-server.py"
  [ -d "$SPEECH_LANE_DIR/models" ] || rmdir "$SPEECH_LANE_DIR" 2>/dev/null \
    || echo "kept $SPEECH_LANE_DIR: it holds files the speech lane did not put there"
fi
# The oc and claude-local launchers, only if they are ours (never a foreign oc binary)
if grep -q 'dgx-spark-qwen38' "$HOME/.local/bin/oc" 2>/dev/null; then
  rm -f "$HOME/.local/bin/oc"
fi
if grep -q 'dgx-spark-qwen38' "$HOME/.local/bin/claude-local" 2>/dev/null; then
  rm -f "$HOME/.local/bin/claude-local"
fi
if [ "$PRIV_DONE" -eq 1 ]; then
  echo "services removed."
elif [ "$PRIV" -eq 0 ]; then
  echo "no service of this repo was installed: nothing to stop or remove there."
fi

# Nor the config, whose API key the services still running read.
if [ "$PRIV" -eq 1 ] && [ "$PRIV_DONE" -eq 0 ]; then
  PURGE_CONFIG=0; KEPT_SAID=1
  echo "config kept at ~/.config/qwen38: the services still running read the API key in it."
elif [ "$PURGE_CONFIG" -eq 0 ] && [ -t 0 ]; then
  read -r -p "Also delete ~/.config/qwen38 (API key, patched templates, compile cache)? opencode's config then loses this box's providers. [y/N] " ans
  { [ "${ans:-n}" = "y" ] || [ "${ans:-n}" = "Y" ]; } && PURGE_CONFIG=1 || true
fi
if [ "$PURGE_CONFIG" -eq 1 ]; then
  # opencode refuses to start at all, every provider included, while a {file:} reference
  # points at a file that is gone, and the providers install.sh writes read the API key
  # this deletes (reference box, 2026-09-23: "bad file reference"). They go first.
  for f in "${OC_USER_CFGS[@]}"; do
    if ! grep -qsF '.config/qwen38/api-key' "$f"; then continue; fi
    python3 "$REPO_DIR/oc-merge-limits.py" "$f" --remove-providers "$CONFIG_DIR/api-key" \
      || echo "NOTE: $f still reads $CONFIG_DIR/api-key: remove those providers by hand, or opencode will not start."
  done
  # The engine container runs as root and writes its compile cache here (the
  # .../sglang-cache:/cache mount), in directories that are root's: a plain rm died on
  # the first of them under set -e, a thousand "Permission denied" lines in, before
  # the reclaim commands (reference box, 2026-09-23). What it cannot remove, sudo does.
  case "$CONFIG_DIR" in */.config/qwen38) ;; *) echo "refusing to delete $CONFIG_DIR" >&2; exit 1 ;; esac
  if rm -rf "$CONFIG_DIR" 2>/dev/null || sudo rm -rf --one-file-system "$CONFIG_DIR"; then
    echo "config removed."
  else
    echo "NOTE: part of $CONFIG_DIR belongs to root (an engine's compile cache) and sudo was refused:"
    echo "      remove it where sudo works: sudo rm -rf --one-file-system $CONFIG_DIR"
  fi
else
  [ "${KEPT_SAID:-0}" -eq 1 ] || echo "config kept at ~/.config/qwen38 (delete manually or re-run with --yes)."
  for f in "${OC_USER_CFGS[@]}"; do
    if grep -qsF '.config/qwen38/api-key' "$f"; then
      echo "opencode's config ($f) still lists this box's providers: they answer again after ./install.sh."
    fi
  done
fi

echo
echo "To also reclaim disk space, run the commands for what the inventory found:"
for entry in ${FOUND_IMAGES[@]+"${FOUND_IMAGES[@]}"}; do
  IFS='|' read -r ref size refs <<< "$entry"
  cmd="docker rmi"
  for r in ${refs:-$ref}; do cmd="$cmd '$r'"; done
  echo "  $cmd    # $size"
done
# An image with no tag at all is dangling to Docker, and prune deletes it: the digest pulls
# of installs before v1.18.6, which tags them. Said only when one is actually there.
UNTAGGED=0
for entry in ${FOUND_IMAGES[@]+"${FOUND_IMAGES[@]}"}; do
  IFS='|' read -r ref _ _ <<< "$entry"
  if [ "$(docker image inspect "$ref" --format '{{len .RepoTags}}' 2>/dev/null || echo 1)" = "0" ]; then UNTAGGED=1; fi
done
if [ "$UNTAGGED" -eq 1 ]; then
  echo "  # never 'docker image prune' on this box: an image above with no tag looks dangling"
  echo "  # and prune deletes it (then a re-pull of 15 to 16 GB before the lane reboots); ./install.sh tags them"
fi
while IFS= read -r cache; do
  for repo in $HF_REPOS; do
    d="$cache/hub/models--${repo//\//--}"
    if [ -d "$d" ]; then echo "  $(rm_cmd "$d") '$d'    # $(dir_size "$d")"; fi
  done
done <<< "$HF_CACHES"
if [ -d "$SPEECH_LANE_DIR/models" ]; then
  echo "  rm -rf '$SPEECH_LANE_DIR/models'    # $(dir_size "$SPEECH_LANE_DIR/models"), speech lane models"
fi
while IFS= read -r p; do
  # an if, not an && list: as the last command of the script, a false test made the whole
  # uninstall exit 1 on a box without a PLE folder
  if [ -n "$p" ] && [ -d "$p" ]; then echo "  $(rm_cmd "$p") '$p'    # $(dir_size "$p"), flash PLE backing file"; fi
done <<< "$PLE_DIRS"
while IFS= read -r p; do
  if [ -n "$p" ] && [ -f "$p/.qwen38-ple-copies" ]; then
    echo "  rm -f $(fl_copies "$p" | sed "s/.*/'&' '&.part'/" | tr '\n' ' ')'$p/.qwen38-ple-copies'; rmdir '$p'    # the Qwen3.8 Flash-Next FP4-Local lane's copies of its n-gram table, and nothing else there"
  fi
done <<< "$FL_TABLE_DIRS"
if [ -d "$SPILL_DIR" ]; then echo "  $(rm_cmd "$SPILL_DIR") '$SPILL_DIR'    # $(dir_size "$SPILL_DIR"), the diffusion runtime's transformed weights"; fi
if [ -d "$DIFF_CACHE" ]; then echo "  $(rm_cmd "$DIFF_CACHE") '$DIFF_CACHE'    # $(dir_size "$DIFF_CACHE"), the diffusion runtime's converted weights and compile caches"; fi
