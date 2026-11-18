# Release 1.6.3

Performance tuning, external Gemma-4 MTP wiring, DFlash/GPU-first loader
hardening, condensed performance-class model catalog, hardware-tier
recommendations (`GPU VRAM x System RAM`), concurrency/audit fixes, and
Direct-Chat / Windows LSP installer improvements.

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or extract the release zip next to your old install —
`settings.json`, `models.json`, `sessions\`, `logs\`, `voice_models\`, `stt\`,
and `.venv` are kept).

## Gemma-4 MTP, Loader Preflight & MoE Calibration

- **Native Gemma-4 external MTP sidecar support (`mtp_draft_filename`):**
  Gemma-4 E4B/E2B profiles now wire `mtp-gemma-4-E4B-it.gguf` /
  `mtp-gemma-4-E2B-it.gguf` via `--model-draft --spec-type draft-mtp --spec-draft-n-max 2 --spec-draft-ngl 999`
  when present on disk, and fall back cleanly without crashing when absent.
- **Per-profile `--flash-attn` override (`flash_attn`):**
  Profiles setting `"flash_attn": false` switch `llama-server` to `--flash-attn off`
  and force `f16` KV cache cleanly (preventing quantized V-cache launch errors on
  `b12001`). Enabled on `ling-3.0-tiny`, `gemma-4:e4b*`, `gemma-4:e2b*`, and
  `mellum2.1:12b-a2.5b-thinking`, while keeping `--flash-attn on` + `q4_0` KV on
  `twil:8b`, `air:26b-a4b`, and `qwen3.5`/`qwen3.6`.
- **DFlash, `mmproj`-aware preflight & GPU-first retry ladder (`llama_start_phases.py`, `manager_load.py`):**
  Added DFlash `p_min` / `fit` / preflight handling, included `mmproj` and extra
  overhead in the context ladder, stripped `--fit` cleanly on MoE configs,
  reordered draft-drop + ctx-step retries *before* falling back to CPU/RAM
  offload, and calibrated `b12001` MoE profiles (`air:26b-a4b`, `groot:19b-a3b`,
  `hermes3.6` APEX, `minicpm-v-4.7:35b`, `cyber-tiel-coder:35b-a3b-mtp-ud`,
  `mellum2.1:12b-a2.5b-thinking`, `ubatch=1024`, Nemotron opt-in `Q8_0` DFlash).

## Condensed Catalog & Hardware Tiers

- **Streamlined 11-model performance catalog (`deploy/fetch_models.py`):**
  Replaced overlapping entries (`twil:8b` moved to specialist footnote), added
  `ling-3.0-tiny` (`32k` ctx) and `nvidia-nemotron-3.5-bolt:e4b`
  (`unsloth/NVIDIA-Nemotron-3.5-Bolt-E4B-GGUF` `UD-Q4_K_XL` + opt-in `Q8_0`
  DFlash sidecar), and pinned `qwen3.6:35b-a3b-mtp` to `UD-IQ4_XS` (~4.3 GB
  smaller than `UD-Q4_K_XL` and faster warm prefill than `UD-Q3_K_XL`).
- **Hardware-tier recommendations (`--print-essential`, `--print-tiers`, `deploy/recommend_models.py --plan`):**
  Detects physical GPU VRAM + system RAM via `probe_hardware()` and recommends
  starter/upgrade sets across `4-6 GB VRAM`, `8 GB + <=24 GB RAM`,
  `8 GB + 32+ GB RAM`, `12-16 GB VRAM`, and `24+ GB VRAM`, while keeping
  `duo_critic` separated onto co-resident `minicpm5:2b-sharp`.

## Concurrency, Core & Direct-Chat Reliability

- **Per-port locking, KV retention & subagent/gateway isolation:**
  Serialized concurrent local LLM HTTP calls per port (`get_port_lock`) with
  shared httpx pooling, pinned active coder slots across rounds, clamped Critic
  ctx to active co-located slot limits, suppressed thinking during context
  compression, added GGUF multi-quant dedup in `llama_models.py`, fixed
  supervisor interval/UI timer leaks, isolated subagent/pre-explore workers,
  and deduplicated `/tools mcp` + stream events in `hivemind_gateway`.
- **Direct-Chat UI, telemetry & mid-chat tool switching (`static/app.js`, `core/direct_runner.py`):**
  - Fixed the duplicate `_createThinkBlock` declaration collision
    (`_buildAgentThinkBlock(tid)`) that prevented Answer/Duo bubbles from
    rendering, kept header `#h-models` synchronized on direct/automap changes,
    and emitted `run_meta` before `_run_direct_tools` so `done.model` reports
    the real model instead of falling back to `analyst`.
  - Added post-history capability trail reinjection (`_append_direct_capability_trail`)
    so toggling Websearch or raising `direct_tools_tier` mid-conversation updates
    the active tool set right before generation rather than being overridden by
    older assistant turns claiming search/tools are off.
  - Avoided false `WorkspaceForceInvalid('')` blocks on fresh installs when UI
    `workspace` is empty, routed workspace-less runs (and Direct chats without an
    explicit chat/UI workspace) into an isolated `default_workspace/`
    placeholder folder instead of the install root (`AGENTS.md`/`server.py`),
    and protected `default_workspace/` from git tracking and updates/prune.
  - Added `/workspace/placeholder` inspection + clear endpoints, confirmation
    prompts before `/new` / `+ NEW CHAT` clears existing files inside
    `default_workspace/`, `/workspace default` reset, and honest workspace
    reporting in Telegram's `ℹ️ Run` card and `/status`.
  - Defaulted `subagent_lite_enabled` to `False`, raised default `duo_ubatch` /
    `llama_ubatch_size` to `512`, and cleaned up Windows Proactor subprocess
    pipe transports in `infra/lsp_client.py`.
- **UI, Code mode & `/v1/chat/completions`:**
  Renamed coding mode to `Code` (`Critic-Duo` / `Agentic`) with inline Critic
  dropdown + `duo_critic_ctx`, excluded `smoke_test` runs from Token
  Statistics, showed live planner decode in the overlay, and wired
  `model_overrides`, conversation history, sampling params, and real token usage
  in `/v1/chat/completions`.

## Windows Installer & LSP Setup

- **LSP installation & PATH discovery (`install.bat`, `deploy/install_lsp_packs.bat`, `infra/lsp_registry.py`):**
  Added direct **`[L] language servers only (LSP)`** entry alongside `[O]`
  optionals and `[1-9]` single-step mode, removed the auto-skip timeout on LSP
  prompts, added `A`/`all` pack selection, refreshed standard tool paths before
  probing (`nodejs`, `npm`, `cargo`, `winget`, `LLVM`, `go`), added `winget`
  auto-setup for missing `rustup` (`Rustlang.Rustup`) plus Python `uv` fallback
  (`python-lsp-server` when `npm` is unavailable), and resolved Windows standard
  LSP directories in `infra/lsp_registry.py` / `infra/lsp_client.py` so newly
  installed servers work without an engine restart.
- **Clean-install hardening (`install.bat`, `fetch_llamacpp.py`):**
  Quoted `nvidia-smi` query flags against `cmd.exe` `for /f` splitting,
  auto-seeded physical GPU VRAM defaults (with `vram_budget_gb` override for
  APUs), selected driver-compatible `<= CUDA` llama.cpp runtimes cleanly, and
  added a Step `[8/9]` TTS provider picker (`[W] Underdog Woof` recommended
  default / `[P] Pocket TTS` / `[B] Both`).
