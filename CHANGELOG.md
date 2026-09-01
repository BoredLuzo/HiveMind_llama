# Changelog

All notable changes to HiveMind are documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## History Note

Versions up to and including v1.1.4 were **private project-file changes**
without any release intent — things were wired up on demand, without changelog
or versioning discipline. Release intent started with v1: clean up, rewire,
pay down technical debt. This changelog starts **now** and only documents
deliberate changes/features, not internal patchwork.

## [1.0.1] - 2026-09-01

### Added

- `deploy/backup_data.py` — backup helper (CLI): backs up `sessions/`,
  `memory.json`, `soul.json`, `presets.json`, `settings.json`,
  `model_configs/learned/`, `learning_logs/` to `backups/<timestamp>/`.
  Missing sources are skipped. Supports `--dry-run` and `--out`.
- `LICENSE` (Business Source License 1.1) and `CHANGELOG.md` (this file) —
  the repo is now prepared for public release. Author: Luzo (BoredLuzo).
- Sandbox hardening for `run_bash`/`run_python`/`start_background`:
  the Windows Job Object now also caps job memory and active process count
  (settings: `duo_tool_sandbox_max_mem_mb`, `duo_tool_sandbox_max_procs`);
  on non-Windows, tool subprocesses start in their own process group and are
  fully terminated via `killpg` on timeout (instead of a bare `kill()`).

### Changed

- `Memory._persist()` now writes atomically (`write_json_atomic`) instead of
  `write_text` + `except: pass`; persistence errors are logged instead of
  swallowed.
- `_save_registry()` (server.py and core/state.py) now writes atomically
  (`write_json_atomic`).
- `HIVEMIND_AUTO_RESUME` removed — dead env var (set, never read) from
  `deploy/hivemind_windows.py` and `deploy/hivemind.service`; README note
  updated.
- server↔state consolidation: the 7 duplicates from `server.py`
  (`registry_get`/`registry_set`/`registry_all`/`_save_registry`/
  `apply_settings_to_pipeline`/`_ws_configure`/`_refresh_safe_profile_policy`)
  removed; `server.py` now imports the canonical implementations from
  `core/state.py` (the routers already used them). `registry_all` had diverged
  between the copies and is now unified. The backend config sync
  (`VRAM_BUDGET_GB`, `MOE_CPU_EXPERTS`, `LLAMA_BIN`, …), previously hidden in
  `server._refresh_safe_profile_policy`, now lives in
  `server._sync_backend_runtime_config()`.
- Config default conflicts fixed: fallback values in `core/duo/_pre_explore.py`
  aligned with `settings.py` DEFAULT_SETTINGS
  (`duo_pre_explore_timeout_seconds` 180→600, `duo_pre_explore_llm_timeout_s`
  300→600, `duo_pre_explore_max_files_est` 40→15,
  `duo_pre_explore_timeout_per_file_s` 15→20,
  `duo_pre_explore_ctx_char_ratio` 4.5→3.0).
- 7 hidden settings keys now explicit in `DEFAULT_SETTINGS`:
  `duo_coder_model`, `duo_critic_model`, `duo_caps`,
  `duo_pyright_path`, `duo_autolint_python_engine`, `read_guard_enabled`,
  `plan_tracker_classifier` (previously only implicit read fallbacks).

### Removed

- ~40 dead imports removed from `server.py` and `core/duo_runner.py`
  (verified via AST + occurrence analysis: `tempfile`, `shutil`, `base64`,
  `struct`, `hashlib as _hashlib`, `datetime`, `_parse_tool_args`,
  `_run_bash_failed`, `_fuzzy_resolve_path`, `_is_truncated`,
  `_build_fix_insight_sentence`, `_estimate_ctx_tokens`,
  `_make_tool_call_event`/`_make_tool_result_event`, `explore.cache` helpers,
  `_save_vision_model_cfg`, `_load_run_counter`, `run_pre_explore`,
  `run_chunked_coder`, `WorkspaceTransaction`, `ToolContextLRU`,
  `ExecutionController`/`AgentState`/`StopReason`,
  `update_model_capability_overrides`, `traceback as _tb`,
  `_get_thinking_profile`/`_calculate_thinking_tokens`, `_parse_tool_error`,
  `_SESSIONS_DIR`, `_try_resume`, `_tool_call_failed`/`_tool_error_has_code`/
  `_tool_error_response`, `_p2_alive`/`_port_alive_with_retry`). No behavioral
  difference — all names were provably unused (no re-export, no dynamic access,
  no side effects).
