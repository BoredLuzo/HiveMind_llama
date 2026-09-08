# Changelog

All notable changes to HiveMind are documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.13] - 2026-09-08

### Added

- **`duo_compress_local_only` setting** (compression block, default `false`):
  skips the compression LLM summary POST entirely and builds the instant local
  fallback summary instead. For hardware where that call routinely hits the
  read timeout (MoE with CPU experts, slow prefill) it removes ~3 min of dead
  time per compression — and the local summary is more cache-friendly, since
  it only lightly edits the summary message instead of rewriting it (live
  run 2026-09-08: LLM summary → 0 % reuse, local summary → 33 %+). The light
  compressor model load and the mini-shrink retry are also skipped in this
  mode. Combine with `duo_partial_compression: true` to keep a byte-identical
  raw tail (KV-shift cache reuse).
- **Linux/POSIX support for the llama.cpp backend**: `gpu_backend` now accepts
  `cpu` (env `HIVEMIND_GPU_BACKEND` / settings), binary discovery is
  platform-aware (`llama-server` without .exe, POSIX: backend fit outranks
  build age), RAM via `/proc/meminfo`, port-kill chain `fuser` -> `/proc`
  inode scan -> `pkill`, backend DLL probe checks `.so` on POSIX and passes
  for the cpu backend.
- **CPU backend load path**: `--n-gpu-layers 0`, no `--device`, MoE-offload
  and device/DLL gates skipped, VRAM pre-flight short-circuited (`cpu-backend`
  source) and `wait_for_vram_reclaim` returns immediately; thread defaults are
  CPU-count-aware on POSIX (Windows keeps the 16/8 pair).
- `deploy/fetch_llamacpp.py` downloads Linux assets (`llama-bXXXX-bin-ubuntu-
  {vulkan,cpu,rocm}-x64.zip|.tar.gz`), extracts tar.gz, chmods the binary;
  ROCm asset regex accepts versioned names (`rocm-10.0-x64`).
- POSIX tool-command ladder (`hive_functions/language_config.py`): `python`
  -> `python3`, PowerShell pipe cmdlets -> `head`/`tail` at import time.
- Full `deploy/install_linux.sh` (system deps, venv, llama.cpp fetch, systemd)
  and `deploy/hivemind.service` backend env; README Linux quick start +
  "Linux — Details" section.
- New regression suite `tests/test_linux_paths.py` (POSIX discovery, meminfo
  parser, /proc port parser, cpu gating source guards).

- Model config `hermes3.6_35b-a3b-uncensored-genesis-v13-mtp-apex-compact.json`
  (mirror of the v12 MTP config: same ctx values and sampling, `mtp: true`) for
  the Hermes3.6 V13 APEX-Compact GGUF; verified via `resolve_model_path` and
  the MTP registry lookup.
- Setting `llama_ubatch_size` (default `256`) + backend seam
  (`server._sync_backend_runtime_config` -> `backend/llama_config.py`
  `LLAMA_UBATCH` -> `manager_load.py` `--ubatch-size`): prompt-processing
  chunk size is now tunable without code edits. Larger values (512/1024)
  speed up prefill for MoE models with CPU expert offloading at the cost of
  a larger VRAM compute buffer — the test lever for the slow
  post-compression re-prefill (~17 tok/s observed on Vulkan MoE).

### Fixed

- **llama.cpp installer never accepted the downloaded DLLs** (release
  blocker found during clean-install testing): `_verify_backend_dlls` in
  `deploy/fetch_llamacpp.py` checked for `ggml-vulkan` / `ggml-cuda`
  WITHOUT the `.dll` extension on Windows (`_so` mapping was wrong), so
  every Vulkan/CUDA install failed at step 4 and deleted the intact build.
  Now checks the correct extension via directory enumeration (robust while
  antivirus scans fresh files), retries for up to ~90 s before failing,
  cross-checks the source archive to distinguish a real incomplete ZIP
  from a quarantined DLL (archive + build are kept, with restore
  instructions), and the downloader no longer fails with HTTP 416 when the
  temp file is already fully downloaded.
- **Per-model config `sampling` block is now applied at runtime**
  (`model_configs/models_registry.py` accessor `get_sampling`,
  `core/model_sampling.py` registry fallback): the documented `sampling` block
  in `model_configs/models/*.json` (used by the shipped qwen3.6/hermes3.6
  APEX-Compact configs) previously had no effect. The
  `_model_sampling_overrides` settings seam still wins when provided.
- **Documentation audit against the implementation**: README no longer claims
  that `Auto` is the default run mode (`mode` defaults to `simple`), that
  AutoMap is Judge-driven (it is a heuristic scorer), that the `[C]ustom`
  wizard collects the `sampling` block, that `GITHUB_TOKEN` is honored by
  `deploy/fetch_llamacpp.py` (never read), the `SEARXNG_SETTINGS_PATH`
  mechanism (the config is baked via `COPY`), or the non-existent
  `--no-vulkan` flag (CPU fallback is an automatic `--n-gpu-layers 0`
  retry). Tool table now lists `get_datetime`, `hivemind_pipeline` and the
  browser `close` action; tool-tier values (`off`/`readonly`/`python`/`full`)
  are documented.
