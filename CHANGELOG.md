# Changelog
## [1.4.6] - 2026-10-21

### Routing decider (no GPU judge needed)

- One decision engine (`decide_from_signals`) for both the sync and the async
  path - the precedence logic used to exist twice.
- The encoder now classifies **concrete actions** (`asks_a_question`,
  `write_new_file`, `edit_existing_code`, `debug_a_problem`, ...) instead of
  abstract `direct|agentic`: 14/14 on the probe set vs 9-10/14, ~630 ms per
  decision (one forward pass for route + planner).
- The plan decision is a local heuristic (no model, 11/12 on the probe set);
  `routing_planner` gained `heuristic` as a value and `auto` now means it.
- Threshold calibrated to the new score range (0.55, was 0.85 - that escalated
  almost everything); `latency_ms` reports inference time, not model load.
- Fixed: `shadow` mode no longer silences the judge; the short-prompt shortcut
  no longer bypasses the decider (a 9-word code task used to run as chat).

### IDE integration

- New `POST/GET/DELETE /context/editor`: an IDE reports the open file,
  selection and diagnostics (per workspace, 300 s TTL, 4k char selection cap).
- The block reaches the prompts (direct/pipeline via `_make_messages`, agentic
  via the duo coder prompt) and the run record only keeps a summary
  (`file`, `selection_chars`, `diagnostics`) - never the code.
- MCP server hardened: paths outside the workspace are rejected
  (`PATH_OUTSIDE_WORKSPACE`) instead of silently rewritten; the governance tiers
  are enforced on every dispatch path; header (32 KB), body (2 MB), concurrency
  (8), batch (16) and request-timeout (300 s) limits; stdio answers an oversized
  line instead of dropping it; per-call audit line (`[MCP-CALL] ...`).
- `/internal/tool/exec` (the endpoint MCP and IDEs proxy into) got a loopback
  host guard and requires `HIVEMIND_INTERNAL_TOKEN` for exec tools; read/write
  stay available, the workspace lock is server-side.
- Docs: `docs/mcp.md` (both directions, tiers, limits, env reference).

### Telegram

- `/help` is generated from the command spec, so every command is listed
  (22 instead of the curated subset) and ships an inline keyboard: tapping a
  command runs it - it never starts a run.
- A command that needs a value asks first; the next message is the value,
  `stop` cancels.
- Voice notes now go through the decider: `mode=auto` plus an explicit
  `duo_agentic_mode=false`, so a spoken note is not forced into a coder run.

### UI

- The agentic context block is a card now: header, label column, compact
  fields with a `tok` unit and a muted VRAM estimate instead of a stack of
  orange number inputs. The planner row only shows when "Planner = Coder" is
  off.
- The plan step offers three choices only: always plan, decider decides, never
  plan. Older values (`auto`, `heuristic`, `judge`) still resolve and stay
  visible in the status line instead of being rewritten silently.
- The routing card sits under MODE in the Agents tab and only appears while
  the mode is `auto`: decider, judge and the plan step. The plan row is called
  "Plan step" and says that the planning MODEL lives in Agents, because the
  old "Planner" label read like a model choice. Live line: `Auto -> decider (encoder, CPU) · planner:
  heuristic (free) · threshold 0.55`, plus a warning when a fixed mode
  bypasses the routing entirely.

### Fixes

- In `auto` mode the persisted `duo_agentic_mode` no longer hijacks the route:
  a stored agentic flag (a preset sets it) used to send every auto run straight
  to code_duo, before the decider was asked. A simple question ended up in a
  coder run. Now the flag only forces agentic when the decider is off; an
  explicit request travels as `agentic_force` (the Telegram `/mode agentic`
  sends it), so an intentional agentic run still works.
- The feature search finds voice and routing: "tts", "stt", "routing", "auto"
  and their German twins expand to the real labels, and explicit index entries
  cover the Voice card (TTS/STT toggles, voice, speed, volume) plus the mode
  row, the routing selects and the plan step. Multi-word aliases now expand to
  a list of terms instead of one phrase that could never match.

### Fixes

- Text to speech no longer fails silently per sentence: when the sidecar
  answers 200 without audio (pocket-tts raised `string keys in translate table
  must be of length 1` for the fetched voice config), the engine classifies it
  as `TTS_ENGINE_BROKEN`, retries once with the shipped default voice, and then
  holds a five minute cooldown instead of paying a synthesis round trip for
  every sentence. The error carries the sidecar log hint.
- The read-aloud prefetch keeps a no-op catch, so a superseded fetch can no
  longer surface as `Uncaught (in promise)`, and a broken engine clears the
  queue instead of retrying the rest of the answer.

### Cleanup

- Removed dead code (`_maybe_add_until_finished_block`,
  `reset_read_guard_after_compression`, `_fallback_explore_prompt`,
  `wait_approval_decision`, `reset_to_base_configs`) and the never-read
  `routing_pipeline_allowed` setting; `mcp_servers` is marked reserved.
- `deploy/recommend_models.py`: suggests a start config from the detected
  VRAM/RAM plus the measured tok/s per model.
- Model catalog: `spark-x2.6:4b` pinned to `XHToken/Spark-X2.6-4B-GGUF`
  (replaces the Spark-X2.5 entries) and `lfm2.5:24b-a2b` pinned to
  `LiquidAI/LFM2.5-24B-A2B-GGUF` with its DSpark drafter sidecar
  (`LFM2.5-24B-A2B-DSpark-F16.gguf`, 0.6 GB). The profile carries the
  calibrated `moe_cpu_experts` (24, measured 6051 MiB @c4096) so the VRAM
  pre-flight and the loader agree: 5.91 GB @4k, 6.54 GB @32k on an 8 GB card. Both have profiles and automap
  entries; `tests/test_model_catalog.py` pins repo, filename and drafter so a
  renamed asset cannot silently download nothing again.
- `model_configs/models/spark-x2.6_4b.json` for Spark-X2.6 (4B, Q4_K_M): sampling
  per model card, thinking on by default (`spark-x2.6` joined the reasoning
  bases), tool calls verified live through the embedded template
  (`tool_calls` + `finish_reason=tool_calls` on b11456) - no external
  `spark25` parser needed on llama.cpp.
- Docs: profile-naming example clarified, roadmap corrections (no
  `deploy/fetch_tts.py`, `core/sidecar.py` not built, `RESULTS.md` not shipped).

## [1.4.5] - 2026-10-20

### Fixed

- **LLM pre-explore could never run: three module globals were missing.**
  `hive_functions/pre_explore/llm.py` used `global _llm_client, _llm_client_lock,
  _pre_explore_usage` but never defined them at module level, so the first read
  raised `NameError: name '_llm_client_lock' is not defined`. Every pre-explore
  LLM call burned 30 retries and ended in `LLM FAILED after 30 attempts` - the
  whole exploration silently never happened. The names are defined now, and a
  test walks the AST of every pre-explore module to make sure a `global`
  statement always has a matching module-level definition.
- **Parallel pre-explore crashed on `asyncio.gather`.** `runner.py` wrapped the
  gather in `asyncio.create_task()`, which requires a coroutine - `gather()`
  returns a future, so the step failed with `a coroutine was expected, got
  <_GatheringFuture pending>`. It uses `ensure_future` now.
- **A pre-explore failure can no longer take the run down.** `_xparallel_results`
  was only bound on some paths, so an early error (for example a failed VRAM
  pre-flight check for the explorer model) ended in `UnboundLocalError` at
  `state.update`. The variable is pre-initialised, and the pre-explore phase is
  wrapped: it logs the full traceback, tells the user, and continues with the
  static repo map instead of aborting.

### Added

- **Opt-in downloader for the routing decider.** The release zip still ships no
  weights; `deploy/fetch_decider.py` fetches `fastino/GLiNER2.5-Decide` into
  `models/decider/` and prints the two settings to use. The Python dependency
  (`gliner2` + torch) stays out of the normal install and lives in
  `requirements-decider.txt`. Without both, `routing_decider` stays off and the
  LLM judge decides as before.

## [1.4.4] - 2026-10-20

### Added

- **Token statistics now answer "which model, how fast, how stable" instead of
  only "how many tokens".** Each run records the phase durations
  (`phases_ms`) and both the requested and the resolved mode, and
  `/soul/token-stats` returns three aggregations: `by_model` (per model *and*
  role: coder, planner, critic, pre-explore, direct — plus the pipeline stages
  analyst, refiner, synthesizer, vision), `by_model_by_day`, and `by_mode`
  (which route actually ran: `code_duo`, `direct`, `pipeline`).
  The Soul panel renders a compact model list (speed · crash-free) and a mode
  list; clicking a model filters the run list below to the runs that model
  touched.
- **Real decode-speed fallback from the llama-server logs.**
  `deploy/backfill_speeds.py` parses `eval time … tokens per second` out of
  `logs/llama_server_*.log`, maps the GGUF file name to a model tag via
  `models.json` and stores a median per model in `token_stats.json`. Records
  without phase timing show that measured rate (marked `~`) instead of nothing.
- **Pipeline runs are measurable now.** The pipeline runner reported no usage at
  all: `requests 0`, `prompt_tokens 0`, no phase data, and its token count was a
  characters/3 guess. `chat_stream` gained an opt-in `usage_out` (default off, so
  direct and duo paths are untouched) and the run wrapper around
  `pipeline_chat_stream` reports every stage as `usage_meta` + `run_meta`, with
  its own PhaseTimer bucket. A pipeline run now records e.g.
  `requests 4`, `prompt_tokens 4205`, `phases {analyst 1100, refiner 133,
  critic 600, synthesizer 172}` and the matching `phases_ms`.

### Fixed

- **`/static/*` no longer hides a deploy behind the browser cache.** The
  `?v=` token only changed when someone remembered to bump it; a middleware now
  sends `Cache-Control: no-cache, must-revalidate`, so a normal reload is
  enough.
- **User stops no longer count as failures.** `crash-free` is
  `completed / (runs − abort − graceful_stop)`: stopping a run yourself says
  nothing about the model.
- **Roles that were configured but never ran are not counted.** The critic
  appeared in 70 duo records without a single critic round, which made the model
  list report runs that never happened.

- **The stream handler no longer swallows error tracebacks.** It logged
  `logger.exception` from a different block than the `except`, so every
  `/stream` failure printed `NoneType: None` and the cause was unreadable. The
  traceback is now captured where the exception happens and logged with it.
- **Routing groundwork (opt-in, inert by default).** `routing/decider.py` adds a
  resolver with three independent switches - `routing_decider` (a CPU encoder,
  GLiNER2.5, no VRAM), `routing_judge` (the existing LLM judge) and
  `routing_planner` - each `off | shadow | on`. Defaults reproduce today's
  behaviour exactly (`decider=off, judge=on, planner=auto`); an existing
  `settings.json` without the keys is resolved through `DEFAULT_SETTINGS`, so a
  missing key never silently disables the judge. The encoder is an optional
  dependency: without `gliner2` the resolver logs one warning and falls back to
  the judge. The decider can only pick `direct` or `agentic` - it never selects
  the pipeline route. A missing dependency, a timeout or a broken judge falls
  back instead of failing the run.

## [1.4.3] - 2026-10-19

### Fixed

- **write_file could hang forever on a folder the engine may not write into.**
  The atomic write used `tempfile.mkstemp(dir=<target>)`. CPython treats
  `PermissionError` there as a name collision while `os.access(dir, W_OK)` says
  the folder is writable - and on Windows that check reads the read-only
  *attribute*, not the ACL, so it is nearly always true. In a sandboxed/ACL-locked
  workspace the call looped up to `TMP_MAX` (2**31-1) at 100 % CPU: the coder
  called `write_file`, the UI sat on "Processing context..." for eleven minutes,
  and the file was never created. All seven atomic write paths now create the temp
  file through `_mkstemp_bounded()` (`O_CREAT|O_EXCL`, at most 16 names, a real
  permission error propagates on the first try), and `write_file` answers the model
  with `WRITE_FILE_PERMISSION` naming the sandbox/ACL cause instead of spinning.
- **Same trap in the STT temp WAV** (`core/stt_audio.py`): a locked-down `%TEMP%`
  could hang a transcription. It now creates the file the same bounded way.
- Settings panel nesting (regressed in 1.4.1, fixed in 1.4.2) and the gated
  `/prefetch/stats` route stay as they are; nothing else changed.

### Note

Start HiveMind by double-clicking `start_hivemind.bat`, not from inside an
agent's restricted terminal - otherwise the engine inherits a write sandbox that
only covers the HiveMind folder.

## [1.4.2] - 2026-10-19

Hotfix for 1.4.1: the settings sidebar came up broken and the Prefetch card had
lost its endpoint. It also folds in the internal work that was already on
`main` after the 1.4.1 tag.

### Fixed

