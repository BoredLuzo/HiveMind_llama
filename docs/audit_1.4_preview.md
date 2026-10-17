# Audit — 1.4 preview (everything after the 1.3.11 tag)

Scope: `git diff v1.3.10..HEAD` (the 1.3.11 release) **plus** the uncommitted 1.4
preview in the working tree. Audited for: correctness, hardcoded values,
duplication, file placement, structural fit, and whether anything else broke.

Numbers in this report were measured on this machine (32 GB RAM, CPU only).

---

## 1. Changelog state (before this audit)

| Block | Covers | Status |
|---|---|---|
| `[1.3.11]` | web search fixes, coder briefing cap, speech to text | matches the tag (`966e72f`) |
| `[1.3.10]` | workspace regression, first disabled voice piece | ok |
| **1.4 preview** | TTS, read aloud, Telegram voice in/out, installer voice step, STT fd fix, UI fixes | **missing entirely** |

The 1.3.11 block is accurate but its "Notes" claim *"has no chat
push-to-talk"* is still true and should stay true until V2 lands.

Fixed here: a `## [1.4] - unreleased` section was added (see `CHANGELOG.md`).

---

## 2. Defects found and fixed

### 2.1 The event loop froze during speech (severity: high)

`/tts` called `tts_engine.synthesize()` directly inside the async handler. The
first request starts the sidecar, so the whole server — SSE, `/tts/status`
polling, every other tab — stalled for **20-30 s**. `/stt` had the same shape
(audio decode + CLI run) for 1-5 s per call.

Fix: `await asyncio.to_thread(...)` for synthesis, Opus encoding, audio decode and
transcription (the pattern `server.py` already used elsewhere).

Verified live: with a synthesis running, `/tts/status` answered in **18 ms**.

### 2.2 The restart budget counted successful starts (severity: medium)

`core/tts_engine.py` compared `_state["starts"] >= 3` — `starts` was incremented
on *every* start, including healthy ones. After three good starts (three idle
shutdowns) the fourth start was refused with *"failed to start three times"*,
i.e. the feature broke itself the longer it was used.

Fix: `failures` counter, incremented only on a failed `/health` wait and reset on
a healthy start.

### 2.3 `__import__("os")` (severity: style, but it reads like test scaffolding)

Two places in `core/tts_engine.py`. Replaced with a normal top-level `import os`.

### 2.4 Leaked log handle per start (severity: low)

`open(logs/pocket_tts.log, "a")` was passed to `Popen` and never kept. Every
restart leaked one handle on Windows. Now stored in `_state["log"]` and closed in
`stop()`.

### 2.5 The voice list existed three times (severity: medium, maintainability)

`core/tts_engine.VOICES` (7 entries), the same seven `<option>` tags in
`index.html`, and a prose copy in `docs/voice_setup.md`. Adding a voice meant
editing Python **and** HTML, and the two could silently disagree.

Fix: `GET /tts/voices` is the single source; the select is filled by
`_ttsFillVoices()` and carries `max_chars` with it. The HTML now holds one
`(loading...)` placeholder. Test pins it (`test_tts_server.py`).

### 2.6 The 600-character cap existed three times (severity: low)

`core/tts_audio.MAX_CHARS`, two literals in `static/app.js`. Now the server sends
`max_chars` (`/tts/status` and `/tts/voices`), JS uses it and only falls back to
600 when the field is missing.

### 2.7 Installer step numbers disagreed (severity: low, visible)

Steps 1-5 said `[x/7]`, steps 6-8 said `[x/8]` after the voice step was inserted.
All eight now say `/8` and the header block lists the voice step.

### 2.8 `tts_timeout_s` could raise a bare `ValueError` (severity: low)

`float(settings.get(...))` on a string value. Wrapped like the STT caps.

### 2.9 STT returned `STT_EMPTY` for every request (fixed before this audit)

`core/stt_audio.to_wav16k` created its temp file with `tempfile.mkstemp(...)[1]`
and never closed the returned fd. On Windows the file stayed open for writing, the
parakeet/whisper child could not read it ("failed to read audio data"), stdout was
empty and the endpoint reported "no speech". `soundfile` reads the same file
happily, which is why the unit test was blind to it.

Fixed with `os.close(fd)` plus a regression test: the decoded WAV must be readable
*and* deletable **by a second process** (both fail while an fd is leaked).

---

## 3. Flagged, deliberately not changed

