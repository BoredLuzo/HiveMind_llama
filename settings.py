# HiveMind — Local Multi-Agent AI Coding Assistant
# Author: Luzo (BoredLuzo) — https://github.com/BoredLuzo


from __future__ import annotations
import copy
import json
import threading
from pathlib import Path

SETTINGS_FILE      = Path(__file__).parent / "settings.json"
PRESETS_FILE       = Path(__file__).parent / "presets.json"
CUSTOM_PROMPTS_DIR = Path(__file__).parent / "custom_prompts"

# ── Defaults ─────────────────────────────────────────────────────────────────
# Recommended models (setup_models.bat / fetch_models.py) as defaults — they
# are the models the installer downloads. Presets are NOT shipped
# (presets.json is empty); the user creates their own presets.
# CODER-TEMP (2026-08-31): higher than for Refiner/Critic — creative coding.

DEFAULT_AGENT_CFG = {
    "analyst":     {"model": "qwen3.5:4b-ud", "temperature": 0.3, "max_tokens": 1100, "thinking": False, "thinking_budget": 0},
    "refiner":     {"model": "qwen3.5:2b",    "temperature": 0.3, "max_tokens": 400, "thinking": False, "thinking_budget": 0},
    "critic":      {"model": "qwen3.5:4b-ud", "temperature": 0.2, "max_tokens": 600, "thinking": False, "thinking_budget": 0},
    "synthesizer": {"model": "qwen3.5:4b-ud", "temperature": 0.2, "max_tokens": 900, "thinking": False, "thinking_budget": 0},
    "direct":      {"model": "lfm2.5:2.6b",   "temperature": 0.4, "max_tokens": 600, "thinking": False, "thinking_budget": 0},
    "judge":       {"model": "lfm2.5:2.6b",   "temperature": 0.1, "max_tokens": 120, "thinking": False, "thinking_budget": 0},
    "duo_coder":   {"model": "lfm2.5:2.6b",   "temperature": 0.8, "max_tokens": 12000, "thinking": False, "thinking_budget": 0},
    "duo_critic":  {"model": "qwen3.5:4b-ud", "temperature": 0.15, "max_tokens": 600, "thinking": False, "thinking_budget": 0},
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
    # 0 = aus, 256 = llama.cpp-Empfehlung.
    "llama_cache_reuse":       256,

    # ════════════════════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════════════════════
    "duo_pair":                "focused",
    "duo_tool_rounds":         0,
    "duo_use_pipeline":        False,
    "duo_critic_tools":        False,
    "duo_chunking":            True,
    "duo_coding_mode":         True,
    "duo_pre_explore":         False,
    "until_finished":          False,
    "duo_use_preset_models":   False,
    "duo_use_presets":         True,
    "duo_runtime_profile":     "balanced",
    "duo_runtime_profile_lock_override": False,
    "duo_profile_speed_model": "qwen3.5:4b-ud",
    "duo_profile_quality_model": "lfm2.5:2.6b",
    "duo_agentic_mode":        False,
    "duo_agentic_thinking":    False,
    "_thinking_before_chunking": None,  # persisted user-preference before chunking forced thinking ON
    # Explizit in DEFAULT_SETTINGS aufgedeckte Automatik-/Override-Keys
    # (vorher nur implizite Lese-Fallbacks in den Konsumenten).
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
    "duo_compress_every":      4,
    "session_compress_threshold": 20,

    # ════════════════════════════════════════════════════════════════════════
    # E) EXPLORE, REPO-MAP & MEMORY
    # ════════════════════════════════════════════════════════════════════════
    "duo_tree_scout_enabled":  True,
    "duo_tree_scout_max_depth": 4,
    "duo_tree_scout_max_files": 200,
    "duo_static_map_chars":    0,   # 0 = Tier-abgeleitet (rich: 8000); >0 = explizites Static-Repo-Map-Char-Budget
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
    "duo_planner_ctx_cap":     None,
    "duo_planner_ctx_target":  None,
    "duo_planner_use_coder_ctx": True,
    "duo_planner_ensure_load_timeout_s": 300,
    "duo_planner_thinking_timeout_s": 600,
    "duo_soft_planner_wall_timeout_s": 300,
    "duo_planner_ttl_seconds": 0,    # 0 = auto (450s), >0 = Override
    "duo_coder_ttl_seconds":   0,    # 0 = auto (420s), >0 = Override
    "plan_tracker_classifier": "heuristic",

    # ════════════════════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════════════════════
    "duo_coder_ctx_agentic":   None,
    "duo_coder_ctx_until_finished": None,
    "duo_coder_ctx_normal":    None,
    "duo_coder_tool_thinking": False,
    "duo_coder_tool_thinking_auto_mode": "on_fail",
    # am Output-Token-Limit abgeschnitten (finish_reason=length → DROPPED → Loop),
    # Token-Budget gekoppelt: max_chars ≈ budget_tokens * Faktor - Overhead.
    # Sprung; Deckel 3.3 (dokumentierter Realwert).
    "duo_write_chars_per_token": 2.5,
    "duo_coder_fallback_model": "qwen3.5:4b-ud",
    "duo_critic_ctx":          None,

    # ════════════════════════════════════════════════════════════════════════
    # H) CRITIC, TESTS & PEER-RATINGS
    # ════════════════════════════════════════════════════════════════════════
    "duo_peer_ratings_agentic": False,
    "duo_tool_autopromote_max_rounds": 4,
    "duo_distilled_executor":  False,
    "duo_test_feedback":       False,
    "duo_test_feedback_chunk": False,
    # TESTING-DEFAULT (2026-08-12): finaler Auto-Test standardmaessig aktiv —
    "duo_test_feedback_final": True,
    "duo_p3_max_fix_attempts": 3,

    # ════════════════════════════════════════════════════════════════════════
    # I) GIT, SANDBOX & SYSTEM
    # ════════════════════════════════════════════════════════════════════════
    "duo_git_autocommit":      False,
    "duo_git_checkpoints":     True,
    # run_python/background) via Windows Job Objects (KILL_ON_JOB_CLOSE) —
    "duo_tool_sandbox":        True,
    # Job resource limits (0 = unlimited) for run_bash/run_python/background.
    "duo_tool_sandbox_max_mem_mb": 4096,
    "duo_tool_sandbox_max_procs": 64,
    "read_guard_enabled":      True,
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

    # ════════════════════════════════════════════════════════════════════════
    # J) WEBSEARCH & SEARXNG
    # ════════════════════════════════════════════════════════════════════════
    "searxng_host":            "http://localhost:8888",
    "searxng_engines":         "brave,wikipedia,github",
    "searxng_language":        "all",
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
    "direct_tools_tier":       "readonly",   # off | readonly(websearch only) | python(read+python) | full
    "direct_tools_max_rounds": 12,

    # ════════════════════════════════════════════════════════════════════════
    # K) VISION
    # ════════════════════════════════════════════════════════════════════════
    "vision_agent_enabled":    False,
    "vision_agent_model":      "",
    "vision_agent_mode":       "sequential",
    "vision_preprocess_timeout_seconds": 30,
    "vision_preprocess_load_timeout_seconds": 120,
    # PIPELINE-VISION (2026-08-19): Bilder direkt an multimodale Pipeline-Agenten?
    "pipeline_vision_direct":  False,
    # auf pipeline_vision_direct (→ {"analyst": True}).
    "pipeline_vision_roles":   {},
    "image_desc_full_pipeline": False,

    # ════════════════════════════════════════════════════════════════════════
    # L) PRELOAD & PREFETCH
    # ════════════════════════════════════════════════════════════════════════
    "smart_preload_enabled":   True,
    "startup_preload_enabled": False,
    "startup_preload_analyst": False,
    "startup_preload_judge_in_agentic": False,
    "startup_preload_coder":   False,
    "judge_prefetch_before_complexity": True,
    "prefetch_agent_avgs":     {},
    "prefetch_lead_seconds":   8.0,
    "preload_workers_after_run": False,

    # ════════════════════════════════════════════════════════════════════════
    # M) AUTOMAP / ROUTING
    # ════════════════════════════════════════════════════════════════════════
    "automap_mode":            "conservative",
    "automap_excluded":        [],
    "automap_code_duo_enabled": False,
    "automap_duo_pre_explore": False,
    "automap_duo_parallel_preexplore": False,
    "automap_pipeline_websearch_enabled": False,
    "model_capability_overrides": {},

    # ════════════════════════════════════════════════════════════════════════
    # N) SUBAGENT-LITE
    # ════════════════════════════════════════════════════════════════════════
    # SUBAGENT-LITE (2026-08-24, Option A aus Feasibility-Report): serielles
    "subagent_lite_enabled":   True,
    "subagent_lite_model_ladder": ["lfm2.5:2.6b", "qwen3.5:0.8b-ud"],
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
    # Q) MCP & AGENT-SUB-KONFIGURATIONEN
    # ════════════════════════════════════════════════════════════════════════
    #   {"name": "...", "command": "npx", "args": ["-y", "@playwright/mcp@latest"]}   (stdio)
    #   {"name": "...", "url": "http://localhost:9000/mcp"}                            (streamable HTTP)
    # als zusaetzliche Tools angeboten.
    "mcp_servers":             [],
    "soul_evolve_agent": {
        "enabled":     False,
        "model":       "gemma-4:e4b-it-obliterated",
        "temperature": 0.65,
        "max_tokens":  800,
    },
    "intent_agent": {
        "enabled":     False,
        "model":       "qwen3.5:4b",
        "temperature": 0.1,
        "max_tokens":  400,
    },
    "exploration_agent": {
        "enabled": True,
        "model":   "qwen3.5:4b-ud",
        "workers": [
            {"model": "qwen3.5:4b-ud", "ctx": 8192},
            {"model": "qwen3.5:4b-ud", "ctx": 8192},
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
            # MIGRATION: duo_test_feedback → neue Split-Toggles (chunk/final)
            _legacy_tf = data.get("duo_test_feedback") if "duo_test_feedback" in data else None
            if "duo_test_feedback_chunk" not in data and _legacy_tf is not None:
                data["duo_test_feedback_chunk"] = bool(_legacy_tf)
            if "duo_test_feedback_final" not in data and _legacy_tf is not None:
                data["duo_test_feedback_final"] = bool(_legacy_tf)
            data["duo_planner_model"] = None
            # MIGRATION: moe_cpu_experts int (globaler Override) -> dict (per-Model)
            if not isinstance(data.get("moe_cpu_experts"), dict):
                data["moe_cpu_experts"] = {}
            for k, v in DEFAULT_SETTINGS.items():
                data.setdefault(k, v)
            # Fix: Einzelne Agent-Keys aus DEFAULT_AGENT_CFG mergen.
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

def save_settings(settings: dict):


    _data = json.dumps(settings, ensure_ascii=False, indent=2)
    _tmp = SETTINGS_FILE.with_name(f".settings_{threading.get_ident()}.tmp")
    _tmp.write_text(_data, encoding="utf-8")
    with _settings_write_lock:
        _tmp.replace(SETTINGS_FILE)                  # atomar, OS-Garantie

def load_presets() -> dict:
    if PRESETS_FILE.exists():
        try:
            return json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(DEFAULT_PRESETS)

_presets_write_lock = threading.Lock()

def save_presets(presets: dict):
    """Analog zu save_settings: atomic write via tmp + replace."""
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
