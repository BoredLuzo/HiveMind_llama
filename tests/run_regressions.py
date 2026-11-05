"""Master regression runner.

Runs all standalone regression suites (tests/test_*.py) in one go and forces
exit 1 on any failure - so fixed behaviors do not silently regress. Each suite
is a standalone script with `sys.exit(0|1)`. If a suite file is missing, it
counts as FAIL.

Run:
    python tests/run_regressions.py            # all suites
    python tests/run_regressions.py --only stuck_bash   # name filter (substring)
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent

# (name, relPath, timeout seconds) - only actually present tests.
SUITES = [
    ("schema_truth",              "tests/test_schema_truth.py",                  60),
    ("budgets",                   "tests/test_budgets.py",                       60),
    ("prefix_stability",          "tests/test_prefix_stability.py",              60),
    ("chunk_resume",              "tests/test_chunk_resume.py",                  60),
    ("testmap",                   "tests/test_testmap.py",                       60),
    ("audit_b",                   "tests/test_audit_b.py",                       60),
    ("ux_quickwins",              "tests/test_ux_quickwins.py",                  60),
    ("duo_e2e_smoke",             "tests/test_duo_e2e_smoke.py",                120),
    ("run_stats",                 "tests/test_run_stats.py",                     60),
    ("preset_autoload_guard",     "tests/test_preset_autoload_guard.py",         60),
    ("debug_dump",                "tests/test_debug_dump.py",                    60),
    ("steer_card",                "tests/test_steer_card.py",                    60),
    ("planner_reuse_ctx",         "tests/test_planner_reuse_ctx.py",             60),
    ("takeover_polish",           "tests/test_takeover_polish.py",               60),
    ("reaudit_polish",            "tests/test_reaudit_polish.py",                60),
    ("compression_plan_anchor",   "tests/test_compression_plan_anchor.py",     120),
    ("feature_search_core",       "tests/test_feature_search_core.py",          60),
    ("transcript_glob",           "tests/test_transcript_glob.py",              60),

    ("compress_plan_reinject",    "tests/test_compress_plan_reinject.py",      120),
    ("destructive_gate",          "tests/test_destructive_gate.py",            120),
    ("direct_tools",              "tests/test_direct_tools.py",                120),
    ("ensure_loaded_stale_port",  "tests/test_ensure_loaded_stale_port.py",     60),
    ("vision_load_race",          "tests/test_vision_load_race.py",              60),
    ("direct_vision_flag",        "tests/test_direct_vision_flag.py",            60),
    ("loop_detect_calibration",   "tests/test_loop_detect_calibration.py",     120),
    ("loop_detect_chunk_read",    "tests/test_loop_detect_chunk_read.py",      120),
    ("loop_detect_run_bash",      "tests/test_loop_detect_run_bash.py",        120),
    ("models_registry",           "tests/test_models_registry.py",              60),
    ("safe_profile_choice",       "tests/test_safe_profile_choice.py",          60),
    ("linux_paths",               "tests/test_linux_paths.py",                  60),
    ("planner_coder_port_verify", "tests/test_planner_coder_port_verify.py",    60),
    ("tool_error_taxonomy",       "tests/test_tool_error_taxonomy.py",          60),
    ("tool_arg_schemas",          "tests/test_tool_arg_schemas.py",             60),
    ("run_audit",                 "tests/test_run_audit.py",                    60),
    ("workspace_guards",          "tests/test_workspace_guards.py",             60),
    ("workspace_provenance",      "tests/test_workspace_provenance.py",         60),
    ("fuzzy_autojunk",            "tests/test_fuzzy_autojunk.py",               60),
    ("chats_ws_guard",            "tests/test_chats_ws_guard.py",               60),
    ("read_after_write",          "tests/test_read_after_write.py",             60),
    ("run_bash_shell_hint",       "tests/test_run_bash_shell_hint.py",          60),
    ("update_zip_layout",         "tests/test_update_zip_layout.py",           120),
    ("restored_caps_truth",       "tests/test_restored_caps_truth.py",          60),
    ("mtp_off_switch",            "tests/test_mtp_off_switch.py",               90),
    ("read_ladder",               "tests/test_read_ladder.py",                  60),
    ("git_tool_gating",           "tests/test_git_tool_gating.py",              60),
    ("cpt_probe",                 "tests/test_cpt_probe.py",                    60),
    ("skip_trap",                 "tests/test_skip_trap.py",                    60),
    ("tool_feedback",             "tests/test_tool_feedback.py",                60),
    ("schema_fit_paths",          "tests/test_schema_fit_paths.py",             60),
    ("wedge_ctx",                 "tests/test_wedge_ctx.py",                    60),
    ("wedge_ui_toggle",           "tests/test_wedge_ui_toggle.py",              60),
    ("duo_split",                 "tests/test_duo_split.py",                    60),
    ("tool_descriptions",         "tests/test_tool_descriptions.py",            60),
    ("live_findings",             "tests/test_live_findings.py",                60),
    ("content_budget_tokens",     "tests/test_content_budget_tokens.py",        60),
    ("preflight_evict_guard",      "tests/test_preflight_evict_guard.py",      60),
    ("coder_after_run",           "tests/test_coder_after_run.py",           60),
    ("inline_think_format",        "tests/test_inline_think_format.py",        60),
    ("installer_uv_path",         "tests/test_installer_uv_path.py",         60),
    ("run_bash_drive_paths",       "tests/test_run_bash_drive_paths.py",      60),
    ("test_detection_fallback",   "tests/test_test_detection_fallback.py",   60),
    ("install_package_errors",    "tests/test_install_package_errors.py",    60),
    ("harness_stats",             "tests/test_harness_stats.py",             60),
    ("readme_currency",           "tests/test_readme_currency.py",           60),
    ("searxng_bat",                "tests/test_searxng_bat.py",               90),
    ("workspace_cpu_guards",       "tests/test_workspace_and_cpu_guards.py",   30),
    ("stt_engine",               "tests/test_stt_engine.py",                 60),
    ("stt_audio",                "tests/test_stt_audio.py",                  90),
    ("stt_server",               "tests/test_stt_server.py",                 60),
    ("voice_card",               "tests/test_voice_card.py",                 45),
    ("websearch_quality",         "tests/test_websearch_quality.py",          60),
    ("coder_briefing",            "tests/test_coder_briefing.py",             30),
    ("tts_audio",                 "tests/test_tts_audio.py",                  60),
    ("tts_engine",                "tests/test_tts_engine.py",                 90),
    ("tts_server",                "tests/test_tts_server.py",                 45),
    ("gateway_voice",             "tests/test_gateway_voice.py",              45),
    ("gateway_voice_out",         "tests/test_gateway_voice_out.py",          30),
    ("tool_registry_consistency",  "tests/test_tool_registry_consistency.py",  30),
    ("injection_budget",          "tests/test_injection_budget.py",             60),
    ("vision_strict",             "tests/test_vision_strict.py",                60),
    ("env_check",                 "tests/test_env_check.py",                   120),
    ("update_prune",              "tests/test_update_prune.py",                120),
    ("update_flow_audit",         "tests/test_update_flow_audit.py",           180),
    ("bind_guard",                "tests/test_bind_guard.py",                   60),
    ("decider_cache",             "tests/test_decider_cache.py",                60),
    ("decider_unload",            "tests/test_decider_unload.py",               60),
    ("tool_budgets",              "tests/test_tool_budgets.py",                 60),
    ("ctx_budget",                "tests/test_ctx_budget.py",                   60),
    ("dispatch_smoke",            "tests/test_dispatch_smoke.py",               60),
    ("destructive_python",        "tests/test_destructive_python.py",           60),
    ("execute_tool_round",        "tests/test_execute_tool_round.py",           60),
    ("ctx_lru_invalidation",      "tests/test_ctx_lru_invalidation.py",          60),
    ("planner_connect_recovery",  "tests/test_planner_connect_recovery.py",    120),
    ("planner_thinking_cap",      "tests/test_planner_thinking_cap.py",        120),
    ("stuck_bash_reset_threshold", "tests/test_stuck_bash_reset_threshold.py", 120),
    ("stuck_edit_false_positive", "tests/test_stuck_edit_false_positive.py",   120),
    ("error_rollup",              "tests/test_error_rollup.py",                  60),
    ("repomap_pin",               "tests/test_repomap_pin.py",                   60),
    ("toolcall_sanitize",         "tests/test_toolcall_sanitize.py",             60),
    ("ctx_fit_and_floor_guard",   "tests/test_ctx_fit_and_floor_guard.py",       60),
    ("write_guard_and_explore_truth", "tests/test_write_guard_and_explore_truth.py", 60),
    ("noop_hint_and_zero_lines",  "tests/test_noop_hint_and_zero_lines.py",       60),
    ("fallback_ctx_stall",     "tests/test_fallback_ctx_stall.py",             60),
    ("mismatch_reload",        "tests/test_mismatch_reload.py",                60),
    ("runstate_persistence",   "tests/test_runstate_persistence.py",           60),
    ("gguf_tag_pinning",       "tests/test_gguf_tag_pinning.py",               60),
    ("mmproj_specs",           "tests/test_mmproj_specs.py",                    30),
    ("git_gate",               "tests/test_git_gate.py",                        60),
    ("chat_blobs",             "tests/test_chat_blobs.py",                      60),
    ("chat_real_roundtrip",    "tests/test_chat_real_roundtrip.py",             60),
    ("history_source",         "tests/test_history_source.py",                  60),
    ("image_plan",             "tests/test_image_plan.py",                      60),
    ("image_context",          "tests/test_image_context.py",                   60),
    ("approval_timeout",       "tests/test_approval_timeout.py",                 60),
    ("chat_concurrency",       "tests/test_chat_concurrency.py",                 90),
    ("bat_encoding",           "tests/test_bat_encoding.py",                     30),
    ("native_lib_check",       "tests/test_native_lib_check.py",               60),
    ("websearch_budget_stop",  "tests/test_websearch_budget_stop.py",          60),
    ("toolgen_stream_card",    "tests/test_toolgen_stream_card.py",            60),
    ("error_cap_decay",        "tests/test_error_cap_decay.py",                60),
    ("action_approval",        "tests/test_action_approval.py",                60),
    ("run_python_encoding",    "tests/test_run_python_encoding.py",            60),
    ("browser_blocked_dedup",  "tests/test_browser_blocked_dedup.py",          60),
    ("appjs_error_net",        "tests/test_appjs_error_net.py",                60),
    ("edit_tool_layer",        "tests/test_edit_tool_layer.py",                60),
    ("wedge_handoff",          "tests/test_wedge_handoff.py",                 120),
    ("tools_audit_fixes",      "tests/test_tools_audit_fixes.py",             120),
    ("mini_shrink_retry",      "tests/test_mini_shrink_retry.py",              60),
    ("output_reserve",         "tests/test_output_reserve.py",                  60),
    ("ctx_guard",              "tests/test_ctx_guard.py",                       60),
    ("planner_model_persist",  "tests/test_planner_model_persist.py",           60),
    ("read_only_detect",       "tests/test_read_only_detect.py",                60),
    ("tool_arg_compact",       "tests/test_tool_arg_compact.py",                60),
    ("lint",                   "tests/test_lint.py",                            60),
    ("no_new_silent_excepts",  "tests/test_no_new_silent_excepts.py",           60),
    ("no_personal_context",    "tests/test_no_personal_context.py",         20),
    ("updater_optionals",      "tests/test_updater_optionals.py",           30),
    ("default_config",         "tests/test_default_config.py",             30),
    ("read_aloud_routing",     "tests/test_read_aloud_routing.py",        20),
    ("bat_block_parens",       "tests/test_bat_block_parens.py",           20),
    ("lfm3_tool_calls",        "tests/test_lfm3_tool_calls.py",            20),
    ("memory_fts",             "tests/test_memory_fts.py",                 30),
    ("safety_review_fixes",    "tests/test_safety_review_fixes.py",        30),
    ("followup_no_plan",       "tests/test_followup_no_plan.py",            20),
    ("coder_plan_dup_guard",   "tests/test_coder_plan_dup_guard.py",        20),
    ("taint",                  "tests/test_taint.py",                      20),
    ("compress_local_only",    "tests/test_compress_local_only.py",             60),
    ("pre_explore_globals",  "tests/test_pre_explore_globals.py",     25),
    ("installer_coreutils", "tests/test_installer_coreutils.py",      20),
    ("installer_download_skip", "tests/test_installer_download_skip.py",     40),
    ("installer_labels", "tests/test_installer_labels.py",             20),
    ("installer_settings_writer", "tests/test_installer_settings_writer.py", 30),
    ("installer_sim", "tests/test_installer_sim.py",                 120),
    ("installer_hardening", "tests/test_installer_hardening.py",      60),
    ("shell_env",           "tests/test_shell_env.py",               20),
    ("follow_ui",           "tests/test_follow_ui.py",               30),
    ("post_run_model",      "tests/test_post_run_model.py",          20),
    ("tts_broken_engine",    "tests/test_tts_broken_engine.py",       30),
    ("feature_search_coverage", "tests/test_feature_search_coverage.py",    40),
    ("ui_context_card",     "tests/test_ui_context_card.py",         20),
    ("vram_guard_lfm",      "tests/test_vram_guard_lfm.py",          20),
    ("model_catalog",      "tests/test_model_catalog.py",           20),
    ("command_flow",       "tests/test_gateway_command_flow.py",    40),
    ("auto_decider_guard",  "tests/test_auto_decider_guard.py",      20),
    ("voice_decider",       "tests/test_voice_decider.py",           30),
    ("mcp_hardening",       "tests/test_mcp_hardening.py",           60),
    ("router_exports",     "tests/test_router_exports.py",          20),
    ("editor_injection",    "tests/test_editor_injection.py",        20),
    ("editor_context",      "tests/test_editor_context.py",          20),
    ("pre_explore_gather",   "tests/test_pre_explore_gather.py",      20),
    ("routing_decider",     "tests/test_routing_decider.py",         15),
    ("token_stats",          "tests/test_token_stats_by_model.py",       15),
    ("write_hang",            "tests/test_write_hang_bounded.py",       20),
    ("verify_gate",           "tests/test_verify_gate.py",               15),
    ("stuck_handler",         "tests/test_stuck_handler_callable.py",        20),
    ("audit_fixes",           "tests/test_audit_fixes_lock_debug.py",      30),
    ("presets_bom",            "tests/test_presets_bom.py",                     30),
    ("preset_never_keys",      "tests/test_preset_never_keys_apply.py",          30),
    ("preset_agent_merge",     "tests/test_preset_agent_merge.py",               30),
    ("preset_snapshot",        "tests/test_preset_snapshot_complete.py",    30),
    ("browser_fileserve",      "tests/test_browser_fileserve.py",               30),
    ("partial_cut",            "tests/test_partial_cut.py",                     30),
    ("memory_routing",         "tests/test_memory_routing.py",                  30),
    ("gateway_auth",           "tests/test_gateway_auth.py",                    60),
    ("gateway_send",           "tests/test_gateway_send.py",                    60),
    ("gateway_redaction",      "tests/test_gateway_redaction.py",               60),
    ("gateway_state",          "tests/test_gateway_state.py",                   60),
    ("gateway_config",         "tests/test_gateway_config.py",                  60),
    ("gateway_render",         "tests/test_gateway_render.py",                  60),
    ("gateway_lint_gate",      "tests/test_gateway_lint_gate.py",               60),
    ("gateway_state_path",     "tests/test_gateway_state_path.py",              60),
    ("gateway_bridge",         "tests/test_gateway_bridge.py",                  60),
    ("gateway_images",         "tests/test_gateway_images.py",                  40),
    ("documents",              "tests/test_documents.py",                    40),
    ("gateway_commands2",      "tests/test_gateway_commands2.py",               60),
    ("send_file",              "tests/test_send_file.py",                       60),
    ("gateway_transport",      "tests/test_gateway_transport.py",               60),
    ("gateway_confinement",    "tests/test_gateway_confinement.py",             90),
    ("webfetch_guard",         "tests/test_webfetch_guard.py",                  60),
    ("csrf_host_guard",        "tests/test_csrf_host_guard.py",                 60),
    ("gateway_supervisor",     "tests/test_gateway_supervisor.py",              60),
    ("phone_restriction",      "tests/test_phone_restriction.py",               60),
    ("verification_honesty",   "tests/test_verification_honesty.py",          60),
    ("profile_launch_flags",   "tests/test_profile_launch_flags.py",         60),
    ("model_filter",            "tests/test_model_filter.py",                  30),
    ("preset_prompts_refix",    "tests/test_preset_prompts_refix.py",          30),
    ("preset_prompt_roles",     "tests/test_preset_prompt_roles.py",           40),
    ("mcp_audit_fixes",         "tests/test_mcp_audit_fixes.py",               60),
    ("mcp_client",              "tests/test_mcp_client.py",                    90),
    ("mcp_client_budgets",      "tests/test_mcp_client_budgets.py",             60),
    ("write_folder_hint",      "tests/test_write_folder_hint.py",         20),
    ("settings_atomic",       "tests/test_settings_atomic.py",            40),
    ("branch_model",           "tests/test_branch_model.py",              20),
    ("planner_scaffold_guard", "tests/test_planner_scaffold_guard.py",    30),
    ("shell_chain_guard",    "tests/test_shell_chain_guard.py",         30),
    ("chunking_fixes",         "tests/test_chunking_fixes.py",               60),
    ("git_init_clone",        "tests/test_git_init_clone.py",               60),
    ("chunk_render_no_write",  "tests/test_chunk_render_no_write_pin.py",      30),
    ("settings_foreign_write", "tests/test_settings_foreign_write_warning.py", 30),
    ("git_ws_guard_init",      "tests/test_git_ws_guard_and_init.py",          60),
    ("ws_guards_chats_skills", "tests/test_ws_guards_chats_skills.py",         60),
    ("tts_start_decay",        "tests/test_tts_start_decay_pin.py",            30),
    ("manager_lock",            "tests/test_manager_lock.py",                  30),
    ("git_clone_cleanup",       "tests/test_git_clone_cleanup.py",              30),
    ("cron",                    "tests/test_cron.py",                           60),
    ("mmproj_fallback",         "tests/test_mmproj_fallback.py",                60),
    ("remember_tool",           "tests/test_remember_tool.py",                  60),
    ("tree_scout_workspace",    "tests/test_tree_scout_workspace.py",           60),
    ("wait_ready",              "tests/test_wait_ready.py",                     60),
    ("chat_search",             "tests/test_chat_search.py",                  60),
    ("compression_plan_survival", "tests/test_compression_plan_survival.py",   60),
    ("smoke_harness",           "tests/test_smoke_harness.py",                 60),
    ("vision_gate",             "tests/test_vision_gate.py",                   60),
    ("steer_images",            "tests/test_steer_images.py",                  60),
    ("log_archive",             "tests/test_log_archive.py",                   30),
    ("load_mode_ram_guard",     "tests/test_load_mode_ram_guard.py",           30),
    ("load_preflight",          "tests/test_load_preflight.py",                60),
    ("preflight_failclosed",    "tests/test_preflight_failclosed.py",          60),
    ("langrun_guard_failclosed", "tests/test_langrun_guard_failclosed.py",     30),
    ("ctx_full_storm",          "tests/test_ctx_full_storm.py",               30),
    ("perf_logging",           "tests/test_perf_logging.py",                30),
    ("duo_ubatch",             "tests/test_duo_ubatch.py",                  30),
    ("cmd_flags",              "tests/test_cmd_flags.py",                  30),
    ("preflight_matrix",       "tests/test_preflight_matrix.py",           60),
    ("groot_profile",          "tests/test_groot_profile.py",               30),
    ("side_fallback",          "tests/test_side_fallback.py",              30),
    ("duo_split_integrity",    "tests/test_duo_split_integrity.py",        30),
    ("cache_budget",           "tests/test_cache_budget.py",               30),
    ("tail_cap",               "tests/test_tail_cap.py",                    30),
    ("compression_empty_summary", "tests/test_compression_empty_summary.py", 30),
    ("vram_profile_sizing",     "tests/test_vram_profile_sizing.py",       30),
    ("tool_call_miss_log",      "tests/test_tool_call_miss_log.py",         30),
    ("tool_round_default",      "tests/test_tool_round_default.py",         30),
    ("runtime_flags",           "tests/test_runtime_flags.py",              60),
    ("deadline_and_416",        "tests/test_deadline_and_416.py",           30),
    ("mcp_security_gate",       "tests/test_mcp_security_gate.py",          60),
    ("mcp_phase2",              "tests/test_mcp_phase2.py",                  90),
    ("mcp_2025",                "tests/test_mcp_2025.py",                    120),
    ("mcp_2026",                "tests/test_mcp_2026.py",                     30),
    ("lsp_client",              "tests/test_lsp_client.py",                  90),
    ("lsp_tools",               "tests/test_lsp_tools.py",                   60),
    ("lsp_real_pyright",        "tests/test_lsp_real_pyright.py",            120),
    ("lsp_real_rust",           "tests/test_lsp_real_rust.py",               180),
    ("update_check",            "tests/test_update_check.py",                30),
    ("compression_whole_drop",  "tests/test_compression_whole_drop.py",      30),
    ("mem_watch",               "tests/test_mem_watch.py",                   30),
    ("worker_warm_pin",         "tests/test_worker_warm_pin.py",             30),
    # audit 2026-11-03 P0 coverage
    ("verify_prompt_text",      "tests/test_verify_prompt_text.py",          20),
    ("write_file_crlf",         "tests/test_write_file_crlf.py",             30),
    ("unbound_guards",          "tests/test_unbound_guards.py",              20),
    ("mcp_secret_guard",        "tests/test_mcp_secret_guard.py",            60),
    ("approval_no_run_id",      "tests/test_approval_no_run_id.py",          20),
    ("git_consolidate_scope",   "tests/test_git_consolidate_scope.py",       60),
    ("git_commit_scope",        "tests/test_git_commit_scope.py",            40),
    ("settings_ui",             "tests/test_settings_ui.py",                 20),
    ("tool_args_nonobject",     "tests/test_tool_args_nonobject.py",         20),
    ("tool_python_interpreter", "tests/test_tool_python_interpreter.py",     60),
    ("max_rounds_truth",        "tests/test_max_rounds_truth.py",            20),
    ("loop_detected_stream",    "tests/test_loop_detected_stream.py",        20),
    ("backend_slot_invariants", "tests/test_backend_slot_invariants.py",     20),
    ("unpin_resolution",        "tests/test_unpin_resolution.py",            30),
    ("prefetch_order",          "tests/test_prefetch_order.py",              20),
    ("critic_empty_verdict",    "tests/test_critic_empty_verdict.py",        20),
    ("git_undo_untracked",      "tests/test_git_undo_untracked.py",          60),
    ("settings_roundtrip",      "tests/test_settings_roundtrip.py",          20),
    ("stream_chat_id",          "tests/test_stream_chat_id.py",              20),
    ("model_download_integrity", "tests/test_model_download_integrity.py",   60),
    ("mcp_stdio_cap",           "tests/test_mcp_stdio_cap.py",               20),
    ("pause_state_sanitized",   "tests/test_pause_state_sanitized.py",       20),
    ("gate_redaction",          "tests/test_gate_redaction.py",              20),
    ("inflight_age",            "tests/test_inflight_age.py",                20),
    ("mcp_write_without_run_id", "tests/test_mcp_write_without_run_id.py",   30),
    ("posix_run_bash",          "tests/test_posix_run_bash.py",              30),
    ("claim_path_normalize",    "tests/test_claim_path_normalize.py",        20),
    ("planner_cancel_race",     "tests/test_planner_cancel_race.py",         20),
    ("memory_request_detector", "tests/test_memory_request_detector.py",     20),
    ("pause_registry",          "tests/test_pause_registry.py",              20),
    ("telegram_split",          "tests/test_telegram_split.py",              20),
    ("safe_profile_models",     "tests/test_safe_profile_models.py",         20),
    ("preset_token_scrub",      "tests/test_preset_token_scrub.py",          20),
    ("gpu_backend",             "tests/test_gpu_backend.py",                 30),
    ("path_case",               "tests/test_path_case.py",                   20),
    ("stream_gap",              "tests/test_stream_gap.py",                  20),
    ("moe_cache",               "tests/test_moe_cache.py",                   20),
    ("fit_flags",               "tests/test_fit_flags.py",                   20),
    ("loader_audit",            "tests/test_loader_audit_2026_11_03.py",     30),
    ("agentic_audit",           "tests/test_agentic_audit_2026_11_03.py",    30),
    ("audit_fixes_p0",          "tests/test_audit_fixes_2026_11_03.py",       30),
    ("lsp_security",            "tests/test_lsp_security.py",                20),

]


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    only = ""
    if "--only" in sys.argv:
        i = sys.argv.index("--only")
        only = sys.argv[i + 1].lower() if i + 1 < len(sys.argv) else ""

    results = []
    failed_total = 0
    print("=" * 64)
    print("REGRESSION RUN - HiveMind Feature-Checks")
    print("=" * 64)
    for name, rel, timeout in SUITES:
        # --only must be applied BEFORE the missing-file check; on a partial
        # checkout it otherwise failed every registered-but-absent suite.
        if only and only not in name.lower():
            continue
        if not (ROOT / rel).exists():
            # SKIP-ONLY-BY-FLAG (2026-10-27, audit): this branch swallowed a
            # DELETED or renamed suite - coverage shrank, the run stayed green.
            # A registered suite missing on a full checkout is a defect.
            if "--allow-missing" in sys.argv:
                print(f"[SKIP] {name:24s} (--allow-missing: file not present)")
                continue
            results.append((name, False, 0.0))
            failed_total += 1
            print(f"[FAIL] {name:<20} ({rel} missing - registered but not on disk;"
                  f" --allow-missing to skip on partial checkouts)")
            continue
        target = ROOT / rel
        t0 = time.time()
        # ENC-FIX (2026-10-15): suites print non-ASCII (❌/✅, umlauts) — a
        # cp1252 console (PowerShell without PYTHONUTF8) killed them with
        # UnicodeEncodeError. Force UTF-8 for every child, independent of
        # the invoking console's locale.
        _suite_env = {**os.environ, "PYTHONIOENCODING": "utf-8:replace"}
        try:
            r = subprocess.run(
                [sys.executable, str(target)],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                env=_suite_env,
                cwd=str(ROOT), timeout=timeout)
        except subprocess.TimeoutExpired:
            dur = time.time() - t0
            failed_total += 1
            results.append((name, False, dur))
            print(f"[FAIL] {name:<20} (timeout after {timeout}s)")
            continue
        dur = time.time() - t0
        ok = r.returncode == 0
        failed_total += 0 if ok else 1
        results.append((name, ok, dur))
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<20} ({dur:.0f}s)")
        if not ok:
            tail = (r.stdout or "").strip().splitlines()[-8:]
            errtail = (r.stderr or "").strip().splitlines()[-5:]
            for l in tail:
                print(f"    | {l}")
            for l in errtail:
                print(f"    ! {l}")

    print("=" * 64)
    ran = len(results)
    npass = sum(1 for _, ok, _ in results if ok)
    print(f"Suites: {npass}/{ran} PASS, {failed_total} suite(s) failed")
    return 1 if failed_total else 0


if __name__ == "__main__":
    sys.exit(main())