- `direct_tools_tier` accepts the alias `websearch` → `readonly` (a literal
  `websearch` value previously disabled all tools).
- `docs/settings.md` regenerated (197 keys; stale at 189).
- **Four reachable NameError crashes fixed** (found via the new ruff gate):
  missing `import re` in `core/duo/_pre_explore.py`, undefined
  `RE_THINK_CLEANUP` in `core/duo_helpers.py`, missing
  `save_learned_config` import in `learning/peer_ratings.py`, and a missing
  `llm_read_timeout` parameter that the pre-explore workers never received
  (`hive_functions/pre_explore/partition.py` / `runner.py`).
- **Lint guardrails**: `ruff.toml` (correctness rules: E9, F821, F601, F811,
  F841, W605) enforced by the new `tests/test_lint.py` suite, plus
  `tests/test_no_new_silent_excepts.py` — an AST counter with a frozen
  baseline (`tests/lint_baseline.json`) that fails when a file gains new
  `try/except: pass` or blind `except Exception` handlers.
- User-visible strings (SSE status, model-load errors, RuntimeError
  messages) translated to English across core/, backend/, hive_functions/,
  tools/ and `static/app.js`; functional German intent keywords (read-only
  detection, chat intents, MCP aliases, vision triggers) are intentionally
  kept.

- **Compression threshold floor lowered below the HTTP-400 overflow zone**
  (`duo_compress_auto_floor` default `0.78` -> `0.70` in `settings.py` and the
  `core/duo_runner.py` fallback): live logs (2026-09-07) show the request
  overflow zone starts at ~72-73% of ctx (prompt + clamped `max_tokens` +
  template overhead), i.e. below the old threshold — every cycle escalated to
  `trigger=force` (6 compressions in 50 min), each costing a full cache-losing
  re-prefill at ~17 tok/s prompt eval (Vulkan MoE). `0.70` fires a planned
  compression clearly before that zone.
- **Silent CPU fallback no longer applies to coder-class models**
  (`backend/manager_load.py` `exit=-1` retry path): the VRAM threshold for the
  one-time `--n-gpu-layers 0` fallback was lowered from 5.0 GB to 3.0 GB. The
  coder (~4.9 GB) sat just under the old value, so a post-eviction
  Vulkan-timing failure could reload it fully on CPU (~1-2 tok/s for the rest
  of the run) instead of failing loudly. Only genuinely small models (light
  compressor ~2.1 GB) still qualify for the CPU fallback.

### Changed

- All comments, log texts, UI status messages and model-facing hints from the
  2026-09-06/07 context-flow work translated to English (line-stable
  replacements; regression source-guard needles updated accordingly).

### Removed

- Dead setting `automap_mode` — defined and documented, but never read by
  any code.

### Tests

- Registered 5 previously unregistered regression suites
  (`auto_split_pending`, `ctx_guard`, `planner_model_persist`,
  `read_only_detect`, `tool_arg_compact`) plus the two new guardrail suites
  (`lint`, `no_new_silent_excepts`) in `tests/run_regressions.py`, and
  extended `test_models_registry.py` with sampling-override coverage.

## [1.0.12] - 2026-09-06

### Added

- **Central token estimator calibration at 3.0 chars/token** (`utils/token.py`
  `CHARS_PER_TOKEN`): the context guard, UI meters and compression notices now share
  one constant; char-based conversions in `hive_functions/ctx_utils.py`,
  `context/ctx_guard.py` and the pre-explore KV budget bind to it. The UI compression
  notice shows rounded integers (no more `22284.5714… est. tokens` artifacts).
- **Dynamic output reserve** (`context/ctx_guard.py` `clamp_request_max_tokens`): each
  tool round clamps `max_tokens` so `prompt + output <= ctx` even when the agent's
  output budget is large. The compression threshold is therefore floor-driven again
  (`duo_compress_auto_floor`, default `0.78`) instead of binding to the full
  `max_tokens` reserve (which previously pushed the trigger down to ~67 % on 40k ctx).
- **Compression on a light model** (`duo_compress_model`, default `lfm2.5:2.6b`):
  summaries run on a small model when it fits without evicting the coder (VRAM
  `can_fit` guard), otherwise they fall back to the coder model. Read timeout is
  configurable via `duo_compress_llm_timeout_s` (default `180`) to avoid the 120 s
  `ReadTimeout` fallback-summary chain.
- **`ling-3.0-tiny` as a recommended low-resource coder** (InclusionAI hybrid MoE,
  7.9B total / 1.3B active): registry config (`model_configs/models/ling-3.0-tiny.json`,
  `reasoning: on`), downloader entry (`deploy/fetch_models.py`, `bartowski` Q4_K_L),
  `setup_models.bat` menu item and README Recommended Model Set row.

### Changed

- **Recommended Coder/Hermes agent model switched to `qwen3.6:…-genesis-final-apex-compact`**
  (Qwen3.6 Genesis Final APEX-Compact, LuffyTheFox, ~17 GB): replaces the
  hermes3.6 Genesis V12 downloader spec (`deploy/fetch_models.py`), the
  `setup_models.bat` menu entry and the README Recommended Model Set row. The
  per-model config now ships as
  `model_configs/models/qwen3.6_…_genesis-final-apex-compact.json` (MTP head +
  35 CPU experts, same launch/sampling settings as the old V12 config); the
  vision projector follows as `mmproj-Qwen3.6-35B-A3B-Uncensored-Genesis-F16.gguf`.
