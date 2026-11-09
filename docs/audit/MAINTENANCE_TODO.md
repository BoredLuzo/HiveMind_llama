# TODO — Maintainability, Release Hardening, Competitiveness

Live maintenance/release/competitiveness backlog. Translated from the German backlog on 2026-11-08; this is the maintained copy.

**As of:** 2026-11-08 · Branch `dev` @ `8f12598`
**Legend:** P0 = blocks release · P1 = next up · P2 = planned · P3 = strategic
**Type:** BUG / DESIGN / DEBT / TEST / RELEASE / COMPETE

## A. Design Gaps from the Live Test (all open)

- [ ] **A1 · P1 · DESIGN (deferred: direct feature later) — Pause on direct runs is ineffective.**
  `/pause/{run_id}` sets `request_pause_after_chunk`; the flag is only checked in the
  Duo chunk loop (`core/duo_runner.py:2957`). A direct stream does not pause.

  *Acceptance:* the direct runner checks at a safe token/round boundary and halts;
  `/pause-state/{chat_id}` becomes active; resume continues. Alternatively, label the
  UI honestly ("pause only for Duo/chunk runs") and have the endpoint return 409.

  *Evidence:* live matrix 2026-11-07, `/pause` → `pause_requested`, then
  `/pause-state` `{"active":false}`, `/resume` 409.

- [ ] **A2 · P1 · DESIGN (deferred: direct feature later) — Steer injection only at tool/chunk boundaries.**
  The queue (`/api/run/{run_id}/steer`) works; a pure direct run without tools does not
  inject the text mid-response, only after run end (frontend queue).

  *Acceptance:* a defined direct boundary (e.g. every N tokens/round) OR the UI clearly
  says "applied after the run". Test for queue → injection → model response.

  *Evidence:* `core/tool_loop.py:553`, `core/duo_runner.py:3604`; the direct runner has
  no drain.

- [x] **A3 · P1 · DESIGN/QUALITY — Auto + Agentic hijacks simple tool prompts.**
  With `duo_agentic_mode=true`, "List files" goes into the Code-Duo path and ends in
  `loop_detected` after list/loop repetitions instead of in the Tool Agent.

  *Acceptance:* tool detection before the Agentic hijack; simple list/read prompts land
  in the Direct-Tools/Tool-Agent path, Code-Duo stays for real coding tasks.

  *Evidence:* live matrix, 745 events, `[LOOP-DETECT-STOP] explore_only=3`.

  *Done 2026-11-07:* `is_simple_tool_prompt` (routing/agent_intent.py) detects short
  read/list prompts and vetoes coding verbs; `_chat_route_decision` applies it before
  the persisted Agentic hijack and before the Judge `duo` override. Explicit
  `agentic_force` and explicit modes still win. Tests:
  `tests/test_simple_tool_routing.py` (20 checks, incl. coding counter-test).

- [x] **A4 · P1 · DEBT/QUALITY — Legacy tool route only reaches max rounds.**
  After the `ResponseNotRead` fix (`c5f686e`) the route runs, but the 2b agent repeats
  tool calls and ends with `max_tool_rounds` instead of an answer.

  *Acceptance:* round-limit/prompt parity with the Direct-Tools path; a simple read task
  ends with `completed` and a text answer. Clean up duplicate logic against
  `tool_loop.py` (C7 note from the report).

  *Evidence:* live retest 2026-11-07, `done stop_reason=max_tool_rounds`.

  *Done 2026-11-07:* `core/chat_tools_route.py` now uses the shared tool advertisement
  of the direct path (`_direct_tools_note`) plus an explicit stop rule;
  `direct_tools_max_rounds` controls both paths (1..300), and a `_repeat_guard` nudges
  an identical repeated call exactly once. Integration test
  `tests/test_tool_route_repeat_guard.py`: without the guard the fake loop ends in
  `max_tool_rounds`, with the guard in `completed` + text answer;
  `tests/test_chat_tools_route_guards.py` covers prompt/signatures/limit.

- [x] **A5 · P2 · DESIGN — Double auto-answer with `ask_user_auto_proceed`.**
  Card timer (JS, 30 s) and engine timeout (30 s) can answer simultaneously. Currently
  harmless (a late `set_user_answer` is a no-op/409), but two sources.

  *Done 2026-11-08:* the card timer was removed; the checkbox toggle only persists
  `ask_user_auto_proceed`, the engine is the single auto-answer source
  (`engine 30s` in the label). Guards in `tests/test_ask_user_auto_proceed.py`.

- [x] **A6 · P2 · TEST — UI wiring of `ask_user_auto_proceed` only verified via API.**
  The checkbox below the action approval gate was never clicked through in the browser.

  *Done 2026-11-08:* real Playwright test against the running engine
  (`toggle flips` → `/settings` follows → reload keeps the value → original restored).
  Opt-in script `tests/browser/test_ask_user_toggle.py` (not in CI, needs engine + browser).

- [x] **A7 · P3 · DESIGN — `coverage_setup` fails under coverage measurement.**
  The check "coverage not in sys.modules" is false by design in the measured process.

  *Done 2026-11-07:* when `COVERAGE_PROCESS_START` is set, exactly this check is counted
  as SKIP; outside measurement it stays enforced.

## B. Maintainability (open)