- **Broken sidebar in 1.4.1**: an extra `</div>` (left over from removing the
  per-role preload rows) closed the Configs panel right after the "Startup
  Preload" card. Everything below it — Keep-Alive, Voice, Smart Preload, Model
  Config, Energy, Git, Telegram — rendered outside any panel, so those cards
  stayed visible under *every* tab and could not be scrolled. Memory, Soul,
  Models and Chats fell out of `#sidebar` into `<body>` and their tabs went
  blank. One line removed; the panel nesting matches 1.4.0 again.
- **`/prefetch/stats` returned 404**: the endpoint lived in the debug router,
  which is gated behind `debug_api_enabled` since 1.4.1 — and that defaults to
  off. The Prefetch card loads the stats on every panel open, so the route now
  lives in the always-mounted core router. No user-facing setting to change.

### DSpark drafter (Sharp-MiniCPM)

- **Ships with the installer**: the two Sharp-MiniCPM5-2B specs download
  `openbmb/MiniCPM5-2B-DSpark-GGUF` (`MiniCPM5-2.6B-DSpark.gguf`, ~650 MB) as a
  sidecar — never registered as a model. Sidecars are now checked *before* the
  `--only-missing` skip, so an existing install gets the drafter on a re-run.
- **Enabled for the Sharp-MiniCPM profiles**: `model_configs/models/`
  `minicpm5_2b-sharp.json` carries `dspark_draft_filename` + `dspark_draft_n_max: 2`.
  Measured with the app's own llama flags (AMD Radeon iGPU, Vulkan0, 16 threads):
  target alone 14.1 tok/s, DSpark n-max 2 10.6 (49% acceptance), n-max 4 7.7
  (34%) — slower on this laptop class. `HIVEMIND_DSPARK_SPEC=0` turns the
  drafter off machine-wide without editing a profile.
- **`dspark_draft_n_max` is honoured**: the profile field existed but nothing
  read it; every DSpark model ran with the global default (10).

### Internal

- `core/duo_runner.py`: the 21 module-level helpers and the stable callbacks
  moved into `core/duo/preflight.py` (pure move, AST-verified, re-exported).
- Stuck handler: `loop_detected` is initialized before the extracted handler
  runs — the `UnboundLocalError` aborted every run after the first tool call.
- Verify gate: a completion accepted after one veto is now marked unverified
  and reported instead of silently passing.

## [1.4.1] - 2026-10-18

Follow-up to the project audit: reproducible installs, no dead switches, less
information disclosure.

### Added

- **`deploy/gen_settings_docs.py`** - the generator `docs/settings.md` always
  claimed to come from. It was never committed (it sat in `.gitignore`), so the
  table had drifted to 204 of the then-236 keys. It is now in the repo,
  regenerated (237 keys) and `--check` fails the suite when the doc is stale.

### Fixed

- **Reproducible installs**: `uv.lock` is committed (64 pinned packages) and both
  installers sync from it (`uv sync --frozen`), so a release installs exactly the
  versions it was tested with instead of whatever is newest that day. `.gitignore`
  had a `*.lock` rule that silently kept the lockfile out of the zip.
- **Dead switches removed**: `automap_code_duo_enabled`,
  `automap_duo_pre_explore`, `automap_duo_parallel_preexplore`,
  `automap_pipeline_websearch_enabled`, `duo_distilled_executor` and the per-role
  `startup_preload_{analyst,coder,judge_in_agentic}` were read by nothing. The
  Automap card now drives the real settings (`duo_pre_explore`,
  `duo_parallel_preexplore`, `pipeline_websearch_enabled`), startup preload has
  one honest switch, and the three per-role preload rows are gone.
- **Debug API closed by default**: `/debug/tasks` (asyncio task stacks with file
  paths), `/debug/models`, `/thinking/debug` and `/budget/debug` were mounted on an
  unauthenticated loopback API. New `debug_api_enabled` (default off) gates the
  whole router - verified: `/debug/tasks` answers 404.
- **Write-truncation log no longer stores user code**: `logs/write_truncations.jsonl`
  kept the full raw `write_file` call (up to 64 KB of generated file content per
  entry, never rotated). It now records `path`, `raw_len`, `finish_reason` and
  `json_complete` only - still enough to see that a call was cut off mid-JSON.

## [1.4.0] - 2026-10-17

Voice, end to end: speak answers here, dictate runs from the phone, and hear the
answer back. Everything is opt-in and nothing starts until you turn it on.

### Added

- **Text to speech (Pocket TTS, CPU only, off by default).** A Voice card block
  with a master switch, seven shipped English voices (default `anna` - `cosette`
  is a non-commercial recording, see `docs/voice_licenses.md`) a speed control
  (`1.08` - Pocket TTS has no speed parameter, so HiveMind time-stretches the audio
  with WSOLA and keeps the pitch), a "Play sample" button and an honest engine
  status line. The speaker button on every answer reads that answer - it is
  hidden unless TTS is on, reads the bubble at click time (works on a still
  streaming answer) and never autoplays.
- **The TTS engine is a supervised sidecar, not a dependency.** It starts through
  `uvx` on the first request (isolated environment, PyTorch never enters
  HiveMind's venv), answers `/health` when ready, is stopped at exit, and shuts
  itself down after `tts_idle_stop_s` (default 900 s). Without `uvx` it fails with
  a clear code instead of downloading 200 MB.
- **Read aloud** (four switches in the Voice card): direct mode reads the whole
  answer; agentic runs read the plan first and then the coder's answer; ask-user
  questions and the defensive gate / approval cards can be spoken too. One item at
  a time, stopped by Stop, long text cut at a sentence boundary.
- **Telegram voice notes in** (`voice_in` / the switch in the Telegram card): the
  note is downloaded (20 MB, 300 s caps), transcribed by the local engine and the
  transcript runs exactly like typed text - same owner/private gates, same
  approvals. Every `STT_*` code is translated into a sentence.
- **Telegram answers as voice notes** (`voice_out` / the switch in the Telegram
  card): the final answer is additionally sent as a voice note (`sendVoice`,
  Ogg/Opus) through the same owner-only `send()` boundary. A TTS failure is a log
  note, never a broken run.
- **Installer voice step** (opt-in, four choices): speech to text only, both, text
  to speech only, or skip - plus `deploy\setup_voice.bat [model] [--tts|--both]`
  for later and `install_linux.sh --voice|--tts|--both`. Whatever you skip stays
  one click away in the Voice card, and all three paths call the same fetch code.
- **`GET /tts/status`, `GET /tts/voices`, `POST /tts`** (`format=wav|opus`,
  `X-HiveMind-Duration/Voice/Speed` headers). The voice list is served from one
  place; the UI builds its dropdown from it.
- **Settings search** finds the new features by their everyday names (English and
  German: `tts`, `vorlesen`, `read aloud`, `diktieren`, `microphone`, `voice note`,
  `telegram voice`).

### Fixed

- **Every `/stt` request returned `STT_EMPTY`.** The temporary WAV was created
  with `tempfile.mkstemp(...)` and the fd was never closed; on Windows the file
  stayed open for writing, the parakeet/whisper child could not read it
  ("failed to read audio data") and the endpoint reported "no speech". Soundfile
  reads the same file happily, so the unit test was blind to it - the regression
  test now requires the decoded WAV to be readable *and* deletable by a second
  process.
- **Speech blocked the whole UI.** `/tts` ran the sidecar start (20-30 s on the
  first request) directly on the event loop: SSE, status polling and every other
  request stalled. Synthesis, Opus encoding, audio decode and transcription now
  run in a worker thread; `/tts/status` answers in ~20 ms during a synthesis.
- **The TTS restart budget counted successful starts:** after three normal idle
  shutdowns the fourth start refused to run ("failed to start three times"). Only
  consecutive failures count now, and a healthy start clears the budget.
- **The mic button rendered as a small white box** (the stylesheet still carried
  an old cache-buster) and the "speech to text" sub-block stayed visible after
  switching it off (the toggle did not update the in-memory setting). The button
  now has the send button's footprint (40x40, 7 px radius, red, pulsing while
  recording) and both master switches collapse their blocks immediately.
- **Installer step numbering** said `[x/7]` for the first five steps and `[x/8]`
  for the last three after the voice step was inserted.

### Changed (tools, sandbox, Linux)

- **Write tools that were never gated now ask**: `edit_ast`, `replace_lines` and
  `patch_file` write files and were missing from the approval list - with phone
  approvals on, the model could rewrite a file without a card. The policy (what is
  gated and, more importantly, what is deliberately not) is now written next to
  the list.
- **The dispatcher refuses what the run does not offer**: a name outside every
  mode's tool list (`patch_file`, a resurrected legacy handler) and a
  machine-changing tool outside the current mode (a chat or read-only run calling
  `write_file`/`run_bash`) are rejected before a handler runs.
- **Malformed tool arguments are an error again** in the agentic loop: broken JSON
  used to become "no arguments" and the tool ran with defaults (a `list_dir` on
  the workspace root instead of the intended folder). The duo path already did
  this; the agentic loop now matches.
- **`start_background` goes through the destructive gate** like `run_bash` (it
  runs the same kind of shell command and was only protected by the approval
  switch).
- **`search_code` runs without a shell**: the backends were shell strings built
  from the model's pattern and path (`rg … "pattern" 2>nul`, `grep … | head -50`).
  A read-only tool that runs without an approval prompt must not hand cmd a
  string; every backend is an argument list now and the 50-line cap is applied in
  Python.
- **Linux gets per-tool limits**: Windows job objects have no twin on POSIX, so
  the only limit was the service-wide `MemoryMax` - a runaway `run_python` could
  take the whole server down. `RLIMIT_AS` / `RLIMIT_NPROC` / `RLIMIT_CPU` are
  applied per child (same settings keys, `0` = off), and the unit now carries
  `HOME`, `UV_CACHE_DIR` and `HF_HOME` for the speech sidecar.
- **Linux installer**: no longer copies `settings.json`/`sessions`/`logs` from a
  developer checkout into `/opt/hivemind`, runs its voice step as its own step
  after llama.cpp, installs `requirements-voice.txt` when the extras are missing,
  and pre-warms the TTS cache for the service user instead of root.

### Fixed (voice polish, 2026-10-18)

- **License audit of the audio stack** (`docs/voice_licenses.md`): the Pocket TTS
  model is CC BY 4.0, the code MIT, and the shipped voices come from four sources
  with different terms. **`cosette` is Expresso = CC BY-NC 4.0 (non-commercial
  only)**, so the shipped default is `anna` (CC BY 4.0) now; `cosette` stays
  selectable and is labelled non-commercial. The Voice card prints the license per
  voice and the attribution line, the README links the table.
- The "Play sample" button wrapped onto two lines (it borrowed the 40x40 icon
  button); it has its own text-button style now.
- The per-answer speaker button moved from the header into a footer under the text
  and only appears once the answer has content - in the header it floated over an
  empty bubble and looked attached to the user's message.
- **Read aloud starts ~7x sooner**: the text is queued in chunks (first one ~60
  characters, then up to 240) instead of one 600-character request, the next chunk
  is fetched while the current one plays, and the engine reuses one keep-alive HTTP
  client. Measured warm: 49 characters -> 0.9 s, 98 -> 1.6 s, 320 -> 6.5 s before
  the first sound (the sidecar is pre-warmed on page load when TTS is on).
- Turning the TTS master on now enables the two answer switches (direct + agentic)
  when none was set, so "voice on" actually speaks without a second visit.

- **Volume control and a louder default**: the sidecar returns speech at about
  -15.5 dBFS peak (measured), i.e. 12-15 dB quieter than a normal speech file. The
  engine now levels every chunk to -1.5 dBFS (`tts_normalize`) and the Voice card
  has a volume slider (`tts_volume`) that applies to read-aloud, the per-answer
  speaker and the sample.

- **Read aloud no longer narrates the tool output**: the extractor strips tool
  chips, websearch result blocks, tool-call rows, think blocks and code panels
  before speaking, so a direct-mode answer is read as the answer - previously the
  whole bubble (every search result included) went to the engine.

- **Voice notes for mirrored runs**: a run started in the browser now sends the
  voice note to the phone as well. The mirror path relayed text only, so "answers
  as voice notes" never fired for anything but a phone-started run.
- **A gateway restart no longer looks like a dead bot**: the replay protection
  discards what arrived while it was offline; the owner now gets one short note
  telling them to resend.

- **Presets are complete snapshots and loads are non-destructive.** Loading a
  preset now MERGES the per-agent config instead of replacing the whole dict, so a
  preset saved before a role or field existed can no longer wipe its
  `max_tokens`/`thinking_budget` (the reported "output budget resets and answers
  get cut"). The live pipeline is refreshed right after a load (it used to keep the
  pre-load budgets, which the next save then stored back), the save carries the
  agent cards that are on screen, and the load no longer blocks the UI with an
  alert. A regression test asserts that a snapshot contains every persisted
  setting (secrets and machine paths stay out).
