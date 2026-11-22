#!/usr/bin/env bash
# Install reverse-agent beside every lane, one agent per served family, each started and
# stopped with its lane.
#
#   ./install-raiversed.sh                 install, then start the agents of the active lanes
#   ./install-raiversed.sh --no-start      install the wiring without starting anything
#   ./install-raiversed.sh --uninstall     remove the containers and the lane hooks
#
# Families: text (the keepalive proxy), embedding, speech, transcribe, image and video.
# Each has its own container, state directory and device approval:
#   reverse-agent            ~/reverse-agent/state
#   reverse-agent-embedding  ~/reverse-agent/embedding
#   reverse-agent-speech     ~/reverse-agent/speech
#   reverse-agent-transcribe ~/reverse-agent/transcribe
#   reverse-agent-image      ~/reverse-agent/image
#   reverse-agent-video      ~/reverse-agent/video
#
# Defaults match the reference box's deployment:
#   relay      https://relay.circle-cyber.com
#   issuer     https://auth.circle-cyber.com
#   client id  reverse-agent
#   scope      openid profile offline_access tunnel:serve
#   image      ghcr.io/circle-rd/ai-reverse-agent:nightly
#
# Variables: RAIVERSED_RELAY_URL, RAIVERSED_AUTH_ISSUER, RAIVERSED_CLIENT_ID,
# RAIVERSED_AUTH_RESOURCE, RAIVERSED_AUTH_SCOPE, RAIVERSED_IMAGE, RAIVERSED_STATE_DIR,
# RAIVERSED_API_KEY_FILE, RAIVERSED_CONTAINER, RAIVERSED_HF_METADATA.
set -euo pipefail
while [ -n "${HOME:-}" ] && [ "$HOME" != / ] && [ "${HOME%/}" != "$HOME" ]; do HOME="${HOME%/}"; done; export HOME
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$HERE/raiversed-lane.py"
CONFIG_DIR="$HOME/.config/qwen38"
ENVFILE="$CONFIG_DIR/raiversed.env"
CONTAINER="${RAIVERSED_CONTAINER:-reverse-agent}"
STATE_DIR="${RAIVERSED_STATE_DIR:-$HOME/reverse-agent/state}"
API_KEY_FILE="${RAIVERSED_API_KEY_FILE:-$CONFIG_DIR/api-key}"
IMAGE="${RAIVERSED_IMAGE:-ghcr.io/circle-rd/ai-reverse-agent:nightly}"
RELAY_URL="${RAIVERSED_RELAY_URL:-https://relay.circle-cyber.com}"
AUTH_ISSUER="${RAIVERSED_AUTH_ISSUER:-https://auth.circle-cyber.com}"
CLIENT_ID="${RAIVERSED_CLIENT_ID:-reverse-agent}"
AUTH_RESOURCE="${RAIVERSED_AUTH_RESOURCE:-$RELAY_URL}"
AUTH_SCOPE="${RAIVERSED_AUTH_SCOPE:-openid profile offline_access tunnel:serve}"
HF_METADATA="${RAIVERSED_HF_METADATA:-true}"
FAMILY_SUFFIXES="embedding speech transcribe image video"

die(){ printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
step(){ printf '\n\033[1;36m── %s\033[0m\n' "$*"; }

if [ "$(id -u)" = "0" ] && [ "${ALLOW_ROOT:-0}" != "1" ]; then
  die "run this as the user who will use the box, not as root (everything it installs is addressed from \$HOME)."
fi

ACTION=install; START=1
for a in "$@"; do case "$a" in
  --no-start) START=0 ;;
  --uninstall) ACTION=uninstall ;;
  -h|--help) awk 'NR == 1 { next } /^#/ { print; next } { exit }' "$0"; exit 0 ;;
  *) die "unknown option: $a" ;;
esac; done

lane_units(){
  python3 - "$HERE" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import lane_registry
for spec in lane_registry.LANES.values():
    print(spec["unit"])
PY
}

dropin_body(){
  cat <<EOF
# Managed by install-raiversed.sh. The lane's reverse-agent starts and stops with the lane.
# The leading '-' keeps a reverse-agent failure from failing the lane itself.
[Service]
ExecStartPost=-/usr/bin/python3 "$HOOK" %n start
ExecStopPost=-/usr/bin/python3 "$HOOK" %n stop
EOF
}

