# shellcheck shell=bash
# lanes.sh: what switch-model.sh and the installers share about the lanes. Sourced, never run;
# it needs CONFIG_DIR, and reads LANES_UNIT_DIR (the units' directory, /etc/systemd/system).
#
# The text lane the box comes back to. A switch to a side lane (image, video, Kolibri) disables
# the text lane's unit, so enablement cannot say which text lane was in use: it is written down
# here, one file, the SGLang text unit (the 27B's or the flash's) the box had at boot last. Until
# v1.29 each side lane kept a link to the lane it took the boot from, written by every switch,
# read by install.sh's six lines and by three uninstalls, each its own way: a round trip between
# side lanes made two links name each other, a side lane removed left another's link naming a
# unit that was gone, and a new lane meant editing all of it (found in review, 2026-10-14). With
# one record there is no chain to follow, and no loop to close.
LANES_UNIT_DIR="${LANES_UNIT_DIR:-/etc/systemd/system}"

# The box's memory class and the lanes that fit it, from boxmem.py beside this file: 128, or 64 for
# the 64 GB DGX Spark NVIDIA's partners ship from 2026-10-23, which gets only the lanes that fit it.
_lanes_dir(){ case "${BASH_SOURCE[0]}" in */*) echo "${BASH_SOURCE[0]%/*}" ;; *) echo . ;; esac; }
# QWEN38_MEMORY_CLASS forces the class (the tests, the 64 GB proofs on a 128 GB box): anything but
# 64 or 128 is refused here, by its name, for every script that sources this one. install.sh read a
# typo as no class and stopped at step 1 on a box it then said needed 64 or 128 GB, while the lane
# installers and the switch went on as on a 128 GB box (found in review, 2026-10-23).
case "${QWEN38_MEMORY_CLASS:-}" in
  ""|64|128) ;;
  *) echo "ERROR: QWEN38_MEMORY_CLASS=$QWEN38_MEMORY_CLASS is not a memory class: 64 or 128, or unset to read the box's own." >&2
     exit 1 ;;
esac
box_mem_class(){ python3 -I "$(_lanes_dir)/boxmem.py" class; }   # 128, 64 or none
fitting_lanes(){ python3 -I "$(_lanes_dir)/boxmem.py" fitting ${MEM_CLASS:+--class "$MEM_CLASS"}; }   # the lanes that fit
# $1 a lane, by install.sh's name; the class install.sh read (MEM_CLASS) when it has one
lane_fits(){ python3 -I "$(_lanes_dir)/boxmem.py" fits "$1" ${MEM_CLASS:+--class "$MEM_CLASS"}; }
target_fits(){ python3 -I "$(_lanes_dir)/boxmem.py" fits-target "$1" ${MEM_CLASS:+--class "$MEM_CLASS"}; }   # $1 a target

text_lane_note(){  # $1 unit: the box's text lane is this one; anything but an SGLang text unit is ignored
  case "$1" in
    qwen38-sglang.service|qwen38-flash.service) mkdir -p "$CONFIG_DIR"; printf '%s\n' "$1" > "$CONFIG_DIR/text-lane" ;;
  esac
}

text_lane_last(){  # [$1 the side lane at boot] [$2 a file]: the text lane noted, else the one the links of v1.29 lead to
  # With $2, the record is not read, and only the links newer than that file are followed: the
  # record knows better than a link written before it (text_lane_migrate's second reading).
  local noted cur side ref="${2:-}"
  if [ -z "$ref" ]; then
    noted="$(cat "$CONFIG_DIR/text-lane" 2>/dev/null || true)"
    case "$noted" in qwen38-sglang.service|qwen38-flash.service) echo "$noted"; return 0 ;; esac
  fi
  # A box switched before v1.30 has only the links: they are followed from the side lane at boot
  # first (the others may be older than its switch), at most once through each side lane.
  for side in ${1:+"$1"} image video kolibri; do
    cur="qwen38-$side.service"
    for _ in 1 2 3 4; do               # its own link, then each other side lane's once
      case "$cur" in
        qwen38-sglang.service|qwen38-flash.service) echo "$cur"; return 0 ;;
        qwen38-image.service|qwen38-video.service|qwen38-kolibri.service)
          cur="${cur#qwen38-}"; cur="$CONFIG_DIR/lane-before-${cur%.service}"
          if [ -n "$ref" ] && ! [ "$cur" -nt "$ref" ]; then break; fi
          cur="$(cat "$cur" 2>/dev/null || true)" ;;
        *) break ;;
      esac
    done
  done
  return 0
}

text_lane_migrate(){  # once, on a box switched before v1.30: what its links say becomes the record
  local u="" side t f was="" ref=""
  if [ -s "$CONFIG_DIR/text-lane" ]; then
    # A link newer than the record was written after it by a version before v1.30 (the box
    # went back to one): it says what that version saw, so the links newer than the record are
    # read again, and the record answers where they lead to an older one (found in review,
    # 2026-10-14). v1.30 writes no link.
    for f in "$CONFIG_DIR/lane-before-image" "$CONFIG_DIR/lane-before-video" "$CONFIG_DIR/lane-before-kolibri"; do
      if [ -e "$f" ] && [ "$f" -nt "$CONFIG_DIR/text-lane" ]; then was="$(cat "$CONFIG_DIR/text-lane")"; fi
    done
    [ -n "$was" ] || return 0
    ref="$CONFIG_DIR/text-lane"
  fi
  # a text lane at boot is the text lane; else the links, from the side lane at boot first
  for t in qwen38-sglang.service qwen38-flash.service; do
    systemctl is-enabled --quiet "$t" 2>/dev/null && { u="$t"; break; }
  done
  if [ -z "$u" ]; then
    for side in image video kolibri; do
      if systemctl is-enabled --quiet "qwen38-$side.service" 2>/dev/null; then u="$(text_lane_last "$side" "$ref")"; break; fi
    done
  fi
  [ -n "$u" ] || u="$(text_lane_last "" "$ref")"
  [ -n "$u" ] || u="$was"              # links that lead nowhere new: the record stays what it said
  [ -z "$u" ] || text_lane_note "$u"
  return 0
}

text_lane_back(){  # [$1 the side lane at boot]: the text unit to give the boot back to, or nothing
  local u
  u="$(text_lane_last "${1:-}")"
  if [ -n "$u" ] && [ -f "$LANES_UNIT_DIR/$u" ] && text_unit_ready "$u"; then echo "$u"; return 0; fi
  # the one noted is gone, cannot start, or nothing was noted: whichever text lane is installed and
  # can start, the 27B first
  for u in qwen38-sglang.service qwen38-flash.service; do
    [ -f "$LANES_UNIT_DIR/$u" ] && text_unit_ready "$u" && { echo "$u"; return 0; }
  done
  return 0
}
# The text unit the box comes back to, whether it can start or not: the one noted, else one installed,
# the 27B first (text_lane_back before v1.37.7). What opencode's default follows when no text lane can
# start (vllm-lane.sh): a lane the box has, not the 27B on a box without one (found in review, 2026-10-21).
text_lane_home(){  # [$1 the side lane at boot]
  local u
  u="$(text_lane_last "${1:-}")"
  if [ -n "$u" ] && [ -f "$LANES_UNIT_DIR/$u" ]; then echo "$u"; return 0; fi
  for u in qwen38-sglang.service qwen38-flash.service; do
    [ -f "$LANES_UNIT_DIR/$u" ] && { echo "$u"; return 0; }
  done
  return 0
}
# A text lane given the boot has to start: its image and its checkpoint on the box. A lane installed
# on demand and never loaded has neither, and a lane left out beside a side lane at boot gets its
# units at the release's pins and none of its downloads (issue #57): given the boot, it pulled its
# image at boot, or started again every 15 s with no checkpoint (found in review, 2026-10-21).
text_unit_ready(){  # $1 a text unit
  case "$1" in
    qwen38-flash.service) text_lane_prepared flash ;;
    qwen38-sglang.service) text_lane_prepared 27b ;;
    *) return 1 ;;
  esac
}

# The uninstall of a side lane at boot gives the boot back (text_lane_back) and says so, "as it was
# before" only of the lane it noted; with none that can start it enables none and says how to get one.
# Each uninstall had its own copy, which said "as it was before" of any lane, and with none advised
# `systemctl enable --now qwen38-sglang.service`, a lane that may not be there or may not start
# (found in review, 2026-10-21).
text_lane_give_back(){  # $1 the side lane (text_lane_back's argument), $2 that lane in words
  local back noted
  back="$(text_lane_back "$1")"; noted="$(text_lane_last "$1")"
  if [ -z "$back" ]; then
    local how="./install.sh installs one"
    case "$(text_lane_home "$1")" in
      qwen38-sglang.service) how="./prepare-lane.sh 27b stock, then ./switch-model.sh stock" ;;
      qwen38-flash.service) how="./prepare-lane.sh flash flash, then ./switch-model.sh flash" ;;
    esac
    echo "NOTE: no engine is enabled at boot any more: no text lane on this box can start (its image and its checkpoint)."
    echo "      Load one from the cockpit's Lanes view, or: $how"
    return 0
  fi
  sudo systemctl enable "$back"
  if [ "$back" = "$noted" ]; then
    echo "$back is enabled at boot again, as it was before $2; start it now with: sudo systemctl start $back   (or the cockpit)"
  else
    echo "$back is enabled at boot, a text lane that can start${noted:+ (the one before $2, $noted, cannot: it is gone, or its image or its checkpoint is not on the box)}; start it now with: sudo systemctl start $back   (or the cockpit)"
  fi
}

# The other text lane, installed beside the lane that serves, is on demand too: an update builds
# it whole only when it is installed as its launcher (flash) or unit (27B) names it, the image
# it runs on the box, the draft vocabulary the flash launcher waits for when it waits for one,
# the 27B's DFlash2 draft, and the checkpoint it serves whole in the cache it mounts (every box
# installed before this is). An update builds a lane whole on that target: the image alone read
# as prepared, and a first Load of flash-nvda stopped after its pull was finished on the default;
# with the vocabulary, a Load stopped before its switch (the page closed, then an update) still
# was, a checkpoint of 135 GB nobody chose, and a 27B Load of fp8 stopped in its download was
# finished on stock (found in review, 2026-10-15).
text_lane_prepared(){  # $1 flash|27b
  local f img map model rev hf
  if [ "$1" = flash ]; then f="$CONFIG_DIR/launch-flash.sh"; else f="${SGL_UNIT_PATH:-$LANES_UNIT_DIR/qwen38-sglang.service}"; fi
  img="$(engine_image "$f")"
  if [ -z "$img" ] || ! docker image inspect "$img" >/dev/null 2>&1; then return 1; fi
  if [ "$1" = flash ]; then
    map="$({ grep -m1 -oE '\[ -s "[^"]+" \] && TIER\+=\(--speculative-token-map' "$f" 2>/dev/null || true; } | head -1 | cut -d'"' -f2)"
    [ -z "$map" ] || [ -s "$map" ] || return 1
  else
    draft_in_cache "$f" || return 1
  fi
  model="$({ grep -m1 -oE -- '--model-path [^ ]+' "$f" 2>/dev/null || true; } | head -1 | cut -d' ' -f2)"
  rev="$({ grep -m1 -oE -- '--revision [^ ]+' "$f" 2>/dev/null || true; } | head -1 | cut -d' ' -f2)"
  hf="$(unit_hf_cache "$f")"
  [ -n "$model" ] && ckpt_cached "${hf:-${HF_CACHE:-$HOME/.cache/huggingface}}/hub/models--${model//\//--}" "${rev:-main}"
}

# A lane's unit, put in place. The [Unit] lines that name the other lanes (Conflicts=, After= and
# the like) order and exclude them, and systemd applies them at its daemon-reload: the engine reads
# none of them. A lane added to the box changes them in every other unit, and its update read as a
# change of what each engine runs: install.sh restarted the text lane that served (5 to 15 min), and
# the other installers said theirs ran older files (found in review, 2026-10-15). Changed only
# there, the unit keeps its date, the one lane_serves_older and engine-inputs.py go by, which leaves
# those lines out too. Prints same, deps (only those lines changed) or new.
UNIT_DEP_KEYS='^(Description|Documentation|Wants|Requires|Requisite|BindsTo|PartOf|Conflicts|After|Before)='
unit_put(){  # $1 the rendered unit, $2 where it goes (root's: read and written with sudo)
  # Each step returns on its failure: a function run in $(...) does not stop at one under set -e,
  # and the caller, which assigns what it prints, stops on that return.
  local old
  if [ -f "$2" ] && sudo cmp -s "$1" "$2"; then echo same; return 0; fi
  if [ -f "$2" ] && cmp -s <(sudo cat "$2" | grep -vE "$UNIT_DEP_KEYS" || true) <(grep -vE "$UNIT_DEP_KEYS" "$1" || true); then
    old="$(stat -c %Y "$2")" || return 1
    sudo install -m 644 "$1" "$2" || return 1
    sudo touch -m -d "@$old" "$2" || return 1
    echo deps; return 0
  fi
  sudo install -m 644 "$1" "$2" || return 1
  echo new
}

lane_serves_older(){  # $1 unit, $2... its files: true when it serves and one of them is newer than its start
  local unit="$1" started f; shift
  [ "$(systemctl show -p ActiveState --value "$unit" 2>/dev/null)" = active ] || return 1
  started="$(systemctl show -p ExecMainStartTimestamp --value --timestamp=unix "$unit" 2>/dev/null | tr -dc '0-9')"
  [ -n "$started" ] || return 1
  for f in "$@"; do
    [ -e "$f" ] && [ "$(stat -L -c %Y "$f")" -gt "$started" ] && return 0
  done
  return 1
}

stale_since(){  # $1 unit, $2... its files: true when it does not serve, or serves older files than these
  local started
  [ "$(systemctl show -p ActiveState --value "$1" 2>/dev/null)" = active ] || return 0
  started="$(systemctl show -p ExecMainStartTimestamp --value --timestamp=unix "$1" 2>/dev/null | tr -dc '0-9')"
  [ -n "$started" ] || return 0
  lane_serves_older "$@"
}

# The builds of a vLLM lane's image that no installed unit names, once the unit names this run's.
# One image can serve two lanes (the Nemotron lanes', install-nemotron-super.sh says why): a build
# goes when no unit names it any more, and not when its own lane's unit names another, which took
# away the build Super VL's unit still named, and its next Load looped on preparing one already
# built (found in review, 2026-10-16). -w: a tag that is the start of another is not that one.
lane_prune_images(){  # $1 the image's repository (qwen38-nemotron), $2 the build kept ("" for none)
  local img
  for img in $(docker images --format '{{.Repository}}:{{.Tag}}' "$1" 2>/dev/null); do
    [ "$img" = "$2" ] && continue
    if grep -qsFw -- "$img" "$LANES_UNIT_DIR"/qwen38-*.service; then
      echo "image kept: $img, which an installed lane's unit names"
    else
      docker rmi "$img" >/dev/null 2>&1 && echo "image removed: $img"
    fi
  done
  return 0
}

# The flash lane's reduced draft vocabulary, built once into $CONFIG_DIR, by install.sh and by the
# lane's first Load when it is installed on demand (prepare-lane.sh flash). Built inside the
# serving image, so the tokenizer that ranks the ids is the one the served model ships, not
# whatever the host has (the host has no transformers at all). Corpus-free by default: the
# ranking falls back to the tokenizer's own construction order, which is a frequency ranking
# learned over the tokenizer's training corpus. Drop a corpus.jsonl in the config directory (one
# JSON object per line with a "text" field, ideally the model's own output) and it is used as
# the primary ranking instead, which is what the model's drafter actually has to predict.
# Needs REPO_DIR (build-token-map.py) and CONFIG_DIR.
flash_token_map(){  # $1 image, $2 HF cache, $3 repo, $4 revision, $5 size, $6 what to say when no snapshot is there yet
  local image="$1" hf="$2" repo="$3" rev="$4" size="$5" name="token-map-$5.pt" cache corpus=()
  if [ -s "$CONFIG_DIR/$name" ]; then
    echo "reduced draft vocabulary already built: $CONFIG_DIR/$name"
    return 0
  fi
  echo "building the reduced draft vocabulary ($size ids, ~30 s)"
  # the commit a ref names (MODEL_REV=main): snapshots/main never exists, and the map was
  # skipped on every run (found in review, 2026-09-24)
  cache="$hf/hub/models--${repo//\//--}"
  [ -f "$cache/refs/$rev" ] && rev="$(cat "$cache/refs/$rev")"
  if [ ! -d "$cache/snapshots/$rev" ]; then
    echo "NOTE: no snapshot at the pinned revision yet; the draft vocabulary is skipped this run."
    echo "      $6"
    return 0
  fi
  [ -s "$CONFIG_DIR/corpus.jsonl" ] && corpus=(--corpus /out/corpus.jsonl)
  # --init: a job the cockpit stops takes its container with it (switch-model.sh says why)
  docker run --rm --init --entrypoint python3 \
    -v "$hf":/root/.cache/huggingface \
    -v "$CONFIG_DIR":/out \
    -v "$REPO_DIR":/repo:ro \
    "$image" \
    /repo/build-token-map.py \
      --snapshot "/root/.cache/huggingface/hub/models--${repo//\//--}/snapshots/$rev" \
      --out "/out/$name" --size "$size" "${corpus[@]}" \
    || return 1
}

# A digest pull leaves the image with no tag, and to Docker an image with no tag is
# dangling: `docker image prune` or `docker system prune`, the usual way to win back room
# on a full disk, deletes it, and the lane pulls 30 GB again at its next start (both
# serving images of the reference box were listed dangling, 2026-09-23). A local tag takes
# it out of that set, for both lanes' pins when they are here (install.sh), and for the image
# a lane's first Load pulls (prepare-lane.sh). uninstall.sh knows the name.
pin_tag() {   # $1 lane, $2 image: a digest reference present on this box gets its local tag
  local tag id
  case "$2" in *@sha256:*) ;; *) return 0 ;; esac
  id="$(docker image inspect "$2" --format '{{.Id}}' 2>/dev/null)" || return 0   # not on this box
  tag="qwen38-pinned:$1-$(printf '%s' "${2##*@sha256:}" | cut -c1-12)"
  if [ "$(docker image inspect "$tag" --format '{{.Id}}' 2>/dev/null || true)" = "$id" ]; then
    return 0   # already tagged
  fi
  if docker tag "$2" "$tag"; then
    echo "tagged $tag, so a docker image prune leaves it alone"
  else
    echo "NOTE: could not tag $2; a docker image prune would delete it"
  fi
}
# A lane's own local tags, and only its own: qwen38-pinned:<lane>-<12 hex>. A glob on the lane's
# name and a dash also takes the tags of every lane whose name starts with it: the flash lane's
# install would have untagged the Qwen3.8 Flash-Next FP4-Local lane's image, whose tag starts with flash-
# (found in review, 2026-10-19).
pin_tags_of() {   # $1 lane: its tags, one per line, each with its image id
  docker images --no-trunc --format '{{.Repository}}:{{.Tag}} {{.ID}}' qwen38-pinned 2>/dev/null \
    | grep -E "^qwen38-pinned:$1-[0-9a-f]{12} " || true
}
# A pin that moves leaves its tag behind, and the tag keeps the retired image out of every
# prune: 30 to 39 GB per release that bumps a pin (found in review, 2026-09-24). Once the
# lane's current pin is here and tagged, the lane's other tags go, unless a container still
# uses their image; the image is then merely dangling, and the usual prune reclaims it.
unpin_stale() {   # $1 lane, $2 the image the lane is pinned to now
  local cur tag id
  cur="$(docker image inspect "$2" --format '{{.Id}}' 2>/dev/null)" || return 0
  # the lane's own tags only: flash- is how the Qwen3.8 Flash-Next FP4-Local lane's tags begin too
  pin_tags_of "$1" \
    | while read -r tag id; do
        if [ "$id" = "$cur" ] || [ -n "$(docker ps -aq --filter "ancestor=$id" 2>/dev/null)" ]; then
          continue
        fi
        if docker rmi "$tag" >/dev/null 2>&1; then
          echo "untagged $tag, a pin this release no longer uses: a docker image prune can reclaim it"
        fi
      done
}
pin_untag() {   # $1 lane: its tags go (its uninstall); the image is then merely dangling
  local tag id left unit other
  pin_tags_of "$1" | while read -r tag id; do
    docker rmi "$tag" >/dev/null 2>&1 || continue
    # The Qwen3.8 Flash-Next FP4-Local and Ornith lanes pin one image, each with its own tag: the other lane's
    # tag keeps it, and no prune reclaims it while that lane is installed.
    left="$(docker image inspect "$id" --format '{{range .RepoTags}}{{println .}}{{end}}' 2>/dev/null | grep -m1 '^qwen38-pinned:' || true)"
    if [ -n "$left" ]; then
      echo "untagged $tag: the image stays, $left still names it"
      continue
    fi
    # A lane loaded from the cockpit after the other had pulled the image found it there, and its
    # preparation, which tags, was not run: an installed unit that runs the image by its digest gets
    # its lane's tag now (found in review, 2026-10-22).
    for unit in "${LANES_UNIT_DIR:-/etc/systemd/system}"/qwen38-*.service; do
      other="${unit##*/qwen38-}"; other="${other%.service}"
      [ "$other" != "$1" ] && grep -qsE "@sha256:${tag##*-}[0-9a-f]{52}" "$unit" || continue
      if docker tag "$id" "qwen38-pinned:$other-${tag##*-}" >/dev/null 2>&1; then
        left="qwen38-pinned:$other-${tag##*-}"
        break
      fi
    done
    if [ -n "$left" ]; then
      echo "untagged $tag: the image stays, tagged $left, as the $other lane's unit runs it"
    else
      echo "untagged $tag: a docker image prune can reclaim it"
    fi
  done
  return 0
}