- **Download catalog fixed**: the Hermes entry pinned file names the upstream repo
  had renamed, so a fresh install fetched neither weights nor projector. The
  README table now lists the real repo and the measured download sizes.

### Telegram output contract (2026-10-18)

- **Choose what a phone run sends back**: `telegram_result_mode` = `both` (text
  plus a voice note), `text`, or `voice` - the last one is a results-only channel
  where the model-generated answer arrives as a voice note and nothing else. A
  failed synthesis falls back to text; an error always stays text.
- **`telegram_progress_msgs`** silences the status line and the tool-call lines.
  Approval cards and the final result are unaffected - they are safety and result,
  not progress.
- **`tts_preload_run`** (Voice card) warms the speech engine when a run starts, so
  a phone-only flow speaks in about a second instead of paying the cold start. The
  gateway calls the new `POST /tts/preload` right after the user turn is written.

- **A broken Python environment is diagnosed instead of guessed.** A fresh
  1.3.8 install on Python 3.14 reported `cannot import name 'ArgsKwargs' from
  'pydantic_core._pydantic_core'`; the repair reinstalled the pair, pip said OK and
  the import still failed, so the only advice left was "delete .venv". The
  launcher's env check now prints the interpreter, the pydantic/core versions and
  the compiled module's size, names a shadowing copy, reinstalls **wheels only**
  (`--only-binary :all:` - a source build is how a half-written extension
  happens), and then walks the real causes in order: antivirus quarantine, a
  mismatched pair, a fresh venv, a fresh folder. `pydantic>=2.11` is now an
  explicit dependency, because an old `pydantic_core` next to a new `pydantic` is
  exactly what raises that error.

- **Installer: the venv is pinned to Python 3.14.** `uv python install
  3.14` failed when an unmanaged `python3.14.exe` existed in `~/.local\bin`; the
  installer noted it and continued, uv then chose **3.15**, where `tree-sitter`
  has no wheel - the source build needs MSVC and killed the install. The
  interpreter is now pinned (`uv venv --python 3.14`, `uv sync --python 3.14`), a
  `--force` retry replaces the foreign executable, a missing 3.14 stops with
  instructions instead of falling back, and the finished venv is asserted to be
  3.14. A leftover `.venv` without `python.exe` is cleared first (that used to
  fail with "not a valid Python environment" on the second run).
- **A stray line in `install.bat`** printed `Der Befehl "ERROR-every-second-run"
  ...` at the start: an unterminated quote in a REM made the next line a command.
- **Telegram card: the voice controls grey out with voice off.** The
  result-format select and the hint lines are now greyed out and disabled together
  with the voice-note toggles.
- **Complete licence table for the audio stack** in the README: Pocket TTS model
  (CC BY 4.0 + gate terms) and code (MIT), the voice recordings per dataset
  (VCTK/Alba CC BY 4.0, Voice-Zero CC0, Expresso **CC BY-NC 4.0**), whisper models
  (MIT), whisper.cpp/parakeet binaries and the parakeet GGUF (MIT, upstream
  CC BY 4.0). Also states that no model is vendored - everything is downloaded at
  first use.

- **Env check without imports**: it reports the resolved paths, shadowing copies
  (PYTHONPATH / user site), the compiled module's size and a missing __init__.py
  (namespace package -> "unknown location"), and escalates the repair to
  uninstall + install so orphaned files are actually removed.

### Notes

- English only. Speech to text models: `parakeet-q4k` 396 MB (3.6 % micro-WER),
  `base` 141 MB, `small` 465 MB, `tiny` 74 MB - pick by RAM, the installer prompts
  once.
- Warm TTS runs at 1.25-1.85x realtime (320 chars -> 19 s of audio in 10 s). The
  first request after idle pays the sidecar start once. Text is capped at 600
  characters per request.
- Speech to text + text to speech cost ~1.4 GB of models and two uv caches, all
  outside the release zip and the repository.
- Chat push-to-talk (V2) is not in this release; the mic button appends a
  transcript to the message box, and the read-aloud switches read answers,
  the plan, the coder answer, ask-user questions and approval cards.

## [1.3.11] - 2026-10-17

Web search that actually answers, a planner-briefing cap, and the voice work
from 1.4 (all off by default).

### Fixed

- **Web search returned the same junk for every query.** Four separate causes:
  the shipped SearXNG config replaced every upstream default instead of merging
  (so no outgoing User-Agent - Wikimedia answered 403 and the wikipedia engine
  silently returned nothing), google was bot-blocked and poisoned general
  queries, `bing` answered English queries with another region's pages, and the
  tool itself pinned `engines="bing,duckduckgo"` + `language="all"`, overriding
  whatever the instance had enabled.
- **Rebuilding the SearXNG image changed nothing** in the running container: the
  image declares `/etc/searxng` as a volume, so an old anonymous volume kept
  shadowing the baked config. Every `compose up` in `searxng.bat` now passes
  `--renew-anon-volumes`.
- **A query that names Wikipedia is answered by the MediaWiki API directly**
  (descriptive User-Agent, shorter-prefix retries, ranked by title overlap) when
  the search engines return nothing useful - the case that used to return shop
  homepages. An empty engine result falls back there too.
- **A web-search budget wall no longer kills the run.** It used to set
  `loop_detected` after two exhausted calls; a live run ended after 24 rounds
  with zero files written. Now the first exhaustion disables `web_search` for the
  rest of the run (the coder keeps working with its other tools) and only a model
  that keeps calling the disabled tool four more times ends the run.
- **Near-identical searches are recognised.** The dedup compared exact strings,
  so "RDP processor RAM memory" and "RDP processor graphics memory" were
  different searches. Token overlap (or three shared words) now counts, and the
  third such attempt stops the topic with a clear instruction instead of burning
  the budget.
- The result header printed the literal `{_ws_today}` instead of the date.

### Added

- **Coder briefing limit** (`duo_coder_briefing_think_tokens`, default 1500): the
  planner's thinking is injected into the coder as a briefing only up to that many
  estimated tokens; above it the coder gets the pure plan - a planner that thinks
  8-16k tokens before writing a plan otherwise poisons it. `0` = plan only,
  `-1` = always inject (old behaviour). The decision is logged as
  `[CODER-BRIEFING] mode=... think_tokens=... limit=...`.
- **Speech to text (English only, off by default):** a Voice card in the settings
  with an honest status line (engine / model / ready), a Download button with
  progress for the whisper.cpp + Parakeet binaries and models, a 3 s microphone
  test, model/language/preload/thread controls, and a red mic button left of send
  that appends the transcript to the message box. Nothing runs until the switch
  is turned on; the optional audio packages live in `requirements-voice.txt`.
- `deploy/fetch_whisper.py` fetches the whisper.cpp release (whisper-cli,
  whisper-server, parakeet-cli) and the model tiers.

### Notes

- Search engines enabled in the shipped config: duckduckgo, mojeek (general) plus
  github/stackoverflow/pypi/npm/lib.rs/arxiv. google, bing, brave, wikidata are
  disabled with the reason in the file.
- Voice is English only in this release and has no chat push-to-talk beyond the
  transcript button described above.

## [1.3.10] - 2026-10-17

Workspace regression fix, plus the first, disabled piece of the voice work.

### Fixed

- **Workspace regression (introduced in 1.3.9).** The web UI sends the global
  *Workspace* field with every run, and 1.3.9 let that value beat every other
  source. Consequences on a machine whose settings came from somewhere else: a
  chat's own workspace was overridden (files landed in the wrong project), and a
  path that only exists on another machine left the run with **no** workspace at
  all - `write_file` had no root and the model looked like it had forgotten the
  project. The UI value is now treated as the default it is; only a value that
  *differs* from the setting counts as an explicit request.
- A stale workspace value no longer ends a run empty-handed: the normal chain
  (task path, chat workspace, UI setting, last used) decides, auto-commit stays
  off, and the run says which directory it used.
- A workspace fallback to the engine directory is no longer stored as "last
  used" - one accidental run used to pin the install folder for every later run.
- `searxng.bat` no longer closes silently: it probes the Docker **daemon**
  (not just the CLI on PATH), explains a stopped Docker Desktop, keeps the
  window open when double-clicked, and reports a stopped container instead of
  "[OK] SearXNG is running". (This was already in the refreshed 1.3.9 asset; it
  is part of the release now.)
- A CPU-only install with a large context now says so (`[PERF-WARN]` with the
  real number and the practical 8k-16k range) instead of looking like a hang.
  This is the "one write_file took 25 minutes on a laptop" class of problem.

### Added (off by default, no UI yet)

- Speech-to-text groundwork, English only: `POST /stt` and `GET /stt/status`,
  `deploy/fetch_whisper.py` to fetch the whisper.cpp / Parakeet binaries and
  models, and the `stt_*` settings. Nothing runs until `stt_enabled` is turned
  on, and the optional audio packages are only pulled in by
  `requirements-voice.txt`. First step of the 1.4 voice work.

## [1.3.9] - 2026-10-15

Chunk-resume verification run + resume durability:

- chunk checkpoints now cover EVERY done path: the persist call existed
  only on the skip branch (the "after every done chunk" comment
  overstated it) - all seven normal completion paths (critic-skipped,
  auto-approve, auto-test skipped/passed/exhausted, normal completion)
  write the resume block after advancing
- _halt_with_resume does NOT halt anything (name misleading) - it only
  writes the resume block; the chunk loop continues on its own
- RESUME DURABILITY (found by the verification run): resume-block
  writes for API-driven runs (no chat session file) silently no-opped -
  the context path resolved to None while the log claimed "Block
  saved"; a hard kill then lost the checkpoint. Every chat_id now gets
  a standalone context file under sessions/, and _write_resume_block
  VERIFIES the write and logs honestly when it did not persist
- verified live end-to-end: planner reads index.html in one call, chunk
  1 committed then chunk 2 starts without pause, the plan is injected
  into the chunk-2 coder context, exactly ONE final test message, and
  after a hard engine kill + restart the resume intake reports
  "Resuming: 2/3 chunks already done" and continues at chunk 3
- ui: short tool results rendered the same text twice (summary carried
  the full text AND the force-opened pre repeated it - every green
  run_tests row); the pre is skipped when the summary IS the content
