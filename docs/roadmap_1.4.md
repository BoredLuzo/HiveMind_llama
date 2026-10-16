# HiveMind 1.4 candidate backlog

Release targets (owner decision 2026-10-10): **1.4 = voice (TTS/STT)
plus a smart MCP update**. 1.5 candidates follow at the end (ComfyUI
full-AI-stack integration).

Working notes. Nothing here is committed to ship - items get promoted
into the changelog when they land.

## Voice (Clawdbot parity gap)

### TTS: kyutai/pocket-tts (owner request 2026-10-07)
https://huggingface.co/kyutai/pocket-tts

- 100M param TTS, runs on CPU only (2 cores, ~200 ms to first audio
  chunk, ~6x realtime on a laptop CPU). Windows/macOS/Linux fine, no
  GPU needed (optional .to("cuda")).
- Languages: en, de, fr, pt, it, es.
- Voice cloning from any wav (quality of the sample carries over -
  clean the sample first) plus a premade voice catalog. `export-voice`
  CLI caches a voice as a safetensors embedding.
- Runs as `pocket-tts serve` (local HTTP server + web UI), CLI
  `uvx pocket-tts generate`, or the python API
  (TTSModel.load_model / generate_audio).
- License CC-BY-4.0, gated repo: setup must prompt for accepting the
  usage conditions once.

Integration sketch:
1. Optional extra `hivemind[tts]` (pocket-tts + torch cpu) - never a
   hard dependency.
2. Managed sidecar: engine supervises `pocket-tts serve` on a local
   port (same pattern as the searxng sidecar), health + restart.
3. Surfaces:
   - chat: speaker button on assistant messages (on demand, no autoplay)
   - tool `say` for the agent (own narration, opt-in)
   - telegram: voice notes (ogg/opus) as outbound messages - gateway
     sends audio, gated behind a per-chat toggle (VRAM is precious,
     pocket-tts is CPU so it can run alongside llama slots)
4. Settings: enabled, voice (catalog entry or exported embedding),
   speed, per-surface toggles (chat/telegram).

### STT counterpart (needs picking)
Pocket-TTS is output only. For voice INPUT (telegram voice notes in,
push-to-talk in the UI) evaluate whisper.cpp (local, quantized) vs
faster-whisper. Same sidecar pattern.

## Agent feedback quality (from the nBody live runs, 2026-10-08)

- identical-edit cut (3x identical -> abort) should instead inject the
  CURRENT file head into the tool result ("edit from this") - the model
  stood one re-read away from fixing itself and got thrown out
- run_tests output condensation: one line per failure (test name,
  assertion, file) instead of raw `head`-capped vitest reporter walls -
  16 failures in 100 lines meant the model saw fragments
- re-read dedup: read_file on an unchanged file answers "(unchanged
  since your last read)" instead of the full content again (main.ts was
  re-read 12x in one run)
- project_map tool: one compact listing of all workspace files plus
  exported signatures, so the coder sees structure without reading
  files one by one (get_signatures was used once in 120 tool rounds)


## MCP modernization (1.4)

Current state: hand-rolled JSON-RPC over plain HTTP
(`infra/mcp_server.py`, ~1.5k lines, tools only, tier guards for
read/write/exec/query, loopback bind + optional bearer token) - solid,
but frozen at protocolVersion 2024-11-05 and no resources/prompts.

Update, smallest useful steps first:

1. Protocol negotiation: advertise and negotiate 2025-03-26 / 2025-06-18
   alongside 2024-11-05 (old clients keep working).
2. Streamable HTTP transport: single /mcp endpoint (POST JSON-RPC +
   optional SSE downstream, session header). The plain-HTTP mode stays
   as a compatibility fallback.
3. Resources: expose read-only state as MCP resources - loaded slots
   and VRAM, latest run journal, telemetry summary, workspace tree.
4. Prompts: run-report and code-review prompt templates.
5. Evaluate the official python `mcp` SDK for the transport layer only
   if hand-rolling streamable HTTP turns out bigger than the shim - the
   tier guards and loopback binding stay hand-rolled either way.

## Engineering debt (promotion candidates)

- split core/duo_runner.py (6.9k lines) into phases/ modules
- split static/app.js (12k lines) into ES modules with a tiny build
- minimal CI: run the 93 regression suites on every push
- DONE in 1.3.3: llama.cpp response timings (predicted_per_second)
  now feed the cleaned decode t/s figure in the perf panel
- DONE in 1.3.5: /stream model overrides patch both the agent card
  and the flat key (they used to lose to the card silently)