- **Compression threshold is floor-driven with a dynamic output reserve**: the old
  `min(P1, ctx − max_tokens − reserve)` behaviour no longer forces compression at ~67 %
  when the coder agent uses a 12000-token output budget; compression now triggers at
  the configured floor (~78 %) while per-request `max_tokens` is clamped to the free
  context (plus estimator headroom) so single generations cannot overflow.
- **New default `duo_compress_auto_floor = 0.78`** (settings.py; per-instance values in
  `settings.json` still win). `ctx_guard.py` module default stays `0.72` (pure-function
  tests unchanged).

### Fixed

- **Compression notices show integer estimates** instead of raw float `chars/3.5`
  values in the UI status text.

## [1.0.11] - 2026-09-04

### Added

- **Cache-friendly context management for long agentic runs** (`context/ctx_guard.py`,
  `core/duo_runner.py`): llama.cpp's prefix cache stays alive between compressions.
  The effective auto-compression threshold is now `min(auto_floor·ctx, ctx − reserve)`
  (UI `duo_compress_threshold > 0` stays an exact token override), and in-place
  recall-marker eviction is used only as an emergency (>90 % context when no
  compression is left). New settings: `duo_cache_friendly_ctx`, `duo_partial_compression`,
  `duo_compress_auto_floor`, `duo_compress_overflow_reserve`, `duo_min_free_ctx_tokens`,
  `duo_max_compressions`.
- **Partial context compression behind a feature flag** (`duo_partial_compression`,
  default off): only the older messages up to a computed cut are condensed while the
  recent tail stays byte-identical at the end, so llama.cpp's KV-shift reuse can
  salvage the suffix after a rebuild. `full` stays the fallback.
- **Cache reuse telemetry** (`core/agentic_tool_loop.py`): logs `[CACHE] prompt/cached/
  reuse%` per coder round, plus compression mode and rule-fallback counters.
- **`deploy/analyze_cache_log.py`**: A/B analyzer over `logs/hivemind.log` (reuse%
  trajectory, cumulative re-prefill, compressions, emergency evictions, optional CSV).
- **`deploy/ab_phase1.ps1`**: switches `duo_cache_friendly_ctx`/`duo_partial_compression`
  live on the running server (fallback: settings.json) and snapshots the log for A/B runs.

### Changed

- **Compression is the primary shrink; in-place eviction is the exception**: between two
  compressions the tool history is append-only, so `[CACHE] reuse` stays ~100 % and long
  runs no longer pay a full re-prefill every round at 78–90 % context.
- **Rule-based compression fallback**: if the LLM summary fails validation, a deterministic
  pass replaces old tool outputs with recall markers and strips stale notices instead of
  keeping the full context; the run only stops after repeated rule-fallback failures.
- **AUTO-SPLIT continuation marker detection is robust**
  (`tools/handlers/file_ops.py`): the marker is accepted with surrounding quotes/backticks
  or in short replies — models that copy `content="<AUTO_SPLIT_CONTINUE>"` no longer write
  the marker literally into the file. A pending remainder is drained on the next append and
  previously literal marker lines at the file end are healed before the remainder is written;
  pending keys are path-canonicalised; a stale pending remainder is dropped when real content
  is appended. Marker-without-pending returns a clear error instead of a literal write.
- **Presets save and load the coder context deterministically** (`routers/config.py`,
  `static/app.js`): the preset endpoint accepts an overlay body (allow-listed
  `duo_coder_ctx_agentic/until_finished/normal`, `duo_planner_ctx_target`) and the UI commits
  + flushes the ctx fields before saving, so a quick "Save" no longer races the debounced
  settings POST. Loading re-evaluates the until-finished / planner-coder mirrors.
- **`models_dir` is never stored into or restored from a preset** and the safe-profile matrix
  no longer overwrites an explicitly set `duo_coder_ctx_*` — presets/policy can no longer
  reset your model folder or context.
- **Compression HTTP timeouts raised** (`context/compression.py`: connect 10 s, read 120 s) to
  avoid pointless fallbacks on slower MoE/CPU-offload setups.
- **UI**: "Compression Limit" label and help text updated to English and to the new auto
  behaviour; `app.js` is cache-busted (`?v=20260904-2`) so UI fixes actually reach clients.
- **Context compaction of executed write tool calls** (`core/tool_executor.py`): after a
  successful oversized `write_file`/`write_file_append`/`edit_file`/`patch_file`/
  `replace_lines`, the still-unsent assistant tool-call arguments (>12k chars) are replaced
  with a small stub (`path` + arg chars + sha1 prefix) - the message was never part of a sent
  prompt, so llama.cpp's prefix cache is not invalidated and ~20-30k tokens of content no
  longer occupy the context window until the next compression.

### Fixed

- Oversized `write_file` + `write_file_append` with a quoted `<AUTO_SPLIT_CONTINUE>` marker
  no longer truncates files by writing the marker as literal content.
