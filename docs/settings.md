# HiveMind Settings

Auto-generated from `settings.py` (`DEFAULT_SETTINGS`) via `python deploy/gen_settings_docs.py` — do not edit by hand.

204 settings keys, based on DEFAULT_SETTINGS.

| Key | Type | Default | Note |
|---|---|---|---|
| `_thinking_before_chunking` | null | `null` | persisted user-preference before chunking forced thinking ON |
| `active_preset` | null | `null` | — |
| `agents` | str | `"<ref DEFAULT_AGENT_CFG>"` | — |
| `allow_cpu_offload` | bool | `True` | — |
| `ask_user_auto_answer` | str | `"Use best judgment, document decision in commit message."` | — |
| `ask_user_max_per_10min` | int | `5` | — |
| `ask_user_throttle_pause_message` | str | `Agent is asking too many questions — manual help required...` | — |
| `ask_user_timeout_until_finished_seconds` | int | `300` | — |
| `automap_code_duo_enabled` | bool | `False` | — |
| `automap_duo_parallel_preexplore` | bool | `False` | — |
| `automap_duo_pre_explore` | bool | `False` | — |
| `automap_excluded` | list | `[]` | — |
| `automap_pipeline_websearch_enabled` | bool | `False` | — |
| `constraint_mode` | bool | `True` | — |
| `ctx_overrides` | object | `{'default': None, 'roles': {}, 'models': {}}` | — |
| `debug_dump_request` | bool | `False` | — | writes every model request as `logs/telemetry/dump_<role>_<chatid>_<ts>.json` |
| `default_keep_alive` | str | `"10m"` | — |
| `desktop_notifications` | bool | `True` | — |
| `direct_tools_enabled` | bool | `True` | — |
| `direct_tools_max_rounds` | int | `12` | — |
| `direct_tools_tier` | str | `"readonly"` | off | readonly(websearch only) | python(read+python) | full |
| `disable_thinking_in_planner` | bool | `False` | — |
| `duo_action_approval_enabled` | bool | `False` | — |
| `duo_agentic_mode` | bool | `False` | — |
| `duo_agentic_thinking` | bool | `False` | — |
| `duo_autolint_python_engine` | str | `"auto"` | — |
| `duo_caps` | object | `{}` | — |
| `duo_chunking` | bool | `True` | — |
| `duo_coder_ctx_agentic` | null | `null` | — |
| `duo_coder_ctx_normal` | null | `null` | — |
| `duo_coder_explore_chars` | int | `0` | — |
| `duo_coder_fallback_model` | str | `""` | — |
| `duo_coder_model` | str | `""` | — |
| `duo_coder_tool_thinking` | bool | `False` | — |
| `duo_coder_tool_thinking_auto_mode` | str | `"off"` | — |
| `duo_coder_ttl_seconds` | int | `0` | 0 = auto (420s), >0 = Override |
| `duo_coding_mode` | bool | `True` | — |
| `duo_compress_auto_floor` | float | `0.7` | — |
| `duo_compress_llm_timeout_s` | int | `180` | — |
| `duo_compress_local_only` | bool | `False` | — |
| `duo_compress_model` | str | `"lfm2.5:2.6b"` | — |
| `duo_compress_overflow_reserve` | int | `1024` | — |
| `duo_compress_threshold` | int | `0` | — |
| `duo_critic_ctx` | null | `null` | — |
| `duo_critic_model` | str | `""` | — |
| `duo_critic_tools` | bool | `False` | — |
| `duo_distilled_executor` | bool | `False` | — |
| `duo_error_rollup` | bool | `True` | — |
| `duo_evict_planner_on_coder` | bool | `True` | — |
| `duo_git_autocommit` | bool | `False` | — |
| `duo_git_checkpoints` | bool | `False` | - | Commit the run's files at chunk start. Writes to your repository, so opt-in. |
| `duo_install_max_calls` | int | `3` | — |
| `duo_llm_slow_timeout_s` | int | `300` | — |
| `duo_max_compressions` | int | `40` | — |
| `duo_max_tool_errors` | int | `12` | — |
| `duo_max_tool_rounds` | int | `64` | — |
| `duo_max_tool_rounds_runtime_cap` | int | `300` | — |
| `duo_noop_hint_enabled` | bool | `True` | — |
| `duo_p3_max_fix_attempts` | int | `3` | — |
| `duo_pair` | str | `"focused"` | — |
| `duo_parallel_preexplore` | bool | `False` | — |
| `duo_partial_compression` | bool | `True` | — |
| `duo_partition_max_files` | int | `30` | — |
| `duo_pass_explore_files` | str | `"touched"` | — |
| `duo_peer_ratings_agentic` | bool | `False` | — |
| `duo_pin_static_map` | bool | `True` | — |
| `duo_planner_ctx_cap` | null | `null` | — |
| `duo_planner_ctx_target` | null | `null` | — |
| `duo_planner_default_thinking` | bool | `True` | — |
| `duo_planner_ensure_load_timeout_s` | int | `300` | — |
| `duo_planner_max_steps` | int | `0` | — |
| `duo_planner_max_tokens` | int | `8000` | — |
| `duo_planner_model` | null | `null` | — |
| `duo_planner_thinking_budget` | int | `8000` | — |
| `duo_planner_thinking_timeout_s` | int | `600` | — |
| `duo_planner_ttl_seconds` | int | `0` | 0 = auto (450s), >0 = Override |
| `duo_planner_use_coder_ctx` | bool | `True` | — |
| `duo_planner_use_exec_model` | bool | `True` | — |
| `duo_pre_explore` | bool | `False` | — |
| `duo_pre_explore_ctx` | int | `4096` | — |
| `duo_pre_explore_ctx_char_ratio` | float | `3.0` | — |
| `duo_pre_explore_llm_timeout_s` | int | `600` | — |
| `duo_pre_explore_max_files_est` | int | `15` | — |
| `duo_pre_explore_max_tools` | int | `20` | — |
| `duo_pre_explore_timeout_per_file_s` | int | `20` | — |
| `duo_pre_explore_timeout_seconds` | int | `600` | — |
| `duo_pre_explore_tokens` | int | `700` | — |
| `duo_profile_quality_model` | str | `"lfm2.5:2.6b"` | — |
| `duo_profile_speed_model` | str | `""` | — |
| `duo_pyright_path` | str | `""` | — |
| `duo_read_timeout` | int | `390` | — |
| `duo_repo_memory_enabled` | bool | `True` | — |
| `duo_repo_memory_min_score` | float | `0.12` | — |
| `duo_repo_memory_top_k` | int | `2` | — |
| `duo_rounds_balanced_cap` | int | `3` | — |
| `duo_run_bash_build_timeout_s` | int | `600` | — |
| `duo_run_timeout_critical_seconds` | int | `900` | — |
| `duo_run_timeout_seconds` | int | `420` | — |
| `duo_runtime_profile` | str | `"balanced"` | — |
| `duo_soft_planner_wall_timeout_s` | int | `300` | — |
| `duo_static_map_chars` | int | `0` | 0 = Tier-abgeleitet (rich: 8000); >0 = explizites Static-Repo-Map-Char-Budget |
| `duo_symbol_ref_enabled` | bool | `True` | — |
| `duo_symbol_ref_max_items` | int | `120` | — |
| `duo_symbol_ref_top_k` | int | `2` | — |
| `duo_test_feedback` | bool | `False` | — |
| `duo_test_feedback_chunk` | bool | `False` | — |
| `duo_test_feedback_final` | bool | `True` | — |
| `duo_thinking_per_chunk` | bool | `False` | — |
| `duo_tool_autopromote_max_rounds` | int | `4` | — |
| `duo_tool_output_ttl` | int | `3` | — |
| `duo_tool_rounds` | int | `0` | — |
| `duo_tool_sandbox` | bool | `True` | — |
| `duo_tool_sandbox_max_mem_mb` | int | `4096` | — |
| `duo_tool_sandbox_max_procs` | int | `64` | — |
| `duo_tool_sandbox_max_cpu_s` | int | `0` | — |

