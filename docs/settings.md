# HiveMind Settings

Auto-generiert aus `settings.py` (`DEFAULT_SETTINGS`) per `python deploy/gen_settings_docs.py` — nicht von Hand editieren.

189 Settings-Keys, Stand: DEFAULT_SETTINGS.

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
| `automap_mode` | str | `"conservative"` | — |
| `automap_pipeline_websearch_enabled` | bool | `False` | — |
| `constraint_mode` | bool | `True` | — |
| `ctx_overrides` | object | `{'default': None, 'roles': {}, 'models': {}}` | — |
| `default_keep_alive` | str | `"10m"` | — |
| `desktop_notifications` | bool | `True` | — |
| `direct_tools_enabled` | bool | `True` | — |
| `direct_tools_max_rounds` | int | `12` | — |
| `direct_tools_tier` | str | `"readonly"` | readonly | python | full | off |
| `disable_thinking_in_planner` | bool | `False` | — |
| `duo_agentic_mode` | bool | `False` | — |
| `duo_agentic_thinking` | bool | `False` | — |
| `duo_autolint_python_engine` | str | `"auto"` | — |
| `duo_caps` | object | `{}` | — |
| `duo_chunking` | bool | `True` | — |
| `duo_coder_ctx_agentic` | null | `null` | — |
| `duo_coder_ctx_normal` | null | `null` | — |
| `duo_coder_ctx_until_finished` | null | `null` | — |
| `duo_coder_explore_chars` | int | `0` | — |
| `duo_coder_fallback_model` | str | `"qwen3.5:4b-ud"` | — |
| `duo_coder_model` | str | `""` | — |
| `duo_coder_tool_thinking` | bool | `False` | — |
| `duo_coder_tool_thinking_auto_mode` | str | `"on_fail"` | — |
| `duo_coder_ttl_seconds` | int | `0` | 0 = auto (420s), >0 = Override |
| `duo_coding_mode` | bool | `True` | — |
| `duo_compress_every` | int | `4` | — |
| `duo_compress_threshold` | int | `0` | — |
| `duo_critic_ctx` | null | `null` | — |
| `duo_critic_model` | str | `""` | — |
| `duo_critic_tools` | bool | `False` | — |
| `duo_distilled_executor` | bool | `False` | — |
| `duo_git_autocommit` | bool | `False` | — |
| `duo_git_checkpoints` | bool | `True` | — |
| `duo_install_max_calls` | int | `3` | — |
| `duo_llm_slow_timeout_s` | int | `300` | — |
| `duo_max_tool_rounds` | int | `64` | — |
| `duo_max_tool_rounds_runtime_cap` | int | `300` | — |
| `duo_p3_max_fix_attempts` | int | `3` | — |
| `duo_pair` | str | `"focused"` | — |
| `duo_parallel_preexplore` | bool | `False` | — |
| `duo_partition_max_files` | int | `30` | — |
| `duo_pass_explore_files` | str | `"touched"` | — |
| `duo_peer_ratings_agentic` | bool | `False` | — |
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
| `duo_profile_quality_model` | str | `"qwen3.5:9b-ud"` | — |
| `duo_profile_speed_model` | str | `"qwen3.5:4b-ud"` | — |
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
| `duo_runtime_profile_lock_override` | bool | `False` | — |
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
| `duo_tree_scout_enabled` | bool | `True` | — |
| `duo_tree_scout_max_depth` | int | `4` | — |
| `duo_tree_scout_max_files` | int | `200` | — |
| `duo_until_finished_cap` | int | `999999` | — |
| `duo_use_pipeline` | bool | `False` | — |
| `duo_use_preset_models` | bool | `False` | — |
| `duo_use_presets` | bool | `True` | — |
| `duo_websearch_enabled` | bool | `False` | — |
| `duo_websearch_max_calls` | int | `20` | — |
| `duo_websearch_timeout_critical_seconds` | int | `24` | — |
| `duo_websearch_timeout_fast_seconds` | int | `13` | — |
| `duo_websearch_timeout_seconds` | int | `20` | — |
| `duo_worker_slots` | int | `2` | — |
| `duo_write_chars_per_token` | float | `2.5` | — |
| `exploration_agent` | object | `{'enabled': True, 'model': 'qwen3.5:4b-ud', 'workers': [{...` | — |
| `git_auto_push` | bool | `False` | — |
| `git_commit_prefix` | str | `"hivemind:"` | — |
| `git_default_branch` | str | `"main"` | — |
| `git_email` | str | `""` | — |
| `git_repo_url` | str | `""` | — |
| `git_token` | str | `""` | — |
| `git_username` | str | `""` | — |
| `gpu_backend` | str | `""` | — |
| `image_desc_full_pipeline` | bool | `False` | — |
| `intent_agent` | object | `{'enabled': False, 'model': 'qwen3.5:4b', 'temperature': ...` | — |
| `judge_keepalive_enabled` | bool | `True` | — |
| `judge_prefetch_before_complexity` | bool | `True` | — |
| `keep_awake_during_run` | bool | `True` | — |
| `learning_preset_mode` | bool | `False` | — |
| `llama_cache_reuse` | int | `256` | — |
| `llama_mlock` | bool | `True` | — |
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
| `searxng_engines` | str | `"brave,wikipedia,github"` | — |
| `searxng_host` | str | `"http://localhost:8888"` | — |
| `searxng_language` | str | `"all"` | — |
| `server_port` | int | `8001` | — |
| `session_compress_threshold` | int | `20` | — |
| `smart_preload_enabled` | bool | `True` | — |
| `smart_preload_keep_alive` | str | `"10m"` | — |
| `soul_evolve_agent` | object | `{'enabled': False, 'model': 'gemma-4:e4b-it-obliterated',...` | — |
| `soul_skill_distillation` | bool | `True` | — |
| `soul_skill_writing` | bool | `False` | — |
| `startup_preload_analyst` | bool | `False` | — |
| `startup_preload_coder` | bool | `False` | — |
| `startup_preload_enabled` | bool | `True` | — |
| `startup_preload_judge_in_agentic` | bool | `True` | — |
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

## Audit disclosure

The following keys were previously only implicit read fallbacks ("hidden") and are now explicit in `DEFAULT_SETTINGS`:

`duo_autolint_python_engine, duo_caps, duo_coder_model, duo_critic_model, duo_pyright_path, plan_tracker_classifier, read_guard_enabled`
