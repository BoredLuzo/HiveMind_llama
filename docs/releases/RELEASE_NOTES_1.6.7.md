# Release 1.6.7

Large-repo exploration (400+ files), Agentic continuity, repo-map quality, and
downloader cleanup release: adds directory-wide `get_signatures`, multi-file
batch `read_file(paths=[...])`, paginated `find_files`, Direct-mode sliding
compaction, and unread coverage tracking across compressions while fixing the
~30-file `PlanTracker` exploration stall; keeps multi-turn Agentic chat history
intact; lets `task_complete` checkpoint subtasks without ending the run; emits
real parameter/return/line signatures across Python/JS/TS/C++ in the static
repo-map; replaces the cross-file Read-Ladder hint with an exact same-file
read-loop abort; brings Action Approval & active workspace awareness to Direct
Mode; and streamlines the model downloader to 9 models featuring
`Nemotron-3.5-Bolt-E4B` `GSQ-RCO-IQ4_XS` (+ DFlash + Vision).

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or extract the release zip next to your old install —
`settings.json`, `models.json`, `sessions\`, `logs\`, `voice_models\`, `stt\`,
and `.venv` are kept).

## Large-Repo Exploration (400+ Files) & Guard Fixes

- **Directory `get_signatures`, batch `read_file`, and paginated `find_files` (`hive_functions/hivemind_feature/ast_tools.py`, `tools/handlers/code_intel.py`, `tools/handlers/file_ops.py`, `tools/definitions.py`):**
  - `get_signatures(path=".", offset=0)` now outlines whole directory trees within the active read budget (secret-filtered, paginated via `offset`), scanning hundreds of files structurally in a few calls.
  - `read_file(path=..., paths=[...])` batch-reads up to 20 files in one call within `read_char_cap` (registering read-guard & coverage stats and deferring overflow paths cleanly), and `find_files` adds `offset` + `limit` (up to `500`).
- **30-file `PlanTracker` stall & Agentic/Direct exploration continuity (`core/plan_tracker.py`, `core/duo_runner.py`, `core/duo_ctx_meter.py`, `core/duo_compress_cycle.py`, `core/tool_executor.py`, `core/tool_guards.py`, `core/direct_runner.py`):**
  - Fixes `PlanTracker` escalating non-destructive read/search/signature calls on other repo files to hard `DEVIATES` (`[CRITICAL STALL]` after ~30 reads).
  - Resets `_dr` on compression and active observation masking, releases masked paths from `_seen_explore_paths`, extends run deadlines by `+240s` every 10 newly read distinct files, tracks `[EXPLORATION PROGRESS: X/N workspace files read; M unread remaining]` across Agentic compressions (up to 2,000 files), and adds deterministic sliding tool-context compaction to Direct Mode.

## Agentic Continuity, Checkpoints & Loop Safety

- **Continuous multi-turn Agentic chat history (`static/js/voice.js`, `context/chat.py`, `core/duo_runner.py`, `core/duo_epilogue.py`, `core/duo_dtool_setup.py`):**
  - Preserves `task_complete` summaries in saved chat history and recovers them automatically from persisted HTML/HIVEBLOB1 blobs for existing chats, preventing follow-up Agentic runs from seeding an empty history.
  - Carries recent conversation context into pre-explore bridges and populates Duo session fallbacks when runs finish cleanly via `task_complete`.
- **Subtask checkpoint continuation in `task_complete` (`tools/definitions.py`, `tools/handlers/misc.py`, `core/tool_executor.py`, `hive_functions/prompts.py`):**
  - Clarifies that a normal `task_complete` ends the entire coding task and adds an optional `more_work_remaining=true` parameter (also triggered by `build_status: "in_progress"|"partial"`) to log a subtask checkpoint (`[task_complete: CHECKPOINT - continuing run]`) while keeping the run open.
- **Same-file read-loop abort replacing Read-Ladder (`tools/limits.py`, `core/tool_exec_helpers.py`, `core/tool_executor.py`, `core/duo_runner.py`):**
  - Removes the cross-file `[READ LADDER]` hint so reading many distinct files (including after compression) or sequential line ranges of large files never nags the model.
  - Replaces it with per-`(path, start_line, end_line)` repeat tracking (including `[SKIP: already read]` loops) that aborts cleanly as `loop_detected` at 5 repeats and resets on writes, edits, tests, bash, or compression.

## Static Repo-Map & Direct Mode

- **Full signatures & JS IIFE/prototype/C++ extraction (`hive_functions/static_repomap.py`, `hive_functions/hivemind_feature/ast_tools.py`):**
  - Emits compact parameter lists, return types, class prefixes, exports, and `@L` line numbers instead of bare symbol names.
  - Adds support for JS/TS IIFE modules, `var X = function(...)`, `X.prototype.m = ...`, arrow functions, namespace/module exports, and C/C++ `function_declarator` unwrapping, and fixes Python variable header line-bleed in `get_signatures`.
- **Direct-mode Action Approval & active workspace prompt (`index.html`, `static/js/settings_io.js`, `core/direct_runner.py`):**
  - Automatically shows the Action Approval and Auto-answer `ask_user` toggles inside *Direct Chat Tools* whenever Direct/Auto mode selects `Python` or `Full` tier (> `MCP`).
  - Includes `Active workspace: <path>` in Direct tool-capable prompts so models know their mounted workspace folder and write permissions.

## Installer & Models

- **Streamlined 9-model downloader & Nemotron `GSQ-RCO-IQ4_XS` (`deploy/fetch_models.py`, `deploy/apply_model_defaults.py`, `deploy/recommend_models.py`, `backend/llama_models.py`, `model_configs/models/nvidia-nemotron-3.5-bolt_e4b.json`):**
  - Switches `nvidia-nemotron-3.5-bolt:e4b` to `abenzerps/Nemotron-3.5-Bolt-E4B-GSQ-RCO-GGUF` (`Nemotron-3.5-Bolt-E4B-GSQ-RCO-IQ4_XS.gguf`, ~4.8 GB) with `Nemotron-3.5-Bolt-E4B-DFlash-Q8_0.gguf` and `mmproj-Q8_0.gguf` (`mmproj-nvidia-nemotron-3.5-bolt-e4b-q8_0.gguf`, Vision enabled).
  - Removes `air:26b-a4b`, `twil:8b`, and `minicpm5:2b-sharp` from the downloader catalog (existing shipped profiles in `model_configs/models/` remain intact), condensing the Essential Starter Set to 3 models (~8.6 GB: `gemma-4:e4b-it-qat`, `qwen3.5:4b-mtp`, `lfm3:1.7b-qad`).
