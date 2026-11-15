# Release 1.6.0

Dedicated `1.6.0` release on top of `1.5.5` (consolidating the unpublished `1.5.6`
sprint and the `1.6.0` release wave — 176 commits total). Unifies GGUF drop-in
discovery and the updated model catalog (`twil:8b`, `air:26b-a4b`,
`qwen3.6:35b-a3b-mtp`), Code Duo verification depth gating and contract v2
extraction, patch-native editing (`apply_patch`), Direct run pause/resume and
mid-run steering, nested Deep Retrieval, 35B/MoE performance levers, shared LLM
transport, security and supply-chain hardening, modularized runner architecture,
and the 3×3 Colab benchmark harness.

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or the startup check offers it), or extract the release zip
next to the old install — `settings.json`, `models.json`, `sessions\`, `logs\`
and `.venv` are kept.

## Highlights

### Model Catalog & GGUF Drop-In (`deploy/fetch_models.py`, `backend/gguf_meta.py`, `model_configs/models/`)
- **New & updated catalog models:**
  - Added `twil:8b` (`webAI-Official/TwIL-8B`, `TwIL-8B-Q4_K_M-balanced.gguf`, 8.8B Granite 4.2 reasoning model with author defaults `num_ctx=40960`, `temperature=1.0`, `top_p=0.95`).
  - Added `air:26b-a4b` (`webAI-Official/Air-26B-A4B-gguf`, `Air-26B-A4B.gguf`, Gemma 4 26B-A4B MoE with `num_ctx=131072`, `q8_0` KV cache, and `moe_cpu_experts=20` keeping 10/30 MoE blocks on GPU for speed on 8 GB+ GPUs), replacing `hermes3.6:35b-a3b-uncensored-genesis-apex-mini` in the installer.
  - Added `qwen3.6:35b-a3b-mtp` (`unsloth/Qwen3.6-35B-A3B-MTP-GGUF`, `Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf`, `mtp=true`) alongside `qwen3.6:35b-a3b-ud`, with `chat_template_sharp_v22.5.0.jinja` pinned across Qwen3.6-35B-A3B profiles.
- **GGUF metadata & shard/projector discovery:** Automatic GGUF quantization, architecture, context, split-shard (`-NNNNN-of-NNNNN`), and paired `mmproj` resolution without overwriting custom filenames; canonical `:latest` / `#N` and case-insensitive profile resolution; `models_registry` cache invalidation and isolated `deepcopy` profile copies.

### Code Duo Verification Gate, Retrieval & Context (`core/duo_task_complete_gate.py`, `core/duo/preflight.py`, `core/tool_exec_helpers.py`, `core/tool_executor.py`)
- **Verification depth gating & smoke checks:** Enforced post-edit verification depth gating and `[SMOKE CHECK REQUIRED]` before `task_complete` is accepted when no unit test suite was discovered; added cross-edit verification failure streak tracking (`verify_fail_streak`), head+tail command output preservation, workspace/src `PYTHONPATH` injection in `run_python`, and expanded Polyglot contract v2 extraction (`bench/import_polyglot.py`, `bench/runner.py`).
- **Pre-seed, Deep Retrieval & symbol guards:** Tiny top-level workspaces (≤4 text files, context-scaled cap) are pre-seeded into round 0 so the coder can edit immediately. For deeper multi-file repos where pre-seed is empty, a bounded incremental repo-index walk (`duo_deep_retrieval`) ranks nested files against the task and injects compact path + size + top-symbol signature hints (never full file bodies). `WOULD_DROP_SYMBOLS` now includes the original definition line to prevent retry loops.