- the smoke-test nudge now INSTRUCTS the model to include at least one real
  assertion or explicit output check ("a file that cannot fail does not
  count as a test"). This is prompt text only: the auto-test gate checks the
  command string of the last run_bash (it looks like a test run), not the
  content of the written test file - a test that cannot fail still passes
  the gate

Budgets redesign: no model-class staffels

- core/budgets.py: Budgets.resolve(n_ctx, cpt, max_out, settings) derives
  every cap from measured sizes ONCE per run and freezes them (schema-
  visible values must not change mid-run — prefix cache). Settings keys
  "budget_<field>" override single values; None/absent = auto. tools/
  limits.py now holds ONLY fractions and named floors/ceilings — the
  only hard numbers left in the codebase
- the fixed staffels are gone: tool-result cap = 0.12 × n_ctx × cpt
  (clamp 2k-24k; ~7.7k at 16k, practically yesterday's 8k), read refusal
  by estimated SIZE against the read budget instead of a 1200-line
  count, anchor window and outline derived from the injection budget,
  compression trigger headroom-based (reserve = clamped max_out + p90
  growth; free < 1.5 × reserve triggers, reproducing ~0.70 at 16k/8k)
  — write-heavy runs trigger earlier without a special case
- tool schema is a CONTINUUM, not compact/full: every tool has a
  priority (tier-0 core always kept), three description levels, and the
  fitter upgrades by priority while the schema budget (0.25 × n_ctx ×
  cpt) holds; what does not fit minimally is dropped. At 16k MiniCPM
  keeps the full schema; an 8k model shortens automatically — no model
  names anywhere (grep test enforces it)
- chars/token is a per-model EMA now (update from every real response:
  sent chars vs usage.prompt_tokens), start value 3.92 from the live
  /tokenize probe — Budgets use it instead of the 3.0 constant
- resolved budgets are logged ([BUDGETS] line) and written as a
  phase="budgets" telemetry row so every run shows what was derived
- new tests: test_budgets (monotonicity in n_ctx, the 16k base case
  reproduces the tuned values, override rule, floors/ceilings at
  extremes, cpt EMA convergence, schema continuum, and a grep test that
  keeps model names/size staffels out of budget and tool-selection
  code); test_schema_truth now pins the schema text to the RESOLVED
  run budgets

Compression, injections, tool profiles and web search:

- compression without slot eviction: deterministic observation masking
  (stale tool outputs -> recall markers, the same machinery as the old
  >90% emergency valve) now runs BEFORE the LLM summary and can resolve
  the threshold crossing without it - no LLM call, no coder evict
  (settings: duo_mask_before_compress, default on)
- the light compression model (duo_compress_model) is only loaded when
  it fits NEXT TO the coder (can_fit excludes the coder's slot) - the
  documented live case (light model evicted the coder: ~20s reload +
  reuse=0%) can no longer happen; otherwise the coder compresses itself
- edit_file failure anchor: instead of a fixed 60-line file head (up to
  4.3k chars per failed attempt) the error now shows +-5 lines around
  the CLOSEST match to the failed old_text (best_match_location in
  patch_2_fuzzy_edit), capped at 1200 chars - the retry loop edits from
  the right region on the first retry
- bash-loop reinject menu is names-only (~300 instead of ~5.2k chars -
  the model already carries the full descriptions in its schema); the
  too-large-read signature outline dropped 200 -> 60 items
- tool descriptions are fitted to the schema budget instead of a
  profile switch (see the budgets section): every tool has a priority,
  core tools are always kept, non-core tools that do not fit even at the
  minimal description level are dropped
- web_fetch has its OWN call budget (webfetch_max_calls, default 20) -
  pure doc-fetching no longer drains the web_search budget
- trafilatura extraction runs in a worker thread - a heavy page no
  longer freezes all streaming on the event loop
- browser click returns only the CHANGED content vs the previous
  snapshot (capped 2000, explicit snapshot for the full page); evaluate
  is capped at 8000 like every other browser action

Tool-pipeline ordering and budgets:
cache hygiene and measurement first):

- measurement basis first: one phase="tools" telemetry row per round
  (tool-result chars before/after the 8k cap, user-role injection
  chars, re-read count, round ms) plus cache_miss flag and shipped
  tools_schema_chars on the coder row - baselines are measurable
  before/after each lever
- real chars/token measured on the live slot via /tokenize: 3.92 for a
  code+prose mix, not the 3.0 constant (schema ~4.3k tokens, not 5.6k;
  the chars/4 estimator overestimates by ~30%)
- schema truth from constants: tools/limits.py is the single source,
  read_file's schema text is generated from it and now states the real
  limits (REFUSED above 1200 lines / capped 32000 chars) - the stale
  "capped at 200 lines" text sent small models into 200-line ritual
  reads the handler no longer needs; the write-result BIG_FILE_NOTE
  400-line lie and the handler's stale comments are gone; a new test
  (test_schema_truth) pins handler, constants and schema text together
- run_bash schema slimmed 1006 -> ~590 chars: the PowerShell traps
  (backticks, -e one-liners, stderr, curl -m) moved into the coder
  system prompt's OS hint (once per run instead of once per round in
  the schema; the routing + UTF-16/no-shell-writes core stays in the
  schema)
- compression trigger gap closed: the guard already prefers the
  server's real prompt_tokens (GUARD-REAL-BASIS), but that value is
  one round stale - the chars appended since that POST are now added
  with the measured real/estimator ratio (DELTA-ON-REAL), so a full
  big-write round can no longer slip past the trigger
- prefix-cache hygiene: the reactive-think activation used to rewrite
  EVERY system message mid-run ("<|think_on|>" prefix), which forced a
  full re-prefill on 8 GB Vulkan. Thinking is now re-enabled
  payload-side (chat_template_kwargs keys off think_runtime, overriding
  no-think) and the only in-context change is one appended note; new
  test (test_prefix_stability) pins the append-only behavior

/vram/estimate fix:

- /vram/estimate now resolves _MOE_TABLE with the same exact/-ud/
  prefix logic as vram_of_moe (new moe_cfg_for helper) - the UI
  estimate previously fell through to the dense heuristic and showed
  the dense size for a large MoE where the pre-flight gate used the
  MoE row

Host-RAM caps (the "compression frees RAM, then swap fills" report):

- b11456-era context features defaulted to host-RAM-heavy values: the
  prompt cache could grow to 8192 MiB (idle-slot snapshots are NOT
  cleared with per-slot KV, which is how HiveMind slots run) and each
  slot kept 32 context checkpoints in host RAM. On long installs this
  filled RAM and then swap; a compression rewrites the prefix, so the
  cached old state was dead weight anyway
- slots now start with --cache-ram 1024 and --ctx-checkpoints 4. The
  effective override path is the environment: HIVEMIND_CACHE_RAM_MIB /
  HIVEMIND_CTX_CHECKPOINTS (read in backend/llama_config.py). The settings
  keys llama_cache_ram_mib / llama_ctx_checkpoints are read by server.py,
  but they have no entry in DEFAULT_SETTINGS, no UI field and no
  documentation - they only take effect if written into settings.json by
  hand, and a settings.json entry then also overrides the env variables

task_complete badge honesty ("untested" despite green tests):

- the badge showed the model-supplied build_status default ("untested")
  even when the run itself had green tests on record. The run's own
  test outcome (model-invoked run_tests, auto-test, or a bash run that
  looks like a test invocation) is now tracked per run and upgrades ONLY
  the default: untested -> passing/failing; an explicit model status like
  "blocked" stays

MoE is now a question, not a name guess:

- the 30B+ heuristic silently decided which models get expert offload.
  Model profiles accept "moe": true/false (config > heuristic), the
  config UI asks per model (Auto / MoE / Dense, POST /config/moe-pin)
  and the hint shows whether the default is pinned, table-calibrated
  or name-autodetected

Tool use and context:

- the tool schema is fitted to the context window on EVERY tool path, not
  only in the duo coder: the direct tiers and the tool agent shipped the
  full set (~14.6k chars) against an 8k window before. At 8k they now send
  the tools that fit with the descriptions that fit; at 16k nothing changes
- git_status and git_commit only appear when git is configured for this
  install (git_tools_enabled: auto | true | false, auto = a git credential
  is set). Tools that need no service stay where they are
- a mistyped tool name used to be corrected with the first eight tool names
  alphabetically - a list that contained none of read_file, write_file,
  run_bash, run_tests or task_complete. The correction now names the most
  important tools first and says how many exist
- content masked or deduplicated out of the context is re-readable again:
  the read guard was not released, so a fresh read answered "already read,
  the content is in your context above" while it was gone
- the "image left the window" note is appended once per run instead of on
  every round
- the read ladder ("exploring but not implementing") now fires for a model
  that only reads - 8 files without a single write - and stays silent in
  read-only modes and in the middle of normal multi-file exploration
- chars-per-token is measured on the running server (/tokenize on the exact
  tool schema) instead of assuming the 3.92 default; every cap in the run
  derives from that measurement. Falls back to the per-model EMA
- the injection budget is enforced instead of only logged, so hint text
  cannot stack without a cap
- wedge-handoff repair: the fix agent inherits the coder's context size
  (it used to reload the warm slot) and its output budget is configurable
  (duo_wedge_handoff_max_tokens)
- **Unload coder after run** (Configs tab toggle, `duo_coder_after_run`): OFF leaves
  the coder warm for the next prompt (the Coder TTL decides when it is released),
  ON releases the slot at the end of the run - also on abort - so the next prompt
  can load any model or mode without evicting something first
- `install_package` reports its own failure (`INSTALL_FAILED`, not a `run_bash`
  label) and, when pip has no wheel for the venv's Python, says so with the
  three ways out - the same hint now also fires when pip is run through
  `run_bash`, which is the only path left once the tool list is trimmed

## [1.3.8] - 2026-10-11

Chunking and long-term-memory reliability:

- chunk progress is persisted (resume block) after every completed
  chunk - a hard kill previously lost all in-memory chunk progress
  and a resumed run repeated chunks from zero
- the auto-test anti-spoof check now recognizes bare python test-file
  runs (python3 test_smoke.py counted as "no test run", which stacked
  auto-test + nudge + unknown-runner on top of a passing test)
- read_file limits are char-based instead of the 200/400-line double
  gate - large files no longer force the model to assemble content
  from many small range reads
- memory discipline prompt: the coder must call the remember tool for
  durable facts; plain-text storage claims no longer count as saved
- telegram: planner thinking tail is relayed (💭 Planning: …) when the
  plan lands

## [1.3.7] - 2026-10-11

Telegram mirror visibility + coder vision honesty:

- browser screenshots for coders running without mmproj now state that
  the model cannot view the image and point at snapshot/evaluate text
  output (source of truth: slot.vision_active from /props)
- telegram mirror relays tool calls made before the mirror attached
  (last 6 ride the adoption note, newest last)
- pre-explore partition_done events are relayed (one line per
  partition - early read/search work is no longer invisible)
- per-tick tool-line cap raised from 12 to 20

## [1.3.6] - 2026-10-10

Follow-up fixes found in live runs:

- write_file calls cut at the output limit are no longer salvaged and
  executed - a half-written file that looks complete caused rewrite
  loops; cut writes are dropped with a clear notice so the model
  re-sends in smaller pieces
- read_file's too-large message now states the file on disk is
  complete, and write results on big files carry a note that full
  reads above 400 lines need line ranges - stops rewrite loops caused
  by partial views of fresh writes
- /stream model overrides now patch the agent card dict as well as the
  flat key; overrides used to silently lose to the card
- ui: finished run journals no longer replay frames over the chat
  transcript, so the end block renders once
- log hygiene: gateway poll lines no longer flood the supervisor log
  (~1.8 MB per two days), the engine httpx log, or the uvicorn access
  log on the console
- VRAM pre-flight gate toggle (settings, on by default): turning it off
  attempts loads the estimate does not trust - real OOM failures drive
  the ctx cascade instead of the estimate
- coder load ctx cascade raised from 3 to 5 attempts (120832 now
  reaches the 8192 floor), and models registered for the new
  K2-Horizon-7B/3.7B and Mellum2.1-12B-A2.5B-Thinking GGUFs

## [1.3.5] - 2026-10-10

Reliability and observability fixes for local runs:

- tool loop requests usage via stream_options.include_usage; llama.cpp
  b11456 ships usage/timings only with this flag, so cache-reuse and
  per-call metrics now arrive on every coder round
- speculative decoding for MTP models: fixed the flag name to
  --spec-draft-n-max and validate it against the binary's --help
  (unsupported binaries start undrafted instead of failing the load)
- VRAM pre-flight: qwen3.5 hybrid KV correction (only 8 of 33 layers
  carry KV, 9.08 KB/token measured) - the old estimate overshot ~2x
  and could block loads that fit
- slot logs: timestamped filenames per server start (no more overwrite)
  and automatic cleanup keeping the newest 10 per port
- telemetry: one JSONL row per coder call under logs/telemetry/
  (prompt/decode timings, cache reuse, time-to-first-byte, compression
  id, prefix divergence index)
- round labels no longer print 'chunk 1/1: None' when a subtask is
  missing
- lint guardrail now fails on unparseable files and scan-count
  mismatches
- internal gateway documents removed from the public tree
- Linux install path re-verified: windows-only packages are
  platform-gated in the dependency lists, all windows imports are
  guarded, the pre-flight falls back to estimates where live GPU
  counters are unavailable, and the uv-based installer needs nothing
  but curl

## [1.3.4] - 2026-10-08

Follow-up on the 1.3.3 showcase round:

- planner parse diagnosis redacts token-like strings in logged heads
- model list collapses same-file aliases (minicpm was listed three
  times for two files)
- git token field: empty field plus saved placeholder instead of the
  cryptic mask; empty and mask-echo saves can never wipe the stored
  token; browser autofill suppressed on the field
- steering divider note placed inside the run block like the steer card
- steer frame: mid-run user messages packaged as the highest
  priority instruction at both injection sites
- tool-round steer drain drops image parts under images_off/preprocess
  like the chunk-boundary drain
- docs: 1.4 candidate backlog (pocket-tts voice, stt, engineering debt)

- full compression as the shipped default (partial off everywhere)
- re-anchor: current file excerpt on old_text errors + project
  inventory after compression
- loop-detected runs commit and push their written files like
  completed runs
- speculative decoding (draft-mtp) flags for MTP models behind a
  registry guard; guardrail baselines updated (re-anchor excerpt,
  re-read dedup unbound fix)

## [1.3.3] - 2026-10-07

Telegram gateway hardening:
branch, release cut at the end of the window):

- takeover relay hardening: fazit on every end path, late-result relay
  for never-adopted runs, silent catch-up on adoption (answer + tool
  dedupe), no more bare "finished" lines
- /gate: G1 force rides body overrides only (mid-run /gate off reaches
  running UI runs); /gate off lifts the toml force floor instead of
  silently degrading phone runs to deny; honest /gate, /help, /status
  texts
- visibility: duo_start model label, ctx meter milestones + compression
  note, file-change lines, coder output deltas, own-path parity, 429
  retry, journal cap resync (logical n + window base), adoption
  state-sync, reattach wording
- ask cards: Yes / No / Wait-later buttons on UI and Telegram (free
  text stays for own commentary); ask cards state they are not the
  approval gate
- coder "takes no images" toggle (duo_coder_images_off); mmproj
  toggles visible in the duo image block
