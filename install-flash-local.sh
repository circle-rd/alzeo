#!/usr/bin/env bash
# Install the Qwen3.8 Flash-Next FP4-Local lane: NVIDIA's own build of Qwen3.8-Flash-Next for a local GPU
# (nvidia/Qwen3.8-Flash-Next-FP4-Local: its experts in NVFP4 and its other layers in MXFP8, by
# NVIDIA's quantization-aware distillation, its n-gram embedding table in NVFP4 too), served by
# the community b12x vLLM image its model card names for a DGX Spark, on the text lanes' port
# behind the proxy and its key. Idempotent, and safe to run alone.
#
#   ./install-flash-local.sh                 first install, or update in place
#   ./install-flash-local.sh --no-smoke      skip the proof at the end
#   ./install-flash-local.sh --uninstall     remove the unit (the checkpoint and the image kept, said)
#   ./install-flash-local.sh --units-only    the unit alone: no image, no checkpoint (install.sh, on demand)
#   ./install-flash-local.sh --prepare       its image, pulled with no sudo: the cockpit's first Load
#                                            runs it (./prepare-lane.sh flash-local)
#   ./install-flash-local.sh --room          the room its table's copies need, said before a download
#   ./install-flash-local.sh --stage FILE    the unit for the checkpoint in the cache, written to FILE
#                                            with no sudo, its chat template and its table made ready
#                                            first: switch-model.sh flash-local installs FILE
#
# THE N-GRAM TABLE is read at every token, from the checkpoint's files: a cache on another disk than
# the home directory's (an external SSD) gets copies of the table's files beside the home directory,
# in ~/flash-local-ple, which the unit mounts over the cache's own (ple-table.py says why and how);
# FLASH_LOCAL_PLE_DIR=<folder> puts them elsewhere (outside the cache), FLASH_LOCAL_PLE_DIR=none reads
# the table where the cache is, and the choice holds for every run after, a Load from the cockpit
# included (FLASH_LOCAL_PLE_DIR= brings the default back). The copies the lane made are listed in
# their folder, and they alone are ever removed there.
#
# install.sh puts this lane's unit in on every plain install, like the other lanes, and its first
# Load from the cockpit pulls its image and fetches its checkpoint; ./install.sh --no-flash-local
# leaves it out of the run that passes it. It takes a checkpoint of 102 GB and an image
# of 18 GB (10 to download): when the disk does not have the room, this refuses before downloading
# anything.
#
# WHY A LANE OF ITS OWN. The flash lane serves Qwen3.8-Flash-Next on SGLang, from checkpoints whose
# n-gram table is FP8 (51 GB, written again beside the cache at every boot). NVIDIA's FP4-Local
# build packs the table in NVFP4 (29 GB) inside the checkpoint's own files, read in place over
# io_uring at every token, in a format only this image reads (vLLM with b12x): 33 GB less on disk,
# 10 GiB less of weights in memory, and a KV cache of 921,090 tokens at the card's memory share
# (measured on the reference box, 2026-10-19; docs/flash-local-lane.md).
#
# ONE ENGINE AT A TIME. The unit says so with Conflicts=: starting it stops whichever lane
# serves, and starting another lane stops it.
set -euo pipefail
# One spelling of the home, without a trailing slash, as install.sh says why: the cockpit's
# exact-argv sudoers is rendered from it and has to match the cockpit's own calls (2026-10-22).
while [ -n "${HOME:-}" ] && [ "$HOME" != / ] && [ "${HOME%/}" != "$HOME" ]; do HOME="${HOME%/}"; done; export HOME
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=qwen38-flash-local.service
# where systemd reads the units, the one variable lanes.sh and vllm-lane.sh read the other units by
INSTALLED="${LANES_UNIT_DIR:-/etc/systemd/system}/$UNIT"
CONTAINER=qwen38-flash-local
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
  --room) [ "$MODE" = full ] || die "--room is a mode of its own"; MODE=room ;;
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
[ "$ACTION" = uninstall ] || [ "$(box_mem_class)" != 64 ] || lane_fits flash-local || die "The Qwen3.8 Flash-Next FP4-Local lane needs a 128 GB DGX Spark, and this box is a 64 GB one."
# A terminal opened before the account joined the docker group: the run goes on with it (lanes.sh).
docker_session bash "$HERE/${BASH_SOURCE[0]##*/}" "$@"
# what the vLLM lanes' installers share: the engine's cache and port, the uninstall, the service,
# the preflight and the room, the fetch, the proof
# shellcheck source=vllm-lane.sh
. "$HERE/vllm-lane.sh"
VLANE=flash-local; VLANE_SHORT="Qwen3.8 Flash-Next FP4-Local"

