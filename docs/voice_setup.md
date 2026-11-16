# Voice setup (optional, 1.4)

HiveMind runs without any of this. Voice is opt-in in every direction: nothing is
downloaded, started or recorded until you enable it.

| Piece | What it costs | Who fetches it |
|---|---|---|
| Audio codecs (`soundfile`, `numpy`) | ~20 MB wheels | **the installer** (`requirements.txt`), silently - also used by TTS for Ogg/Opus and Underdog Woof |
| Speech-to-text engine + model | ~21 MB binaries + 74-465 MB model | **you choose**: the installer asks once, `deploy\setup_voice.bat` later, or the Download button in *Settings → Voice* |
| Text-to-speech (Pocket TTS, default) | PyTorch CPU in `uv`'s own cache (~200 MB) + ~200 MB model | **you choose**: the installer can prepare it (opt-in), `deploy\setup_voice.bat --tts`, or first request from the UI; needs a one-time `hf auth login` because the model is gated |
| Text-to-speech (Underdog Woof, optional) | ~41 MB (`woof_int8.npz` + pure-NumPy runtime) in `voice_models/underdog_woof/` | **you choose**: switch *Backend* to `Underdog Woof` in *Settings → Voice* and click *Download*, run `deploy\setup_voice.bat --woof`, or let it download on first synthesis; **no PyTorch, no Hugging Face token**, non-commercial license |

Full guide for gated Hugging Face downloads (accepting the gate, token types, `HF_TOKEN`, systemd, 401/403 troubleshooting): [huggingface_token.md](huggingface_token.md).

The installer asks once, with four choices: speech to text only, both, text to
speech only, or skip. **Skipping never means reinstalling**: `deploy\setup_voice.bat`
(or the buttons in *Settings → Voice*) adds either part later, it only touches
`stt/`, uv's cache and the Hugging Face cache - no HiveMind file is replaced.

## Speech to text

English only in this release. The model tiers (all CPU):

| Tier | Size | Measured English WER | When |
|---|---|---|---|
| `parakeet-q4k` (default) | 396 MB | **3.6 %** | best accuracy per byte, >= 8 GB RAM |
| `base` | 141 MB | 9.9 % | light default, multilingual |
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
2. *Settings → Voice → Text to speech*, keep *Backend* on `Pocket TTS`, pick a
   voice (default `anna`; `cosette` is a non-commercial recording, see
   [voice_licenses.md](voice_licenses.md)) and a speed (default `1.08`).
3. The first request starts `uvx pocket-tts serve` in an isolated environment
   (PyTorch never enters HiveMind's venv) and takes a few seconds; afterwards it
   is warm (~1.5 s for a sentence) and stops itself after 15 minutes idle.

### Option B: Underdog Woof (pure NumPy, ~41 MB, no token)

1. In *Settings → Voice → Text to speech*, set *Backend* to `Underdog Woof` and
   click *Download* (or run `deploy\setup_voice.bat --woof`).
2. Pick `juno` (default) or `mira` and your preferred speed.
3. No Hugging Face account, no PyTorch download, and no sidecar process are
   needed — `ConwayResearch/Underdog-Woof` (~41 MB int8 weights + `LICENSE` /
   `NOTICE`) is fetched once into `voice_models/underdog_woof/` and unloaded
   from RAM automatically after the idle timeout. Underdog Woof is licensed
   under the **Underdog Non-Commercial License 1.0** (non-commercial use only;
   see [voice_licenses.md](voice_licenses.md)).

Answers are never autoplayed: use the speaker button on a message, the sample
button in the card, or enable *Answers as voice notes* in the Telegram card.
