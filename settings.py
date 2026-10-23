# HiveMind — Local Multi-Agent AI Coding Assistant


from __future__ import annotations
import copy
import json
import threading
from datetime import datetime as _dt
from pathlib import Path

SETTINGS_FILE      = Path(__file__).parent / "settings.json"
PRESETS_FILE       = Path(__file__).parent / "presets.json"
CUSTOM_PROMPTS_DIR = Path(__file__).parent / "custom_prompts"

# ── Defaults ─────────────────────────────────────────────────────────────────
# Recommended models (setup_models.bat / fetch_models.py) as defaults — they
# are the models the installer downloads.
# CODER-TEMP (2026-08-31): higher than for Refiner/Critic — creative coding.

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
    "max_iterations":          2,
    "mode":                    "simple",
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
    "max_concurrent_models":   None,
    "allow_cpu_offload":       True,
    "warn_on_cpu_offload":     False,
    "max_model_size_gb":       None,
    "prefer_smaller_models":   False,
    "pin_direct_after_response": False,
    "judge_keepalive_enabled": False,
    "duo_worker_slots":        2,
    "moe_cpu_experts":         {},
    "llama_mlock":             True,
    "gpu_backend":             "",
    # 0 = off, 256 = llama.cpp recommendation.
    "llama_cache_reuse":       256,
    # Prompt-processing chunk size (llama.cpp --ubatch-size). Larger values
    # (512/1024) speed up prefill substantially for MoE models with CPU expert
    # offloading (fewer RAM-streaming passes over the expert weights), at the
    # cost of a larger VRAM compute buffer. Test lever for slow post-compression
    # prefill; revert to 256 if a load OOMs on tight GPUs.
    "llama_ubatch_size":       256,
    # VRAM pre-flight gate (2026-10-11): when True (default), loads estimated
    # not to fit are refused before llama-server starts (estimate can be
    # wrong for foreign models - turning this OFF makes the engine attempt
    # the load and halve ctx on real failures instead).
    "vram_preflight_gate":     True,

    # ════════════════════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════════════════════
    "duo_tool_rounds":         0,
    "duo_use_pipeline":        False,
    "duo_critic_tools":        False,
    "duo_chunking":            True,
    "duo_coding_mode":         True,
    "duo_pre_explore":         False,
    # UI state only: remembers the Duo "pair" selector position. The same
    # request also sets agents.duo_coder.model, which IS what the engine reads.
    "duo_pair":                "focused",
    "until_finished":          False,
    "duo_runtime_profile":     "balanced",
    "duo_profile_speed_model": "",
    "duo_profile_quality_model": "",
    "duo_agentic_mode":        False,
    # ── ROUTING (2026-10-20): zwei unabhaengige Entscheider, beide schaltbar ──
    # Der LLM-Judge kostet einen Modell-Load in den VRAM (60 s + Verdraengung)
    # und liefert Text, der geparst werden muss. Der Decider ist ein Encoder
    # (GLiNER2.5, CPU, ~0.5-1.2 s, kein VRAM) und optional installiert
    # (pip install hivemind[decider]). Jeder der beiden laesst sich einzeln
    # abschalten; "shadow" rechnet mit und loggt, entscheidet aber nicht.
    #   off | shadow | on
    "routing_decider":          "off",
    "routing_judge":            "on",
    # Wer entscheidet, ob ein Planner gebraucht wird?
    #   auto    = Decider, wenn er laeuft, sonst Judge (spart den GPU-Hit)
    #   decider | judge = fest
    #   always | never  = Planner immer / nie
    "routing_planner":          "auto",
    "routing_decider_model":    "fastino/GLiNER2.5-Decide",
    # Unter dieser Konfidenz wird die Entscheidung als unsicher markiert (und geht
    # an den Judge, falls aktiv). Kalibriert auf die Multi-Label-Aktions-Labels:
    # Treffer lagen bei 0,49-0,92 (Median 0,72) - 0,85 haette fast alles eskaliert.
    "routing_decider_threshold": 0.55,
    "duo_agentic_thinking":    False,
    "_thinking_before_chunking": None,  # persisted user-preference before chunking forced thinking ON
    # Explizit in DEFAULT_SETTINGS aufgedeckte Automatik-/Override-Keys
    # (previously only implicit read fallbacks in the consumers).
    "duo_coder_model":          "",
    "duo_critic_model":         "",
    "duo_caps":                 {},
    "duo_autolint_python_engine": "auto",
    "duo_pyright_path":         "",

    # ════════════════════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════════════════════
    "duo_run_timeout_seconds": 420,
    "duo_run_timeout_critical_seconds": 900,
    "duo_until_finished_cap":  999999,
    "duo_rounds_balanced_cap": 3,
    "duo_read_timeout":        390,
    "duo_run_bash_build_timeout_s": 600,
    "duo_llm_slow_timeout_s":  300,
    "duo_max_tool_rounds_runtime_cap": 300,
    "duo_max_tool_rounds":     64,
    "duo_tool_output_ttl":     3,
    "duo_compress_threshold":  0,
    # CACHE-FRIENDLY (2026-09-04): protect the llama.cpp prefix cache.
    # LEGACY-REMOVAL (2026-09-09): duo_cache_friendly_ctx entfernt —
    # Compression-first is the only regime.
    # - duo_partial_compression: condense only the old part, keep the raw tail
    #   byte-identisch am Ende behalten (KV-Shift-Reuse). Recommended ON:
    #   preserves the llama.cpp prefix cache and the raw tail stays readable
    #   for the coder; telemetry phase is done (2026-09-09).
    # - duo_compress_threshold > 0 = exaktes Override in absoluten Tokens
    #   (settings.json / Power-User, kein UI-Feld);
    #   0 = auto: Schwelle = duo_compress_auto_floor*ctx (dynamische
    #   output reserve clamps every tool round so prompt+output <= ctx holds,
    #   daher kein statischer max_tokens-Abzug noetig).
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
    "duo_static_map_chars":    0,   # 0 = Tier-abgeleitet (rich: 8000); >0 = explizites Static-Repo-Map-Char-Budget
    # REPO-MAP-PIN (2026-09-06): put "## Static Repo-Map" into the system message
    # (byte-stabil = cache-stabil ueber Kompressionen). Deltas/TODO-Konsolidierung
    # siehe core/repomap_pin.py. Baseline (Live 2026-09-06, PacMan/8 Dateien):
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
    # PLANNER-OUTPUT-BUDGET (0.99.2, User-Entscheidung): sichtbares Aufgabenbudget
    "duo_planner_max_tokens":  8000,
    # Thinking → hermes-MoE thought until the 600s wall timeout and delivered empty
    # output. 4k was too tight for complex plans → 8k default (2026-08-31).
    "duo_planner_thinking_budget": 8000,
    # CODER-BRIEFING-STAGE (2026-10-17, owner): the planner's thinking is only
    # useful as a briefing while it is SMALL. A planner that thinks 8-16k tokens
    # before writing a plan poisons the coder prompt (and the coder's window), so
    # above this many ESTIMATED tokens only the pure plan is injected. 0 = never
    # inject thinking (always plan-only). -1 = always inject (old behaviour).
    "duo_coder_briefing_think_tokens": 1500,
    "duo_planner_ctx_cap":     None,
    "duo_planner_ctx_target":  None,
    "duo_planner_use_coder_ctx": True,
    "duo_planner_ensure_load_timeout_s": 300,
    "duo_planner_thinking_timeout_s": 600,
    "duo_soft_planner_wall_timeout_s": 300,
    "duo_planner_ttl_seconds": 0,    # 0 = auto (450s), >0 = Override
    "duo_coder_ttl_seconds":   0,    # 0 = auto (420s), >0 = Override
    # CODER LIFETIME (2026-10-17): keep = the slot stays warm after the
    # run (idle TTL decides), evict = release it at the run end so the next
    # prompt can load any model/mode. Owner request.
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
    # truncated at the output-token limit (finish_reason=length → DROPPED → loop),
    # Token-Budget gekoppelt: max_chars ≈ budget_tokens * Faktor - Overhead.
    # Sprung; Deckel 3.3 (dokumentierter Realwert).
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
    # TESTING-DEFAULT (2026-08-12): finaler Auto-Test standardmaessig aktiv —
    "duo_test_feedback_final": True,
    "duo_p3_max_fix_attempts": 3,
    # ERROR ROLLUP (2026-09-06): deterministic dedup of repeating test/compile
    # failures in the fix-loop (core/error_rollup.py). First failure full,
    # repeats rendered as PERSISTS/FIXED/REOPENED. Runtime state per run.
    "duo_error_rollup":        True,

    # ════════════════════════════════════════════════════════════════════════
    # I) GIT, SANDBOX & SYSTEM
    # ════════════════════════════════════════════════════════════════════════
    "duo_git_autocommit":      False,
    # OFF (2026-10-18): a checkpoint is a real git commit in the user's repo.
    # Opt-in; a bad write is still covered by undo_last + the write transaction.
    "duo_git_checkpoints":     False,
    # run_python/background) via Windows Job Objects (KILL_ON_JOB_CLOSE) —
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
    # validated in live/test — flip duo_wedge_handoff_enabled to true there.
    "duo_wedge_handoff_enabled":         False,
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
    # DESKTOP-NOTIFICATIONS (2026-08-27, User-Wunsch): Windows-Toasts via
    "desktop_notifications":   True,
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
    # Telegram gateway voice (per bot, opt-in). The gateway reads these from the
    # engine, so the UI switches actually drive it; the greying in the UI follows
    # stt_enabled / tts_enabled (a voice note needs STT, a voice answer needs TTS).
    # Read-aloud (frontend): what the UI speaks on its own when TTS is on.
    # Direct = the whole answer; agentic = the plan first, then the coder's
    # answer; ask/gate read the interactive cards. All default off.
    "tts_read_direct":           False,
    "tts_read_agentic":          False,
    "tts_read_ask":              False,
    "tts_read_gate":             False,
    "telegram_voice_in":         False,
    "telegram_voice_out":        False,
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
    "direct_tools_tier":       "readonly",   # off | readonly(websearch only) | python(read+python) | full
    "direct_tools_max_rounds": 12,

    # ════════════════════════════════════════════════════════════════════════
    # K) VISION
    # ════════════════════════════════════════════════════════════════════════
    "vision_agent_enabled":    False,
    "vision_agent_model":      "",
    # vision_agent_mode removed (2026-10-03): no backend reader, the runner
    # is sequential-only. It is also in _PRESET_NEVER_KEYS so stale values
    # in old settings.json/presets can never load back.
    "vision_preprocess_timeout_seconds": 30,
    "vision_preprocess_load_timeout_seconds": 120,
    # PIPELINE-VISION (2026-08-19): feed images directly to multimodal pipeline agents?
    "pipeline_vision_direct":  False,
    # on pipeline_vision_direct (→ {"analyst": True}).
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
    "subagent_lite_model_ladder": [],
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
    "soul_skill_distillation": True,
    "soul_skill_writing":      False,
    "learning_preset_mode":    False,

    # ════════════════════════════════════════════════════════════════════════
    # P) ASK-USER (Pause + Resume)
    # ════════════════════════════════════════════════════════════════════════
    "ask_user_timeout_until_finished_seconds": 300,
    "ask_user_max_per_10min":  5,
    "ask_user_auto_answer":    "Use best judgment, document decision in commit message.",
    "ask_user_throttle_pause_message": "Agent is asking too many questions \u2014 manual help required. Check the agent status and Resume with clarification.",

    # ════════════════════════════════════════════════════════════════════════
    # P2) ACTION APPROVAL (user confirms code/cmd execution) + ERROR CAP
    # ════════════════════════════════════════════════════════════════════════
    # When on, the run pauses before everything that changes the project —
    # code/cmd execution (run_bash, run_python, install_package,
    # start_background), file writes (write_file, edit_file,
    # write_file_append) and git_commit. "Approve" buttons: once, or
    # remember EXACTLY this call for the CURRENT CHAT (writes per file,
    # commands 1:1 per exact arguments — anything new or a new chat asks
    # again). The toggle applies mid-run.
    # RISK-NOTE (2026-10-21, audit): False = write/exec tools run WITHOUT
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
    # Owner toggle (2026-10-07): hard "the coder takes NO images" — plan
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
    # Q) MCP & AGENT-SUB-KONFIGURATIONEN
    # ════════════════════════════════════════════════════════════════════════
    #   {"name": "...", "command": "npx", "args": ["-y", "@playwright/mcp@latest"]}   (stdio)
    #   {"name": "...", "url": "http://localhost:9000/mcp"}                            (streamable HTTP)
    # as additional tools.
    # RESERVED (2026-10-21): config for MCP *clients* HiveMind may call
    # (e.g. an IDE exposing editor context). Nothing reads it yet; kept so
    # preset snapshots stay stable until the client lands.
    "mcp_servers":             [],
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