# The pins, at column 0 like install.sh's: the checkpoint at its revision of 2026-10-15 19:45 UTC,
# the last one (checked on 2026-10-20: every revision since the weights came, on 2026-10-08, changes
# its model card only, file by file), and the image its model card names for a DGX Spark, by the
# digest measured (arm64, built 2026-10-15). The memory share and the requests at a time are the
# card's, the download in GB. switch-model.sh reads them here.
FLASH_LOCAL_REPO="nvidia/Qwen3.8-Flash-Next-FP4-Local"
FLASH_LOCAL_REV="b9ce18a6887e18c089fdea8509a33e63429a3300"
FLASH_LOCAL_IMAGE="eugr/spark-vllm-b12x@sha256:03a956544e9f9b4b7ba91690469fbe7848bdbef29a7d2e0f5788368467bb3595"
FLASH_LOCAL_GMU="0.8"
FLASH_LOCAL_GB="103"
FLASH_LOCAL_SEQS="8"
# the seven files that hold the n-gram table at this revision, 32.31 GB, rounded up: what their copies
# take on the disk of the home directory when the cache is on another (ple-table.py room)
FLASH_LOCAL_TABLE_GB="33"
MODEL="$FLASH_LOCAL_REPO"; MODEL_REV="$FLASH_LOCAL_REV"
GMU="$FLASH_LOCAL_GMU"; WEIGHTS_GB="$FLASH_LOCAL_GB"; SEQS="$FLASH_LOCAL_SEQS"
# The image is pulled as published, by digest: nothing is built on the box. vlane_fetch fetches
# the checkpoint in it, as the other vLLM lanes fetch theirs in their own image.
LOCAL_IMAGE="$FLASH_LOCAL_IMAGE"
# Its pull at its peak: 18.2 GB unpacked and 9.7 GB to download, which the containerd image store
# (Docker 29's default on a new install) keeps beside it, 26.0 GiB; ceil(x 1.02) + 1 as df -BG
# rounds up (install-kolibri.sh counts its image so).
IMAGE_GB=28

# The text lanes' cache and port: FP4-Local is one of them for the proxy (vllm-lane.sh).
vlane_engine_env "${FLASH_LOCAL_PORT:-}"

