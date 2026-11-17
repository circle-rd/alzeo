#!/usr/bin/env bash
# Install the embedding lane: PPLX Embed v1 0.6B in Text Embeddings Inference, on the CPU,
# behind the box's own OpenAI-compatible /v1/embeddings route and the same API key.
# Idempotent, and safe to run alone.
#
#   ./install-embed.sh                 first install, or update in place
#   ./install-embed.sh --no-smoke      skip the round trip at the end
#   ./install-embed.sh --uninstall     remove the unit and the wiring (the model cache stays)
#
# Variables: EMBED_PORT (30026), EMBED_BOOT=1 (start it with the box, off by default),
# EMBED_LANE_DIR (~/.local/share/qwen38-embed), EMBED_BATCH_TOKENS (2048, which is also
# the longest input it takes), EMBED_CONCURRENCY (8), EMBED_MEM_MAX (4g), EMBED_CPUS.
#
# WHY THE CPU. The box serves one GPU lane at a time, and the flash lane holds almost all
# of the unified memory. Measured on the reference box on 2026-11-17: TEI's defaults
# (--max-batch-tokens 16384, 512 concurrent requests, 19 tokenizers) held 22.4 GiB and
# 33.8 GiB at peak, enough to take the flash lane's load down; at 2048 batch tokens, 8
# concurrent requests and 2 tokenizers, the same model held 2.5 GiB and answered a short
# request in 59 ms. TEI clamps max_input_length to the batch budget, so 2048 tokens is
# the longest text this lane takes: chunk a codebase at or below it.
#
# NOT IN THE ONE COMMAND YET. ./install.sh does not run this: the lane is installed on
# purpose, by this script. It is not started at boot unless EMBED_BOOT=1.
set -euo pipefail
while [ -n "${HOME:-}" ] && [ "$HOME" != / ] && [ "${HOME%/}" != "$HOME" ]; do HOME="${HOME%/}"; done; export HOME
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=qwen38-embed.service
INSTALLED="/etc/systemd/system/$UNIT"
DROPIN_DIR="/etc/systemd/system/qwen38-keepalive.service.d"
DROPIN="$DROPIN_DIR/embed.conf"
die(){ printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
step(){ printf '\n\033[1;36m── %s\033[0m\n' "$*"; }
# shellcheck source=lanes.sh
. "$HERE/lanes.sh"

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

installed(){ { grep -m1 -E "^$1=" "$INSTALLED" 2>/dev/null || true; } | cut -d= -f2-; }
CONFIG_DIR="$HOME/.config/qwen38"
LAUNCHER="$CONFIG_DIR/launch-embed.sh"
ENVFILE="$CONFIG_DIR/embed.env"
launcher_flag(){ { grep -oE -- "$1 [^ \\\\]+" "$LAUNCHER" 2>/dev/null || true; } | awk '{print $2}' | tail -1; }
LANE_DIR="${EMBED_LANE_DIR:-$(installed WorkingDirectory | tr -s /)}"; LANE_DIR="${LANE_DIR:-$HOME/.local/share/qwen38-embed}"
PORT="${EMBED_PORT:-$(launcher_flag --port)}"; PORT="${PORT:-30026}"
CACHE="$LANE_DIR/hf"

# The pins. TEI 1.9.4 is the first release the model card names for this checkpoint, and
# the model revision is what an index must name: a moving main changes the vectors.
EMBED_IMAGE="${EMBED_IMAGE:-ghcr.io/huggingface/text-embeddings-inference:cpu-arm64-1.9@sha256:798cd7615e060a72ccbdf19aa0ada52289c6a0147490195bafaece6ce04d70f1}"
EMBED_MODEL="${EMBED_MODEL:-perplexity-ai/pplx-embed-v1-0.6b}"
EMBED_REV="${EMBED_REV:-2c4d510dd4a732063c31a0f70193e35067b51fd8}"
EMBED_NAME="${EMBED_NAME:-pplx-embed-v1-0.6b}"
EMBED_DTYPE="${EMBED_DTYPE:-float32}"
EMBED_BATCH_TOKENS="${EMBED_BATCH_TOKENS:-2048}"
EMBED_CONCURRENCY="${EMBED_CONCURRENCY:-8}"
EMBED_CLIENT_BATCH="${EMBED_CLIENT_BATCH:-64}"
EMBED_TOKENIZER_WORKERS="${EMBED_TOKENIZER_WORKERS:-2}"
EMBED_MEM_MAX="${EMBED_MEM_MAX:-4g}"
MODEL_GB=3
RUNTIME_GB=2

if [ "$ACTION" = uninstall ]; then
  step "Removing the embedding lane"
  if [ -f "$INSTALLED" ]; then
    sudo systemctl disable --now "$UNIT" 2>/dev/null || true
    sudo rm -f "$INSTALLED"; echo "unit removed"
  fi
  if [ -f "$DROPIN" ]; then
    sudo rm -f "$DROPIN"
    sudo rmdir "$DROPIN_DIR" 2>/dev/null || true
    sudo systemctl daemon-reload
    if systemctl is-active --quiet qwen38-keepalive.service; then
      sudo systemctl restart qwen38-keepalive.service
      echo "keepalive proxy restarted without the embedding route"
    fi
  fi
  docker rm -f qwen38-embed >/dev/null 2>&1 || true
  rm -f "$LAUNCHER" "$ENVFILE"
  if [ -d "$CACHE" ]; then
    echo "the model cache is left in $CACHE ($(du -sh "$CACHE" 2>/dev/null | cut -f1)); delete it yourself if you want the space:"
    echo "  rm -rf $CACHE && rmdir $LANE_DIR 2>/dev/null || true"
  else
    rmdir "$LANE_DIR" 2>/dev/null || echo "kept $LANE_DIR: it holds files the embedding lane did not put there"
  fi
  exit 0
fi

step "1/5 Preflight"
case "$(uname -m)" in aarch64|arm64) : ;; *) die "this lane is verified on the DGX Spark's ARM64 GB10 only (this box is $(uname -m))." ;; esac
HOST_PKGS=()
for t in docker:docker.io curl:curl; do
  command -v "${t%%:*}" >/dev/null || HOST_PKGS+=("${t#*:}")