- `run_chunked_coder` stub (`raise NotImplementedError`) removed from
  `hive_functions/pre_explore.py` — had 0 callers.

### Added (Quick Wins)

- `GET /health` on the main app — returns
  `{status, version, llama_ok, model_count}` (llama_ok based on running
  llama slots).
- `requirements-dev.txt` (ruff, pytest).
- `docs/settings.md` — auto-generated from `settings.py` `DEFAULT_SETTINGS`
  (generator: `deploy/gen_settings_docs.py`); also covers the 7 previously
  hidden keys explicitly.
- Deterministic eval suites (no LLM needed), registered in the regression
  runner (14 → 17 suites):
  - `tests/test_tool_error_taxonomy.py` — format A/B parsing, code/tool
    matching, round-trip of the error taxonomy (`tools/errors.py`).
  - `tests/test_tool_arg_schemas.py` — schema integrity of all tool
    definitions, allowlist/subset consistency, minimal validator for
    required/type checks.
  - `tests/test_run_audit.py` — audit trail append, cap 40, load.
  - `tests/test_workspace_guards.py` — protected paths, workspace confinement,
    path normalization (`utils/file.py`).
  - `tests/test_tool_budgets.py` — websearch/install budgets
    (`tools/runner.py`, ContextVar-based).
  - `tests/test_ctx_budget.py` — session budget + CharCaps + ContextBudget
    tiers (`hive_functions/ctx_utils.py`).
  - `tests/test_dispatch_smoke.py` — central tool funnel offline:
    dispatch, write-outside-workspace block, TOOL_NOT_FOUND.
  - `tests/test_destructive_python.py` — destructive-Python gate incl. no
    false positives (complement to the existing bash-gate test).

### Fixed

- `tools/errors.parse_tool_error()` — the message of a format-B error no
  longer contains the `[TOOL_ERROR_META]` block (revealed by the
  `test_tool_error_taxonomy` eval); code/tool/matching unchanged.
- UTF-8 BOM removed from 5 files (breaks AST tooling):
  `backend/llama_client.py`, `backend/llama_server_manager.py`,
  `core/pipeline_runner.py`, `hive_functions/tree_scout.py`,
  `routers/config.py`.
- Mojibake fix of the 3 functional regex lines in `server.py` (double-encoded
  UTF-8): `tschüss` (greeting), `erzähl`/`erkläre mir kurz` (chat detection),
  `erkläre|beschreibe` (question detection) — real German now matches again.
  The rest (comments/strings) was deliberately left untouched.
- `_model_profile` consolidated: server.py now uses the canonical
  implementation `core/model_sampling._model_profile` (incl. user-config merge
  from `model_configs/models/*.json`, which previously only lived in the
  server copy); local duplicate definition + dead imports removed.
- README staleness corrected: `duo_coder_fallback_model` qwen3.5:4b → `4b-ud`,
  `duo_planner_thinking_budget` 4000 → `8000`, settings count ~195 → `189`
  (referencing `docs/settings.md`).

### Refactored

- **`_phase_pre_explore` (core/duo/_pre_explore.py, 2049 → 1393 lines) — sub-phases
  extracted** (mechanical, state-dict flow per the `_phase_vram` convention):
  - `_phase_pre_explore_bootstrap` (219): entry reads, resume check,
    workspace/tree-scout, follow-up hint, static-repo-map-only.
  - `_phase_pre_explore_cache` (79): pre-explore cache-hit (owns its guard).
  - `_phase_pre_explore_prepare` (185): setup (ctx/opts/budgets/msgs/tools).
  - `_phase_pre_explore_finalize` (329): static-map merge, success emits,
    unified evict, persistence, contract merge.
  - The risky core (try/finally + parallel/sequential explore, ~1100 lines)
    stays in the parent for now — separate follow-up (highest risk density).

