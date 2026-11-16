# Release 1.6.1

Consolidated 1.6.1 release combining Image Processing UI/backend coherence,
VRAM/MoE offload and slot stability, Windows installer/updater fixes, opt-in
local CPU TTS via Underdog Woof, Websearch/SearXNG quality improvements,
long-term memory extraction fixes, and repository root cleanup on top of 1.6.0.

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or the startup check offers it), or extract the release zip
next to your old install — `settings.json`, `models.json`, `sessions\`, `logs\`,
`voice_models\`, and `.venv` are kept.

## Image Processing & Docs

- Unified Image Processing behavior across Direct, Code Duo (`preprocess` vs.
  `direct` mode, sentinel suppression, mid-run steer image policy), dynamic
  Agentic handoffs, vision-capable automap selection, and planner image
  attachment, with clarified UI controls and descriptions.
- Refreshed `README.md` and `docs/guide.md` with clearer hardware/RAM/disk
  guidance, Image Processing documentation, and organized release notes under
  `docs/releases/` (excluded from release zips via `.gitattributes`).

## Optional Underdog Woof Local CPU TTS

- Added an optional **Underdog Woof** (`woof`) backend under **Settings -> Voice ->
  Text to speech** alongside the default Pocket TTS backend: pure NumPy on CPU
  (~40.6 MB int8 weights, voices `juno` and `mira`), with no PyTorch install and
  no Hugging Face login required (`deploy/fetch_woof.py`, `core/tts_woof.py`,
  `core/tts_engine.py`, `deploy/setup_voice.bat --woof`).
- Weights, runtime code, and verbatim `LICENSE` / `NOTICE` files are fetched
  on demand into `voice_models/underdog_woof/` (never bundled in release zips)
  and protected during updates (`deploy/updater.py`, `deploy/update_prune.ps1`).
  Voice by Underdog Woof (<https://underdog.ai>) is licensed under the Underdog
  Non-Commercial License 1.0 (non-commercial use only; visible credit and links
  shown in the Voice card and `README.md`).

## Websearch, SearXNG & Long-Term Memory

- Cleared legacy `"bing,duckduckgo"` + `"all"` pins in `settings.py` so upgraded
  installs use healthy SearXNG engines cleanly (`duckduckgo web`, `yahoo`, and
  upstream `google cse`, keeping CAPTCHA/fallback-spam engines off in
  `searxng-config/settings.yml.example`), added strict `site:`/`-site:` checks,
  query-term relevance ranking/filtering, tracking-parameter cleanup, automatic
  rescue fallback on CAPTCHA/rate-limited default engines, and accurate `[N]`
  result counts in `static/app.js`.
- Fixed explicit memory shortcuts (`"remember that..."`) returning empty writes
  when model thinking tokens interfered with extraction (`context/chat_util.py`,
  `core/chat_shortcuts.py`, `hive_functions/pipeline.py`).

## VRAM, MoE Offload & Slot Stability

- Fixed a 240s `_vulkan_init_sem` deadlock in `wait_ready()` after
  `force_kill_all()`/`kill()` clears a slot mid-load, preventing spurious CPU
  `--n-gpu-layers 0` fallbacks and cross-task slot evictions (`SlotAbortedError`
  and generation-fenced wait aborts).
- Added override-aware `vram_of_moe(..., n_cpu_moe=...)` and automatic
  `--n-cpu-moe` step-up in `_auto_step_moe_cpu_offload()` when live GPU free
  VRAM is tight, alongside MiB `* 1024` unit fixes in Pre-Flight slot checks.

## Installer & Updater

- Fixed `watchdog_engine.bat` failing with a missing-path error on clean
  installs or updates where `%LOCALAPPDATA%\HiveMindGateway` or `settings.json`
  does not exist yet.
- Updated `deploy/check_optionals.bat` and `install.bat` so the optional routing
  decider installs `gliner2[local]` with CPU PyTorch before downloading weights,
  recognizes Hugging Face cached `GLiNER2.5-Decide` weights from `install.bat`,
  and verifies `torch` + `gliner2` (`AutoExtractor`) imports after `uv` with an
  automatic `--reinstall` repair pass instead of falsely reporting `[OK]`.