- banner byline back (by: Luzo), /stream 400 on empty body
- git: credentials gate needs Username AND Email - empty git_email
  silently blocked every auto-commit/push (found via live selftest)

## [1.3.1] - 2026-10-06

Deep-audit hardening release: 34 findings across the whole harness fixed
(6 HIGH, 18 MED, 10 LOW), plus the takeover/steering/vision work that
landed during 1.3.0's lifecycle.

### Highlights
- Approval integrity: the approval card now shows the REAL command
  (run_bash's cmd key) instead of a 60-char fallback; expired and
  superseded cards answer honestly on every phone path.
- Slot manager: the steer-image vision upgrade is lock-protected with a
  loading guard and a zombie-slot kill on any start failure; a stale
  ready-event can no longer hand out a dead port; the planner slot is
  truly evicted when the coder needs more context; vision loads of
  unlisted models no longer crash; prefetched/unpinned models age out.
- Containment: browser screenshots are workspace-gated, install_package
  rejects shell-redirect characters, task-path workspaces refuse drive
  roots and files, protected-path matching survives Win32 trailing-dot
  and case tricks, write_file_append checks containment before probing.
- Visibility: takeover runs now deliver the run's RESULT to the phone
  (not just "completed"); ask_user pauses actually pause in direct mode
  (run_id was never propagated); agent-mode OpenAI streaming works
  (frames carried literal backslash-n).
- Hygiene: steer queues / step-skip / pause maps swept at registration
  time, models cache mutates in place, git_token mask can no longer be
  persisted by a stale tab, /vram and first-chat scans off the event
  loop, agent models not on disk wipe to unconfigured at startup.

## [1.2.4] - 2026-10-03

One-click self-updates, a real image pipeline for the agentic duo, and a
round of reliability work on writes, approvals and model loading.

### Highlights
- `update.bat` updates an installation in place from the latest GitHub
  release: automatic backup, release notes with warning surfacing,
  a dependency refresh for the existing .venv, and rollback
  instructions. Downgrades are refused and the download is verified
  against the release tag.
- Images work end to end: an image plan decides per role whether the
  planner/coder run with a vision projector or receive the raw image,
  projectors resolve per model size, and uploads can optionally be kept
  in the workspace. Previews and chat thumbnails open in a zoom
  lightbox.
- Runs survive a page reload or a dropped connection: the UI reattaches
  and replays what it missed, and mid-run steering is wired in.
- Writes: looping output is caught early, truncations are diagnosed and
  salvaged instead of producing broken files.
- Approvals: an unanswered card auto-approves once after a configurable
  timeout instead of blocking the run forever.
- VRAM: a slot that was just killed no longer tricks the pre-flight
  gate into blocking the next load.
- Git: auto-commits and auto-push only run when credentials are
  configured in the Git panel, and the model installer ships a
  projector for every vision-capable model it offers.

### Also
- Code panel tabs with a remembered view mode; reloading a chat
  restores the panel state and tool-call chips.
- Per-instance llama ports, so two HiveMinds can run on one machine.
- New in the installer catalog: Sharp-MiniCPM5-2B. Duo compression
  defaults to Auto (Coder).
- Chat continuity: continuing a loaded chat feeds the model the saved
  conversation (last 40 turns, up to 8k characters each) instead of a
  small sidecar window; complex runs persist their history too; the
  per-chat workspace is stored with the chat and survives reloads; and
  conversation summaries reach the prompt instead of being filtered out.
- Developers: a pre-commit hook ships under `scripts/hooks` (see
  README for the one-time activation).

## [1.3.0] - 2026-10-05

The live-installation baseline. Everything below is IN:
image upload paths (collision-free, real formats), chat continuity
(transcript seeding, token-budget window, summary fix), the hardened
self-updater (verified end-to-end with a real release zip, including
preview-aware version ordering), rev compare-and-set with server-wins
409, fail-closed approval timeout, the edit shrink guard, planner/
thinking/checklist persistence (small variant, out of the model seed),
one-file-per-chat fork guard with rev-based adoption, a 200 MB save
brake, concurrent-writer coverage.

Added in the 2026-10-05 round:
- feature search palette (/ or Ctrl+K in the web UI): indexes every
  panel's sections, toggles, agent cards, settings fields (alias ctx ->
  context) and the seven tabs themselves (soul, tokens -> token budgets,
  chats...); results are mode-independent, a click jumps to the setting
  and reveals mode-hidden wrappers - the startup banner shows a TIP
  line for it
- chat list shows each chat's disk size (json + blobs) in the meta line
- steering with screenshots: /steer accepts images (max 4), injected as
  content parts at the next round boundary - the workaround for images
  falling out of the compressed window
- image path hardening: the direct-chat send gate uses the model
  registry (legacy name matching silently discarded images for every
  shipped vision model), GIF/WebP are re-encoded to JPEG in the browser,
  the flow card refreshes on mode changes
- qwen3.5 MTP models are vision-capable (projector pinned per model)

NOT built (deliberate, for a later release):
- message edit / regenerate controls in the chat UI do not exist yet
  (history edits are only possible via the chats API; the seed path
  handles them - verified - but there is no button for them)
- steering notes, status lines and approval/ask cards are not persisted
  (approval cards recover server-side only while the run lives)
- chat cleanup UI (the sizes are now displayed, but there is no button
  to shrink a chat yet)
- no live warning at the ctx slider when the set ctx cannot fit the
  VRAM budget (the guard only fires when a run starts; the observed
  failure class - 35B @ ctx 71680 with a pinned planner, 9B @ 40960 -
  is only caught there); /vram/estimate ignores pinned slots too, and
  the preflight waits ~18 s on VRAM that will never free instead of
  failing immediately with the concrete numbers
- an orphaned run (browser/tab died) keeps its VRAM until it finishes
  or /abort arrives; server-side continuation is proven, but UI
  completeness after reconnect (planner parts, thinking foldout) is
  still a manual browser test
- the per-round image trace is observe-only: when an image falls out
  of the compressed window (measured live: round 1 carries it, later
  rounds do not), nothing re-attaches it - the coder now gets an
  explicit "[VISION UPDATE]: image no longer in context, do not invent
  details" replacement note plus one visible status line instead
- session compression never runs in agentic chat (skipped by design)
  - the summary path is therefore live-untested; testable only with
  agentic_mode off
- whether the planner's visual details survive the compression summary
  is verified for the anchor (full plan re-injected every compression);
  what the summary does to surrounding context is untested

PARTIAL (known, accepted):
- blob versions are add-only until a chat is deleted (no per-chat GC)
- message html/content stay inline in the chat json (the bulk of large
  chats); images double-store (chat + .hive_uploads) when persistence
  is on
- restored tool chips stay silent when their path spelling differs from
  the panel key
- the judge-duo uncompressed re-read gate is dead code (unreachable
  threshold) - judge-routed duo runs keep the compressed window
- the history budget estimator under-counts short messages by ~1.5x
  (measured with the real /tokenize endpoint: 340 chars filler = 131
  real tokens vs 85 estimated); calibration open
- killing llama-server mid-run aborts cleanly (hard_stop /
  runtime_load_error, no loop_detected); what a SUCCESSFUL reload does
  to partial results is untested
- chat continuity: the chat json is re-seeded on every run (edits and
  regenerations included); sidecars written before that fix may contain
  foreign history and still serve as fallback for chats without json
- a future Telegram gateway sharing one server with an open UI would
  multiplex the (global) in-process session memory - needs a per-chat
  session store before that lands

Vision honesty (2026-10-04 review round):
- the server's real vision capability is read back from /props after
  every load and stored on the slot; when an image plan says "raw" but
  the loaded model runs without a projector, the run downgrades that
  role to the image description (or skips the image) with a visible
  warning instead of prompting a blind model to describe what it
  cannot see
- the coder's [VISION] prompt note is only added when the plan really
  routes the image to it, and is retracted when the post-load check
  says the projector dropped; the planner note distinguishes whether
  the coder will see the image too
- every model call that carries images logs images_in_request=N (and
  the planner POST logs it too), so "did the model see the image" is
  checkable in the log
- GIF/WebP uploads are re-encoded to JPEG in the browser (first frame)
  before upload - the formats the server-side decoder chokes on no
  longer reach the pipeline; a failed conversion is dropped with a
  visible notice
- release zips embed BUILD_INFO.txt (git describe + commit date) and
  the start banner prints the exact build a zip-installed copy runs
- repo syncs write the same BUILD_INFO.txt into the install, so a
  synced live/test copy can also say which commit it runs
- direct chat mode strips images (with a loud log line) when the
  loaded server reports vision:false instead of sending a request the
  server would reject
- image panel: the duo block carries its own multimodal info line (the
  generic one used to vanish permanently in duo mode); the chat hint
  names the planner first and follows planner/coder model changes live
  without re-attaching the image
- qwen3.5 MTP specs are vision-capable now (projector pinned per
  model); if a projector fails to load, the /props gate downgrades the
  run with a warning instead of pretending

### Telegram gateway (opt-in, new)

Control HiveMind from your phone: plain message in, the PC runs the
agent, status and result come back as Telegram messages. Off by
default — nothing starts, nothing is installed until you run it
yourself. Setup guide with a security walkthrough:
`docs/gateway_setup.md`.

- Pairing is anchored to the PC: a one-time code is printed on the
  gateway console (5 min, single use), `/pair` binds exactly one
  Telegram account, then pairing closes. Wrong tries count on the
  console, five lock it until restart. Nobody pairs from a distance.
- Text runs end to end: the message starts a run in a dedicated `[TG]`
  chat, the conversation is written back into the HiveMind transcript
  (both turns, so the UI shows it and the next run seeds from it),
  status edits update one message instead of streaming, long results
  arrive as a .txt document.
- Commands from the phone: /new, /stop (abort a running job), /status,
  /verbose, /mode (phone-side run mode via the /stream body — the
  browser UI keeps its own mode), /models + /setModel (numbered model
  pick, then one-line preset/ctx answers; ctx overrides ride the run
  body, preset load is global), /cancel, /help. One run at a time — if the browser already runs
  something, the phone gets a busy note instead of a queue.
- Failure is readable: model load blocked by VRAM, engine offline or a
  run error all produce a plain-language message instead of silence.
- Restart behavior: the gateway survives its own crashes without
  double-running anything (Telegram queue position saved before a run
  starts), recognizes an orphaned run at startup and asks the phone
  whether to abort it.
- Security posture: allowlist-only access, no webhook and no open port
  (outbound long-polling only), the bot token is read from the
  environment or the Windows Credential Manager and scrubbed from every
  log line, model/tool output is filtered for secrets before sending,
  a kill-switch file on the PC overrides everything the phone sends,
  and phone paths/usernames are collapsed before they reach Telegram.
- Approval policy for phone runs (/gate in the chat, select in the UI):
  deny = gated calls (shell/write/git) are auto-denied (default);
  ask = they pause the run and arrive as tappable cards on the phone
  (1 once / 2 always-this-chat / 3 deny); off = no gate force.
- Restrict phone runs (default ON): phone runs may only use web and
  read-only tools plus text — the single biggest safety switch.
- Mirror/takeover: a run started in the browser UI appears on the phone
  with tappable approval cards; phone texts steer the run.
- Supervisor: Start/Stop/status for the gateway process live in the UI
  card; one-time token setup via `start_gateway.bat setup` (Credential
  Manager), `start_gateway.bat stop` for detached instances.
- Model selection now also applies to simple/direct runs (P9 closed).

Not included in 1.3 (deliberate): photos and voice messages from the
phone, group chats, queues, token streaming, skills, cron. The bot's
phone-side texts are English.

## [1.2.3] - 2026-09-26

Hotfix: fresh installs crashed on startup before the server ever bound
its port.

### Startup
- The v1.2.2 duo_coder settings preset ships `temperature: null` (the
  coder temperature stays pipeline-controlled by design). On the first
  start, `apply_settings_to_pipeline` hit that null with `float()` during
  the server import: an uncaught `TypeError: float() argument must be a
  string or a real number, not 'NoneType'` killed the process right after
  the start banner printed its localhost link, and the console closed
  before any error could be read. The server never bound its port.
- Agent temperatures of `null` are now skipped instead of cast, keeping
  the pipeline-controlled semantics. Every other agent field is applied
  unchanged and no settings edit is needed: existing settings.json files
  with the shipped null start cleanly.

## [1.2.2] - 2026-09-20

Agentic coder pipeline repair: events, context, approvals, code panel.

### Coder core
- Coder events (tokens, tool chips, results) reach the browser reliably
  again: the SSE queue insert of the event bridge was dead code, runs
  worked server-side while the UI sat on an empty bubble.
- The coder's exec context follows the agentic context setting again
  instead of silently running truncated 8k prompts; empty instant-answer
  rounds from that are gone.
