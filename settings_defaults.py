# -*- coding: utf-8 -*-
"""DEFAULT_SETTINGS + DEFAULT_AGENT_CFG — pure data, no I/O.

Split from settings.py (2026-11-05 maintainability audit): settings.py was
imported by 63 modules and 700 of its 1,094 lines were this dict. Pure data
in its own module makes the schema diffable and shrinks the import surface;
settings.py re-exports both names, so no importer changes.
"""

DEFAULT_AGENT_CFG = {
    # AGENT-DEFAULTS (2026-10-05, no-hardcoded-models): fresh installs
    # ship WITHOUT model names — a run with an unconfigured/absent model
    # fails fast with a model-load error instead of aborting with a
    # "not found" on some hardcoded GGUF. Pick models in the UI, via
    # /setModel (phone) or setup_models.bat; existing settings.json
    # files are untouched by this.
    "analyst":     {"model": "", "temperature": 0.3, "max_tokens": 1100, "thinking": False, "thinking_budget": 0},
    "refiner":     {"model": "",    "temperature": 0.3, "max_tokens": 400, "thinking": False, "thinking_budget": 0},
    "critic":      {"model": "", "temperature": 0.2, "max_tokens": 600, "thinking": False, "thinking_budget": 0},
    "synthesizer": {"model": "", "temperature": 0.2, "max_tokens": 900, "thinking": False, "thinking_budget": 0},
    "direct":      {"model": "", "temperature": 0.4, "max_tokens": 600, "thinking": False, "thinking_budget": 0},
    "judge":       {"model": "",   "temperature": 0.1, "max_tokens": 120, "thinking": False, "thinking_budget": 0},
    "duo_coder":   {"model": "", "temperature": None, "max_tokens": 8000, "thinking": False, "thinking_budget": 0},
    "duo_critic":  {"model": "", "temperature": 0.15, "max_tokens": 600, "thinking": False, "thinking_budget": 0},
}