if [ "$ACTION" = uninstall ]; then
  step "Removing the reverse-agent lane hooks and containers"
  while read -r unit; do
    [ -n "$unit" ] || continue
    sudo rm -f "/etc/systemd/system/$unit.d/raiversed.conf"
    sudo rmdir "/etc/systemd/system/$unit.d" 2>/dev/null || true
  done < <(lane_units)
  sudo systemctl daemon-reload
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  for f in $FAMILY_SUFFIXES; do docker rm -f "$CONTAINER-$f" >/dev/null 2>&1 || true; done
  rm -f "$ENVFILE" "$CONFIG_DIR"/raiversed-*.lock
  echo "containers and hooks removed"
  echo "the credentials are kept under ~/reverse-agent; delete a family's directory to force its device approval"
  exit 0
fi

step "1/4 Preflight"
for t in docker python3; do
  command -v "$t" >/dev/null || die "$t is missing."
done
docker info >/dev/null 2>&1 || die "docker is not answering; start it or add this user to the docker group."
[ -f "$API_KEY_FILE" ] || die "no engine key at $API_KEY_FILE: run ./install.sh first."
if [ -f "$CONFIG_DIR/launch-embed.sh" ] && ! grep -q -- '--alias' "$CONFIG_DIR/launch-embed.sh"; then
  echo "WARNING: the deployed embedding launcher has no --alias: rerun ./install-embed.sh so the"
  echo "         embedding lane reports pplx-embed-v1-0.6b, the name its agent declares."
fi
mkdir -p "$CONFIG_DIR" "$(dirname "$STATE_DIR")"
chmod +x "$HOOK"

step "2/4 Image"
case "$(uname -m)" in
  aarch64|arm64) PLATFORM=linux/arm64 ;;
  x86_64|amd64) PLATFORM=linux/amd64 ;;
  *) PLATFORM="" ;;
esac
if [ -n "$PLATFORM" ]; then
  docker pull --platform "$PLATFORM" "$IMAGE" >/dev/null || die "could not pull $IMAGE"
else
  docker pull "$IMAGE" >/dev/null || die "could not pull $IMAGE"
fi
echo "image ready: $IMAGE"

step "3/4 Configuration and lane hooks"
umask 077
{
  printf 'RAIVERSED_IMAGE=%s\n' "$IMAGE"
  printf 'RAIVERSED_CONTAINER=%s\n' "$CONTAINER"
  printf 'RAIVERSED_RELAY_URL=%s\n' "$RELAY_URL"
  printf 'RAIVERSED_AUTH_ISSUER=%s\n' "$AUTH_ISSUER"
  printf 'RAIVERSED_CLIENT_ID=%s\n' "$CLIENT_ID"
  printf 'RAIVERSED_AUTH_RESOURCE=%s\n' "$AUTH_RESOURCE"
  printf 'RAIVERSED_AUTH_SCOPE=%s\n' "$AUTH_SCOPE"
  printf 'RAIVERSED_STATE_DIR=%s\n' "$STATE_DIR"
  printf 'RAIVERSED_API_KEY_FILE=%s\n' "$API_KEY_FILE"
  printf 'RAIVERSED_HF_METADATA=%s\n' "$HF_METADATA"
} > "$ENVFILE"
chmod 600 "$ENVFILE"
umask 022
echo "settings written to $ENVFILE"

TMPDROP="$(mktemp)"
trap 'rm -f "$TMPDROP"' EXIT
dropin_body > "$TMPDROP"
while read -r unit; do
  [ -n "$unit" ] || continue
  sudo install -D -m 644 "$TMPDROP" "/etc/systemd/system/$unit.d/raiversed.conf"
done < <(lane_units)
sudo systemctl daemon-reload
echo "lane hooks installed on all $(lane_units | wc -l) lane units"

step "4/4 Starting"
if [ "$START" -eq 1 ]; then
  while read -r unit; do
    [ -n "$unit" ] || continue
    if [ "$(systemctl is-active "$unit" 2>/dev/null || true)" = active ]; then
      python3 "$HOOK" "$unit" start || die "reverse-agent for $unit did not start; see the message above"
    fi
  done < <(lane_units)
  echo
  echo "First start of each family: open the verification link printed by its container"
  echo "(docker logs <container>) and approve the code. Each credential is reused across restarts."
else
  echo "not started (--no-start); each agent starts with its lane"
fi
echo
echo "  text       : $CONTAINER              relay $RELAY_URL  client $CLIENT_ID"
echo "  families   : embedding, speech, transcribe, image, video (one container and approval each)"
echo "  note       : the transcription id is declared from the GGUF file name; check it with"
echo "               curl -s http://127.0.0.1:30036/v1/models after the lane starts"
