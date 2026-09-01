import asyncio, logging, time
import httpx

logger = logging.getLogger("hivemind.duo")

from vram.loader import _bk_evict, _bk_load, _get_loaded_models_set
from tools.definitions import _get_inline_tools
from core.duo_helpers import DEFAULT_VRAM_BUDGET_GB, update_model_capability_overrides
from hive_functions.num_ctx_config import resolve_ctx


async def _phase_vram(ctx, state: dict):
    """Phase A: VRAM setup, model pinning, tool-thinking modes, duo_start emit."""
    _MAX_FOCUS_PATHS = 10
    _recent_focus_paths = []
    _MAX_TREE_DEPTH = 4
    _MAX_TREE_FILES = 200
    _avail = set(ctx.models_cache) if ctx.models_cache else set()
    _free_mdl = (ctx.settings.get("duo_coder_model")
                 or (ctx.settings.get("agents", {}).get("duo_coder", {}) or {}).get("model", "")
                 or "qwen3.5:4b")
    coder_mdl  = _free_mdl
    critic_mdl = (ctx.settings.get("duo_critic_model")
                  or (ctx.settings.get("agents", {}).get("duo_critic", {}) or {}).get("model", "")
                  or _free_mdl)
    exec_mdl   = _free_mdl
    _is_solo   = critic_mdl == _free_mdl
    _duo_runtime_profile = ctx.resolve_duo_runtime_profile(
        ctx.duo_config.runtime_profile,
        important_task=ctx.duo_config.important_task,
        until_finished=ctx.duo_config.until_finished,
        lock_override=ctx.duo_config.runtime_profile_lock_override,
    )
    _vram_budget_for_profile = ctx.vram_budget  # P1-2 FIX: use cached value
    _duo_rounds_cap = 5
    if _duo_runtime_profile == "fast":
        _duo_rounds_cap = 2
    elif _duo_runtime_profile == "balanced":
        _duo_rounds_cap = int(ctx.settings.get("duo_rounds_balanced_cap", 3))
    duo_rounds = min(max(int(ctx.iterations), 1), max(1, _duo_rounds_cap))
    _planner_step_cap = int(ctx.settings.get("duo_planner_max_steps", 0))
    if _planner_step_cap > 0:
        _planner_step_cap = max(3, min(100, _planner_step_cap))
    _duo_run_timeout_s = ctx.resolve_duo_run_timeout_seconds(_duo_runtime_profile)
    _duo_deadline_at = time.time() + _duo_run_timeout_s
    _duo_timed_out = False
    _ctx_peak_tokens = 0
    _ctx_limit_seen = 0
    _ctx_pressure_peak = 0.0
    _ctx_compressions = 0
    _ctx_evictions = 0
    _tool_round_durations: list[float] = []

    if ctx.duo_config.agentic_mode:
        if exec_mdl != coder_mdl:
            exec_mdl = coder_mdl

    update_model_capability_overrides(ctx.settings.get("model_capability_overrides", {}))
    _coder_tc  = ctx.model_profile(coder_mdl).get("tool_call", False)
    _critic_tc = ctx.model_profile(critic_mdl).get("tool_call", False)
    _exec_tc   = ctx.model_profile(exec_mdl).get("tool_call", False)

    _critic_tools_enabled = bool(ctx.settings.get("duo_critic_tools", False)) and _critic_tc

    _status_model_names = coder_mdl.split(':')[0] if ctx.duo_config.agentic_mode else f"{coder_mdl.split(':')[0]} + {critic_mdl.split(':')[0]}"
    yield await ctx.emit({"type": "status",
                      "content": f"⚡ Code: {_status_model_names} → VRAM"})

    _duo_pinned: set = set()
    _skip_coder_pin = False
    _budget_eff = ctx.vram_budget
    _coder_ctx = resolve_ctx(ctx.settings.get("duo_coder_ctx_agentic"), coder_mdl, "agentic") if ctx.duo_config.agentic_mode else resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "coder")
    _exec_ctx  = _coder_ctx
    _critic_ctx = resolve_ctx(ctx.settings.get("duo_critic_ctx") or ctx.settings.get("duo_coder_ctx_normal"), critic_mdl, "critic")
    try:
        from backend.llama_vram_table import vram_of_with_ctx as _vram_ctx_est
        if ctx.duo_config.agentic_mode:
            _duo_total  = max(_vram_ctx_est(coder_mdl, _coder_ctx), _vram_ctx_est(exec_mdl, _exec_ctx))
        else:
            _duo_total  = max(_vram_ctx_est(coder_mdl, _coder_ctx), _vram_ctx_est(exec_mdl, _exec_ctx)) + _vram_ctx_est(critic_mdl, _critic_ctx)
    except Exception:
        if ctx.duo_config.agentic_mode:
            _duo_total  = max(ctx.vram_lookup_gb.get(coder_mdl, 4.0), ctx.vram_lookup_gb.get(exec_mdl, 4.0))
        else:
            _duo_total  = max(ctx.vram_lookup_gb.get(coder_mdl, 4.0), ctx.vram_lookup_gb.get(exec_mdl, 4.0)) + ctx.vram_lookup_gb.get(critic_mdl, 4.0)
    _duo_over_budget = _duo_total > _budget_eff
    if _duo_over_budget:
        _vram_warn_names = coder_mdl.split(chr(58))[0] if ctx.duo_config.agentic_mode else f"{coder_mdl.split(chr(58))[0]}+{critic_mdl.split(chr(58))[0]}"
        yield await ctx.emit({"type": "status",
                          "content": f"⚠ VRAM warning: {_vram_warn_names} = {_duo_total:.1f}GB > budget {_budget_eff:.1f}GB (CPU overflow possible)"})
    try:
        from backend.llama_vram_table import vram_of_with_ctx as _vram_ctx_guard, VRAM_OVERFLOW_MODELS as _vram_overflow_models
        from backend.llama_vram_table import vram_of_moe as _vram_moe_guard, _MOE_TABLE as _moe_table_guard
        _coder_base = coder_mdl.rsplit("#", 1)[0] if "#" in coder_mdl else coder_mdl
        _critic_base = critic_mdl.rsplit("#", 1)[0] if "#" in critic_mdl else critic_mdl
        _coder_need = float(_vram_moe_guard(_coder_base, int(_coder_ctx)))
        if _coder_base in _vram_overflow_models or _coder_need > _budget_eff:
            yield await ctx.emit({
                "type": "status",
                "content": (
                    f"⛔ Coder ctx too large for VRAM: {_coder_ctx} (≈{_coder_need:.1f}GB > {_budget_eff:.1f}GB). "
                    "Please reduce ctx."
                ),
            })
            state["_duo_aborted"] = True
            return
        if not ctx.duo_config.agentic_mode:
            _critic_need = float(_vram_moe_guard(_critic_base, int(_critic_ctx)))
            if _critic_base in _vram_overflow_models or _critic_need > _budget_eff:
                yield await ctx.emit({
                    "type": "status",
                    "content": (
                        f"⛔ Critic ctx too large for VRAM: {_critic_ctx} (≈{_critic_need:.1f}GB > {_budget_eff:.1f}GB). "
                        "Please reduce ctx."
                    ),
                })
                state["_duo_aborted"] = True
                return
    except Exception:
        logger.warning("VRAM guard failed - on-demand fallback active", exc_info=True)
        pass
    try:

        _skip_early_evict = bool(ctx.duo_config.pre_explore and ctx.duo_config.agentic_mode)
        _evicted_count = 0
        if not _skip_early_evict:
            if ctx.duo_config.agentic_mode and _skip_coder_pin:
                _duo_model_set = set()
            else:
                _duo_model_set = {coder_mdl, critic_mdl}
            try:
                _loaded_pre = await _get_loaded_models_set(max_age=0.0)
                for _ev_m in _loaded_pre:
                    if _ev_m not in _duo_model_set:
                        try:
                            await _bk_evict(_ev_m)
                            _evicted_count += 1
                        except Exception:
                            pass
            except Exception:
                logger.warning("VRAM-Eviction failed", exc_info=True)
                pass

        if _evicted_count > 0:
            await asyncio.sleep(1.0)

        async with httpx.AsyncClient(timeout=60.0) as _pc:
            _judge_mdl = ctx.registry.get("judge", "")
            _judge_gb  = ctx.vram_lookup_gb.get(_judge_mdl, 2.1) if _judge_mdl else 0.0
            if _judge_mdl and (_duo_total + _judge_gb) > (_budget_eff - 0.5):
                try:
                    await _bk_evict(_judge_mdl)
                except Exception:
                    pass
            _skip_coder_pin = bool(ctx.duo_config.pre_explore and _exec_tc) or ctx.duo_config.agentic_mode
            if not _skip_coder_pin and (ctx.duo_config.chunking or ctx.duo_config.planner):
                _pre_pin_planner_mdl = exec_mdl if (
                    not bool(ctx.settings.get("disable_thinking_in_planner", False))
                    and (ctx.duo_config.agentic_thinking or (bool(ctx.settings.get("duo_planner_default_thinking", True)) and ctx.duo_config.coding_mode))
                ) or bool(ctx.settings.get("duo_planner_use_exec_model", True)) else coder_mdl
                _pre_pin_planner_gb = ctx.vram_lookup_gb.get(_pre_pin_planner_mdl, 4.0)
                try:
                    _pre_pin_coder_gb = float(_vram_ctx_est(exec_mdl if exec_mdl != coder_mdl else coder_mdl, int(_coder_ctx)))
                except Exception:
                    _pre_pin_coder_gb = ctx.vram_lookup_gb.get(exec_mdl, 2.5) if exec_mdl != coder_mdl else ctx.vram_lookup_gb.get(coder_mdl, 2.5)
                _pre_pin_critic_gb = 0.0 if ctx.duo_config.agentic_mode else ctx.vram_lookup_gb.get(critic_mdl, 2.0)
                try:
                    if not ctx.duo_config.agentic_mode:
                        _pre_pin_critic_gb = float(_vram_ctx_est(critic_mdl, int(_critic_ctx)))
                except Exception:
                    _pre_pin_critic_gb = 0.0 if ctx.duo_config.agentic_mode else ctx.vram_lookup_gb.get(critic_mdl, 2.0)
                _raw_b = ctx.settings.get("vram_budget_gb")
                _pre_pin_budget = float(_raw_b) if _raw_b is not None else DEFAULT_VRAM_BUDGET_GB
                if _pre_pin_planner_mdl != coder_mdl and (_pre_pin_coder_gb + _pre_pin_critic_gb + _pre_pin_planner_gb) > _pre_pin_budget:
                    _skip_coder_pin = True
                    logger.info("[VRAM-SMART-PIN] Coder pin skipped - planner (%s, %.1fGB) + coder (%.1fGB) + critic (%.1fGB) = %.1fGB > %.1fGB budget",
                                _pre_pin_planner_mdl, _pre_pin_planner_gb, _pre_pin_coder_gb, _pre_pin_critic_gb,
                                _pre_pin_coder_gb + _pre_pin_critic_gb + _pre_pin_planner_gb, _pre_pin_budget)
            _pin_coder = [] if _skip_coder_pin else [exec_mdl if exec_mdl != coder_mdl else coder_mdl]
            _pin_models = _pin_coder + ([] if ctx.duo_config.agentic_mode else [critic_mdl])
            for _pm in _pin_models:
                if _pm == critic_mdl and _is_solo:
                    break
                if ctx.duo_config.agentic_mode and _pm == coder_mdl:
                    _pc_ctx = resolve_ctx(ctx.settings.get("duo_coder_ctx_agentic"), _pm, "agentic")
                elif _pm == coder_mdl:
                    _pc_ctx = resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), _pm, "coder")
                else:
                    _pc_ctx = resolve_ctx(ctx.settings.get("duo_critic_ctx") or ctx.settings.get("duo_coder_ctx_normal"), _pm, "critic")
                await _bk_load(_pm, keep_alive="-1", num_ctx=_pc_ctx or None)
                _duo_pinned.add(_pm)
            _pinned_names = " + ".join(m.split(":")[0] for m in _duo_pinned) or exec_mdl.split(":")[0]
            if _skip_coder_pin and _duo_pinned:
                yield await ctx.emit({"type": "status",
                                  "content": f"⏳ {_pinned_names} ready — coder will be loaded after planner (VRAM smart-pin)"})
            elif _skip_coder_pin and not _duo_pinned:
                if ctx.duo_config.agentic_mode:
                    yield await ctx.emit({"type": "status",
                                      "content": f"⏳ Agentic mode — model loaded lazily (full GPU after pre-explore)"})
                else:
                    yield await ctx.emit({"type": "status",
                                      "content": f"⏳ Coder & critic lazy — planner needs full VRAM"})
            else:
                yield await ctx.emit({"type": "status",
                                  "content": f"✅ {_pinned_names} ready"})
    except Exception as _pin_err:
            yield await ctx.emit({"type": "status",
                              "content": f"⚠️ Pin failed ({str(_pin_err)[:60]}), on-demand loading active"})

    _duo_ws    = ctx.settings.get("duo_websearch_enabled", False) and ctx.websearch_available
    _duo_seen_web_queries: set[str] = set()
    _xtools_ws = _get_inline_tools(include_websearch=_duo_ws, mode="duo_full")
    _tool_think_auto_mode = str(ctx.duo_config.coder_tool_thinking_auto_mode or ctx.settings.get("ctx.duo_config.coder_tool_thinking_auto_mode", "off") or "off").strip().lower()
    if _tool_think_auto_mode == "critical":
        _tool_think_auto_mode = "on_fail"
    if _tool_think_auto_mode not in {"off", "balanced", "on_fail", "always"}:
        _tool_think_auto_mode = "off"
    _effective_tool_think_toggle = bool(ctx.duo_config.coder_tool_thinking)
    _model_hint_thinking = bool(
        ctx.model_profile(exec_mdl).get("thinking", False)
    )
    _exec_supports_thinking = _model_hint_thinking or _effective_tool_think_toggle or bool(ctx.duo_config.agentic_thinking)
    _exec_has_thinking = _exec_supports_thinking
    _coder_tool_think = (
        _effective_tool_think_toggle
        and _exec_supports_thinking
    )
    if ctx.duo_config.coder_tool_thinking_explicit and _coder_tool_think:
        if _tool_think_auto_mode == "always":
            yield await ctx.emit({
                "type": "status",
                "content": "🧠 Tool thinking: always active",
            })
        else:
            yield await ctx.emit({
                "type": "status",
                "content": "🧠 Tool thinking active (manual)",
            })
    elif _tool_think_auto_mode != "off" and _exec_supports_thinking:
        yield await ctx.emit({
            "type": "status",
            "content": (
                f"🧠 Tool thinking: reactive on errors "
                f"(mode={_tool_think_auto_mode})"
            ),
        })

    if ctx.duo_config.agentic_mode:
        _vram_mdl = ctx.vram_lookup_gb.get(coder_mdl, 4.1)
        _runtime_ctx_target = resolve_ctx(ctx.settings.get("duo_coder_ctx_agentic"), coder_mdl, "agentic")
        _ctx_k = _runtime_ctx_target // 1024
        if ctx.duo_config.pre_explore:
            _agentic_label = ""
        else:
            _agentic_label = f"Agentic — {coder_mdl} solo ({_vram_mdl:.1f} GB @ ctx={_ctx_k}k)"
        _runtime_model_source = "dropdown"
    else:
        _agentic_label = f"{coder_mdl} solo"
        _runtime_ctx_target = resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "coder")
        _runtime_model_source = "dropdown"
    yield await ctx.emit({
        "type": "status",
        "content": (
            f"⏱ Duo profile: {_duo_runtime_profile} "
            f"(Run-Timeout ~{int(_duo_run_timeout_s)}s)"
        ),
    })
    yield await ctx.emit({
        "type":             "duo_start",
        "coder":            coder_mdl,
        "executor":         exec_mdl,
        "critic":           critic_mdl,
        "rounds":           duo_rounds,
        "pair":             "free",
        "label":            _agentic_label,
        "coder_tc":         _coder_tc,
        "critic_tc":        _critic_tc,
        "tool_rounds":      ctx.duo_config.tool_rounds,
        "use_pipeline":     ctx.duo_config.use_pipeline,
        "coding_mode":      ctx.duo_config.coding_mode,
        "chunking":         ctx.duo_config.chunking,
        "agentic_mode":     ctx.duo_config.agentic_mode,
        "no_think":         False,
        "websearch_active": _duo_ws,
        "runtime_profile":  _duo_runtime_profile,
        "runtime_profile_requested": str(ctx.duo_config.runtime_profile or ""),
        "runtime_profile_lock_override": bool(ctx.duo_config.runtime_profile_lock_override),
        "runtime_use_preset_models": False,
        "runtime_timeout_s": int(_duo_run_timeout_s),
        "runtime_model_source": _runtime_model_source,
        "runtime_ctx_target": int(_runtime_ctx_target),
        "runtime_model_override": coder_mdl,
        "n_workers":        len([w for w in ((ctx.settings.get("exploration_agent") or {}).get("workers") or []) if w.get("model")])
                            if ctx.duo_config.pre_explore
                            else 0,
    })

    # User-override for --reasoning: explicit thinking toggle has priority over model-category heuristic.
    # Always set (or reset to None) to prevent stale override leaking into next run.
    from backend.llama_server_manager import manager as _lsm_ro
    _lsm_ro._reasoning_override = _coder_tool_think if ctx.duo_config.coder_tool_thinking_explicit else None

    # ── State population ──
    state.update({
        "coder_mdl": coder_mdl,
        "critic_mdl": critic_mdl,
        "exec_mdl": exec_mdl,
        "_duo_runtime_profile": _duo_runtime_profile,
        "duo_rounds": duo_rounds,
        "_planner_step_cap": _planner_step_cap,
        "_duo_run_timeout_s": _duo_run_timeout_s,
        "_duo_deadline_at": _duo_deadline_at,
        "_duo_timed_out": _duo_timed_out,
        "_ctx_peak_tokens": _ctx_peak_tokens,
        "_ctx_limit_seen": _ctx_limit_seen,
        "_ctx_pressure_peak": _ctx_pressure_peak,
        "_ctx_compressions": _ctx_compressions,
        "_ctx_evictions": _ctx_evictions,
        "_tool_round_durations": _tool_round_durations,
        "_coder_tc": _coder_tc,
        "_critic_tc": _critic_tc,
        "_exec_tc": _exec_tc,
        "_critic_tools_enabled": _critic_tools_enabled,
        "_duo_pinned": _duo_pinned,
        "_skip_coder_pin": _skip_coder_pin,
        "_coder_ctx": _coder_ctx,
        "_exec_ctx": _exec_ctx,
        "_critic_ctx": _critic_ctx,
        "_duo_ws": _duo_ws,
        "_duo_seen_web_queries": _duo_seen_web_queries,
        "_xtools_ws": _xtools_ws,
        "_tool_think_auto_mode": _tool_think_auto_mode,
        "_exec_supports_thinking": _exec_supports_thinking,
        "_exec_has_thinking": _exec_has_thinking,
        "_coder_tool_think": _coder_tool_think,
        "_avail": _avail,
        "_recent_focus_paths": _recent_focus_paths,
        "_MAX_TREE_DEPTH": _MAX_TREE_DEPTH,
        "_MAX_TREE_FILES": _MAX_TREE_FILES,
        "_runtime_ctx_target": _runtime_ctx_target,
    })
