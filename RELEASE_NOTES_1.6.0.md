# Release 1.6.0

Dedicated `1.6.0` release unifying GGUF drop-in discovery and the updated model
catalog (`twil:8b`, `air:26b-a4b`, `qwen3.6:35b-a3b-mtp`), Code Duo verification
depth gating and contract v2 extraction, shared LLM transport, security and
supply-chain hardening, scheduler fixes, and the 3×3 Colab benchmark harness.

## Highlights

- **Model Catalog & GGUF Drop-In (`deploy/fetch_models.py`, `backend/gguf_meta.py`, `model_configs/models/`):**
  - Added `twil:8b` (`webAI-Official/TwIL-8B`, `TwIL-8B-Q4_K_M-balanced.gguf`, 8.8B Granite 4.2 reasoning model with author defaults `num_ctx=40960`, `temperature=1.0`, `top_p=0.95`) and `air:26b-a4b` (`webAI-Official/Air-26B-A4B-gguf`, `Air-26B-A4B.gguf`, Gemma 4 26B-A4B MoE with `num_ctx=131072`, `q8_0` KV cache, and `moe_cpu_experts=20` keeping 10/30 MoE blocks on GPU for speed on 8 GB+ GPUs), replacing `hermes3.6:35b-a3b-uncensored-genesis-apex-mini` in the installer.
  - Added `qwen3.6:35b-a3b-mtp` (`unsloth/Qwen3.6-35B-A3B-MTP-GGUF`, `Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf`, `mtp=true`) alongside `qwen3.6:35b-a3b-ud`, with `chat_template_sharp_v22.5.0.jinja` pinned across Qwen3.6-35B-A3B profiles.
  - Automatic GGUF quantization, architecture, context, split-shard (`-NNNNN-of-NNNNN`), and paired `mmproj` resolution without overwriting custom filenames; `models_registry` cache invalidation and isolated profile copies.
- **Duo Verification Gate & Contract v2 (`core/duo_task_complete_gate.py`, `core/duo/preflight.py`, `core/tool_exec_helpers.py`, `core/tool_executor.py`, `tools/handlers/exec_tools.py`):**
  - Enforced post-edit verification depth gating and smoke-check requirements (`[SMOKE CHECK REQUIRED]`) before `task_complete` is accepted when no unit test suite was discovered.
  - Added cross-edit verification failure streak tracking (`verify_fail_streak`), head+tail command output preservation, workspace/src `PYTHONPATH` injection in `run_python`, and expanded Polyglot contract v2 extraction (`bench/import_polyglot.py`, `bench/runner.py`).
- **Shared Transport & Direct/Duo/Agentic Fixes (`core/llm_transport.py`):**
  - Unified `cache_prompt` and `preserve_thinking` / `thinking_budget` payload handling, prompt-only usage tracking across Direct and Chat ledgers, Linux `mmap+mlock` safety downgrade under constrained `RLIMIT_MEMLOCK`, guarded Duo slot cleanup on disconnect/failure, `_loading` peer-slot eviction protection (`backend/llama_start_phases.py`), and safe `complexity` initialization on intent-agent early exits (`core/chat_run.py`).
- **Security & Supply-Chain Hardening:**
  - Native file/search tools enforce the shared secret-path denylist (`.env`, private keys, credentials); `run_tests` restricts `paths`/`targets` inside the active workspace; `deploy/updater.py` fails closed on missing release digests; `deploy/llama_build_pin.txt` pins verified `llama.cpp` build `b12001`; and `model_configs` rejects path-traversal identifiers.
- **Tools, UI & Scheduler:**
  - `subagent_research` promoted to an optional `[+Subagent]` toggle (keeping `duo_core` at 18 base tools, 19 with default `+Subagent`), `duo_lean` prompt hints aligned with advertised tools (`hive_functions/prompts.py`), `apply_patch` EOF no-newline and rollback hardening, `SchedulerManager` pending-fire race protection, and synced Direct/Agent card state.
- **Python 3.12–3.14 & 3×3 Colab Autopilot (`bench/external/hivemind_bench_colab_autopilot.ipynb`, `bench/external/rent_bootstrap.sh`):**
  - Broadened `requires-python` to `>=3.12,<3.15`, stripped `.approaches` solution spoilers from Polyglot tasks, enforced anti-cheat workspace isolation, added automatic server reload on commit change, and structured the Colab autopilot with per-run cells across Core (3 runs), Aider Polyglot (3 runs), and DeepSWE (3 runs on both 4B and 35B-MTP).

## Verification

- Verified on Windows dev tree with `386/386` standalone regression suites passing, clean `ruff` lint, `pyright` baseline unchanged (`1`), `settings` docs up to date, and shell/JS syntax checks passing.