if [ "$ACTION" = uninstall ]; then
  step "Removing the Qwen3.8 Flash-Next FP4-Local lane"
  # The folders the copies of the n-gram table can be in, read before the unit goes: the one the unit
  # mounts them from, the one chosen (FLASH_LOCAL_PLE_DIR, kept) and the default one. In each, the
  # copies ple-table.py lists as its own go, and nothing else (Hasan's choice, 2026-10-20: they go with
  # the lane; the checkpoint they were copied from stays). Each once, read one slash at a time: a folder
  # given from a shell on a home with a trailing slash (FLASH_LOCAL_PLE_DIR=~/...) says "//".
  TABLE_DIRS="$( { { grep -hoE -- "-v [^ :]+:/root/\.cache/huggingface/hub/[^ :]+/blobs/[0-9a-f]{64}:ro" "$INSTALLED" 2>/dev/null || true; } \
                     | sed -e 's/^-v //' -e 's|:.*$||' -e 's|/[0-9a-f]\{64\}$||'
                   { head -1 "$CONFIG_DIR/flash-local-ple-dir" 2>/dev/null || true; } | grep -vx none
                   printf '%s\n' "$HOME/flash-local-ple"; } | tr -s / | awk 'NF && !seen[$0]++')"
  vlane_uninstall_unit
  vlane_uninstall_clients
  # The image is pulled, not built: no build of it to prune (qwen38-flash-local names none), and
  # its local tag goes alone (lanes.sh). Pruning the repository the tags live in, qwen38-pinned,
  # would have untagged every lane's pin, none of which a unit names by that tag.
  vlane_uninstall_runtime qwen38-flash-local "$FLASH_LOCAL_IMAGE" "$FLASH_LOCAL_REPO"
  pin_untag flash-local
  rm -f "$CONFIG_DIR/chat-template-flash-local.jinja" "$CONFIG_DIR/flash-local-ple-dir" "$CONFIG_DIR/flash-local-files.sh" \
        "$CONFIG_DIR/flash-local-context" "$CONFIG_DIR/flash-local-yarn.json"
  # The seccomp profile is the Ornith lane's too: it stays while that lane's unit reads it.
  if [ -f "$LANES_UNIT_DIR/qwen38-ornith-122b.service" ]; then
    echo "seccomp-io-uring.json kept: the Ornith 1.5 122B lane reads it too"
  else
    rm -f "$CONFIG_DIR/seccomp-io-uring.json"
  fi
  while IFS= read -r d; do
    [ -f "$d/.qwen38-ple-copies" ] || continue
    # the copies by name (blobs named by their sha256), the list, then their folder if nothing else is in it
    n=0
    while IFS= read -r c; do
      if [[ "$c" =~ ^[0-9a-f]{64}$ ]]; then rm -f "$d/$c" "$d/$c.part"; n=$((n + 1)); fi
    done < "$d/.qwen38-ple-copies"
    rm -f "$d/.qwen38-ple-copies"
    rmdir "$d" 2>/dev/null || true
    echo "the n-gram table's copies removed from $d ($n files)"
  done <<< "$TABLE_DIRS"
  exit 0
fi

# The checkpoint's own chat template, with this box's fixes (patch-template.py) and the default its
# model card ships, xhigh: Hasan's choice for this lane (2026-10-19), the lean level offered beside
# it. Written again at every install and every switch; left as it is, mtime included, when nothing
# changed.
TEMPLATE="$CONFIG_DIR/chat-template-flash-local.jinja"
TEMPLATE_FIXES=(LEAN_DEFAULT=0)
lane_template(){ vlane_template "$TEMPLATE" "${TEMPLATE_FIXES[@]}"; }

# The n-gram table: where the engine reads it, copied first when the cache is on another disk than
# the home directory's (ple-table.py). TABLE_MOUNTS is the unit's -v arguments for the copies, empty
# when the table is read where the cache is. Without a checkpoint in the cache (--units-only on a
# new box) the unit names no copy; the Load's switch makes them.
# FLASH_LOCAL_PLE_DIR is kept for the runs after, in a file of its own, as install.sh keeps PLE_DIR:
# a Load from the cockpit stages the unit with none of the user's environment, and without the
# choice it read the table where the cache is, or copied it again to ~/flash-local-ple (found in
# review, 2026-10-20). Given again it replaces the one kept, given empty the default comes back. A
# folder is kept whole: docker run -v takes a host path only from the root.
PLE_CHOICE="$CONFIG_DIR/flash-local-ple-dir"
if [ -n "${FLASH_LOCAL_PLE_DIR+set}" ]; then
  case "$FLASH_LOCAL_PLE_DIR" in ""|none) ;; *) FLASH_LOCAL_PLE_DIR="$(realpath -m -- "$FLASH_LOCAL_PLE_DIR")" ;; esac
  # refused before it is kept: kept first, a folder ple-table.py refuses stayed the choice of every
  # Load after, each failing the same way (found in review, 2026-10-20)
  python3 "$HERE/ple-table.py" "$HF_CACHE" "$MODEL" "$MODEL_REV" --home "$HOME" --dir "$FLASH_LOCAL_PLE_DIR" check \
    || die "FLASH_LOCAL_PLE_DIR=$FLASH_LOCAL_PLE_DIR is not kept (the line above says why)"
  if [ -n "$FLASH_LOCAL_PLE_DIR" ]; then
    mkdir -p "$CONFIG_DIR" && printf '%s\n' "$FLASH_LOCAL_PLE_DIR" > "$PLE_CHOICE"
  else
    rm -f "$PLE_CHOICE"
  fi