flash_cannot_start(){
  # Prints why this GPU cannot start the flash lane and returns 0, or returns 1. Only the check's
  # status 1 says so (HMM or None, issue #54): any other, a check that is not there or cannot
  # run included, is no answer, and the engine decides as before. Read as "cannot", a missing
  # check stopped the lane for good, with no reason given (found in review, 2026-10-19).
  local why rc=0
  why="$(bash "$REPO_DIR/flash-ats-check.sh" 2>/dev/null)" || rc=$?
  [ "$rc" -eq 1 ] && [ -n "$why" ] || return 1
  printf '%s\n' "$why"
}

# The image a text lane's launcher (flash) or unit (27B) runs: the word on the line before its
# server command, whatever its form (a digest pin, a tag an operator passed, a local build).
engine_image(){  # $1 the launcher or the unit
  { grep -B1 -m1 -- 'python3 -m sglang.launch_server' "$1" 2>/dev/null || true; } | head -1 | awk '{print $1}'
}
# Whether a checkpoint is whole in a cache: every shard its index names is in the snapshot of the
# revision, since a file appears there only once its blob is whole (a link to a blob gone is not).
ckpt_cached(){  # $1 = the repo's cache folder, $2 = the revision (a commit or a ref name)
  python3 - "$1" "$2" <<'PY'
import json, pathlib, sys
repo, rev = pathlib.Path(sys.argv[1]), sys.argv[2]
ref = repo / "refs" / rev
snap = repo / "snapshots" / (ref.read_text().strip() if ref.is_file() else rev)
try:
    shards = set(json.loads((snap / "model.safetensors.index.json").read_text())["weight_map"].values())
except (OSError, ValueError, KeyError, TypeError, AttributeError):
    shards = {"model.safetensors"}
sys.exit(0 if shards and all((snap / s).exists() for s in shards) else 1)
PY
}
# The Hugging Face cache the first of these units or launchers mounts (-v <cache>:/root/.cache/
# huggingface), nothing when none does; and the nearest folder of a path that exists, where df and
# stat can look. One reading each for every script: they were copied into a dozen places, a fix to
# one missing from the next (found in review, 2026-10-16).
unit_hf_cache(){  # $1... units or launchers; one slash at a time, as a unit rendered from a home with a
  # trailing slash wrote "//" and the home is spelled without it now (install.sh, 2026-10-22)
  { grep -hoE -- '-v [^ :]+:/root/\.cache/huggingface' "$@" 2>/dev/null || true; } \
    | head -1 | sed -e 's/^-v //' -e 's|:/root/\.cache/huggingface$||' | tr -s /
}
existing(){  # $1 a path
  local p="$1"; while [ ! -e "$p" ]; do p="$(dirname "$p")"; done; printf '%s\n' "$p"
}