- multi-channel gateway (several chats/owners in parallel)
- phone parity: /forget, agent free-text notes, media richness

## Looking past 1.4: 1.5 candidates (owner direction 2026-10-10)

### ComfyUI integration - local full-AI stack

Goal: one surface where the local LLM agents can also generate images
(and later video) via a local ComfyUI instance - no cloud.

Sketch:

1. Connector: ComfyUI HTTP API on 127.0.0.1:8188 - POST /prompt with a
   workflow JSON, GET /history/{id}, WebSocket /ws for progress. No new
   server process if ComfyUI already runs; otherwise document a sidecar
   start (same pattern as searxng).
2. Agent tool `generate_image(prompt, workflow, output)`: tier-guarded
   like browser; workflow JSON files live in the repo as templates with
   prompt/seed/steps plugs (sd15/sdxl/flux starters).
3. VRAM arbitration is the interesting part: ComfyUI workflows compete
   with the llama slots for the same 8 GB. Extend the SERIAL-SLOTS idea
   - ComfyUI counts as a slot, gets evicted before a big llama load,
   and workflows declare an expected VRAM class (low/med/high).
4. Surfaces: chat (image appears inline), telegram (photo message),
   tool output (path + dimensions). Settings: enabled, host/port,
   default workflow, auto-evict behavior.

## 1.4 execution plan (2026-10-17)

Release target from the backlog above: **voice (TTS + STT) plus the MCP
modernization**. Sequencing is driver-first: the audio paths are only worth
building once the sidecar lifecycle they share is proven, so Phase 1 delivers a
reusable sidecar supervisor and TTS rides on it.

| Phase | Deliverable | Estimate | Depends on |
|-------|-------------|----------|------------|
| 0 | Decisions frozen (STT engine, audio format, voice source, packaging) | 0.5 d | - |
| 1 | Sidecar supervisor + TTS engine + chat speaker + `say` tool | 4-6 d | 0 |
| 2 | Telegram voice OUT (ogg/opus voice notes, per-chat toggle) | 2 d | 1 |
| 3 | STT engine + Telegram voice IN + UI push-to-talk | 3-5 d | 1 |
| 4 | MCP modernization (per-client tool policy, auth, audit log) | 2-4 d | - |
| 5 | Hardening, docs, installer/packaging, release | 2-3 d | 1-4 |

Total ~2-3 focused weeks for one person. Phases 2 and 4 are independent and can
run in any order after 1.

### Phase 0 - decisions (recommendations)

