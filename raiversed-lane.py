#!/usr/bin/env python3
"""Keep one reverse-agent per served family, each started and stopped with its lane.

Each lane unit owns one agent: text (the keepalive proxy), embedding, speech, transcription,
image and video. Their models are declared statically here, because at ExecStartPost the
engine is still loading its weights and answers nothing on /v1/models. The agent polls its
own upstream afterwards, so an engine that is slow to load is reported as unavailable, not
missing. Separate agents mean separate identities: each family costs one device approval and
no lane has to arbitrate with another.

The drop-ins call this script with the unit name and "start" or "stop". The leading '-' in
the drop-in keeps a reverse-agent failure from failing the lane itself.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from contextlib import contextmanager

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import lane_registry

CONFIG_DIR = pathlib.Path(os.environ.get("RAIVERSED_CONFIG_DIR", "~/.config/qwen38")).expanduser()
ENV_FILE = CONFIG_DIR / "raiversed.env"
TEXT_PROXY = "http://127.0.0.1:30001"
SPEECH_PORT = 30024
TRANSCRIBE_PORT = 30036
VIDEO_PORT = 30022
EMBED_DROPIN = pathlib.Path("/etc/systemd/system/qwen38-keepalive.service.d/embed.conf")

# The ids the engines answer with, declared because the hook cannot read them at start.
# Speech: the names install-speech.sh gives its recognizer and the voice. Transcription: the
# name llama-server derives from the GGUF file (verify with /v1/models after an install).
SPEECH_MODELS = (("qwen3-asr-0.6b", ("transcription",)), ("kokoro-82m", ("speech",)))
TRANSCRIBE_MODEL = ("NVIDIA-Nemotron-3.5-Transcribe-30B-A4B.Q8_0.gguf", ("transcription",))
EMBED_MODEL = ("pplx-embed-v1-0.6b", ("embedding",))
# Image and video engines do not list their model in /v1/models, so the id is declared as an
# extra as well. It follows the install's default checkpoint.
MEDIA_MODELS = {
    "qwen38-image.service": ("Qwen/Qwen-Image-2.1", ("image",)),
    "qwen38-image-h3.service": ("MiniMaxAI/MiniMax-H3-Image", ("image",)),
    "qwen38-video.service": ("MiniMaxAI/MiniMax-H3", ("video",)),
}

FAMILY_OF_KIND = {"text": "text", "embedding": "embedding", "speech": "speech",
                  "transcribe": "transcribe", "image": "image", "video": "video"}
ACTIONS = ("start", "stop")

DEFAULTS = {
    "RAIVERSED_IMAGE": "ghcr.io/circle-rd/ai-reverse-agent:nightly",
    "RAIVERSED_CONTAINER": "reverse-agent",
    "RAIVERSED_RELAY_URL": "https://relay.circle-cyber.com",
    "RAIVERSED_AUTH_ISSUER": "https://auth.circle-cyber.com",
    "RAIVERSED_CLIENT_ID": "reverse-agent",
    "RAIVERSED_AUTH_RESOURCE": "https://relay.circle-cyber.com",
    "RAIVERSED_AUTH_SCOPE": "openid profile offline_access tunnel:serve",
    "RAIVERSED_STATE_DIR": "~/reverse-agent/state",
    "RAIVERSED_API_KEY_FILE": "~/.config/qwen38/api-key",
    "RAIVERSED_HF_METADATA": "true",
}


def log(message: str) -> None:
    print(f"raiversed: {message}", file=sys.stderr)


def read_config() -> dict[str, str]:
    values = dict(DEFAULTS)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, sep, value = line.partition("=")
                if sep:
                    values[key.strip()] = value
    for key in ("RAIVERSED_STATE_DIR", "RAIVERSED_API_KEY_FILE"):
        values[key] = str(pathlib.Path(values[key]).expanduser())
    return values


def lane_for_unit(unit: str) -> tuple[str, dict] | None:
    for name, spec in lane_registry.LANES.items():
        if spec["unit"] == unit:
            return name, spec
    return None


def family_of(spec: dict) -> str | None:
    return FAMILY_OF_KIND.get(spec["kind"])


def text_tags(name: str) -> tuple[str, ...]:
    if re.search(r"(^|[-_])vl($|[-_])|vision", name, re.IGNORECASE):
        return ("text", "vision", "tools")
    return ("text", "tools")


def embedding_upstream() -> str:
    """The port the embedding lane listens on: the proxy's drop-in, then its launcher."""
    try:
        found = re.search(r"^Environment=EMBED_UPSTREAM=(\S+)", EMBED_DROPIN.read_text(), re.MULTILINE)
        if found:
            return found.group(1)
    except OSError:
        pass
    try:
        ports = re.findall(r"--port\s+([0-9]+)", (CONFIG_DIR / "launch-embed.sh").read_text())
        if ports:
            return f"http://127.0.0.1:{ports[-1]}"
    except OSError:
        pass
    return f"http://127.0.0.1:{lane_registry.LANES['embedding']['ports'][0]}"