DEFAULT_SETTINGS = {
    # ════════════════════════════════════════════════════════════════════════
    # A) WORKSPACE & BASIS
    # ════════════════════════════════════════════════════════════════════════
    "workspace":               "",
    "workspace_force_ui":      True,
    "server_port":             8001,
    "models_dir":              "",
    "agents":                  DEFAULT_AGENT_CFG,
    # CARD-BOOKKEEPING (written by the agent-card save path; read by presets)
    "agents_user_model_choice": {},
    "agents_user_sampling_choice": {},
    "max_iterations":          2,
    # DEFAULT-MODE: a fresh install answers in the chat/direct route. The old
    # value "simple" is the same route under a legacy name - the UI calls it
    # CHAT, so the default says chat.
    "mode":                    "chat",
    "active_preset":           None,

    # ════════════════════════════════════════════════════════════════════════
    # B) VRAM & GPU
    # ════════════════════════════════════════════════════════════════════════
    "vram_budget_gb":          7.5,
    "safe_profile_policy":     "default_8gb_v1",
    "safe_profile_matrix_file": "model_configs/safe_profile_matrix.json",
    "default_keep_alive":      "10m",
    # DEBUG-DUMP (2026-10-16): request instrumentation. Off by default; when
    # set, every model request is written to
    # logs/telemetry/dump_<role>_<chatid>_<ts>.json exactly as sent
    # (core/telemetry.py, role "planner" or "coder"). The key is listed here so
    # the switch is discoverable and round-trips through settings.json instead
    # of requiring a hand-edited file.
    "debug_dump_request":      False,
    "smart_preload_keep_alive": "10m",
    "max_concurrent_models":   2,
    "allow_cpu_offload":       True,
    "warn_on_cpu_offload":     True,
    "max_model_size_gb":       6.0,
    "prefer_smaller_models":   True,
    "pin_direct_after_response": False,
    "judge_keepalive_enabled": False,
    "duo_worker_slots":        2,
    "moe_cpu_experts":         {},
    # EXPERT-CACHE (2026-11-03): opt-in llama.cpp --moe-cache-mib. Keeps hot
    # MoE experts in VRAM (decode only, small batches). 0 = off. Vulkan builds
    # can crash at load (#30697); the loader retries once without the flag.
    "moe_cache_mib":           0,
    "llama_mlock":             True,
    # Auto-fit (audit B3): omit --n-gpu-layers so llama.cpp fits the layers
    # to the free VRAM itself — it refuses while the flag pins 99. Default
    # keeps the explicit full-offload behavior.
    "llama_gpu_layers_auto":   False,
    # FIT (2026-11-03): default ON - llama.cpp --fit sizes unset args
    # (layers, ctx) to the free VRAM. Turn it OFF when you set your own MoE
    # distribution (moe_cpu_experts) or a fixed --n-gpu-layers. Target is the
    # per-device margin in MiB (0 = llama default).
    "llama_fit":               True,
    "llama_fit_target_mib":    0,
    "gpu_backend":             "",
    # 0 = off, 256 = llama.cpp recommendation.
    "llama_cache_reuse":       256,
    # Prompt-processing chunk size (llama.cpp --ubatch-size). Larger values
    # (512/1024) speed up prefill substantially for MoE models with CPU expert
    # offloading (fewer RAM-streaming passes over the expert weights), at the
    # cost of a larger VRAM compute buffer. Test lever for slow post-compression
    # prefill; revert to 256 if a load OOMs on tight GPUs.
    # Canonical key (the older llama_ubatch_size below stays as the alias).
    # 256 is the shipped tight-GPU baseline; 512/1024 is the measured opt-in.
    "duo_ubatch":              256,
    "llama_ubatch_size":       256,
    # HOST-RAM / VRAM TREND (2026-10-27): llama.cpp keeps context checkpoints
    # and a host-side prompt cache per slot, and both grow with how much a slot
    # has processed. A long run got slower the longer it ran. 0 disables.
    "llama_ctx_checkpoints":   4,
    "llama_cache_ram_mib":     1024,
    # VRAM pre-flight gate (2026-10-11): when True (default), loads estimated
    # not to fit are refused before llama-server starts (estimate can be
    # wrong for foreign models - turning this OFF makes the engine attempt
    # the load and halve ctx on real failures instead).
    "vram_preflight_gate":     True,

    # ════════════════════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════════════════════
    # Agentic tool rounds per chunk. 50 (2026-10-31): a fresh install must
    # be able to write files; 0 silently disabled the whole tool loop and
    # chunking then produced text-only "implementations".
    "duo_tool_rounds":         50,
    # CORE CODER TIER (2026-11-08): default "core" (17 tools: read/edit/run/
    # tests + lsp_diagnostics) instead of "full" (33 incl. git/web/MCP/
    # browser/LSP extras). Full is opt-in when a task really needs them.
    "duo_coder_tool_tier":     "core",
    "duo_use_pipeline":        False,
    "duo_critic_tools":        False,
    # CHUNKING DEFAULT OFF (2026-10-27, owner): stays a working opt-in, but a
    # fresh install runs one coder pass (the planner subtasks are skipped
    # with an honest status) instead of silently chunking.
    "duo_chunking":            False,
    "duo_coding_mode":         True,
    "duo_pre_explore":         False,
    # UI state only: remembers the Duo "pair" selector position. The same
    # request also sets agents.duo_coder.model, which IS what the engine reads.
    "duo_pair":                "focused",
    # ON by default (2026-10-27, owner): a coding run should finish the job
    # instead of stopping at the round limit. Turn off for a quick answer.
    "until_finished":          True,
    "duo_runtime_profile":     "balanced",
    "duo_profile_speed_model": "",
    "duo_profile_quality_model": "",
    # DEFAULT-SUBMODE: the agentic coder (one model, solo in VRAM) is the
    # default; the Critic-Duo is one click away and costs a second model.
    "duo_agentic_mode":        True,
    # ── ROUTING (2026-10-20): two independent deciders, each switchable ──
    # The LLM judge costs a model load into VRAM (60 s + eviction) and
    # returns text that has to be parsed. The decider is an encoder
    # (GLiNER2.5, CPU, ~0.5-1.2 s, no VRAM) and is optional
    # (pip install hivemind[decider]). Each can be turned off on its own;
    # "shadow" computes and logs but does not decide.
    #   off | shadow | on
    "routing_decider":          "off",
    "routing_judge":            "on",
    # Who decides whether a planner is needed?
    #   auto    = decider when running, otherwise judge (avoids the GPU hit)
    #   decider | judge = fixed
    #   always | never  = planner always / never
    "routing_planner":          "auto",
    "routing_decider_model":    "fastino/GLiNER2.5-Decide",
    # Below this confidence the decision is marked uncertain (and goes to
    # the judge when active). Calibrated on the multi-label action labels:
    # hits were 0.49-0.92 (median 0.72) - 0.85 would have escalated almost
    # everything.
    "routing_decider_threshold": 0.55,
    "duo_agentic_thinking":    False,
    "_thinking_before_chunking": None,  # persisted user-preference before chunking forced thinking ON
    # Automatic/override keys that DEFAULT_SETTINGS now exposes explicitly
    # (previously only implicit read fallbacks in the consumers).
    "duo_coder_model":          "",
    "duo_critic_model":         "",
    "duo_caps":                 {},
    "duo_pyright_path":         "",

    # ════════════════════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════════════════════
    "duo_run_timeout_seconds": 420,
    "duo_run_timeout_critical_seconds": 900,
    "duo_until_finished_cap":  999999,
    # UNTIL-FINISHED-BOUND (2026-11-02): until-finished runs are meant to work
    # for hours - the owner can always kill a run (/stop, the Stop button, the
    # phone). The wall clock is therefore generous by default (24 h) and only
    # there so a wedged model cannot hold the machine forever; raise it up to
    # 72 h when a task genuinely needs it.
    "duo_until_finished_max_hours": 24,
    "duo_rounds_balanced_cap": 3,
    "duo_read_timeout":        390,
    "duo_run_bash_build_timeout_s": 600,
    "duo_llm_slow_timeout_s":  300,
    "duo_max_tool_rounds_runtime_cap": 300,
    "duo_max_tool_rounds":     64,
    "duo_tool_output_ttl":     3,
    "duo_compress_threshold":  0,
    # CACHE-FRIENDLY (2026-09-04): protect the llama.cpp prefix cache.
    # LEGACY-REMOVAL (2026-09-09): duo_cache_friendly_ctx was removed —
    # compression-first is the only regime.
    # - duo_partial_compression: condense only the old part and keep the raw
    #   tail byte-identical at the end (KV shift reuse). Recommended ON:
    #   preserves the llama.cpp prefix cache and the raw tail stays readable
    #   for the coder; telemetry phase is done (2026-09-09).
    # - duo_compress_threshold > 0 = exact override in absolute tokens
    #   (settings.json / power user, no UI field);
    #   0 = auto: threshold = duo_compress_auto_floor*ctx (the dynamic output
    #   reserve clamps every tool round so prompt+output <= ctx holds, so no
    #   static max_tokens deduction is needed).
    # - duo_compress_model: light model for the compression summary
    #   (if VRAM suffices without evicting the coder, else falls back to the coder model).
    # - duo_compress_llm_timeout_s: read timeout of the compression LLM (120s was
    #   too tight for the 35B CPU MoE -> ReadTimeout -> fallback summaries).
    # - duo_compress_local_only: skip the LLM summary entirely and build the
    #   local (instant) fallback summary directly. For hardware where the
    #   summary call routinely runs into the read timeout (MoE with CPU
    #   experts, slow prefill): there the LLM attempt only costs dead time
    #   and the local summary is also more cache-friendly.
    # SHIPPED DEFAULT CHANGE (2026-10-08, owner): full compression only.
    # Partial kept the raw tail for prefix-cache reuse, but on slow-prefill
    # hardware (Vulkan) the barely-shrunken large prompts cost far more in
    # per-round prefill/decode than the occasional cache miss - and the
    # owner's live test read the full mode as "fast + actually empties".
    "duo_partial_compression":   False,
    "duo_compress_auto_floor":   0.70,
    # MASK-FIRST (2026-10-14, audit phase 4): deterministic observation
    # masking (stale tool outputs -> recall markers) runs before the LLM
    # summary and can resolve the threshold crossing without it — no LLM
    # call, no slot evict.
    "duo_mask_before_compress":  True,
    "duo_compress_overflow_reserve": 1024,
    # Chars kept from tool output when a compression tail-caps it (read by
    # duo_runner with an 8000 fallback).
    "duo_compress_tail_tool_max_chars": 8000,
    "duo_max_compressions":      40,
    "duo_compress_model":        "auto",
    "duo_compress_llm_timeout_s": 180,
    "duo_compress_local_only":   False,
    "session_compress_threshold": 20,
    # Token budget for the conversation history a run sees (2026-10-03).
    # Replaces blind message-count caps: newest turns verbatim, older
    # turns dropped whole once the budget is full. 0 (default) derives
    # the budget from the direct model's context (35% of n_ctx minus a
    # 4096 reserve for system prompt and output); set a number to force
    # a fixed budget.
    "session_history_budget_tokens": 0,

    # ════════════════════════════════════════════════════════════════════════
    # E) EXPLORE, REPO-MAP & MEMORY
    # ════════════════════════════════════════════════════════════════════════
    "duo_tree_scout_enabled":  True,
    "duo_tree_scout_max_depth": 4,
    "duo_tree_scout_max_files": 200,
    "duo_static_map_chars":    0,   # 0 = tier-derived (rich: 8000); >0 = explicit static repo-map char budget
    # REPO-MAP-PIN (2026-09-06): put "## Static Repo-Map" into the system message
    # (byte-stable = cache-stable across compressions). Deltas/TODO consolidation
    # see core/repomap_pin.py. Baseline (live 2026-09-06, PacMan/8 files):
    # post-compress cached was ~6905/7137 (system+tools) and rose to ~8003 with
    # pinning - reference value in case a change breaks prefix stability.
    "duo_pin_static_map":      True,
    "duo_coder_explore_chars": 0,
    "duo_parallel_preexplore": False,
    "duo_partition_max_files": 30,
    "duo_pass_explore_files":  "touched",
    "duo_pre_explore_timeout_seconds": 600,
    "duo_pre_explore_max_tools": 20,
    "duo_pre_explore_ctx":     4096,
    "duo_pre_explore_ctx_char_ratio": 3.0,
    "duo_pre_explore_tokens":  700,
    "duo_pre_explore_llm_timeout_s": 600,
    "duo_pre_explore_max_files_est": 15,
    "duo_pre_explore_timeout_per_file_s": 20,
    "duo_repo_memory_enabled": True,
    "duo_repo_memory_top_k":   2,
    "duo_repo_memory_min_score": 0.12,
    "duo_symbol_ref_enabled":  True,
    "duo_symbol_ref_top_k":    2,
    "duo_symbol_ref_max_items": 120,

    # ════════════════════════════════════════════════════════════════════════
    # F) PLANNER
    # ════════════════════════════════════════════════════════════════════════
    "duo_planner_use_exec_model": True,
    "duo_planner_model":       None,
    "duo_planner_default_thinking": True,
    "duo_planner_max_steps":   0,
    "duo_thinking_per_chunk":  False,
    "disable_thinking_in_planner": False,
    # PLANNER-OUTPUT-BUDGET (0.99.2, user decision): visible task budget
    "duo_planner_max_tokens":  8000,
    # Planner on/off for the request path (server.py reads this key).
    # DEFAULT-PLANNER: plan first. The planner shares the coder context
    # (duo_planner_use_coder_ctx), so this costs no second model load.
    "duo_planner_enabled":     True,
    # Thinking → hermes-MoE thought until the 600s wall timeout and delivered empty
    # output. 4k was too tight for complex plans → 8k default (2026-08-31).
    "duo_planner_thinking_budget": 8000,
    "duo_planner_ctx_cap":     None,
    "duo_planner_ctx_target":  None,
    "duo_planner_use_coder_ctx": True,
    "duo_planner_ensure_load_timeout_s": 300,
    "duo_planner_thinking_timeout_s": 600,
    "duo_soft_planner_wall_timeout_s": 300,
    "duo_planner_ttl_seconds": 0,    # 0 = auto (450s), >0 = Override
    "duo_coder_ttl_seconds":   0,    # 0 = auto (420s), >0 = Override
    # Send images to the coder even when the task looks text-only.
    "duo_coder_always_vision": False,
    # CODER LIFETIME (2026-10-17): keep = the slot stays warm after the
    # run (idle TTL decides), evict = release it at the run end so the next
    # prompt can load any model/mode without evicting anything.
    "duo_coder_after_run":     "keep",
    "plan_tracker_classifier": "heuristic",

    # ════════════════════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════════════════════
    # SERIAL-SLOTS (2026-09-10): unload the planner when the coder phase
    # starts and the models differ (frees VRAM for coder ctx; costs one
    # reload when switching back).
    "duo_evict_planner_on_coder": True,
    "duo_coder_ctx_agentic":   None,
    "duo_coder_ctx_normal":    None,
    "duo_coder_tool_thinking": False,
    "duo_coder_tool_thinking_auto_mode": "off",
    # truncated at the output-token limit (finish_reason=length -> DROPPED -> loop),
    # coupled to the token budget: max_chars ~ budget_tokens * factor - overhead.
    # Step; cap 3.3 (documented measured value).
    "duo_write_chars_per_token": 2.5,
    "duo_coder_fallback_model": "",
    "duo_critic_ctx":          None,

    # ════════════════════════════════════════════════════════════════════════
    # H) CRITIC, TESTS & PEER-RATINGS
    # ════════════════════════════════════════════════════════════════════════
    "duo_peer_ratings_agentic": False,
    "duo_tool_autopromote_max_rounds": 4,
    "duo_test_feedback":       False,
    # LOOP-DEFAULT (2026-10-23): per-chunk auto-test + self-fix now ships ON.
    # The EDIT→TEST→FIX loop is this harness's core idea — the per-chunk
    # verify (and the edit→test mapping) only ran for users who found this
    # toggle, so most agentic runs finished without any test feedback.
    # Both this and duo_test_feedback_final must be ON for the full loop.
    "duo_test_feedback_chunk": True,
    # TESTING-DEFAULT (2026-08-12): the final auto-test is on by default —
    "duo_test_feedback_final": True,
    # SELECTIVE-TESTS (D4, 2026-11-09): run_tests scopes pytest/npm to the
    # testmap-mapped tests of this run's written files; [] falls back to the
    # full suite. Explicit paths/targets win; set false to always run all.
    "duo_selective_tests": True,
    # CONTEXT BUDGET (D2, 2026-11-09): upper char cap for the tiny-workspace
    # pre-seed block (core/duo_helpers.build_preseed_context). The effective
    # cap is min(this, ctx_tokens/4, floor 2000); 8000 keeps the old behavior.
    "duo_context_budget_chars": 8000,
    # ERROR ROLLUP (2026-09-06): deterministic dedup of repeating test/compile
    # failures in the fix-loop (core/error_rollup.py). First failure full,
    # repeats rendered as PERSISTS/FIXED/REOPENED. Runtime state per run.
    "duo_error_rollup":        True,

    # AUTOMATIONS (2026-11-05): engine-owned scheduled runs (UI tab
    # "Automations"). False is the kill switch; jobs live in
    # automations.json, not here.
    "automations_enabled":     True,

    # ════════════════════════════════════════════════════════════════════════
    # I) GIT, SANDBOX & SYSTEM
    # ════════════════════════════════════════════════════════════════════════
    "duo_git_autocommit":      False,
    # OFF (2026-10-18): a checkpoint is a real git commit in the user's repo.
    # Opt-in; a bad write is still covered by undo_last + the write transaction.
    "duo_git_checkpoints":     False,
    # Sandbox subprocesses (run_bash/run_python/background) via Windows Job
    # Objects (KILL_ON_JOB_CLOSE).
    "duo_tool_sandbox":        True,
    # Job resource limits (0 = unlimited) for run_bash/run_python/background.
    "duo_tool_sandbox_max_mem_mb": 4096,
    "duo_tool_sandbox_max_procs": 64,
    # POSIX only (Linux/macOS): RLIMIT_CPU seconds per tool child. 0 = unset.
    "duo_tool_sandbox_max_cpu_s": 0,

    # DEBUG API (2026-10-18, audit): /debug/tasks returns asyncio task stacks with
    # file paths, /debug/models registry and filesystem internals, /thinking/debug
    # and /budget/debug expose internals - all on an unauthenticated loopback API.
    # Off by default; the router is not even mounted until this is true.
    "debug_api_enabled":       False,
    "read_guard_enabled":      True,
    # WRITE-GUARD duo_full (2026-09-06): blocks write_file on existing files
    # never seen in this run (rollback lever like duo_error_rollup /
    # duo_pin_static_map). False = old behavior (blind write_file allowed).
    "duo_write_guard_enabled": True,
    # NO-OP hint (2026-09-07): injects a read_file nudge after 2x ineffective
    # edits on the same path (Python run-state, compression-proof).
    "duo_noop_hint_enabled":    True,
    # WEDGE-HANDOFF (2026-09-17): when the coder fails the same edit on one
    # file repeatedly (streak threshold, or the identical call twice within a
    # streak), the run delegates that ONE edit to a fresh-context fix agent
    # (same model weights, clean window) and continues. Off by default until
    # WEDGE-DEFAULT (audit 2026-10-24): the handoff converts the #1 small-
    # model death spiral (repeating a failing edit) into a fresh-context fix
    # agent — default on. Includes run_bash wedges (see EDIT_FAMILY_TOOLS
    # counterpart in the gate).
    "duo_wedge_handoff_enabled":         True,
    "duo_wedge_handoff_streak_threshold": 4,
    "duo_wedge_handoff_max_per_run":     2,
    "duo_wedge_handoff_timeout_s":       900,
    "duo_wedge_handoff_max_rounds":      10,
    # MAX-TOKENS-KEY (2026-10-17): core/fix_agent.py has always read this key
    # (int(_s("duo_wedge_handoff_max_tokens", 1200))), but it was missing here
    # and in docs/settings.md - the value was fixed at 1200 and could not be
    # configured. Listed now so the UI/settings.json can set it.
    "duo_wedge_handoff_max_tokens":      1200,
    "duo_wedge_handoff_model":           "",   # empty = coder model (fresh context, same weights)
    "keep_awake_during_run":   True,
    # Windows toasts for run start/end and approval cards.
    "desktop_notifications":   True,
    # UI-side only (B5, 2026-11-07): gates the browser's chat autosave - it
    # flushes the chat json before /stream and after edits. The engine never
    # reads it, and that is intentional: persistence is a client decision.
    # Listed here so the checkbox is discoverable and round-trips through
    # settings.json instead of depending on a hand-edited key.
    "chat_autosave_enabled":   True,
    # UI-side only: VRAM panel refresh interval (static/app.js). The engine
    # never reads it; listed so the postSettings key is discoverable.
    "vram_refresh_interval_s": 8,
    "constraint_mode":         True,
    "git_repo_url":            "",
    "git_username":            "",
    "git_email":               "",
    "git_token":               "",
    "git_default_branch":      "main",
    "git_commit_prefix":       "hivemind:",
    "git_auto_push":           False,
    # GIT-TOOLS-GATE (2026-10-17): git_status/git_commit only ship in a mode's
    # tool schema when git is set up. "auto" = one of git_repo_url /
    # git_username / git_email / git_token is filled in; true/false force it.
    "git_tools_enabled":       "auto",

    # ════════════════════════════════════════════════════════════════════════
    # J) WEBSEARCH & SEARXNG
    # ════════════════════════════════════════════════════════════════════════
    "searxng_host":            "http://localhost:8888",
    # --- Voice: speech-to-text (1.4, English only) -----------------------
    # Scope for 1.4 (docs/plan_1.4_voice.md): English only, one
    # CLI call per request, model tier chosen by RAM unless set explicitly.
    "stt_enabled":              False,
    "stt_model":                "",      # "" = RAM default (parakeet q4_k >= 8 GB)
    "stt_language":             "en",    # 1.4 ships en only
    "stt_translate":            False,   # whisper --translate (English target; whisper tiers only)
    "stt_threads":              0,        # 0 = min(4, cpu//2)
    "stt_preload":              False,    # warm whisper-server (whisper models only)
    "stt_max_seconds":          300,
    "stt_timeout_s":            120,
    "stt_log_transcripts":      False,
    # --- Voice: text-to-speech (1.4, English, Pocket TTS via uvx) --------
    # Opt-in like STT: the sidecar (torch) only starts when tts_enabled is on.
    # Cosette + 1.08x speed are the owner's picks (2026-10-17); Pocket TTS has
    # no speed parameter, so the factor is applied by HiveMind (WSOLA).
    "tts_enabled":               False,
    "tts_voice":                 "anna",
    "tts_speed":                 1.08,
    "tts_timeout_s":             120,
    "tts_idle_stop_s":           900,
    # Read-aloud (frontend): what the UI speaks on its own when TTS is on.
    # Direct = the whole answer; agentic = the plan first, then the coder's
    # answer; ask/gate read the interactive cards. All default off.
    "tts_read_direct":           False,
    "tts_read_agentic":          False,
    "tts_read_ask":              False,
    "tts_read_gate":             False,
    # Telegram gateway voice (per bot, opt-in). The gateway reads these from
    # the engine, so the UI switches drive it; greying follows stt_enabled /
    # tts_enabled (a voice note needs STT, a voice answer needs TTS).
    "telegram_voice_in":         False,
    "telegram_voice_out":        False,
    # IMAGES (2026-11-01): a photo sent to the bot becomes a run with the
    # caption as prompt and the picture attached (engine /stream images).
    # Needs a vision-capable model for the active route, or a configured
    # vision agent that preprocesses the picture into text.
    "telegram_image_in":         True,
    # PDF/text extraction limits (engine /documents/extract). The gateway
    # sends bytes, the engine answers text or a DOC_* code.
    "document_max_chars":        120000,
    "document_max_pages":        40,
    # What a phone run sends back (1.4.2). "both" = text + voice note, "text" =
    # text only, "voice" = only the model-generated answer as a voice note (the
    # progress lines stay text). telegram_progress_msgs turns the info lines off
    # entirely for a pure "results only" channel.
    "telegram_result_mode":      "both",
    "telegram_progress_msgs":    True,
    # Warm the TTS engine when a run starts instead of when the first chunk is
    # ready - the sidecar then needs ~1 s per sentence instead of a 12 s cold
    # start. Useful for phone runs where nobody touched the PC.
    "tts_preload_run":           True,
    # Browser playback level (0..1). The engine already normalises the WAV to
    # -1.5 dBFS peak, so this is the user's "how loud do I want it here" knob.
    "tts_volume":                1.0,
    # Server-side levelling of the sidecar output (measured -15.5 dBFS peak).
    "tts_normalize":             True,

    # Only bing + duckduckgo answer reliably on stock SearXNG instances:
    # google sits CAPTCHA-suspended, wikipedia returns 0 under most language
    # tags, github floods the ranking with 30 keyword-matched repos per query.
    # Leave empty: SearXNG then queries every engine it has ENABLED. Pinning
    # "bing,duckduckgo" here (the old default) hid the wikipedia/other engines
    # and every English query came back as German shop pages.
    "searxng_engines":         "",
    # DO NOT set a region/language tag ("en", "en-US", ...): on stock instances
    # that makes every engine return region-fallback garbage (google.fr login
    # pages for a "qwen3 context length" query — live-tested 2026-09-18).
    # "all" returns English results for English queries, which the coder emits.
    "searxng_language":        "en",
    "pipeline_websearch_enabled": True,
    "duo_websearch_enabled":   False,
    "websearch_auto_trigger":  True,
    "duo_websearch_max_calls": 20,
    "duo_install_max_calls":   3,
    "duo_websearch_timeout_seconds": 20,
    "duo_websearch_timeout_fast_seconds": 13,
    "duo_websearch_timeout_critical_seconds": 24,

    # ════════════════════════════════════════════════════════════════════════
    # J2) DIRECT-CHAT-TOOLS (Simple/Direct-Mode Tool-Use, 2026-08-31)
    # ════════════════════════════════════════════════════════════════════════
    "direct_tools_enabled":    True,
    # run_bash shell on Windows: "auto" = Git Bash when present (small
    # models emit POSIX bash; powershell chokes on every line), else
    # powershell. "bash"/"powershell" force one.
    "run_bash_shell":          "auto",
    "direct_tools_tier":       "readonly",   # off | readonly(websearch) | research(web+read) | python(read+python) | full
    "direct_tools_max_rounds": 12,

    # ════════════════════════════════════════════════════════════════════════
    # K) VISION
    # ════════════════════════════════════════════════════════════════════════
    "vision_agent_enabled":    False,
    "vision_agent_model":      "gemma-4:e4b-it-qat",
    "vision_agent_mode":       "sequential",
    "vision_preprocess_timeout_seconds": 30,
    "vision_preprocess_load_timeout_seconds": 120,
    # PIPELINE-VISION (2026-08-19): feed images directly to multimodal pipeline agents?
    "pipeline_vision_direct":  False,
    # Per-role direct-image flags, e.g. {"analyst": true}.
    "pipeline_vision_roles":   {},
    "image_desc_full_pipeline": False,

    # ════════════════════════════════════════════════════════════════════════
    # L) PRELOAD & PREFETCH
    # ════════════════════════════════════════════════════════════════════════
    # OFF by default (2026-10-18): prefetch/keep-warm loads the NEXT agent's
    # model while the current one works - great on a big card, but on a tight
    # VRAM budget it can evict the model that is actually answering. Opt in when
    # the card has room.
    "smart_preload_enabled":   False,
    "startup_preload_enabled": False,
    # OFF (2026-10-18): warms the judge model before the routing decision - pure
    # latency win on a big card, an eviction risk on a small one. Opt-in.
    "judge_prefetch_before_complexity": False,
    "prefetch_agent_avgs":     {},
    "prefetch_lead_seconds":   8.0,
    "preload_workers_after_run": False,

    # ════════════════════════════════════════════════════════════════════════
    # M) AUTOMAP / ROUTING
    # ════════════════════════════════════════════════════════════════════════
    "automap_excluded":        [],
    "model_capability_overrides": {},

    # ════════════════════════════════════════════════════════════════════════
    # N) SUBAGENT-LITE
    # ════════════════════════════════════════════════════════════════════════
    # SUBAGENT-LITE (2026-08-24, option A from the feasibility report): serial
    "subagent_lite_enabled":   True,
    "subagent_lite_model_ladder": ["lfm3:1.7b-qad", "minicpm5:2b-sharp",
                                   "qwen3.5:4b-mtp"],
    "subagent_lite_ctx_default": 8192,
    "subagent_lite_min_free_ram_gb": 5.0,
    "subagent_lite_max_tools": 12,
    "subagent_lite_max_tokens": 700,
    "subagent_lite_timeout_s": 120,
    "subagent_lite_cooldown_s": 60,
    "subagent_lite_safety_margin_mib": 256,

    # ════════════════════════════════════════════════════════════════════════
    # O) SOUL & LEARNING
    # ════════════════════════════════════════════════════════════════════════
    # DEFAULT OFF: the distiller spends one extra LLM call after a run. It is
    # useful, but a fresh install should not grow background model work
    # nobody asked for - switch it on in the SOUL tab.
    "soul_skill_distillation": False,
    "soul_skill_writing":      False,
    "learning_preset_mode":    False,

    # ════════════════════════════════════════════════════════════════════════
    # P) ASK-USER (Pause + Resume)
    # ════════════════════════════════════════════════════════════════════════
    "ask_user_timeout_until_finished_seconds": 300,
    # ASK-USER-AUTO-PROCEED (W2-3, 2026-11-07): real engine setting. When on,
    # the ENGINE auto-answers an open ask_user question after 30 s
    # (AUTO_PROCEED_TIMEOUT_S, infra/ask_user_governor.py) - the safety net
    # behind the per-card "Auto 30s" timer. until_finished runs keep their
    # own ask_user_timeout_until_finished_seconds (that configured timeout
    # wins over the fixed 30 s). Read at question time, so toggling applies
    # mid-run without a restart.
    "ask_user_auto_proceed":     False,
    "ask_user_max_per_10min":  5,
    "ask_user_auto_answer":    "Use best judgment, document decision in commit message.",
    "ask_user_throttle_pause_message": "Agent is asking too many questions \u2014 manual help required. Check the agent status and Resume with clarification.",

    # ════════════════════════════════════════════════════════════════════════
    # P2) ACTION APPROVAL (user confirms code/cmd execution) + ERROR CAP
    # ════════════════════════════════════════════════════════════════════════
    # When on, the run pauses before everything that changes the project —
    # code/cmd execution (run_bash, run_python, install_package,
    # start_background, run_tests), file writes (write_file, edit_file,
    # write_file_append), git_commit and dynamic MCP tools. "Approve" buttons:
    # once, or remember EXACTLY this call for the CURRENT CHAT (writes per
    # file, commands 1:1 per exact arguments — anything new or a new chat asks
    # again). The toggle applies mid-run and is the only approval control:
    # with it off no card is forced, even after untrusted web/MCP content
    # (those results are still marked in the transcript). RISK-NOTE
    # (2026-10-21, audit): False = write/exec tools run WITHOUT
    # approval out of the box. Mitigations ship (destructive gate,
    # workspace confinement, loopback-only bind + external-bind token
    # guard) - keep this False only on machines where that layer is
    # enough, and flip to True for exposed or shared setups.
    "duo_action_approval_enabled": False,
    # Auto-DENY an unanswered approval card after N seconds (0 = off,
    # attended runs wait as before). With N > 0 and nobody at the desk the
    # gated call is DENIED once (fail closed, 2026-10-03 review: the old
    # auto-approve-once behavior approved unseen actions) - the gate wait
    # ends immediately, the model sees a timeout note and picks a
    # different approach. The NEXT late user decision is discarded once,
    # so a stale click can never approve the following gated call.
    "duo_action_approval_timeout_s": 0,
    # EVICT-ON-PAUSE (2026-11-02): while a run waits for an ask_user answer
    # the coder sits in VRAM untouched. On a small card that blocks every
    # other model load; this releases it until the run resumes. Off by
    # default - the reload costs a few seconds. Approval cards keep the model
    # loaded: that answer usually arrives within seconds.
    "duo_evict_on_pause":        False,

    # Duo image plan (2026-10-01, replaces duo_coder_raw_images): how the
    # agentic duo run handles attached images. None = derived (active
    # vision_model.json -> "preprocess", else "direct"); explicit
    # "direct"/"preprocess" wins. duo_image_to_planner/_coder are the
    # per-role switches for the raw path. resolve_image_plan() decides per
    # role: raw | description | none. Presets may carry None back in — the
    # derivation is deterministic, so a preset load resets an explicit mode
    # to "derived" (documented, tested).
    "duo_image_mode": None,
    "duo_image_to_planner": False,
    "duo_image_to_coder": False,
    # Explicit toggle (2026-10-07): hard "the coder takes NO images" — plan
    # says none for the coder, the coder slot never loads the projector,
    # and image-carrying steers to the coder are dropped. Saves the mmproj
    # VRAM even when an image arrives in the run.
    "duo_coder_images_off": False,
    # False = uploads live only as base64 inside the run (nothing on disk);
    # True = kept under <workspace>/.hive_uploads and referenced by path.
    "image_uploads_persistent": False,
    # Tool-error hard-stop cap for agentic runs. Successful non-write calls
    # decay the counter by 1, successful writes reset it to 0 — only a
    # no-progress run (this many fails with nothing succeeded in between)
    # is stopped.
    "duo_max_tool_errors": 12,

    # ════════════════════════════════════════════════════════════════════════
    # P) TELEGRAM GATEWAY
    # ════════════════════════════════════════════════════════════════════════
    # Gateway-owned keys (phone UI and /gate write them).
    # telegram_gateway_enabled is deliberately NOT defaulted: the gateway
    # treats "key absent" as "not configured, the toml master switch decides"
    # and "key present and false" as a veto. A shipped False would shut down
    # every toml-only gateway on the next start.
    "telegram_mirror_enabled":    False,
    "telegram_approval_mode":     "deny",
    "telegram_phone_restricted":  True,

    # ════════════════════════════════════════════════════════════════════════
    # Q) MCP & AGENT-SUB-KONFIGURATIONEN
    # ════════════════════════════════════════════════════════════════════════
    #   {"name": "...", "command": "npx", "args": ["-y", "@playwright/mcp@latest"]}   (stdio)
    #   {"name": "...", "url": "http://localhost:9000/mcp"}                            (streamable HTTP)
    # as additional tools.
    # External MCP servers (client direction): every entry is a stdio or
    # streamable-HTTP server; infra/mcp_client.py reads this list, and
    # docs/mcp_client.md documents the shape.
    "mcp_servers":             [],
    # MCP client (beta): use external MCP servers over stdio. Off by default;
    # phone runs stay blocked by the phone restriction gate.
    "mcp_client_enabled":      False,
    "mcp_client_timeout_s":    30,
    "mcp_client_max_calls_per_run": 40,

    # ════════════════════════════════════════════════════════════════════════
    # Q2) LANGUAGE SERVERS (LSP, 2026-10-31)
    # ════════════════════════════════════════════════════════════════════════
    # lsp_enabled is the ONLY switch - there are deliberately no per-language
    # on/off flags. True = the lsp_* tools exist; a language server process
    # still starts LAZILY on the first tool call, never at boot. A language
    # without a found server command answers LSP_UNAVAILABLE (with an
    # install hint) instead of silently degrading; lsp_status lists what is
    # installed without starting anything.
    "lsp_enabled":             True,
    # Optional command override per language id, read by the LSP client:
    #   {"python": "pyright-langserver --stdio", "rust": ["rust-analyzer"]}
    # A configured command REPLACES the default candidates for that language
    # and must resolve (otherwise the language reports as missing - no
    # silent fallback to a different server).
    "lsp_servers":             {},
    # LSP-SECURITY (2026-11-03): repo .vscode/settings.json can select the
    # command a server runs; forward it only in trusted workspaces.
    "lsp_forward_workspace_settings": False,
    # Per-request timeout in seconds for one LSP request; also caps the
    # initialize handshake. A hung server is killed and lazily restarted on
    # the next call.
    "lsp_client_timeout_s":    30,
    # How long lsp_diagnostics waits for the server's diagnostics
    # publication after open/change before returning what it has.
    "lsp_diagnostics_wait_s":  5.0,
    # Attach a compact diagnostics block to a successful write/edit. A warm
    # server is used directly; a cold one is started in the background
    # (lsp_auto_warm_on_write) and the write waits at most 3 s for it.
    # Failures are silent - a broken/missing server never fails a write.
    "lsp_auto_diagnostics":    True,
    "lsp_auto_warm_on_write":  True,
    # Output caps for the lsp_* tools (results per call).
    "lsp_max_diagnostics":     50,
    "lsp_max_results":         50,   # cap for locations/symbols/actions lists
    # Close a language server after this many idle seconds (0 = off). The
    # next lsp_* call restarts it lazily; keeps commit/RAM down in long
    # sessions where a server is used once and never again.
    "lsp_idle_timeout_s":      600,
    # Per-server MCP client call budgets: {"server-name": max calls per
    # rolling hour}. Absent/0 = unlimited (previous behavior).
    "mcp_server_budgets":      {},
    # [MEM] commit-charge snapshots at startup/load/evict/run-end.
    "mem_watch_enabled":       True,

    "soul_evolve_agent": {
        "enabled":     False,
        "model": "",
        "temperature": 0.65,
        "max_tokens":  800,
    },
    "intent_agent": {
        "enabled":     False,
        "model": "",
        "temperature": 0.1,
        "max_tokens":  400,
    },
    "exploration_agent": {
        "enabled": True,
        "model": "",
        "workers": [
            {"model": "", "ctx": 8192},
            {"model": "", "ctx": 8192},
        ],
    },
    "ctx_overrides": {
        "default": None,
        "roles": {},
        "models": {},
    },
}

