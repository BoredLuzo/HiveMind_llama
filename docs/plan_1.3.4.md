# 1.3.4 Plan (draft, owner-reviewed)

Scope decision from the 1.3.3 audit: the release hygiene is now enforced
(runtime cache untracked, safety regex, no-PII scan step). 1.3.4 is the
maintainability + parity release: split the two monoliths, add CI, and
close the clawdbot parity gaps that matter for daily phone use.

## M1 - Maintainability (the big one)

1. Split `core/duo_runner.py` (6.9k lines) into `core/duo/`:
   - `phases.py` (planner / coder / critic phase drivers)
   - `tool_loop.py` (coder tool rounds + steer drains)
   - `chunking.py` glue (build_chunk_context usage stays in hive_functions)
   - `events.py` (duo_start, duo_round, ctx meter, usage_meta emission)
   - `fazit.py` (end paths, stop reason texts)
   Mechanical, test-driven moves only. The 93 suites are the net; no
   behavior changes allowed in this milestone. Commit per extraction.
2. Split `static/app.js` (12.1k lines) into ES modules, no build step:
   `sse.js` (feed/journal tail), `perf.js` (meter + tool metrics),
   `askcards.js`, `approvals.js`, `gateway.js` (commands/toggles),
   `chats.js`, `settings.js`, `render.js` (markdown/fences), and a thin
   `main.js` bootstrap. `<script type="module">`, cache buster stays.
   Hard rule: zero behavior drift, the regression pins must stay green.
3. CI: GitHub Actions on windows-latest (the suites are Windows-shaped:
   encodings, paths, process handling). Jobs: ruff, node --check,
   run_regressions.py. Runs on every push to the gateway branch.
4. Docs cleanup: move audit series + WP working notes to `docs/archive/`,
   refresh `architecture.md` (module map after the splits).

## M2 - Clawdbot parity (phone daily-driver features)

1. `/forget <key>` on the phone (wraps the existing memory forget).
2. `/note <text>`: phone-side quick memory write, mirrors the remember
   tool; raise or make configurable the 2000 char memory injection cap.
3. Voice IN: Telegram voice notes transcribed to text (local STT, start
   with faster-whisper small, Windows CPU) and injected as user text.
   TTS out stays out of scope for 1.3.4.
4. Documents IN: PDF/TXT/MD sent to the bot land in the chat workspace
   (`.hive_uploads`) and are offered to the run as context.
5. Media OUT: agent-produced images/screenshots (browser tool) come back
   to the phone as photos instead of text-only.
6. Channel abstraction prep: gateway config gains a `channels` table
   (telegram first), owner whitelist per channel. No second channel
   implementation yet - this is the 1.3.5 door.

## M3 - Audit leftovers (small, from the 1.3.3 window)

1. Journal selection with two parallel runs: UI attach should pin a
   run_id instead of "most recently active" (WP8 leftover).
2. web_fetch: resolve DNS once and pin the IP for redirect hops
   (rebinding leftover from the audit plan).
3. Coder ctx: engine-side sanity clamp for the duo path too (planner
   has one, coder relies on the G13 intake cap only).
4. release_scan.py: turn the PII/runtime-pattern scan into a deploy
   script that runs inside package_release.bat (ship gate).

## Release discipline (carried over)

- No em dashes in phone texts and release notes.
- Ship scan (runtime artifacts + PII patterns) gates every release zip.
- Release title format: "HiveMind <version>", English notes.
- Behavior-free refactors land as their own commits, one extraction
  per commit, suites green between commits.

## Open questions for the owner

- ES modules without a build step vs. a tiny esbuild bundler step in
  package_release.bat (bundle keeps the cache buster story simple).
- Voice: acceptable to ship STT with a fixed model (faster-whisper
  small, ~500 MB) or should the model be optional download on first use?
- Priority between M1 and M2 if both do not fit into one release.
