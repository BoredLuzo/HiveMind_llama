# Release 1.6.4

Two-layer drop-in model registry & Model Profiles UI, `HIVEMIND.md` workspace
project rules, strict workspace `.venv` isolation, multi-chunk Markdown-cleaned
TTS, `run_bash`/LSP/security hardening (Waves 1–3), and updated A100/L4 Google
Colab benchmark autopilot defaults for `qwen3.6:35b-a3b-mtp`.

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or extract the release zip next to your old install —
`settings.json`, `models.json`, `sessions\`, `logs\`, `voice_models\`, `stt\`,
and `.venv` are kept).

## Drop-in Model Registry, Model Profiles UI & Engine Settings

- **Two-layer model registry (`model_configs/arch_compat.json`, `model_configs/models_registry.py`, `routers/models.py`):**
  Shipped model profiles (`model_configs/models/`) are now cleanly layered under
  user override profiles (`user_models/`) with atomic writes, sparse saves from
  the Model Profiles modal (only modified fields are persisted), and one-click
  revert to shipped defaults.
- **Automatic GGUF drop-in discovery (`backend/llama_models.py`, `backend/gguf_meta.py`):**
  Dropping an arbitrary GGUF into `models/` automatically inspects architecture
  metadata, `has_tool_call` chat-template support, and sibling sidecars
  (`mmproj`, `mtp`, `dflash`, `dspark`) using dynamic `arch_compat.json` rules.
  Added `mtime`-stamped index caching so idle 30s checks no longer rebuild or
  spam `[GGUF-INDEX]` logs when `models/` has not changed.
- **13 exposed engine settings & `/settings/schema` (`settings.py`, `backend/runtime_flags.py`, `docs/settings.md`):**
  Surfaced advanced runtime controls (`llama_extra_args`, `gpu_device`,
  `total_vram_mib_override`, `vram_pre_flight_margin_mib`, `spec_decode_mode`,
  `llama_update_channel`, etc.) in the Settings UI and schema, and wired
  `llama_update_channel` (`pinned` / `latest`) through `deploy/fetch_llamacpp.py`
  and `update_llama.bat`.

## Workspace Isolation & Project Rules

- **`HIVEMIND.md` workspace project rules (`hive_functions/project_rules.py`, `core/duo_runner.py`, `core/direct_runner.py`):**
  Automatically loads workspace-root project instructions (`HIVEMIND.md` >
  `.hivemind/HIVEMIND.md` > `AGENTS.md` > `CLAUDE.md` fallback) on the **first
  turn of a chat only** when present in the active workspace root, with
  symlink-escape protection, UTF-8 BOM stripping, case-insensitive fallback, and
  KV-cache-safe prompt placement.
- **Strict workspace `.venv` enforcement & 3-tier Python resolution (`infra/child_env.py`, `tools/handlers/exec_tools.py`, `hive_functions/test_runner.py`, `infra/lsp_client.py`):**
  Unified Python interpreter lookup across tool execution, test runner, and LSP
  (`workspace/.venv` > real system Python skipping WindowsApps stubs and
  HiveMind's own `.venv` > engine venv fallback). Unscoped `pip install` calls
  without a workspace `.venv` are blocked with a clear `PIP_NO_WORKSPACE_VENV`
  hint so workspace packages never contaminate HiveMind's engine environment.

## Multi-Chunk TTS, `run_bash`, LSP & Web UI Fixes

- **Multi-chunk Markdown-cleaned TTS (`core/tts_audio.py`, `core/tts_engine.py`):**
  Replaced the hard 600-char TTS cutoff with Markdown/code/URL/table cleaning
  (`clean_for_speech`), sentence-boundary chunking up to `3600` chars
  (`split_speech_chunks`), and WAV concatenation with 160ms pauses
  (`concat_wavs`) — fixing truncated Telegram voice messages and long Web UI
  readouts.
- **`run_bash` & `task_complete` hardening (`tools/handlers/exec_tools.py`, `core/tool_executor.py`):**
  Added 16 KiB chunked pipe draining (eliminates >64 KiB `LimitOverrunError`),
  `stdin=DEVNULL`, Windows `pip`/`pip3`/`python3` shims, PowerShell `&&`
  `$LASTEXITCODE` `$null` guard and `_cd_strip` subdirectory fix, instant
  repeat-timeout rejection (`RUN_BASH_TIMEOUT_CMD:`), line-aligned 3400-char
  `RUN_BASH_NONZERO` tails, and `[run_bash: exit 0]` + smoke-test acceptance in
  the `task_complete` gate.
- **13-server live LSP audit & Web UI polish (`infra/lsp_client.py`, `infra/lsp_registry.py`, `tools/handlers/lsp.py`, `static/app.js`):**
  Verified and fixed root initialization, executable resolution, and timeout
  handling across 13 language servers; fixed the stuck speaker (`🔊`) button
  after pre-call reasoning collapses, multi-line `run_python` `<summary>`
  rendering, and `task_complete` badge matching.

## Security, Updater & Colab Benchmark Updates

- **Waves 1–3 security, concurrency & typing hardening:**
  Hardened Telegram gateway redaction/bridge/config, sandbox & taint guards,
  slot/VRAM/health locking, and LLM HTTP transport pooling; cleared 19 Pyright
  diagnostics back to baseline (`1`), and scoped `deploy/updater.py`
  `running_install_processes()` strictly to the target installation path.
- **Google Colab A100/L4 autopilot (`bench/external/rent_bootstrap.sh`, `bench/external/hivemind_bench_colab_autopilot.ipynb`):**
  Updated default 35B runs to `qwen3.6:35b-a3b-mtp` (`UD-IQ4_XS`, `17.0 GB` L4
  MoE VRAM sizing) and installed DeepSWE test dependencies symmetrically into
  both `.venv` and Colab system `python3` for fair head-to-head benchmarking
  against Aider, Hermes, and Pi.