done
[ "${#HOST_PKGS[@]}" -eq 0 ] || host_packages "the embedding lane" "${HOST_PKGS[@]}" || true
for t in docker curl ss; do
  command -v "$t" >/dev/null || die "$t is missing. Fix: sudo apt-get install -y docker.io curl iproute2"
done
docker info >/dev/null 2>&1 || die "docker is not answering; start it (sudo systemctl start docker) or add this user to the docker group."
existing(){ local p="$1"; while [ ! -e "$p" ]; do p="$(dirname "$p")"; done; printf '%s\n' "$p"; }
free_gb(){ { df -BG --output=avail "$(existing "$1")" 2>/dev/null || true; } | tail -1 | tr -dc '0-9'; }
FREE="$(free_gb "$LANE_DIR")"; WANT=$((MODEL_GB + RUNTIME_GB + 1))
[ "${FREE:-0}" -ge "$WANT" ] || die "${FREE:-?} GiB free under $LANE_DIR, this lane needs about $WANT GiB. Free some space, or point EMBED_LANE_DIR at a bigger disk."
if ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q . && ! systemctl is-active --quiet "$UNIT" 2>/dev/null; then
  die "port $PORT is taken by another program; set EMBED_PORT."
fi
# The small cores, from the CPUs' own maximum frequencies: on the GB10 the ten Cortex-A725
# (2.8 GHz, CPUs 0-4 and 10-14) against the ten Cortex-X925 (3.9 GHz). The text engines'
# busy threads run on the big ones, so this lane keeps to the small ones, as the speech
# lane does. A CPU with one kind of core is split down the middle.
small_cores(){  # stdin: "CPU MAXMHZ" lines; stdout: the CPUs of the slowest kind, sorted
  awk 'NF==2 && $1 ~ /^[0-9]+$/ { mhz[$1]=int($2); if (min=="" || int($2)<min) min=int($2); n++ }
       END { k=0; for (c in mhz) if (mhz[c]==min) k++
             for (c in mhz) if (mhz[c]==min || k<4) print c }' | sort -n
}
CORES="${EMBED_CPUS:-$(lscpu -e=CPU,MAXMHZ 2>/dev/null | tail -n +2 | small_cores | paste -sd' ' -)}"
read -r -a CORE_LIST <<< "$CORES"
[ "${#CORE_LIST[@]}" -ge 2 ] || die "could not read the CPUs' frequencies (lscpu -e=CPU,MAXMHZ); set EMBED_CPUS."
EMBED_CPUS="$(printf '%s\n' "${CORE_LIST[@]}" | paste -sd, -)"
EMBED_THREADS="${EMBED_THREADS:-${#CORE_LIST[@]}}"
[ "$EMBED_THREADS" -le "${#CORE_LIST[@]}" ] || EMBED_THREADS=${#CORE_LIST[@]}
echo "OK: $(uname -m), $EMBED_MODEL at ${EMBED_REV:0:12}, on CPUs $EMBED_CPUS, batch $EMBED_BATCH_TOKENS tokens"

step "2/5 Image and model cache"
mkdir -p "$CACHE"
docker pull --platform linux/arm64 "$EMBED_IMAGE" >/dev/null || die "could not pull $EMBED_IMAGE. Check the network and the image pin."
echo "image ready: $EMBED_IMAGE"

step "3/5 Service and proxy wiring"
CHANGED=0
mkdir -p "$CONFIG_DIR"
# The route lives in this branch's proxy, and the box runs the copy install.sh put in
# CONFIG_DIR, not the clone's: a git update alone left the old proxy in memory and the
# route reached the text engine, which answered 400 (seen on the reference box, 2026-11-17).
# Install only when the deployed copy has no route at all, and leave every other difference
# to ./install.sh, which owns that file.
[ -f "$CONFIG_DIR/keepalive-proxy.py" ] || die "no proxy at $CONFIG_DIR/keepalive-proxy.py: run ./install.sh first."
grep -q '^EMBED_UPSTREAM' "$HERE/keepalive-proxy.py" || die "this branch's keepalive-proxy.py has no embedding route."
if ! grep -q '^EMBED_UPSTREAM' "$CONFIG_DIR/keepalive-proxy.py"; then
  install -m 755 "$HERE/keepalive-proxy.py" "$CONFIG_DIR/keepalive-proxy.py"; CHANGED=1
  echo "proxy updated from this branch: it carries the /v1/embeddings route"
fi
# The key the lane checks is the box's own: one key opens the text engine and the
# embedding lane, and the proxy already holds it. The file is private, and the proxy's
# drop-in reads it so the two cannot disagree about which key is current.
umask 077
printf 'EMBED_API_KEY=%s\n' "$(cat "$CONFIG_DIR/api-key")" > "$ENVFILE"
chmod 600 "$ENVFILE"
umask 022
render(){  # $1 template: every placeholder this lane fills
  sed -e "s|__USER__|$(id -un)|g" -e "s|__GROUP__|$(id -gn "$(id -un)")|g" -e "s|__HOME__|$HOME|g" \
      -e "s|__CONFIG_DIR__|$CONFIG_DIR|g" -e "s|__EMBED_LANE_DIR__|$LANE_DIR|g" \
      -e "s|__EMBED_PORT__|$PORT|g" -e "s|__EMBED_CPUS__|$EMBED_CPUS|g" \
      -e "s|__EMBED_THREADS__|$EMBED_THREADS|g" -e "s|__EMBED_MEM_MAX__|$EMBED_MEM_MAX|g" \
      -e "s|__EMBED_IMAGE__|$EMBED_IMAGE|g" -e "s|__EMBED_MODEL__|$EMBED_MODEL|g" \
      -e "s|__EMBED_REV__|$EMBED_REV|g" -e "s|__EMBED_NAME__|$EMBED_NAME|g" \
      -e "s|__EMBED_DTYPE__|$EMBED_DTYPE|g" -e "s|__EMBED_BATCH_TOKENS__|$EMBED_BATCH_TOKENS|g" \
      -e "s|__EMBED_CONCURRENCY__|$EMBED_CONCURRENCY|g" -e "s|__EMBED_CLIENT_BATCH__|$EMBED_CLIENT_BATCH|g" \
      -e "s|__EMBED_TOKENIZER_WORKERS__|$EMBED_TOKENIZER_WORKERS|g" \
      "$1"
}
RENDER_UNIT="$(mktemp)"; RENDER_LAUNCH="$(mktemp)"; RENDER_DROPIN="$(mktemp)"
trap 'rm -f "$RENDER_UNIT" "$RENDER_LAUNCH" "$RENDER_DROPIN"' EXIT
render "$HERE/qwen38-embed.service.template" > "$RENDER_UNIT"
render "$HERE/qwen38-embed-launch.sh.template" > "$RENDER_LAUNCH"
printf '[Service]\nEnvironment=EMBED_UPSTREAM=http://127.0.0.1:%s\nEnvironmentFile=-%s\n' "$PORT" "$ENVFILE" > "$RENDER_DROPIN"
for f in "$RENDER_UNIT" "$RENDER_LAUNCH" "$RENDER_DROPIN"; do
  grep -q '__[A-Z][A-Z0-9_]*__' "$f" && die "a template still holds an unsubstituted placeholder: $(grep -o '__[A-Z][A-Z0-9_]*__' "$f" | sort -u | tr '\n' ' ')"
done
if ! cmp -s "$RENDER_LAUNCH" "$LAUNCHER"; then install -m 755 "$RENDER_LAUNCH" "$LAUNCHER"; CHANGED=1; fi
if [ -f "$INSTALLED" ] && sudo cmp -s "$RENDER_UNIT" "$INSTALLED"; then
  echo "unit unchanged"
else
  sudo install -m 644 "$RENDER_UNIT" "$INSTALLED"; CHANGED=1
  echo "unit installed at $INSTALLED"
fi
sudo mkdir -p "$DROPIN_DIR"
if sudo cmp -s "$RENDER_DROPIN" "$DROPIN"; then
  echo "proxy drop-in unchanged"
else
  sudo install -m 644 "$RENDER_DROPIN" "$DROPIN"; CHANGED=1
  echo "proxy drop-in installed at $DROPIN"
fi
sudo systemctl daemon-reload
if systemctl is-active --quiet "$UNIT" && [ "$CHANGED" -eq 1 ]; then
  sudo systemctl restart "$UNIT"; echo "embedding lane restarted, to serve what this run installed"
fi
# The proxy reads EMBED_UPSTREAM at start, so a changed drop-in needs it restarted. It is
# the box's only door: this cuts requests in flight, once, and says so.
if [ "$CHANGED" -eq 1 ] && systemctl is-active --quiet qwen38-keepalive.service; then
  sudo systemctl restart qwen38-keepalive.service
  echo "keepalive proxy restarted: /v1/embeddings now reaches the embedding lane"
fi
if [ "${EMBED_BOOT:-0}" = "1" ]; then sudo systemctl enable --now "$UNIT" >/dev/null 2>&1; echo "enabled at boot and running (EMBED_BOOT=1)"; fi

if [ "$SMOKE" -eq 0 ]; then step "Done (round trip skipped)"; exit 0; fi

step "4/5 Proving the lane serves"
WAS_ACTIVE=0; systemctl is-active --quiet "$UNIT" && WAS_ACTIVE=1
KEEP=$WAS_ACTIVE; [ "${EMBED_BOOT:-0}" = "1" ] && KEEP=1
SMOKE_DIR="$(mktemp -d)"
cleanup(){
  rm -f "$RENDER_UNIT" "$RENDER_LAUNCH" "$RENDER_DROPIN"; rm -rf "$SMOKE_DIR"
  if [ "$KEEP" -eq 0 ]; then sudo systemctl stop "$UNIT" 2>/dev/null || true; fi
}
trap cleanup EXIT
[ "$WAS_ACTIVE" -eq 1 ] || sudo systemctl start "$UNIT"
READY=0
for i in $(seq 1 60); do
  curl -fsS -m 5 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && { READY=1; break; }
  sleep 2
done
[ "$READY" -eq 1 ] || { sudo journalctl -u "$UNIT" -n 40 --no-pager || true; die "the lane did not answer /health within 2 min; the journal above says why."; }
echo "   up after ~$(( (i - 1) * 2 )) s"
KEY="$(cat "$CONFIG_DIR/api-key")"
CODE=$(curl -sS -o "$SMOKE_DIR/direct.json" -w '%{http_code}' -m 120 "http://127.0.0.1:$PORT/v1/embeddings" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"pplx-embed-v1-0.6b","input":["La curiosité scientifique","L exploration de l univers"]}' || true)
[ "$CODE" = 200 ] || { head -c 300 "$SMOKE_DIR/direct.json" 2>/dev/null; echo; sudo journalctl -u "$UNIT" -n 30 --no-pager || true; die "the embedding lane did not answer (HTTP ${CODE:-000})."; }
python3 - "$SMOKE_DIR/direct.json" <<'PY' || die "the embedding lane answered something other than two 1024-dimension vectors."
import json, sys
doc = json.load(open(sys.argv[1]))
rows = doc.get("data") or []
assert len(rows) == 2, rows
assert all(len(r["embedding"]) == 1024 for r in rows), [len(r["embedding"]) for r in rows]
print(f"   direct: {len(rows)} vectors of {len(rows[0]['embedding'])} dimensions")
PY
PROXY_PORT="$(grep -oE 'keepalive-proxy\.py [0-9]+' /etc/systemd/system/qwen38-keepalive.service 2>/dev/null | head -1 | tr -dc '0-9' || true)"
if [ -n "$PROXY_PORT" ] && systemctl is-active --quiet qwen38-keepalive.service; then
  CODE=$(curl -sS -o "$SMOKE_DIR/proxy.json" -w '%{http_code}' -m 120 "http://127.0.0.1:$PROXY_PORT/v1/embeddings" \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d '{"model":"pplx-embed-v1-0.6b","input":["La curiosité scientifique","L exploration de l univers"]}' || true)
  [ "$CODE" = 200 ] || { head -c 300 "$SMOKE_DIR/proxy.json" 2>/dev/null; echo; die "the proxy did not relay /v1/embeddings (HTTP ${CODE:-000})."; }
  python3 - "$SMOKE_DIR/proxy.json" <<'PY' || die "the proxy relayed something other than two 1024-dimension vectors."
import json, sys
doc = json.load(open(sys.argv[1]))
rows = doc.get("data") or []
assert len(rows) == 2 and all(len(r["embedding"]) == 1024 for r in rows), doc
print(f"   proxy : {len(rows)} vectors of {len(rows[0]['embedding'])} dimensions, same key")
PY
else
  echo "   proxy : skipped, qwen38-keepalive.service is not running"
fi

step "5/5 Done"
echo "  embeddings : curl http://127.0.0.1:${PROXY_PORT:-30001}/v1/embeddings -H 'Authorization: Bearer <api-key>' \\"
echo "                 -H 'Content-Type: application/json' -d '{\"input\":[\"hello\"]}'"
echo "  model      : $EMBED_MODEL, 1024 dimensions, cosine similarity"
echo "  start/stop : sudo systemctl start $UNIT   (and stop; EMBED_BOOT=1 ./install-embed.sh starts it with the box)"
if [ "$KEEP" -eq 0 ]; then echo "  left stopped, as it was: the proxy's /v1/embeddings answers 503 until it is started"; fi
