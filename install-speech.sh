#!/usr/bin/env bash
# Install the speech lane: speech recognition (Qwen3-ASR in llama.cpp's llama-server) and
# speech synthesis (Kokoro-82M), behind OpenAI-compatible audio routes, on the CPU alone.
# Idempotent, and safe to run alone.
#
#   ./install-speech.sh                 first install, or update in place
#   ./install-speech.sh --no-smoke      skip the round trip at the end
#   ./install-speech.sh --uninstall     remove the unit and the runtime (models kept)
#
# Variables: SPEECH_ASR (0.6b, the default, or 1.7b: French WER 9.7% against 5.6% here,
# 2.7 GB against 4.2 GB, half of real time against real time), SPEECH_VOICE (af_heart; GET /v1/voices lists the 41, by language),
# SPEECH_PORT (30024), SPEECH_BIND (127.0.0.1), SPEECH_BOOT=1 (start it with the box),
# SPEECH_LANE_DIR (~/.local/share/qwen38-speech).
#
# WHY THE CPU. Measured on the reference box on 2026-10-05 (docs/speech-lane.md): on the
# GPU, a busy speech lane took 45% of the flash lane's decode, and beside flash after long
# agent prompts a new CUDA context failed to start with 17.6 GiB of MemAvailable. On the
# small cores, with each engine on cores of its own, the flash lane did not move. So this
# lane hides the GPU, takes the small cores only, and runs beside any other lane.
#
# NOT IN THE ONE COMMAND YET. ./install.sh does not run this: the lane is new, and is
# installed on purpose, by this script. It is not started at boot unless SPEECH_BOOT=1:
# running, it holds 2.5 to 2.7 GB of host memory (4.2 with the 1.7B) that a flash lane's
# long prompts also want.
set -euo pipefail
# One spelling of the home, without a trailing slash, as install.sh says why: the cockpit's
# exact-argv sudoers is rendered from it and has to match the cockpit's own calls (2026-10-22).
while [ -n "${HOME:-}" ] && [ "$HOME" != / ] && [ "${HOME%/}" != "$HOME" ]; do HOME="${HOME%/}"; done; export HOME
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=qwen38-speech.service
INSTALLED="/etc/systemd/system/$UNIT"
die(){ printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
step(){ printf '\n\033[1;36m── %s\033[0m\n' "$*"; }
# what the lanes' installers share; this one takes host_packages from it
# shellcheck source=lanes.sh
. "$HERE/lanes.sh"

# Under sudo every path below moves to /root. install.sh refuses the same way.
if [ "$(id -u)" = "0" ] && [ "${ALLOW_ROOT:-0}" != "1" ]; then
  die "run this as the user who will use the box, not as root (everything it installs is addressed from \$HOME)."
fi

SMOKE=1; ACTION=install
for a in "$@"; do case "$a" in
  --no-smoke) SMOKE=0 ;;
  --uninstall) ACTION=uninstall ;;
  -h|--help) awk 'NR == 1 { next } /^#/ { print; next } { exit }' "$0"; exit 0 ;;
  *) die "unknown option: $a" ;;
esac; done

# What the installed unit and launcher say, so a re-run without variables changes nothing.
installed(){ { grep -m1 -E "^$1=" "$INSTALLED" 2>/dev/null || true; } | cut -d= -f2-; }
CONFIG_DIR="$HOME/.config/qwen38"
LAUNCHER="$CONFIG_DIR/launch-speech.sh"
# Every occurrence, last one kept: the recognizer's --host and --port come first in the
# launcher, the routes' own after them.
launcher_flag(){ { grep -oE -- "$1 [^ \\\\]+" "$LAUNCHER" 2>/dev/null || true; } | awk '{print $2}' | tail -1; }
# Read back one slash at a time, as unit_hf_cache reads the cache (lanes.sh): a unit rendered from a
# home with a trailing slash says "//", and its own spelling kept it as it was at every update.
LANE_DIR="${SPEECH_LANE_DIR:-$(installed WorkingDirectory | tr -s /)}"; LANE_DIR="${LANE_DIR:-$HOME/.local/share/qwen38-speech}"
VENV="$LANE_DIR/venv"
LLAMA_DIR="$LANE_DIR/llama.cpp"
MODELS="$LANE_DIR/models"
PORT="${SPEECH_PORT:-$(launcher_flag --port)}"; PORT="${PORT:-30024}"
ASR_PORT=$((PORT + 1))
VOICE="${SPEECH_VOICE:-$(launcher_flag --default-voice)}"; VOICE="${VOICE:-af_heart}"
ASR_SIZE="${SPEECH_ASR:-}"
if [ -z "$ASR_SIZE" ]; then
  case "$(launcher_flag --asr-name)" in qwen3-asr-1.7b) ASR_SIZE=1.7b ;; *) ASR_SIZE=0.6b ;; esac