- Preset coder context no longer resets to the safe-profile default after a page reload.
- Long runs at high context no longer degrade into per-round full re-prefills
  (`[CACHE-MISS]` / `[MSGSIG-CHANGE]` churn) in the cache-friendly mode.
- Post-run insight extraction / skill distillation no longer stalls up to 240 s by loading a
  new small model while the evicted big model still holds RAM (`learning/insights.py`): when
  available RAM is low the currently loaded model is reused instead, and extraction is skipped
  entirely if no model is loaded.
- `read_file` aborts oversized full reads early (`tools/handlers/file_ops.py`): a binary sniff
  on the file head plus a streaming newline count (stops at line 401) replace reading the whole
  file into memory just to answer `FILE_TOO_LARGE_NEED_RANGE` with the file outline.
- UI: a transport-level stream drop ("Error in input stream") is no longer shown as a red
  model error - aborts/connection drops get a neutral "run stopped / possibly parked" notice
  (`static/app.js`).
- A "skip" pressed while the planner is running now aborts the planner and starts the coder
  directly (`core/duo_runner.py`) instead of letting a long planner finish and then silently
  ending the run with zero coder output ("Coder 0s"). A skip on a single non-chunk round is
  ignored so it can no longer complete a coding run without doing any work.

## [1.0.10] - 2026-09-04

### Changed

- **Chat template updated to qwen3.8-froggeric v22.5** (`model_configs/chat_template22.5.jinja`,
  shipped under the versioned name so users can tell the template version at a glance). The
  previous `model_configs/chat_template22.4.jinja` is removed; the per-model configs
  (`qwen3.5_0.8b-ud`, `qwen3.5_2b`, `qwen3.5_4b-ud`, `qwen3.5_9b-ud`, `qwen3.6_35b-a3b-ud`)
  and the `hermes3.6` code fallback (`backend/manager_load.py`) now point at
  `chat_template22.5.jinja`, so qwen (3.5/3.6) and hermes3.6 all use the new template.
  The repo fallback copy `model_configs/qwen3.6_chat_template.jinja` is kept in sync.

## [1.0.9] - 2026-09-03

### Added

- **Server-side AUTO-SPLIT for oversized writes** (`tools/handlers/file_ops.py`):
  when a single `write_file` / `edit_file` full-rewrite / `write_file_append`
  call exceeds the per-call char limit, the leading chunk is written immediately
  and the REMAINDER is cached server-side. The model finishes the file with one
  tiny call `write_file_append(path, content="<AUTO_SPLIT_CONTINUE>")` — no
  regeneration of the content, no "7-minute total loss" after an oversized call
  is rejected. The previous existing-file full-rewrite error
  (`EDIT_FILE_CONTENT_TOO_LARGE`) is replaced by this split. Pending entries
  expire after 5 minutes; a stale/missing marker returns a clear fallback error.
- **Active preset is auto-loaded at startup** (`server.py`): the preset that was
  last explicitly Loaded is applied again on boot (`[PRESET] startup auto-load`),
  so the full configuration — models incl. `duo_planner_model` + planner
  context, options, mode, prompts — survives a restart without a manual reload.
- **Read-only detection understands content constraints**
  (`core/duo_helpers.py`): `is_read_only_request()` only treats a request as
  read-only when a read-only phrase is present AND there is no (non-negated)
  implementation intent. Phrases like "DO NOT MODIFY" inside a build task no
  longer disable the tool round. In read-only / text mode the coder receives an
  explicit note forbidding textual tool-call syntax (`[TOOL_CALL] …`,
  `edit_file<argkey>…`).

### Changed

- **Coder WRITE RULES + dynamic char cap injected into the system prompt**
  (`core/duo_runner.py`): the exact per-call limit (resolved via
  `resolve_write_char_limits`, the same source the runtime tool check uses) and
  the `<AUTO_SPLIT_CONTINUE>` continuation are spelled out before generation.
- **Tool schemas advertise the write cap** (`tools/definitions.py`):
  `maxLength: 20000` on the `content`/`edits` of `write_file`,
  `write_file_append` and `edit_file`; descriptions document the AUTO-SPLIT
  marker.
- **`write_file_append` appends received content in one go**: no artificial
  splitting that would force the model to re-send the same content.

### Removed

- **Tiel-Coder model recommendations and their default configs dropped**:
  removed from the recommended-model downloader (`deploy/fetch_models.py`),
  `setup_models.bat`, the README model table and the shipped per-model configs
  (`model_configs/models/tiel-coder_*.json` deleted) together with their launch
  defaults (MoE expert counts, MTP registration, sampling profile, VRAM table,
  vision/mmproj base lists). The models are no longer advertised or
  auto-configured; the repo history (CHANGELOG) keeps the original entries.

### Fixed

- **Context compression crash** (`context/compression.py`): the empty-history
  early return handed back a 2-tuple while the caller unpacks 3 →
  `ValueError: not enough values to unpack (expected 3, got 2)` during the
  second compression; the error was mis-stamped as `loop_detected` and
  hard-stopped otherwise healthy runs. The early return now yields a 3-tuple.
- **Planner model lost on every settings reload** (`settings.py`): the loader
  forced `duo_planner_model = None` unconditionally, wiping a user/preset-
  selected planner model on restart (the planner silently fell back to the
  coder model). The stored value now persists across reloads and presets.

