# Release 1.6.2

Hardening release focused on clean-install defaults, model/runtime fetchers,
Windows/Linux installer flows, safe in-place updates, prune behavior, and
release packaging reliability on top of 1.6.1.

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or the startup check offers it), or extract the release zip
next to your old install — `settings.json`, `models.json`, `sessions\`, `logs\`,
`voice_models\`, `stt\`, and `.venv` are kept.

## Clean Installs & Runtime Model Resolution

- Isolated `DEFAULT_SETTINGS` and `DEFAULT_AGENT_CFG` copies on load (`settings.py`)
  and synced startup sanitized agent configs back into live pipeline state (`server.py`).
- Gated `apply_safe_profile_policy` against actually installed models when a
  workspace is supplied (`hive_functions/safe_profile_policy.py`), stopping
  unloaded matrix model assignments on empty fresh installs.
- Prioritized matching `GPU_BACKEND` builds in `_find_llama_server()`
  (`backend/llama_config.py`) so newer foreign backend folders never shadow the
  configured accelerator.
- Added built-in `_BUILTIN_FILENAME_TAGS` fallback and improved `e2b`/`e4b`
  projector matching in `backend/llama_models.py`, fixed hyphenated Gemma keys in
  `routing/model_automap.py`, and added POSIX `LD_LIBRARY_PATH` forwarding for
  STT subprocesses (`core/stt_engine.py`).

## Fetchers & Setup Utilities

- Hardened HTTP retries, Content-Length / SHA256 checks, range-resume recovery,
  and lock-safe atomic swaps across `deploy/fetch_llamacpp.py`,
  `deploy/fetch_whisper.py`, `deploy/fetch_models.py`, and `deploy/fetch_woof.py`.
- Supported Linux split `cudart-llama-...-ubuntu-cuda-*.tar.gz` archives and
  `whisper-bin-ubuntu-x64.tar.gz` for `blas`/CPU Linux installs with `.so`
  symlink preservation and size-proportional partial model re-downloads.
- Improved clean-install tolerance and edge-case handling across
  `deploy/fetch_decider.py`, `deploy/add_model.py`,
  `deploy/apply_model_defaults.py`, `deploy/recommend_models.py`, and
  `deploy/backfill_speeds.py`.

## Windows Installer, Launchers & Linux Installer

- Fixed `install.bat` non-interactive `winget` agreements + `install.ps1` fallback,
  empty User `Path` PowerShell handling, variable leakage across prompts, quiet
  sub-script handoffs (`HIVEMIND_NO_PAUSE=1`), and `:lsp_done` step reporting.
- Hardened `setup_models.bat`, `deploy/check_optionals.bat`,
  `deploy/setup_voice.bat`, `update_llama.bat`, `watchdog_engine.bat`,
  `stop_llama.bat`, `start_hivemind.bat`, `start_gateway*.bat`, `searxng.bat`,
  `deploy/install_windows.bat`, and `deploy/install_lsp_packs.bat`.
- Synced `deploy/install_linux.sh` and `deploy/hivemind.service` with
  `render`/`video` groups, `voice_models`/`stt`/`tts-venv` excludes, Playwright
  Chromium install, backend unit injection, and raised service resource caps.

## Updater, Prune & Packaging

- Hardened `update.bat`, `deploy/updater.py`, `deploy/update_prune.ps1`,
  `deploy/package_release.bat`, and `scripts/release_zip_report.py` for
  caller-relative `--zip` resolution, complete backup/prune runtime exclusions,
  pre-release version stage ordering (`alpha < beta < rc`), stale 90s watchdog
  detection, first-upgrade orphan cleanup, empty directory cleanup, and strict
  release leak checks, covered by `tests/test_installer_updater_1_6_2.py`.

## Search, UI & Documentation Alignment

- Fixed Feature Search panel targeting (`Voice` in `Configs`, `Automations` tab,
  `Planner` dedup collision, Image Processing & MoE entries, Underdog Woof /
  GLiNER / MCP keywords), wired saved chat search (`GET /chats/search`) in the
  Chats panel, fixed `searxng.bat external <url>`, `tools/websearch.py` empty
  language / `site:` guards, pruned code/file search, and
  `lsp_forward_workspace_settings` forwarding.
- Updated `README.md`, `docs/`, `CONTRIBUTING.md`, `AGENTS.md`, and `bench/`
  docs to match current `1.6.2` models, installer steps, security/approval/LSP/MCP/
  Voice behavior, and defaults.
