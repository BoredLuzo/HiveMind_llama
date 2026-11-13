# Release 1.6.0-beta

Pre-benchmark beta release (`1.6.0-beta`) ahead of the Colab 4B/35B benchmark
runs. Accumulates the architecture, backend, UI, tool-gating, scheduler, and
benchmark-harness improvements built on top of `1.5.6` prior to the final `1.6.0`
release.

## Highlights

- **GGUF drop-in & multi-part models (`backend/gguf_meta.py`):** Automatic
  quantization, architecture, context, split-shard (`-NNNNN-of-NNNNN`) and
  paired `mmproj` resolution without overwriting custom filenames; `models_registry`
  cache invalidation and isolated profile copies.
- **Shared transport & Direct/Duo/Agentic fixes (`core/llm_transport.py`):**
  Unified `cache_prompt` and `preserve_thinking` / `thinking_budget` payload
  handling, prompt-only usage tracking across Direct and Chat ledgers, Linux
  `mmap+mlock` safety downgrade under constrained `RLIMIT_MEMLOCK`, and guarded
  Duo slot cleanup on disconnect/failure.
- **Tools, UI & scheduler:** `subagent_research` promoted to an optional
  `[+Subagent]` toggle (keeping `duo_core` at 18 base tools, 19 with default
  `+Subagent`), `apply_patch` EOF no-newline and rollback hardening,
  `SchedulerManager` pending-fire race protection, and synced Direct/Agent card
  state.
- **Python 3.12–3.14 & Colab autopilot:** Broadened `requires-python` to
  `>=3.12,<3.15`, stripped `.approaches` solution spoilers from Polyglot tasks,
  tightened `.gitattributes` `export-ignore`, refreshed UI screenshots, and added
  clean-state reset plus 4B (`100k` ctx) and 35B-MTP (`120k` ctx) benchmark stages.

## Verification

- Verified on Windows dev tree with `385/385` regression suites passing, clean
  `ruff` lint, `pyright` baseline unchanged (`1`), `settings` docs up to date,
  and shell/JS syntax checks passing.