elif [ -s "$PLE_CHOICE" ]; then
  FLASH_LOCAL_PLE_DIR="$(head -1 "$PLE_CHOICE")"
fi
TABLE_DIR_ARG=(--dir "${FLASH_LOCAL_PLE_DIR:-}")
# FLASH_LOCAL_CONTEXT=1m: a window of 1,048,576 tokens by YaRN, a factor of 4 over the native 262,144 with the model's
# multimodal RoPE kept, as measured on the reference box (2026-11-09, discussion #63): ready in 406 s, a KV pool of
# 1,061,819 tokens at the lane's share, a passphrase retrieved 8 times of 8 up to 1,036,576 tokens (608 s to the first
# token there), at least 15.1 GiB of memory left. Kept for the runs after in a file of its own, as the PLE folder is:
# a Load from the cockpit stages the unit with none of the user's environment. native (or empty) brings 262,144 back.
# The choice is read here and kept by the render that takes it (render_unit): a refused one is not kept.
CONTEXT_CHOICE="$CONFIG_DIR/flash-local-context"; YARN_JSON="$CONFIG_DIR/flash-local-yarn.json"; CONTEXT_GIVEN=""
if [ -n "${FLASH_LOCAL_CONTEXT+set}" ]; then
  case "$FLASH_LOCAL_CONTEXT" in
    1m) ;;
    ""|native) FLASH_LOCAL_CONTEXT=native ;;
    *) die "FLASH_LOCAL_CONTEXT=$FLASH_LOCAL_CONTEXT: native (262,144 tokens, the default) or 1m (1,048,576 by YaRN)" ;;
  esac
  CONTEXT_GIVEN=1
elif [ "$(head -1 "$CONTEXT_CHOICE" 2>/dev/null)" = 1m ]; then
  FLASH_LOCAL_CONTEXT=1m
else
  FLASH_LOCAL_CONTEXT=native
fi
# The overrides the 1M window takes, from the pinned checkpoint's own RoPE, written to YARN_JSON only when they change
# (its date tells a lane that serves whether it runs them, vlane_put_unit). 0: written, 1: refused (the RoPE is not the
# one measured, or a stage has no checkpoint), 2: no checkpoint in the cache yet, which a Load fetches.
flash_local_yarn(){
  local cfg="$HF_CACHE/hub/models--${MODEL//\//--}/snapshots/$MODEL_REV/config.json"
  if [ ! -f "$cfg" ]; then
    [ "$MODE" != stage ] || { echo "FLASH_LOCAL_CONTEXT=1m: no config.json in $(dirname "$cfg")" >&2; return 1; }
    return 2
  fi
  python3 - "$cfg" "$YARN_JSON" <<'PY' || return 1
import json, sys
c = json.load(open(sys.argv[1])); t = c.get("text_config", c); r = dict(t.get("rope_parameters") or {})
want = {"rope_type": "default", "mrope_interleaved": True, "mrope_section": [11, 11, 10],
        "partial_rotary_factor": 0.25, "rope_theta": 10000000}
if t.get("max_position_embeddings") != 262144 or any(r.get(k) != v for k, v in want.items()):
    sys.exit("FLASH_LOCAL_CONTEXT=1m: the pinned checkpoint's RoPE is not the one the 1M window was measured on")
r.update(rope_type="yarn", factor=4.0, original_max_position_embeddings=262144)
text = json.dumps({"text_config": {"rope_parameters": r, "max_position_embeddings": 1048576}}, separators=(",", ":"))
try:
    same = open(sys.argv[2]).read() == text
except OSError:
    same = False
if not same:
    with open(sys.argv[2] + ".new", "w") as f:
        f.write(text)
    import os
    os.replace(sys.argv[2] + ".new", sys.argv[2])
PY
}
TABLE_MOUNTS=""
lane_table(){  # $1 copy: make the copies whole first; mounts: only name the copies already there
  local rc=0
  if [ "$1" = copy ]; then
    python3 "$HERE/ple-table.py" "$HF_CACHE" "$MODEL" "$MODEL_REV" --home "$HOME" "${TABLE_DIR_ARG[@]}" copy \
      || die "the n-gram table could not be put on the home directory's disk (the lines above say why)"
  fi
  TABLE_MOUNTS="$(python3 "$HERE/ple-table.py" "$HF_CACHE" "$MODEL" "$MODEL_REV" --home "$HOME" "${TABLE_DIR_ARG[@]}" mounts 2>/dev/null)" || rc=$?
  case "$rc" in
    0) ;;
    2) TABLE_MOUNTS="" ;;   # no checkpoint in the cache yet
    *) TABLE_MOUNTS=""
       echo "NOTE: the n-gram table's copies are not all there: the lane reads the table from the cache's disk until its next Load (or ./switch-model.sh flash-local) makes them" ;;
  esac
}