- [x] **B1 · P1 · DEBT — C4/C5 extraction: measured and rejected.**
  C4 (269 lines) touches **60+ loop-state variables**: besides the 32 runner inputs also
  run-persistent compression counters (`_MAX_COMPRESSIONS`, `_compress_fail_streak`,
  `_ctx_compressions`, `_ctx_critical_warned`, `_ctx_evictions`, `_force_compress_next`,
  `_last_force_compress_round`, `_swa_triggered_compress`, …) as well as `_dtool_msgs`,
  `_est_tokens`, `_guard_tokens`. A clean extraction produces exactly the forbidden
  60-field `RoundState` object; the first attempt was fully rolled back (no code
  changed). C5 (230 lines, 3 loop crossings) is the same class.

  *Decision:* **do not extract as a whole** (won't-do, like B4). Instead pull only
  small, cohesive sub-blocks with ≤5 outputs (pattern W4a: `duo_loop_guards`) or
  deliberately leave the loop as a monolith. The sync-back contract in
  `duo_compress_cycle` remains open as F1.

  *Acceptance (if later):* characterization test before every move; no state object
  >15 fields; `duo_runner` must not grow by interface code.

- [x] **B2 · P1 · DEBT — `backend/manager_load.py` excluded from Pyright TARGETS.**
  55 cross-mixin attribute errors; a TYPE_CHECKING contract like in
  `manager_prefetch.py` is missing.

  *Done 2026-11-07:* TYPE_CHECKING contract like `manager_prefetch.py`, 55 → 0 errors,
  in TARGETS, baseline 1/1 (only `run_code_duo` complexity).

- [x] **B3 · P2 · DEBT — Source-string pins → behavior tests.**
  195/333 suites use the shared harness; many pins still grep source text.

  *Acceptance:* per touched suite at least one behavior test; pins only for architecture
  invariants that cannot be behavior-tested.

  *Progress 2026-11-07:* pin `j` in `test_split_audit_fixes` is now a real
  `_phase_moe` probe (cache MiB as int, slot bool marker, flags in cmd, cache-off
  path), no longer a source grep. The new helper suites (`pipeline_runner`, `chat_run`)
  are fully behavioral.
  *Progress 2026-11-09:* `tests/test_duo_compress_cycle_behavior.py` (13 behavioral checks)
  and range-read fuzzy-edit / `min_viable_tool_budget` / `duo_core` priority behavior
  checks added; policy continues per-touch. `test_moe_cache` pins 3/6 and the
  `test_duo_ubatch` resolver pin now run real probes; remaining pins are UI/loader
  wiring with no cheap probe. Closed as per-touch policy: every touched suite has a
  behavior check; no bulk rewrite.

- [x] **B4 · P2 · TEST — Remaining harness migration.** (195 of 333 suites migrated)

  *Decision 2026-11-07 (owner):* **won't do** as a bulk migration. Purely cosmetic, no
  product behavior and no performance gain; any further migration is churn. New suites
  use the harness, existing ones are converted only when touched. Special formats stay
  as they are.

- [x] **B5 · P2 · DEBT — Dead UI/settings legacy keys.**
  `chat_autosave_enabled` (client-side only), `duo_pair` (UI only); decide: wire up or
  remove (defaults, app.js, docs).

  *Done 2026-11-07:* both are **not** dead keys, but deliberately client-side:
  `chat_autosave_enabled` controls whether the UI persists the chat before `/stream` or
  after edits (`static/app.js:6852/7012/10864`); `duo_pair` remembers the pair
  selection, while `agents.duo_coder.model` is the engine side. `chat_autosave_enabled`
  now has default `True` in `settings_defaults.py` and is thereby documented in
  `docs/settings.md`; no engine reader needed.

- [x] **B6 · P2 · DEBT — Pyright remainder: 1 error (`reportGeneralTypeIssues` in `run_code_duo`).**
  Stays coupled to B1 (monolith decision); baseline 0/0 is no longer a goal while the
  loop deliberately stands.

  *Acceptance:* 0/0 only if the diagnostic disappears without restructuring; otherwise
  1 remains with documented justification.

  *Done 2026-11-09:* documented as accepted coupled to B1; all 7 Batch-20 split modules
  (`duo_policies`, `duo_round_gate`, `duo_finish_round`, `duo_plan_round`,
  `truncated_write_salvage`, `duo_ctx_meter`, `duo_explore_gate`) added to
  `deploy/pyright_baseline.py` `TARGETS` and pin `q` in `tests/test_split_audit_fixes.py`
  (0 new errors, baseline stays 1/1).

- [x] **B7 · P3 · DEBT — Extend the Ruff rule set.** (F401: 537 → 179 → 0; enabled)
  Currently only E9/F821/F601/F811/F841/W605; `target-version` is 3.14.

  *Progress 2026-11-07:* 358 unused imports removed (54 files), 6 justified
  `# noqa: F401` keeps, ruff/test green. F401 stays off until the facades
  (`llama_server_manager`, `manager_load`, `__init__`) are explicit via
  `__all__`/per-file-ignores and the test residues are cleaned up.

  *Done 2026-11-09:* `routers/__init__.py` and `tools/handlers/__init__.py` made explicit
  via `__all__`; unused imports removed in `backend/__init__.py`, `core/duo_runner.py`,
  and 42 test files; `per-file-ignores` set in `ruff.toml` for the runtime-sync/mixin
  facades (`backend/llama_server_manager.py`, `backend/manager_load.py`); `F401` enabled
  in `ruff.toml` (`ruff check .` → 0 errors).

- [x] **B8 · P2 · TEST — Raise coverage from 46.12% in a targeted way.**
  Weakest core modules: `core/pipeline_runner.py` 2.5%, `duo_compress_cycle` 4.1%,
  `chat_run` 10.3%, `llama_start_phases` 11.1%, `_pre_explore` 11.6%,
  `manager_load` 18.9%.

  *Acceptance (changed 2026-11-07, owner):* **no percentage target.** New tests only
  when a module is touched, a bug is fixed, or a refactor is put on a safety net; per
  touched module one happy-path and one error-path test, no pure string pins. Coverage
  stays diagnostic, not a target.

  *Progress 2026-11-07:* `project_state` (40), `background` (31), `log_noise` (33),
  `keep_awake` (27), `testmap` (26), `manager_health` (46), `compression` (58),
  `chat_route_decision` (25) covered with new behavior suites. Batch 11:
  `pipeline_runner` helpers (17 checks), `chat_run` helpers (14),
  `llama_start_phases._phase_claim` (+13) and the MoE phase probe (+4).
  *Progress 2026-11-09:* `core/duo_compress_cycle.py` covered with
  `tests/test_duo_compress_cycle_behavior.py` (happy path + both 3x fail-streak stop
  paths + round-0 incompressible guard + `_walk_workspace_inventory`).
  *Progress 2026-11-09 (B8 batch):* new behavior suites drive the previously weak
  modules end-to-end with fakes: `pipeline_runner` (6 checks), `chat_run` (6),
  `llama_start_phases` (6), `_pre_explore` helpers (6), `manager_load` helpers (5).
  The pipeline suite also found and pinned a real abort-before-round-1
  `UnboundLocalError` (`_pipeline_ws_ctx`), fixed in `core/pipeline_runner.py`.
  Coverage stays diagnostic; no percentage target.

- [x] **B9 · P3 · TEST — Test runtime and flake quarantine.**
  Full run ~8–12 min (6 workers); `--changed` exists, but no impact mapping and no
  flake history.

  *Done 2026-11-07:* the runner writes pass/fail/duration per run to
  `tests/.regression_history.json` (git-ignored, last 30 runs); `--flake-report [N]`
  shows flips, pass rate and median duration; `tests/quarantine.json` is the reviewed
  quarantine list (`[QUAR]`, `--include-quarantined`, `--no-history`). No retry: a
  flake stays visible. Two over-selection bugs in `--changed` found and fixed: doc
  files were not removed before selection, and `tests/<fixture>` produced the pattern
  `tests` (253/331 suites). Mixed 7-file change: 331 → 14 suites in 2.3 s;
  `core/tool_loop.py` alone → 17 suites in 38 s (33 s of that `mcp_client`), i.e. under
  the 60-s target mark. 19 mechanics checks in `tests/test_regression_runner_b9.py`.

- [x] **B10 · P3 · DEBT — Harden dependencies.**
  `requirements.txt` uses `>=`; `uv.lock` is the exact source.

  *Done 2026-11-07:* both CI workflows check `uv lock --check`
  (`astral-sh/setup-uv@v5`); the updater already uses `uv sync --frozen`.
  `requirements.txt` remains the installer fallback.

## C. Release/Update Hardening (open)

- [x] **C1 · P1 · RELEASE — Build the release ZIP from `HEAD` and install it fresh.**
  So far only source/clean checkout tested; no `package_release.bat` run and no
  installation from the generated ZIP.

  *Done 2026-11-07:* ZIP from an isolated worktree (`git archive HEAD`), 368 entries,
  one root, no leaks (settings/presets/models/tests/sessions), BUILD_INFO=c4efbac.
  Fresh `uv sync --frozen` installation: `/health` ok, 35 models, direct chat
  completed, `read_file` tool, UI 200. Only finding: a corrupt 0-byte entry in the
  local uv cache for pydantic-core; `uv cache clean pydantic-core` + reinstall fixes it
  (not in the ZIP).

- [x] **C2 · P1 · RELEASE — Test the update migration 1.5.5 → next version.**
  The `server.py` compatibility anchor is pinned, but the **old** 1.5.5 `update.bat`
  has not yet been run against a ZIP from the current tree.

  *Done 2026-11-07:* original 1.5.5 installation + old `update.bat --zip` against the
  new ZIP: backup `update_backup_1.5.5`, apply, verify green, new modules
  (`duo_coder_preloop`, `duo_task_complete_gate`, `chat_tools_route`, `duo_loop_guards`)
  present, `update.bat` swapped to the new shim, BUILD_INFO/manifest correct. The
  `server.py` anchor carried the old probe.

- [ ] **C3 · P1 · RELEASE — Observe the first `windows-full` cron run (Monday).**
  Python 3.14, `--ci-subset --jobs 2`, exclusive lane, timeout 60.

  *Acceptance:* green run or documented fixture fix-up; no WinError-10053 flakes.

  *Local equivalent 2026-11-09 (still open — the hosted cron run is the evidence):*
  `.venv/Scripts/python tests/run_regressions.py --ci-subset --jobs 2` on the dev
  working tree, Windows 11, Python 3.14.7: **365/365 PASS**, **0 WinError-10053**,
  `lsp_real_pyright` and `lsp_real_rust` ran for real. The earlier 350/352
  (D4 settings/docs) is fixed: `duo_selective_tests` is exempted and
  `docs/settings.md` regenerated.
  *The first cron run must verify:* (1) it checked out `dev` — the `ref: dev` line only
  takes effect once `windows-full.yml` is merged to `main` (main's copy differs, +33/−8);
  (2) `uv lock --check` and `uv sync --frozen` pass on the hosted image; (3) `npm i -g
  pyright` makes `lsp_real_pyright` run instead of skip, and note whether `lsp_real_rust`
  skips (no rust-analyzer on the runner); (4) total runtime well under the 60-min cap
  on 2 cores; (5) no WinError-10053 / socket flakes; (6) 362/362 or a documented
  fixture fix-up.

- [x] **C4 · P2 · RELEASE — Automate the version anchor on bump.**
  The text anchor in `server.py` must stay in sync with `core/version.py` (pin h2).

  *Acceptance:* the release/bump script updates both, or a test fails early on drift
  (currently only a test, no bump hook).

  *Done 2026-11-09:* `deploy/bump_version.py <x.y.z> [--dry-run]` updates
  `core/version.py`, the `server.py` anchor and `pyproject.toml` together (CRLF kept,
  computed before anything is written); `--check` reports drift (exit 1); invalid
  semver (strict, lowercase pre-release only) or a missing anchor is refused
  (exit 2, nothing written). The CHANGELOG heading stays manual (script reminds).
  `tests/test_version_bump.py` (10 checks, temp copies only, real files hashed
  before/after), registered as `version_bump` in `tests/run_regressions.py`.

- [ ] **C5 · P2 · TEST — External integration matrix.**
  Telegram gateway (token/owner), real STT/TTS audio processing, real vision inference
  with an image, SearXNG, configured MCP servers, `send_file`.

  *Acceptance:* per integration a documented smoke test with credentials/fixtures;
  status today: **not tested**, not "green".

  *Tooling done 2026-11-09, results still open:* `tests/live_integrations.py` (7 opt-in
  checks: `telegram`, `stt`, `tts`, `vision`, `searxng`, `mcp`, `send_file`; `--list`,
  selectable names, `--strict`), documented in `docs/integrations.md` (env vars,
  commands, expected result). Offline contract (all SKIP without env, no network,
  token redaction) pinned by `tests/test_live_integrations_offline.py` (8 checks,
  registered as `live_integrations_offline`). First run here: 7/7 SKIP — no engine on :8001, no SearXNG on
  :8888, no bot token/owner id, no STT fixture. Each integration stays **not tested**
  until a run with real credentials records a PASS here.

## F. Deep Audit 2026-11-07 (5 subagents, read-only)

Verdict of the audits: **partially done.** The code/test/release base is good; open
items are an unguarded sync contract, two manager lock/retry topics, and smaller
docs/runner leftovers. Done in Batch 15: bench mode/timeouts/hidden-test hardening,
eviction/process probe off-loop, `tzdata` into the production deps, CI installs the
frozen lock, leak regex/export ignore, CHANGELOG/gateway contract, dead surface
(aliases, `_DIRECT_TIME_NOTE`, `build_execution_block`, 7 dead pre-explore kwargs),
runner robustness and flake report (chronic/partial).

- [x] **F1 · P1 · DEBT — `duo_compress_cycle` sync-back is unguarded at early returns.**
  ~48 locals are read from `ps` and written back only at the natural end (18 fields);
  the two stop paths sync 5 and 11 respectively. Two `STOP-SYNC` fixes in the history
  attest to this bug class; `test_duo_split_integrity` only sees declared fields, not
  missing syncs.

  *Done 2026-11-07:* a `_sync_state()` writes all 18 fields and is called at both stop
  returns AND at the natural end; an AST guard forbids direct `ps.*` writes outside the
  helper (`ps.stop` exempt) and checks "every return syncs first".

- [x] **F2 · P2 · PERF — Manager lock is held across `sleep(5)` + kills.**
  `_enforce_vram_budget` (backend/manager_load.py) holds `self._lock` across
  `asyncio.sleep(5)` and `_kill_slot_async` (up to ~10 s per slot). The event loop no
  longer freezes (Batch 15), but all manager ops serialize.

  *Done 2026-11-07:* three short critical sections (snapshot, fresh selection,
  re-check); the sleep and both kills run outside the lock. `tests/test_manager_vram_lock.py`
  (16 checks) proves the ordering and that the lock is not held during a kill.

- [x] **F3 · P2 · PERF — 45-fold 5xx retry can multiply prefill.**
  `LlamaClient` (`_TRANSIENT_HTTP_STATUSES`, `max_attempts=45`) also repeats
  deterministic 500s (context overflow/template) and sends the full prompt every time.

  *Done 2026-11-07:* `_should_retry_http_status` — 500/502/504 max. 3 attempts,
  overflow markers ("exceed"/"context window"/…) never; 408/429/503 keep the long
  retry. Both paths (`chat`, `chat_stream`) use the helper; 49 checks incl.
  fake-client integration.

- [x] **F4 · P3 · PERF — Remaining sync I/O on the loop.**
  `os.walk` in `core/duo/_pre_explore.py` and `core/duo_compress_cycle.py`; the prefill
  meter reads `llama_server_{port}.log` (files are `..._{timestamp}.log` since
  rotation); telemetry append per call.

  *Done 2026-11-07:* both walks as `asyncio.to_thread` helpers (selection/cap
  identical, result sorted); the prefill meter globs the newest
  `llama_server_{port}_*.log` (30-s cache, 64-KB tail in the thread). Telemetry
  deliberately stays synchronous. 41 checks in `test_prefill_log_tail.py`.

- [x] **F5 · P3 · DEBT — `_find_loaded` orphan check releases the lock and mutates after re-acquiring it.**
  Rare clobber of an in-flight load.

  *Done 2026-11-07:* snapshot (port/model/process/dead flag) under lock, probe
  outside; re-validation before every mutation (`_loading`, model, process, registry
  identity). On change, rescan instead of reset; 52 checks in `test_find_loaded_race.py`.

- [x] **F6 · P3 · TEST — Three pin suites with high conversion value.**
  `test_voice_card` (~100 string pins), `test_ui_context_card`, `test_toolgen_stream_card`;
  convert only when touched (B3 rule), start with `/stt/fetch` as an httpx behavior test.

  *Decision 2026-11-08:* **won't do as a bulk conversion** (like B4). These suites
  secure UI wiring that cannot be meaningfully behavior-tested without a browser/JS
  runtime; convert only when the respective code is touched anyway.

- [ ] **F7 · P3 · RELEASE — Weekly cron keeps testing `main` until the merge.**
  `windows-full.yml` now has `ref: dev`, but scheduled workflows read the file from the
  default branch; effective only after the next release merge.

  *Status:* not solvable in the tree; needs the next merge/release.

- [x] **F8 · P3 · DEBT — Two snapshot/rollback classes with different capture semantics.**
  `WorkspaceTransaction` (re-capture) vs. `FileTransaction` (first-wins).

  *Done 2026-11-07:* the divergence and the merge precondition are documented at both
  class headers; no code merge (semantics stay as they are).

- [x] **F9 · P1 · DEBT — Deep audit after the 1.5.5 refactor wave.**
  *Done 2026-11-08:* explicit lock ownership in `_find_loaded` (manager callers
  pass `lock_held=True`; core callers never release a lock they do not own),
  claim-for-kill (`_loading=True`) for LRU/pre-flight victims, rehabilitated
  orphans keep the orphan port, `duo_core` exempted from the READ_REQUIRED
  write guard (the `write_file` alias stays gated), `direct_research` prompt +
  `task_complete`, split-integrity covers the five new seams (dead `_ri_ps`
  writes/fields removed), legacy tool prompt built from the real schema,
  abort-registry sweep wired, benchmark tier/test-feedback/ctx pins + server
  lift, cell timeout/preflight/check_output/churn/analyze fixes, docs/CI.

- [x] **F10 · P2 · TEST — LSP tool settings bypass the run snapshot.**
  *Done 2026-11-09:* verified the existing snapshot chain (`/stream` body pin
  → `chat_run` run settings → `tools.runner._run_settings_snapshot` →
  `lsp._lsp_settings`), fixed the missing-snapshot test bug, 112 LSP checks.

- [x] **F11 · P3 · PERF — Coder 5xx retry still lacks the body cap.**
  *Done 2026-11-09:* AgenticToolLoop reuses the F3 policy — 500/502/504 max.
  3 attempts, context-overflow bodies never retried, 408/429/503 unchanged.
  29 new checks.

- [x] **F12 · P3 · DEBT — `tool_phase` subset bypasses the tier filter.**
  *Done 2026-11-09:* phase tools are intersected with the mode tier when
  `cfg.tool_mode` is set; empty intersection keeps the phase subset. 12 new
  checks, phase-only behavior unchanged.

- [x] **F13 · P1 · DEBT — `run_code_duo` god function.**
  *Done 2026-11-09:* 5,934 → 2,936 lines (−50.5 %). Policies/gates live in
  `duo_policies`, `duo_round_gate` (39 checks), `duo_finish_round` (20),
  `duo_plan_round` (12), `truncated_write_salvage` (6), `duo_ctx_meter`
  (8 + 33 shrink checks), `duo_explore_gate` (6). The coder-POST wait/result
  seam moved to `_duo_coder_post_wait` (60 checks); state dataclasses live
  with their helpers. Remaining mass is deliberate argument plumbing
  (compression/exec constructors), where further cuts would add wrappers or
  state bags.

- [x] **F14 · P2 · DEBT — Split complexity moved into state bags.**
  *Done 2026-11-09:* dead fields/args removed, seven state classes relocated
  to their helper modules, `timed_out` and scalar `_last_too_large_path`
  round-trips removed. The remaining 48-field compress state is load-bearing
  (AST-verified: zero dead unpacks). Two real seam bugs found and fixed
  (preloop `duo_coder_sys` input, final-cleanup run-counter sync).

- [x] **F15 · P3 · DEBT — Two new back-import cycles + deferred imports grew.**
  `duo_finalgate`/`duo_finalize` defer-import `duo_runner`; the chat SCC grew
  2 → 7 modules via `_history_budget_tokens`. Deferred imports 505 → 554.

  *Done 2026-11-08:* `_history_budget_tokens` moved to `core/llm_stream.py`
  (chat_history_seed/chat_route_decision import it there); `duo_finalize`
  imports `_auto_*` from `core.duo.preflight`, resume helpers from
  `context.resume`, project state from `tools.runner`; `duo_finalgate`
  imports `_write_resume_block` from `context.resume`. The F401 re-export
  comment is gone, and 11 verified dead top-level names plus two dead
  split-state fields were removed.

## D. Competitiveness (open)

- [x] **D1 · P2 · COMPETE — Benchmark runner: relative gain instead of absolute quota.**
  (Recommendation from the review with Sonnet, 2026-11-07)

  *Ordering:* **after A3/A4** (owner, 2026-11-07) — fix the routing first, then build
  the runner, so the baseline does not measure a known bug. No outcome measurement for
  coding quality (only pins/units). What matters is **not** a high absolute quota but
  the relative gain over the baseline: in RExBench, Aider, Claude Code and OpenHands
  with nine models fail most tasks; even with hints the best value stays below 40%.
  WebGen-Bench: best combination 27.8%. Absolute numbers are therefore not a usable
  target.

  *Setup:*

  - **Same model, same server.** One realistically used model (`qwen3.5:4b-mtp`,
    `qwen3.6:35b-a3b-ud`); all conditions run on the same llama-server setup, so
    quantization and sampling do not make the difference.
  - **Four conditions:** (a) bare model (one prompt, no tools), (b) HiveMind Direct +
    full tools, (c) HiveMind Code-Duo agentic, (d) Aider with the same model as an
    OpenAI-compatible backend.
  - **Tasks with automatic checking:** 20–40 small tasks with hidden tests (red/green);
    the obvious common yardstick is Aider's public benchmark tasks (exact count
    unverified), plus 5–10 tasks from our own repos where "correct" is known.
  - **Repetitions:** 3–5 runs per task and condition, pass rate instead of single
    results (local models vary a lot). Also measure runtime and token usage — a
    harness that takes twice as long has to earn that with better results.
  - **HiveMind advantage:** OpenAI-compatible endpoint in agent tool-loop mode; the
    runner talks to HiveMind like the other conditions.

  *Evaluation:* delta against the baseline (bare model) per task category; a bad result
  is useful because it shows which parts (planner, chunking) cost instead of help.

  *Acceptance:* `bench/` runner + task format + result JSON, reproducible run, weekly
  comparison; report with pass rate, delta, runtime and tokens per condition.
  **No absolute quota target.**

  *Progress 2026-11-07:* scaffolding in place — `bench/runner.py` with the four
  conditions (`raw`, `hivemind_direct`, `hivemind_duo`, `aider`), task format
  (`task.json` + visible `repo/` + hidden `tests/` that are copied in only at check
  time), JSON report, delta-vs-raw evaluation, `--runs`/`--keep-work`, README.
  **21 starter tasks** (9 bugfix, 5 feature, 3 refactor, 4 multi-file), each verified
  red-before-fix/green-after-reference-fix; 41 checks in `tests/test_bench_runner.py`.

  *Smoke 2026-11-08:* `direct` 2/2 (15.6k/21.3k tokens), `duo` 1/1 (52 s, 68.5k
  tokens), `raw` 0/2 (invalid diffs), `raw_files` 2/2 (680 tokens) — report
  `docs/benchmarks/RESULTS_2026-11-08.md`. Fixed along the way: real `mode=simple`,
  model fairness (`direct_model`/duo roles = `--model`) and ToolLoop usage counting
  (previously ~0 tokens for tool runs).

  *Aider 2026-11-08:* 0.86.2 installed in `HiveMind_bench_tools/aider` and wired via
  `--aider-bin`; smoke 1/1 (10.4 s, 833 tokens).

  *Quiet run 2026-11-08 (6 tasks, 4B):* aider 100% (12.4 s, 970 tok/cell), direct 100%
  (20.2 s, 22,554 tok/cell), raw_files 67%, duo 0% (4B coder does not edit; planner
  runs). Report `docs/benchmarks/RESULTS_2026-11-08.md`; duo needs the 35B run before it is
  judged.

  *Correction 2026-11-08:* the 0% was `duo_tool_rounds=0` (tools off), not a 4B limit.
  After pinning it to 50: 2/2 smoke, ~58 s. Coder-only run against Aider to follow.

  *21-task run 2026-11-08 (3 runs, 252 cells):* aider 90.5% [81–96], direct 92.1%
  [83–97], **McNemar p=1.0** → no measurable quality difference; direct costs ~22×
  tokens (21,228 vs. 946/cell) and 1.6× time. raw_files 71.4%, raw 0%. Categories are
  complementary (direct feature 100%, aider multi-file 100%). Report
  `docs/benchmarks/RESULTS_2026-11-08.md`.

  *Coder-only 2026-11-08 (21 tasks, 1 run):* 95.2% [77–99], 58.7 s, 108,378
  tokens/cell. Same 20/21 tasks as Direct, McNemar p=1.0; ~114× Aider tokens, ~5×
  Direct. The cause of the earlier 0% was `duo_tool_rounds=0` (tools off), not the 4B
  model. Open: 5–10 tasks from real repos and the 35B/duo run.

  *Token tiers 2026-11-08:* the coder now defaults to `duo_core` (17 tools,
  ~2.8k schema vs ~5k full, incl. `lsp_diagnostics` for warm auto-diagnostics);
  `duo_coder_tool_tier=full` opts into git/web/MCP/browser/LSP extras. Direct
  gains `direct_research` (web+read, no write/exec).
  Live 2-task coder core smoke: 2/2, ~48k tokens/cell (the earlier full-tool
  2-task smoke was ~62k; different task mix, indicative). A/B on 21 tasks and
  churn metrics still pending.

  **Procedure/runbook/time budget:** `docs/benchmarks/ROADMAP.md`.

  *Done 2026-11-09:* `bench/relative.py` is the single mapping/stats source;
  runner + analyzer emit `delta_vs_raw`, `delta_vs_raw_files`, per-category
  deltas, `cached_share`, `rounds_total/median` and net-token ratios, with tests
  for missing baselines, zero tokens and all-pass/all-fail. Old result files
  remain readable. No absolute quota target.

- [ ] **D2 · P2 · COMPETE — Context engine.**
  Tree-Scout/static map + FTS exist; incremental index, retrieval and per-task budget
  are missing.

  *Acceptance:* fewer tokens per task at the same/greater eval success rate.

  *Progress 2026-11-09:* tiny top-level workspaces are pre-seeded into the first
  coder message (`build_preseed_context`, context-scaled cap) and the benchmark
  now reports `cached_tokens`/`net_tokens`/`rounds`. A true incremental index and
  per-task retrieval budget remain open.

  *Progress 2026-11-09 (D2 core landed):* `context/repo_index.py` (stdlib only)
  is an incremental per-workspace index — top-level text/code files with
  mtime+size invalidation, .py symbols via `ast`, cheap regex symbols for
  .js/.ts/.jsx/.tsx, hard `max_files` cap (2000, no recursion into a huge
  repo) — and `search()` ranks by path/stem/symbol token overlap.
  `build_preseed_context` takes the task text as `query` now: with more than 4
  top-level files it inlines up to 4 index-selected relevant files instead of
  refusing (empty query or no match keeps the old no-preseed behavior; the
  <=4-file path is byte-identical). The char cap is configurable via
  `duo_context_budget_chars` (default 8000 = the previous cap). Tests:
  `tests/test_repo_index.py` (9 checks), `tests/test_preseed_context.py`
  extended with the >4-file selection case (10 checks). Still open: retrieval
  beyond the tiny-workspace pre-seed (ranking deep files into the coder
  prompt) and the eval A/B backing the "fewer tokens" acceptance.

- [ ] **D3 · P2 · COMPETE — Edit/diff engine.**
  `edit_file`/fuzzy and `write_file` work; patch-native editing and multi-file
  refactor with dependency ordering are missing.

  *Acceptance:* multi-file task in the eval suite without full-file-rewrite churn.

  *Progress 2026-11-09:* `WOULD_DROP_SYMBOLS` now carries the original
  definition line and applies to `write_file` + both edit paths; tiny-workspace
  pre-seed and range-read fuzzy edit reduce read/rewrite churn. True patch-native
  multi-edit and dependency-ordered multi-file refactors remain open.

  *Progress 2026-11-09 (batch multi-edit landed):* `edit_file` accepts optional
  `edits: [{old_text, new_text}]` — up to 20 exact replacements applied
  sequentially to ONE file with a single atomic write (the single
  old_text/new_text API is unchanged, patch_file key spellings accepted per
  entry). Each entry is validated against the evolving text (unique/not-found,
  empty, `<parameter=` malformed fragments); the shrink guard and
  `WOULD_DROP_SYMBOLS` compare the ORIGINAL content with the final result, so
  a rejected batch never writes a byte. Definition documented (edits array in
  the schema). Tests: `tests/test_multi_edit.py` (8 checks, registered in
  `tests/run_regressions.py`). Multi-FILE dependency ordering stays a
  plan/prompt concern — no new planner; patch-native format remains open.

- [x] **D4 · P3 · COMPETE — Verification depth.**
  `run_tests` + testmap + auto-verify exist; selective coverage/impact selection and
  best-of-N/draft-verify are missing.

  *Acceptance:* fewer unnecessary test runs, higher verified rate in the eval suite.

  *Done 2026-11-09:* `duo_selective_tests` (default true) scopes `run_tests` to
  the testmap-mapped tests of this run's written files; explicit `paths`/`targets`
  win, empty mapping falls back to the full suite. Tests:
  `tests/test_selective_tests.py` (3 checks).

- [x] **D5 · P3 · COMPETE — Sandbox for `run_bash`.**
  Workspace/destructive guards exist, but no OS isolation.

  *Acceptance:* an optional container/Job Object sandbox mode with documented limits;
  default stays local.

  *Done 2026-11-09:* already implemented in `tools/sandbox.py` (`ToolJob`,
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, memory/process limits) and wired through
  `spawn_kwargs`/`ToolJob.confine` in `run_bash`, `run_python`, `run_tests`, and
  `start_background`; settings `duo_tool_sandbox*` gate it. No new code needed.

- [x] **D6 · P3 · COMPETE — Outcome-based routing.**
  Judge/AutoMap route statically; results (success/cost per task type) do not flow
  back.

  *Acceptance:* routing hints from completed runs, measurable in the eval suite.

  *Done 2026-11-09:* `routing/outcome_hints.py` aggregates chat-scoped
  `run_end` audit entries; `_chat_route_decision` emits a status hint when the
  current route has ≥3 runs and another route is ≥25 percentage points better.
  Hint only, never an override; Duo now records `route="code_duo"`.
  Tests: `tests/test_outcome_hints.py` (6 checks).

- [x] **D7 · P3 · COMPETE — Diff review/undo tree in the UI.**
  Git checkpoints exist; convenient review/revert per run is missing.

  *Acceptance:* run diff visible, one-click checkpoint restore.

  *Done 2026-11-09:* `/git/diff` returns a capped read-only stat+patch;
  `/git/checkpoints` lists marker commits; `/git/checkpoint/restore` resets
  tracked files to a chosen checkpoint and creates a safety stash snapshot
  first. The Git panel has "View Changes", "Checkpoints" and "Restore".
  Untracked files are left in place (documented limitation).
  Tests: `tests/test_git_diff_route.py` (3), `tests/test_git_checkpoints.py` (6).

- [x] **D8 · P3 · COMPETE — Benchmark against Claude Code / Codex / Aider.**
  Without comparison there is no "competitive" claim.

  *Acceptance:* the same task suite as D1, documented success/cost/latency figures;
  D1 (relative gain) is the mandatory part, D8 the bonus. Report the same-model part
  (Aider) and the reference part (Claude Code/Codex with default models) separately —
  see `docs/benchmarks/ROADMAP.md` Phase 5.

  *Done 2026-11-09 (scaffold):* `bench/external/run_external.py` runs the same
  task suite through Claude Code/Codex when installed, with hidden-test checks,
  pass/fail, duration, token/cost fields, `--list` dry-run, and clean skip when a
  binary/credential is absent. No external results are claimed yet.

## E. Already Done (Reference)

- W1: Commit, CHANGELOG, Ruff 3.14, update compatibility anchor (`server.py`).
- W2: `--changed` runner, exception drift, `ask_user_auto_proceed` wired up.
- W3: Pyright 191→1, shared harness, freshness bug, `retryable` bug.
- W4: `duo_loop_guards.py`, Guard-C behavior test.
- W5: coverage setup, harness batch 2, `uv.lock`.
- W6: harness batch 3 (63 suites), Pyright +17 modules, coverage wrapper fix,
  `numpy`/`tzdata` gaps.
- Live matrix 2026-11-07: API/CRUD/tools/modes/control; tool route bug
  `ResponseNotRead` fixed (`c5f686e`), 322/322 green locally.
- Batch 8/9 (2026-11-07): 358 unused imports removed, harness at 193/331 suites, four
  coverage suites (`testmap`, `manager_health`, `compression`, `chat_route_decision`),
  `uv lock --check` in both CI workflows.
- Batch 10 (2026-11-07): B5 documented (UI-only keys), B9 flake history/quarantine/
  `--flake-report`, two `--changed` over-selection bugs fixed, D1 benchmark plan
  (relative gain) added.
- Batch 11 (2026-11-07): B8 behavior suites for `pipeline_runner` and `chat_run`
  helpers, `_phase_claim` (+13) and MoE phase (+4; replaces the source pin `j`),
  333/333 suites green.
- Batch 12 (2026-11-07): A3 simple tool routing before Agentic/Judge-Duo, A4
  prompt/round parity + repeat guard in the legacy tool route; B4 closed as won't-do,
  B8 percentage target dropped, D1 ranked after A3/A4.
- Batch 13 (2026-11-07): D1 scaffolding `bench/` (four conditions, task format,
  JSON report, delta vs. raw), example task and 20 harness checks.
- Batch 14 (2026-11-07): D1 starter corpus with 21 verified tasks
  (bugfix/feature/refactor/multi-file), hidden tests copied in only at check time
  (tamper-proof), 28 harness checks.
- Batch 15 (2026-11-07/08): deep-audit fixes — bench mode/timeouts, eviction + process
  probe off-loop, `tzdata` into production deps, frozen CI lock, leak regex/export
  ignore, docs/dead surface, runner robustness and flake report; 337/337.
- Batch 16 (2026-11-08): F2 manager lock scope (sleep/kills outside, 16 checks) and F3
  5xx retry policy (500/502/504 max. 3, overflow never; 49 checks); 339/339.
- Batch 17 (2026-11-08): F4 sync I/O (walks/prefill meter, 41 checks), F5
  `_find_loaded` race (52 checks), F8 divergence documented; Aider 0.86.2 installed
  separately, `--aider-bin` wired, smoke 1/1; 341/341.
- Batch 18 (2026-11-08): A5 card timer removed (engine the only auto-answer source),
  A6 Playwright browser test passed and stored as an opt-in script, F6 closed as
  won't-do.
- Batch 19 (2026-11-08): deep audit (4 subagents) — manager lock ownership/claim-
  for-kill fixes, duo_core write guard, direct_research prompt, split-integrity
  coverage, benchmark pins/timeout/preflight/churn/analyze, docs/CI; 343/343.
  Remaining: F10 (LSP settings snapshot), F11/F12 (P3).
- Batch 20 (2026-11-08): split audit vs 1.5.5 (3 subagents, 147 symbols + 20 split
  seams) — all functions present, 0 lost code, 3 documented behavior changes; fixed
  Guard-C telemetry (`slot_ctx_floor_stop` seam), epilogue AUTO-STOP carry keys,
  dead round-input/epilogue carry reads, restored 1.5.5 broad guards in compression
  cleanup/plan-pin/inloop-planner, added carry-key integrity check (40 checks).
  F15 closed: both back-import cycles removed, `_history_budget_tokens` moved
  to `llm_stream`, 11 dead names + 2 dead state fields deleted.
  Follow-up simplification (same audit): AST-verified dead stores removed,
  TOOL-CALL-MISS logging extracted (behavior test), emergency-evict block
  deduplicated, empty-answer branches merged; policy helpers moved to
  `core/duo_policies.py`, grace/max-rounds/auto-test gates to
  `core/duo_round_gate.py` (39 checks), round-finish policy to
  `core/duo_finish_round.py` (20 checks), PlanTracker round to
  `core/duo_plan_round.py` (12 checks), truncated-write salvage to
  `core/truncated_write_salvage.py` (6 checks), ctx-meter/SWA to
  `core/duo_ctx_meter.py` (8 checks), explore-only gate to
  `core/duo_explore_gate.py` (6 checks), WRITE-TRUNCATION diagnostic to
  `core/duo_policies.py`, seven state dataclasses to their helper modules,
  coder-POST wait seam to `_duo_coder_post_wait` — `run_code_duo`
  3,604 → 2,936, `duo_runner.py` 5,606 → 5,001. F10/F11/F12 closed (LSP
  snapshot chain, F3 5xx policy, phase×mode intersection). Three parallel
  subagents also found and fixed two real split regressions: the preloop no
  longer received `duo_coder_sys` (coder prompt overwritten) and final
  cleanup never synced `_project_state_run_counted` (double run count).
  Tool tiers + LSP setting are now pinned in the `/stream` body (UI race fix,
  11 frontend checks). Static backlog complete; 353/353.
- Live smoke 2026-11-09: `tests/live_smoke.py` added. Direct PASS (10.1 s,
  `completed`, exact `OK`). Duo tool-layer live run blocked by ComfyUI holding
  ~7 GB VRAM (`needed 3.5 GB, free 1.2 GB`); the manager refuses correctly.
  After closing ComfyUI, Duo PASS (36.8 s): `hello.py` written, `run_bash`
  verification green, `task_complete` accepted. Smoke defaults to 16k coder
  ctx; at 8k with the shipped 16k output budget every round force-compresses
  and can end in `loop_detected` even though the file was written.
- Core-coder vs Aider 2026-11-09 (21 tasks, 1 run/cell): both 19/21 (90.5%),
  coder 54.3 s / 66,089 tok per cell vs. Aider 22.1 s / 970. Coder wins
  `feature_parse_bool` + `feature_word_freq`, loses `multi_config` +
  `multi_move`; McNemar p=1.0. Core tier is ~39% cheaper than the 2026-11-08
  full-tool run at the same quality band. Details:
  `docs/benchmarks/RESULTS_2026-11-09.md`.
- Batch 21 (2026-11-09): 8k context guard fixed end-to-end (`duo_dtool_setup`
  no longer duplicates `_dtool_sys_eff` into `trail_msg`; `duo_runner` uses
  `factor=1.05` once `_coder_real_prompt_tokens` is calibrated;
  `clamp_request_max_tokens` enforces `min_viable_tool_budget`; `live_smoke.py
  --ctx 8192` PASS in 29.2 s with 0 compressions). `WOULD_DROP_SYMBOLS` symbol
  guard added for `.py` files in both `write_file` and `edit_file` + re-export
  rule in `WRITE RULES`. Truncated writes are dropped without writing markers to
  disk (`[WRITE-TRUNCATED]` in `_drop_notices`). Verify gate without suite runs
  deterministic syntax check (`syntax_verified`). Range-read unlocks fuzzy edit
  (`_files_seen_in_run`). B6/B7 closed (`F401` enabled, 0 ruff errors, pyright
  1/1), B8 `duo_compress_cycle` raised from 4.1% to 67% coverage (355/355
  suites). Benchmark 4 multi-file tasks: 4/4 (100%) on `duo_core`, `duo_lean`,
  and `aider`; full 21-task run: 19/21 (90.5%) across all three, 95.2% KV cache
  hit rate (`net_tokens` 64,072 on `duo_core` vs. 62,092 on `duo_lean`, median
  35.8 s vs. 52.7 s — `duo_core` stays permanent baseline; see
  `docs/audit/HANDOFF_2026_11_09.md`).

## G. Performance backlog (2026-11-09, open)

Measured baseline: `duo_core` is 19/21, 35.8 s median, 64,072 net tokens over
21 tasks (~3,051/task); 95.2% of the gross prompt is KV-cache hits. Aider is
19/21, 26.1 s, 20,360 net tokens. The remaining gap is **round count**, not
prefill. Benchmark caveat: external GPU load started around `feature_flatten`
in the 21-task run; its timings after that point are invalid and the failed
tasks must be retried without load. The clean 4-task multi-file run is valid.

| Prio | Lever | Expected gain | Status |
|---|---|---|---|
| 1 | Pre-seed working set for small tasks (≤4 files / <8 KB) so round 0 edits directly | -1 LLM round, -5..8 s, -600 net tokens/task | done: `build_preseed_context`; benchmark pending |
| 2 | Auto-test-on-edit: run mapped tests in the same tool turn as the edit | -1..2 rounds, -6..12 s/task | open |
| 3 | Compact old edit payloads in history (`[applied diff: path (+X/-Y)]`) | -15..25% net prompt growth in long runs | open |
| 4 | Sharpen `WOULD_DROP_SYMBOLS` error with the lost original line | fewer retries; protects `multi_config` | done: `Keep definition: …` hint |
| 5 | Selective test impact via testmap before full pytest | -30..70% test wall time in large repos | open |
| 6 | 35B planner / 4B coder routing or 35B single-model | quality on multi-file/real-repo tasks | open, needs benchmark |
| 7 | Windows Job Object sandbox for `run_bash` | process isolation/cleanup | open |
| 8 | UI diff review + one-click run revert | UX differentiator | done: `/git/diff` + checkpoint restore |

35B-specific (installed Hermes/Groot; **Qwen3.6 GGUF is not installed**, so
its profile is untested; Hermes 3.6 35B is a Qwen distill and the closest
available proxy):

| # | Lever | Expected gain | Status/blocker |
|---|---|---|---|
| 1 | Stop `llama_mlock` forcing `mmap+mlock` for >12 GB MoE | up to 4.7x decode (claimed measurement, verify) | open |
| 2 | Enable MTP for Qwen3.6-35B (Hermes already has it) | +50..55% decode (claimed) | blocked: GGUF missing |
| 3 | MoE ubatch 512/1024 instead of 256 | +24..55% prefill (measured on 35B) | open |
| 4 | Do not halve `--threads-batch`; only `--threads` | +20..35% prefill (estimate) | done: j5/j6 behavior checks |
| 5 | Use ~1.8 GB free VRAM via `n-cpu-moe` 29..31 | +15..25% (estimate) | open, medium risk |
| 6 | Skip vision projector on text-only runs | save ~0.8–1.3 GB VRAM | open; profile points to missing `mmproj-BF16.gguf` |
| 7 | Cap/disable thinking in 35B tool rounds | save 1k–2.5k decode tokens/task | open |
| 8 | Avoid 35B↔4B slot thrashing | save 15–35 s per switch | open |

Retry command for the load-contaminated failures:

```powershell
.venv\Scripts\python.exe -u bench/runner.py --only feature_parse_bool,feature_word_freq,fix_path_join,multi_config --conditions hivemind_duo,aider --no-duo-planner --aider-bin "C:\Users\NtheP\Desktop\HiveMind_bench_tools\aider\.venv\Scripts\aider.exe" --out bench/results/run-retry4-core-aider-2026-11-09.json
```
