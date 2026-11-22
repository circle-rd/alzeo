# reverse-agent beside the lanes

`install-raiversed.sh` runs `reverse-agent` from
[circle-rd/ai-reverse](https://github.com/circle-rd/ai-reverse) on this box: an
outbound-only tunnel that publishes the box's engines to `relay.circle-cyber.com`,
through the `reverse-agent` OAuth client.

One agent serves one family of engines, and it starts and stops with the lane that
belongs to it. The relay routes each request to the agent that announces its model,
so several agents on the same account coexist without arbitration.

## Families

| Family | Lanes (unit) | Container | State | Upstream | Announced models and tags |
| --- | --- | --- | --- | --- | --- |
| text | every text lane, one at a time | `reverse-agent` | `~/reverse-agent/state` | `127.0.0.1:30001` (keepalive proxy) | the lane's served name, `text,tools` (`vision` for the VL lanes) |
| embedding | `qwen38-embed` | `reverse-agent-embedding` | `~/reverse-agent/embedding` | the port in the proxy's `embed.conf` | `pplx-embed-v1-0.6b`, `embedding` |
| speech | `qwen38-speech` | `reverse-agent-speech` | `~/reverse-agent/speech` | `127.0.0.1:30024` | `qwen3-asr-0.6b` as `transcription`, `kokoro-82m` as `speech` |
| transcribe | `qwen38-transcribe` | `reverse-agent-transcribe` | `~/reverse-agent/transcribe` | `127.0.0.1:30036` | the GGUF-derived id, `transcription` |
| image | `qwen38-image`, `qwen38-image-h3` | `reverse-agent-image` | `~/reverse-agent/image` | the unit's own port (30020, 30026) | `Qwen/Qwen-Image-2.1` or `MiniMaxAI/MiniMax-H3-Image`, `image` |
| video | `qwen38-video` | `reverse-agent-video` | `~/reverse-agent/video` | `127.0.0.1:30022` | `MiniMaxAI/MiniMax-H3`, `video` |

Each family is one device approval, once. The approval is the cost of keeping
identities apart, and it is what removes any arbitration between lanes.

The models are declared in `raiversed-lane.py`, not discovered. At `ExecStartPost` the
engine is still loading its weights and answers nothing on `/v1/models`. The agent polls
its own upstream after that, so a slow engine is reported unavailable, not missing.
The image and video engines do not list their model, so their ids are also declared as
`AGENT_EXTRA_MODELS`.

## Install

```bash
./install-raiversed.sh
```

Then, for each family, open the verification link printed by its container
(`docker logs reverse-agent-<family>`) and approve the code. Each credential is kept in
its state directory and reused across restarts.

Defaults match the reference box:

| Setting | Default |
| --- | --- |
| Relay | `https://relay.circle-cyber.com` |
| Auth issuer | `https://auth.circle-cyber.com` |
| Client id | `reverse-agent` |
| Scope | `openid profile offline_access tunnel:serve` |
| Image | `ghcr.io/circle-rd/ai-reverse-agent:nightly` |
| Engine key | `~/.config/qwen38/api-key`, mounted read-only |

Override them with `RAIVERSED_RELAY_URL`, `RAIVERSED_AUTH_ISSUER`,
`RAIVERSED_CLIENT_ID`, `RAIVERSED_AUTH_RESOURCE`, `RAIVERSED_AUTH_SCOPE`,
`RAIVERSED_IMAGE`, `RAIVERSED_STATE_DIR`, `RAIVERSED_API_KEY_FILE`,
`RAIVERSED_CONTAINER`, or `RAIVERSED_HF_METADATA`.

## Routes that cross the tunnel

`reverse-core`'s allowlist, as LiteLLM proxies the OpenAI surfaces:

- text and embeddings: chat, completions, responses, messages, embeddings;
- images: `POST /v1/images/generations`;
- audio: `POST /v1/audio/transcriptions`, `POST /v1/audio/speech`;
- video: `POST /v1/videos`, `GET /v1/videos`, `GET /v1/videos/{id}`,
  `GET /v1/videos/{id}/content`, `POST /v1/videos/{id}/remix`, `POST /v1/videos/edits`,
  `POST /v1/videos/extensions`, `POST /v1/videos/characters`,
  `GET /v1/videos/characters/{id}`.

Realtime websockets are not forwarded. OpenAI shut down its Sora Videos API on
24 September 2026 with no replacement, so the video shape is LiteLLM's, which is also
the Alzeo lane's.

### Video body translation (deferred)

The lane serves the same paths as the OpenAI shape, but its request bodies differ: it
requires `task` and `target`, and takes `seconds` from 4 to 15. Translating the body is
out of this work's scope and is tracked by the `TODO:` in `raiversed-lane.py`. Until then,
an OpenAI-shaped request reaches the lane unchanged and is refused by it.

### Mode names in LiteLLM

The relay publishes LiteLLM modes from the tags: `embedding`, `image_generation`,
`audio_transcription`, `video_generation`. The mode is informational for LiteLLM unless
`enforce_supported_endpoints` is set, and the exact name `video_generation` is not in the
documentation fetched while writing this: verify it on the reference LiteLLM before relying on it.

## Hooks

The installer writes `/etc/systemd/system/<unit>.d/raiversed.conf` for every lane unit:

```ini
[Service]
ExecStartPost=-/usr/bin/python3 <repo>/raiversed-lane.py %n start
ExecStopPost=-/usr/bin/python3 <repo>/raiversed-lane.py %n stop
```

`start` recreates the family's container with the lane's upstream and tags; `stop`
removes it, so the relay withdraws the models after its grace period instead of
advertising an engine that is gone. The leading `-` keeps a reverse-agent failure from
failing the lane. The same drop-ins cover `switch-model.sh`, the cockpit's Load, and the
lane-server job, since they all start and stop the same units.

## Checks after install

- transcription: `curl -s http://127.0.0.1:30036/v1/models`: the id must match the one
  declared in `raiversed-lane.py` (`TRANSCRIBE_MODEL`), or the tags will not apply;
- embedding: rerun `./install-embed.sh` so the launcher carries `--alias`;
- image and video: their served names follow the install's default checkpoint.

## Uninstall

```bash
./install-raiversed.sh --uninstall
```

It removes the containers and the drop-ins and keeps the credentials under
`~/reverse-agent`. Delete a family's directory to force its device approval again.