# The unit, rendered for this box: written to $1. Its start checks what the engine reads before it runs: the
# checkpoint's index at the pinned revision, and each copy of the table its mounts name.
render_unit(){
  local maxlen=262144 yarn="" rc=0
  if [ "$FLASH_LOCAL_CONTEXT" = 1m ]; then
    flash_local_yarn || rc=$?
    case "$rc" in
      0) maxlen=1048576
         # read by the unit's start from its file: the JSON on the unit's own line would go through systemd's quoting
         yarn="--hf-overrides \"\$\$(cat $YARN_JSON)\"" ;;
      2) echo "NOTE: FLASH_LOCAL_CONTEXT=1m: the checkpoint is not in the cache yet, so the unit keeps the native window" \
              "until the lane's Load, which fetches it and sets the 1M window" ;;
      *) die "the 1M window's overrides could not be written (the line above says why). Nothing was changed: the lane" \
             "keeps its window, and FLASH_LOCAL_CONTEXT=native ./install-flash-local.sh --units-only sets the native one" ;;
    esac
  fi
  RENDERED_MAXLEN="$maxlen"
  local index="$HF_CACHE/hub/models--${MODEL//\//--}/snapshots/$MODEL_REV/model.safetensors.index.json" sources
  sources="$( { grep -oE -- '-v [^ :]+:' <<< "$TABLE_MOUNTS" || true; } | sed -e 's/^-v //' -e 's/:$//' | tr '\n' ' ')"
  sed -e "s|__CHECKPOINT_INDEX__|$index|g" -e "s|__TABLE_SOURCES__|${sources% }|g" \
      -e "s|__USER__|$(id -un)|g" -e "s|__GROUP__|$(id -gn "$(id -un)")|g" -e "s|__HOME__|$HOME|g" \
      -e "s|__IMAGE__|$LOCAL_IMAGE|g" -e "s|__MODEL__|$MODEL|g" -e "s|__REV__|$MODEL_REV|g" \
      -e "s|__GMU__|$GMU|g" -e "s|__SEQS__|$SEQS|g" -e "s|__HF_CACHE__|$HF_CACHE|g" \
      -e "s|__MAXLEN__|$maxlen|g" -e "s|__YARN__|$yarn|g" \
      -e "s|__ENGINE_BIND__|$ENGINE_BIND|g" -e "s|__PORT__|$PORT|g" \
      -e "s|__TABLE_MOUNTS__|$TABLE_MOUNTS|g" -e '/^ *\\$/d' \
      "$HERE/$UNIT.template" > "$1"
  # the choice given, kept for the runs after (a Load stages the unit with none of the user's environment)
  if [ -n "$CONTEXT_GIVEN" ]; then
    if [ "$FLASH_LOCAL_CONTEXT" = 1m ]; then mkdir -p "$CONFIG_DIR" && echo 1m > "$CONTEXT_CHOICE"; else rm -f "$CONTEXT_CHOICE"; fi
  fi
}

