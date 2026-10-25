# Release 1.4.8

Scope: v1.4.7..v1.4.8. Themes: the EDIT→TEST→FIX loop densified, voice
over Telegram end to end, a measured coder performance round, frontend
speed, and a hardening/simplification pass.

## The EDIT→TEST→FIX loop, densified
- Per-chunk test feedback ships ON (was opt-in and effectively unused).
- Edit→test mapping: the auto test runs the test files that cover what
  was written, not always the full suite (no match falls back to the
  full suite).
- Auto-verify on the completion veto: the harness runs the test suite
  itself — green → completion accepted as verified, red → concrete
  failures into context, no tests → smoke-test nudge.
- run_tests runs in parallel with read-only tools; `python -m pytest`
  replaces the bare `pytest` call (not on PATH everywhere — every auto
  test read as a false red on such machines).

## Agentic quality fixes
- Plan tracking only advances a step on successful, path-matching work —
  previously the plan advanced every round regardless, manufacturing
  false replans and stalls.
- The post-explore briefing teaches the real edit_file contract
  (exact old_text/new_text) instead of a marker format the handler
  rejects; stale references to the removed patch_file tool are gone.
- Verification honesty: run_bash counts as verification only with the
  server's genuine `[exit code: 0]` marker — keyword echoing no longer
  fools the gate.
- The model's current focus files survive context compression
  (no more "old_text not found" loops caused by masking the file being
  edited); evicted reads no longer answer "UNCHANGED" on a later read.
- Wedge handoff on by default and now also covers run_bash: a repeated
  failing edit or command is handed to a fresh-context fix agent instead
  of burning rounds (max 2 handoffs per run).

## Measured coder performance (+57% prefill, live-measured)
- The hermes 35B coder runs without draft-mtp and with eager loading
  (`--load-mode none`): prefill 78.5 → 123.3 t/s, decode unchanged.
  Context 100k costs nothing measurable; moe 35 stays optimal.
- `llama_mlock` now actually reaches the load path (was a no-op since
  introduction); `HIVEMIND_MTP_SPEC=0` is honored by every code path.
- New guard: speculative decoding + eager load = Vulkan OOM
  (live-reproduced) — speculative models always keep mmap.

## Frontend
- Streaming performance: four hotspots fixed (whole-chat clone scans at
  4 Hz, per-frame markdown re-parse of the full answer, quadratic think
  block rewrites, per-token perf bar paints).
- Run stats line under every answer (elapsed · tokens · cached · t/s,
  model on hover), persisted with the chat.
- Context-fit warnings on every context input (real VRAM estimate, red
  over budget — warn-only), a models chip in the header, compact
  estimates for agent cards.
- UX quick wins: English-only UI, honest mode descriptions (the auto
  text names what actually routes; Code-Duo no longer promises a critic
  it does not run), every run-end reason carries an actionable hint,
  opt-in browser autostart from the interactive launcher.

## Reliability
- Preset autoload no longer silently reverts user settings on every
  engine boot (newer settings win).
- Watchdog handshake: heartbeat marker + stop mode; the manual launcher
  and the headless watchdog no longer fight over the port.
- GET /chats disk scan off the event loop; the gateway journal fetch
  sends its cursor (was re-downloading the full window twice per 5 s).
- Blob store binary-safe (text-mode writes corrupted CRLF payloads;
  legacy files repaired on read); rendered html is blobified like the
  other big payloads.
- Monotonic clocks for run deadlines and the slot lifecycle; idle
  eviction runs outside the manager lock and never kills a mid-load
  slot; a Windows blocklist bypass via the drive-path rewrite closed.
- The whisper warm server is actually used for transcription now (was
  dead since 1.3.11).

## Voice over Telegram (end to end, live-verified)
- Voice note → transcript → decider → run verified live; the from_voice
  flag was dead code — spoken notes now route through the decider.
- /decider on|off|shadow; OFF frees ~1.8 GB RAM immediately; the
  decision cache survives restarts.

## Under the hood
- First-ever end-to-end regression coverage for the duo core (a
  scripted llama-server drives the real generator).
- Net −300 lines through adversarial audit + simplification rounds;
  181 test suites, ruff clean.