- **run_pipeline (core/pipeline_runner.py, 818 → 764 lines)**: closures
  `_pin_pipeline_models`/`_unpin_pipeline_models`/`_role_vision` extracted as
  module functions; the 4 agent-round blocks stay (repetitive but with nuances —
  no mechanical unification without behavior change).

- **execute_tool_round: 32 → 16 parameters — `ToolRoundState` dataclass**
  (audit point "36 params / mutable-slot boxing"): the 17 mutable slots
  (`tool_ctx_lru`, `duo_deadline_at`, `verify_*_serial`, `last_too_large_path`,
  `attempts_per_file`, `tool_error_retries`, `call_sigs`, `recent_focus_paths`,
  `file_changes`, `duo_seen_web_queries`, `cached_coder_port`,
  `task_complete_blocked_count`, `total_tool_errors`, …) are passed through as
  `trs: ToolRoundState` (boxing identity preserved — duo_runner reads
  `trs.last_too_large_path[0]`/`trs.cached_coder_port[0]` after the call).
  The `core/duo_runner.py` call site builds the state object. Eval 26/26 unchanged.

- **execute_tool_round (core/tool_executor.py, 1169 → 472 lines): batch 3 — more
  helpers moved to `core/tool_exec_helpers.py`** (no behavior delta, eval 26/26
  unchanged):
  - `_handle_ask_user`, `_execute_one_tool` (web_search dedup + execution),
    `_maybe_activate_reactive_think`, `_run_bash_fail_fix_pass_insight`,
    `_patch_file_fallback_hint`, `_read_required_and_python_hints`,
    `_unknown_error_hint`, `_track_file_changes` (git auto-diff),
    `_register_context_lru`.
  - U4 (6-error saturation with `break`) stayed correctly in the orchestrator.

- **execute_tool_round (core/tool_executor.py, 1169 → 783 lines): helper extraction
  into `core/tool_exec_helpers.py`** (no signature change, no behavior
  delta):
  - `_prefetch_readonly_tools` (parallel prefetch), `_warn_duplicate_write_targets`,
    `_track_focus_path`, `_note_successful_write`, `_update_read_ladder`.
  - `_handle_too_large` (SPLIT-REQUIRED) + `_inject_tool_error_hints` (the
    21-branch error-hint chain, returns `(dresult, matched)`); `task_complete`-
    and unknown-error branches stay inline, now nested in the else branch.
  - `_RECOVERY_PHRASES`/`_recovery_saturated` + `_SYS_PREFIX` moved to the
    helpers (cycle-free).
  - New eval `tests/test_execute_tool_round.py` (26 checks, `_run_inline_tool`
    + hooks mocked) secures the core paths before/after — baseline and
    refactor identically green.

- **M3: `backend/llama_server_manager.py` (2406 → 166 lines) split into modules/mixins**:
  - `backend/llama_manager_utils.py` — 13 pure helpers/constants + 10 statics
    as module functions (`_probe_*`, `_kill_port_sync`, `_tcp_alive`, `_nm`,
    `_prefetch_key`, …).
  - `backend/llama_slots.py` — `ModelSlot` (144 lines) + slot logic.
  - 5 mixin classes: `manager_load.py` (adopt/startup/load/ensure_loaded/
    `_start_process`), `manager_evict.py`, `manager_process.py`,
    `manager_prefetch.py`, `manager_health.py` —
    `LlamaServerManager(LlamaLoadMixin, LlamaEvictMixin, LlamaProcessMixin,
    LlamaPrefetchMixin, LlamaHealthMixin)`; `__init__` + singleton stay.
  - Class-cache references in `_start_process` (`LlamaServerManager.X`, 26×)
    → `type(self).X` (behavior identical).
  - Re-exports remain importable (`VRAMPreFlightError`, `_available_ram_gb`,
    `resolve_model_path`); source-guard test
    `test_ensure_loaded_stale_port.py` now checks all manager sources.

