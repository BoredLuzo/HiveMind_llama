# Changelog

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

## [1.1.4] - before the release structure

Internal phase before the public repo layout, no changelog discipline.
