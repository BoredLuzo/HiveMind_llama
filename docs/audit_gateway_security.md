# Audit — gateway/Telegram security + agentic run + context flow

Third audit of this session. Scope: the Telegram voice work (`b773f82`, `f7eba0a`)
and a **real agentic run** through the live engine to prove the context plumbing
still works.

---

## 1. Security review of the voice/Telegram changes

### Checked and sound

| Surface | Finding |
|---|---|
| **Who can speak** | Both directions sit behind `gw_auth.classify` → only the paired owner; every outbound call goes through `gw_send.send()`, which refuses any chat that is not the owner's. `send_voice` uses the same boundary as text. |
| **Rate limiting** | The limiter runs **before** the tool work (`main.py:852`), so a voice note is subject to the same 20/min as text. A voice note cannot be used to spin a run loop. |
| **Secrets in audio** | `_finish` filters the answer (`bridge.py:838`) **before** `_maybe_voice_answer`, and `_mirror_fazit` filters at `:1807`. The spoken/voiced text is the filtered text - no token can be read aloud by a run that prints one. |
| **Size / duration caps** | 20 MB (`voice_max_bytes`) and 300 s (`voice_max_seconds`), enforced before download and before transcription; the engine additionally caps audio seconds and rejects non-audio magic. |
| **Downloaded audio** | Kept in memory, transcribed, dropped. The engine's temp WAV is deleted in a `finally`; nothing persists the owner's voice. |
| **New endpoints** | `POST /tts/preload` is loopback-only like the rest of the API, does nothing while `tts_enabled` is false, and runs the start in a worker thread (no event-loop block). |
| **`telegram_result_mode`** | Cannot silence an error: an error run is always text. A failed synthesis falls back to text. |

### Two holes I fixed in this pass

1. **A voice-only channel dropped the safety lines.** `_finish` with
   `telegram_result_mode=voice` sent only the audio - and with it the
   "🔒 N approval(s) auto-denied" note and a non-completed stop reason vanished.
   Those are not progress, they are the only place that information exists.
   Now the notes (and the stop reason when it is not `completed`) are sent as text
   even in voice mode; the mirror path does the same for a non-completed run.
2. **A restart looked like a dead bot** (fixed in `b773f82`, listed here for the
   record): replay protection discards what arrived while the gateway was down; the
   owner now gets one short note asking to resend.

### Open risks (pre-existing, not introduced here)

- **Localhost API has no CSRF/Origin guard.** A malicious web page can POST to
  `http://127.0.0.1:8001/...` with a *simple* request (no preflight) and, e.g.,
  start a run or the TTS sidecar. This is true for the whole API (`/stream`
  included), not just the new endpoints - so it is an architecture decision, not a
  voice bug. Worth a token/origin check in a later release.
- **`/tts/preload` starts torch on request.** Any local process (or such a page)
  can cost ~1.3 GB RAM for 15 minutes. Same class as above.
- **Voice input is prompt injection by design**: a transcript becomes the prompt.
  Owner-only, so it is the same trust level as typing - no elevated risk.

---

## 2. Agentic run end-to-end (real, live engine)

Prompt: *create `greet.py`, run it once with `run_bash`* - `POST /stream` with
`mode=code_duo`, `duo_agentic=true`.

| Observation | Value |
|---|---|
| Result | **`stop_reason: completed`** |
| Duration | 231 s (Qwen3.5-4B-MTP) |
| Events | 578: `planner_thinking_token` 175, `planner_thinking` 36, `planner_plan_token` 25, `tool_gen` 252, `ctx_meter` 27, `heartbeat` 11, `tool_call`/`tool_result` 6 each, `file_change` 2 |
| Tool sequence | `write_file` → `run_bash` → `task_complete` (**twice**, i.e. two rounds) |
| Artifact | `Test2/greet.py` (101 B) written at 23:30:03, `[TASK-COMPLETE] accepted` at 23:30:55 |

## 3. Context flow: intact (log evidence, same run)

| Line | Meaning |
|---|---|
| `[CTX-EFFECTIVE] mode=agentic coder_ctx=70656 planner_target=8192` | context resolution per role/phase works |
| `[CODER-BRIEFING] mode=full think_tokens=185 limit=1500` | **briefing cap works**: 185 estimated think tokens < 1500 → full briefing injected (plan-only would have triggered above 1500) |
| `[BUDGETS] ctx=70656 cpt=3.99 out=8000 -> tool_cap=24000 read_cap=64000 schema=40000` | the budget chain (schema / tool result / read caps) is computed from the real ctx and chars-per-token |
| `ctx_meter` 27 events, no `[CTX-CUT]` | the meter ran, nothing had to be evicted in this run |
| `[PLANNER-PARSE] mode=numbered subtasks=3` | planner output parsed into 3 subtasks |
| `[WRITE-CALIBRATION] … ratio=2.07 chars/token` | write-size calibration still measured |

No compression/eviction fired (the run stayed inside the budget), and the
briefing/plan path produced a briefing rather than a bare plan - i.e. the 1.3.11
staging logic (`coder_briefing_text`, `plan-only` above the limit) is intact.

## 4. Verdict

The Telegram voice path is **structurally sound** (owner-only, rate-limited before
the expensive work, secrets filtered, sizes capped) with the two fixes above. The
agentic pipeline **completes** and the context plumbing is measurably unchanged.
The one thing that remains a design decision, not a defect, is the unauthenticated
loopback API - worth a token in a later release.