DEFAULT_PRESETS = {}

# ── Load / Save ───────────────────────────────────────────────────────────────

_load_cache_key: tuple | None = None
_load_cache_data: dict | None = None


def load_settings() -> dict:
    """Cached based on mtime.

    Avoids per-call disk read + JSON parse + ~60-line migration
    (which e.g. used to run per run_bash/run_python spawn via tools/sandbox.enabled()
    and is executed there). Each reader gets a flat copy so that
    mutations of the returned dict do not corrupt the cache. save_settings()
    changes the file
    """
    global _load_cache_key, _load_cache_data
    _key = None
    try:
        _st = SETTINGS_FILE.stat()
        _key = (_st.st_mtime_ns, _st.st_size)
    except Exception:
        _key = None
    if _key is not None and _key == _load_cache_key and _load_cache_data is not None:
        return copy.deepcopy(_load_cache_data)
    data = _load_settings_from_disk()
    if _key is not None:
        _load_cache_key = _key
        _load_cache_data = copy.deepcopy(data)
    return data


def _load_settings_from_disk() -> dict:
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if "image_forces_pipeline" in data and "image_desc_full_pipeline" not in data:
                data["image_desc_full_pipeline"] = data.pop("image_forces_pipeline")
            data.pop("duo_important_task_default", None)
            for _dead_key in ("duo_agentic_post_review_enabled",
                              "duo_agentic_post_review_max_tokens",
                              "duo_agentic_post_review_context_injection"):
                data.pop(_dead_key, None)
            # MIGRATION (presets re-enabled 2026-09-02): `active_preset` persists
            # so the UI can highlight the loaded preset; the removed runtime
            # override keys (duo presets/runtime-profile) stay stripped.
            for _dead_preset_key in ("duo_use_presets",
                                     "duo_use_preset_models",
                                     "duo_runtime_profile_lock_override"):
                data.pop(_dead_preset_key, None)
            # MIGRATION (0.99.2): duo_planner_content_tokens -> duo_planner_max_tokens.
            _old_cap = data.get("duo_planner_content_tokens")
            if "duo_planner_max_tokens" not in data:
                if _old_cap is not None:
                    try:
                        _old_cap_i = int(_old_cap)
                    except (TypeError, ValueError):
                        _old_cap_i = 0
                    data["duo_planner_max_tokens"] = _old_cap_i if _old_cap_i > 0 else DEFAULT_SETTINGS["duo_planner_max_tokens"]
                else:
                    data["duo_planner_max_tokens"] = DEFAULT_SETTINGS["duo_planner_max_tokens"]
            data.pop("duo_planner_content_tokens", None)
            _xa = data.get("exploration_agent")
            if isinstance(_xa, dict) and _xa.get("enabled") and not (_xa.get("model") or "").strip():
                _xa["model"] = DEFAULT_SETTINGS["exploration_agent"]["model"]
            # if data.get("disable_thinking_in_planner") is True:
            #     data["disable_thinking_in_planner"] = False
            _tbc = data.get("_thinking_before_chunking")
            if isinstance(_tbc, bool) and data.get("duo_chunking"):
                data["duo_agentic_thinking"] = _tbc
            if data.get("duo_parallel_preexplore") == "auto":
                data["duo_parallel_preexplore"] = True
            for _ws_key in ("searxng_engines", "searxng_language"):
                _ws_val = data.get(_ws_key)
                if _ws_val is not None and not str(_ws_val).strip():
                    data[_ws_key] = None
            data.pop("pre_explore_parallel", None)
    # MIGRATION: duo_test_feedback → new split toggles (chunk/final)
            _legacy_tf = data.get("duo_test_feedback") if "duo_test_feedback" in data else None
            if "duo_test_feedback_chunk" not in data and _legacy_tf is not None:
                data["duo_test_feedback_chunk"] = bool(_legacy_tf)
            if "duo_test_feedback_final" not in data and _legacy_tf is not None:
                data["duo_test_feedback_final"] = bool(_legacy_tf)
            # duo_planner_model persists across restarts. (The forced None here
            # wiped a user/preset-selected planner model on every reload —
            # live: preset "1" Loaded hermes as planner model, after a restart
            # the planner silently fell back to the coder model.)
            if "duo_planner_model" not in data:
                data["duo_planner_model"] = None
            # MIGRATION: moe_cpu_experts int (globaler Override) -> dict (per-Model)
            if not isinstance(data.get("moe_cpu_experts"), dict):
                data["moe_cpu_experts"] = {}
            for k, v in DEFAULT_SETTINGS.items():
                data.setdefault(k, v)
    # Fix: merge individual agent keys from DEFAULT_AGENT_CFG.
            _sa = data.setdefault("agents", {})
            for _ak, _av in DEFAULT_AGENT_CFG.items():
                _sa.setdefault(_ak, _av)
            for _ak, _av in DEFAULT_AGENT_CFG.items():
                if _ak in _sa and isinstance(_sa[_ak], dict):
                    _sa[_ak].setdefault("model", _av.get("model", ""))
                    _sa[_ak].setdefault("temperature", _av.get("temperature", 0.3))
                    _sa[_ak].setdefault("max_tokens", _av.get("max_tokens", 400))
                    _sa[_ak].setdefault("thinking", _av.get("thinking", False))
                    _sa[_ak].setdefault("thinking_budget", _av.get("thinking_budget", 0))
            return data
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)