# Step 4, the one part that needs root: the unit and its start scripts. A function, since a lane
# installed on demand (--units-only) takes it straight after the preflight.
install_service(){
  step "4/5 Service"
  # vLLM's compile cache, FlashInfer's and Triton's kernels and b12x's kernel choices, built at
  # the first boot (17 min on the reference box, 5 min 37 s once they are there)
  vlane_service_files "$CONFIG_DIR/flash-local-cache/vllm" "$CONFIG_DIR/flash-local-cache/flashinfer" \
                      "$CONFIG_DIR/flash-local-cache/b12x" "$CONFIG_DIR/flash-local-cache/triton"
  # Docker's default seccomp profile and the three io_uring calls the n-gram table is read with
  # (the unit says why); the docker client reads it at every start, as the user
  cmp -s "$HERE/seccomp-io-uring.json" "$CONFIG_DIR/seccomp-io-uring.json" \
    || install -m 644 "$HERE/seccomp-io-uring.json" "$CONFIG_DIR/seccomp-io-uring.json"
  # the check of what the engine reads, the head of the unit's ExecStart (flash-local-files.sh says why)
  cmp -s "$HERE/flash-local-files.sh" "$CONFIG_DIR/flash-local-files.sh" \
    || install -m 755 "$HERE/flash-local-files.sh" "$CONFIG_DIR/flash-local-files.sh"
  RENDER="$(mktemp)"; trap 'rm -f "$RENDER"' EXIT
  render_unit "$RENDER"
  # with what the engine reads at its start besides the unit: a lane started before they changed
  # serves the old ones (vllm-lane.sh); the 1M window's overrides among them when its unit reads them
  local starts=("$CONFIG_DIR/seccomp-io-uring.json" "$TEMPLATE")
  [ "$RENDERED_MAXLEN" = 262144 ] || starts+=("$YARN_JSON")
  vlane_put_unit "$RENDER" "${starts[@]}"
  # Installed, not switched to: the lane this box serves stays the lane it serves.
  echo "installed as a lane of its own. Switch to it like any lane:"
  echo "  the cockpit: Load on Qwen3.8 Flash-Next FP4-Local in the Lanes view"
  echo "  a terminal : ./switch-model.sh flash-local, then the commands it prints"
}

# The room the table's copies need on their folder's disk, said before anything is downloaded: the
# copy refused only after the 102 GB of the checkpoint were fetched, and the Load failed then (found
# in review, 2026-10-20). --room is that alone, for switch-model.sh before its fetch.
lane_table_room(){
  python3 "$HERE/ple-table.py" "$HF_CACHE" "$MODEL" "$MODEL_REV" --home "$HOME" "${TABLE_DIR_ARG[@]}" \
    --need-gb "$FLASH_LOCAL_TABLE_GB" room \
    || die "the copies of the n-gram table would not fit (the line above says why): nothing was downloaded"
}
if [ "$MODE" = room ]; then lane_table_room; exit 0; fi

# --stage: the unit for the checkpoint in the cache, for switch-model.sh, with no sudo: its template
# and its table first, as the unit names them. The checkpoint must be whole (the switch fetched it).
if [ "$MODE" = stage ]; then
  lane_template >&2
  lane_table copy >&2
  # This lane's own files only, as the other lanes' switches touch nothing but their unit: the start
  # scripts every lane runs (vlane_service_files) are ./install.sh's to put in, and a Load that put
  # them in from a checkout moved past its install ran newer ones under every lane, outside the
  # record of what is installed (found in review, 2026-10-20). Its caches made as the user, so that
  # docker does not make them as root, and its filter only when it is missing.
  mkdir -p "$CONFIG_DIR/flash-local-cache/vllm" "$CONFIG_DIR/flash-local-cache/flashinfer" \
           "$CONFIG_DIR/flash-local-cache/b12x" "$CONFIG_DIR/flash-local-cache/triton"
  [ -f "$CONFIG_DIR/seccomp-io-uring.json" ] || install -m 644 "$HERE/seccomp-io-uring.json" "$CONFIG_DIR/seccomp-io-uring.json"
  [ -f "$CONFIG_DIR/flash-local-files.sh" ] || install -m 755 "$HERE/flash-local-files.sh" "$CONFIG_DIR/flash-local-files.sh"
  render_unit "$STAGE_OUT"
  grep -q '__[A-Z][A-Z0-9_]*__' "$STAGE_OUT" && die "the unit template still holds an unsubstituted placeholder"
  exit 0
