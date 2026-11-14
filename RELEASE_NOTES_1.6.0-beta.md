# Release 1.6.0-beta

Pre-benchmark beta release (`1.6.0-beta`) ahead of the Colab 4B/35B benchmark
runs. Accumulates the architecture, backend, security, UI, tool-gating,
scheduler, and benchmark-harness improvements built on top of `1.5.6` prior to
the final `1.6.0` release.

## Highlights

- **GGUF drop-in & multi-part models (`backend/gguf_meta.py`):** Automatic
  quantization, architecture, context, split-shard (`-NNNNN-of-NNNNN`) and
  paired `mmproj` resolution without overwriting custom filenames; `models_registry`
  cache invalidation and isolated profile copies.
- **Shared transport & Direct/Duo/Agentic fixes (`core/llm_transport.py`):**
  Unified `cache_prompt` and `preserve_thinking` / `thinking_budget` payload
  handling, prompt-only usage tracking across Direct and Chat ledgers, Linux
  `mmap+mlock` safety downgrade under constrained `RLIMIT_MEMLOCK`, guarded
  Duo slot cleanup on disconnect/failure, `_loading` peer-slot eviction protection
  (`backend/llama_start_phases.py`), and safe `complexity` initialization on
  intent-agent early exits (`core/chat_run.py`).
- **Security & supply-chain hardening:** Native file/search tools enforce the
  shared secret-path denylist (`.env`, private keys, credentials); `run_tests`
  restricts `paths`/`targets` inside the active workspace; `deploy/updater.py`
  fails closed on missing release digests; `deploy/llama_build_pin.txt` pins
  verified `llama.cpp` build `b12001`; and `model_configs` rejects path-traversal
  identifiers.
- **Tools, UI & scheduler:** `subagent_research` promoted to an optional
  `[+Subagent]` toggle (keeping `duo_core` at 18 base tools, 19 with default
  `+Subagent`), `apply_patch` EOF no-newline and rollback hardening,
  `SchedulerManager` pending-fire race protection, and synced Direct/Agent card
  state.
- **Python 3.12–3.14 & 3×3 Colab autopilot:** Broadened `requires-python` to
  `>=3.12,<3.15`, stripped `.approaches` solution spoilers from Polyglot tasks,
  tightened `.gitattributes` `export-ignore`, refreshed UI screenshots, and
  structured `bench/external/hivemind_bench_colab_autopilot.ipynb` with
  per-run cells across Core (3 runs), Aider Polyglot (3 runs), and DeepSWE
  (3 runs on both 4B and 35B-MTP), plus live 45s heartbeat/AFK deadlock
  monitoring and clean post-save pipe exit.

## Verification

- Verified on Windows dev tree with `386/386` standalone regression suites
  passing, clean `ruff` lint, `pyright` baseline unchanged (`1`), `settings`
  docs up to date, and shell/JS syntax checks passing.