_settings_write_lock = threading.Lock()


def _now_iso() -> str:
    return _dt.now().astimezone().isoformat(timespec="seconds")


def save_settings(settings: dict, *, saved_at: str | None = None):
    """Persist the settings dict atomically.

    saved_at: explicit stamp for callers that must keep two keys in lockstep
    (the preset load writes preset_applied_at == settings_saved_at, so the
    autoload guard sees "applied at the latest save", not "one write older").
    """


    settings["settings_saved_at"] = saved_at or _now_iso()
    _data = json.dumps(settings, ensure_ascii=False, indent=2)
    _tmp = SETTINGS_FILE.with_name(f".settings_{threading.get_ident()}.tmp")
    _tmp.write_text(_data, encoding="utf-8")
    with _settings_write_lock:
        _tmp.replace(SETTINGS_FILE)                  # atomar, OS-Garantie


# ── Presets (user config snapshots) ──────────────────────────────────────────
def load_presets() -> dict:
    if PRESETS_FILE.exists():
        try:
            # utf-8-sig: tolerate a UTF-8 BOM. External Windows editors add one;
            # strict utf-8 json.loads then failed, load_presets() silently
            # returned {} and every preset load 404'd.
            return json.loads(PRESETS_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return dict(DEFAULT_PRESETS)


_presets_write_lock = threading.Lock()


def save_presets(presets: dict):
    _data = json.dumps(presets, ensure_ascii=False, indent=2)
    _tmp = PRESETS_FILE.with_name(f".presets_{threading.get_ident()}.tmp")
    _tmp.write_text(_data, encoding="utf-8")
    with _presets_write_lock:
        _tmp.replace(PRESETS_FILE)


def get_custom_prompt(preset_name: str, agent_key: str) -> str | None:
    path = CUSTOM_PROMPTS_DIR / f"{preset_name}_{agent_key}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return None


def save_custom_prompt(preset_name: str, agent_key: str, content: str):
    CUSTOM_PROMPTS_DIR.mkdir(exist_ok=True)
    path = CUSTOM_PROMPTS_DIR / f"{preset_name}_{agent_key}.txt"
    path.write_text(content, encoding="utf-8")


def delete_custom_prompts_for_preset(preset_name: str):
    if not CUSTOM_PROMPTS_DIR.exists():
        return
    for f in CUSTOM_PROMPTS_DIR.glob(f"{preset_name}_*.txt"):
        f.unlink()


def copy_prompts_to_preset(src_preset: str | None, dst_preset: str,
                            agent_keys: list[str], base_prompts: dict):
    for key in agent_keys:
        src_content = get_custom_prompt(src_preset, key) if src_preset else None
        content = src_content or base_prompts.get(key, "")
        if content:
            save_custom_prompt(dst_preset, key, content)
