# HiveMind 1.4.0

Voice, end to end: speak answers here, dictate runs from the phone, and hear the
answer back. Plus a hardened tool layer and a Linux install that no longer copies
your settings.

Everything below is opt-in. Nothing is downloaded or started until you switch it
on, and no model or binary enters the release zip.

## Voice

**Text to speech** - the Voice card gets a master switch, seven English voices
(default `anna`; `cosette` is non-commercial), a speed control and a "Play sample" button. Every answer
carries a speaker button (hidden while TTS is off, never autoplays). The engine is
Pocket TTS in its own `uvx` environment: PyTorch never enters HiveMind's venv, the
sidecar starts on the first request and stops itself after 15 idle minutes.

**Speed without chipmunk** - Pocket TTS has no speed parameter, so `1.08x` is a
WSOLA time-stretch inside HiveMind: the audio gets shorter, the pitch stays
exactly where it was (verified: 4 s -> 3.71 s, F0 unchanged, syllable peaks at
`t/speed` within ~14 ms).

**Read aloud** - four switches: direct mode reads the whole answer, agentic runs
read the plan first and then the coder's answer, and ask-user questions and the
defensive-gate/approval cards can be spoken too. One item at a time, Stop kills
it, long text is cut at a sentence boundary.

**Telegram voice** - a voice note from your phone is transcribed locally and runs
exactly like typed text (same owner gate, same approvals); answers can come back
as a voice note (`sendVoice`, Ogg/Opus). Both switches sit in the Telegram card
and are greyed out until speech in/out is enabled.

**Speech to text** - whisper.cpp/Parakeet on the CPU. Tiers: `parakeet-q4k`
(396 MB, 3.6 % micro-WER), `base` (141 MB, 9.9 %), `small` (465 MB, 7.3 %), `tiny`
(74 MB, 12.4 %). English only. A red mic button left of send appends the
transcript to the message box (push-to-talk stays a 1.5 topic).

**Installer** - one prompt with four choices (speech to text / both / text to
speech / skip). Skipping never means reinstalling: `deploy\setup_voice.bat` or the
Voice card adds it later. `[O]` at the installer's first prompt jumps straight to
the optional parts.

## Tool layer

- **Ungated write tools closed**: `edit_ast`, `replace_lines`, `patch_file` write
  files and were missing from the approval list - the model could rewrite a file
  without a card. Now gated, with the whole approval policy written next to the
  list.
- **The dispatcher refuses what the run does not offer**: legacy/unknown tool
  names, and machine-changing tools outside the current mode (a chat run calling
  `write_file`).
- **Malformed tool arguments are an error again** in the agentic loop - broken
  JSON used to become "no arguments" and the tool ran on defaults.
- **`search_code` runs without a shell**: its backends were shell strings built
  from the model's pattern and path. A read-only tool that never asks for approval
  must not hand `cmd` a string.
- **`start_background` joins the destructive gate** like `run_bash`.
- A new consistency suite links the four tool lists (schemas, handlers, approval,
  phone allowlist) so a forgotten registration fails the tests instead of a run.

## Linux

- **Per-tool limits**: Windows job objects have no POSIX twin, so the only cap was
  the service-wide `MemoryMax` - a runaway `run_python` could take the server down
  with it. `RLIMIT_AS` / `RLIMIT_NPROC` / `RLIMIT_CPU` now apply per child (same
  settings keys, `0` = off).
- **The installer no longer copies user state** (`settings.json`, `sessions/`,
  `logs/`) from a checkout into `/opt/hivemind`.
- **Speech on Linux works out of the box**: the unit carries `HOME`,
  `UV_CACHE_DIR`, `HF_HOME`; the installer pre-warms the TTS cache as the service
  user; the gated model is documented as `Environment=HF_TOKEN=...`.

## Also fixed

- **A broken Python environment is diagnosed, not guessed.** A fresh install on
  Python 3.14 hit `cannot import name 'ArgsKwargs' from
  'pydantic_core._pydantic_core'`; the old repair reinstalled the pair, pip said OK
  and the import still failed, leaving "delete .venv" as the only advice. The env
  check now prints the interpreter, the pydantic/core versions and the compiled
  module's size, reinstalls **wheels only** and then names the real causes in
  order (antivirus quarantine, mismatched pair, fresh venv, fresh folder).
- **Every `/stt` request returned "no speech"**: the temp WAV kept a leaked file
  descriptor open, so the transcript engine could not read it. Found on the first
  real request, fixed with a regression test (the decoded file must be readable
  *and* deletable by a second process).
- **Speech blocked the whole UI**: the first TTS request started the sidecar on
  the event loop (20-30 s freeze). Synthesis, Opus encoding, audio decode and
  transcription run in a worker thread now.
- **The TTS restart budget counted successful starts**, so after three idle
  shutdowns the fourth start refused to run.
- The mic button rendered as a small white box (stale stylesheet cache-buster) and
  the speech-to-text block stayed visible after switching it off.
- Installer step numbers, the exit code after a full install, and a stray `)`
  inside a config block.

## Numbers

- TTS warm: 1.25-1.85x realtime (320 characters -> 19 s of audio in 10 s); first
  request after idle pays a 20-30 s sidecar start.
- Speech to text: 13.6 s clip in ~1.5 s with `parakeet-q4k` on a CPU.
- Models: ~620 MB for speech to text, ~200 MB + PyTorch for text to speech - all
  outside the zip.

Full detail: `CHANGELOG.md`. Setup: `docs/voice_setup.md`. Licences and their
obligations: `docs/voice_licenses.md`.