- **M2b: `run_stream` (1065 lines) → `core/chat_run.py`** (1:1, import
  bridge). server.py: 2636 → **1573 lines**. `run_stream` still builds the
  RunContext (settings, presets, prefetch closures) and delegates to
  `run_stream_orchestrated`; server-specific names are resolved lazily via
  `from server import (...)` (52 names) — cycle-free because server is fully
  loaded at runtime. `pipeline`/`memory` access via `core.state`
  (AST-span-based replacements — string literals like `mode == "pipeline"`
  unchanged). Statically verified: no open free names (the rest are closure
  params/locals).

- **M2: server.py extraction** (module→module, mechanical; server.py
  3000→2636 lines):
  - `context/chat_util.py` (new) — `_make_messages`, `get_effective_prompt`,
    `_validate_preset_prompt`, `_extract_ws_query`, `_trim_query`,
    `_extract_memory`, `_auto_memory_from_input` (+ `_AUTO_MEMORY_PATTERNS`,
    `_RE_NUMBERED_LIST`); memory access now via `core.state.memory`.
  - `infra/security.py` (new) — CSRF/origin guard (middleware registration
    stays in server.py via `app.middleware("http")(_csrf_origin_guard)`).
  - `infra/log_noise.py` (new) — Uvicorn `_NoiseFilter` +
    `_install_uvicorn_noise_filter`.
  - `core/duo_helpers.py` — `_resolve_duo_runtime_profile`,
    `_resolve_duo_run_timeout_seconds`, `_bucket_stop_reason` (+
    `_DUO_RUNTIME_PROFILES`); settings access via `core.state`.
  - `tools/websearch.py` — `_get_websearch_timeout_seconds`, `_safe_web_search`,
    `_safe_web_fetch`; use `core.state` (settings/websearch availability).
  - All call sites unchanged (server.py re-imports the names; e.g.
    `peer_ratings` still gets `get_effective_prompt` from `server`).

- **M1a: `tools/handlers.py` → package `tools/handlers/`** (module→package,
  mechanical, 1:1): `file_ops.py`, `code_intel.py`, `exec_tools.py`,
  `git_tools.py`, `web_tools.py`, `linting.py`, `misc.py` + `_shared.py`
  (runtime state slots + `init_runtime_deps`). The facade `__init__.py`
  re-exports all 27 `_inline_tool_*` — imports from `tools/runner.py`,
  `core/direct_runner.py`, `server.py` stay unchanged. State access now via
  `_shared.` reference (late-binding as before via `global`).
- **M1b: `hive_functions/pre_explore.py` → package
  `hive_functions/pre_explore/`**: `contracts.py`, `llm.py`, `tooling.py`,
  `context.py`, `partition.py` (the 980-line `_explore_partition` block
  isolated), `runner.py`. The facade exports `run_pre_explore` — callers
  (`duo_helpers`, `core/duo/_pre_explore`) stay unchanged.

### Fixed

- **Pre-existing bug** (exposed by the package test, confirmed in the stage
  copy): `_inline_tool_git_commit` referenced `workspace_lock` instead of the
  parameter `_workspace_lock` → `git_commit` always crashed with NameError as
  soon as Git was available. Parameter reference corrected.

### Infra

- Repo state mirrored into a staging copy (robocopy /MIR) — beta and stage are
  byte-identical (3983 files, verification: file list + sizes + `import server`
  + 22/22 regressions in the clone). Stage leftovers (`index.html.bak_20260831`,
  `routing_weights.json`, `tests/test_ask_user_queue.py`) removed since they no
  longer exist in beta.

## [1.1.4] - Before Release Structure

Internal file changes without changelog discipline (private project phase).