# Whether the DFlash2 draft a 27B unit names is in the cache it mounts, at its revision (a ref
# such as main read through refs/): its config and a weights file (a snapshot links a file only
# once it is whole).
draft_in_cache(){  # $1 the 27B unit
  local repo rev hf snap f
  repo="$({ grep -oE -- '--speculative-draft-model-path [^ ]+' "$1" 2>/dev/null || true; } | head -1 | cut -d' ' -f2)"
  [ -n "$repo" ] || return 0   # no draft named: its image is all the lane needs
  rev="$({ grep -oE -- '--speculative-draft-model-revision [^ ]+' "$1" || true; } | head -1 | cut -d' ' -f2)"
  hf="$(unit_hf_cache "$1")"
  snap="$hf/hub/models--${repo//\//--}"
  [ -n "$rev" ] || rev=main
  if [ -f "$snap/refs/$rev" ]; then rev="$(cat "$snap/refs/$rev")"; fi
  [ -f "$snap/snapshots/$rev/config.json" ] || return 1
  for f in "$snap/snapshots/$rev"/*.safetensors; do [ -e "$f" ] && return 0; done
  return 1
}
# Room on the disk that holds a path, in GB: the nearest folder of it that exists.
free_gb_at(){  # $1 a path
  { df -BG --output=avail "$(existing "$1")" 2>/dev/null || true; } | tail -1 | tr -dc '0-9'
}

# The video lane's runtime writes two caches of its own at its first start, reused after, on the
# disk of $HOME and outside the lane's folder: copies of 52 transformed DiT weights (fused q/k/v,
# reordered rows) as file mappings, 11.2 GiB, and the video decoder's weights converted to its
# decode dtype, 4.5 GiB (reference box, 2026-09-25). One line each, folder|GiB still needed|what
# (what is already there comes off): install-video.sh counts them with the runtime, and
# switch-model.sh with the checkpoint, on the disk each lands on.
video_first_start_caches(){
  local p d gb what have n
  for p in "$HOME/.cache/sglang/diffusion/host_spill|12|the runtime's transformed weights" \
           "$HOME/.cache/sgl_diffusion/decode_dtype_store|5|the decoder's converted weights"; do
    IFS='|' read -r d gb what <<< "$p"
    have="$({ du -s --apparent-size -B1 "$d" 2>/dev/null || true; } | cut -f1)"
    n=$(( (gb * 1073741824 - ${have:-0}) / 1073741824 )); [ "$n" -ge 0 ] || n=0
    printf '%s|%s|%s\n' "$d" "$n" "$what"
  done
}

# The room a text lane's checkpoint asks of its cache's disk, with its caches, whole GB still
# needed, as install.sh counts it: 10 when the target is whole in the cache, else 45 for a 27B
# target (its DFlash2 draft in it) and 180 for flash, what the cache holds of the repo off, 10 at
# least. The switch checks it (switch-model.sh), and so does a first Load before its first
# download (prepare-lane.sh). A flash checkpoint already whole was asked 54 here and 10 by
# install.sh, and a first Load after a Download from Library was refused for want of room it
# did not need (found in review, 2026-10-15).
ckpt_room_gb(){  # $1 flash|27b, $2 the HF cache, $3 the repo, $4 its revision
  local n=45 have dir="$2/hub/models--${3//\//--}"; [ "$1" = flash ] && n=180
  if ckpt_cached "$dir" "${4:-main}"; then echo 10; return 0; fi
  have="$({ du -s --apparent-size -B1 "$dir/blobs" 2>/dev/null || true; } | cut -f1)"
  n=$(( n - ${have:-0} / 1073741824 )); [ "$n" -ge 10 ] || n=10
  echo "$n"
}
# What a first Load downloads, summed on each disk before the first byte: "path|GB|what" each.
# Each part checked its own room as it came, so a disk with room for the image and the
# checkpoint and not for the PLE table beside them took 150 GB, then stopped (found in review,
# 2026-10-15); install.sh sums its parts on each disk too. The refusal names the disk by the
# folders of its parts: "on one disk" left a box with two to guess which (the same review).
room_per_disk(){
  local -A need=() free=() what=() at=()
  local part path gb w p dev
  for part in "$@"; do
    IFS='|' read -r path gb w <<< "$part"
    [ "${gb:-0}" -gt 0 ] || continue
    p="$(existing "$path")"
    dev="$(stat -L -c %d "$p")"
    need[$dev]=$(( ${need[$dev]:-0} + gb )); free[$dev]="$(free_gb_at "$path")"
    what[$dev]="${what[$dev]:+${what[$dev]}; }$gb for $w"; at[$dev]="${at[$dev]:+${at[$dev]} and }$path"
  done
  for dev in "${!need[@]}"; do
    [ "${free[$dev]:-0}" -ge "${need[$dev]}" ] \
      || die "need ~${need[$dev]} GB free on the disk of ${at[$dev]} for this first Load (${what[$dev]}); found ${free[$dev]:-unknown} GB. Free some space, then Load the lane again: what came already is kept."
  done
}

# ── What a box fresh from its first-boot setup lacks, put right where a script needs it ──────────
# The docker group. NVIDIA's first-boot setup puts the account it creates in adm, sudo, audio, dip,
# plugdev, users and lpadmin, never docker (its oobe-admin, DGX OS 7.6.0 on an ASUS GX10), and the
# daemon's socket is root:docker 0660: the engines, the cockpit and these scripts run their
# containers as that account. The one-liner stopped at its first check on every new box, and its
# fix told a desktop user to log out (found on an ASUS GX10, 2026-10-20). An account added to the
# group has it from its next login on; a terminal opened before, the install's own included, keeps
# the groups of its login, and the scripts run from it come through docker_session.

# A script started again by docker_relaunch ignores again what it ignored before: sg sets SIGHUP,
# SIGINT, SIGQUIT and the terminal-stop signals back to their defaults in the run it starts, and a
# run under nohup lost its SIGHUP there (measured, 2026-10-20).
for _sig in ${QWEN38_SIG_IGNORED:-}; do
  case "$_sig" in HUP|INT|QUIT|TSTP|TTIN|TTOU) trap '' "$_sig" ;; esac
done
unset _sig

docker_in_session(){  # this process has the docker group
  local line gid
  line="$(getent group docker 2>/dev/null)" || return 1
  gid="${line#*:*:}"; gid="${gid%%:*}"
  case " $(id -G) " in *" $gid "*) return 0 ;; esac
  return 1
}

docker_in_group(){  # the account is in the docker group, as its next login will have it
  case " $(id -nG "$(id -un)" 2>/dev/null) " in *" docker "*) return 0 ;; esac
  return 1
}

# docker_relaunch_ok CMD...: docker_relaunch below can start CMD again here: not done already; sg,
# env, setsid and /proc there; the account's own group with a name (sg takes no number); the account
# named in the docker group's own member list (an account a directory service puts in the group
# without it is asked a group password by sg); no word of CMD, of the paths of sg and env, of the
# account's group, or of a value to hand back past sg, holding a character sg's `sh -c` would read as
# its own; and sg going to docker without a password, tried with no terminal to ask one on. sg takes
# the members from /etc/gshadow, which usermod writes as well: an account added to /etc/group alone,
# by hand, is asked the group's password there, and the run ended on "Invalid password." (measured
# with Ubuntu 24.04's login 1:4.13, 2026-10-21).
docker_relaunch_ok(){
  local v line own
  [ -z "${QWEN38_DOCKER_RELAUNCHED:-}" ] || return 1
  command -v sg >/dev/null 2>&1 && command -v env >/dev/null 2>&1 && command -v setsid >/dev/null 2>&1 \
    && [ -r "/proc/$$/environ" ] || return 1
  own="$(id -gn "$(id -un)" 2>/dev/null)" || return 1
  line="$(getent group docker 2>/dev/null)" || return 1
  case ",${line##*:}," in *",$(id -un),"*) ;; *) return 1 ;; esac
  for v in "$(command -v sg)" "$(command -v env)" "$own" "$@"; do
    case "$v" in ''|*[!A-Za-z0-9._/:=@+,-]*) return 1 ;; esac
  done
  while IFS= read -r -d '' v; do
    case "${v%%=*}" in
      GCONV_PATH|GETCONF_DIR|GLIBC_TUNABLES|HOSTALIASES|LD_*|LOCALDOMAIN|LOCPATH|MALLOC_*|NIS_PATH|NLSPATH|RESOLV_HOST_CONF|RES_OPTIONS|TMPDIR|TZDIR)
        case "$v" in *[!A-Za-z0-9._/:=@+,-]*) return 1 ;; esac ;;
    esac
  done < "/proc/$$/environ"
  # tried once: docker_relaunch asks this function again, and sg's answer holds for the run
  [ -n "${_docker_sg_tried:-}" ] && return 0
  setsid -w "$(command -v sg)" docker -c true < /dev/null > /dev/null 2>&1 || return 1
  _docker_sg_tried=1
  return 0
}

# docker_relaunch WHY CMD...: CMD again, with the groups a new login has, for the rest of this run:
# sg to docker, then to the account's own group, which a lone `sg docker` leaves in docker's place (the
# units take it for their Group=; measured on the reference box, 2026-10-20). sg runs CMD as a child
# it waits for and passes its exit status back (measured): the run is then the child of the second sg
# and the grandchild of the first, both waiting with SIGINT and SIGHUP ignored, so it stops by its
# process group (Ctrl+C, systemd, the cockpit), and a kill of the first PID stops only that sg. CMD
# gets the environment this process started with: what it exported since is taken out, what it
# changed is set back. The start's values it left as they were pass in the environment, never in a
# command line; those it changed are set back as env's arguments, and the variables the C library
# takes from a setuid program's environment (TMPDIR and the like), handed back after sg, go into its
# `sh -c` line: neither is a key the scripts read. The signals this process ignores
# are ignored again (lanes.sh, above), and WHY is said to the run it starts (QWEN38_DOCKER_RELAUNCHED).
# Returns 1, having run nothing, where docker_relaunch_ok says no or a step here fails: the callers
# then say the way out.
docker_relaunch(){
  local why="$1" sg envp own n v back="" ign="" mask sig
  local -a start=() fix=()
  local -A at_start=()
  shift
  docker_relaunch_ok "$@" || return 1
  sg="$(command -v sg)" && envp="$(command -v env)" && own="$(id -gn "$(id -un)")" || return 1
  mapfile -d '' -t start < "/proc/$$/environ" 2>/dev/null || return 1
  for v in "${start[@]}"; do
    n="${v%%=*}"
    case "$n" in ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;; esac
    at_start[$n]="${v#*=}"
    case "$n" in
      GCONV_PATH|GETCONF_DIR|GLIBC_TUNABLES|HOSTALIASES|LD_*|LOCALDOMAIN|LOCPATH|MALLOC_*|NIS_PATH|NLSPATH|RESOLV_HOST_CONF|RES_OPTIONS|TMPDIR|TZDIR)
        back="$back $v" ;;
    esac
  done
  while IFS= read -r n; do
    [ -n "${at_start[$n]+x}" ] || fix+=(-u "$n")
  done < <(compgen -e)
  for n in "${!at_start[@]}"; do
    case "$n" in _|SHLVL|PWD|OLDPWD) continue ;; esac
    if [ "${!n-}" != "${at_start[$n]}" ] || ! printenv "$n" >/dev/null; then
      fix+=("$n=${at_start[$n]}")
    fi
  done
  mask="$(awk '/^SigIgn:/ {print $2}' "/proc/$$/status" 2>/dev/null)" || mask=0
  for sig in 1:HUP 2:INT 3:QUIT 20:TSTP 21:TTIN 22:TTOU; do
    [ $(( 0x${mask:-0} >> (${sig%%:*} - 1) & 1 )) -eq 0 ] || ign="$ign ${sig#*:}"
  done
  exec "$envp" "${fix[@]}" QWEN38_DOCKER_RELAUNCHED="$why" QWEN38_SIG_IGNORED="${ign# }" \
    "$sg" docker -c "exec $sg $own -c 'exec $envp$back $*'"
}

# docker_session CMD...: the account is in the docker group and this process is not (its terminal
# was opened before the account joined): CMD again with the group, as from a new login. Otherwise
# nothing: a docker that refuses for another reason says so where it is reached.
docker_session(){
  docker_in_session && return 0
  docker_in_group || return 0
  if docker_relaunch_ok "$@"; then
    echo "Docker: $(id -un) is in the docker group, and this terminal was opened before it joined; going on with the group (no need to log out)."
    docker_relaunch session "$@" || true
  fi
  die "Docker: this terminal was opened before $(id -un) joined the docker group, and this run cannot start again with it here. Log out and back in (or open a new SSH session), then the same command again."
}

# docker_grant CMD...: docker answers, or this run makes it answer and runs CMD again. A refusal on
# the system's socket (root:docker) of an account outside the group is put right with sudo, said
# before sudo runs: where it asks for the password, a person can stop there. A run that cannot start
# again here (docker_relaunch_ok) still gets the account added, and says to log in again instead of
# going on. DOCKER_GRANT=no (a run that uses no sudo) says how instead. A daemon that is not running,
# a refusal anywhere else (DOCKER_HOST, a context, a rootless Docker), an account sudo refuses or that
# a directory service keeps: each stops with its own way out. No docker command at all is left to the
# caller's own check.
docker_grant(){
  local err who
  command -v docker >/dev/null 2>&1 || return 0
  err="$(docker info 2>&1 >/dev/null)" && return 0
  who="$(id -un)"
  case "$err" in
    */run/docker.sock*) ;;
    *) die "docker does not answer: $(printf '%s' "$err" | tr '\n' ' ' | cut -c1-300)" ;;
  esac
  case "$err" in
    *[Pp]"ermission denied"*) ;;
    *"daemon is running"*|*"docker daemon running"*|*"Cannot connect to the Docker daemon"*)
      die "Docker is installed, but its service is not running. Run sudo systemctl enable --now docker, then the same command again." ;;
    *) die "docker does not answer: $(printf '%s' "$err" | tr '\n' ' ' | cut -c1-300)" ;;
  esac
  [ "$(stat -L -c %G /run/docker.sock 2>/dev/null)" = docker ] \
    || die "docker refuses $who on /run/docker.sock, which is not the docker group's ($(stat -L -c '%U:%G %a' /run/docker.sock 2>/dev/null)): $(printf '%s' "$err" | head -1 | cut -c1-200)"
  docker_in_session \
    && die "docker refuses $who on /run/docker.sock, although this process has the docker group: $(printf '%s' "$err" | head -1 | cut -c1-200)"
  docker_session "$@"
  if [ "${DOCKER_GRANT:-yes}" = no ]; then
    die "Docker: $who is not in the docker group, which the engines run their containers through (the box's first-boot setup leaves it out), and this run uses no sudo. Run sudo usermod -aG docker $who, log out and back in, then the same command again."
  fi
  echo "Docker: $who is not in the docker group, which the engines and the cockpit run their containers through (the box's first-boot setup leaves it out)."
  echo "Docker: adding it now, with sudo. A member of that group can run any container, which makes it root on this machine (SECURITY.md)."
  sudo usermod -aG docker "$who" \
    || die "Docker: $who could not be added to the docker group. Run sudo usermod -aG docker $who, log out and back in, then the same command again."
  docker_in_group \
    || die "Docker: usermod said yes, and $who is still not in the docker group (an account of a directory service?). Have it added there, log in again, then the same command again."
  if docker_relaunch_ok "$@"; then
    echo "Docker: $who is in the docker group now; this run goes on with it (no need to log out)."
    docker_relaunch granted "$@" || true
  fi
  die "Docker: $who is in the docker group now, and this run cannot start again with it here. Log out and back in (or open a new SSH session), then the same command again."
}