> Sandbox limits apply per tool child. On Windows they are job objects
> (`KILL_ON_JOB_CLOSE`); on Linux/macOS they are `RLIMIT_AS` / `RLIMIT_NPROC` /
> `RLIMIT_CPU`, applied only when the value is above 0. `duo_tool_sandbox` itself
> is Windows-only - the Linux service relies on its systemd `MemoryMax`/`TasksMax`
> plus the optional rlimits (details: `tools/sandbox.py`, `_posix_preexec`).

| `duo_tree_scout_enabled` | bool | `True` | — |
| `duo_tree_scout_max_depth` | int | `4` | — |
| `duo_tree_scout_max_files` | int | `200` | — |
| `duo_until_finished_cap` | int | `999999` | — |
| `duo_use_pipeline` | bool | `False` | — |
| `duo_websearch_enabled` | bool | `False` | — |
| `duo_websearch_max_calls` | int | `20` | — |
| `duo_websearch_timeout_critical_seconds` | int | `24` | — |
| `duo_websearch_timeout_fast_seconds` | int | `13` | — |
| `duo_websearch_timeout_seconds` | int | `20` | — |
| `duo_wedge_handoff_enabled` | bool | `False` | **Beta, off by default.** When the coder fails the same edit on one file 4x in a row, a fresh-context repair agent takes over that one edit (same model, clean window, max `duo_wedge_handoff_max_per_run` per run). |
| `duo_wedge_handoff_max_per_run` | int | `2` | — |
| `duo_wedge_handoff_max_rounds` | int | `10` | — |
| `duo_wedge_handoff_max_tokens` | int | `1200` | output budget of one fix-agent round |
| `duo_wedge_handoff_model` | str | `""` | empty = coder model (fresh context, same weights) |
| `duo_wedge_handoff_streak_threshold` | int | `4` | — |
| `duo_wedge_handoff_timeout_s` | int | `900` | — |
| `duo_worker_slots` | int | `2` | — |
| `duo_write_chars_per_token` | float | `2.5` | — |
| `duo_write_guard_enabled` | bool | `True` | — |
| `exploration_agent` | object | `{'enabled': True, 'model': '', 'workers': [{...` | — |
| `git_auto_push` | bool | `False` | — |
| `git_commit_prefix` | str | `"hivemind:"` | — |
| `git_default_branch` | str | `"main"` | — |
| `git_email` | str | `""` | — |
| `git_repo_url` | str | `""` | — |
| `git_token` | str | `""` | — |
| `git_tools_enabled` | str | `"auto"` | git_status/git_commit in the tool schema: auto = one of git_repo_url/git_username/git_email/git_token is set, true = always, false = never |
| `git_username` | str | `""` | — |
| `gpu_backend` | str | `""` | — |
| `image_desc_full_pipeline` | bool | `False` | — |
| `intent_agent` | object | `{'enabled': False, 'model': 'qwen3.5:4b', 'temperature': ...` | — |
| `judge_keepalive_enabled` | bool | `False` | — |
| `judge_prefetch_before_complexity` | bool | `False` | - | Warm the judge model before the routing check. Latency win on a big card, an eviction risk on a small one. |
| `keep_awake_during_run` | bool | `True` | — |
| `learning_preset_mode` | bool | `False` | — |
| `llama_cache_reuse` | int | `256` | — |
| `llama_mlock` | bool | `True` | — |
| `llama_ubatch_size` | int | `256` | — |
| `max_concurrent_models` | null | `null` | — |
| `max_iterations` | int | `2` | — |
| `max_model_size_gb` | null | `null` | — |
| `mcp_servers` | list | `[]` | — |
| `mode` | str | `"simple"` | — |
| `model_capability_overrides` | object | `{}` | — |
| `models_dir` | str | `""` | — |
| `moe_cpu_experts` | object | `{}` | — |
| `pin_direct_after_response` | bool | `False` | — |
| `pipeline_vision_direct` | bool | `False` | — |
| `pipeline_vision_roles` | object | `{}` | — |
| `pipeline_websearch_enabled` | bool | `True` | — |
| `plan_tracker_classifier` | str | `"heuristic"` | — |
| `prefer_smaller_models` | bool | `False` | — |
| `prefetch_agent_avgs` | object | `{}` | — |
| `prefetch_lead_seconds` | float | `8.0` | — |
| `preload_workers_after_run` | bool | `False` | — |
| `read_guard_enabled` | bool | `True` | — |
| `safe_profile_matrix_file` | str | `"model_configs/safe_profile_matrix.json"` | — |
| `safe_profile_policy` | str | `"default_8gb_v1"` | — |
| `searxng_engines` | str | `""` | — | Empty = SearXNG queries every engine it has enabled. Pinning a subset (the old `"bing,duckduckgo"`) hid wikipedia and made English queries return German shop pages. |
| `searxng_host` | str | `"http://localhost:8888"` | — |
| `searxng_language` | str | `"en"` | — | Language tag sent to the engines. `"all"` let them fall back to the instance region (German pages for English queries). |
| `server_port` | int | `8001` | — |
| `session_compress_threshold` | int | `20` | — |
| `smart_preload_enabled` | bool | `False` | - | Prefetch: load the next agent's model while the current one works, and keep it warm (`smart_preload_keep_alive`). Off by default - on a tight VRAM budget the prefetched model can evict the one that is answering. |
| `smart_preload_keep_alive` | str | `"10m"` | — |
| `soul_evolve_agent` | object | `{'enabled': False, 'model': 'gemma-4:e4b-it-obliterated',...` | — |
| `soul_skill_distillation` | bool | `True` | — |
| `soul_skill_writing` | bool | `False` | — |
| `startup_preload_analyst` | bool | `False` | — |
| `startup_preload_coder` | bool | `False` | — |
| `startup_preload_enabled` | bool | `False` | — |
| `startup_preload_judge_in_agentic` | bool | `False` | — |
| `subagent_lite_cooldown_s` | int | `60` | — |
| `subagent_lite_ctx_default` | int | `8192` | — |
| `subagent_lite_enabled` | bool | `True` | — |
| `subagent_lite_max_tokens` | int | `700` | — |
| `subagent_lite_max_tools` | int | `12` | — |
| `subagent_lite_min_free_ram_gb` | float | `5.0` | — |
| `subagent_lite_model_ladder` | list | `['lfm2.5:2.6b', 'qwen3.5:0.8b-ud']` | — |
| `subagent_lite_safety_margin_mib` | int | `256` | — |
| `subagent_lite_timeout_s` | int | `120` | — |
| `until_finished` | bool | `False` | — |
| `vision_agent_enabled` | bool | `False` | — |
| `vision_agent_mode` | str | `"sequential"` | — |
| `vision_agent_model` | str | `""` | — |
| `vision_preprocess_load_timeout_seconds` | int | `120` | — |
| `vision_preprocess_timeout_seconds` | int | `30` | — |
| `vram_budget_gb` | float | `7.5` | — |
| `warn_on_cpu_offload` | bool | `False` | — |
| `websearch_auto_trigger` | bool | `True` | — |
| `workspace` | str | `""` | — |
| `workspace_force_ui` | bool | `True` | — |
| `duo_coder_briefing_think_tokens` | int | `1500` | Duo card | Planner thinking is injected into the coder as a briefing only up to this many estimated tokens; above it the coder gets the pure plan (a planner that thinks 8-16k tokens would poison it). `0` = plan only, `-1` = always inject. |
| `tts_enabled` | bool | `False` | Voice card | Text-to-speech master switch. Starts the Pocket TTS sidecar (isolated uv environment) on the first request; off = no process, no speaker buttons. |
| `tts_voice` | str | `"anna"` | Voice card | One of the shipped English voices (`anna` (default, CC BY 4.0), `alba`, `cosette` (CC BY-NC 4.0, non-commercial), `eve`, `jane`, `vera`, `bill_boerst`). |
| `tts_speed` | float | `1.08` | Voice card | Playback speed 0.8-1.5. Pocket TTS has no speed parameter, so HiveMind time-stretches the audio (WSOLA, pitch preserved). |
| `tts_timeout_s` | int | `120` | - | Hard limit per synthesis request. |
| `tts_idle_stop_s` | int | `900` | - | The sidecar stops itself after this much idle time (it holds torch + the model in RAM while warm). |
| `tts_preload_run` | bool | `True` | Voice card | Warm the TTS engine when a run starts instead of at the first sentence - removes the ~12 s cold start for phone runs. |
| `tts_volume` | float | `1.0` | Voice card | Browser playback level 0-1 for read-aloud, the speaker button and the sample. |
| `tts_normalize` | bool | `True` | - | Level the sidecar output (it comes back at about -15.5 dBFS peak) to -1.5 dBFS so the volume slider has range. |
| `stt_enabled` | bool | `False` | Voice card | Speech-to-text master switch (English only in 1.4). Needs the whisper.cpp binaries: `python deploy/fetch_whisper.py`. |
| `stt_model` | str | `""` | Voice card | Model tier. `""` = RAM default (parakeet q4_k on >= 8 GB, else whisper base). Keys: `parakeet-q4k`, `base`, `tiny`, `small`. |
| `stt_language` | str | `"en"` | Voice card | Only **English is supported** in 1.4; another two-letter code is passed through as *experimental* (Whisper can transcribe other languages), anything that is not a code degrades to `en`. |
| `stt_translate` | bool | `False` | — | Whisper `--translate` into English (English target only; needs a whisper tier, parakeet cannot translate). Off by default: translation quality has to be evaluated before it is offered. |
| `stt_threads` | int | `0` | Voice card | Worker threads for the CLI. `0` = `min(4, cpu_count//2)`. |
| `stt_preload` | bool | `False` | Voice card | Keep a warm `whisper-server` for whisper models (parakeet has no server and loads in well under a second). Falls back to the CLI when the warm path fails. |
| `stt_max_seconds` | int | `300` | Voice card | Longest accepted audio; longer voice notes are refused with a clear message. The decoder also caps the byte size (20 MB, the Telegram download limit). |
| `stt_timeout_s` | int | `120` | — | Hard per-request timeout; the child process is killed. |
| `stt_log_transcripts` | bool | `False` | — | Write the transcript into `logs/`. Off by default: transcripts are content, not diagnostics. |

