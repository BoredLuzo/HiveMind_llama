# -*- coding: utf-8 -*-
"""Chat run orchestration (run_stream) — extracted from server.py (M2b).

run_stream builds the RunContext (settings, presets, prefetch machinery)
and delegates to run_stream_orchestrated (core/stream.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid

import httpx

from core import state as _state
from core.state import (
    settings,
    registry_get, registry_set, registry_all,
    apply_settings_to_pipeline, _get_num_ctx,
)
from core.duo_config import DuoConfig
from core.run_context import RunContext
from core.stream import run_stream_orchestrated
from core.model_sampling import _model_profile
from core.duo_helpers import (
    DEFAULT_VRAM_BUDGET_GB,
    _resolve_duo_runtime_profile, _resolve_duo_run_timeout_seconds,
    _bucket_stop_reason,
)
from context.chat_util import (
    _make_messages, _extract_ws_query, _extract_memory, _auto_memory_from_input,
)
from context.compression import SESSION_COMPRESS_THRESHOLD
from infra.phase_timer import PhaseTimer
from infra.token_stats import _estimate_tokens_from_content
from utils.math import percentile_float as _percentile_float
from utils.httpx_utils import make_httpx_timeout as _make_httpx_timeout
from tools.websearch import _safe_web_search
from hive_functions.prompts import AGENT_ROLES, HIVEMIND_SOUL, VISION_AGENT_PROMPT
from hive_functions.soul_engine import (
    build_soul_prompt_layer, load_soul, run_soul_cycle,
)

logger = logging.getLogger("hivemind.chat_run")
_logger = logger

async def run_stream(
    user_input: str,
    images: list,
    mode: str,
    iterations: int,
    active_preset,
    constraint_mode: bool = True,
    force_complexity: str | None = None,
    skip_agents: set = frozenset(),
    judge_bias: int = 50,
    chat_id: str | None = None,
    duo_config: DuoConfig | None = None,
    model_overrides: dict | None = None,
):
    # Lazy server import (avoids an import cycle; server is loaded at runtime).
    from server import (
        S_models_cache,
        THIS_FILE,
        _COMPLEX_DIRECT_MODELS,
        _SIMPLE_DIRECT_MODELS,
        _VRAM_LOOKUP_GB,
        _WEBSEARCH_AVAILABLE,
        _abort_event,
        _bk_evict,
        _bk_load,
        _bk_pin,
        _check_complexity_with_bias,
        _clear_resume_block,
        _clear_step_skip,
        _compress_chat_session,
        _filter_vision_images,
        _flush_prefetch_settings,
        _get_avg_elapsed,
        _get_inline_tools,
        _get_loaded_models_set,
        _increment_run_counter,
        _is_aborted,
        _load_chat_context,
        _load_resume_block,
        _maybe_trigger_soul_evolution,
        _pick_direct_model,
        _pipeline_chat_stream,
        _preprocess_images_to_text,
        _record_run,
        _refresh_judge_keepalive,
        _register_abort,
        _register_step_skip,
        _registry,
        _run_insight_extractor,
        _run_skill_distillation,
        _unregister_abort,
        _unregister_step_skip,
        _update_prefetch_lead,
        _vision_cfg,
        append_learning_log,
        apply_learned_configs_for_run,
        detect_agent_intent,
        detect_task_type,
        detect_tool_request,
        detect_vision_need,
        get_automap,
        get_effective_config,
        get_effective_prompt_with_override,
        get_learned_config,
        get_question_from_intent,
        get_routing_suggestion,
        read_learning_log,
        smart_preload_if_needed,
    )

    if duo_config is None:
        duo_config = DuoConfig()

    logger.warning(
        "[RUN-ENTRY] mode=%s agentic=%s pre_explore=%s planner=%s chunking=%s "
        "chat_id=%s force=%s until_finished=%s coding_mode=%s",
        mode, duo_config.agentic_mode, duo_config.pre_explore, duo_config.planner,
        duo_config.chunking, chat_id, force_complexity, duo_config.until_finished,
        duo_config.coding_mode,
    )

    _run_settings = dict(settings)
    if model_overrides:
        _run_settings.update(model_overrides)
    logger.warning("[RUN-TRACE] settings-kopie ok (overrides=%s)", sorted(model_overrides or {}))

    # Per-run token estimate accumulator ─ incremented by emit() on content events
    _run_token_estimate: int = 0
    _run_real_tokens_by_phase: dict = {}
    _run_prompt_by_phase: dict = {}
    _run_cached_by_phase: dict = {}
    _run_models: dict = {}
    _run_requests: int = 0

    async def emit(data: dict) -> str:
        nonlocal _run_token_estimate, _run_real_tokens_by_phase, _run_models, _run_requests
        nonlocal _run_prompt_by_phase, _run_cached_by_phase
        # Track generated output tokens from content events (all token types)
        _tok_types = {"token", "thinking_token", "planner_token", "planner_thinking_token", "planner_plan_token"}
        if data.get("type") in _tok_types and data.get("content"):
            n = _estimate_tokens_from_content(str(data["content"]))
            _run_token_estimate += n
            _phase_timer.add_tokens(n)
        # A-P2-7: echte completion_tokens vom llama.cpp-Server akkumulieren (pro Phase).
        if data.get("type") == "usage_meta" and data.get("completion_tokens"):
            _ph = str(data.get("phase") or "coder")
            _run_requests += 1
            _run_real_tokens_by_phase[_ph] = _run_real_tokens_by_phase.get(_ph, 0) + int(data["completion_tokens"])
            _run_prompt_by_phase[_ph] = _run_prompt_by_phase.get(_ph, 0) + int(data.get("prompt_tokens") or 0)
            _run_cached_by_phase[_ph] = _run_cached_by_phase.get(_ph, 0) + int(data.get("cached_tokens") or 0)
            # D2-DIAG (2026-08-21): Cache-Miss erkennen — prompt gross, cached klein
            # [CTX-COMPRESS] korrelierbar.
            try:
                _cached_n = int(data.get("cached_tokens") or 0)
                _prompt_n = int(data.get("prompt_tokens") or 0)
                if _prompt_n >= 20000 and _cached_n < _prompt_n * 0.25:
                    logger.warning(
                        "[CACHE-MISS] phase=%s prompt=%d cached=%d (reuse %.0f%%)",
                        _ph, _prompt_n, _cached_n,
                        (100.0 * _cached_n / _prompt_n) if _prompt_n else 0.0,
                    )
            except Exception:
                pass
            _pt_key = {
                "coder": "coder_loop", "planner": "soft_planner",
                "pre_explore": "pre_explore", "critic": "coder_loop",
            }.get(_ph, "coder_loop")
            _phase_timer.add_real(_pt_key, int(data["completion_tokens"]), float(data.get("gen_ms") or 0.0))
        if data.get("type") == "run_meta" and isinstance(data.get("models"), dict):
            _run_models.update(data["models"])
            return ""
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    def _stop_reason_bucket(stop_reason: str) -> str:
        return _bucket_stop_reason(stop_reason)

    def _done_event(elapsed: float, stop_reason: str = "completed", **extra) -> dict:
        nonlocal _run_token_estimate, _run_real_tokens_by_phase
        nonlocal _run_prompt_by_phase, _run_cached_by_phase
        _real_total = sum(int(v) for v in _run_real_tokens_by_phase.values() if isinstance(v, (int, float)))
        _prompt_total = sum(int(v) for v in _run_prompt_by_phase.values() if isinstance(v, (int, float)))
        _cached_total = sum(int(v) for v in _run_cached_by_phase.values() if isinstance(v, (int, float)))
        _tokens_to_record = _real_total if _real_total > 0 else _run_token_estimate
        if _real_total > 0:
            logger.info("[A-P2-7] real eval_counts=%d prompt=%d cached=%d (by phase=%s) heuristic=%d — recording %d",
                        _real_total, _prompt_total, _cached_total,
                        _run_real_tokens_by_phase, _run_token_estimate, _tokens_to_record)
        _record_ok = stop_reason in ("completed", "loop_detected", "timeout", "hard_stop", "graceful_stop", "aborted")
        if _record_ok and _tokens_to_record > 0:
            _phases = dict(_run_real_tokens_by_phase)
            try:
                _snap = _phase_timer.snapshot()
                for _pname, _skey in (("pre_explore", "pre_explore"), ("planner", "soft_planner"), ("coder", "coder_loop")):
                    if not _phases.get(_pname):
                        _ot = (_snap.get(_skey) or {}).get("output_tokens", 0)
                        if _ot:
                            _phases[_pname] = int(_ot)
            except Exception:
                pass
            threading.Thread(
                target=_record_run,
                args=({
                    "run_id": run_id,
                    "tokens": _tokens_to_record,
                    "elapsed_s": float(elapsed),
                    "stop_reason": str(stop_reason),
                    "phases": _phases,
                    "models": _run_models,
                    # TOKEN-TRACKER 1.1.0: Input-/Cache-Dimension
                    "prompt_tokens": _prompt_total,
                    "cached_tokens": _cached_total,
                    "requests": _run_requests,
                    "phases_prompt": dict(_run_prompt_by_phase),
                    "phases_cached": dict(_run_cached_by_phase),
                },),
                daemon=True,
            ).start()
        if stop_reason == "completed":
            try:
                from infra.notify import notify_run_completed
                _cmsg = str(extra.get("phase_summary", "") if extra else "") or "Run completed successfully"
                notify_run_completed(_cmsg)
            except Exception:
                pass
        elif stop_reason not in ("graceful_stop",):
            try:
                from infra.notify import notify_run_stopped
                notify_run_stopped(str(stop_reason), str(extra.get("summary", "") if extra else ""))
            except Exception:
                pass
        ev = {
            "type": "done",
            "elapsed": float(elapsed),
            "stop_reason": str(stop_reason or "completed"),
            "stop_reason_bucket": _stop_reason_bucket(stop_reason),
            "tokens_generated": _tokens_to_record,
            "prompt_tokens_total": _prompt_total,
            "cached_tokens_total": _cached_total,
            "requests_total": _run_requests,
        }
        if extra:
            ev.update(extra)
        return ev

    t_total     = time.time()
    _phase_timer = PhaseTimer()
    run_id      = f"{int(t_total)}-{uuid.uuid4().hex[:8]}"
    # WORKSPACE-PERSISTENCE (2026-08-25 REWORK): Central resolution via
    from utils.workspace_resolve import (
        resolve_workspace as _resolve_workspace,
        sync_env_workspace as _sync_env_workspace,
        save_last_workspace as _save_last_ws,
        WorkspaceForceInvalid as _WsForceInvalid,
    )
    try:
        _chat_ctx_ws = _load_chat_context(chat_id) if chat_id else {}
        _ws_str, _ws_src = _resolve_workspace(settings, _chat_ctx_ws, user_input)
    except _WsForceInvalid as _wfi:
        logger.error("[WS-RESOLVE] %s", _wfi)
        yield await emit({"type": "status", "content": f"\u26d4 {_wfi}"})
        yield await emit(_done_event(0.0, "blocked",
                                    summary=f"Workspace invalid: {str(_wfi.raw)[:120]}"))
        return
    # ENV-Sync: Subprozesse (run_bash/run_python/browser) + Fallback-Lesezugriffe
    _sync_env_workspace(_ws_str)
    _save_last_ws(_ws_str)
    logger.warning("[RUN-TRACE] workspace=%s (source=%s)", _ws_str, _ws_src)

    _exec_ctrl                = None  # duo_runner / pipeline_runner
    _resolve_resume_block     = None  # Runner bei Resume
    _resolve_duo_rounds_cap   = None  # Runner
    _do_prefetch              = None  # _schedule_prefetch()
    _prefetch_judge_once      = None
    run_peer_ratings          = None

    use_learned   = settings.get("learning_preset_mode", False)
    smart_preload = settings.get("smart_preload_enabled", True)
    # P1-2 FIX: Cache VRAM budget ONCE per run ─ prevents mid-run setting changes
    # from causing VRAM overcommit ─ OOM ─ model crash. Previously parsed from
    # mutable settings dict 13+ times per run with inconsistent values possible.
    _raw_budget = settings.get("vram_budget_gb")
    _vram_budget = float(_raw_budget) if _raw_budget is not None else DEFAULT_VRAM_BUDGET_GB
    abort_ev      = _register_abort(run_id)
    step_skip_ev = _register_step_skip(run_id)  # NEW
    logger.warning("[RUN-TRACE] abort/stepskip registered run_id=%s", run_id)
    _agent_elapsed: dict = {}   # agent_key ─ elapsed seconds, fÃ¼r Log + Prefetch-Kalibrierung
    _vram_cache:    set  = await _get_loaded_models_set() if smart_preload else set()

    # Phase H: run-scoped perf metrics + prefetch tuning state.
    _ctx_pressure_peak = 0.0
    _ctx_peak_tokens = 0
    _ctx_limit_seen = 0
    _ctx_evictions = 0
    _ctx_compressions = 0
    _tool_round_durations: list[float] = []
    _prefetch_inflight = 0
    _prefetch_inflight_max = 0
    _prefetch_run_active = True
    _prefetch_state = {
        "prefetch_agent_avgs": dict(settings.get("prefetch_agent_avgs", {}) or {}),
        "prefetch_lead_seconds": float(settings.get("prefetch_lead_seconds", 8.0) or 8.0),
    }

    def _runtime_telemetry_snapshot() -> dict:
        try:
            import backend.llama_server_manager as _lsm_metrics
            _mgr = getattr(_lsm_metrics, "manager", None)
            if _mgr and hasattr(_mgr, "telemetry_snapshot"):
                return dict(_mgr.telemetry_snapshot() or {})
        except Exception:
            pass
        return {}

    _runtime_telemetry_start = _runtime_telemetry_snapshot()

    def _runtime_delta(now: dict, key: str) -> int:
        try:
            return int(now.get(key, 0)) - int(_runtime_telemetry_start.get(key, 0))
        except Exception:
            return 0

    def _collect_done_metrics() -> dict:
        _agent_vals = [float(v) for v in _agent_elapsed.values() if isinstance(v, (int, float))]
        _tool_vals = [float(v) for v in _tool_round_durations if isinstance(v, (int, float))]
        _rt_now = _runtime_telemetry_snapshot()
        return {
            "metrics_ctx_pressure_peak": round(_ctx_pressure_peak, 3),
            "metrics_ctx_peak_tokens": int(_ctx_peak_tokens),
            "metrics_ctx_limit": int(_ctx_limit_seen),
            "metrics_ctx_evictions": int(_ctx_evictions),
            "metrics_ctx_compressions": int(_ctx_compressions),
            "metrics_tool_rounds": int(len(_tool_vals)),
            "metrics_tool_round_p50_s": round(_percentile_float(_tool_vals, 0.5), 3),
            "metrics_tool_round_p95_s": round(_percentile_float(_tool_vals, 0.95), 3),
            "metrics_agent_p50_s": round(_percentile_float(_agent_vals, 0.5), 3),
            "metrics_agent_p95_s": round(_percentile_float(_agent_vals, 0.95), 3),
            "metrics_prefetch_lead_s": float(_prefetch_state.get("prefetch_lead_seconds", settings.get("prefetch_lead_seconds", 8.0))),
            "metrics_prefetch_inflight_max": int(_prefetch_inflight_max),
            "metrics_runtime_evictions": _runtime_delta(_rt_now, "evictions_total"),
            "metrics_runtime_evictions_manual": _runtime_delta(_rt_now, "evictions_manual"),
            "metrics_runtime_evictions_lru": _runtime_delta(_rt_now, "evictions_lru"),
            "metrics_runtime_evictions_idle": _runtime_delta(_rt_now, "evictions_idle"),
            "metrics_runtime_evictions_ctx_reload": _runtime_delta(_rt_now, "evictions_ctx_reload"),
            "metrics_runtime_evictions_parallel_reload": _runtime_delta(_rt_now, "evictions_parallel_reload"),
            "metrics_runtime_orphan_rehabilitations": _runtime_delta(_rt_now, "orphan_rehabilitations"),
            "metrics_runtime_prefetch_enqueued": _runtime_delta(_rt_now, "prefetch_enqueued"),
            "metrics_runtime_prefetch_dequeued": _runtime_delta(_rt_now, "prefetch_dequeued"),
            "metrics_runtime_prefetch_failures": _runtime_delta(_rt_now, "prefetch_failures"),
            "metrics_runtime_prefetch_pending_end": int(_rt_now.get("prefetch_pending", 0)),
            "metrics_runtime_prefetch_active_end": int(_rt_now.get("prefetch_active", 0)),
            "phase_timings": _phase_timer.snapshot(),
            "phase_summary": _phase_timer.ui_summary(),
        }

    async def _maybe_preload(model: str) -> str | None:
        nonlocal _vram_cache
        if not smart_preload:
            return None
        did_load, _vram_cache = await smart_preload_if_needed(model, _vram_cache)
        if did_load:
            return await emit({"type": "status", "content": f"⚡ Smart Preload: {model.split(':')[0]} → VRAM"})
        return None

    def _schedule_prefetch(next_model: str, current_agent: str, agent_start_time: float | None = None):


        if not smart_preload:
            return
        current_model = registry_get(current_agent) if current_agent in _state.pipeline.agents else ""
        if next_model == current_model:
            return
        lead  = float(_prefetch_state.get("prefetch_lead_seconds", settings.get("prefetch_lead_seconds", 8.0)))
        avg   = float(_prefetch_state.get("prefetch_agent_avgs", {}).get(current_agent, _get_avg_elapsed(current_agent)))
        now   = time.time()
        start = agent_start_time or now

        # Phase H: load-aware lead backoff under VRAM contention.
        try:
            _budget = max(2.0, _vram_budget)
            _used = sum(float(_VRAM_LOOKUP_GB.get(m, 4.0)) for m in _vram_cache)
            _pressure = _used / max(0.1, _budget)
            if _pressure >= 0.85:
                lead = max(2.0, lead * 0.7)
            elif _pressure >= 0.75:
                lead = max(2.5, lead * 0.82)
            if _prefetch_inflight >= 2:
                lead = max(2.0, lead * 0.85)
        except Exception:
            pass

        delay = max(0.5, (start + avg - lead) - now) if avg > 0 else 0.5

        async def _do_prefetch():
            nonlocal _vram_cache, _prefetch_inflight, _prefetch_inflight_max
            _prefetch_inflight += 1
            _prefetch_inflight_max = max(_prefetch_inflight_max, _prefetch_inflight)
            try:
                await asyncio.sleep(delay)
                if not _prefetch_run_active or _abort_event(run_id) is None:
                    return
                # Refresh the cache before prefetching.
                try:
                    _fresh = await _get_loaded_models_set(max_age=0)
                    _vram_cache = _fresh
                except Exception:
                    pass
                # Pipeline agents bypass judge protection via a direct httpx call.
                _judge_mdl = _registry.get("judge", "")
                _is_judge_model = bool(_judge_mdl and (
                    next_model == _judge_mdl or next_model.split(":")[0] == _judge_mdl.split(":")[0]
                ))
                if _is_judge_model:
                    _already_warm = next_model in _vram_cache or any(
                        next_model.split(":")[0] == m.split(":")[0] for m in _vram_cache
                    )
                    if not _already_warm:
                        try:
                            _pf_ctx = _get_num_ctx(next_model)
                            from backend import api_prefetch_next as _api_pf_next
                            await _api_pf_next(next_model, num_ctx=_pf_ctx)
                            _vram_cache |= {next_model}
                        except Exception:
                            pass
                else:
                    from backend import api_prefetch_next as _api_pf_next
                    await _api_pf_next(next_model, num_ctx=_get_num_ctx(next_model))
            except asyncio.CancelledError:
                raise
            except Exception as _pf_err:
                logger.warning("Prefetch task failed (%s via %s): %s", next_model, current_agent, str(_pf_err)[:120])
            finally:
                _prefetch_inflight = max(0, _prefetch_inflight - 1)

        asyncio.create_task(_do_prefetch())

    logger.warning("[RUN-TRACE] vor run_id-yield")
    yield await emit({"type": "run_id", "run_id": run_id})

    if use_learned:
        apply_learned_configs_for_run()
    else:
        apply_settings_to_pipeline(settings)
    logger.warning("[RUN-TRACE] run_id-yield + pipeline-apply ok (use_learned=%s)", use_learned)

    _judge_prefetch_task: asyncio.Task | None = None
    _can_need_judge = (
        mode in ("auto", "automap")
        and not duo_config.agentic_mode
        and force_complexity not in ("simple", "complex")
        and 10 < int(judge_bias) < 90
    )
    if _can_need_judge and bool(settings.get("judge_prefetch_before_complexity", True)):
        async def _prefetch_judge_once():
            try:
                _judge_model = _registry.get("judge", "")
                if not _judge_model:
                    return
                _budget = _vram_budget
                _judge_gb = float(_VRAM_LOOKUP_GB.get(_judge_model, 2.1))
                _loaded = await _get_loaded_models_set(max_age=3.0)
                _jbase = _judge_model.split(":")[0]
                _other_gb = sum(
                    float(_VRAM_LOOKUP_GB.get(m, 4.0))
                    for m in _loaded
                    if m.split(":")[0] != _jbase
                )
                if _other_gb + _judge_gb > _budget:
                    return
                from backend import api_prefetch_next as _api_pf_next
                await _api_pf_next(_judge_model, num_ctx=_get_num_ctx(_judge_model, "judge"))
            except Exception:
                pass
        _judge_prefetch_task = asyncio.create_task(_prefetch_judge_once())

    _vision_images, _vision_input_status = _filter_vision_images(images)
    if _vision_input_status:
        yield await emit({"type": "status", "content": _vision_input_status})
    images = _vision_images

    image_description: str | None = None
    effective_images = images
    vision_agent_images = list(images) if images else []

    _pipeline_soul = load_soul(THIS_FILE)

    def _aborted() -> bool:
        return abort_ev.is_set()
     
    def _step_skipped() -> bool:
        return step_skip_ev.is_set()

    # Memory special cases
    if _state.pipeline._is_list_memory_request(user_input):
        logger.warning("[RUN-ENTRY] early return: list_memory_request")
        mems = list(_state.memory.list_memories())
        if not mems:
            yield await emit({"type": "token", "content": "Nothing stored."})
        else:
            for k, v, d in mems:
                yield await emit({"type": "token", "content": f"{k}: {v} ({d})\n"})
        yield await emit(_done_event(0, "memory_list"))
        _unregister_abort(run_id)
        _unregister_step_skip(run_id)
        return

    if _state.pipeline._is_forget_request(user_input):
        logger.warning("[RUN-ENTRY] early return: forget (user_input=%s)", user_input[:80].replace("\n", " "))
        words   = user_input.lower().split()
        deleted = [k for k in list(_state.memory.get_all()) if k in words]
        for k in deleted:
            _state.memory.forget(k)
        msg = f"Deleted: {', '.join(deleted)}" if deleted else "Nothing found."
        yield await emit({"type": "token", "content": msg})
        yield await emit(_done_event(0, "memory_forget"))
        _unregister_abort(run_id)
        _unregister_step_skip(run_id)
        return

    if _state.pipeline._is_memory_request(user_input):
        logger.warning("[RUN-ENTRY] early return: memory_request")
        _mem_key, _mem_val = _extract_memory(user_input)
        if _mem_key and _mem_val:
            _state.memory.remember(_mem_key, _mem_val)
            result = f"Stored: {_mem_key} = {_mem_val}"
        else:
            result = await _state.pipeline._handle_memory_request(user_input)
        _state.memory.add_to_session("user", user_input)
        _state.memory.add_to_session("assistant", result)
        yield await emit({"type": "token", "content": result})
        yield await emit({"type": "memory_saved"})
        yield await emit(_done_event(0, "memory_write"))
        _unregister_abort(run_id)
        _unregister_step_skip(run_id)
        return

    # Self-query
    if _state.pipeline._is_self_question(user_input):
        logger.warning("[RUN-ENTRY] early return: self_question")
        reg         = registry_all()
        model_lines = []
        for a_key, a in _state.pipeline.agents.items():
            model       = reg.get(a_key, a.model)
            has_learned = get_learned_config(THIS_FILE, model, a_key) is not None
            log_entries = len(read_learning_log(THIS_FILE, model, limit=100))
            model_lines.append(
                f"- {a.name} ({a_key}): {model}"
                + (" [learned config]" if has_learned else "")
                + (f" [{log_entries} log entries]" if log_entries else "")
            )
        model_info = "\n".join(model_lines)
        mem_count  = len(list(_state.memory.list_memories()))
        soul       = _pipeline_soul
        soul_layer = build_soul_prompt_layer(soul)

        custom_sys = (
            HIVEMIND_SOUL
            + (f"\n\n{soul_layer}" if soul_layer else "")
            + f"\n\nCurrent configuration:\n{model_info}"
            + f"\nIterations: {iterations}  Mode: {mode}"
            + f"\nPersistent memory entries: {mem_count}"
            + f"\nLearning preset mode: {'ON -- peer ratings active' if use_learned else 'off'}"
            + f"\nConstraint feedback loop: {'ON -- adversarial JSON constraints' if constraint_mode else 'off'}"
            + f"\nAutomap: {'available' if S_models_cache else 'no model cache'}"
            + f"\nSoul evolution: v{soul.get('version', 1)}, {soul.get('evolution_count', 0)} evolutions"
        )

        agent    = _state.pipeline.agents["direct"]
        messages = _make_messages(_state.pipeline, custom_sys, user_input, images, False, False)

        yield await emit({"type": "agent", "content": "Hivemind", "model": agent.model})

        parts, t = [], time.time()
        async for tok in _pipeline_chat_stream(agent.model, messages, 0.3, 600):  # BUG-11 FIX: num_ctx via wrapper
            parts.append(tok)
            yield await emit({"type": "token", "content": tok})
        yield await emit({"type": "agent_done", "elapsed": round(time.time() - t, 1)})
        _state.memory.add_to_session("user", user_input)
        _state.memory.add_to_session("assistant", "".join(parts))
        yield await emit(_done_event(round(time.time() - t_total, 1), "self_query_completed"))
        _unregister_abort(run_id)
        _unregister_step_skip(run_id)
        return

    # -- Vision Preprocessing --
    _vdbg_has_images = bool(images)
    _vdbg_enabled    = bool(_vision_cfg.get("enabled"))
    _vdbg_model      = _vision_cfg.get("model", "")
    _logger.debug("[Vision-Trigger] images=%s enabled=%s model=%r", _vdbg_has_images, _vdbg_enabled, _vdbg_model)
    if images and _vision_cfg.get("enabled") and _vision_cfg.get("model"):
        _vision_model_name = _vision_cfg["model"]
        _configured_direct = registry_get("direct") if "direct" in _state.pipeline.agents else ""
        _direct_caps = _model_profile(_configured_direct) if _configured_direct else {}
        _direct_is_vision = bool(_direct_caps.get("vision", False))
        _multimodal_available: list = []
        if not _direct_is_vision:
            _multimodal_available = [
                m for m in (S_models_cache or [])
                if bool((_model_profile(m) or {}).get("vision", False))
                and m.split(":")[0] in ("qwen3.5", "qwen3.6", "hermes3.6", "hermes", "gemma-4", "tiel-coder")
            ]
            if _multimodal_available:
                _direct_is_vision = True
                _logger.debug("[Vision-Trigger] No multimodal Direct configured but available: %s - image direct",
                              _multimodal_available[:3])

        if _direct_is_vision:
            if _configured_direct and bool((_model_profile(_configured_direct) or {}).get("vision", False)):
                _direct_disp = _configured_direct
            else:
                _direct_disp = _multimodal_available[0] if _multimodal_available else "multimodal model"
            yield await emit({"type": "status",
                "content": f"Multimodal model ({_direct_disp}) processes the image directly — no vision preprocessing"})
            _logger.info("[Vision-Trigger] Image direct to multimodal model - prepro skipped (direct=%r, avail=%s)",
                         _configured_direct, _multimodal_available[:3] if _multimodal_available else "configured")
        else:
            yield await emit({"type": "status", "content": "Vision model (" + _vision_model_name + ") describes images..."})
            _vp_total_timeout = float(settings.get("vision_preprocess_timeout_seconds", 30.0) or 30.0)
            try:
                image_description = await asyncio.wait_for(
                    _preprocess_images_to_text(images, user_input),
                    timeout=_vp_total_timeout,
                )
            except asyncio.TimeoutError:
                image_description = f"[Vision preprocessing error: timeout after {_vp_total_timeout:.1f}s]"
                _logger.warning(
                    "[Vision-Prepro] Overall timeout after %.1fs (model=%s)",
                    _vp_total_timeout, _vision_model_name,
                )
            if image_description and not image_description.startswith("[Vision-Preprocessing-Error"):
                yield await emit({"type": "image_description", "content": image_description})
            elif image_description and image_description.startswith("[Vision-Preprocessing-Error"):
                yield await emit({"type": "status", "content": image_description})

        try:
            from backend.llama_server_manager import manager as _vsm
            from backend.llama_vram_table import vram_of as _vram_of_fn
            _direct_for_vram = _configured_direct or _vision_model_name
            _vision_gb = _vram_of_fn(_vision_model_name)
            _direct_gb = _vram_of_fn(_direct_for_vram)
            _judge_gb  = _vram_of_fn(_registry.get("judge", "granite-4.1:3b"))
            _budget_gb = _vram_budget
            _same_exact = _direct_for_vram == _vision_model_name
            if _same_exact:
                _logger.debug("[Vision-Evict] Reuse: %s == %s", _direct_for_vram, _vision_model_name)
            elif _vision_gb + _direct_gb + _judge_gb > _budget_gb - 0.3:
                # Too tight ─ evict vision
                await _bk_evict(_vision_model_name)
                _v_port = next(
                    (s.port for s in _vsm._slots if s.model == _vision_model_name), None
                )
                if _v_port:
                    for _ in range(50):  # max 7.5s
                        if not await _vsm._port_alive(_v_port):
                            break
                        await asyncio.sleep(0.15)
                await asyncio.sleep(3.0)
                _logger.debug("[Vision-Evict] Evicted %s ─ %.1f+%.1f+%.1f=%.1fGB > %.1fGB",
                              _vision_model_name, _vision_gb, _direct_gb, _judge_gb,
                              _vision_gb + _direct_gb + _judge_gb, _budget_gb)
            else:
                _logger.debug("[Vision-Evict] No evict - %.1f+%.1f+%.1f=%.1fGB <= %.1fGB budget",
                              _vision_gb, _direct_gb, _judge_gb,
                              _vision_gb + _direct_gb + _judge_gb, _budget_gb)
        except Exception as _ve:
            _logger.debug("[Vision-Evict] Check failed (%s) - evicting to be safe", _ve)
            try:
                await _bk_evict(_vision_model_name)
                await asyncio.sleep(3.0)
            except Exception:
                pass
        if _configured_direct and _configured_direct != _vision_model_name:
            async def _prefetch_direct_post():
                try:
                    from backend import api_prefetch_next as _api_pf_next
                    await _api_pf_next(_configured_direct, num_ctx=_get_num_ctx(_configured_direct))
                except Exception:
                    pass
            asyncio.create_task(_prefetch_direct_post())
        if _direct_is_vision:
            effective_images = images
            _logger.debug("[Vision-Trigger] Multimodal Direct model - raw images stay in effective_images (%d)", len(images))
        else:
            effective_images = []
    elif images:
        effective_images = []
        _va_cfg_agent = _state.pipeline.agents.get("vision")
        _va_cfg_model = settings.get("vision_agent_model", "") or (_va_cfg_agent.model if _va_cfg_agent else "")
        _va_cfg_enabled = bool(settings.get("vision_agent_enabled", False) and _va_cfg_model)
        _direct_caps2 = _model_profile(registry_get("direct") if "direct" in _state.pipeline.agents else "")
        _direct_is_vision2 = bool((_direct_caps2 or {}).get("vision", False))
        _multimodal_avail2: list = []
        if not _direct_is_vision2:
            _multimodal_avail2 = [
                m for m in (S_models_cache or [])
                if bool((_model_profile(m) or {}).get("vision", False))
                and m.split(":")[0] in ("qwen3.5", "qwen3.6", "hermes3.6", "hermes", "gemma-4", "tiel-coder")
            ]
            _direct_is_vision2 = bool(_multimodal_avail2)
        if _direct_is_vision2:
            effective_images = images
            _disp2 = (registry_get("direct") if registry_get("direct")
                      and bool((_model_profile(registry_get("direct")) or {}).get("vision", False))
                      else (_multimodal_avail2[0] if _multimodal_avail2 else "multimodal model"))
            yield await emit({"type": "status",
                "content": f"Multimodal model ({_disp2}) processes the image directly"})
        elif _va_cfg_enabled:
            yield await emit({"type": "status", "content": "[Vision preprocessing off ─ vision-agent uses raw image]"})
        else:
            vision_agent_images = []
            yield await emit({"type": "status", "content": "[Image ignored ─ no vision model active]"})

    # P1-2 (2026-08-12): Restore the session per chat from .context.json,
    if chat_id and not _state.memory.get_session_messages():
        try:
            _sess_persist = (_load_chat_context(chat_id) or {}).get("session") or []
            if _sess_persist:
                _state.memory.seed_session(_sess_persist)
        except Exception:
            pass

    _pipeline_mem_ctx  = _state.pipeline.memory.as_context_string()
    _pipeline_sess_msgs = _state.memory.get_session_messages()  # SESSION-CACHE: 8 DB-Calls ─ 1 pro Run
    # Threshold: SESSION_COMPRESS_THRESHOLD messages (configurable via settings).
    _sess_compress_threshold = int(settings.get("session_compress_threshold", SESSION_COMPRESS_THRESHOLD))
    _skip_compress = (mode == "code_duo" or duo_config.agentic_mode)
    if not _skip_compress and len(_pipeline_sess_msgs) > _sess_compress_threshold:
        _pipeline_sess_msgs = await _compress_chat_session(_pipeline_sess_msgs)


    # Natural agent routing is controlled by the Intent-Agent toggle.
    _intent_cfg = settings.get("intent_agent", {}) or {}
    _intent_enabled = bool(_intent_cfg.get("enabled", False))
    intent_agent = detect_agent_intent(user_input) if _intent_enabled else None

    _prepro_active  = bool(images and _vision_cfg.get("enabled") and _vision_cfg.get("model"))
    _prepro_success = bool(image_description and not image_description.startswith("[Vision"))
    _effective_has_images = bool(images) and not _prepro_success
    _task_type = detect_task_type(user_input, has_images=_effective_has_images)


    _duo_mode_active = (mode == "code_duo" or duo_config.agentic_mode)
    if intent_agent and _duo_mode_active:
        intent_agent = None

    if intent_agent:
        logger.warning("[RUN-ENTRY] early return: intent_agent=%s", intent_agent)
        question   = get_question_from_intent(user_input, intent_agent)
        agent      = _state.pipeline.agents[intent_agent]
        role       = AGENT_ROLES.get(intent_agent, "")
        sys_prompt = get_effective_prompt_with_override(intent_agent, active_preset, use_learned)
        custom_sys = f"Agent: {agent.name} in Hivemind. Role: {role}\n\n{sys_prompt}"
        messages   = _make_messages(_state.pipeline, custom_sys, question, images, True, True, cached_mem_ctx=_pipeline_mem_ctx, cached_sess_msgs=_pipeline_sess_msgs)
        intent_model = registry_get(intent_agent)
        yield await emit({"type": "agent", "content": agent.name, "model": intent_model})
        parts, t = [], time.time()
        _think_open_i = False
        async for tok in _pipeline_chat_stream(
            intent_model, messages, agent.temperature, agent.max_tokens
        ):
            if _aborted(): break
            if _step_skipped():   # NEW
                yield await emit({"type": "status", "content": "— Pre-Explore skipped."})
                _explore_ctx = ""
                break
            parts.append(tok)
            if not _think_open_i and "<think>" in tok:
                before = tok.split("<think>")[0]
                if before:
                    yield await emit({"type": "token", "content": before})
                _think_open_i = True
            if _think_open_i and "</think>" in tok:
                _think_open_i = False
                after = tok.split("</think>")[-1]
                if after:
                    yield await emit({"type": "token", "content": after})
            elif not _think_open_i and "<think>" not in tok:
                yield await emit({"type": "token", "content": tok})
        intent_out = "".join(parts).strip()  # REMOVED: implicit _re.sub() ─ Thinking now explicitly controlled
        yield await emit({"type": "agent_done", "elapsed": round(time.time() - t, 1)})
        _state.memory.add_to_session("user", user_input)
        _state.memory.add_to_session("assistant", intent_out)
        _intent_stop_reason = "aborted" if _aborted() else "completed"
        yield await emit(_done_event(round(time.time() - t_total, 1), _intent_stop_reason, **_collect_done_metrics()))
        _unregister_abort(run_id)
        _unregister_step_skip(run_id)
        return

    if mode == "automap" and not duo_config.agentic_mode:
        available   = list(S_models_cache) if S_models_cache else list(
            set(a.model for a in _state.pipeline.agents.values())
        )
        if _prepro_success:
            vision_type = None
            _automap_has_images = False
        else:
            vision_type = detect_vision_need(images, user_input)
            _automap_has_images = bool(images)

        am = get_automap(
            user_input, available,
            has_images=_automap_has_images,
            task_type_override=vision_type,
            base_path=THIS_FILE,
            vram_budget_gb=float(settings.get("vram_budget_gb") or DEFAULT_VRAM_BUDGET_GB),
        )
        _automap_excluded = set(settings.get("automap_excluded", []))
        _automap_applied  = {}
        for agent_key, mdl in am["assignments"].items():
            if agent_key in _state.pipeline.agents and agent_key not in _automap_excluded:
                registry_set(agent_key, mdl)
                _automap_applied[agent_key] = mdl
            else:
                _automap_applied[agent_key] = registry_get(agent_key)
        yield await emit({
            "type":        "automap",
            "task_type":   am["task_type"],
            "assignments": _automap_applied,
            "reasoning":   am.get("reasoning", ""),
        })

    if _judge_prefetch_task and not _judge_prefetch_task.done():
        try:
            await asyncio.wait_for(asyncio.shield(_judge_prefetch_task), timeout=0.2)
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass

    # Mode decision (with force_complexity + judge_bias support)
    _complexity_source = "judge"  # Default
    _judge_verdict: dict = {}     # VollstÃ¤ndiges Judge-Urteil (route, task_type, tool_model)
    _effective_task_type = _task_type

    logger.warning("[RUN-TRACE] vor mode-decision")
    logger.warning("[AGENTIC-RULE] mode=%s agentic=%s use_pipeline=%s force=%s",
                   mode, duo_config.agentic_mode, duo_config.use_pipeline, force_complexity)
    if duo_config.agentic_mode and mode in ("auto", "automap"):
        complexity = "code_duo"
        _complexity_source = "agentic"
        yield await emit({"type": "status",
            "content": "Agentic mode active ─ direct to Pre-Explore/Planner/Coder (no judge routing)"})
    elif force_complexity in ("simple", "complex"):
        complexity = force_complexity
        _complexity_source = "manual"
        yield await emit({"type": "status", "content": f">> complexity manually: {complexity.upper()}"})
    elif mode == "simple":
        complexity = "simple"
        _complexity_source = "mode"
    elif mode == "pipeline":
        complexity = "complex"
        _complexity_source = "mode"
        if images and not _prepro_success:
            _effective_task_type = "vision"
    elif mode == "agent":
        # Agent mode: tool-call loop via /v1/chat endpoint
        complexity = "simple"
        _complexity_source = "mode"
    elif mode == "code_duo":
        complexity = "code_duo"
        _complexity_source = "mode"
    elif mode == "automap":
        if images and not _prepro_success:
            complexity = "complex"
            _complexity_source = "image"
        else:
            _word_count = len(user_input.split())
            _va_wants_pipeline = (settings.get("vision_agent_enabled", False) and bool(images))
            if _prepro_success and _word_count <= 12 and _task_type in (
                    "general", "creative", "factual", "reasoning") and not _va_wants_pipeline:
                complexity = "simple"
                _complexity_source = "shortcut"
                _effective_task_type = "factual"
            elif not _prepro_success and _task_type in ("general", "creative") and _word_count <= 6 and not _va_wants_pipeline:
                complexity = "trivial"
                _complexity_source = "shortcut"
            else:
                yield await emit({"type": "status", "content": "Automap: checking complexity..."})
                complexity = await _check_complexity_with_bias(user_input, judge_bias)
                _judge_verdict = getattr(_state.pipeline, "_last_judge_verdict", {})
                _complexity_source = "judge"
    else:
        if images and not _prepro_success:
            complexity = "simple"
            _effective_task_type = "vision"
            _complexity_source = "image_fallback"
        else:
            _word_count = len(user_input.split())
            _va_wants_pipeline = (settings.get("vision_agent_enabled", False) and bool(images))
            if _task_type in ("general", "creative") and _word_count <= 10 and not _va_wants_pipeline:
                complexity = "trivial"
                _complexity_source = "shortcut"
            else:
                yield await emit({"type": "status", "content": "Assessing complexity..."})
                complexity = await _check_complexity_with_bias(user_input, judge_bias)
                _judge_verdict = getattr(_state.pipeline, "_last_judge_verdict", {})
                _complexity_source = "judge"

    _judge_route    = _judge_verdict.get("route", "")           # "direct"|"tool"|"pipeline"|""
    _judge_tasktype = _judge_verdict.get("task_type", "") or _task_type
    _judge_toolsize = _judge_verdict.get("tool_model", "small")  # "small"|"large"

    if _judge_route == "pipeline" and complexity not in ("complex",):
        complexity = "complex"

    if _judge_route == "duo" and mode not in ("simple", "pipeline", "code_duo"):
        complexity = "code_duo"
        _complexity_source = "judge"
        intent_agent = None
        if len(_state.memory.get_session_messages()) > _sess_compress_threshold:
            _pipeline_sess_msgs = _state.memory.get_session_messages()  # fresh, uncompressed

    if _judge_verdict:
        _effective_task_type = _judge_tasktype  # Judge knows task_type from full analysis

    # LEARNED-ROUTE: consult routing weights for data-driven mode selection.
    # Only overrides if enough samples exist (N >= 20) and confidence >= 0.70.
    _routing_hint = get_routing_suggestion(_task_type)
    if _routing_hint is not None and _routing_hint.get("confidence", 0) >= 0.70:
        _preferred = _routing_hint["preferred_mode"]
        _mapping = {"duo": "code_duo", "pipeline": "complex", "direct": "simple"}
        _new_complexity = _mapping.get(_preferred)
        if _new_complexity and _new_complexity != complexity:
            complexity = _new_complexity
            _complexity_source = "learned"
            logging.getLogger("routing").debug(
                "[LEARNED-ROUTE] task=%s -> %s (confidence=%.2f)",
                _task_type, _new_complexity, _routing_hint["confidence"],
            )
            yield await emit({"type": "status",
                "content": f"Learned routing: {_preferred} mode for '{_task_type}' (confidence {_routing_hint['confidence']:.0%})"})

    # DIRECT-CHAT-TOOLS (2026-08-31): when the direct chat has tools enabled,
    # tool-y requests are handled by the direct tool loop (run_direct) instead
    # of the separate tool-agent path — one unified chat context/session.
    _direct_tools_route = bool(settings.get("direct_tools_enabled", True))
    _route_as_tool = (
        ((_judge_route == "tool") or (not _judge_verdict and detect_tool_request(user_input)))
        and mode not in ("simple", "pipeline", "agent")
        and complexity != "code_duo"
        and not _direct_tools_route
    )
    if _direct_tools_route and _judge_route == "tool" and mode in ("auto", "automap") \
            and complexity != "code_duo":
        complexity = "simple"
        _complexity_source = "direct_tools"

    if _route_as_tool:
        _available = list(S_models_cache) if S_models_cache else []
        # small: simple file reads, git ─ qwen2.5:3b suffices
        if _judge_toolsize == "large":
            _tool_priority = ["qwen3.5:4b", "qwen3.5:9b-ud", "qwen3.5:2b", "granite-4.1:3b"]
        else:
            _tool_priority = ["qwen3.5:2b", "granite-4.1:3b", "qwen3.5:4b", "qwen3.5:9b-ud"]
        _tool_model = next((m for m in _tool_priority if m in _available),
                           registry_get("direct"))
        _tool_ctx_override = max(_get_num_ctx(_tool_model) or 4096, 8192)
        _tool_opts: dict = {"temperature": 0.1, "num_predict": 1200, "num_ctx": _tool_ctx_override}
        _tool_sys = (
            "Use the available tools to complete file and code operations. Be direct and concise.\n\n"
            "Available tools:\n"
            "  read_file(path, start_line?, end_line?) ─ read file (optional: line range only)\n"
            "  get_signatures(path, max_items?) ─ structural overview with line numbers\n"
            "  find_files(pattern, path?)  ─ glob search: '**/*.py', 'src/*.ts'\n"
            "  list_dir(path)              ─ list directory contents\n"
            "  search_code(pattern, path?) ─ regex search across code files\n"
            "  patch_file(path, old_str, new_str) ─ surgical edit (preferred for modifications)\n"
            "  edit_file(path, edits)      ─ create new files OR modify existing ones\n"
            "  run_bash(cmd)               ─ shell command: tests, pip install, npm run, grep...\n"
            "  run_python(code)            ─ execute python snippet directly\n"
            "  git_status(cmd)             ─ git: status|diff|log|show\n"
            "  git_commit(message)         ─ commit all changes with message\n\n"
            "Workflow:\n"
            "  1. Explore first (list_dir / find_files / read_file)\n"
            "  2. Modify existing files with edit_file or patch_file\n"
            "  3. After implementing, run tests (run_bash pytest / npm test)\n"
            "  4. On failure: patch_file ─ test again\n"
            "  Do not ask for confirmation ─ just act."
        )
        _tool_msgs = _make_messages(_state.pipeline, _tool_sys, user_input, effective_images, False, False)
        yield await emit({"type": "complexity", "content": "simple", "source": "tool"})
        yield await emit({"type": "agent", "content": "Tool Agent", "model": _tool_model})
        _tool_t = time.time()
        _tool_loop_msgs = list(_tool_msgs)
        _tool_out_parts: list[str] = []
        _tool_max_rounds = 6
        _tool_calls: list = []
        _tool_round = 0
        _tool_stop_reason = "completed"
        _tool_ws = bool(settings.get("duo_websearch_enabled", False)) and _WEBSEARCH_AVAILABLE
        _active_tools = _get_inline_tools(include_websearch=_tool_ws, mode="tool_agent")
        # Timeout strategy:
        _tool_read_timeout_s = float(settings.get("duo_llm_slow_timeout_s", 300))
        _tool_http_timeout = _make_httpx_timeout(read_s=_tool_read_timeout_s)
        try:
            from backend.llama_server_manager import manager as _lsm
            _tool_port = await _lsm.ensure_loaded(_tool_model, num_ctx=_tool_opts.get("num_ctx", 4096))
            async with httpx.AsyncClient(timeout=_tool_http_timeout) as _tc:
                from core.tool_loop import ToolLoop, ToolLoopConfig
                _loop = ToolLoop(
                    config=ToolLoopConfig(
                        stream=False, max_rounds=_tool_max_rounds, max_post_attempts=1,
                        model=_tool_model, temperature=_tool_opts.get("temperature", 0.2),
                        max_tokens=_tool_opts.get("num_predict", 1200),
                        num_ctx=_tool_opts.get("num_ctx", 4096),
                        tools=_active_tools, tool_mode="tool_agent",
                        include_websearch=_tool_ws,
                        read_timeout_s=_tool_read_timeout_s,
                    ),
                    http_client=_tc, port=_tool_port,
                    workspace=_ws_str,
                    abort_check=_aborted,
                )
                async for _ev in _loop.run(_tool_loop_msgs):
                    if _ev["type"] == "token":
                        _tool_out_parts.append(_ev["content"])
                    yield await emit(_ev)
                _tool_stop_reason = _loop.state.stop_reason
                _tool_calls = bool(_loop.state.tool_calls_made)
        except httpx.TimeoutException as _te:
            _elapsed_so_far = round(time.time() - _tool_t, 1)
            _err = f"[Tool timeout after {_elapsed_so_far}s ─ model did not respond in time. try a smaller model or simplify the request.]"
            yield await emit({"type": "token", "content": _err})
            _tool_out_parts.append(_err)
            _tool_stop_reason = "timeout"
        except Exception as _te:
            _err = f"[Tool error: {type(_te).__name__}: {str(_te)[:200]}]"
            yield await emit({"type": "token", "content": _err})
            _tool_out_parts.append(_err)
            _tool_stop_reason = "error"
        else:
            if _tool_stop_reason == "max_tool_rounds" and _tool_calls and not _aborted():
                _limit_msg = f"\n[Max. {_tool_max_rounds} tool rounds reached]"
                yield await emit({"type": "token", "content": _limit_msg})
                _tool_out_parts.append(_limit_msg)
            elif _aborted():
                _tool_stop_reason = "aborted"
        _tool_out = "".join(_tool_out_parts)
        yield await emit({"type": "agent_done", "elapsed": round(time.time() - _tool_t, 1)})
        _state.memory.add_to_session("user", user_input)
        _state.memory.add_to_session("assistant", _tool_out)
        yield await emit(_done_event(round(time.time() - t_total, 1), _tool_stop_reason, **_collect_done_metrics()))
        _unregister_abort(run_id)
        _unregister_step_skip(run_id)
        return

    # ──────────────────────────────────────────────────────────────────────────

    def _build_run_context():
        """Build RunContext with all shared state for extracted runners."""
        return RunContext(
            user_input=user_input,
            images=images,
            mode=mode,
            iterations=iterations,
            active_preset=active_preset,
            constraint_mode=constraint_mode,
            force_complexity=force_complexity,
            skip_agents=skip_agents,
            judge_bias=judge_bias,
            duo_config=duo_config,
            chat_id=chat_id,
            run_id=run_id,
            t_total=t_total,
            complexity=complexity,
            complexity_source=_complexity_source,
            effective_task_type=_effective_task_type,
            use_learned=use_learned,
            workspace=_ws_str,
            prepro_success=_prepro_success,
            image_description=image_description,
            effective_images=effective_images,
            vision_cfg=_vision_cfg,
            models_cache=S_models_cache,
            pipeline=_state.pipeline,
            settings=_run_settings,
            memory=_state.memory,
            registry=_registry,
            vram_budget=_vram_budget,
            vram_cache=_vram_cache,
            websearch_available=_WEBSEARCH_AVAILABLE,
            pipeline_mem_ctx=_pipeline_mem_ctx,
            pipeline_sess_msgs=_pipeline_sess_msgs,
            pipeline_soul=_pipeline_soul,
            exec_ctrl=_exec_ctrl,
            agent_elapsed=_agent_elapsed,
            prefetch_state=_prefetch_state,
            prefetch_run_active=_prefetch_run_active,
            this_file=THIS_FILE,
            simple_direct_models=_SIMPLE_DIRECT_MODELS,
            complex_direct_models=_COMPLEX_DIRECT_MODELS,
            vram_lookup_gb=_VRAM_LOOKUP_GB,
            # Callables (inner functions)
            emit=emit,
            done_event=_done_event,
            aborted=_aborted,
            step_skipped=_step_skipped,
            maybe_preload=_maybe_preload,
            maybe_trigger_soul_evolution=_maybe_trigger_soul_evolution,
            auto_memory_from_input=_auto_memory_from_input,
            pick_direct_model=_pick_direct_model,
            model_profile=_model_profile,
            get_num_ctx=_get_num_ctx,
            bk_load=_bk_load,
            bk_pin=_bk_pin,
            bk_evict=_bk_evict,
            safe_web_search=_safe_web_search,
            extract_ws_query=_extract_ws_query,
            make_messages=_make_messages,
            refresh_judge_keepalive=_refresh_judge_keepalive,
            increment_run_counter=_increment_run_counter,
            collect_done_metrics=_collect_done_metrics,
            unregister_abort=_unregister_abort,
            unregister_step_skip=_unregister_step_skip,
            schedule_prefetch=_schedule_prefetch,
            pipeline_chat_stream=_pipeline_chat_stream,
            get_loaded_models_set=_get_loaded_models_set,
            smart_preload_if_needed=smart_preload_if_needed,
            clear_step_skip=lambda *a: _clear_step_skip(run_id),
            update_prefetch_lead=_update_prefetch_lead,
            flush_prefetch_settings=_flush_prefetch_settings,
            register_abort=_register_abort,
            register_step_skip=_register_step_skip,
            prefetch_judge_once=_prefetch_judge_once,
            do_prefetch=_do_prefetch,
            runtime_telemetry_snapshot=_runtime_telemetry_snapshot,
            runtime_delta=_runtime_delta,
            vision_agent_prompt=VISION_AGENT_PROMPT,
            phase_timer=_phase_timer,
            resolve_duo_runtime_profile=_resolve_duo_runtime_profile,
            resolve_duo_run_timeout_seconds=_resolve_duo_run_timeout_seconds,
            build_soul_prompt_layer=build_soul_prompt_layer,
            # Additional module-level functions
            registry_get=registry_get,
            get_effective_prompt_with_override=get_effective_prompt_with_override,
            run_peer_ratings=run_peer_ratings,
            run_soul_cycle=run_soul_cycle,
            get_effective_config=get_effective_config,
            is_aborted_global=_aborted,
            is_aborted_chat=_is_aborted,
            clear_resume_block=_clear_resume_block,
            run_insight_extractor=_run_insight_extractor,
            run_skill_distillation=_run_skill_distillation,
            resolve_resume_block=_resolve_resume_block,
            load_resume_block=_load_resume_block,
            resolve_duo_rounds_cap=_resolve_duo_rounds_cap,
            append_learning_log=append_learning_log,
        )

    # ─── Route to extracted runner ───
    _ctx = _build_run_context()
    logger.warning("[RUN-TRACE] vor runner-dispatch complexity=%s", _ctx.complexity)
    async for event in run_stream_orchestrated(_ctx):
        if isinstance(event, dict) and event.get("type") == "ctx_meter":
            _est = int(event.get("est_tokens") or 0)
            _lim = int(event.get("ctx_limit") or 0)
            if _est > 0:
                _ctx_peak_tokens = max(_ctx_peak_tokens, _est)
                _ctx_limit_seen = max(_ctx_limit_seen, _lim)
                if _lim > 0:
                    _ctx_pressure_peak = max(_ctx_pressure_peak, _est / _lim)
        yield event
    return
