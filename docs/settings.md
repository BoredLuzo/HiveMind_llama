# HiveMind Settings

Generated from `settings.py` + `settings_defaults.py` (`DEFAULT_SETTINGS`) by
`python deploy/gen_settings_docs.py` - do not edit by hand; edit the default (and
its comment) in either file, then re-run the generator.

**312 settings keys.**

| Key | Type | Default | UI | Note |
|---|---|---|---|---|
| `_thinking_before_chunking` | null | `None` | yes | persisted user-preference before chunking forced thinking ON |
| `active_preset` | null | `None` | yes |  |
| `agents` | object | `{...}` | yes |  |
| `agents_user_model_choice` | object | `{}` | - | CARD-BOOKKEEPING (written by the agent-card save path; read by presets) |
| `agents_user_sampling_choice` | object | `{}` | - |  |
| `allow_cpu_offload` | bool | `True` | yes |  |
| `ask_user_auto_answer` | str | `Use best judgment, document decision ...` | yes |  |
| `ask_user_auto_proceed` | bool | `False` | yes | ASK-USER-AUTO-PROCEED (W2-3, ): real engine setting. When on, the ENGINE auto-answers an open ask_user question after 30 s (AUTO_PROCEED_TIMEOUT_S, infra/ask_user_governor.py) - the safety net behind the per-card "Auto 30s" timer. until_finished runs keep their own… |
| `ask_user_max_per_10min` | int | `5` | yes |  |
| `ask_user_throttle_pause_message` | str | `Agent is asking too many questions — ...` | - |  |
| `ask_user_timeout_until_finished_seconds` | int | `300` | yes |  |
| `automap_excluded` | list | `[]` | - |  |
| `automations_enabled` | bool | `True` | - | AUTOMATIONS : engine-owned scheduled runs (UI tab "Automations"). False is the kill switch; jobs live in automations.json, not here. |
| `chat_autosave_enabled` | bool | `True` | yes | UI-side only (B5, ): gates the browser's chat autosave - it flushes the chat json before /stream and after edits. The engine never reads it, and that is intentional: persistence is a client decision. Listed here so the checkbox is discoverable and round-trips through… |
| `constraint_mode` | bool | `True` | yes |  |
| `ctx_overrides` | object | `{...}` | yes |  |
| `debug_api_enabled` | bool | `False` | yes | DEBUG API : /debug/tasks returns asyncio task stacks with file paths, /debug/models registry and filesystem internals, /thinking/debug and /budget/debug expose internals - all on an unauthenticated loopback API. Off by default; the router is not even mounted… |
| `debug_dump_request` | bool | `False` | yes | DEBUG-DUMP : request instrumentation. Off by default; when set, every model request is written to logs/telemetry/dump_<role>_<chatid>_<ts>.json exactly as sent (core/telemetry.py, role "planner" or "coder"). The key is listed here so the switch is discoverable and… |
| `default_keep_alive` | str | `10m` | yes |  |
| `default_unprofiled_sampling` | object | `{...}` | yes | Default sampling parameters for unprofiled GGUF models. |
| `desktop_notifications` | bool | `True` | yes | Windows toasts for run start/end and approval cards. |
| `dflash_draft_p_min` | float | `0.4` | yes | Minimum draft token acceptance probability for DFlash. |
| `direct_tools_enabled` | bool | `True` | yes |  |
| `direct_tools_max_rounds` | int | `12` | yes |  |
| `direct_tools_tier` | str | `readonly` | yes | off \| readonly \| research \| python \| mcp \| full |
| `disable_thinking_in_planner` | bool | `False` | yes |  |
| `document_max_chars` | int | `120000` | yes | PDF/text extraction limits (engine /documents/extract). The gateway sends bytes, the engine answers text or a DOC_* code. |
| `document_max_pages` | int | `40` | yes |  |
| `duo_action_approval_enabled` | bool | `False` | yes | When on, pauses before mutating/exec tools (run_bash, run_python, install_package, start_background, run_tests, write_file, edit_file, apply_patch, write_file_append, undo_last, browser, lsp_rename, lsp_format, git_commit, dynamic MCP tools). Approve once, remember per exact… |
| `duo_action_approval_timeout_s` | int | `0` | yes | Auto-DENY an unanswered approval card after N seconds (0 = off, attended runs wait as before). With N > 0 and nobody at the desk the gated call is DENIED once (fail closed, review: the old auto-approve-once behavior approved unseen actions) - the gate wait ends… |
| `duo_agentic_mode` | bool | `True` | yes | DEFAULT-SUBMODE: the agentic coder (one model, solo in VRAM) is the default; the Critic-Duo is one click away and costs a second model. |
| `duo_agentic_thinking` | bool | `False` | yes |  |
| `duo_auto_test_max_per_run` | int | `2` | yes |  |
| `duo_auto_test_on_edit` | bool | `False` | yes | AUTO-TEST-ON-EDIT (G#2 lever, ): after a successful edit/write/apply_patch, run the testmap-mapped tests of that file in the SAME tool turn and append an [AUTO-TEST] note to the result. Default OFF (opt-in): nothing runs unless enabled. Bounds: mapped test files only… |
| `duo_auto_test_timeout` | int | `60` | yes |  |
| `duo_caps` | object | `{}` | - |  |
| `duo_chunking` | bool | `False` | yes | CHUNKING DEFAULT OFF : stays a working opt-in, but a fresh install runs one coder pass (the planner subtasks are skipped with an honest status) instead of silently chunking. |
| `duo_coder_after_run` | str | `keep` | yes | CODER LIFETIME : keep = the slot stays warm after the run (idle TTL decides), evict = release it at the run end so the next prompt can load any model/mode without evicting anything. |
| `duo_coder_always_vision` | bool | `False` | yes | Preload --mmproj on the coder slot even on text-only tasks so mid-run image steers skip a reload (costs projector VRAM; ignored if Coder off). |
| `duo_coder_ctx_agentic` | null | `None` | yes |  |
| `duo_coder_ctx_normal` | null | `None` | yes |  |
| `duo_coder_explore_chars` | int | `0` | yes |  |
| `duo_coder_fallback_model` | str | `` | yes |  |
| `duo_coder_images_off` | bool | `False` | yes | Explicit toggle : hard "the coder takes NO images" — plan says none for the coder, the coder slot never loads the projector, and image-carrying steers to the coder are dropped. Saves the mmproj VRAM even when an image arrives in the run. |
| `duo_coder_model` | str | `` | yes | Automatic/override keys that DEFAULT_SETTINGS now exposes explicitly (previously only implicit read fallbacks in the consumers). |
| `duo_coder_tool_thinking` | bool | `False` | yes |  |
| `duo_coder_tool_thinking_auto_mode` | str | `off` | yes |  |
| `duo_coder_tool_tier` | str | `core` | yes | CORE CODER TIER: default "core" (19 base tools incl. read/edit/apply_patch/ write/run/tests/lsp_diagnostics + optional subagent) instead of "full" (33 base + git/web/MCP/browser/LSP extras). Full is opt-in. |
| `duo_coder_ttl_seconds` | int | `0` | yes | 0 = auto (420s), >0 = Override |
| `duo_coding_mode` | bool | `True` | yes |  |
| `duo_compress_auto_floor` | float | `0.7` | yes |  |
| `duo_compress_llm_timeout_s` | int | `180` | yes |  |
| `duo_compress_local_only` | bool | `False` | yes |  |
| `duo_compress_mode` | str | `auto` | yes | Context compression strategy ("auto", "mask_only", "partial", or "full"). |
| `duo_compress_model` | str | `auto` | yes |  |
| `duo_compress_overflow_reserve` | int | `1024` | yes |  |
| `duo_compress_tail_tool_max_chars` | int | `8000` | yes | Chars kept from tool output when a compression tail-caps it (read by duo_runner with an 8000 fallback). |
| `duo_compress_threshold` | int | `0` | yes |  |
| `duo_concise_write_thinking` | bool | `False` | yes | Opt-in concise-thinking hint for file writes: tells reasoning models not to draft full file contents inside <think> before calling write_file, edit_file, or apply_patch. Off by default; live toggle in Advanced Settings -> Agentic. |
| `duo_context_budget_chars` | int | `8000` | yes | CONTEXT BUDGET (D2, ): upper char cap for the tiny-workspace pre-seed block (core/duo_helpers.build_preseed_context). The effective cap is min(this, ctx_tokens/4, floor 2000); 8000 keeps the old behavior. |
| `duo_critic_ctx` | null | `None` | yes |  |
| `duo_critic_model` | str | `` | yes |  |
| `duo_critic_tools` | bool | `False` | yes |  |
| `duo_deep_retrieval` | bool | `True` | yes | DEEP RETRIEVAL (D2, ): when the tiny-workspace pre-seed produces nothing, the repo index (context/repo_index.py, BOUNDED walk: hidden/heavy dirs pruned, depth + file caps) ranks nested repo files against the task text and the coder's first message gets a compact hint… |
| `duo_deep_retrieval_max_chars` | int | `3000` | yes | Upper char cap for the hint block; ranked hints that do not fit are dropped (never truncated mid-line). |
| `duo_deep_retrieval_max_files` | int | `5` | yes | How many ranked files may appear in the hint block (clamped 1..32). |
| `duo_error_rollup` | bool | `True` | yes | ERROR ROLLUP : deterministic dedup of repeating test/compile failures in the fix-loop (core/error_rollup.py). First failure full, repeats rendered as PERSISTS/FIXED/REOPENED. Runtime state per run. |
| `duo_evict_on_pause` | bool | `False` | yes | EVICT-ON-PAUSE : while a run waits for an ask_user answer the coder sits in VRAM untouched. On a small card that blocks every other model load; this releases it until the run resumes. Off by default - the reload costs a few seconds. Approval cards keep the model… |
| `duo_evict_planner_on_coder` | bool | `True` | yes | SERIAL-SLOTS : unload the planner when the coder phase starts and the models differ (frees VRAM for coder ctx; costs one reload when switching back). |
| `duo_git_autocommit` | bool | `False` | yes |  |
| `duo_git_checkpoints` | bool | `False` | yes | OFF : a checkpoint is a real git commit in the user's repo. Opt-in; a bad write is still covered by undo_last + the write transaction. |
| `duo_image_mode` | null | `None` | yes | Duo image plan : how the agentic duo run handles attached images. None = derived (active vision_model.json -> "preprocess", else "direct"); explicit "direct"/"preprocess" wins. duo_image_to_planner/_coder are the per-role switches for… |
| `duo_image_to_coder` | bool | `False` | yes |  |
| `duo_image_to_planner` | bool | `False` | yes |  |
| `duo_index_max_depth` | int | `6` | yes | max directory levels below the root |
| `duo_index_max_files` | int | `2000` | yes | hard file cap for the index walk |
| `duo_install_max_calls` | int | `3` | yes |  |
| `duo_llm_slow_timeout_s` | int | `300` | yes |  |
| `duo_mask_before_compress` | bool | `True` | yes | MASK-FIRST : deterministic observation masking (stale tool outputs -> recall markers) runs before the LLM summary and can resolve the threshold crossing without it — no LLM call, no slot evict. |
| `duo_max_compressions` | int | `40` | yes |  |
| `duo_max_tool_errors` | int | `12` | yes | Tool-error hard-stop cap for agentic runs. Successful non-write calls decay the counter by 1, successful writes reset it to 0 — only a no-progress run (this many fails with nothing succeeded in between) is stopped. |
| `duo_max_tool_rounds` | int | `64` | yes |  |
| `duo_max_tool_rounds_runtime_cap` | int | `300` | yes |  |
| `duo_noop_hint_enabled` | bool | `True` | yes | NO-OP hint : injects a read_file nudge after 2x ineffective edits on the same path (Python run-state, compression-proof). |
| `duo_pair` | str | `focused` | yes | UI state only: remembers the Duo "pair" selector position. The same request also sets agents.duo_coder.model, which IS what the engine reads. |
| `duo_parallel_preexplore` | bool | `False` | yes |  |
| `duo_partial_compression` | bool | `False` | yes | CACHE-FRIENDLY : protect the llama.cpp prefix cache. LEGACY-REMOVAL : duo_cache_friendly_ctx was removed — compression-first is the only regime. - duo_partial_compression: condense only the old part and keep the raw tail byte-identical at the end (KV… |
| `duo_partition_max_files` | int | `30` | yes |  |
| `duo_pass_explore_files` | str | `touched` | yes |  |
| `duo_peer_ratings_agentic` | bool | `False` | yes |  |
| `duo_pin_static_map` | bool | `True` | yes | REPO-MAP-PIN : put "## Static Repo-Map" into the system message (byte-stable = cache-stable across compressions). Deltas/TODO consolidation see core/repomap_pin.py. Baseline : post-compress cached was ~6905/7137 (system+tools) and… |
| `duo_planner_ctx_cap` | null | `None` | yes |  |
| `duo_planner_ctx_target` | null | `None` | yes |  |
| `duo_planner_default_thinking` | bool | `True` | yes |  |
| `duo_planner_enabled` | bool | `True` | yes | Planner on/off for the request path (server.py reads this key). DEFAULT-PLANNER: plan first. The planner shares the coder context (duo_planner_use_coder_ctx), so this costs no second model load. |
| `duo_planner_ensure_load_timeout_s` | int | `300` | yes |  |
| `duo_planner_max_steps` | int | `0` | yes |  |
| `duo_planner_max_tokens` | int | `8000` | yes | PLANNER-OUTPUT-BUDGET (0.99.2, user decision): visible task budget |
| `duo_planner_model` | null | `None` | yes |  |
| `duo_planner_thinking_budget` | int | `8000` | yes | Thinking → hermes-MoE thought until the 600s wall timeout and delivered empty output. 4k was too tight for complex plans → 8k default . |
| `duo_planner_thinking_timeout_s` | int | `600` | yes |  |
| `duo_planner_ttl_seconds` | int | `0` | yes | 0 = auto (450s), >0 = Override |
| `duo_planner_use_coder_ctx` | bool | `True` | yes |  |
| `duo_planner_use_exec_model` | bool | `True` | yes |  |
| `duo_pre_explore` | bool | `False` | yes |  |
| `duo_pre_explore_ctx` | int | `4096` | yes |  |
| `duo_pre_explore_ctx_char_ratio` | float | `3.0` | yes |  |
| `duo_pre_explore_llm_timeout_s` | int | `600` | yes |  |
| `duo_pre_explore_max_files_est` | int | `15` | yes |  |
| `duo_pre_explore_max_tools` | int | `20` | yes |  |
| `duo_pre_explore_timeout_per_file_s` | int | `20` | yes |  |
| `duo_pre_explore_timeout_seconds` | int | `600` | yes |  |
| `duo_pre_explore_tokens` | int | `700` | yes |  |
| `duo_profile_quality_model` | str | `` | yes |  |
| `duo_profile_speed_model` | str | `` | yes |  |
| `duo_pyright_path` | str | `` | yes |  |
| `duo_read_timeout` | int | `390` | yes |  |
| `duo_repo_memory_enabled` | bool | `True` | yes |  |
| `duo_repo_memory_min_score` | float | `0.12` | yes |  |
| `duo_repo_memory_top_k` | int | `2` | yes |  |
| `duo_rounds_balanced_cap` | int | `3` | yes |  |
| `duo_run_bash_build_timeout_s` | int | `600` | yes |  |
| `duo_run_timeout_critical_seconds` | int | `900` | yes |  |
| `duo_run_timeout_seconds` | int | `420` | yes |  |
| `duo_runtime_profile` | str | `balanced` | - |  |
| `duo_selective_tests` | bool | `True` | yes | SELECTIVE-TESTS (D4, ): run_tests scopes pytest/npm to the testmap-mapped tests of this run's written files; [] falls back to the full suite. Explicit paths/targets win; set false to always run all. |
| `duo_soft_planner_wall_timeout_s` | int | `300` | yes |  |
| `duo_static_map_chars` | int | `0` | yes | 0 = tier-derived (rich: 8000); >0 = explicit static repo-map char budget |
| `duo_symbol_ref_enabled` | bool | `True` | yes |  |
| `duo_symbol_ref_max_items` | int | `120` | yes |  |
| `duo_symbol_ref_top_k` | int | `2` | yes |  |
| `duo_test_feedback` | bool | `False` | yes |  |
| `duo_test_feedback_chunk` | bool | `True` | yes | LOOP-DEFAULT : per-chunk auto-test + self-fix now ships ON. The EDIT→TEST→FIX loop is this harness's core idea — the per-chunk verify (and the edit→test mapping) only ran for users who found this toggle, so most agentic runs finished without any test feedback. Both… |
| `duo_test_feedback_final` | bool | `True` | yes | TESTING-DEFAULT : the final auto-test is on by default — |
| `duo_thinking_per_chunk` | bool | `False` | yes |  |
| `duo_tool_autopromote_max_rounds` | int | `4` | yes |  |
| `duo_tool_output_ttl` | int | `3` | yes |  |
| `duo_tool_rounds` | int | `50` | yes | Agentic tool rounds per chunk. 50 : a fresh install must be able to write files; 0 silently disabled the whole tool loop and chunking then produced text-only "implementations". |
| `duo_tool_sandbox` | bool | `True` | yes | Sandbox subprocesses (run_bash/run_python/background) via Windows Job Objects (KILL_ON_JOB_CLOSE). |
| `duo_tool_sandbox_max_cpu_s` | int | `0` | yes | POSIX only (Linux/macOS): RLIMIT_CPU seconds per tool child. 0 = unset. |
| `duo_tool_sandbox_max_mem_mb` | int | `4096` | yes | Job resource limits (0 = unlimited) for run_bash/run_python/background. |
| `duo_tool_sandbox_max_procs` | int | `64` | yes |  |
| `duo_tool_thinking_budget_cap` | int | `0` | yes | TOOL-THINKING BUDGET CAP: upper bound for the per-round thinking budget in agentic tool rounds (only has an effect when tool thinking is on via duo_coder_tool_thinking / the reactive auto mode). 0 = no cap (model default). On a 35B-A3B at ~14-20 tok/s every <think> block before… |
| `duo_tree_scout_enabled` | bool | `True` | yes |  |
| `duo_tree_scout_max_depth` | int | `4` | yes |  |
| `duo_tree_scout_max_files` | int | `200` | yes |  |
| `duo_ubatch` | int | `512` | yes | Prompt-processing chunk size (llama.cpp --ubatch-size). Larger values (512/1024) speed up prefill substantially for MoE models with CPU expert offloading (fewer RAM-streaming passes over the expert weights), at the cost of a larger VRAM compute buffer. Test lever for slow… |
| `duo_ubatch_moe` | int | `0` | yes | MoE UBATCH RAISE: 0 = the model profile decides ("ubatch" key, MoE models only — e.g. the 35B-A3B profile ships 512 for the measured +24..55% prefill); a value > 0 forces this ubatch for every MoE model; a negative value vetoes any raise and keeps duo_ubatch everywhere. A raise… |
| `duo_until_finished_cap` | int | `999999` | yes |  |
| `duo_until_finished_max_hours` | int | `24` | yes | UNTIL-FINISHED-BOUND : until-finished runs are meant to work for hours - the operator can always kill a run (/stop, the Stop button, the phone). The wall clock is therefore generous by default (24 h) and only there so a wedged model cannot hold the machine forever;… |
| `duo_use_pipeline` | bool | `False` | yes |  |
| `duo_websearch_enabled` | bool | `False` | yes |  |
| `duo_websearch_max_calls` | int | `20` | yes |  |
| `duo_websearch_timeout_critical_seconds` | int | `24` | yes |  |
| `duo_websearch_timeout_fast_seconds` | int | `13` | yes |  |
| `duo_websearch_timeout_seconds` | int | `20` | yes |  |
| `duo_wedge_handoff_enabled` | bool | `True` | yes | WEDGE-HANDOFF: when the coder fails the same edit on one file repeatedly (streak threshold, or identical call twice within a streak), delegates that edit to a fresh-context fix agent (same model weights, clean window). Default ON (includes run_bash wedges; see EDIT_FAMILY_TOOLS). |
| `duo_wedge_handoff_max_per_run` | int | `2` | yes |  |
| `duo_wedge_handoff_max_rounds` | int | `10` | yes |  |
| `duo_wedge_handoff_max_tokens` | int | `1200` | yes | MAX-TOKENS-KEY : core/fix_agent.py has always read this key (int(_s("duo_wedge_handoff_max_tokens", 1200))), but it was missing here and in docs/settings.md - the value was fixed at 1200 and could not be configured. Listed now so the UI/settings.json can set it. |
| `duo_wedge_handoff_model` | str | `` | yes | empty = coder model (fresh context, same weights) |
| `duo_wedge_handoff_streak_threshold` | int | `4` | yes |  |
| `duo_wedge_handoff_timeout_s` | int | `900` | yes |  |
| `duo_worker_slots` | int | `2` | - |  |
| `duo_write_chars_per_token` | float | `2.5` | yes | truncated at the output-token limit (finish_reason=length -> DROPPED -> loop), coupled to the token budget: max_chars ~ budget_tokens * factor - overhead. Step; cap 3.3 (documented measured value). |
| `duo_write_guard_enabled` | bool | `True` | yes | WRITE-GUARD duo_full : blocks write_file on existing files never seen in this run (rollback lever like duo_error_rollup / duo_pin_static_map). False = old behavior (blind write_file allowed). |
| `exploration_agent` | object | `{...}` | yes |  |
| `git_auto_push` | bool | `False` | yes |  |
| `git_commit_prefix` | str | `hivemind:` | yes |  |
| `git_default_branch` | str | `main` | yes |  |
| `git_email` | str | `` | yes |  |
| `git_repo_url` | str | `` | yes |  |
| `git_token` | str | `` | yes |  |
| `git_tools_enabled` | str | `auto` | yes | GIT-TOOLS-GATE : git_status/git_commit require configured git credentials (repo_url / username / email / token) AND not being toggled off ("false"). Without credentials they remain effectively off. |
| `git_username` | str | `` | yes |  |
| `gpu_backend` | str | `` | yes |  |
| `image_desc_full_pipeline` | bool | `False` | yes |  |
| `image_uploads_persistent` | bool | `False` | yes | False = uploads live only as base64 inside the run (nothing on disk); True = kept under <workspace>/.hive_uploads and referenced by path for Direct, Duo Coder, and Pipeline Analyst runs. |
| `intent_agent` | object | `{...}` | yes |  |
| `judge_keepalive_enabled` | bool | `False` | yes |  |
| `judge_prefetch_before_complexity` | bool | `False` | yes | OFF : warms the judge model before the routing decision - pure latency win on a big card, an eviction risk on a small one. Opt-in. |
| `keep_awake_during_run` | bool | `True` | yes |  |
| `learning_preset_mode` | bool | `False` | yes |  |
| `llama_cache_ram_mib` | int | `1024` | yes |  |
| `llama_cache_reuse` | int | `256` | yes | 0 = off, 256 = llama.cpp recommendation. |
| `llama_ctx_checkpoints` | int | `4` | yes | HOST-RAM / VRAM TREND : llama.cpp keeps context checkpoints and a host-side prompt cache per slot, and both grow with how much a slot has processed. A long run got slower the longer it ran. 0 disables. |
| `llama_extra_args` | str | `` | yes | Extra CLI flags appended to llama-server launches. |
| `llama_fit` | bool | `True` | yes | FIT : default ON - llama.cpp --fit sizes unset args (layers, ctx) to the free VRAM. Turn it OFF when you set your own MoE distribution (moe_cpu_experts) or a fixed --n-gpu-layers. Target is the per-device margin in MiB (0 = llama default). |
| `llama_fit_target_mib` | int | `0` | yes |  |
| `llama_gpu_device` | int | `0` | yes | GPU device index passed to llama-server (--device). |
| `llama_gpu_layers_auto` | bool | `False` | yes | Auto-fit : omit --n-gpu-layers so llama.cpp fits the layers to the free VRAM itself — it refuses while the flag pins 99. Default keeps the explicit full-offload behavior. |
| `llama_kv_cache_type` | str | `q4_0` | yes | Global KV cache quantization type ("q4_0", "q8_0", or "f16"). |
| `llama_max_slots` | int | `3` | yes | Max concurrent llama-server slots. |
| `llama_mlock` | bool | `True` | yes |  |
| `llama_mlock_skip_large_moe` | bool | `True` | yes | LARGE-MOE MLOCK SKIP: llama_mlock=True forces --load-mode mmap+mlock, which on large MoE GGUFs (>12 GB, experts streamed from RAM) measures up to 4.7x slower decode than load-mode none (llama-bench tg128 on the 35B-A3B: 2.92 t/s mmap+mlock vs 13.77 t/s none) because the locked… |
| `llama_ubatch_size` | int | `512` | yes |  |
| `llama_update_channel` | str | `latest` | yes | llama.cpp binary update channel ("latest" or "pinned"). |
| `llama_vk_disable_host_visible_vidmem` | bool | `True` | yes | VULKAN-WDDM-PIN : on Windows Vulkan (AMD/Intel/Nvidia dGPUs with ReBAR), ggml-vulkan prefers HOST_VISIBLE\|DEVICE_LOCAL heaps that WDDM can silently demote into shared system RAM under GPU commit pressure (measured 15-21x decode drops on RX 6600). Pinning… |
| `lsp_auto_diagnostics` | bool | `True` | yes | Attach a compact diagnostics block to a successful write/edit. A warm server is used directly; a cold one is started in the background (lsp_auto_warm_on_write) and the write waits at most 3 s for it. Failures are silent - a broken/missing server never fails a write. |
| `lsp_auto_warm_on_write` | bool | `True` | yes |  |
| `lsp_client_timeout_s` | int | `30` | yes | Per-request timeout in seconds for one LSP request; also caps the initialize handshake. A hung server is killed and lazily restarted on the next call. |
| `lsp_diagnostics_wait_s` | float | `5.0` | yes | How long lsp_diagnostics waits for the server's diagnostics publication after open/change before returning what it has. |
| `lsp_enabled` | bool | `True` | yes | lsp_enabled is the ONLY switch - there are deliberately no per-language on/off flags. True = the lsp_* tools exist; a language server process still starts LAZILY on the first tool call, never at boot. A language without a found server command answers LSP_UNAVAILABLE (with an… |
| `lsp_forward_workspace_settings` | bool | `False` | yes | LSP-SECURITY : repo .vscode/settings.json can select the command a server runs; forward it only in trusted workspaces. |
| `lsp_idle_timeout_s` | int | `600` | yes | Close a language server after this many idle seconds (0 = off). The next lsp_* call restarts it lazily; keeps commit/RAM down in long sessions where a server is used once and never again. |
| `lsp_max_diagnostics` | int | `50` | yes | Output caps for the lsp_* tools (results per call). |
| `lsp_max_results` | int | `50` | yes | cap for locations/symbols/actions lists |
| `lsp_servers` | object | `{}` | yes | Optional command override per language id, read by the LSP client: {"python": "pyright-langserver --stdio", "rust": ["rust-analyzer"]} A configured command REPLACES the default candidates for that language and must resolve (otherwise the language reports as missing - no silent… |
| `max_concurrent_models` | int | `2` | yes |  |
| `max_iterations` | int | `2` | yes |  |
| `max_model_size_gb` | float | `6.0` | yes |  |
| `mcp_client_enabled` | bool | `False` | yes | MCP client (beta): connect external MCP servers over stdio or HTTP. Off by default; phone runs stay gated by telegram_phone_restricted. |
| `mcp_client_max_calls_per_run` | int | `40` | yes |  |
| `mcp_client_timeout_s` | int | `30` | yes |  |
| `mcp_server_budgets` | object | `{}` | yes | Per-server MCP client call budgets: {"server-name": max calls per rolling hour}. Absent/0 = unlimited (previous behavior). |
| `mcp_servers` | list | `[]` | yes | {"name": "...", "command": "npx", "args": ["-y", "@playwright/mcp@latest"]} (stdio) {"name": "...", "url": "http://localhost:9000/mcp"} (streamable HTTP) as additional tools. External MCP servers (client direction): every entry is a stdio or… |
| `mem_watch_enabled` | bool | `True` | yes | [MEM] commit-charge snapshots at startup/load/evict/run-end. |
| `mode` | str | `chat` | yes | DEFAULT-MODE: a fresh install answers in the chat/direct route. The old value "simple" is the same route under a legacy name - the UI calls it CHAT, so the default says chat. |
| `model_capability_overrides` | object | `{}` | yes |  |
| `models_dir` | str | `` | - |  |
| `moe_cache_mib` | int | `0` | yes | EXPERT-CACHE : opt-in llama.cpp --moe-cache-mib. Keeps hot MoE experts in VRAM (decode only, small batches). 0 = off. Vulkan builds can crash at load (#30697); the loader retries once without the flag. |
| `moe_cpu_experts` | object | `{}` | yes |  |
| `mtp_draft_n_max` | int | `2` | yes | Maximum speculative draft tokens per step for MTP models. |
| `mtp_draft_p_min` | float | `0.0` | yes | Minimum draft token acceptance probability for MTP (0.0-0.95). |
| `pin_direct_after_response` | bool | `False` | yes |  |
| `pipeline_vision_direct` | bool | `False` | yes | PIPELINE-VISION : feed images directly to multimodal pipeline agents? |
| `pipeline_vision_roles` | object | `{}` | yes | Per-role direct-image flags, e.g. {"analyst": true}. |
| `pipeline_websearch_enabled` | bool | `True` | yes |  |
| `plan_tracker_classifier` | str | `heuristic` | - |  |
| `planner_reasoning_effort` | str | `auto` | yes | PLANNER-REASONING-EFFORT : independent sidebar reasoning effort for the Code Duo Planner ("auto" follows Planner Thinking + planner model default). |
| `prefer_smaller_models` | bool | `True` | yes |  |
| `prefetch_agent_avgs` | object | `{}` | - |  |
| `prefetch_lead_seconds` | float | `8.0` | yes |  |
| `preload_workers_after_run` | bool | `False` | yes |  |
| `read_guard_enabled` | bool | `True` | yes |  |
| `reasoning_effort` | str | `auto` | yes | REASONING-EFFORT : template-driven composer reasoning effort / thinking toggle ("auto" inherits per-agent sidebar/card settings + model profile defaults; independent of Think-Budget sliders). |
| `routing_decider` | str | `off` | yes | ── ROUTING : two independent deciders, each switchable ── The LLM judge costs a model load into VRAM (60 s + eviction) and returns text that has to be parsed. The decider is an encoder (GLiNER2.5, CPU, ~0.5-1.2 s, no VRAM) and is optional (pip install… |
| `routing_decider_model` | str | `fastino/GLiNER2.5-Decide` | yes |  |
| `routing_decider_threshold` | float | `0.55` | yes | Below this confidence the decision is marked uncertain (and goes to the judge when active). Calibrated on the multi-label action labels: hits were 0.49-0.92 (median 0.72) - 0.85 would have escalated almost everything. |
| `routing_judge` | str | `on` | yes |  |
| `routing_planner` | str | `auto` | yes | Who decides whether a planner is needed? auto = decider when running, otherwise judge (avoids the GPU hit) decider \| judge = fixed always \| never = planner always / never |
| `run_bash_shell` | str | `auto` | yes | run_bash shell on Windows: "auto" = Git Bash when present (small models emit POSIX bash; powershell chokes on every line), else powershell. "bash"/"powershell" force one. |
| `safe_profile_matrix_file` | str | `model_configs/safe_profile_matrix.json` | - |  |
| `safe_profile_policy` | str | `default_8gb_v1` | - |  |
| `searxng_engines` | str | `` | yes | Leave empty: HiveMind lets SearXNG query its healthy enabled general engines (yahoo, duckduckgo web, google cse — bing/duckduckgo-api stay disabled). Legacy "bing,duckduckgo" pins are auto-cleared on load. |
| `searxng_host` | str | `http://localhost:8888` | yes |  |
| `searxng_language` | str | `en` | yes | Keep "en" by default so technical/developer searches stay in English regardless of host IP geolocation (multilingual engines like yahoo and duckduckgo web still return native results when querying in German). |
| `server_port` | int | `8001` | - |  |
| `session_compress_threshold` | int | `20` | yes |  |
| `session_history_budget_tokens` | int | `0` | yes | Token budget for the conversation history a run sees . Replaces blind message-count caps: newest turns verbatim, older turns dropped whole once the budget is full. 0 (default) derives the budget from the direct model's context (35% of n_ctx minus a 4096 reserve for… |
| `smart_preload_enabled` | bool | `False` | yes | OFF by default : prefetch/keep-warm loads the NEXT agent's model while the current one works - great on a big card, but on a tight VRAM budget it can evict the model that is actually answering. Opt in when the card has room. |
| `smart_preload_keep_alive` | str | `10m` | yes |  |
| `soul_evolve_agent` | object | `{...}` | yes |  |
| `soul_skill_distillation` | bool | `False` | yes | DEFAULT OFF: the distiller spends one extra LLM call after a run. It is useful, but a fresh install should not grow background model work nobody asked for - switch it on in the SOUL tab. |
| `soul_skill_writing` | bool | `False` | yes |  |
| `spec_decode_mode` | str | `auto` | yes | Speculative decoding mode ("auto" or "off"). |
| `startup_preload_enabled` | bool | `False` | yes |  |
| `stt_enabled` | bool | `False` | yes | English only: one CLI call per request, tier chosen by RAM unless set. |
| `stt_language` | str | `en` | yes | English only |
| `stt_log_transcripts` | bool | `False` | yes |  |
| `stt_max_seconds` | int | `300` | yes |  |
| `stt_model` | str | `` | yes | "" = RAM default (parakeet q4_k >= 8 GB) |
| `stt_preload` | bool | `False` | yes | warm whisper-server (whisper models only) |
| `stt_threads` | int | `0` | yes | 0 = min(4, cpu//2) |
| `stt_timeout_s` | int | `120` | yes |  |
| `stt_translate` | bool | `False` | yes | whisper --translate (English target; whisper tiers only) |
| `subagent_lite_cooldown_s` | int | `60` | yes |  |
| `subagent_lite_ctx_default` | int | `8192` | yes |  |
| `subagent_lite_enabled` | bool | `False` | yes | SUBAGENT-LITE : serial, default off so fresh installs do not spawn extra delegate loads unless enabled. |
| `subagent_lite_max_tokens` | int | `700` | yes |  |
| `subagent_lite_max_tools` | int | `12` | yes |  |
| `subagent_lite_min_free_ram_gb` | float | `5.0` | yes |  |
| `subagent_lite_model_ladder` | list | `[]` | yes | Ordered model ladder for subagent-lite ([] = auto-pick). |
| `subagent_lite_safety_margin_mib` | int | `256` | yes |  |
| `subagent_lite_timeout_s` | int | `120` | yes |  |
| `telegram_approval_mode` | str | `deny` | yes |  |
| `telegram_image_in` | bool | `True` | yes | IMAGES : a photo sent to the bot becomes a run with the caption as prompt and the picture attached (engine /stream images). Needs a vision-capable model for the active route, or a configured vision agent that preprocesses the picture into text. |
| `telegram_mirror_enabled` | bool | `False` | yes | Gateway-owned keys (phone UI and /gate write them). telegram_gateway_enabled is deliberately NOT defaulted: the gateway treats "key absent" as "not configured, the toml master switch decides" and "key present and false" as a veto. A shipped False would shut down every toml-only… |
| `telegram_phone_restricted` | bool | `True` | yes |  |
| `telegram_progress_msgs` | bool | `True` | yes |  |
| `telegram_result_mode` | str | `both` | yes | What a phone run sends back (1.4.2). "both" = text + voice note, "text" = text only, "voice" = only the model-generated answer as a voice note (the progress lines stay text). telegram_progress_msgs turns the info lines off entirely for a pure "results only" channel. |
| `telegram_voice_in` | bool | `False` | yes | Telegram gateway voice (per bot, opt-in). The gateway reads these from the engine, so the UI switches drive it; greying follows stt_enabled / tts_enabled (a voice note needs STT, a voice answer needs TTS). |
| `telegram_voice_out` | bool | `False` | yes |  |
| `total_vram_mib_override` | int | `0` | yes | Manual total GPU VRAM override in MiB (0 = auto-detect). |
| `tts_backend` | str | `pocket` | yes | TTS engine: "pocket" (Kyutai Pocket TTS via uvx, CC BY 4.0) or "woof" (ConwayResearch/Underdog-Woof CPU NumPy ~41 MB, non-commercial only). |
| `tts_enabled` | bool | `False` | yes | Opt-in like STT: nothing starts until tts_enabled is on. Default voice is anna (CC BY 4.0) at 1.08x; WSOLA applies speed factor. |
| `tts_idle_stop_s` | int | `900` | yes |  |
| `tts_normalize` | bool | `True` | yes | Server-side levelling of the sidecar output (measured -15.5 dBFS peak). |
| `tts_preload_run` | bool | `True` | yes | Warm the TTS engine when a run starts instead of when the first chunk is ready - the sidecar then needs ~1 s per sentence instead of a 12 s cold start. Useful for phone runs where nobody touched the PC. |
| `tts_read_agentic` | bool | `False` | yes |  |
| `tts_read_ask` | bool | `False` | yes |  |
| `tts_read_direct` | bool | `False` | yes | Read-aloud (frontend): what the UI speaks on its own when TTS is on. Direct = the whole answer; agentic = the plan first, then the coder's answer; ask/gate read the interactive cards. All default off. |
| `tts_read_gate` | bool | `False` | yes |  |
| `tts_speed` | float | `1.08` | yes |  |
| `tts_timeout_s` | int | `120` | yes |  |
| `tts_voice` | str | `anna` | yes |  |
| `tts_volume` | float | `1.0` | yes | Browser playback level (0..1). The engine already normalises the WAV to -1.5 dBFS peak, so this is the user's "how loud do I want it here" knob. |
| `until_finished` | bool | `True` | yes | ON by default : a coding run should finish the job instead of stopping at the round limit. Turn off for a quick answer. |
| `vision_agent_enabled` | bool | `False` | yes |  |
| `vision_agent_mode` | str | `sequential` | yes |  |
| `vision_agent_model` | str | `` | yes | Vision agent model ("" = auto-pick smallest capable installed model). |
| `vision_mmproj_always` | bool | `False` | yes | MMPROJ ON TEXT-ONLY LOADS (default False = skip): the vision projector (--mmproj, ~0.8-1.3 GB VRAM) is attached only when the request actually carries images; a text-only load of a vision-capable model starts without it, and the first image request pays a one-time upgrade reload… |
| `vision_preprocess_load_timeout_seconds` | int | `120` | yes |  |
| `vision_preprocess_timeout_seconds` | int | `30` | yes |  |
| `vram_budget_gb` | float | `7.5` | yes |  |
| `vram_preflight_gate` | bool | `True` | yes | VRAM pre-flight gate : when True (default), loads estimated not to fit are refused before llama-server starts (estimate can be wrong for foreign models - turning this OFF makes the engine attempt the load and halve ctx on real failures instead). |
| `vram_preflight_margin_mib` | int | `768` | yes | Safety headroom in MiB reserved by the VRAM pre-flight check. |
| `vram_refresh_interval_s` | int | `8` | yes | UI-side only: VRAM panel refresh interval (static/app.js). The engine never reads it; listed so the postSettings key is discoverable. |
| `warn_on_cpu_offload` | bool | `True` | yes |  |
| `websearch_auto_trigger` | bool | `True` | yes |  |
| `workspace` | str | `` | yes |  |
| `workspace_force_ui` | bool | `True` | yes |  |