fi

step "1/5 Preflight (FP4-Local: $MODEL @ ${MODEL_REV:0:12})"
vlane_preflight engine-preflight.sh engine-secrets.sh vllm_engine_auth.py ckpt-fetch.py patch-template.py seccomp-io-uring.json \
  flash-local-files.sh
vlane_room "$FLASH_LOCAL_IMAGE" "$MODEL"
if [ "$MODE" = units ]; then
  echo "OK: $(uname -m), key present; the image and the checkpoint come with the lane's first Load"
  # a checkpoint already in the cache (an install.sh run after a Load) keeps the table's mounts
  lane_table mounts
  vlane_template_if_cached "$TEMPLATE" "${TEMPLATE_FIXES[@]}"
  install_service
  step "Done: the Qwen3.8 Flash-Next FP4-Local lane is installed; its first Load prepares it"
  echo "  the cockpit: Load on Qwen3.8 Flash-Next FP4-Local in the Lanes view pulls its image (about 18 GB, 10 to"
  echo "               download), fetches its checkpoint (about $WEIGHTS_GB GB), then starts it"
  echo "  a terminal : ./install-flash-local.sh --prepare, then ./switch-model.sh flash-local and the commands it prints"
  exit 0
fi

step "2/5 Image (${FLASH_LOCAL_IMAGE:0:40}...)"
if docker image inspect "$FLASH_LOCAL_IMAGE" >/dev/null 2>&1; then
  echo "already here"
else
  docker pull "$FLASH_LOCAL_IMAGE" \
    || die "docker pull of $FLASH_LOCAL_IMAGE failed. Causes: no internet, a Docker Hub rate limit (retry in a few minutes, or docker login), or the digest removed upstream."
fi
# its local tag, so that a docker image prune leaves it alone, and the tags of the images it was
# pinned to before go (lanes.sh): kept, each bump of the pin left 18 GB that no prune reclaimed
# (found in review, 2026-10-20)
pin_tag flash-local "$FLASH_LOCAL_IMAGE"
unpin_stale flash-local "$FLASH_LOCAL_IMAGE" || true

if [ "$MODE" = prepare ]; then
  step "Done: the Qwen3.8 Flash-Next FP4-Local lane's image is ready; its Load fetches the checkpoint and starts it"
  exit 0
fi

step "3/5 Checkpoint ($MODEL @ ${MODEL_REV:0:12}, resumable), its chat template and its n-gram table"
lane_table_room
vlane_fetch "$MODEL" "$MODEL_REV"
lane_template
lane_table copy

install_service

if [ "$SMOKE" -eq 0 ]; then step "Done (proof skipped)"; exit 0; fi

step "5/5 Proving it serves (a reply and a tool call, then the box goes back to the lane it was serving)"
VLANE_SERVED=qwen3.8-flash-next-fp4; VLANE_KWARGS='{"enable_thinking":false}'
VLANE_PARSERS="the qwen3_xml parser is"; VLANE_BOOT="about 6 min, 17 at its first boot"; VLANE_WAIT_MIN=40
vlane_prove

step "Done: the Qwen3.8 Flash-Next FP4-Local lane is installed and proved it serves on $ENGINE_BIND:$PORT"
echo "  switch to it : Load on Qwen3.8 Flash-Next FP4-Local in the cockpit, or ./switch-model.sh flash-local"
echo "  clients      : the proxy on :30001 with the box's key, model qwen3.8-flash-next-fp4 (OpenAI and Anthropic APIs)"