### Tools, Patch-Native Editing & LSP Auto-Diagnostics (`tools/`, `infra/lsp_client.py`)
- **`apply_patch` & edit compaction:** New `apply_patch` tool applies unified diffs across one or more files in a single call with line-drift tolerance, unique context matching, EOF no-newline handling, path-escape validation, atomic writes, and full rollback on failure (`old_text/new_text` and batch `edits=[...]` remain available). `_approval_call_key` / `_norm_write_path` (`tools/runner.py`) extract target paths from `apply_patch` `patch`/`diff` headers for per-target approval parity. Optional `duo_auto_test_on_edit` runs mapped tests (`duo_selective_tests`) in the same tool turn, and stale edit payloads in long histories compact to `[applied diff: path (+X/-Y)]` stubs.
- **Tool tiers & optional toggles:** `duo_core` remains the coder baseline (`18` base tools + optional `[+Subagent]` toggle = `19` default, saving ~39% schema/round tokens vs. full tier; `[+Websearch]`, `[+Git]`, or `duo_coder_tool_tier=full` add git/web/MCP/browser/LSP extras). `duo_lean` expanded from `10` to `12` tools (`run_python` and `undo_last` added in `tools/definitions.py` so shared verification and recovery prompts match advertised tools, with prompt hints aligned in `hive_functions/prompts.py`). Direct gains a `research` tier (`web + read/search`, no write/exec).
- **LSP background warm & auto-diagnostics:** Successful writes warm the language server in the background (`lsp_auto_warm_on_write`, bounded 3 s wait) and attach compiler/linter diagnostics autonomously, falling back to pyright/per-language lint when LSP is unavailable.
- **Git & outcome surfaces:** Added `/git/diff`, git checkpoint listing/restore UI panel, chat-scoped outcome hints, and a single engine-owned 30 s Ask-User auto-answer governor (`ask_user_auto_proceed`).

### Direct Run Control, Routing & Shared Transport (`core/chat_run.py`, `core/llm_transport.py`, `routing/`)
- **Direct `/pause`, `/resume` & mid-run steering:** `/pause` and `/pause-state/{chat_id}` now gate plain Direct streams before model rounds and at token chunks; `/resume` continues and abort during pause ends cleanly as `aborted`. Mid-run `[OWNER STEER]` messages queued before the answer ride into the prompt, steers queued during streaming trigger a bounded continuation round (max 2), and late steers deliver after the run.
- **Routing guards:** Persisted agentic mode no longer hijacks short read/list questions into Code Duo (`is_simple_tool_prompt` with coding-verb veto); the legacy tool route shares Direct prompt rules, respects `direct_tools_max_rounds` (1..300), and nudges identical repeated tool calls so read-only tasks finish with an answer.
- **Shared LLM transport & UI/scheduler sync:** Extracted `core/llm_transport.py` unifying `cache_prompt` and `preserve_thinking` / `thinking_budget` handling, prompt-only `usage_meta` tracking across Direct/Chat ledgers, idempotent Duo slot cleanup on disconnect/failure, safe `complexity` initialization on intent-agent early exits, `SchedulerManager` pending-fire race protection, synced Direct/Agent cards, and guarded frontend `localStorage` wrappers (`_hmStorageGet`/`_hmStorageSet` preventing Firefox blocked-storage error toasts).

### 35B / MoE Performance & Backend Reliability (`backend/llama_config.py`, `backend/llama_start_phases.py`, `backend/manager_load.py`, `backend/manager_process.py`)
- **Large MoE `mlock` & Linux `RLIMIT_MEMLOCK` guards:** Large MoE GGUFs (>12 GB) no longer inherit global `llama_mlock=true` as `mmap+mlock` (avoiding up to 4.7x slower decode from NVMe paging; `llama_mlock_skip_large_moe=false` restores old behavior), and Linux `mmap+mlock` automatically downgrades when `RLIMIT_MEMLOCK` or free RAM is constrained.
- **Gate-aware MoE `ubatch` & thread split:** MoE models can raise `ubatch` via profile `"ubatch"` or `duo_ubatch_moe` when VRAM pre-flight fits the larger compute buffer (with baseline fallback); MoE prefill keeps full `--threads-batch` while decode uses physical-core `--threads`.
- **Vision skip & slot lifecycle:** Text-only loads skip loading the `mmproj` vision projector (`vision_mmproj_always=true` restores always-attach) while preserving vision on `image_url` steers and retries. Slot matching canonicalizes `:latest`/`#N` and casing to avoid reloads on role switches; LRU eviction runs off the event loop (`_evict_lru_async`) and skips `_loading` peer slots to prevent concurrent weight-load thrash.

