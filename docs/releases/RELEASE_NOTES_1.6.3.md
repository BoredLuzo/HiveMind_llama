# Release 1.6.3

Performance tuning, external Gemma-4 MTP wiring, condensed performance-class
model catalog (~63 GB full catalog vs ~106 GB), and automatic hardware-tier
recommendations (`GPU VRAM x System RAM`).

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or extract the release zip next to your old install —
`settings.json`, `models.json`, `sessions\`, `logs\`, `voice_models\`, `stt\`,
and `.venv` are kept).

## Gemma-4 MTP & Per-Profile Flash-Attention Control

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

## Condensed Catalog, Hardware Tiers & Quant Tuning

- **Streamlined 11-model performance catalog (`deploy/fetch_models.py`):**
  Replaced overlapping entries (`twil:8b` moved to specialist footnote), added
  `ling-3.0-tiny` (`32k` ctx) and `nvidia-nemotron-3.5-bolt:e4b`
  (`unsloth/NVIDIA-Nemotron-3.5-Bolt-E4B-GGUF` `UD-Q4_K_XL` + opt-in `Q8_0`
  DFlash sidecar), and pinned `qwen3.6:35b-a3b-mtp` to `UD-IQ4_XS` (smaller than
  `UD-Q4_K_XL` by ~4.3 GB and faster warm prefill than `UD-Q3_K_XL`).
- **Hardware-tier recommendations (`--print-essential`, `--print-tiers`, `deploy/recommend_models.py --plan`):**
  Detects physical GPU VRAM + system RAM and prints tailored starter/upgrade
  sets for `4-6 GB VRAM`, `8 GB + <=24 GB RAM`, `8 GB + 32+ GB RAM`,
  `12-16 GB VRAM`, and `24+ GB VRAM`, while keeping `duo_critic` separated onto
  co-resident `minicpm5:2b-sharp`.
- **Calibrated MoE & DFlash profiles:**
  Added `cyber-tiel-coder:35b-a3b-mtp-ud` (`ncmoe=35`, `ubatch=1024`),
  enabled `ubatch=1024` on `mellum2.1`, `qwen3.6_35b-a3b-mtp`, and
  `hermes3.6` APEX, tuned `twil:8b` (`16384` ctx on 8 GB GPUs), calibrated
  Gemma-4 SWA `kv_bytes_per_token=32768`, and configured Nemotron DFlash as
  opt-in (`spec_enabled_default=false`, `draft_overhead_mib=950`, `Q8_0` alternate).

## Stability, UI & Windows Installer Fixes

- **Per-port run locking, KV retention & `ubatch=512` default:**
  Added per-port `asyncio.Lock` serialization and shared local connection pooling,
  pinned coder slots during active runs to preserve KV caches, raised default
  `duo_ubatch` / `llama_ubatch_size` from `256` to `512`, disabled the Research
  subagent (`subagent_lite_enabled`) by default on fresh installs, and
  suppressed thinking during context compression.
- **UI, OpenAI `/v1/chat/completions` & live metrics:**
  Renamed coding mode to `Code` (`Critic-Duo` / `Agentic`) with inline Critic
  dropdown + `duo_critic_ctx` wiring, fixed multi-turn metric footers, excluded
  `smoke_test` runs from Token Statistics, showed live planner decode in the
  overlay, wired `model_overrides`, history, sampling parameters, and token
  usage in `/v1/chat/completions`, and avoided false `WorkspaceForceInvalid('')`
  blocks on fresh installs when the UI workspace field is empty.
- **Windows clean-install hardening (`install.bat`, `fetch_llamacpp.py`):**
  Quoted `nvidia-smi` query flags against `cmd.exe` `for /f` splitting,
  auto-seeded detected GPU VRAM as default prompt (plus `vram_budget_gb`
  override in `probe_hardware()`), skipped the redundant shortcut pause,
  selected driver-compatible `<= CUDA` llama.cpp runtimes cleanly, added
  single-step `[1-9]` selection (`[Y/N/O/1-9]`), expanded `[O]` optionals-only
  mode (`Routing Decider`, `LSP`, `SearXNG`, `Voice`, `Telegram gateway`),
  removed the auto-skip timeout on LSP setup, and added a TTS provider picker
  (`[W] Underdog Woof` recommended default / `[P] Pocket TTS` / `[B] Both`).