fi

# The pins. llama.cpp is the commit the lane was measured with (master on 2026-10-05,
# which carries Qwen3-ASR since #19441 and /v1/audio/transcriptions since #21863). The
# recognizer's GGUF are ggml-org's, at the revision measured, each file checked by sha256.
LLAMA_CPP_PIN="${SPEECH_LLAMA_CPP_PIN:-6c59c40076c00eab49754dc955d7652d93f9e125}"
case "$ASR_SIZE" in
  0.6b)
    ASR_REPO="ggml-org/Qwen3-ASR-0.6B-GGUF"; ASR_REV="928ab958557df9aa2ef1c93e0e83c7ad0933fae2"
    ASR_FILE="Qwen3-ASR-0.6B-Q8_0.gguf"; ASR_SHA="bca259818b50ca7c4c05e9bdb35a5dc04fa039653a6d6f3f0f331f96f6aa1971"
    MMPROJ_FILE="mmproj-Qwen3-ASR-0.6B-Q8_0.gguf"; MMPROJ_SHA="41a342b5e4c514e968cb756de6cd1b7be39eff43c44c57a2ef5fc6522e36603d"
    MEM_HIGH=4G; MEM_MAX=5G; MEM_NOTE="about 2.7 GB with the 0.6B"; MODELS_GB=2 ;;
  1.7b)
    ASR_REPO="ggml-org/Qwen3-ASR-1.7B-GGUF"; ASR_REV="36a678687ba7d07a74ca70ccb0e36902e005fb80"
    ASR_FILE="Qwen3-ASR-1.7B-Q8_0.gguf"; ASR_SHA="58e22d0532d4eacaf034cfac17a6fed159f37c41390c710186783be439d1fc57"
    MMPROJ_FILE="mmproj-Qwen3-ASR-1.7B-Q8_0.gguf"; MMPROJ_SHA="46c1d533af3f354ceb37ce855dbceff7da7fa7cf1e6a523df3b13440bd164c0d"
    MEM_HIGH=6G; MEM_MAX=7G; MEM_NOTE="about 4.2 GB with the 1.7B"; MODELS_GB=3 ;;
  *) die "SPEECH_ASR takes 0.6b or 1.7b, got: $ASR_SIZE" ;;
esac
ASR_NAME="qwen3-asr-$ASR_SIZE"
ASR_CTX=8192   # tokens: about seven minutes of speech in one pass; the routes send pieces of 30 s
# Kokoro-82M (Apache-2.0) as kokoro-onnx publishes it, checked by sha256.
KOKORO_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_MODEL_FILE="kokoro-v1.0.onnx"; KOKORO_MODEL_SHA="7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"
KOKORO_VOICES_FILE="voices-v1.0.bin";  KOKORO_VOICES_SHA="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"
RUNTIME_GB=2   # llama.cpp's source and build, and the venv (168 MB)

# Loopback: the routes have no key, as for the image and video lanes, and neither the cockpit
# nor the proxy relays them: programs on the box reach them, another machine through an SSH
# tunnel. SPEECH_BIND overrides it for someone who means to, and is told what it costs.
BIND="${SPEECH_BIND:-$(launcher_flag --host)}"; BIND="${BIND:-127.0.0.1}"
case "$BIND" in
  127.0.0.1|localhost) ;;
  *) echo "WARNING: binding $BIND. These routes have no API key, so anyone who can reach that"
     echo "         address can transcribe and speak on this box. Loopback, and an SSH tunnel from"
     echo "         another machine, is the intended shape." ;;
esac
case "$BIND" in
  *[!0-9.]*|""|*..*) [ "$BIND" = localhost ] || die "SPEECH_BIND takes an IPv4 address, got: $BIND" ;;
esac

