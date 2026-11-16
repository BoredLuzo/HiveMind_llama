# Release 1.6.0

Dedicated `1.6.0` release on top of `1.5.5` (consolidating the unpublished `1.5.6`
sprint and the `1.6.0` release wave). Unifies capability-agnostic GGUF drop-in
discovery (plus a refreshed model catalog), Code Duo verification depth gating
and contract v2 extraction, patch-native editing (`apply_patch`), Direct run
pause/resume and mid-run steering, nested Deep Retrieval, MoE & large-GGUF
performance levers, shared LLM transport, security and supply-chain hardening,
modularized runner architecture, and the model-agnostic 3×3 Colab benchmark
harness.

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or the startup check offers it), or extract the release zip
next to the old install — `settings.json`, `models.json`, `sessions\`, `logs\`
and `.venv` are kept.

## Upgrade notes (behavior changes)

- **Updater is fail-closed:** a remote download is refused when the release metadata
  carries no SHA-256 digest or the digest does not match (`deploy/updater.py`).
  Nothing is changed in that case and a backup is kept. A locally supplied zip skips
  this check, so extracting the release zip next to the old install always works.
- **`POST /mcp` is stricter:** requests with `Sec-Fetch-Site: cross-site` or a
  non-loopback `Origin` are rejected (allow more origins via `MCP_HTTP_CORS_ORIGINS`),
  and a non-JSON `Content-Type` gets `415`. Clients that send no `Origin` header and
  JSON bodies (CLI/desktop MCP clients) are unaffected.
- **Text-only loads no longer attach the vision projector:** the first image request
  after a text-only load reloads the model with `mmproj` (one-time wait).
  `vision_mmproj_always=true` restores always-attach.
- **Large MoE GGUFs (>12 GB) skip `mlock`** even when `llama_mlock=true`.
  `llama_mlock_skip_large_moe=false` restores the old behavior.
- **New installer lineup:** the default preset changes (replaces `hermes3.6`). Models
  that are already installed are untouched.

## Highlights

### GGUF Drop-In Discovery & Model Catalog (`backend/gguf_meta.py`, `model_configs/`, `deploy/fetch_models.py`)
- **Capability-agnostic GGUF drop-in discovery:** Automatic GGUF quantization, architecture, context, split-shard (`-NNNNN-of-NNNNN`), and paired `mmproj` resolution without overwriting custom filenames; canonical `:latest` / `#N`, quant-variant fallback, and case-insensitive profile resolution, with explicit model profiles taking precedence over discovered values. MoE expert offload for unknown filenames still relies on name-based detection (`<N>B-A<M>B` pattern, ≥30B total) unless a profile or the `moe_cpu_experts` override sets it; `models_registry` cache invalidation and isolated `deepcopy` profile copies.
- **Refreshed catalog presets:** Updated the installer lineup (`deploy/fetch_models.py`, replacing the older `hermes3.6` default) with refreshed presets (`twil:8b`, `air:26b-a4b`, `qwen3.6:35b-a3b-mtp`) using shared chat-template, KV cache, and MTP/MoE profile configuration.