- Hermes Final sampling matches the model card again (seed 42, top_p
  disabled in the agent profile).
- Zero-activity guard: a text-only summary with zero reads and zero
  writes is no longer accepted as run completion. The model could echo
  the plan briefing for three rounds and finish as "completed" without
  touching a file. One targeted start-working nudge is sent first; if it
  keeps talking, the run aborts as a loop instead of faking completion.

### Approvals
- Approval and ask_user scope keyed on the persisted chat id while the
  UI polls with the registered run id. In saved chats the poll missed
  the pending entry, wiped the staged card, and the run waited forever
  behind a stale "Processing context" counter. Scope is run id first
  now; the card handler also clears the stale heartbeat line.

### UI
- Run journal: every SSE frame is journaled per run and replayable. A
  page reload or a silently dropped stream no longer detaches the UI
  from a running run. The frontend reattaches on load and tails the
  journal when the live stream ends without a done event.
- Prefill meter: the coder heartbeat shows real llama-server prefill
  progress (percent, tokens, tok/s), interpolated between the log's
  batch lines and hidden below 3k tokens. Long post-compression
  re-prefills stop looking like a hang.
- Code panel: `file_change` ships again for `replaced` edit results
  (the v1.2.1 result phrasing matched none of the tracker patterns, so
  the File view stayed empty for whole runs), the File view shows the
  streamed text while an edit generates, and every finished chip carries
  its diff id so a click pins that exact call instead of the first one.
- Code panel follows streams strictly on click: a newly streaming write
  updates its tab silently and never switches the view on its own —
  clicking the generating tool call (or its tab) follows the live text,
  opening the panel mid-stream jumps to it.

### Tools and models
- run_bash description warns off shell-written file contents on
  Windows: no heredocs, `>` redirects write UTF-16, backticks are
  PowerShell escapes. File content belongs to write_file/edit_file.
- min_p fix: a declared min_p 0.0 now ships in coder and critic
  payloads. The old guards dropped it, re-enabling the server's 0.05
  default that model cards explicitly warn against.
- The explore meter no longer logs a LOOP-DETECT warning on every
  healthy read round (it reported a streak of 0 at warning level).

## [1.2.1] - 2026-09-19

Pause lifecycle and tool metrics fixes from the first full Linux/Docker E2E pass.

- A resolved pause now leaves the pause registry (`wait_for_resume` cleans
  up after a successful resume, not only on timeout). Until now an
  answered ask_user left a dead entry in `_pause_events`/`_decision_ids`,
  so `/approval/decide` kept routing staged-card decisions into the dead
  pause: the pre-decision was silently lost and the gate paused a second
  time after generation finished.
- Decision endpoints (`/api/run/{id}/resume`, `/approval/decide/{id}`)
  route on a *waiting* pause only (`is_pause_waiting`) instead of bare
  registry membership, so stale entries can no longer swallow fresh
  decisions.
- The destructive gate publishes its pause like the approval gate does
  (`/approval/pending`, kind "ask"). Previously the gate paused without a
  card: the UI poll reported nothing, nobody could answer, and the run sat
  silent for the full pause timeout before declining. Button answers
  ("1", "approve", "once") now count as confirmation.
- Tool chips no longer show a line count next to the path. What a call
  changed lives in the result metrics below (added, removed, changed
  blocks), now including the call's estimated token size.
- Auto-split continuation calls no longer report the marker token's line
  count as if it were content.

## [1.2.0] - 2026-09-19

Action approval gate, websearch reliability, live code panel, Linux fixes.

### Action Approval
- Write file tool calls are reworked for efficiency on local models: a
  call generates exactly once. Approving during generation runs the write
  with zero extra decoding; denying aborts the stream mid-generation. The
  earlier hold design regenerated the entire call after the decision,
  doubling decode time on every approved write_file (a 3k-token write is
  over a minute of extra decoding at 40 tok/s) and a denied hold wasted
  the full generation. This makes large writes viable on small models,
  alongside the existing auto-split and truncation salvage paths.
- Opt in gate (`duo_action_approval_enabled`, toggle in the agentic settings,
  effective mid run): the coder pauses before run_bash, run_python,
  install_package, start_background, file writes and git_commit.
- Buttons: Approve once, Approve and remember (writes per file, commands per
  exact arguments), Deny, Input only, plus an optional note to the agent.
  Chat scoped memory, nothing survives a restart.
- The card appears the moment the tool name streams in and stays pinned at
  the chat bottom while the call generates. Approving during generation
  runs the call without a pause and without regenerating; deny aborts the
  generation immediately. A decision racing the pause setup is consumed
  instead of deadlocking the run.
- Card state survives page reloads (pending approval poll) and clears on
  decision, abort and run end. No silent bypass in until_finished runs;
  pauses and decisions are logged ([APPROVAL] lines).
- Decisions carry the pause's id (decision nonce): a stale tab or a late
  answer for an already resolved pause is dropped instead of silently
  overwriting the next question.
- `ask_user` goes through the same card: question, answer input, buttons for
  sending the answer or proceeding autonomously. In until_finished runs it
  now really pauses instead of silently throttling; the governor (max
  questions per 10 min, timeout auto-answer) stays as the runaway guard.
  A card toggle with a 30s timer auto answers questions while it is on;
  the toggle is a setting (`ask_user_auto_proceed`) and part of presets.

### Websearch
- SearXNG default engines `bing,duckduckgo` (google CAPTCHA suspended,
  wikipedia dead under most tags, github flooded); safesearch on; per
  hostname result cap.
- Budget exhaustion stops the run after 2 consecutive hits instead of
  burning rounds. No result answers carry a reformulation hint.

### UI
- Generated code streams into the right code panel (tab per file, plain
  render, throttled, drag resizable) instead of inline under the tool call.
- Sticky follow for chat and panel: pinned while you sit at the bottom,
  scrolling up pauses, returning resumes. Panel stream survives reloads.
- DIFFSTAT blocks start collapsed and show `@@ line N @@`.
- Tool gen streaming card keyed by tool call index, parallel writes no
  longer mix arguments.

### Fixes
- Error cap `duo_max_tool_errors` (12) decays on successful calls.
- run_python child IO forced to UTF 8, so check and cross marks no longer
  crash on cp1252.
- Browser: repeated navigate attempts to a blocked target escalate after
  the second try.
- start_hivemind.bat restart: stale PID no longer aborts a successful kill.
- run_state refs survive round re creation.
- edit diffs on repeat writes compare against the pre edit content
  (workspace snapshots re capture when the file changed between rounds).
- Tool chips report real line counts; a trailing newline no longer adds a
  phantom line. Pending approval and ask cards pin back to back at the
  chat end when both are pending.

### Linux
- Live VRAM via nvidia-smi (was Windows PDH only). The reclaim wait no
  longer spins its full timeout when no live source exists.
- `HIVEMIND_GPU_BACKEND` beats a `gpu_backend` key in settings.json.
- mlock warns once on Linux; Docker needs `--ulimit memlock=-1` because the
  default 64 MiB limit breaks the model load.
- Port cleanup no longer falls back to a name based pkill that would kill
  every llama-server on the host; the /proc scan stays port scoped.
- run_tests kills the whole test tree on POSIX (`start_new_session` plus
  `killpg`) instead of orphaning pytest and npm children.
- start_background commands run under /bin/bash on POSIX; plain `chmod +x`
  no longer trips the destructive gate there, recursive chmod still does.
- DLL and device probes are Windows only; Linux skips them, which removes
  the false `cudaGetDeviceCount=0` abort in CUDA containers.
- Desktop notifications are a silent no op off Windows.
- run.py error output no longer dies with EOFError without a TTY.

## [1.1.5] - 2026-09-17

UI polish and streaming fix.

### UI