## [1.0.7] - 2026-09-02

### Added

- **Tool-Loop Bash-Reinjection** (`core/tool_executor.py`): when the loop
  detector catches repeated `run_bash` calls (3× identical, ABAB or ABCABC
  pattern), the run is **no longer aborted**. Instead the full toolset that is
  actually available in the current tool phase is re-injected as a menu message
  and the loop signature is reset, so the model can pick a different tool
  (e.g. `read_file`, `run_tests`, `browser`) instead of retrying the same shell
  command. Non-`run_bash` loops (e.g. repeated `read_file` ABAB) keep the
  previous abort behaviour. The round budget still bounds the loop.
- **Tiel-Coder 35B-A3B MTP models by
  [BoredLuzo](https://huggingface.co/BoredLuzo) are now recommended**
  (`deploy/fetch_models.py`, `setup_models.bat` options `9` and `10`): the two
  self-quantized GGUFs `Tiel-Coder-35B-A3B-MTP-Compact.gguf` (~18 GB) and
  `Tiel-Coder-35B-A3B-MTP-APEX.gguf` (~26 GB). Per-model start-configs ship as
  `model_configs/models/tiel-coder_35b-a3b-mtp-compact.json` (MTP head,
  32 CPU experts) and `model_configs/models/tiel-coder_35b-a3b-mtp-apex.json`
  (MTP head, 35 CPU experts), so once a GGUF is registered/auto-detected the
  launch settings apply automatically. Tiel-Coder follows the upstream author's
  guidance ([peculiar-ragdoll](https://huggingface.co/peculiar-ragdoll)):
  launch with `--jinja` + the GGUF's own embedded chat template (not the qwen
  custom templates), `-ngl 99` style full offload, sampling temperature 1.0 /
  top_p 0.95 / top_k 20 (0.6 for agentic coding), and vision via its own
  shared `mmproj-BF16.gguf` projector (Ornith-1.5 vision tower, original BF16,
  untouched by quantization).

### Fixed

- **safe-profile policy never overwrites the user's saved `vram_budget_gb`**
  (`hive_functions/safe_profile_policy.py`): `vram_budget_gb` is a
  user-preference key, so `apply_safe_profile_policy()` no longer forces the
  matrix default (7.5) back over the value the user saved on every settings
  reload. The policy default is informational only; the user's saved budget
  persists (USER-WINS, same philosophy as the existing agent-model
  CARDS-WIN logic).

## [1.0.6] - 2026-09-02

### Changed

- **MoE CPU experts dropdown lists only installed models** (`static/app.js`):
  it previously offered every MoE model from the defaults table regardless of
  whether it was actually present on this machine. The list is now built from
  the installed/configured models only, and `-ud` variants are stored/read via
  the backend-compatible key (so an override set for `…-ud` really applies at
  load time instead of silently being ignored).

### Added

- **Hermes3.6 Genesis V12 MTP-APEX-Compact is a recommended downloader model
  again** (`deploy/fetch_models.py` spec, `setup_models.bat` list): the GGUF
  (`Hermes3.6-35B-A3B-Uncensored-Genesis-V12-MTP-APEX-Compact.gguf`, ~17 GB)
  plus its `mmproj` are fetched from Hugging Face like the other recommended
  models, and the per-model config
  (`model_configs/models/hermes3.6_…_v12-mtp-apex-compact.json`) ships with the
  release so the MoE/MTP launch settings apply.
- **Optional Desktop shortcut for end users** (`create_shortcut.bat`, new
  `[7/7]` step in `install.bat`): the installer can now create a
  `HiveMind.lnk` on the user's Desktop (OneDrive-safe resolution) that starts
  `start_hivemind.bat` with the HiveMind icon (`static\favicon.ico`). The
  helper can also be run manually — optionally with a custom shortcut name as
  argument (`create_shortcut.bat "HiveMind v1.0.6"`).

## [1.0.5] - 2026-09-02

### Added

- **Presets are back** (user-config snapshots): save/load/delete named
  configurations incl. per-agent prompts (Prompt Editor), shown in a dedicated
  "Presets" tab. **Load replaces the whole configuration — and only on an
  explicit click.** There is no auto-load at startup and no preset override of
  model/context/behaviour in the agentic coder anymore; `git_token` is never
  stored in a preset.

### Changed

- **Agentic/Duo context is now user-controlled**: the per-agent **Context**
  setting (agent card → `ctx_overrides`) is honoured in the duo/agentic paths,
  and the 16k agentic floor is only a *minimum* — a larger model/role default is
  used when no override is set. The agent-card context slider now goes up to
  128k.
- **Websearch status** no longer lists the search engines in the UI.
- **`pip` installs** (`install_package` and the Python language install) now use
  `python -m pip`, so a stale `pip.exe` (e.g. a leftover Python 3.11 script)
  cannot fail silently with `exit 1` on Windows.

### Fixed

- Removed the stale "History Note" from the changelog header.
- Duo/agentic context now honours a user-set per-agent **Context** override
  (`ctx_overrides`) even when a persisted `duo_*` context default exists —
  raising the context actually takes effect now.
- llama.cpp updater now fetches the **newest `bXXXX` nightly** directly from the
  releases list (the stable `nightly-tag.txt` pointer lagged ~100 builds).
- Duo context inputs (**Context Coder (Agentic) / Normal / Planner**) persist
  immediately while typing (`oninput`/Enter) — a typed value without blur/Enter
  is no longer lost. The effective context is logged at run start
  (`[CTX-EFFECTIVE] …`).
- Each run now also sends the context chosen in the frontend **with the
  `/stream` request** and applies it to that run — the displayed/used context
  can no longer silently stay on a stale default (e.g. 16k).
- Fixed `NameError: workspace_lock is not defined` in the tool loop
  (`_inject_tool_error_hints` used `workspace_lock` without the parameter),
  which previously crashed every tool round that hit an
  `EDIT_FILE_NO_BLOCKS_APPLIED` hint and was then auto-stopped as a loop.
- **Tool-thinking is off by default** (`duo_coder_tool_thinking_auto_mode` now
  defaults to `"off"` instead of `"on_fail"`) — no automatic thinking for tool
  calls unless the user enables it.
- **MoE CPU experts dropdown** now shows the full model key (no more short,
  ambiguous/duplicate labels like several `hermes3.6` variants).
- **llama.cpp updater** no longer dies with a raw traceback inside the
  installer — transient failures now print a clear message with retry and
  manual-install instructions.
- **llama.cpp CUDA nightlies (b10760+)** split the CUDA runtime into a separate
  `cudart-llama-bin-win-cuda-<ver>-x64.zip`. The updater now downloads that
  extra zip too and places the runtime DLLs next to `llama-server.exe`, so the
  CUDA build installs successfully again.

## [1.0.4] - 2026-09-02

### Added

- **Direct-chat tool tiers redesigned**: the "read" tier is now **Websearch**
  (`web_search`/`web_fetch` only, no file reads); the "python" tier is
  "read + python". Ladder: off → Websearch → read+python → full.
- **`web_fetch` sends a real browser User-Agent** to reduce HTTP 403s (e.g.
  Wikipedia). Strict sites may still block — `web_search` is preferred.
- **Semantic LRU reclaims more context** (`hive_functions/memory.py`,
  `context/compression.py`):
  - Stale `read_file` outputs are evicted **immediately** after a successful
    edit/write/patch of the same path — the model no longer carries outdated
    file content (semantic safety + token win).
  - A repeated **full** read of an already-read file dedupes the older full
    copies (recall marker), keeping only the newest; partial (line-range)
    reads are untouched.
  - Path-less outputs (`run_bash`/`run_python`/web) now age out via half-rate
    TTL decay instead of staying fresh forever; the newest path-less result
    is kept alive like a focus-refresh. Error outputs keep their doubled TTL.

### Fixed

- **run.py missing-file guard blocked startup on a fresh install**
  (`run.py`): the guard still required a plain `hive_functions/pre_explore.py`
  FILE, but `pre_explore` was refactored into a package
  (`hive_functions/pre_explore/__init__.py` re-exports `run_pre_explore`). A
  clean extraction therefore aborted with
  `[ERROR] Missing files: hive_functions/pre_explore.py`. The guard now checks
  for the package entry point `pre_explore/__init__.py` instead.
- **Agentic coder crashed on every code_duo run** (`core/duo/_pre_explore.py`):
  the extracted pre-explore phases lost several locals; each phase now reads
  them from `state`.
- **8 GB setup works out of the box**: `direct`/`duo_coder` default to
  `lfm2.5:2.6b`, the `default_8gb_v1` profile no longer forces the 9B model,
  and a model chosen on the Agent-tab card always wins.
- **VRAM pre-flight block now suggests fitting models** instead of a generic
  "ctx senken" hint.
- **llama.cpp download is resumable + CRC-verified**; `install.bat` no longer
  dies on parenthesized install folders.

## [1.0.3] - 2026-09-01

### Fixed

- **Installer crashed silently when the install folder contained parentheses**
  (`install.bat`, `setup_models.bat`): a `for /f` over `%~dp0...` breaks when
  the path contains `( )` (e.g. `Downloads\...\HiveMind_v1.0.3 (3)\`) — cmd
  eats the `)` as the end of the for-block and the batch dies without a
  message right after the llama.cpp step. The recursive file checks now run
  via PowerShell reading the path from an ENV var (the parens never pass
  through cmd's for-block parser). Verified with a folder containing `(3)`.
- **SearXNG stale-container bind-mount error persisted after `docker rm`**
  (`searxng.bat`, `searxng_repair.bat`): a leftover container from an OLD
  HiveMind install (different folder, e.g. `HiveMindv3`) kept its stale
  bind-mount config even after `docker rm -f` — `docker compose up` again
  failed with `invalid mount config for type "bind"`. The `:compose_up`
  fallback now runs `docker compose down --remove-orphans` (removes container
  AND the compose network) before retrying, so the mount error cannot persist.
- **Installer could close the window without a message**
  (`install.bat`, `deploy/fetch_llamacpp.py`, `deploy/fetch_models.py`,
  `deploy/add_model.py`):
  - `install.bat` now writes every step to `install.log` (start, llama.cpp
    exit code, models step, setup_models exit) so a hidden crash is always
    diagnosable; the llama.cpp return code is captured before any logging.
  - `fetch_llamacpp.py` wraps download + extract in try/except (a corrupt
    partial ZIP used to raise `BadZipFile` and hard-close the window); all
    three installer scripts have a top-level crash guard that prints the
    error and waits for a key instead of silently closing.
- **VRAM budget silently reset to 7.5 GB despite the installer's 8 GB**
  (`hive_functions/safe_profile_policy.py`): the `default_8gb_v1` safety
  policy overwrote the user-configured `vram_budget_gb` (8.0 → 7.5) on every
  server start. The policy value is now a SAFETY CEILING: an explicitly set
  higher budget (e.g. 8.0) is respected.
- **PRE-FLIGHT VRAM block on high context** (`backend/manager_load.py`): a
  leftover `ctx_overrides` of 32768 (or any too-large ctx) made model loads
  fail hard on 8 GB GPUs. Before raising `VRAMPreFlightError`, the loader now
  auto-downgrades the context (16384 → 8192 → 4096) and rewrites the
  `--ctx-size` flag if a smaller window fits.
- **llama.cpp updater failed on freshly-published nightlies**
  (`deploy/fetch_llamacpp.py`): `_latest_nightly_release()` now only picks a
  build that already exposes a matching asset for the requested backend and
  falls back to the next lower build otherwise. The error message reports the
  actual nightly tag instead of the stable release (v0.3.0).
- **Agent-card temperature was ignored** (`core/tool_loop.py`,
  `core/duo_runner.py`): in the tool-loop / duo coder / critic payloads the
  model sampling profile always overwrote the temperature set in the Agent
  cards. Now an explicitly set Agent temperature wins; otherwise the model
  sampling profile supplies the value. `ToolLoopConfig.temperature` default
  is `None` (= use profile).
- **"Run completed" divider too loud in simple chat** (`static/app.js`):
  in simple/direct mode a successful run now renders a subtle centered line
  instead of a large green banner; error/abort states and multi-agent modes
  keep the full marker.
- **Agent-card context slider capped at 32k** (`static/app.js`): raised to
  65536 so larger contexts (thinking traces + full repo context) can be set.

### Changed

- **Hermes3.6 V12 "Hermes compact" config aligned to the qwen3.5:9b-ud launch
  parameters** (`model_configs/models/hermes3.6_..._v12_mtp_apex-compact.json`):
  `num_ctx` 8192→4096 and `num_ctx_duo_coder` 16384→8192 so the 35B MoE runs
  on an 8 GB GPU (MoE CPU offload 35 + MTP head stay as required).
- **Sampling profiles aligned to the official Qwen model cards**
  (`core/model_sampling.py`): `sampling_text` non-thinking general
  `temp 0.6→0.7`, `top_p 0.95→0.8`; `sampling_thinking_code` precise coding
  `presence_penalty 1.5→0.0`; QWEN35 `non_thinking` alias now matches
  Instruct general. Hermes3.6 V12 sampling unchanged (only temperature +
  top_k active, everything else disabled, seed 42).

## [1.0.2] - 2026-09-01

### Added

- `tests/test_release_integrity.py` — deterministic release guard (no LLM):
  run.py missing-file check accepts the pre_explore package layout, the
  searxng engine lists are consistent across `settings.py` /
  `tools/websearch.py` / `settings.json` / `searxng-config/settings.yml`, and
  the SearXNG config is valid YAML with a real secret placeholder. Registered
  in `tests/run_regressions.py` (24 suites total). README documents the
  regression-suite workflow (`python tests\run_regressions.py`).

### Fixed

- **"Folder 'models' is missing" note shown despite a configured models folder**
  (`start_hivemind.bat`): the NOTE is now suppressed when
  `settings.json → models_dir` is set (previously only `models\` +
  `HIVEMIND_MODELS_DIR` were checked).
- **VRAM budget display sync** (app.js): after saving, the budget input is
  refilled from the server response and the "GB effective" value matches what
  was persisted; `_vramBudgetGb` now falls back to the saved value on load.
- **MoE CPU experts dropdown showed duplicate entries** (app.js): the model
  list is now deduplicated by base name, so `qwen3.6:35b-a3b` +
  `qwen3.6:35b-a3b-uncensored` (and hermes v7/v10/v12, tiel-coder ±mtp) appear
  once instead of several identical labels.
- **SearXNG install fails on a stale container** (`searxng.bat`,
  `searxng_repair.bat`): `docker compose up` now uses `--force-recreate` and,
  on failure, removes the leftover `hivemind-searxng` container and retries.
  A container created by an OLD HiveMind install carried a stale bind-mount
  config (old WSL2 path) that made `up --build` fail with
  `invalid mount config for type "bind": bind source path does not exist`.
  The current `docker-compose.yml` has no bind mounts; `--force-recreate`
  rebuilds from it. Quick manual fix: `docker rm -f hivemind-searxng`.
- **run.py missing-file guard** now accepts `hive_functions/pre_explore/` as a
  PACKAGE (the old file check blocked server startup with
  "[ERROR] Missing files: hive_functions/pre_explore.py" after the
  module→package refactor).

### Changed

- **Websearch engines**: default engine list is now
  `brave,bing,github,wikipedia,mojeek,stackoverflow,pypi`. Google was removed
  (captcha/rate-limit prone from datacenter IPs); mojeek added (captcha-free).
  Updated in `settings.py`, `settings.json`, `tools/websearch.py` and
  `searxng-config/settings.yml`.
- **Frontend websearch status**: turning a websearch toggle ON now triggers
  `checkWebsearchStatus()` immediately (previously the dot stayed empty until a
  manual "Check status" click); the host input value is read live so the
  "Unreachable" text shows the current host.
- **Preload defaults OFF** (Configs tab / `settings.py` / `settings.json`):
  `startup_preload_enabled`, `startup_preload_judge_in_agentic`,
  `smart_preload_enabled` and `judge_keepalive_enabled` now default to `false`.
  No model is loaded at boot that isn't needed immediately; prefetch without
  learned timing data (`prefetch_agent_avgs`) is off. Server-side
  `settings.get(..., True)` fallbacks aligned to `False`.
- **Configs tab regrouped** (index.html): three semantic sections — "Model
  Preload & VRAM" (Startup Preload, Keep-Alive/Pinning, Smart Preload),
  "Learning & Model Config" (Learning Preset Mode, Model Config), "System"
  (Energy, Git Integration).
- **Git single source of truth**: the duplicated Auto-Commit toggle in the
  Agents tab was removed; git config now lives only in the Configs tab. The
  Agents tab shows a "Git in Configs →" link instead.
- **Preload sub-toggle cascade** (app.js): turning the Startup Preload master
  toggle OFF also clears the judge-in-agentic / analyst / coder sub-toggles.
- **"Use Presets" panel removed from the Agentic coder** (index.html): the
  runtime-profile / lock-profile / preset-models toggles are gone. The user
  picks the planner + coder model directly in the Duo options and Agent cards.
  `duo_runtime_profile` stays `"balanced"` as the fixed backend default
  (still used for run timeouts / important-task escalation).
- **Composer tool-status line removed** (index.html + app.js): the status text
  above the input (e.g. "⇄ Code Duo: coder tools off") is gone; the header
  Chat-Tools badge remains the single indicator.
- `run.py`/UI default semantics for the four changed settings now read
  `=== true` instead of `!== false` so the new OFF default renders correctly.

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
- **Hermes3.6 Genesis V12 "Hermes compact" (MTP-APEX-Compact)** added to the
  model downloader (`deploy/fetch_models.py` spec,
  `setup_models.bat` list, per-model config
  `model_configs/models/hermes3.6_..._v12-mtp-apex-compact.json`, MoE/MTP
  launch entries in `backend/llama_config.py`). Source:
  https://huggingface.co/LuffyTheFox/Qwen3.6-35B-A3B-Uncensored-Genesis-Hermes-V12-GGUF
- **Hermes V12 default sampling = strict (producer recommendation)** — only
  `temperature` + `top_k` active; `top_p`/`min_p`/`presence_penalty`/
  `repetition_penalty` disabled (noise gate removed → stable). The per-model
  `sampling` block overrides repeatable 1.05→1.0 from the model card.
- **llama.cpp updater fix** (`deploy/fetch_llamacpp.py`): the nightly build is
  now resolved from the GitHub releases list by the HIGHEST `bXXXX` build
  number instead of the stale `nightly-tag.txt` of the stable release
  (v0.3.0 pointed to b10621 while the newest nightly was b10742). Old
  `nightly-tag.txt` logic stays as fallback.
- **`searxng_repair.bat`** — new SearXNG repair script (+ `searxng.bat repair`
  dispatch): starts Docker Desktop automatically if the engine is down, waits
  up to 120s for the engine, recreates/restarts the `hivemind-searxng`
  container and verifies `/healthz`. If the container runs a stale baked-in
  config (no HTTP 200), it rebuilds the image and re-checks.
- **SearXNG config bugfix** (`searxng-config/Dockerfile` +
  `docker-compose.yml`): the base image's `/etc/searxng` is a Docker volume
  that shadowed the baked `settings.yml` (stale/empty file → HTTP 500 /
  `KeyError: default_doi_resolver`). The settings are now installed to
  `/usr/local/searxng/settings-hivemind.yml` and selected via
  `SEARXNG_SETTINGS_PATH`, which uses the custom config as the FULL app
  config instead of the template from `/etc/searxng`. Verified: `/healthz`
  HTTP 200 with the HiveMind engine set (brave/bing/github/wikipedia).
- **Per-model sampling stats** (`sampling` block in
  `model_configs/models/*.json`): per-mode llama.cpp sampling parameters
  (temperature/top_p/top_k/min_p/seed/presence_penalty/repetition_penalty),
  keyed by the runtime mode keys (`thinking`, `non_thinking`,
  `sampling_thinking_code`, `sampling_thinking_text`, `sampling_text`).
  Highest priority over the built-in family profiles
  (`core/model_sampling.py`), generic for any model. Collected by the
  `[C]ustom` wizard in `deploy/add_model.py` and the `--json` path.
- `seed` support in the OpenAI payload builders (tool loop, duo coder/critic,
  pre-explore) so a configured `seed` is actually sent to llama-server.

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