| # | Item | Why it stays | Decision needed |
|---|---|---|---|
| 3.1 | `1.08` as a literal in `settings.py`, `tts_engine.DEFAULT_SPEED`, `index.html`, `app.js`, docs | `settings.py` is the runtime truth, the others are fallbacks/defaults for a form field | optional: one `TTS_DEFAULT_SPEED` constant served via `/tts/status` |
| 3.2 | `1500` for the briefing cap in `duo_runner` + `settings.py` | same pattern as every other default | none |
| 3.3 | `docs/plan_1.4_voice.md` **is tracked and ships** in the release zip (`git archive HEAD`) | it is English, contains no secrets - but it is an internal design doc with owner decisions | keep public, or add it to the `.gitignore` group that already holds the other internal docs |
| 3.4 | Model tier names in `deploy/setup_voice.bat` | `fetch_whisper.py --model <key>` validates against `core/stt_engine.MODELS`, so a typo fails loudly instead of quietly | none |
| 3.5 | `voice_max_bytes` 20 MB in `config.py` + as a default in `telegram_api.download_file` | the caller passes the config value; the parameter default is a guard for direct calls | none |
| 3.6 | Read-aloud cap 600 chars | a 600-char answer is ~35 s of audio and ~19 s of generation (see 4) | optional: lower the read-aloud cap to ~350 chars, or add a "max read length" setting |

---

## 4. Measured behaviour (warm sidecar, cosette, 1.08x)

| Text | Audio | Time | Ratio |
|---|---|---|---|
| 27 chars | 1.9 s | 1.5 s | 1.25x realtime |
| 191 chars | 10.2 s | 5.8 s | 1.77x realtime |
| 319 chars | 19.3 s | 10.4 s | 1.85x realtime |

Cold start (first request after idle or reboot): **20-30 s** once, then warm.
The sidecar stops itself after `tts_idle_stop_s` (default 900 s) and is restarted
by the next request. Telegram voice-out therefore needs the sidecar warm to feel
instant; the first answer after a pause carries that one-time cost.

---

## 5. Structure check (does it fit HiveMind?)

- **Placement**: `core/tts_engine.py` + `core/tts_audio.py` mirror
  `core/stt_engine.py` + `core/stt_audio.py` (same names, same split: process
  lifecycle vs. audio maths), same `logger = logging.getLogger(__name__)`, same
  error-code style (`SttError`/`TtsError` with `.as_dict()`).
- **Endpoints**: `/tts`, `/tts/status`, `/tts/voices` sit directly next to the
  `/stt*` block in `server.py` and copy its guards (409 when off, 400 on engine
  errors, same `JSONResponse` shape).
- **Gateway**: voice in/out use the existing `ParsedUpdate` → `start_owner_run`
  path and the central `send()` boundary; no new chat_id code path, no way to
  reach a chat that is not the owner's.
- **Dependencies**: no new package in HiveMind's venv. TTS runs in `uvx`'s own
  environment (PyTorch never enters the install), audio codecs were already in
  `requirements.txt`, the models are gitignored and fetched on demand.
- **Settings**: every switch is a documented key in `settings.py` +
  `docs/settings.md`, default off, UI-first.
- **Tests**: five new suites (`test_tts_audio`, `test_tts_engine`,
  `test_tts_server`, `test_gateway_voice`, `test_gateway_voice_out`) plus the
  extended `test_voice_card`; all registered in `tests/run_regressions.py`
  (142 suites, 142 pass).

---

## 6. Regression evidence

- Suite: **142/142 pass** after every fix in this audit.
- Live engine restart + smoke: `/tts/voices` (7 voices, `max_chars` 600),
  `/tts/status` (all fields), synthesis 200 with Ogg/Opus for Telegram,
  `/tts/status` answered in 18 ms while a synthesis ran.
- STT live: WAV and Ogg/Opus (16/24/48 kHz) transcribe a 13.6 s clip correctly.
- `test_bat_encoding` (17 checks) keeps every `.bat` ASCII + CRLF, `bash -n` on
  `deploy/install_linux.sh` is clean.

## 7. Open items (not defects)

1. Voice-out in Telegram needs the **gateway process restarted** to pick up the
   new bridge code (documented, not automatable from here).
2. `plan_1.4_voice.md` shipping decision (3.3).
3. Optional: read-aloud cap (3.6), speed constant (3.1).