def announced(unit: str, lane: str, spec: dict) -> tuple[str, list[tuple[str, tuple[str, ...]]], bool]:
    """The upstream, the (model, tags) pairs, and whether the models are declared as extras."""
    family = family_of(spec)
    if family == "text":
        model = spec["served"]
        return TEXT_PROXY, [(model, text_tags(model))], False
    if family == "embedding":
        return embedding_upstream(), [EMBED_MODEL], False
    if family == "speech":
        return f"http://127.0.0.1:{SPEECH_PORT}", list(SPEECH_MODELS), False
    if family == "transcribe":
        return f"http://127.0.0.1:{TRANSCRIBE_PORT}", [TRANSCRIBE_MODEL], False
    if family in ("image", "video"):
        # TODO: translate the OpenAI video body for the Alzeo lane. The lane requires `task`
        # (t2va or fl2va) and `target`, and takes seconds from 4 to 15; the OpenAI shape has
        # neither and restricts seconds to 4, 8 or 12. reverse-agent forwards bodies unchanged.
        model = MEDIA_MODELS[unit]
        return f"http://127.0.0.1:{spec['ports'][0]}", [model], True
    raise RuntimeError(f"no reverse-agent family for {lane} ({spec['kind']})")


def container_name(base: str, family: str) -> str:
    return base if family == "text" else f"{base}-{family}"


def state_dir(config: dict[str, str], family: str) -> pathlib.Path:
    base = pathlib.Path(config["RAIVERSED_STATE_DIR"])
    return base if family == "text" else base.parent / family


def desired_env(upstream: str, models: list[tuple[str, tuple[str, ...]]],
                extra: bool, config: dict[str, str]) -> dict[str, str]:
    env = {
        "AGENT_RELAY_URL": config["RAIVERSED_RELAY_URL"],
        "AGENT_UPSTREAM_URL": upstream,
        "AGENT_AUTH_ISSUER": config["RAIVERSED_AUTH_ISSUER"],
        "AGENT_AUTH_CLIENT_ID": config["RAIVERSED_CLIENT_ID"],
        "AGENT_AUTH_RESOURCE": config["RAIVERSED_AUTH_RESOURCE"],
        "AGENT_AUTH_SCOPE": config["RAIVERSED_AUTH_SCOPE"],
        "AGENT_UPSTREAM_API_KEY_FILE": "/run/secrets/upstream-key",
        "AGENT_CREDENTIAL_FILE": "/data/credential",
        "AGENT_MODELS_POLL_SECS": "5",
        "AGENT_RECONNECT_MIN_SECS": "1",
        "AGENT_RECONNECT_MAX_SECS": "30",
        "AGENT_CONNECT_TIMEOUT_SECS": "5",
        "AGENT_UPSTREAM_HEADERS_TIMEOUT_SECS": "900",
        "AGENT_IDLE_TIMEOUT_SECS": "600",
        "AGENT_MAX_REQUEST_BYTES": "67108864",
        "AGENT_HF_METADATA": config["RAIVERSED_HF_METADATA"],
        "AGENT_HF_TIMEOUT_SECS": "5",
        "AGENT_INSECURE_TRANSPORT": "false",
        "RUST_LOG": "info",
        "AGENT_MODEL_TAGS": ";".join(f"{name}={','.join(tags)}" for name, tags in models),
    }
    if extra:
        env["AGENT_EXTRA_MODELS"] = ",".join(name for name, _ in models)
    return env


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, capture_output=True, text=True)


def container_exists(name: str) -> bool:
    try:
        run(["docker", "inspect", name])
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def remove(name: str, dry_run: bool) -> None:
    if dry_run:
        print(json.dumps({"remove": name}))
        return
    if container_exists(name):
        run(["docker", "rm", "-f", name])
        log(f"stopped {name}")


def recreate(name: str, family: str, env: dict[str, str], config: dict[str, str], dry_run: bool) -> None:
    key_file = pathlib.Path(config["RAIVERSED_API_KEY_FILE"])
    if not key_file.exists():
        raise RuntimeError(f"no engine key at {key_file}: run ./install.sh first")
    state = state_dir(config, family)
    argv = [
        "docker", "run", "-d", "--name", name,
        "--restart", "unless-stopped",
        "--network", "host", "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--ulimit", "core=0",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{state}:/data",
        "-v", f"{key_file}:/run/secrets/upstream-key:ro",
    ]
    for key, value in env.items():
        argv.extend(["-e", f"{key}={value}"])
    argv.append(config["RAIVERSED_IMAGE"])
    if dry_run:
        print(json.dumps({"argv": argv, "env": env}, indent=2))
        return
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    if container_exists(name):
        run(["docker", "rm", "-f", name])
    run(argv)
    log(f"{name} -> {env['AGENT_UPSTREAM_URL']} ({env['AGENT_MODEL_TAGS']})")


@contextmanager
def locked(family: str):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_DIR / f"raiversed-{family}.lock", "w") as handle:
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.2)
        yield True


def handle(unit: str, action: str, dry_run: bool) -> int:
    known = lane_for_unit(unit)
    if known is None:
        log(f"{unit} is not a lane; nothing to do")
        return 0
    lane, spec = known
    family = family_of(spec)
    if family is None:
        log(f"{lane} has no reverse-agent family")
        return 0
    config = read_config()
    name = container_name(config["RAIVERSED_CONTAINER"], family)
    with locked(family) as acquired:
        if not acquired:
            log(f"another {family} event is still updating {name}")
            return 0
        if action == "stop":
            remove(name, dry_run)
            return 0
        upstream, models, extra = announced(unit, lane, spec)
        env = desired_env(upstream, models, extra, config)
        recreate(name, family, env, config, dry_run)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unit", help="the lane unit systemd just started or stopped")
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--dry-run", action="store_true", help="print the docker commands instead")
    args = parser.parse_args()
    return handle(args.unit, args.action, args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        log(str(error))
        sys.exit(1)