## Audit disclosure

The following keys were previously only implicit read fallbacks ("hidden") and are now explicit in `DEFAULT_SETTINGS`:

`duo_autolint_python_engine, duo_caps, duo_coder_model, duo_critic_model, duo_pyright_path, plan_tracker_classifier, read_guard_enabled`

| `duo_coder_after_run` | `keep` | Coder slot lifetime after a run: `keep` (stays warm, the TTL decides) or `evict` (released at run end, also on abort). |

### Voice, images and the remaining knobs

These were missing from the table above; defaults are from `settings.py`.

| Setting | Type | Default | Card | Description |
|---|---|---|---|---|
| `tts_read_direct` | bool | `False` | Voice | Read direct-mode answers aloud. |
| `tts_read_agentic` | bool | `False` | Voice | Read agentic answers (the fazit) aloud. |
| `tts_read_ask` | bool | `False` | Voice | Read `ask_user` questions aloud. |
| `tts_read_gate` | bool | `False` | Voice | Read approval cards aloud. |
| `telegram_voice_in` | bool | `False` | Voice / gateway | Accept voice notes from the phone (transcribed, audio discarded). `voice_in` in `gateway.toml` is the fallback. |
| `telegram_voice_out` | bool | `False` | Voice / gateway | Send the final answer back as a voice note. |
| `telegram_result_mode` | str | `"both"` | Telegram | `both`, `text` or `voice` (results-only line; errors stay text). |
| `telegram_progress_msgs` | bool | `True` | Telegram | OFF silences status + tool lines; approvals and the result still arrive. |
| `debug_api_enabled` | bool | `False` | - | Mount the debug endpoints (`/debug/tasks`, `/debug/models`, `/thinking/debug`, `/budget/debug`). Off by default - they expose task stacks and registry paths. |
| `run_bash_shell` | str | `"auto"` | - | Shell used by `run_bash` (`auto` picks pwsh/cmd or bash per platform). |
| `duo_action_approval_timeout_s` | int | `0` | - | Auto-deny a pending approval card after this many seconds (`0` = wait forever). |
| `vram_preflight_gate` | bool | `True` | - | Refuse a model load that the VRAM estimate says will not fit. |
| `duo_mask_before_compress` | bool | `True` | - | Mask tool payloads before context compression (keeps the shape, drops the bulk). |
| `session_history_budget_tokens` | int | `0` | - | Token budget for seeding chat history into a run (`0` = derive from ctx). |
| `duo_image_mode` | str\|null | `None` | - | Image preprocessing mode for the pipeline (`None` = sequential default). |
| `duo_image_to_planner` | bool | `False` | - | Hand attached images to the planner as well. |
| `duo_image_to_coder` | bool | `False` | - | Hand attached images to the coder as well. |
| `duo_coder_images_off` | bool | `False` | - | Never send images to the coder (text-only coder models). |
| `image_uploads_persistent` | bool | `False` | - | Keep uploaded images on disk instead of memory (`sessions/`). |
