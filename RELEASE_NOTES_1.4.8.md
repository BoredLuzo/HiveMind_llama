# Release 1.4.8

Scope: v1.4.7..HEAD. Themes: the EDIT→TEST→FIX loop densified, voice
over Telegram end to end, a big measured performance round for the
coder model, frontend speed, and a deep hardening/simplification pass.

## The EDIT→TEST→FIX loop, densified
- Per-chunk test feedback ships ON (was opt-in and effectively unused).
- New edit→test mapping (`hive_functions/testmap.py`): the auto test runs
  the test files that cover what was written, not always the full suite
  (pytest/jest name conventions + a bounded python import probe; no match
  falls back to the full suite).
- Auto-verify on the completion veto: the harness runs the test suite
  itself instead of begging the model to do it — green → completion
  accepted as verified, red → concrete failures into context, no tests →
  smoke-test nudge.
- Scoped test runs with a no-match fallback; `python -m pytest` (a bare
  `pytest` exe is not on PATH everywhere — every auto test read as a
  false red on such machines).

## Measured coder performance (+57% prefill, live-measured)
- The hermes 35B coder now runs without draft-mtp and with eager loading
  (`--load-mode none`): prefill 78.5 → 123.3 t/s, decode unchanged.
  Context 100k costs nothing measurable; moe 35 stays optimal on this
  hardware (more CPU experts is faster here — the iGPU expert path is
  weaker than 8 CPU threads).
- `llama_mlock` was a NO-OP since introduction (by-value import bug) —
  the setting now actually reaches the load path.
- `HIVEMIND_MTP_SPEC=0` was bypassed by the registry-MTP block — closed.
- New crash guard: speculative decoding + eager load = Vulkan OOM
  (live-reproduced). Speculative models (draft-mtp, DSpark) keep mmap
  regardless of the mlock setting.

## Frontend
- Streaming performance: four hotspots fixed (whole-chat clone scans at
  4 Hz, per-frame markdown re-parse of the full answer, quadratic think
  block rewrites, per-token perf bar paints).
- Run stats line under every answer (elapsed · tokens · cached · t/s,
  model on hover), persisted with the chat.
- Context-fit warnings on every context input (real VRAM estimate, red
  over budget — warn-only), a models chip in the header, compact
  estimates for agent cards.
- UX quick wins: English-only UI, the auto mode text names what actually
  routes, every run-end reason carries an actionable hint, opt-in
  browser autostart from the interactive launcher.

## Reliability
- Preset autoload no longer silently reverts user settings on every
  engine boot (newer settings win; the preset re-engages on explicit
  load).
- Watchdog handshake: heartbeat marker + stop mode; the manual launcher
  and the headless watchdog no longer fight over the port (winerror
  10048).
- GET /chats disk scan off the event loop (1.77 s stall measured on 480
  chats); the gateway journal fetch sends its cursor (was re-downloading
  the full window twice per 5 s tick).
- Blob store binary-safe (text-mode writes corrupted CRLF payloads since
  2026-10-03; legacy files repaired on read); the rendered html is
  blobified like the other big payloads.
- Monotonic clocks for run deadlines and the slot lifecycle; idle
  eviction kills outside the manager lock and never kills a mid-load
  slot.
- Shared tree-kill for the test runner with a Windows fallback; one
  atomic-write path for every file tool (six hand-rolled copies
  collapsed); a Windows blocklist bypass via the drive-path rewrite
  closed (`rd /s /q /c/users` slipped through).
- run_tests runs in parallel with read-only tools; the whisper warm
  server is actually used for transcription now (was dead since 1.3.11).

## Voice over Telegram (end to end, live-verified)
- Voice note → transcript → decider → run verified live; the from_voice
  flag was dead code (frozen-dataclass assignment) — spoken notes now
  route through the decider.
- /decider on|off|shadow; OFF frees ~1.8 GB RAM immediately; the
  decision cache survives restarts.

## Under the hood
- First-ever end-to-end regression coverage for the 6.6k-line duo core
  (a scripted llama-server drives the real generator).
- Net −300 lines through four adversarial audit + simplification rounds
  (dead branches, dead gates, duplicated blocks consolidated); 180 test
  suites, ruff clean.
- Model housekeeping: MiniCPM-V-4_7 drafter moved to the Q6_K build; the
  retired AD variant and old drafters removed.