- Reasoning blocks now render ```-fenced code as styled code blocks (was
  plain text) — formatting applies when the block closes.
- Fixed: whitespace-only stream chunks were dropped, gluing words together
  around numbers ("September2026", "um20 Uhr"). Chat history was always
  correct server-side; this was display only.
- vram_guard stop reason now shows as an error with retry buttons instead
  of a green "completed".

## [1.1.4] - 2026-09-13

Tools deep audit: 14 fixes across the tool loop, file ops and external
tools. Full findings ledger: docs/tools-audit.md.

### Verify chain (was effectively dead)

- `verify_last_ok_serial` was bumped by every tool type — the duo verify
  gate, critic veto and final hard-stop could never fire. Only a passing
  run_bash bumps now.
- Verification spoofing closed: echoing `[TEST-RESULT] ✅`/`pytest` into
  run_bash output no longer satisfies the auto-test gate (only real run_tests
  results or test-command evidence count); the exit-code scan takes the LAST
  `[exit code: N]` match; a failing run_tests (❌) counts as a failure.

### Correctness

- Four hint-escalation branches replaced the tool result without appending
  the tool message → dangling tool_call in history (API 400 risk).
- CRLF files are no longer silently rewritten as LF by edit_file /
  patch_file / replace_lines / edit_ast (universal-newline reads fixed).
- write_file_append: a >250k-char remainder was silently truncated with a
  success message; empty content destroyed a pending auto-split remainder.
  Both are honest errors now.
- run_python: non-empty output no longer swallows a non-zero exit (same
  `[exit code: N]` marker as run_bash).
- task_complete "blocked" detection actually matches the handler output.

### Safety

- Browser tool: loopback/private IPs and "localhost" are no longer navigable
  (llama-server, SearXNG, the UI were reachable) — only the tool's own file
  server origin.
- critic_verify now runs with the workspace lock (was unconstrained).
- install_package pip requires a workspace venv (can no longer install into
  HiveMind's own venv).
- test_runner timeout kills the whole process tree (taskkill /T), not just
  the shell; post-eviction read-guard SKIP trap removed; patch_file-branded
  hint no longer fires on unrelated tool failures.

## [1.1.3] - 2026-09-13

Linux native-lib check fix.

### Fixes

- The installer hung forever in "DLL check pending (ggml-vulkan.so)" on
  Linux: the check globbed for a FLAT ggml-vulkan.so next to the binary,
  but the ubuntu tarballs ship lib/libggml-vulkan.so (lib prefix + lib/
  subdir). Both the installer check and the backend runtime probe now
  accept both layouts (rglob); Windows .dll behavior unchanged. Backend
  error message names the correct file per platform.

## [1.1.2] - 2026-09-13

Tree-scout workspace fix.

### Fixes

- The repo map could be built from the WRONG folder: a Windows path inside
  the task text (e.g. copied from another machine) won over the resolved
  UI workspace, and non-existent paths were accepted without an existence
  check. The resolved workspace now always wins when valid; text-extracted
  paths are only a fallback for workspace-less chats and must exist.
  Ignored text paths are logged.

## [1.1.1] - 2026-09-11

Agentic-run fixes, run-state persistence and installer model-setup rework.

### Fixes — agentic runs

- Coder load with a context mismatch (planner slot at a smaller ctx than the
  agentic coder target, e.g. 10240 vs 32768) now reloads the model instead of
  instantly stopping with `loop_detected` after 0 tool rounds.
- Runs without planner and without chunking no longer hit an
  `UnboundLocalError` (`_planner_model`) in the post-explore worker evict.
- The smoke-test nudge (`[AUTO-TEST] No test suite`) fired every tool round
  instead of once per run; TC-DE-NAG and the READ-LADDER counters suffered
  the same per-round reset — all run-state is run-persistent now.

### Model setup

- Curated 7-model download catalog, every entry pinned to an exact Hugging
  Face repo (no fuzzy search — it could pull wrong lookalike repos):
  gemma-4 E4B/E2B it-QAT (UD-Q4_K_XL, mmproj, MTP drafter),
  qwen3.6:35b-a3b-ud, Hermes3.6 Genesis V13 MTP-APEX-Compact,
  qwen3.5:4b-mtp, qwen3.5:2b-mtp, lfm2.5:2.6b (+DSpark drafter).
- A custom models folder (e.g. another drive) is asked for ONCE and kept:
  one pass downloads missing models AND registers the whole folder (the
  installer previously asked twice and could not be used with an own
  folder at all).
- setup_models.bat renders its menu from the catalog (single source of
  truth).
- The desktop-shortcut step now runs BEFORE the long download steps, so a
  first install gets its icon even if the user aborts during the model
  downloads.

### Docs

- architecture.md mermaid fix + freshness pass; README download-catalog
  section rewritten.

### Launcher, settings, thinking toggle (2026-09-10)

- Windows launcher: the console-kill job object is created with
  `CreateJobObject()` (the previous `CreateJob()` call does not exist in
  pywin32 and crashed the launcher on startup).
- `POST /settings` no longer returns 500 for request bodies without an
  `agents` block (`UnboundLocalError` on the model-sync role list).

### Thinking toggle (planner vs. tool rounds)

- The `--reasoning on/off` server flag is now scoped per model: the duo
  tool-thinking override previously landed on whichever model loaded first
  (typically the planner) and left the coder server at template default —
  so tool rounds thought even with the toggle off, and the planner's
  thinking was suppressed despite being enabled. Now the planner thinks
  and tool rounds don't, exactly as configured.

### Model choice & recommendations (user over policy)

- Explicit model choices from the UI are recorded persistently
  (`agents_user_model_choice`) and always win: the safe-profile policy
  matrix no longer reverts a card to its own model — including when the
  user re-picks the shipped default (the old CARDS-WIN heuristic could not
  express that and silently swapped e.g. duo_coder back to lfm2.5:2.6b).
- Explicit card temperatures are recorded too (`agents_user_sampling_choice`)
  and beat both the policy matrix and the model recommendation.
- On a pure model change the card adopts the model's recommended
  temperature from `model_configs/models/*.json` — the model itself, ctx
  and thinking are never touched by recommendations.
- The safe-profile matrix no longer pushes fixed ctx defaults
  (`duo_coder_ctx_agentic/normal`) or `thinking`/`thinking_budget` onto
  agent cards; ctx is a per-model/per-user decision only.
- New regression suite `tests/test_safe_profile_choice.py` (12 checks)
  registered in `tests/run_regressions.py`.

### Models & defaults

- `qwen3.5:4b-mtp` is the default coder/planner/subagent model (MTP
  speculative decoding) and was added to the model downloader.

### Tool loop & stability

- `patch_file` removed from the model-facing tool list (`edit_file`
  fuzzy-matches SEARCH blocks internally).
- Final-summary accept, escalate-now for <10% partial compressions, adaptive
  no-think retries, per-chunk tool budgets, planner plan re-injection.
- Coder VRAM fallback slot runs at >=20k context; an incompressible round-0
  payload runs with a minimal budget instead of tripping the 3-strike stop.
- Grace round actually runs now; a failing chunk ends itself instead of
  halting the run.
- Browser tool serves workspace `file://` URLs over a loopback HTTP server
  (ES modules/fetch work; everything else stays rejected).

### Launcher & installer

- `start_hivemind.bat` can kill + restart an already-running instance.
- Windows: the whole process tree (python + llama-servers) is bound to a
  kill-on-close job object — closing the console ends the stack.
- Installer: `uv sync` falls back to plain venv + `requirements.txt`,
  parenthesized install paths no longer break the dependency step.

### Language & cleanup

- English-only across MCP tool descriptions, JSON-RPC errors, user-visible
  strings and LLM-facing scaffolding; memory extraction understands English
  phrasing (data keys unchanged).
- Dead settings removed; `hivemind.service` ExecStart points at `.venv`.


## [1.1.0] - 2026-09-09

Context flow and run stability release: compression, planner→coder handoff
and the agentic tool loop got a round of fixes, plus new model support and
installer hardening.

### Context flow & stability

- Compression works now where it silently did nothing before: the cut index
  no longer counts the pinned system prompt, no-ops are logged loudly and
  escalate instead of tripping the fail guard invisibly.
- `duo_partial_compression` is on by default: summary + raw tail keeps the
  llama.cpp prefix cache alive and the raw tail readable for the coder.
- Grace tool rounds, per-chunk budget resets and chunk containment in the
  agentic loop: a chunk that runs out of budget no longer eats the loop's
  detection logic or leaks rounds into the next chunk.
- Deadline grace: a successful write/edit extends the run deadline (+300s),
  so long productive rounds on slow backends are no longer killed mid-task
  while loops still abort on schedule.
- Planner deadline abort is clean: cancelling the planner task at the run
  deadline no longer produces an "InvalidStateError: Result is not set"
  traceback, and falls back to best-effort coding as intended.
- Read ladder path-reset: only repeated reads of the SAME file escalate the
  "exploring but not implementing" hint — reading different files resets the
  counter instead of falsely flagging normal multi-file exploration.
- Real token numbers in the UI context meter (estimated vs. real, per-round
  cache-reuse display).
- Stale-tab protection: settings posts carry a revision; outdated tabs get
  rejected on protected compression keys instead of silently overwriting.
- Default duo coder budget lowered to 8000 max tokens (was 12000): a single
  huge completion round could eat half the post-compression headroom on
  32-60k contexts; big files are written in multiple appends instead.

### Tool calling

- ARG-COMPACT stub now nudges read-before-write; a new STUB-ECHO-GUARD
  rejects write/edit calls that try to write the internal stub back into a
  file (observed with small models).
- Memory/forget keyword routing requires short command-like messages, so a
  long task spec containing "store"/"note"/"delete" is no longer swallowed
  by the memory early-return.

### Models

- Spark-X2.5 (4B/1.7B) profiles and per-model configs, verified live on
  llama.cpp b10872+ (thinking + tool calling, ~60 tok/s on Vulkan).
- Ling-3.0-tiny sampling profile per model card.

### UI

- Three compression toggles surfaced (cache-friendly / partial / local-only);
  the absolute "Compression Limit" field was removed — the auto floor (70% of
  ctx) is the baseline, `duo_compress_threshold` stays a settings.json
  override.
- Preset loading is reliable (UTF-8 BOM tolerant) and reports success/failure
  instead of failing silently.

### Installer

- uv-based install on Windows AND Linux (`uv python install 3.14` +
  `uv sync --all-extras`; requirements.txt stays as legacy fallback), with a
  fresh machine needing no preinstalled Python at all.
- Paste-safe interactive answers (pasted multi-line replies no longer crash
  the dependency step with a syntax error).
- `clean_release.bat` strips `.venv` and verifies installer inputs before
  packaging (a zip without `pyproject.toml` used to crash on first run).
- English-only keyword matchers and system-facing strings throughout;
  `AGENTS.md` documents repo conventions.

## [1.0.13] - 2026-09-08

### Added

- `duo_compress_local_only` (default off): skips the compression LLM summary call
  entirely and builds the local fallback summary instead. For setups where that call
  always runs into the read timeout (MoE with CPU experts, slow prefill). Also skips
  the light compressor load and the mini-shrink retry. Combines with
  `duo_partial_compression`.
- Linux/POSIX support for the llama.cpp backend: `gpu_backend` accepts `cpu` (env
  `HIVEMIND_GPU_BACKEND`), platform-aware binary discovery, RAM via /proc/meminfo,
  port-kill chain fuser -> /proc inode scan -> pkill, `.so` probe on POSIX.
- CPU backend load path: `--n-gpu-layers 0`, no `--device`, VRAM pre-flight
  short-circuited, CPU-count thread defaults on POSIX (Windows keeps 16/8).
- `deploy/fetch_llamacpp.py` downloads linux assets (ubuntu vulkan/cpu/rocm, zip and
  tar.gz, chmod +x); rocm regex accepts versioned names (`rocm-10.0-x64`).
- POSIX tool-command ladder (`hive_functions/language_config.py`): `python` -> `python3`,
  PowerShell pipe cmdlets -> `head`/`tail`.
- `deploy/install_linux.sh` (system deps, venv, llama.cpp fetch, systemd) +
  `deploy/hivemind.service`; README linux sections.
- `tests/test_linux_paths.py`.
- Model config `hermes3.6_35b-a3b-uncensored-genesis-v13-mtp-apex-compact.json`
  (mirror of the v12 config, `mtp: true`).
- `llama_ubatch_size` setting (default 256), wired through to `--ubatch-size`. Larger
  values (512/1024) speed up prefill for MoE models with CPU expert offload at the
  cost of a bigger compute buffer.

### Fixed

- The installer never accepted downloaded DLLs (release blocker):
  `_verify_backend_dlls` checked `ggml-vulkan` without the `.dll` extension on Windows,
  so every vulkan/cuda install failed at step 4 and deleted the intact build. Now:
  correct extension via directory enumeration (survives antivirus scanning fresh
  files), ~90s retry window, archive cross-check (a quarantined DLL keeps the build
  and prints restore instructions instead of deleting), and no more HTTP 416 when the
  temp file is already fully downloaded.
- The documented per-model `sampling` block in `model_configs/models/*.json` is now
  applied at runtime (`models_registry.get_sampling`, fallback in
  `core/model_sampling.py`). It previously had no effect; `_model_sampling_overrides`
  still wins when set.
- Documentation audit against the code: README no longer claims `Auto` is the default
  mode, that AutoMap is judge-driven, that the custom wizard collects `sampling`, that
  `GITHUB_TOKEN` or `SEARXNG_SETTINGS_PATH` exist, or a `--no-vulkan` flag. Tool table
  completed, tool-tier values documented.
- `direct_tools_tier` accepts the `websearch` alias (a literal `websearch` value used
  to disable all tools).
- `docs/settings.md` regenerated (197 keys, was stale at 189).
- Four reachable NameErrors (found by the ruff gate): missing `import re` in
  `core/duo/_pre_explore.py`, undefined `RE_THINK_CLEANUP` in `core/duo_helpers.py`,
  missing `save_learned_config` import in `learning/peer_ratings.py`, and
  `llm_read_timeout` never passed to the pre-explore workers.
- Lint guardrails: `ruff.toml` (E9, F821, F601, F811, F841, W605) enforced by
  `tests/test_lint.py`, plus `tests/test_no_new_silent_excepts.py` with a frozen AST
  baseline that fails on new silent except handlers.
- User-visible strings (SSE status, model-load errors, RuntimeErrors) translated to
  english across core/, backend/, hive_functions/, tools/ and app.js. Functional
  german intent keywords stay on purpose.
- `duo_compress_auto_floor` default 0.78 -> 0.70: the request overflow zone starts at
  ~72-73% of ctx (prompt + clamped max_tokens + template overhead), so the old floor
  forced `trigger=force` compressions through it, each costing a full cache-losing
  re-prefill.
- Silent CPU fallback no longer applies to coder-class models: the VRAM threshold for
  the one-time `--n-gpu-layers 0` retry dropped from 5.0 to 3.0 GB. The coder sat just
  under 5 GB and could reload fully on CPU (~1-2 tok/s) instead of failing loudly.

### Removed

- Dead setting `automap_mode` (defined, documented, never read).

### Tests

- Registered the unregistered suites `auto_split_pending`, `ctx_guard`,
  `planner_model_persist`, `read_only_detect`, `tool_arg_compact` plus the new
  `lint` and `no_new_silent_excepts`; sampling-override coverage in
  `test_models_registry.py`.

## [1.0.12] - 2026-09-06

### Added

- Token estimator calibrated to 3.0 chars/token (`utils/token.py CHARS_PER_TOKEN`),
  shared by ctx guard, UI meters and compression notices; the UI shows integers now.
- `clamp_request_max_tokens` (`context/ctx_guard.py`): each tool round clamps
  `max_tokens` so prompt + output fits the ctx. The compression threshold is
  floor-driven again (`duo_compress_auto_floor`) instead of binding to the full output
  reserve.
- `duo_compress_model` (default `lfm2.5:2.6b`): compressions run on a small model when
  it fits without evicting the coder, otherwise on the coder. Read timeout configurable
  via `duo_compress_llm_timeout_s` (default 180).
- `ling-3.0-tiny` as recommended low-resource coder (registry config, downloader entry,
  setup menu, README).

### Changed

- Recommended coder/hermes model switched to `qwen3.6:...-genesis-final-apex-compact`
  (~17 GB, MTP, 35 CPU experts) — replaces hermes3.6 v12 in the downloader, setup menu,
  README and per-model config.
- New default `duo_compress_auto_floor = 0.78`.

### Fixed

- Compression notices show integer estimates instead of raw float values.

## [1.0.11] - 2026-09-04

### Added

- Cache-friendly context management for long runs (`context/ctx_guard.py`,
  `core/duo_runner.py`): the llama.cpp prefix cache stays alive between compressions.
  Auto threshold is `min(auto_floor*ctx, ctx - reserve)`; in-place recall-marker
  eviction only as emergency (>90%, no compression left). New settings:
  `duo_cache_friendly_ctx`, `duo_partial_compression`, `duo_compress_auto_floor`,
  `duo_compress_overflow_reserve`, `duo_min_free_ctx_tokens`, `duo_max_compressions`.
- `duo_partial_compression` (default off): only the older part of the history is
  condensed, the recent tail stays byte-identical so KV-shift reuse can salvage the
  suffix after a rebuild.
- `[CACHE] prompt/cached/reuse%` telemetry per coder round plus compression and
  eviction counters.
- `deploy/analyze_cache_log.py` (A/B analyzer over hivemind.log) and
  `deploy/ab_phase1.ps1` (live A/B toggle + log snapshot).

### Changed

- Compression is the primary shrink, in-place eviction the exception. The tool history
  is append-only between compressions, so `[CACHE] reuse` stays ~100%.
- Rule-based compression fallback: if the LLM summary fails validation, a deterministic
  pass replaces old tool outputs with recall markers instead of keeping full context.
- AUTO-SPLIT continuation marker detection is robust: quotes/backticks, short replies,
  literal marker lines at the file end get healed, pending keys canonicalised.
- Presets save and load the coder context deterministically (overlay body + UI flush
  before save, no more race with the debounced settings POST).
- `models_dir` is never stored into or restored from presets; the safe-profile matrix
  no longer overwrites explicitly set `duo_coder_ctx_*`.
- Compression HTTP timeouts raised (connect 10s, read 120s).
- UI: compression label/help in english, app.js cache-busted.
- Context compaction of executed write calls (`core/tool_executor.py`): after a
  successful oversized write, the unsent tool-call arguments are replaced with a stub
  (path + arg chars + sha1 prefix). The prefix cache stays alive and ~20-30k tokens no
  longer sit in the context until the next compression.

### Fixed

- Oversized `write_file`/`write_file_append` with a quoted AUTO-SPLIT marker no longer
  truncates files by writing the marker as literal content.
- Preset coder context no longer resets to the safe-profile default after reload.
- Long runs no longer degrade into per-round full re-prefills in cache-friendly mode.
- Post-run insight extraction no longer stalls up to 240s loading a small model while
  the evicted big model still holds RAM; skipped entirely when no model is loaded.
- `read_file` aborts oversized full reads early (binary sniff + streaming newline
  count) instead of reading the whole file just to answer FILE_TOO_LARGE.
- Transport-level stream drops show a neutral "run stopped / possibly parked" notice
  instead of a red model error.
- Skip during the planner aborts the planner and starts the coder (previously a long
  planner finished and the run ended with zero coder output). Skip on a single
  non-chunk round is ignored.

## [1.0.10] - 2026-09-04

### Changed

- Chat template updated to qwen3.8-froggeric v22.5 (`model_configs/chat_template22.5.jinja`,
  versioned filename so you can tell the template version at a glance). 22.4 removed;
  the qwen3.5/3.6 and hermes3.6 configs plus the code fallback point at it.

## [1.0.9] - 2026-09-03

### Added

- Server-side AUTO-SPLIT for oversized writes (`tools/handlers/file_ops.py`): when a
  write exceeds the per-call char limit, the leading chunk is written immediately and
  the remainder is cached server-side. The model finishes with one tiny
  `write_file_append(path, content="<AUTO_SPLIT_CONTINUE>")` call instead of
  regenerating everything. Pending entries expire after 5 minutes.
- The last explicitly loaded preset is applied again on startup.
- Read-only detection understands content constraints (`core/duo_helpers.py`):
  `is_read_only_request()` needs a read-only phrase AND no non-negated implementation
  intent, so "DO NOT MODIFY" inside a build task no longer disables the tool round.

### Changed

- Coder WRITE RULES + the dynamic char cap are injected into the system prompt; tool
  schemas advertise `maxLength: 20000` and the AUTO-SPLIT marker; `write_file_append`
  appends received content in one go.

### Removed

- Tiel-Coder model recommendations and their default configs (downloader, setup menu,
  README table, per-model configs, launch defaults). History keeps the entries.

### Fixed

- Context compression crash: the empty-history early return handed back a 2-tuple
  where the caller unpacks 3, mis-stamped as loop_detected and hard-stopped healthy
  runs.
- The planner model was wiped to None on every settings reload, silently falling back
  to the coder model. Stored value persists now.

## [1.0.7] - 2026-09-02

### Added

- Tool-loop bash reinjection: repeated `run_bash` loops (3x identical, ABAB, ABCABC)
  no longer abort the run. The available toolset is re-injected as a menu message and
  the loop signature resets so the model can pick a different tool. Non-bash loops
  keep the abort; the round budget still bounds everything.
- Tiel-Coder 35B-A3B MTP models (Compact ~18 GB, APEX ~26 GB) as recommended downloads
  with per-model configs: `--jinja` + embedded template, full offload, temp 1.0 /
  top_p 0.95 / top_k 20 (0.6 agentic), vision via the shared mmproj.

### Fixed

- Safe-profile policy no longer overwrites the user's saved `vram_budget_gb` on every
  reload (user-preference key, policy value is informational).

## [1.0.6] - 2026-09-02

### Changed

- MoE CPU experts dropdown lists only installed models; `-ud` variants stored/read via
  the backend-compatible key so overrides actually apply.
- Hermes3.6 Genesis V12 MTP-APEX-Compact is a recommended downloader model again
  (+mmproj, per-model config ships with the release).
- Optional desktop shortcut (`create_shortcut.bat`, installer step 7/7), custom name
  via argument.

## [1.0.5] - 2026-09-02

### Added

- Presets are back: save/load/delete named configurations incl. per-agent prompts.
  Load replaces the whole configuration, only on an explicit click. No auto-load at
  startup, no preset override in the agentic coder, `git_token` never stored.

### Changed

- Agentic/duo context is user-controlled: the per-agent Context setting is honoured in
  the duo/agentic paths, the 16k agentic floor is a minimum only, slider goes to 128k.
- `pip` installs use `python -m pip` so a stale pip.exe cannot fail silently.
- Tool-thinking off by default (`duo_coder_tool_thinking_auto_mode` = off).

### Fixed

- Duo/agentic context honours a user-set override even when a persisted duo_* default
  exists.
- The llama.cpp updater fetches the newest nightly straight from the releases list
  (the old nightly-tag.txt pointer lagged ~100 builds); transient failures print a
  clear message instead of a traceback.
- Duo context inputs persist while typing; the effective context is logged
  (`[CTX-EFFECTIVE]`).
- The context chosen in the frontend is sent with the /stream request and applied to
  that run, so display and reality can't drift apart.
- `NameError: workspace_lock` in `_inject_tool_error_hints` crashed every tool round
  that hit an `EDIT_FILE_NO_BLOCKS_APPLIED` hint.
- MoE CPU experts dropdown shows the full model key (no ambiguous duplicates).
- CUDA nightlies (b10760+) split the runtime into
  `cudart-llama-bin-win-cuda-<ver>-x64.zip`; the updater downloads and places it next
  to llama-server.exe.

## [1.0.4] - 2026-09-02

### Added

- Direct-chat tool tiers redesigned: off -> Websearch (`web_search`/`web_fetch` only)
  -> read + python -> full.
- `web_fetch` sends a real browser User-Agent to reduce 403s; strict sites may still
  block, prefer `web_search` there.
- Semantic LRU reclaims more context: stale `read_file` outputs are evicted after a
  successful write to the same path, repeated full reads dedupe to the newest copy,
  path-less outputs age out via half-rate TTL decay (errors keep double TTL).

### Fixed

- run.py missing-file guard blocked fresh installs after the pre_explore module became
  a package ("Missing files: hive_functions/pre_explore.py").
- Agentic coder crashed on every code_duo run: the extracted pre-explore phases lost
  several locals, now read from state.
- 8 GB setup works out of the box: direct/duo_coder default to `lfm2.5:2.6b`,
  `default_8gb_v1` no longer forces the 9B model, agent-card choice always wins.
- VRAM pre-flight suggests fitting models instead of a generic hint.
- llama.cpp download is resumable + CRC-verified; installer survives parenthesized
  install folders.

## [1.0.3] - 2026-09-01

### Fixed

- Installer crashed silently when the install folder contained parentheses (cmd
  for-block parsing); the recursive checks now run via PowerShell with the path from
  an env var.
- SearXNG stale-container bind-mount error survived `docker rm`; the compose fallback
  runs `docker compose down --remove-orphans` before retrying.
- The installer window could close without a message: steps are logged to
  install.log, downloads are wrapped in try/except, all installer scripts have a
  top-level crash guard.
- Safe-profile policy reset `vram_budget_gb` 8.0 -> 7.5 on every start; the policy
  value is a safety ceiling now, an explicitly set higher budget wins.
- PRE-FLIGHT VRAM block on high context: before failing, the loader auto-downgrades
  the context (16384 -> 8192 -> 4096) and rewrites --ctx-size.
- The updater failed on freshly-published nightlies; it now only picks a build that
  already exposes a matching asset.
- Agent-card temperature was overwritten by the model sampling profile; an explicit
  agent temperature wins now.
- "Run completed" divider is a subtle line in simple chat, full marker elsewhere.
- Agent-card context slider cap raised 32k -> 65536.

### Changed

- Hermes3.6 V12 compact config aligned to the qwen3.5:9b-ud launch parameters
  (num_ctx 4096, duo_coder 8192) so the 35B MoE runs on 8 GB.
- Sampling profiles aligned to the official Qwen model cards.

## [1.0.2] - 2026-09-01

### Added

- `tests/test_release_integrity.py`: missing-file check accepts the pre_explore
  package layout, searxng engine lists are consistent across settings/websearch/yml,
  the SearXNG config is valid YAML with a secret placeholder.

### Fixed

- "models folder missing" note suppressed when `models_dir` is configured.
- VRAM budget display syncs with the server response after save.
- MoE dropdown deduplicated by base name.
- SearXNG install on a stale container: `--force-recreate`, remove + retry on failure.
- run.py guard accepts `hive_functions/pre_explore/` as a package.

### Changed

- Default engine list: brave, bing, github, wikipedia, mojeek, stackoverflow, pypi
  (google removed for captcha/rate-limit reasons, mojeek added).
- Websearch toggle checks status immediately; host input read live.
- Preload defaults off (`startup_preload_enabled`, `startup_preload_judge_in_agentic`,
  `smart_preload_enabled`, `judge_keepalive_enabled`).
- Configs tab regrouped into Model Preload & VRAM / Learning & Model Config / System;
  the duplicated git toggle in the Agents tab is gone.
- "Use Presets" panel removed from the agentic coder; `duo_runtime_profile` stays
  "balanced" as the backend default.
- Composer tool-status line removed; the header Chat-Tools badge is the indicator.

## [1.0.1] - 2026-09-01

### Added

- `deploy/backup_data.py`: backs up sessions/, memory.json, soul.json, presets.json,
  settings.json, learned configs, learning_logs to backups/<timestamp>/.
- LICENSE (BUSL 1.1) and this changelog; repo prepared for public release.
- Sandbox hardening: the Windows Job Object also caps job memory and process count
  (`duo_tool_sandbox_max_mem_mb`, `duo_tool_sandbox_max_procs`); POSIX subprocesses
  run in their own process group and die via killpg.
- Hermes3.6 Genesis V12 MTP-APEX-Compact in the downloader + per-model config; strict
  producer sampling (temperature + top_k only, everything else disabled).
- searxng_repair.bat: auto-starts Docker, recreates the container, verifies /healthz,
  rebuilds on a stale baked-in config.
- SearXNG settings installed to /usr/local/searxng/settings-hivemind.yml and selected
  via SEARXNG_SETTINGS_PATH — the /etc/searxng volume shadowed the baked config and
  caused HTTP 500 / KeyError: default_doi_resolver.
- Per-model sampling stats: `sampling` block in model_configs/models/*.json keyed by
  the runtime mode keys, highest priority over the built-in family profiles,
  collected by the custom-model wizard.
- `seed` support in the OpenAI payload builders.
- `GET /health`, requirements-dev.txt, generated docs/settings.md, 8 new regression
  suites (17 total).

### Changed

- Memory and registry writes are atomic now, persistence errors logged not swallowed.
- Dead env var `HIVEMIND_AUTO_RESUME` removed.
- server<->state consolidation: 7 duplicated helpers removed, canonical versions live
  in core/state.py; backend config sync lives in `server._sync_backend_runtime_config`.
- Pre-explore fallback defaults aligned with settings.py DEFAULT_SETTINGS.
- 7 previously hidden settings keys are explicit in DEFAULT_SETTINGS.

### Removed

- ~40 dead imports from server.py and core/duo_runner.py (AST-verified unused).

### Fixed

- parse_tool_error leaked the [TOOL_ERROR_META] block in format-B messages.
- UTF-8 BOM removed from 5 files (breaks AST tooling).
- Mojibake fix in the 3 functional regex lines of server.py (double-encoded UTF-8) —
  german greetings/questions match again.
- `_model_profile` consolidated into core/model_sampling (incl. user-config merge).
- README staleness (fallback model 4b-ud, planner thinking budget 8000).

### Refactored

- server.py 3000 -> 1573 lines in stages: tools/handlers package, pre_explore package,
  chat_util/security/log_noise extractions, run_stream -> core/chat_run.py.
- backend/llama_server_manager.py (2406 lines) split into llama_manager_utils,
  llama_slots and 5 mixins (load/evict/process/prefetch/health).
- execute_tool_round: helpers moved to core/tool_exec_helpers.py, mutable slots boxed
  into a ToolRoundState dataclass (32 -> 16 parameters).
- pre_explore sub-phases extracted as functions.
- Fixed along the way: git_commit referenced workspace_lock instead of _workspace_lock
  and crashed with a NameError whenever git was available.