### Code Duo Verification Gate, Retrieval & Context (`core/duo_task_complete_gate.py`, `core/duo/preflight.py`, `core/tool_exec_helpers.py`, `core/tool_executor.py`)
- **Verification depth gating, repo-map deltas & smoke checks:** Post-edit verification depth gating: after a write, `task_complete` is vetoed (up to 3 times, with a harness auto-verify run) until a green test run or enough green exec depth follows; after that the run completes but is flagged as unverified. A one-shot `[SMOKE CHECK REQUIRED]` applies when no unit test suite was discovered; added cross-edit verification failure streak tracking (`verify_fail_streak`), dynamic `while` round-budget enforcement (replan bonuses apply without resetting the loop limit), head+tail command output preservation, workspace/src `PYTHONPATH` injection in `run_python`, and expanded Polyglot contract v2 extraction (`bench/import_polyglot.py`, `bench/runner.py`). `append_repo_map_delta` (`core/repomap_pin.py`, `core/duo_compress_cycle.py`) now attaches top-level symbol summaries per modified/created file so the pinned `[REPO-MAP-DELTA — changed since the pin]` block is refreshed at each compression, while `_compress_cycle` no longer masks `COMPRESSION FAILED` errors as valid local summaries.
- **KV-cache horizon, Pre-Explore fingerprinting & Deep Retrieval (`RepoIndex`):** Editor context injection appends after the byte-stable system prompt prefix rather than invalidating byte 0; `context/compression.py` and `core/duo_ctx_meter.py` add `cache_horizon` tail-first eviction and reset stale pre-mask/evict token counts so guard re-decisions never read stale values. Tiny top-level workspaces (≤4 text files, context-scaled cap) are pre-seeded into round 0; deeper repos use bounded `duo_deep_retrieval` with `camelCase`/`snake_case` identifier splitting and multi-language symbol indexing (Python, JS/TS, Rust, Go, Java, C/C++, C#, Ruby, PHP), while `explore/cache.py` includes `.gitignore`/manifest signatures in its invalidation fingerprint. `WOULD_DROP_SYMBOLS` includes the original definition line to prevent retry loops.

### Tools, Patch-Native Editing & LSP Auto-Diagnostics (`tools/`, `infra/lsp_client.py`)
- **`apply_patch` & edit compaction:** New `apply_patch` tool applies unified diffs across one or more files in a single call with line-drift tolerance, unique context matching, EOF no-newline handling, path-escape validation, atomic writes, and full rollback on failure (`old_text/new_text` and batch `edits=[...]` remain available). `_approval_call_key` / `_norm_write_path` (`tools/runner.py`) extract target paths from `apply_patch` `patch`/`diff` headers for per-target approval parity. Optional `duo_auto_test_on_edit` runs mapped tests (`duo_selective_tests`) in the same tool turn, and stale edit payloads in long histories compact to `[applied diff: path (+X/-Y)]` stubs.
- **Tool tiers & optional toggles:** `duo_core` remains the coder baseline (`18` base tools + optional `[+Subagent]` toggle = `19` default, saving ~39% schema/round tokens vs. full tier; `[+Websearch]`, `[+Git]`, or `duo_coder_tool_tier=full` add git/web/MCP/browser/LSP extras). `duo_lean` is streamlined to the **8 orthogonal core tools** (`read_file`, `edit_file`, `write_file`, `search_code`, `find_files`, `run_bash`, `run_tests`, `task_complete`) with `hive_functions/prompts.py`, `core/duo_coder_preloop.py`, `core/tool_exec_helpers.py`, and UI hints (`index.html`/`static/app.js`) aligned so recovery hints and prompts name only tools the active tier advertises (the lean split-write hint is tier-aware). Direct gains a `research` tier (`web + read/search`, no write/exec).
- **LSP background warm & auto-diagnostics:** Successful writes warm the language server in the background (`lsp_auto_warm_on_write`, bounded 3 s wait) and attach compiler/linter diagnostics autonomously, falling back to pyright/per-language lint when LSP is unavailable.
- **Git & outcome surfaces:** Added `/git/diff`, git checkpoint listing/restore UI panel, chat-scoped outcome hints, and a single engine-owned 30 s Ask-User auto-answer governor (`ask_user_auto_proceed`).

### Direct Run Control, Routing & Shared Transport (`core/chat_run.py`, `core/llm_transport.py`, `routing/`)
- **Direct `/pause`, `/resume` & mid-run steering:** `/pause` and `/pause-state/{chat_id}` now gate plain Direct streams as well as Direct `ToolLoop` rounds before model requests and at token chunks; `/resume` continues and abort during pause ends cleanly as `aborted`. Mid-run `[OWNER STEER]` messages queued before the answer ride into the prompt, steers queued during streaming trigger a bounded continuation round (max 2), and late steers deliver after the run.
- **Routing guards (`Auto` / `Direct` / `Agentic`):** `mode="auto"` with persisted `duo_agentic_mode=true` no longer hijacks general questions when the encoder decider is off (`core/chat_route_decision.py` checks `rule_decision` / `routing_judge` instead of unconditionally jumping to `code_duo`, and `rule_decision` adds `code_target` detection + `judge_cold` fallback); short read/list requests stay on Direct tools (`is_simple_tool_prompt` with coding-verb veto); the legacy tool route shares Direct prompt rules, respects `direct_tools_max_rounds` (1..300), and nudges identical repeated tool calls so read-only tasks finish with an answer.
- **Shared LLM transport & UI/scheduler sync:** Extracted `core/llm_transport.py` unifying `cache_prompt` and `preserve_thinking` / `thinking_budget` handling, prompt-only `usage_meta` tracking across Direct/Chat ledgers, `use_learned` gating on peer ratings (`direct_runner.py`, `pipeline.py`), idempotent Duo slot cleanup on disconnect/failure, safe `complexity` initialization on intent-agent early exits, `SchedulerManager` pending-fire race protection, synced Direct/Agent cards, and guarded frontend `localStorage` wrappers (`_hmStorageGet`/`_hmStorageSet` preventing Firefox blocked-storage error toasts).

### MoE & Backend Performance & Reliability (`backend/llama_config.py`, `backend/llama_start_phases.py`, `backend/manager_load.py`, `backend/manager_process.py`)
- **Large MoE `mlock` & Linux `RLIMIT_MEMLOCK` guards:** Large MoE GGUFs (`>12 GB` file size) no longer inherit global `llama_mlock=true` as `mmap+mlock` (avoiding up to 4.7x slower decode from NVMe paging; `llama_mlock_skip_large_moe=false` restores old behavior), and Linux `mmap+mlock` automatically downgrades when `RLIMIT_MEMLOCK` or free RAM is constrained.
- **Gate-aware MoE `ubatch` & thread split:** MoE models can raise `ubatch` via profile `"ubatch"` or `duo_ubatch_moe` when VRAM pre-flight fits the larger compute buffer (with baseline fallback); MoE prefill keeps full `--threads-batch` while decode uses physical-core `--threads`.
- **Vision skip & slot lifecycle:** Text-only loads skip loading the `mmproj` vision projector (`vision_mmproj_always=true` restores always-attach) while preserving vision on `image_url` steers and retries. Slot matching canonicalizes `:latest`/`#N` and casing to avoid reloads on role switches; LRU eviction runs off the event loop (`_evict_lru_async`) and skips `_loading` peer slots to prevent concurrent weight-load thrash.

### Security & Supply-Chain Hardening (`utils/file.py`, `tools/handlers/`, `infra/mcp_server.py`, `deploy/`)
- **Secret denylist, CSRF & workspace containment:** Native `read_file`, `list_files`, `find_by_name`, and `search_code` tools enforce the shared secret-path denylist (`.env`, SSH/TLS private keys, credentials, cloud/token configs); `run_tests` validates `paths`/`targets` via `os.path.commonpath` to block workspace escape; `model_configs` rejects path-traversal identifiers; `infra/mcp_server.py` enforces browser `Origin` checks, `415 Unsupported Media Type` on non-JSON `POST /mcp`, and scrubs secret environment variables from `tool_shell`.
- **Verified updates, Windows NSSM & Whisper Linux keys:** `deploy/updater.py` fails closed when release SHA-256 digests are missing or mismatched, `deploy/llama_build_pin.txt` pins verified `llama.cpp` build `b12001`, `deploy/fetch_whisper.py` maps Linux/Ubuntu asset keys properly, and `deploy/install_windows.bat` registers the NSSM service directly against `.venv\Scripts\python.exe run.py`.

### Architecture Split, Documentation & Python 3.12–3.14 (`core/`, `docs/`, `bench/`)
- **Modularized runners & updater:** Split `core/duo_runner.py` (`duo_coder_preloop`, `duo_task_complete_gate`, `duo_finalize`, `duo_finalgate`, `duo_epilogue`, `duo_round_input`, `duo_dtool_setup`, `duo_inloop_planner`, `duo_coder_load`, `duo_compress_cycle`, `duo_policies`, `duo_round_gate`, `duo_finish_round`, `duo_plan_round`, `duo_ctx_meter`, `duo_explore_gate`, `truncated_write_salvage`), `core/chat_run.py` (`chat_shortcuts`, `chat_vision_pre`, `chat_history_seed`, `chat_route`, `chat_route_decision`, `chat_tools_route`, `RunTokenLedger`, `PrefetchCoordinator`), `backend/llama_start_phases.py`, `core/version.py`, and `deploy/updater.py`.
- **Docs & Python compatibility:** Split `README.md` into a concise landing page plus `docs/guide.md` and `docs/privacy.md`; broadened `requires-python` to `>=3.12,<3.15` (with `tzdata` shipped in core dependencies).
- **Benchmark harness & 3×3 Colab Autopilot (`bench/`):** Added the D1 benchmark harness (`hivemind_direct`, `hivemind_duo`, `hivemind_duo_full`, `aider`, `hermes`, `pi` with shared `_agent_timeout_s`, hardened SSE token accounting via `_accumulate_hivemind_sse`, and hidden-test `conftest.py` isolation in `install_hidden_tests`), stripped `.approaches` solution spoilers from Polyglot tasks, enforced anti-cheat workspace isolation, added automatic server reload on commit change in `bench/external/rent_bootstrap.sh`, and structured `bench/external/hivemind_bench_colab_autopilot.ipynb` into per-run cells across **3 Suites × 3 Runs** (Core 23 tasks, Aider Polyglot 34 tasks imported at runtime / 10 shipped offline fixtures, DeepSWE 3 tasks — selectable for any local model) with atomic Drive sync and deadlock age checks.

## Verification

- **Regression gate:** Expanded from `314/314` suites in `1.5.5` (and `380/380` in `1.5.6`) to **`386/386` standalone regression suites passing** on the Windows dev tree, plus the opt-in `tests/live_integrations.py` smoke matrix (`telegram`, `stt`, `tts`, `vision`, `searxng`, `mcp`, `send_file`).
- **Static & runtime checks:** `ruff` clean, `pyright` typing baseline reduced from `191` errors to `1` (`0` new errors), `docs/settings.md` up to date (`294` keys), `13/13` LSP language servers passing `deploy/lsp_selftest.py`, `13/13` Hugging Face `mmproj` specs verified online via `python tests/test_mmproj_specs.py --network`, and `.bat`/`.sh`/`static/app.js` syntax gates clean.