# docker_snap_engine: what says the Docker engine this account reaches is the Snap package, on
# stdout, and 1 when it is not. Its data root and the paths of its runtimes say it, not which docker
# binary or service the box has: a box with Docker CE beside the Snap engine reached the Snap one
# (issue #58, 2026-10-21).
docker_snap_engine(){
  local root runtimes r
  root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  case "$root" in /var/snap/docker/*) echo "its data in $root"; return 0 ;; esac
  runtimes="$(docker info --format '{{range $k, $v := .Runtimes}}{{$v.Path}} {{end}}' 2>/dev/null || true)"
  for r in $runtimes; do case "$r" in /snap/*) echo "a runtime at $r"; return 0 ;; esac; done
  return 1
}
# host_nvidia_toolkit: the NVIDIA Container Toolkit installed on the host as Ubuntu packages, as DGX
# OS installs it. dpkg-query's lines are read whole before they are looked at: piped into grep -q,
# which ends at the first match, dpkg-query died writing the next one (SIGPIPE, 141), and under
# install.sh's pipefail the check said "not installed" now and then, which let a Snap engine through
# the guard of issue #58 (found by the CI run of v1.39.0, 2026-10-23; its test failed then in 1 run
# of 60 on the reference box).
host_nvidia_toolkit(){
  local st
  st="$(dpkg-query -W -f='${Status}\n' nvidia-container-toolkit nvidia-container-toolkit-base 2>/dev/null || true)"
  grep -qx 'install ok installed' <<< "$st"
}
# docker_engine_line: the engine this account reaches, in one line, for a failure to show
docker_engine_line(){
  printf 'Docker %s, data in %s, endpoint %s, runtimes %s' \
    "$(docker info --format '{{.ServerVersion}}' 2>/dev/null || echo '?')" \
    "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo '?')" \
    "$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || echo '?')" \
    "$(docker info --format '{{range $k, $v := .Runtimes}}{{$k}}={{$v.Path}} {{end}}' 2>/dev/null || echo '?')"
}

# host_packages WHY PKG...: Ubuntu packages a lane builds or runs with, which the caller found
# missing, installed with sudo, named first. Only where sudo can ask (a terminal) or need not (a
# ticket): the cockpit's Loads have neither, so install.sh puts these in while its terminal is there
# (DGX OS 7.6.0 has neither python3-dev nor ffmpeg; found on an ASUS GX10, 2026-10-20). Once per run:
# the lanes' installers that install.sh calls do not try again. apt is told it has no one to ask
# (needrestart asked in the terminal), or, where sudo says it may not set those variables (a rule for
# apt-get alone), run without them. A sudo that refuses the account or its password is not asked
# again. apt waits up to five minutes for another package manager, and a lock still held then is said
# rather than waited for again. Lists too old for the archive are refreshed once. sudo and apt speak
# English here (LC_ALL=C), whatever the account's language, so that their words can be read. Returns
# 1 when they could not be installed, saying the command: the caller's own check then says what is
# missing.
host_packages(){
  local why="$1" rc out
  shift
  [ "$#" -gt 0 ] || return 0
  [ -z "${QWEN38_HOST_PACKAGES_TRIED:-}" ] || return 1
  export QWEN38_HOST_PACKAGES_TRIED=1
  if ! sudo -n true 2>/dev/null && ! { : < /dev/tty; } 2>/dev/null; then
    echo "NOTE: missing on this box, for $why: $*. No terminal is here for sudo to ask its password: run sudo apt-get install -y $* yourself." >&2
    return 1
  fi
  echo "Installing $* for $why, missing on this box (sudo apt-get install -y $*)."
  out="$(mktemp)"
  local -a apt=(sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get -o DPkg::Lock::Timeout=300 -q -y)
  if LC_ALL=C "${apt[@]}" install "$@" 2> "$out"; then rm -f "$out"; return 0; else rc=$?; fi
  cat "$out" >&2
  # sudo itself said no (a wrong password three times, an account outside sudoers): never asked again
  if [ "$rc" -eq 1 ] && grep -q -E "incorrect password|password is required|not in the sudoers|not allowed to (run|execute)" "$out"; then
    rm -f "$out"
    echo "NOTE: sudo refused; run sudo apt-get install -y $* yourself." >&2
    return 1
  fi
  if [ "$rc" -eq 1 ] && grep -q "not allowed to set" "$out"; then
    apt=(sudo apt-get -o DPkg::Lock::Timeout=300 -q -y)
    if LC_ALL=C "${apt[@]}" install "$@" 2> "$out"; then rm -f "$out"; return 0; fi
    cat "$out" >&2
  fi
  if grep -q -E "Could not get lock|Unable to acquire the dpkg frontend lock" "$out"; then
    rm -f "$out"
    echo "NOTE: another package manager held apt's lock; run sudo apt-get install -y $* once it is done." >&2
    return 1
  fi
  rm -f "$out"
  "${apt[@]}" update || true
  if "${apt[@]}" install "$@"; then return 0; fi
  echo "NOTE: apt-get could not install $*; run sudo apt-get install -y $* yourself." >&2
  return 1
}

# run_mark CONFIG_DIR: while this run lasts, a file that tells the cockpit an update runs
# ($CONFIG_DIR/install-running.<pid>.json: the pid, its start time as /proc has it, when it started,
# and the release this clone is). A long update showed the banner that asks to run the installer,
# under the installer running (an ASUS Ascent GX10, 2026-10-21: an update of 1 h 45, 144 GB of
# video). The cockpit believes the file while this very process lives (the same pid with the same
# start time: a pid used again is not it), and forget_run_mark, run by the caller's EXIT trap,
# takes it away however the run ends; the next run removes one a run killed outright left. A file
# that cannot be written leaves the run as it was.
RUN_MARK=""
run_mark(){
  local f="$1/install-running.$$.json"
  mkdir -p "$1" 2>/dev/null || return 0
  python3 -I - "$$" "$f" "${REPO_DIR:-.}/CHANGELOG.md" <<'PY' 2>/dev/null && RUN_MARK="$f"
import json, os, re, sys, time
def start_of(pid):  # the 22nd field of /proc/<pid>/stat, after the name in parentheses
    with open(f"/proc/{pid}/stat") as f:
        stat = f.read()
    return stat[stat.rindex(")") + 2:].split()[19]
pid, out, changelog = int(sys.argv[1]), sys.argv[2], sys.argv[3]
start, to = start_of(pid), None
try:
    with open(changelog, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^## (v\d+\.\d+(?:\.\d+)?)\b", line)
            if m:
                to = m.group(1)
                break
except OSError:
    pass
with open(out + ".tmp", "w") as f:
    json.dump({"pid": pid, "start": start, "started": int(time.time()), "to": to}, f)
os.replace(out + ".tmp", out)
# and the files of runs gone without their trap (a SIGKILL, a power cut): what no process is now
d = os.path.dirname(out)
for name in os.listdir(d):
    m = re.fullmatch(r"install-running\.(\d+)\.json", name)
    if not m or os.path.join(d, name) == out:
        continue
    try:
        with open(os.path.join(d, name)) as f:
            theirs = str(json.load(f)["start"])
        if start_of(int(m.group(1))) == theirs:
            continue
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        pass
    try:
        os.remove(os.path.join(d, name))
    except OSError:
        pass
PY
  return 0
}
forget_run_mark(){ [ -z "$RUN_MARK" ] || rm -f -- "$RUN_MARK"; return 0; }

# A diffusion lane's own sockets (v1.40.6): torch's TCPStore listens on every interface whatever its rendezvous
# address (port 30005), and gloo's and NCCL's bootstrap took the box's LAN address until the unit pinned it to lo
# (the reference box, 2026-10-30, no firewall). A bind on a loopback address, any 127.* or localhost, gets the unit's
# IP filter, IPAddressDeny=any with IPAddressAllow=localhost: the lane talks to loopback alone. A bind past loopback
# serves others on purpose and gets no filter. Printed: the deny list ("any"), or nothing.
lane_ip_deny(){  # $1 the lane's bind
  case "$1" in 127.*|localhost) echo any ;; *) echo "" ;; esac
}