- **STT engine: whisper.cpp**, with the model file as the tier - the same release
  ships `whisper-cli`/`whisper-server` *and* `parakeet-cli`, so "support weak
  CPUs" is a model choice, not a second install. Measured on the owner's machine
  (`C:\Users\NtheP\Desktop\STTEngineTest\RESULTS.md`), 8 threads:
  - **real human English, 73 LibriSpeech utterances / 481 s, micro-WER**:
    parakeet v3 **q4_k 3.57 %** (RTFx 18.7, 552 MB), q8_0 3.65 % (10.6, 794 MB),
    whisper small 7.30 % (4.1, 582 MB), base 9.91 % (14.1, 317 MB),
    base.en 9.74 %, tiny 12.43 % (23.6, 205 MB), tiny.en 10.43 % (27.1).
    Parakeet halves whisper-small's error rate while being 4.5x faster;
    quantizing it q4_k costs nothing measurable.
  - **default: Parakeet v3 q4_k** (English is the owner's main case);
    other languages: whisper `base`/`small` by setting (language-routed, like the
    production reference). Weak-CPU floor: whisper `tiny`/`tiny.en`, though
    Parakeet q4_k is faster than base here - prefer it unless RAM is the limit.
  - English-only v2 is the alternative if German accuracy proves weak in
    practice; German real-world accuracy is still unmeasured (needs a human
    sample, not TTS).
- **Decode stays in Python**: both CLIs (miniaudio) fail on real Ogg/Opus, which
  is exactly what Telegram sends; `soundfile` (2 MB wheel, no ffmpeg) decodes it
  in ~15 ms with an identical transcript. Verified: whisper.cpp reads **only
  WAV** without ffmpeg, and `whisper-server --convert` exits when ffmpeg is
  missing.
- **Language must be explicit.** Without a `language` field the whisper server
  transcribed German audio into English (it even reported the correct detected
  language while translating); `-l auto` in the CLI is correct, and the server
  needs `--language auto` or a per-request field.

- **Audio: ogg/opus** as the wire format (Telegram voice notes require it),
  16 kHz mono for STT, 24 kHz for TTS output. Encode with ffmpeg when present,
  else `soundfile`/`opuslib`; the installer must say clearly when neither is
  available instead of failing at runtime.
- **Voice source: premade catalog first**, cloned embedding as a second step
  (voice cloning raises a consent/licensing question that the README should
  answer before it ships).
- **Packaging: strictly optional.** `[tts]`/`[stt]` extras plus a sidecar. An
  install without the extras must behave exactly like 1.3.9 - enforced by the
  existing regression suite plus one new "extras absent" test.

### Phase 1 - sidecar supervisor + TTS

1. `core/sidecar.py`: generic supervised child process (spawn, port probe,
   health endpoint, restart with backoff, RAM budget, shutdown with the engine,
   no orphan on crash). Property-tested without real processes.
2. `deploy/fetch_tts.py` + docs: pocket-tts (gated HF repo, CC-BY-4.0, accept
   the terms once); `uvx pocket-tts serve` documented as the manual path.
3. Engine API: `POST /tts` (text, voice, speed -> audio bytes), `GET /tts/voices`,
   caching per `(text, voice)`, hard caps on text length and response size.
4. Surfaces: speaker button on assistant messages (on demand, never autoplay),
   `say` tool (tier 2 so it drops out at small windows, budgeted like any other
   tool so it cannot flood the context).
5. Settings + UI: enabled, port, voice, speed, per-surface toggles; CHANGELOG,
   `docs/settings.md`, README section.

Acceptance: engine without the extra unchanged (suite green); with the extra,
`/tts` returns audio for a 200-char sentence in under 2 s on CPU after the first
call; killing the sidecar is recovered within the restart budget; no VRAM used.

### Phase 2 - Telegram voice OUT

`sendVoice` (ogg/opus), per-chat toggle, size cap handling, fallback to a text
message when encoding fails. Acceptance: a run started from the phone answers
with a voice note when the toggle is on, and with text when it is off.

### Phase 3 - STT

1. Sidecar + `POST /stt` (audio -> text, language setting, duration cap),
   transcript redaction in logs by default.
2. Telegram voice IN: `getFile`, size check (Telegram caps bot downloads at
   20 MB), transcode, transcribe, inject as a user message marked as
   transcribed audio (untrusted input - same treatment as typed text).
3. UI push-to-talk (MediaRecorder -> `/stt`), transcript prefilled into the
   input box instead of sending immediately.

Acceptance: 30 s voice note -> transcript in under ~15 s on CPU; wrong-language
input does not abort the run; no transcript in `logs/` unless debug dumping is on.

### Phase 4 - MCP modernization

Per-client tool exposure policy (which tools an IDE client may see/call),
token auth for the MCP HTTP server, audit log of MCP tool calls, and a
confinement check that reuses the workspace guards. Acceptance: a client without
a token gets 401; a client with a read-only policy cannot reach a write tool;
every MCP call appears in the audit log.

### Phase 5 - hardening and release

Suite additions per phase (sidecar, TTS/STT API contracts, tool wiring, UI
guards, extras-absent), docs (README voice section, gateway voice notes,
settings), installer messages for ffmpeg/opus, fresh-folder smoke with and
without the extras, release notes with the honest limitations (CPU speed of STT,
voice quality of a 100M TTS).

### Risks

| Risk | Mitigation |
|------|------------|
| Sidecar lifecycle (the real risk, not the models) | Phase 1 builds it once, property tests, restart budget, orphan checks |
| Audio dependencies (ffmpeg/opus) differ per OS | detect + clear installer message; pure-Python fallback where possible |
| Gated HF repo / licenses (CC-BY-4.0) | Phase 0 documents the one-time acceptance and attribution |
| Telegram limits (20 MB down, 50 MB up, codec) | size checks before download, transcode, text fallback |
| Scope creep (voice + MCP + ComfyUI in one release) | ComfyUI stays a 1.5 candidate; 1.4 ships voice + MCP only |

### 1.3.10 candidates (if a patch line is wanted before 1.4)

- steering latency: drain steers between tool calls, not only at round
  boundaries (the UI promises "next tool-call boundary")
- `subagent_lite`: tool timeout, message-list cap, and its first tests
- direct/tool-agent path: `max_post_attempts` is 1, so a single malformed
  response aborts the loop
- `update.bat` / `install.bat`: verify the downloaded release zip against a
  published hash (currently HTTPS + GitHub trust only)
- drop the three unreachable editors (`edit_ast`, `patch_file`, `replace_lines`)

