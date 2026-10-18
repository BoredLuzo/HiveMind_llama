# 1.4 voice build plan — STT (whisper.cpp) + TTS (Pocket TTS)

Status: **signed off 2026-10-17** (owner answers in §0a). V0 (STT core) is
implemented: `deploy/fetch_whisper.py`, `core/stt_engine.py`, `core/stt_audio.py`,
`POST /stt` + `GET /stt/status`, three suites. Everything else is still plan.
Dev-only doc (export-ignored, never in a release zip).

## 0a. Owner decisions (2026-10-17) — FINAL

| # | Decision | Answer |
|---|---|---|
| 1 | STT tiers | **English only** in 1.4. Parakeet v3 q4_k is the default on a >= 8 GB machine, whisper base the light fallback below that. No German/multi-language promise in this release. |
| 2 | Process model | Spawn-per-request, **plus a preload toggle** (`stt_preload`): a warm `whisper-server` for whisper models, a no-op for parakeet (it loads in < 1 s and has no server binary). |
| 3 | UI push-to-talk | Browser-side WAV encoding, no new dependency. |
| 4 | TTS runtime | `uvx pocket-tts serve` (isolated environment, PyTorch never enters HiveMind's venv). `tts-venv/` stays the documented fallback. |
| 5 | Voices | **English only**; female voices first (several to pick from in-app), one serious male voice as the alternative; no cloning. |
| 6 | Order | STT first (V0-V3), TTS (V4-V5) in the same release if it lands in time. |

Consequences of "English only": the German TTS model and the 24-layer variants
drop out of 1.4, the voice catalog is one language, and the per-voice licenses
stay simple (Kyutai's English premade voices). The STT side never has to deal
with the whisper server's translate-on-auto behaviour either.

## 0d. Router idea (TinyDecide) - measured 2026-10-17

Owner idea: a mini classification model that reads the transcript and picks
mode/model/context. Candidate: [`TheREZOR/TinyDecide`](https://huggingface.co/TheREZOR/TinyDecide)
(ELECTRA-small, 10.4M params, 6.2 MB 4-bit, Apache-2.0, zero-shot with
plain-English questions at call time, Python engine needs **numpy only**).

Measured here with the shipped python engine (3 questions per call, realistic
HiveMind prompts):

| aspect | result |
|---|---|
| latency | **~325 ms** for one question, ~375 ms for three (numpy engine; the model card's 1.1 s is an ESP32 number) |
| mode routing | **7/12** plausible on our prompts, and the confidence is ~0.00-0.53 |
| failure mode | long technical imperatives ("fix the failing test in app.js" -> *search*, conf 0.00; "run the test suite" -> *chat*) |
| fits the model? | no: it is trained for short *everyday* messages (app routing, inbox triage, span extraction). Its own "Limits" section says two-step reasoning is near chance. |

Conclusion: **not a drop-in router**. Plan for the router:
1. **Rules first** (0 ms, deterministic, explainable): imperative coding verbs,
   file paths / extensions, "site:" or URL -> research, pure question -> chat.
2. **TinyDecide only in the ambiguous band** (rules unsure, short message,
   `auto_route_tiny` on): one `choice` question, ~300 ms, and only a
   *suggestion* - a confidence threshold plus the visible `[ROUTE]` line and the
   usual fallback to the preset.
3. If it is used seriously, **calibrate on our own runs** (chat history, chosen
   presets in `token_stats.json`) - the model is zero-shot, so this is prompt/
   threshold tuning first, fine-tuning only if that is not enough.
4. Keep it opt-in (`router_enabled`, default off) and fetch the 6.2 MB files on
   demand (`deploy/fetch_router.py`), same pattern as STT/TTS.

Also worth using it for what it *is* good at: `noul`/`span` questions on short
voice notes (e.g. "does this ask to change files?" showed 0.25-0.29 on coding
prompts, i.e. weak; but "extract the time/path" style spans are its strength).

## 0c. TTS voice decision (2026-10-17)

* **Voice: `cosette`** (owner, after listening to alba/anna/cosette/eve/jane/vera
  and bill_boerst). Female, English, the reference render is
  `tts_samples/cosette.wav`.
* **Pocket TTS has NO speed/tempo parameter** - verified in the CLI flags and in
  `TTSModel.generate_audio` (only `max_tokens` / `frames_after_eos`). "Speak a bit
  faster" is therefore a HiveMind post-process, not a model setting.
* **Speed = WSOLA time-stretch** (waveform-similarity overlap-add, numpy only, no
  new dependency): duration changes, pitch does not. Measured on cosette:
  1.08x -> 5.53 s, 1.12x -> 5.33 s, 1.18x -> 5.06 s, all at F0 94.9 Hz, while a
  naive resample at 1.12x shifts the pitch to 105.7 Hz (chipmunk) and is only
  kept as a negative reference.
* Setting `tts_speed` (default 1.12, range 0.8-1.5), applied after generation and
  before the ogg/opus encode, with the same WSOLA code the samples were made
  with. The speed factor belongs in the TTS card next to the voice.
* Note for Telegram: the voice-note path re-encodes to Ogg/Opus anyway, so the
  stretch costs no extra format step.

## 0b. Progress (2026-10-17)

* **V0 done** - `deploy/fetch_whisper.py`, `core/stt_engine.py`, `core/stt_audio.py`,
  `POST /stt` + `GET /stt/status`, three suites. Verified end to end with the real
  binaries and models (parakeet q4_k 2.14 s for a 17.6 s German clip).
* **V1 done** - the **Voice card** in the settings (opt-in, English, status line,
  a download button with progress through `POST /stt/fetch`, and a 3 s microphone
  test that posts WAV and never starts a run). `tests/test_voice_card.py`, 57
  checks.
* **Remaining**: V2 push-to-talk in the chat, V3 Telegram voice-in, V4/V5 TTS
  (Pocket TTS through `uvx`, female-first voice shortlist, speaker button).
* Telegram audio and spoken answers stay **separate opt-in switches** in the
  Gateway card, per chat, off by default.

## 0. Answers to the open questions first

**Does Parakeet run on AMD?** Yes — on the CPU. Every number in the STT spike was
measured on the owner's AMD Ryzen 7 5700X with the official whisper.cpp
`whisper-bin-blas-x64` build; the model runs in the ggml CPU backend, so the GPU
vendor does not matter. What *is* missing is AMD **GPU offload**: the official
release assets ship CUDA and one Qualcomm-Adreno OpenCL build, and no Vulkan
asset — Vulkan-on-Windows is an open upstream request
([issue #3673](https://github.com/ggml-org/whisper.cpp/issues/3673), AMD support
PR [#3691](https://github.com/ggml-org/whisper.cpp/pull/3691)). A self-built
`-DGGML_VULKAN=ON` build would use the AMD card (the same trick HiveMind already
uses for llama.cpp), but it needs a build toolchain and is **not** needed: the
measured CPU cost is 1.4-1.9 s for a 15-20 s voice note with Parakeet q4_k, and a
Telegram voice note has no interactive deadline. Plan: CPU now, Vulkan only as an
*optional* later fetch if upstream ever ships assets.

**How many TTS voices?** One or two, and that is not a real limitation: Pocket
TTS voices are small prompt/embedding files, not models, and the engine reports
its own catalog. The axis that costs something is the **language**, because each
language is a separate model config (and non-English has a bigger 24-layer
variant). Plan: **English voice by default, German as the second language**, and
inside a language the voice name is a free dropdown fed from the engine — so
"more voices later" is a settings change, not code. No voice cloning in 1.4
(consent/licensing, and it is not needed).

**Dependency size (owner's hard constraint).** The only thing HiveMind itself
gains is `soundfile` (~2 MB, wheel with libsndfile). STT is a ~8-21 MB native zip
plus model files, no Python deps. TTS runs in an **isolated `uvx` environment**,
so PyTorch never enters HiveMind's venv — the reason `uvx` exists.

## 1. Goals, non-goals, constraints

Goals
- Voice in: Telegram voice notes and a UI push-to-talk button become normal user
  messages.
- Voice out: the assistant can answer with audio in the chat and as a Telegram
  voice note.
- Everything local, CPU-only, no cloud, no GPU requirement.
- Verified by the existing automation: every new behaviour gets a suite.

Non-goals (1.4)
- Real-time streaming conversation (barge-in, live duplex).
- Voice cloning, voice conversion, or a voice library UI.
- Wake word / always-on listening.
- Audio for every assistant message automatically (audio is always on demand).
- GPU offload for STT/TTS.

Constraints
- **Dependency budget** (owner): no torch in the core environment, no ffmpeg
  unless proven necessary, no Docker for voice, no new system service.
- Both features must be **optional extras**: an install without the STT binaries
  or without `uv`/pocket-tts behaves exactly like 1.3.9 and every existing suite
  stays green.
- English is the primary voice language; German (owner's second language) must
  work too.
- No new always-on process: the STT path must not hold RAM or a port while idle.

## 2. Dependency budget (the whole point)

| Addition | Size | Scope | Why it earns its place |
|---|---|---|---|
| `soundfile` + bundled libsndfile | ~2 MB wheel | always | Ogg/**Opus** decode (Telegram voice in) and encode (voice out). Measured: decode 15 ms for 17.6 s, encode 551 KB -> 52 KB in 163 ms, transcript from the decoded file byte-identical. No other pure-Python Opus exists. |
| whisper.cpp release zip (`whisper-bin-blas-x64` / `-x64`, per OS) | 21 MB (BLAS) / 8.5 MB (CPU) | STT only, fetched on demand | `whisper-cli` + `parakeet-cli` + `whisper-server` in one runtime, CPU-only, no Python deps, same asset-fetch pattern as llama.cpp. |
| STT models (`ggml-parakeet-…-q4_k` 396 MB, `ggml-base` 141 MB, `ggml-tiny` 74 MB, `ggml-small` 465 MB) | user picks | STT only | One file per tier; only the selected one is downloaded. |
| Pocket TTS via `uvx pocket-tts` | **0 bytes in HiveMind's venv** (isolated uv cache, ~200 MB CPU wheels + model on first use) | TTS only, opt-in | First-chunk ~200 ms, ~6x realtime on a laptop-class CPU, 2 cores, 100M params, 7 languages incl. English + German. `uv` is already part of the HiveMind install, so the *mechanism* is already present. |
| `numpy` | already a dependency | always | resampling to 16 kHz mono. |

Rejected, with reasons
- **ffmpeg** (~80 MB): only job would be Opus and webm handling; `soundfile`
  covers Opus, and the browser covers webm (see 3.3). Revisit only if a format
  appears that libsndfile cannot read.
- **faster-whisper / CTranslate2**: torch + a second Python dependency tree for
  no accuracy gain over whisper.cpp (measured).
- **Whisper large-v3**: 8.5x slower than Parakeet at equal-or-worse noisy-speech
  WER (published head-to-head) and 4+ GB of RAM.
- **System TTS (SAPI/Windows voices)**: no emotion control, no cross-platform
  parity, German quality varies per installed voice pack.
- **sherpa-onnx / PocketTTS.cpp / ONNX routes**: genuinely leaner runtimes for
  Pocket TTS, but there is no official Windows binary release to fetch
  (`PocketTTS.cpp` is a source drop). Recorded as the fallback if `uvx` proves
  too heavy in practice.

## 3. STT design

### 3.1 Engine and model tiers (measured)

| Tier | Model | Real English WER | RTFx | RAM | When |
|---|---|---|---|---|---|
| default | `parakeet-tdt-0.6b-v3-q4_k` (396 MB) | **3.57 %** | 18.7 | 552 MB | English, best accuracy/speed balance |
| light | `ggml-base` (141 MB) | 9.91 % | 14.1 | 317 MB | non-English, small installs |
| light+ | `ggml-small` (465 MB) | 7.30 % | 4.1 | 582 MB | non-English when RAM allows |
| floor | `ggml-tiny` / `tiny.en` (74 MB) | 12.43 % / 10.43 % | 23.6 | 205 MB | weak CPUs, "must work everywhere" |

Settings: `stt_model` (default derived from RAM: >=8 GB -> parakeet q4_k, else
base), `stt_language` (`auto` default; `-l auto` verified to keep the spoken
language, the whisper *server* default translated to English and must never be
used), `stt_threads` (default `min(4, cpu_count//2)`), `stt_enabled` (default
off), `stt_log_transcripts` (default off).

### 3.2 Process model: one CLI call per request (no daemon)

Measured: model load + transcription for a 17.6 s clip is 1.5-1.9 s total with
Parakeet q4_k and 1.4 s with whisper base; the load part is a few hundred ms. A
warm server would save that and cost a permanently resident process, a port, a
lifecycle and 200-600 MB RAM. For a voice note that arrives every few minutes,
**spawn-per-request wins**: zero idle footprint, no supervisor, no port, nothing
to leak. `whisper-server` stays available for a future "always warm" setting, but
is not part of 1.4's required path.

Consequences: a hard per-request timeout (default 120 s, `stt_timeout_s`), a
process kill on timeout, and at most one concurrent transcription per process
(a simple asyncio semaphore; queue depth 1 with a clear "busy" status instead of
a growing queue).

### 3.3 Audio in, two sources, one format

1. **Telegram voice note** (Ogg/**Opus**, <= 20 MB download limit): download via
   `getFile`, cap size and duration (default max 5 min), `soundfile` decode ->
   16 kHz mono 16-bit WAV -> engine -> text.
2. **UI push-to-talk**: `MediaRecorder` gives webm/Opus in Chromium - libsndfile
   cannot read webm. Do **not** add a decoder: decode the recorded blob in the
   browser with `decodeAudioData`, resample to 16 kHz mono and write a WAV
   header in plain JS (~40 lines, no library), then POST that WAV. Firefox
   produces Ogg/Opus and can use the same JS path or the server decode.
   Fallback if the JS path fails in a browser: record and upload as-is, decode
   server-side only if libsndfile accepts it, otherwise a clear error.

Transcript becomes a normal user message, prefixed with a marker
(`[voice note, transcribed, de]`), subject to the existing input budgets; it is
**untrusted input** exactly like typed text (no implicit tool approval, no
prompt-injection privilege). Temp WAV files live in `tempfile.gettempdir()` and
are deleted in a `finally`.

### 3.4 Files

| File | Content |
|---|---|
| `core/stt_engine.py` | engine abstraction: model tier table, binary resolution, `transcribe(wav_path, language, threads, timeout)` with `subprocess.run(list, shell=False)`, timeout + kill, output parsing (parakeet = stdout line, whisper = `-otxt` per-file file), error taxonomy |
| `core/stt_audio.py` | Opus/any -> 16 kHz mono WAV via `soundfile` + numpy; duration/size caps; temp-file lifecycle |
| `deploy/fetch_whisper.py` | downloads the whisper.cpp release asset for the platform (same machinery as the llama.cpp fetcher) + selected models into `stt/`; `--list`, `--model`, `--verify` (sha256 of the asset, size check) |
| `server.py` | `POST /stt` (multipart or raw WAV/Opus + optional language), `GET /stt/status` (engine present, models, defaults) |
| `core/chat_run.py` | inject the transcript as the user message when the request carries audio |
| `infra/telegram*.py` | voice/audio message handler -> download -> `/stt` path -> run |
| `static/app.js` + `index.html` | mic button (push-to-talk, hold to record), transcript preview before send, settings rows |
| `settings.py` | the keys from 3.1 |

### 3.5 Limits, failures, security

- Caps: audio <= 20 MB, duration <= `stt_max_seconds` (default 300), transcript
  <= the existing input budget; over-long input is truncated with a visible note.
- Failures have their own codes: `STT_DISABLED`, `STT_NO_ENGINE` (with the fetch
  command), `STT_NO_MODEL`, `STT_DECODE_FAILED`, `STT_TIMEOUT`, `STT_BUSY`,
  `STT_EMPTY` (no speech detected). Each maps to one short user-facing sentence -
  no silent `except`.
- Security: no shell, argv only; no user-controlled path reaches the engine (temp
  file names are generated); no transcript in `logs/` unless
  `stt_log_transcripts=True`; transcripts are never written to the workspace;
  Telegram attachment handling keeps the existing owner/approval gates untouched.

### 3.6 Tests (new suites)

`test_stt_engine.py` (tier defaults, argv shape, timeout/kill, error taxonomy,
no shell), `test_stt_audio.py` (opus round-trip on a generated file, caps,
temp cleanup), `test_stt_server.py` (endpoint contract, disabled/missing-engine
paths, busy/timeout), `test_stt_telegram.py` (size/duration caps, untrusted
marking, per-chat toggle), `test_stt_ui.py` (mic button wiring, no autoplay, JS
WAV encoder unit-tested via a small node harness or by asserting the pure
functions through a JS snapshot).

## 4. TTS design (Pocket TTS)

### 4.1 Runtime

`uvx --index https://download.pytorch.org/whl/cpu pocket-tts serve` as a
**managed child process** of the engine (`core/tts_engine.py`):
- started lazily on the first synthesis request, not at boot;
- port probed, health-checked, restart budget (2 tries, backoff), killed with the
  engine (`atexit` + shutdown hook) so no orphan survives a crash;
- **idle shutdown** after `tts_idle_stop_s` (default 900 s) to give the RAM back;
- `uv` missing or the HF gate unaccepted -> one clear message with the exact
  command, TTS stays off, nothing else breaks.

Alternative if `uvx` misbehaves in practice: `pocket-tts` inside a dedicated
`tts-venv/` next to `.venv` (still outside HiveMind's environment), same
supervisor code.

### 4.2 Voices and languages

- Language for 1.4: **English only** (scope decision). One model config, one
  voice catalog; the German config and the 24-layer variants stay a setting for
  later.
- Voices (owner: "sexy female voices, otherwise one serious male voice"): ship a
  **female-first shortlist** - `alba` (the reference voice), `anna`, `cosette`,
  `eve`, `jane`, `vera` - plus one serious male alternative (`bill_boerst` or
  `jean`). The dropdown is fed from the engine catalog, so adding a voice later
  is a settings change, not code; `tts_voice` is the only new voice setting.
  **The pick must be made by ear**: V4 generates one sample per candidate and the
  owner chooses - nothing is picked from a name alone.
- Voice embedding export (`export-voice`) is done once per voice and cached as
  safetensors next to the model, so switching voices is a fast load, not a
  re-clone.
- **No cloned voices** ship. Cloning stays a documented, explicit, user-supplied
  opt-in with the consent warning from Kyutai's model card; it is not a 1.4
  feature.
- Licenses: model CC-BY-4.0 (attribution goes in the README + settings docs),
  per-voice licenses are documented upstream and listed in our docs. The HF model
  repo is gated -> the README gets a one-time "accept the terms / `hf auth login`"
  step. A third-party ungated mirror exists and will **not** be used.

### 4.3 Audio out

- Engine returns WAV (24 kHz); HiveMind converts to **Ogg/Opus** with
  `soundfile` for Telegram `sendVoice` (the format Telegram requires) and serves
  WAV for the browser (26 KB/s -> 3 KB/s for the Telegram path, measured 11x).
- Text caps: `tts_max_chars` (default 600 per request), sentence-aware chunking
  for longer answers, `tts_timeout_s` (default 120).
- Surfaces: speaker button on assistant messages (on demand, never autoplay),
  Telegram voice-out per chat toggle (`telegram_voice_out`, default off),
  optional `say` tool at priority tier 2 (drops out of small windows like other
  tools; text budget applies so it cannot flood the context).

### 4.4 Files

`core/tts_engine.py` (sidecar lifecycle + HTTP client), `core/tts_audio.py`
(WAV -> Opus, chunking), `deploy/fetch_tts.py` or documented `uv sync` for the
isolated env, `server.py` (`POST /tts`, `GET /tts/voices`, `GET /tts/status`),
`infra/telegram*.py` (sendVoice), `static/app.js` + `index.html` (speaker button,
settings), `tools/handlers/` + `tools/definitions.py` for the optional `say`
tool, `settings.py` keys.

### 4.5 Limits, failures, security

- TTS text is assistant output, never user-controlled in a way that could inject
  arguments (argv is fixed; text goes over HTTP JSON).
- The sidecar binds 127.0.0.1 only, no CORS, no auth needed for a loopback-only
  port but the port is randomised if taken and stored in the process, not in
  settings.
- Failure codes `TTS_DISABLED`, `TTS_NO_UV`, `TTS_NO_MODEL`, `TTS_GATED` (terms
  not accepted), `TTS_TIMEOUT`, `TTS_BUSY`, `TTS_EMPTY`.
- RAM: the sidecar holds ~200-400 MB while warm; it counts against the host-RAM
  budget in the status panel and shuts down when idle.

### 4.6 Tests (new suites)

`test_tts_engine.py` (lifecycle with a fake child process: start, health, restart
budget, idle shutdown, no orphan), `test_tts_audio.py` (WAV->Opus correctness,
chunking, caps), `test_tts_server.py` (endpoint contract + failure codes),
`test_tts_ui.py` (speaker button, no autoplay, voices list from a fake catalog),
`test_voice_optional.py` (with STT/TTS unavailable the engine starts, runs and
behaves exactly as 1.3.9 - the regression promise).

## 5. Settings and UI surface (all new keys)

| Key | Default | UI |
|---|---|---|
| `stt_enabled` | false | Voice card toggle |
| `stt_model` | auto (>=8 GB RAM: parakeet q4_k) | dropdown + "Download" button |
| `stt_language` | auto | dropdown (auto/en/de/...) |
| `stt_threads` | min(4, cores//2) | numeric |
| `stt_max_seconds` | 300 | numeric |
| `stt_timeout_s` | 120 | numeric (advanced) |
| `stt_log_transcripts` | false | toggle (advanced, off) |
| `tts_enabled` | false | Voice card toggle |
| `tts_language` | english | dropdown (english/german) |
| `tts_voice` | alba / juergen | dropdown from engine catalog |
| `tts_max_chars` | 600 | numeric |
| `tts_idle_stop_s` | 900 | numeric (advanced) |
| `tts_voice_out_chat` | false | toggle |
| `telegram_voice_in` | false | per chat, gateway settings |
| `telegram_voice_out` | false | per chat, gateway settings |

UI goes in first for anything user-facing (owner rule): one "Voice" card in
Settings with two blocks (Speech-to-text, Text-to-speech), status line per block
(engine present? model? RAM?), download buttons that run the fetch scripts and
show progress, and honest "CPU only" notes.

## 6. Phasing (each step is one commit + its suites)

| Phase | Deliverable | Est. | Acceptance |
|---|---|---|---|
| V0 | decision record: this doc signed off; `fetch_whisper.py` + model download + `stt_engine` + `/stt` (no UI, no Telegram) | 1-1.5 d | a WAV posted to `/stt` returns text; measured WER on the LibriSpeech set reproduced; engine absent -> `STT_NO_ENGINE`, suite green |
| V1 | Settings UI (voice card, downloads, status) | 1 d | every key in section 5 is reachable in the UI; settings round-trip; no dead rows |
| V2 | UI push-to-talk (browser WAV path) | 1-1.5 d | hold-to-record -> transcript appears in the input box, not sent automatically; works with STT off (clear message) |
| V3 | Telegram voice in | 1 d | voice note -> transcript -> normal run; per-chat toggle; size/duration caps; untrusted marking |
| V4 | TTS engine + `/tts` + `/tts/voices` (behind `tts_enabled`) | 1.5-2 d | first chunk < 1 s warm on this machine; idle shutdown returns the RAM; missing uv/gate -> clear message |
| V5 | TTS surfaces: chat speaker button, Telegram voice out, optional `say` tool | 1.5-2 d | audio plays on demand only; Telegram voice note arrives; `say` disappears at small windows |
| V6 | Docs, packaging, installer hints, README, release notes, "extras absent" regression | 1 d | suite green with and without the extras; zip contains no models; docs/settings generated |

Total ~8-10 focused days. V0-V3 (STT) and V4-V5 (TTS) are independent after V0;
both can be delivered separately, and the release can ship STT alone if TTS
slips.

## 7. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Pocket TTS sidecar lifecycle (the only real new process) | orphan/port/hang | lazy start, health probe, restart budget, idle shutdown, kill on exit, fake-child tests |
| `uvx` cold start (first run downloads torch CPU wheels) | first TTS request takes minutes | explicit progress status + pre-warm button in Settings; documented size (~200 MB) |
| HF gate on `kyutai/pocket-tts` | TTS dead on a fresh machine | README + UI message with the exact `hf auth login` step; error code `TTS_GATED` |
| Opus encode/decode on exotic platforms (libsndfile build) | voice path dead | `soundfile` wheel bundles libsndfile; a start-up self-test (encode+decode a 0.5 s tone) reports a clear failure instead of failing later |
| Browser webm/Opus not decodable server-side | push-to-talk fails in Chrome | browser-side WAV encoding (3.3); server path only ever sees WAV from the UI |
| Small-model users get worse transcripts | trust | default model from RAM, and the UI shows the active model + a "better accuracy" hint |
| Scope creep (streaming, cloning, wake word) | release slips | explicitly non-goals in section 1 |

## 8. Decisions needed for sign-off

1. **STT default model**: parakeet v3 q4_k for English (measured 3.57 % WER,
   552 MB) with whisper base as the non-English/light fallback - or whisper-first
   for simplicity? *Recommendation: as measured; auto by RAM.*
2. **Spawn-per-request vs warm whisper-server** for STT. *Recommendation:
   spawn-per-request; no idle process.*
3. **UI push-to-talk**: browser-side WAV encoding (no new dependency) or add a
   webm decoder? *Recommendation: browser-side.*
4. **TTS runtime**: isolated `uvx pocket-tts serve` (0 bytes in our venv) vs a
   dedicated `tts-venv/` we manage. *Recommendation: `uvx` first, `tts-venv` as
   the documented fallback.*
5. **TTS languages/voices for 1.4**: English + German, one curated default voice
   each, dropdown fed from the engine catalog, no cloning. *Recommendation: yes.*
6. **Ship STT and TTS together, or STT first?** *Recommendation: STT first
   (V0-V3), TTS behind the same release if it lands in time; both are extras, so
   either can be absent without breaking anything.*
