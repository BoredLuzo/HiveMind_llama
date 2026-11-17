# Voice setup (optional)

HiveMind runs without any of this. Voice is opt-in in every direction: nothing is
downloaded, started or recorded until you enable it.

| Piece | What it costs | Who fetches it |
|---|---|---|
| Audio codecs (`soundfile`, `numpy`) | ~20 MB wheels | **the installer** (`pyproject.toml` `[voice]` extra via `uv sync`, fallback `requirements.txt`) — also used by Underdog Woof |
| Speech-to-text engine + model | ~21 MB binaries + 74–465 MB model | **you choose**: installer step `[8/9]`, `deploy\setup_voice.bat`, or Download in *Settings → Voice* |
| Text-to-speech (Pocket TTS, default) | PyTorch CPU in `uv`'s cache (~200 MB) + ~200 MB model | **you choose**: installer step `[8/9]` (`--tts`/`--both`), `deploy\setup_voice.bat --tts`, or first request; needs `hf auth login` (gated) |
| Text-to-speech (Underdog Woof, optional) | ~41 MB (`woof_int8.npz` + pure-NumPy runtime) in `voice_models/underdog_woof/` | **you choose**: *Voice model* → `Underdog Woof` + *Download (~41 MB)*, `deploy\setup_voice.bat --woof`, or first synthesis (**no PyTorch, no HF token**, non-commercial) |

Full guide for gated Hugging Face downloads (accepting the gate, token types, `HF_TOKEN`, systemd, 401/403 troubleshooting): [huggingface_token.md](huggingface_token.md).

The installer asks once (`[1]` STT, `[2]` STT + Pocket TTS, `[3]` Pocket TTS, `[4]` Skip); add STT, Pocket TTS, or Underdog Woof any time later via `deploy\setup_voice.bat` or *Settings → Voice* (`stt/`, `voice_models/underdog_woof/`, `logs/pocket_tts.log`, plus `uv`/HF caches; overridable via `HIVEMIND_STT_DIR`, `HIVEMIND_WOOF_DIR`, `HIVEMIND_TTS_URL`).

## Speech to text

English only in this release. The model tiers (all CPU):

| Tier | Size | Measured English WER | When |
|---|---|---|---|
| `parakeet-q4k` (default) | 396 MB | **3.6 %** | best accuracy per byte, >= 8 GB RAM |
| `base` | 141 MB | 9.9 % | light tier, fast CPU fallback |
| `tiny` | 74 MB | 12.4 % | weak CPUs |
| `small` | 465 MB | 7.3 % | slower, accurate |

Run these any time; they are idempotent (existing files are skipped):

```bat
deploy\setup_voice.bat                 REM RAM default (parakeet q4k)
deploy\setup_voice.bat tiny            REM pick a tier
deploy\setup_voice.bat --tts           REM Pocket TTS (pre-warm + model)
deploy\setup_voice.bat --woof          REM Underdog Woof (~41 MB, no HF token)
deploy\setup_voice.bat --both          REM STT + Pocket TTS
python deploy\fetch_whisper.py --check REM what STT models are installed?
python deploy\fetch_woof.py --check    REM is Underdog Woof installed?
```

Linux: `sudo deploy/install_linux.sh --voice` (speech to text), `--tts` (text to
speech) or `--both`; env equivalents `HIVEMIND_VOICE=1` / `HIVEMIND_TTS=1`.
For Underdog Woof on Linux, select **Underdog Woof** under *Settings → Voice*
and click *Download*, or run `.venv/bin/python deploy/fetch_woof.py`.

Then: *Settings → Voice → Speech to text*. A Telegram voice note works once
`voice_in = true` in `gateway.toml` (or the switch in *Settings → Telegram
Gateway*).

## Text to speech

HiveMind supports two opt-in local CPU backends under *Settings → Voice → Text
to speech*:

### Option A: Pocket TTS (default)

The model is gated, so even the installer needs your login first - it can pre-warm
PyTorch and fetch the model, but not accept the terms for you:

1. `hf auth login` (once, with your Hugging Face account) and accept the terms on
   <https://huggingface.co/kyutai/pocket-tts>.
2. *Settings → Voice → Text to speech*: keep *Voice model* on `Pocket TTS`, pick a
   voice (default `anna`; `cosette` is non-commercial — see
   [voice_licenses.md](voice_licenses.md)), speed (`1.08`), and volume (`1.0`).
3. First request starts `uvx --python 3.12 pocket-tts serve` in an isolated cache
   (warm ~1.5 s per sentence; evicted after idle timeout `tts_idle_stop_s`).

### Option B: Underdog Woof (pure NumPy, ~41 MB, no token)

1. In *Settings → Voice → Text to speech*, set *Voice model* to `Underdog Woof`
   and click *Download (~41 MB)* (or run `deploy\setup_voice.bat --woof`).
2. Pick `juno` (default) or `mira`, speed, and volume.
3. No Hugging Face login, PyTorch, or sidecar process needed —
   `ConwayResearch/Underdog-Woof` weights + `LICENSE`/`NOTICE` fetch once into
   `voice_models/underdog_woof/` and unload when idle (`Underdog Non-Commercial
   License 1.0`, see [voice_licenses.md](voice_licenses.md)).

### Playback & preload

- **Manual & Telegram:** speaker button on messages, *Play sample* in *Settings →
  Voice*, and *Answers as voice notes* (`telegram_voice_out`) in Telegram Gateway.
- **Read aloud (UI, all off by default):** optional toggles per surface (`Direct`,
  `Agentic`, `Questions`, `Approvals`) + *Pre-load the engine when a run starts*
  (`tts_preload_run`, default on; *Keep engine warm* for Whisper STT).