# The small cores, from the CPUs' own maximum frequencies: on the GB10 the ten Cortex-A725
# (2.8 GHz, CPUs 0-4 and 10-14) against the ten Cortex-X925 (3.9 GHz). The recognizer gets
# the first half and the voice the second, each with as many threads as cores. A CPU with
# one kind of core is split down the middle instead.
small_cores(){  # stdin: "CPU MAXMHZ" lines; stdout: the CPUs of the slowest kind, sorted
  awk 'NF==2 && $1 ~ /^[0-9]+$/ { mhz[$1]=int($2); if (min=="" || int($2)<min) min=int($2); n++ }
       END { k=0; for (c in mhz) if (mhz[c]==min) k++
             for (c in mhz) if (mhz[c]==min || k<4) print c }' | sort -n
}
CORES="$(lscpu -e=CPU,MAXMHZ 2>/dev/null | tail -n +2 | small_cores | paste -sd' ' -)"
read -r -a CORE_LIST <<< "$CORES"
[ "${#CORE_LIST[@]}" -ge 2 ] || die "could not read the CPUs' frequencies (lscpu -e=CPU,MAXMHZ); this lane splits them between its two engines."
HALF=$(( ${#CORE_LIST[@]} / 2 ))
ASR_CPUS="$(printf '%s\n' "${CORE_LIST[@]:0:$HALF}" | paste -sd, -)"
TTS_CPUS="$(printf '%s\n' "${CORE_LIST[@]:$HALF}" | paste -sd, -)"
ASR_THREADS=$HALF; TTS_THREADS=$(( ${#CORE_LIST[@]} - HALF ))
UNIT_CPUS="$(printf '%s\n' "${CORE_LIST[@]}" | paste -sd' ' -)"

if [ "$ACTION" = uninstall ]; then
  step "Removing the speech lane"
  if [ -f "$INSTALLED" ]; then
    sudo systemctl disable --now "$UNIT" 2>/dev/null || true
    sudo rm -f "$INSTALLED"; sudo systemctl daemon-reload
    echo "unit removed"
  fi
  rm -f "$LAUNCHER"
  # Only what this script put there: LANE_DIR can be shared with other work. The models
  # stay, as the image and video lanes keep their weights: the reclaim command is printed.
  if [ -d "$LANE_DIR" ]; then
    rm -rf "$VENV" "$LLAMA_DIR"
    rm -f "$LANE_DIR/speech-server.py"
    echo "runtime removed: $VENV, $LLAMA_DIR and the routes' script"
    if [ -d "$MODELS" ]; then
      echo "the models are left in $MODELS ($(du -sh "$MODELS" 2>/dev/null | cut -f1)); delete them yourself if you want the space:"
      echo "  rm -rf $MODELS && rmdir $LANE_DIR"
    else
      rmdir "$LANE_DIR" 2>/dev/null || echo "kept $LANE_DIR: it holds files the speech lane did not put there"
    fi
  fi
  exit 0
fi

step "1/6 Preflight"
case "$(uname -m)" in aarch64|arm64) : ;; *) die "this lane is verified on the DGX Spark's ARM64 GB10 only (this box is $(uname -m))." ;; esac
# Run from a terminal, this installs what the lane needs and the box lacks (lanes.sh
# host_packages), each tool by the package that has it. The checks below, as before, say what
# could not be put in.
HOST_PKGS=()
for t in git:git curl:curl cmake:cmake c++:build-essential sha256sum:coreutils taskset:util-linux; do
  command -v "${t%%:*}" >/dev/null || HOST_PKGS+=("${t#*:}")
done
command -v ffmpeg >/dev/null || HOST_PKGS+=(ffmpeg)
python3 -c 'import venv, ensurepip' 2>/dev/null || HOST_PKGS+=(python3-venv)
[ "${#HOST_PKGS[@]}" -eq 0 ] || host_packages "the speech lane" "${HOST_PKGS[@]}" || true
for t in git curl cmake c++ sha256sum taskset; do
  command -v "$t" >/dev/null || die "$t is missing. Fix: sudo apt-get install -y git curl cmake build-essential coreutils util-linux"
done
# ffmpeg reads what llama.cpp's decoder does not (a phone's m4a, a browser's webm or ogg) and
# lets the routes cut a long recording into pieces. The video lane needs it too.
command -v ffmpeg >/dev/null || die "ffmpeg is missing: the speech lane reads audio of any format with it, and cuts long recordings into pieces. Fix: sudo apt-get install -y ffmpeg"
# ensurepip, not just venv: a box without python3-venv makes a venv with no pip.
python3 -c 'import venv, ensurepip' 2>/dev/null || die "python3-venv is missing (a venv made without it has no pip). Fix: sudo apt-get install -y python3-venv"
existing(){ local p="$1"; while [ ! -e "$p" ]; do p="$(dirname "$p")"; done; printf '%s\n' "$p"; }
free_gb(){ { df -BG --output=avail "$(existing "$1")" 2>/dev/null || true; } | tail -1 | tr -dc '0-9'; }
FREE="$(free_gb "$LANE_DIR")"; WANT=$((MODELS_GB + RUNTIME_GB + 1))
[ "${FREE:-0}" -ge "$WANT" ] || die "${FREE:-?} GiB free under $LANE_DIR, this lane needs about $WANT GiB. Free some space, or point SPEECH_LANE_DIR at a bigger disk."
# The ports must be ours or free: another service on them would answer the proof.
for p in "$PORT" "$ASR_PORT"; do
  if ss -ltnH "sport = :$p" 2>/dev/null | grep -q . && ! systemctl is-active --quiet "$UNIT" 2>/dev/null; then
    die "port $p is taken by another program; set SPEECH_PORT (this lane uses it and the next one)."
  fi
done
echo "OK: $(uname -m), recognizer $ASR_NAME, default voice $VOICE, recognizer on CPUs $ASR_CPUS and voice on $TTS_CPUS"

# Whatever this run changes that the running lane reads (the build, the routes' script, a
# model, the launcher, the unit) restarts it at the end of step 5.
CHANGED=0

step "2/6 llama.cpp at ${LLAMA_CPP_PIN:0:12}, the recognizer's server, built for the CPU"
mkdir -p "$LANE_DIR"
STAMP="$LLAMA_DIR/.qwen38-built"; BUILD_LOG="$LLAMA_DIR/qwen38-build.log"
if [ "$(cat "$STAMP" 2>/dev/null || true)" = "$LLAMA_CPP_PIN" ] && [ -x "$LLAMA_DIR/build/bin/llama-server" ]; then
  echo "already built at the pin"
else
  # One commit, not the history: a shallow fetch of the pin is a few tens of MB.
  [ -d "$LLAMA_DIR/.git" ] || git init -q "$LLAMA_DIR"
  git -C "$LLAMA_DIR" fetch -q --depth 1 https://github.com/ggml-org/llama.cpp.git "$LLAMA_CPP_PIN" \
    || die "could not fetch llama.cpp at $LLAMA_CPP_PIN (no internet, or the commit is gone upstream)."
  git -C "$LLAMA_DIR" -c advice.detachedHead=false checkout -q --force FETCH_HEAD
  # No CUDA (nothing here may touch the GPU), no HTTPS client (the installer downloads),
  # no tests or examples: only llama-server, tuned for this CPU by the native build.
  # On the small cores, as the lane will run: the box keeps serving while it compiles. And
  # no more jobs than a third of the free memory in GB: the box may be serving, and its
  # memory edge is the one place a compile could hurt it.
  JOBS=$(( $(awk '/^MemAvailable:/ { print int($2 / 1048576) }' /proc/meminfo) / 3 ))
  [ "$JOBS" -ge 1 ] || JOBS=1; [ "$JOBS" -le "${#CORE_LIST[@]}" ] || JOBS=${#CORE_LIST[@]}
  cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=OFF \
    -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF > "$BUILD_LOG" 2>&1 \
    && taskset -c "$ASR_CPUS,$TTS_CPUS" cmake --build "$LLAMA_DIR/build" -j "$JOBS" --target llama-server \
       >> "$BUILD_LOG" 2>&1 \
    || { tail -20 "$BUILD_LOG"; die "the llama.cpp build failed; the lines above and $BUILD_LOG say why."; }
  printf '%s\n' "$LLAMA_CPP_PIN" > "$STAMP"; CHANGED=1
  echo "built: $LLAMA_DIR/build/bin/llama-server"
fi
LLAMA_SERVER="$LLAMA_DIR/build/bin/llama-server"

step "3/6 Python runtime ($VENV)"
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
  python3 -m venv --clear "$VENV" || die "could not make the venv at $VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null
# Wheels only: every pin has an aarch64 wheel, so nothing is compiled and no header or
# compiler is needed (the image and video lanes learned that the hard way with python3-dev).
"$VENV/bin/pip" install --quiet --only-binary :all: -r "$HERE/speech-requirements.txt" \
  || die "the venv's packages did not install. Causes: no internet, or PyPI without a wheel for this Python (python3 --version should say 3.12)."
cmp -s "$HERE/speech-server.py" "$LANE_DIR/speech-server.py" \
  || { install -m 644 "$HERE/speech-server.py" "$LANE_DIR/speech-server.py"; CHANGED=1; }
echo "venv ready: kokoro-onnx $("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("kokoro-onnx"))')"

step "4/6 Models ($MODELS)"
mkdir -p "$MODELS"
fetch(){  # $1 url, $2 file, $3 sha256: download once, check, keep only a file that matches
  local url="$1" out="$MODELS/$2" sha="$3"
  if [ -f "$out" ] && [ "$(sha256sum "$out" | cut -d' ' -f1)" = "$sha" ]; then
    echo "   $2: present"; return 0
  fi
  # The token goes to Hugging Face only, and on curl's standard input: a process's arguments
  # are any local user's to read (ps).
  local auth=() header=""
  if [ -n "${HF_TOKEN:-}" ]; then
    case "$url" in https://huggingface.co/*) auth=(-H @-); header="Authorization: Bearer $HF_TOKEN" ;; esac
  fi
  curl -fL --retry 3 --retry-delay 5 -C - "${auth[@]}" -o "$out.part" "$url" <<< "$header" \
    || die "could not download $2 from $url (no internet, or HuggingFace throttling: set HF_TOKEN). Re-running resumes."
  [ "$(sha256sum "$out.part" | cut -d' ' -f1)" = "$sha" ] \
    || { rm -f "$out.part"; die "$2 does not match its pinned sha256: not keeping it."; }
  mv "$out.part" "$out"; CHANGED=1; echo "   $2: downloaded and checked"
}
fetch "https://huggingface.co/$ASR_REPO/resolve/$ASR_REV/$ASR_FILE" "$ASR_FILE" "$ASR_SHA"
fetch "https://huggingface.co/$ASR_REPO/resolve/$ASR_REV/$MMPROJ_FILE" "$MMPROJ_FILE" "$MMPROJ_SHA"
fetch "$KOKORO_URL/$KOKORO_MODEL_FILE" "$KOKORO_MODEL_FILE" "$KOKORO_MODEL_SHA"
# The recognizer of the other size, after a SPEECH_ASR change: only the files this lane names.
for f in "$MODELS"/Qwen3-ASR-*.gguf "$MODELS"/mmproj-Qwen3-ASR-*.gguf; do
  [ -e "$f" ] || continue
  case "$(basename "$f")" in "$ASR_FILE"|"$MMPROJ_FILE") ;; *) rm -f "$f"; echo "   removed $(basename "$f"), not the recognizer this run installs" ;; esac
done
fetch "$KOKORO_URL/$KOKORO_VOICES_FILE" "$KOKORO_VOICES_FILE" "$KOKORO_VOICES_SHA"
"$VENV/bin/python" - "$MODELS/$KOKORO_MODEL_FILE" "$MODELS/$KOKORO_VOICES_FILE" "$VOICE" <<'PY' || die "SPEECH_VOICE=$VOICE is not one of the voices this lane speaks with (listed above)."
import sys
from kokoro_onnx import Kokoro
# The languages speech-server.py speaks (its LANG_OF_PREFIX): not Japanese, not Mandarin.
voices = [v for v in sorted(Kokoro(sys.argv[1], sys.argv[2]).get_voices()) if v[:1] in "abefhip"]
if sys.argv[3] not in voices:
    print("the voices this lane speaks with: " + " ".join(voices)); sys.exit(1)
PY

step "5/6 Service"
mkdir -p "$CONFIG_DIR"
render(){  # $1 template: every placeholder this lane fills
  sed -e "s|__USER__|$(id -un)|g" -e "s|__GROUP__|$(id -gn "$(id -un)")|g" -e "s|__HOME__|$HOME|g" \
      -e "s|__SPEECH_LANE_DIR__|$LANE_DIR|g" -e "s|__SPEECH_VENV__|$VENV|g" \
      -e "s|__SPEECH_PORT__|$PORT|g" -e "s|__SPEECH_BIND__|$BIND|g" -e "s|__ASR_PORT__|$ASR_PORT|g" \
      -e "s|__SPEECH_VOICE__|$VOICE|g" -e "s|__SPEECH_CPUS__|$UNIT_CPUS|g" \
      -e "s|__ASR_CPUS__|$ASR_CPUS|g" -e "s|__TTS_CPUS__|$TTS_CPUS|g" \
      -e "s|__ASR_THREADS__|$ASR_THREADS|g" -e "s|__TTS_THREADS__|$TTS_THREADS|g" \
      -e "s|__LLAMA_SERVER__|$LLAMA_SERVER|g" -e "s|__ASR_NAME__|$ASR_NAME|g" -e "s|__ASR_CTX__|$ASR_CTX|g" \
      -e "s|__ASR_MODEL__|$MODELS/$ASR_FILE|g" -e "s|__ASR_MMPROJ__|$MODELS/$MMPROJ_FILE|g" \
      -e "s|__KOKORO_MODEL__|$MODELS/$KOKORO_MODEL_FILE|g" -e "s|__KOKORO_VOICES__|$MODELS/$KOKORO_VOICES_FILE|g" \
      -e "s|__SPEECH_MEM_HIGH__|$MEM_HIGH|g" -e "s|__SPEECH_MEM_MAX__|$MEM_MAX|g" -e "s|__SPEECH_MEM_NOTE__|$MEM_NOTE|g" \
      "$1"
}
RENDER_UNIT="$(mktemp)"; RENDER_LAUNCH="$(mktemp)"; trap 'rm -f "$RENDER_UNIT" "$RENDER_LAUNCH"' EXIT
render "$HERE/qwen38-speech.service.template" > "$RENDER_UNIT"
render "$HERE/qwen38-speech-launch.sh.template" > "$RENDER_LAUNCH"
for f in "$RENDER_UNIT" "$RENDER_LAUNCH"; do
  grep -q '__[A-Z][A-Z0-9_]*__' "$f" && die "a template still holds an unsubstituted placeholder: $(grep -o '__[A-Z][A-Z0-9_]*__' "$f" | sort -u | tr '\n' ' ')"
done
if ! cmp -s "$RENDER_LAUNCH" "$LAUNCHER"; then install -m 755 "$RENDER_LAUNCH" "$LAUNCHER"; CHANGED=1; fi
if [ -f "$INSTALLED" ] && sudo cmp -s "$RENDER_UNIT" "$INSTALLED"; then
  echo "unit unchanged"
else
  sudo install -m 644 "$RENDER_UNIT" "$INSTALLED"; sudo systemctl daemon-reload; CHANGED=1
  echo "unit installed at $INSTALLED"
fi
# A running lane serves what it read when it started: anything this run changed needs a restart.
if systemctl is-active --quiet "$UNIT" && [ "$CHANGED" -eq 1 ]; then
  sudo systemctl restart "$UNIT"; echo "restarted, to serve what this run installed"
fi
# Asked to run with the box: from now on, not only from the next boot.
if [ "${SPEECH_BOOT:-0}" = "1" ]; then sudo systemctl enable --now "$UNIT" >/dev/null 2>&1; echo "enabled at boot and running (SPEECH_BOOT=1)"; fi

if [ "$SMOKE" -eq 0 ]; then step "Done (round trip skipped)"; exit 0; fi

step "6/6 Proving it serves (one sentence spoken, then heard back)"
WAS_ACTIVE=0; systemctl is-active --quiet "$UNIT" && WAS_ACTIVE=1
KEEP=$WAS_ACTIVE; [ "${SPEECH_BOOT:-0}" = "1" ] && KEEP=1   # asked to run with the box: left running
SMOKE_DIR="$(mktemp -d)"
cleanup(){
  rm -f "$RENDER_UNIT" "$RENDER_LAUNCH"; rm -rf "$SMOKE_DIR"
  # Left as found: a lane that was not running before the proof is stopped after it.
  if [ "$KEEP" -eq 0 ]; then sudo systemctl stop "$UNIT" 2>/dev/null || true; fi
}
trap cleanup EXIT
[ "$WAS_ACTIVE" -eq 1 ] || sudo systemctl start "$UNIT"
READY=0
for i in $(seq 1 60); do
  curl -fsS -m 5 "http://$BIND:$PORT/health" >/dev/null 2>&1 && { READY=1; break; }
  sleep 3
done
[ "$READY" -eq 1 ] || { sudo journalctl -u "$UNIT" -n 40 --no-pager || true; die "the lane did not answer /health within 3 min; the journal above says why."; }
echo "   up after ~$(( (i - 1) * 3 )) s"
SAY="The speech lane on this box can hear what it says."
CODE=$(curl -sS -o "$SMOKE_DIR/say.wav" -w '%{http_code}' -m 120 "http://$BIND:$PORT/v1/audio/speech" \
  -H 'Content-Type: application/json' -d "{\"input\":\"$SAY\",\"voice\":\"af_heart\"}" || true)
[ "$CODE" = 200 ] || { head -c 300 "$SMOKE_DIR/say.wav" 2>/dev/null; echo; sudo journalctl -u "$UNIT" -n 30 --no-pager || true; die "the voice did not answer (HTTP ${CODE:-000})."; }
CODE=$(curl -sS -o "$SMOKE_DIR/heard.json" -w '%{http_code}' -m 300 "http://$BIND:$PORT/v1/audio/transcriptions" \
  -F "file=@$SMOKE_DIR/say.wav" -F language=en || true)
[ "$CODE" = 200 ] || { head -c 300 "$SMOKE_DIR/heard.json" 2>/dev/null; echo; sudo journalctl -u "$UNIT" -n 30 --no-pager || true; die "the recognizer did not answer (HTTP ${CODE:-000})."; }
python3 - "$SMOKE_DIR/say.wav" "$SMOKE_DIR/heard.json" "$SAY" <<'PY' || die "the round trip came back wrong: the voice or the recognizer does not work as measured."
import json, re, sys, wave
with wave.open(sys.argv[1]) as w:
    seconds = w.getnframes() / w.getframerate()
heard = json.load(open(sys.argv[2]))["text"]
words = lambda s: re.sub(r"[^\w\s]", " ", s.lower()).split()
said, got = words(sys.argv[3]), words(heard)
missing = [w for w in said if w not in got]
print(f"   spoken: {seconds:.1f} s of audio; heard back: \"{heard}\"")
sys.exit(0 if seconds > 1 and len(missing) <= 1 and "<asr_text>" not in heard else 1)
PY
CG="/sys/fs/cgroup$(systemctl show -p ControlGroup --value "$UNIT" 2>/dev/null)"
if [ -r "$CG/cpu.stat" ]; then
  sleep 3; U0=$(awk '/^usage_usec/{print $2}' "$CG/cpu.stat"); sleep 10
  U1=$(awk '/^usage_usec/{print $2}' "$CG/cpu.stat")
  echo "   at rest: $(awk -v a="$U0" -v b="$U1" 'BEGIN{printf "%.2f", (b-a)/1e7}') CPU cores"
  # What the processes hold, not the cgroup's count: that one leaves out the model's pages
  # when something else read them into the page cache first (the download, on a first run).
  RSS_KB=$(for p in $(cat "$CG/cgroup.procs" 2>/dev/null); do awk '/^VmRSS:/ { print $2 }' "/proc/$p/status" 2>/dev/null; done | awk '{ s += $1 } END { print s + 0 }')
  echo "   memory : $(awk -v k="$RSS_KB" 'BEGIN { printf "%.1f GiB", k / 1048576 }') resident"
fi

step "Done: the speech lane is installed and proved it hears what it says, on $BIND:$PORT"
echo "  transcribe : curl http://$BIND:$PORT/v1/audio/transcriptions -F file=@audio.wav -F language=en"
echo "  speak      : curl http://$BIND:$PORT/v1/audio/speech -H 'Content-Type: application/json' \\"
echo "                 -d '{\"input\":\"Hello\",\"voice\":\"$VOICE\"}' -o hello.wav"
echo "  voices     : curl http://$BIND:$PORT/v1/voices"
if [ "$KEEP" -eq 1 ]; then
  echo "  it is running, and stays running"
else
  echo "  start/stop : sudo systemctl start $UNIT   (and stop; SPEECH_BOOT=1 ./install-speech.sh starts it with the box)"
fi
