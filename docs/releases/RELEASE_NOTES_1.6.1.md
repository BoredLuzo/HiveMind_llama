# Release 1.6.1

Stability, Image Processing, MoE offload, and Windows installer/updater fixes
on top of 1.6.0.

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or the startup check offers it), or extract the release zip
next to your old install — `settings.json`, `models.json`, `sessions\`, `logs\`
and `.venv` are kept.

## Image Processing & Docs

- Unified Image Processing behavior across Direct, Code Duo (`preprocess` vs.
  `direct` mode, sentinel suppression, mid-run steer image policy), dynamic
  Agentic handoffs, vision-capable automap selection, and planner image
  attachment, with clarified UI controls and descriptions.
- Refreshed `README.md` and `docs/guide.md` with clearer hardware/RAM/disk
  guidance and Image Processing documentation.

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
