# Hugging Face login token - why, where, how

HiveMind downloads every model itself; nothing is bundled. One of them is
**gated**: the Pocket TTS model `kyutai/pocket-tts` (the text-to-speech voice
engine). Gated means: you must accept its terms on the model page **and** prove
who you are with a Hugging Face access token. That is a one-time setup.

Whisper and Parakeet (speech to text) are **not** gated - they download without a
token. The token only matters for text to speech.

> A token is a **secret**. It is never part of `settings.json`, never written into
> a preset, and never sent anywhere except `huggingface.co`.

---

## 1. Accept the terms (once, in the browser)

1. Log in / create an account: <https://huggingface.co/join>
2. Open the model page: <https://huggingface.co/kyutai/pocket-tts>
3. Accept the gate (the form names the prohibited uses - no voice impersonation or
   cloning without consent, no deceptive use).

Without this step the download returns **401/403 even with a valid token**.

## 2. Create the token

<https://huggingface.co/settings/tokens> -> **New token**

- **Read** access is enough (HiveMind only downloads, it never uploads).
- If you use a *fine-grained* token, give it access to `kyutai/pocket-tts` (and
  `kyutai/tts-voices` if you want to switch voices later).
- Copy it immediately - it is shown once. It looks like `hf_xxxxxxxx...`.

## 3. Tell HiveMind the token

Pick **one** of these; both are read by the hub libraries.

### a) `hf auth login` (persistent, no environment variable)

```bat
uvx --from huggingface_hub hf auth login
```

Paste the token, press Enter. It is stored in
`%USERPROFILE%\.cache\huggingface\token` (Linux: `~/.cache/huggingface/token`)
and every later download picks it up - including the TTS sidecar that HiveMind
starts in the background.

Verify:

```bat
uvx --from huggingface_hub hf auth whoami
```

### b) Environment variable `HF_TOKEN`

Windows, current window only (then start HiveMind from that window):

```bat
set HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxx
start_hivemind.bat
```

Windows, persistent for your user (new windows pick it up; the engine must be
restarted):

```bat
setx HF_TOKEN hf_xxxxxxxxxxxxxxxxxxxxx
```

Linux / systemd - edit `/etc/systemd/system/hivemind.service`:

```ini
Environment=HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxx
# optional, keeps the cache inside the install:
Environment=HF_HOME=/opt/hivemind/.cache/huggingface
```

then `sudo systemctl daemon-reload && sudo systemctl restart hivemind`.

**Windows service / autostart note:** if HiveMind runs as a *different* user
(Task Scheduler as SYSTEM, another account), it does not see `setx` of your
account and does not see your `~/.cache` token file. Use a machine-wide variable
(`setx /M HF_TOKEN ...` from an admin shell) or run HiveMind as the same user.

## 4. Check that it worked

- `GET /tts/status` now reports `"hf_token": true` (the value itself is never
  returned).
- Settings -> Voice -> **Play sample**, or

  ```bat
  curl -X POST http://127.0.0.1:8001/tts/preload
  ```

  `{"warm": true}` means the sidecar started. If it says
  `{"warm": false, "reason": "TTS_DISABLED"}`, text to speech is simply off.

## 5. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `401 Unauthorized` / `403 Forbidden` while fetching the model | Terms not accepted on the model page (step 1), or the token has no access to that repo (fine-grained token). |
| `Invalid credentials` | Token copied incompletely or revoked - create a new one. |
| The setup worked in your terminal, the background engine still fails | The engine runs under another user or as a service: use `setx /M` or run it as yourself (see the note above). |
| Download works but the voice never speaks | Not a token problem - check `tts_enabled`, then `/tts/status` (`running`, `voice`). |
| Everything fails behind a company proxy | Set `HTTPS_PROXY` for the engine's environment; the token stays the same. |
| `403 ... gated` right after accepting | Wait a minute (the gate propagates), then retry; re-run `hf auth whoami`. |

## Where the model lands

Default cache: `%USERPROFILE%\.cache\huggingface\hub` (Linux:
`~/.cache/huggingface/hub`). If you point `HF_HOME` somewhere else, use the same
value for the interactive setup **and** the engine/unit - otherwise the second
download happens twice.

## Nothing to do for speech to text

Whisper models (`ggml-*.bin`) and the Parakeet GGUF are public repos - no account,
no token. If the STT download fails, it is the network or disk space.
