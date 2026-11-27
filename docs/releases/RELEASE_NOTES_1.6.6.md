# Release 1.6.6

Stability, modularization, and agentic-loop reliability release: fixes
post-compression and tail-cap read-guard traps, eliminates Duo VRAM ctx-cascade
reload loops, hardens backend/gateway/frontend hot paths from audit Waves 1–5,
and updates the optional `inkling-2-mini` installer quant to `ISTA-DASLab`
`GSQ-RCO-Q2_K_XL`.

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or extract the release zip next to your old install —
`settings.json`, `models.json`, `sessions\`, `logs\`, `voice_models\`, `stt\`,
and `.venv` are kept).

## Agentic Context, Compression & Duo Reliability

- **Post-compression & tail-cap read-guard consistency (`context/compression.py`, `core/duo_dtool_setup.py`):**
  - Post-compression summaries no longer tell the coder *"do NOT read/list these again"* after raw file contents were compressed away; they explicitly instruct the model to call `read_file` before inspecting or editing compressed files.
  - Compaction and `tail_tool_max_chars` truncation now extract `read_file` paths from both tool headers (including relative paths and paths with spaces) and matching `assistant` `tool_calls`, ensuring `_files_read_in_run` releases truncated/compressed files instead of returning false `[SKIP: already read]` blocks.
  - The pre-explore bridge computes the 8000-char history trim before building its prompt header so trimmed files are never labeled as already in context.
- **Duo preloop ctx-cascade pinning (`core/duo_coder_preloop.py`):**
  - Pins `_coder_ctx_override` when the coder preloop steps context down during a VRAM or timeout cascade, eliminating immediate `Cached port ctx mismatch` kill/reload loops.

## Installer & Models

- **Updated `inkling-2-mini` installer quant (`deploy/fetch_models.py`, `backend/llama_models.py`, `model_configs/gguf_filename_tags.json`, `docs/guide.md`):**
  - Optional Tier-4 `inkling-2-mini` now downloads `Inkling-2-Mini-GSQ-RCO-Q2_K_XL.gguf` (~9.8 GB) + `mmproj-Inkling-2-Mini-BF16.gguf` (~0.6 GB) from `ISTA-DASLab/Inkling-2-Mini-GSQ-RCO-GGUF`.
  - Existing local `GSQ-RCO-IQ3_XS` and `UD-IQ3_XXS` GGUFs remain recognized automatically under the canonical `inkling-2-mini` tag.

## Architecture Modularization & Hot-Path Hardening

- **Focused submodules (`routers/`, `hivemind_gateway/`, `static/js/`, `deploy/_common.py`, `infra/proc_kill.py`, `model_configs/profile_validation.py`, `tools/handlers/_win_shims.py`):**
  - Split oversized router, gateway bridge, and UI panel modules into focused units and consolidated shared deploy, process-kill, profile-validation, and Windows shell-shim helpers.
- **Backend, gateway, router & UI hot-path fixes (`backend/llama_client.py`, `core/duo_runner.py`, `hivemind_gateway/bridge.py`, `static/js/markdown.js`, `routers/`, `tools/sandbox.py`):**
  - Prevents `LlamaClient` slot `in_flight` leaks/double-releases on recovery retries or cancellation, removes stale `ToolRoundState` chunk caching, eliminates concurrent `RunBridge` approval-card races, fixes `markdown.js` >25 KB trailing render drops, restores live router settings/workspace handling, and maintains a clean Pyright baseline (`1/1`) and Ruff lint pass across the full suite.