### Security & Supply-Chain Hardening (`utils/file.py`, `tools/handlers/`, `deploy/`)
- **Secret denylist & workspace containment:** Native `read_file`, `list_files`, `find_by_name`, and `search_code` tools enforce the shared secret-path denylist (`.env`, SSH/TLS private keys, credentials, cloud/token configs); `run_tests` validates `paths`/`targets` via `os.path.commonpath` to block workspace escape; `model_configs` rejects path-traversal identifiers.
- **Verified updates & pinned binaries:** `deploy/updater.py` fails closed when release SHA-256 digests are missing or mismatched, and `deploy/llama_build_pin.txt` pins verified `llama.cpp` build `b12001`.

### Architecture Split, Documentation & Python 3.12–3.14 (`core/`, `docs/`, `bench/`)
- **Modularized runners & updater:** Split `core/duo_runner.py` (`duo_coder_preloop`, `duo_task_complete_gate`, `duo_finalize`, `duo_finalgate`, `duo_epilogue`, `duo_round_input`, `duo_dtool_setup`, `duo_inloop_planner`, `duo_coder_load`, `duo_compress_cycle`, `duo_policies`, `duo_round_gate`, `duo_finish_round`, `duo_plan_round`, `duo_ctx_meter`, `duo_explore_gate`, `truncated_write_salvage`), `core/chat_run.py` (`chat_shortcuts`, `chat_vision_pre`, `chat_history_seed`, `chat_route`, `chat_route_decision`, `chat_tools_route`, `RunTokenLedger`, `PrefetchCoordinator`), `backend/llama_start_phases.py`, `core/version.py`, and `deploy/updater.py`.
- **Docs & Python compatibility:** Split `README.md` into a concise landing page plus `docs/guide.md` and `docs/privacy.md`; broadened `requires-python` to `>=3.12,<3.15` (with `tzdata` shipped in core dependencies).
- **Benchmark harness & 3×3 Colab Autopilot (`bench/`):** Added the D1 benchmark harness (`hivemind_direct`, `hivemind_duo`, `hivemind_duo_full`, `aider`, `hermes`, `pi` with shared `_agent_timeout_s`, hardened SSE token accounting via `_accumulate_hivemind_sse`, and hidden-test `conftest.py` isolation in `install_hidden_tests`), stripped `.approaches` solution spoilers from Polyglot tasks, enforced anti-cheat workspace isolation, added automatic server reload on commit change in `bench/external/rent_bootstrap.sh`, and structured `bench/external/hivemind_bench_colab_autopilot.ipynb` into per-run cells across **3 Suites × 3 Runs** (Core 23 tasks, Aider Polyglot 34 tasks imported at runtime / 10 shipped offline fixtures, DeepSWE 3 tasks on both 4B and 35B-MTP) with atomic Drive sync and deadlock age checks.

## Verification

- **Regression gate:** Expanded from `314/314` suites in `1.5.5` (and `380/380` in `1.5.6`) to **`386/386` standalone regression suites passing** on the Windows dev tree, plus the opt-in `tests/live_integrations.py` smoke matrix (`telegram`, `stt`, `tts`, `vision`, `searxng`, `mcp`, `send_file`).
- **Static & runtime checks:** `ruff` clean, `pyright` typing baseline reduced from `191` errors to `1` (`0` new errors), `docs/settings.md` up to date (`294` keys), `13/13` LSP language servers passing `deploy/lsp_selftest.py`, `13/13` Hugging Face `mmproj` specs verified online via `python tests/test_mmproj_specs.py --network`, and `.bat`/`.sh`/`static/app.js` syntax gates clean.
