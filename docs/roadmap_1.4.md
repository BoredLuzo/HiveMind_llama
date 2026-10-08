# HiveMind 1.4 candidate backlog

Working notes for the next minor release. Nothing here is committed to
ship - items get promoted into the changelog when they land.

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

## Engineering debt (promotion candidates)

- split core/duo_runner.py (6.9k lines) into phases/ modules
- split static/app.js (12k lines) into ES modules with a tiny build
- minimal CI: run the 93 regression suites on every push
- DONE in 1.3.3: llama.cpp response timings (predicted_per_second)
  now feed the cleaned decode t/s figure in the perf panel
- consolidate duo_coder_model: the /stream body override is ignored as
  the agentic exec model, only agents.duo_coder.model takes effect
- multi-channel gateway (several chats/owners in parallel)
- phone parity: /forget, agent free-text notes, media richness
