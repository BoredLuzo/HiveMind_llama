# External integration smoke matrix

`tests/live_integrations.py` is an **opt-in** smoke runner for the integrations
the regression gate cannot cover: they need real credentials, a running engine,
model files or an external service. It is **not** part of
`tests/run_regressions.py`. Its offline contract (all checks skip without
configuration, no network, secrets redacted) is pinned by
`tests/test_live_integrations_offline.py`.

```text
python tests/live_integrations.py --list             # offline: checks + "configured"/"not configured"
python tests/live_integrations.py                    # run every check (unconfigured ones SKIP)
python tests/live_integrations.py telegram searxng   # run selected checks
python tests/live_integrations.py --strict           # a SKIP counts as failure (release sign-off)
```

Exit codes: `0` no FAIL (skips allowed), `1` a check failed (or skipped under
`--strict`), `2` unknown check name.

Rules every check follows:

- Missing env/fixture/service → `[SKIP] <name> <reason>`; the check makes **no**
  network call before its env is complete.
- Engine-backed checks first probe `GET /health`; an engine that is down is a
  SKIP (not a FAIL). An engine with the feature switched off
  (`STT_DISABLED`/`TTS_DISABLED`) is a SKIP too.
- Output never contains the bot token: every message passes through
  `redact()` (configured token + any `digits:token`-shaped string).
- Checks that cost GPU time or message the owner need an extra `=1` opt-in.

## Shared settings

| Env var | Meaning |
|---|---|
| `HIVEMIND_LIVE_BASE` | Engine URL, e.g. `http://127.0.0.1:8001` (`python run.py` running) |
| `HIVEMIND_TG_TOKEN` | Bot token — the same variable the gateway reads. Never commit it. |
| `HIVEMIND_TG_OWNER_ID` | Numeric Telegram user id of the owner (smoke-only; the gateway itself learns the owner by pairing). The owner must have pressed `/start` on the bot once. |

## Matrix

| Check | Required env | Optional env | What it does | Expected PASS |
|---|---|---|---|---|
| `telegram` | `HIVEMIND_TG_TOKEN`, `HIVEMIND_TG_OWNER_ID` | `HIVEMIND_LIVE_TG_SEND=1` | `getMe`; with the flag also `sendMessage` to the owner | `token valid (bot @name)`; with the flag the owner receives "HiveMind integration check: Telegram OK" |
| `stt` | `HIVEMIND_LIVE_BASE`, `HIVEMIND_LIVE_STT_AUDIO` (path to a short speech clip, ogg/wav/mp3) | `HIVEMIND_LIVE_STT_EXPECT` (a word the transcript must contain) | Raw-body `POST /stt` — the exact gateway voice-in path | `<n>s audio -> <m> chars (<lang>)`; needs `stt_enabled=true` and the whisper model (`deploy/fetch_whisper.py`) |
| `tts` | `HIVEMIND_LIVE_BASE`, `HIVEMIND_LIVE_TTS=1` | `HIVEMIND_LIVE_TTS_OUT` (keep the `.ogg`) | `POST /tts {"format":"opus"}` — the gateway voice-out path | `<n> bytes Ogg/Opus`; body starts with `OggS`, ≥1 KB; needs `tts_enabled=true` + voice deps (`requirements-voice.txt`) |
| `vision` | `HIVEMIND_LIVE_BASE`, `HIVEMIND_LIVE_VISION=1` | `HIVEMIND_LIVE_VISION_MODEL` (`direct_model` tag) | Generates a 1280×720 PNG with a **random** 4-char code (font from `tests/smoke_image.py`), one `mode=chat` `/stream` run | `model read the image code`; loads a vision model + projector on the GPU. Deeper diagnosis: `tests/smoke_image.py`, `tests/direct_vision_check.py` |
| `searxng` | `HIVEMIND_LIVE_SEARXNG_URL` (e.g. `http://localhost:8888`) | — | `GET /search?format=json` | `<n> results`; a 403 means `json` is missing from `search.formats` (see `searxng-config/settings.yml.example`) |
| `mcp` | `HIVEMIND_LIVE_BASE`, `HIVEMIND_LIVE_MCP=1` | `HIVEMIND_LIVE_MCP_TOOL` (tool that must be listed) | `GET /mcp/servers` (redacted view) | `<n> server(s), <r> running, <t> tools`; SKIP when no server is enabled in `mcp_servers`; FAIL on a dead server. The client connects lazily — run one chat first if it reports 0 tools |
| `send_file` | `HIVEMIND_TG_TOKEN`, `HIVEMIND_TG_OWNER_ID`, `HIVEMIND_LIVE_SEND_FILE=1` | — | In a temp workspace: the `core.send_files` guard must refuse `.env`, then `read_sendable` + `sendDocument` of a small `.txt` to the owner | `guard refused .env; integration_check.txt (<n> bytes) delivered`; the owner receives the file |

## Example (PowerShell, one session)

```powershell
$env:HIVEMIND_LIVE_BASE = "http://127.0.0.1:8001"
$env:HIVEMIND_LIVE_SEARXNG_URL = "http://localhost:8888"
$env:HIVEMIND_LIVE_MCP = "1"
$env:HIVEMIND_LIVE_TTS = "1"
$env:HIVEMIND_LIVE_STT_AUDIO = "C:\fixtures\hello.ogg"
$env:HIVEMIND_LIVE_STT_EXPECT = "hello"
# token from the credential store / a password manager - not from a file in the repo
$env:HIVEMIND_TG_OWNER_ID = "123456789"
python tests/live_integrations.py --list
python tests/live_integrations.py
```

Tip: `tts` with `HIVEMIND_LIVE_TTS_OUT=...\tts.ogg` produces a clip that can be
fed back as `HIVEMIND_LIVE_STT_AUDIO` for an STT round trip.

## Status

Status is tracked in the local maintenance notes (outside the repository).
Until a check has a recorded PASS with real credentials/fixtures, its
integration counts as **not tested**.
