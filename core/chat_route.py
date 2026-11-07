"""Chat-run intent routing pre-block (2026-11-06 split 3.4).

Intent-agent detection and its early-exit handling, task-type
detection, the automap routing suggestion and the judge-prefetch wait
at the top of the mode decision. Verbatim code motion — no logic
changes. The intent-agent early exit sets `ps.handled = True` and
returns; the runner does the return.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import cast
import time

from core import state as _state
from core.duo_helpers import DEFAULT_VRAM_BUDGET_GB
from context.chat_util import _make_messages
from core.state import registry_get, registry_set, settings
from hive_functions.prompts import AGENT_ROLES

logger = logging.getLogger("hivemind.chat_run")


@dataclasses.dataclass
class _ChatRoutePreState:
    """Intent-routing pre-block state (split 3.4); `handled` is the
    early-exit signal, `carry` holds the runner closures and the judge
    prefetch task."""
    _aborted: object = None
    _judge_prefetch_task: object = None
    _pipeline_mem_ctx: object = None
    _pipeline_sess_msgs: object = None
    _step_skipped: object = None
    active_preset: object = None
    duo_config: object = None
    emit: object = None
    force_complexity: object = None
    image_description: object = None
    images: object = None
    mode: object = None
    run_id: object = None
    t_total: object = None
    use_learned: object = None
    user_input: object = None
    _done_event: object = None
    _collect_done_metrics: object = None
    _complexity_source: str = ""
    _effective_task_type: str = ""
    _judge_verdict: dict = dataclasses.field(default_factory=dict)
    _prepro_success: bool = False
    _task_type: str = ""
    handled: bool = False


async def _chat_route_pre(ps):
    from infra.run_control import _unregister_abort, _unregister_step_skip
    from routing.agent_intent import (
        detect_agent_intent, get_question_from_intent,
    )
    from routing.model_automap import detect_task_type
    from routing.model_automap import get_automap
    from server import (
        S_models_cache, THIS_FILE,
        _vision_cfg, get_effective_prompt_with_override,
    )
    from core.llm_stream import _pipeline_chat_stream
    _aborted = ps._aborted
    detect_vision_need = _state.detect_vision_need
    _judge_prefetch_task = ps._judge_prefetch_task
    _pipeline_mem_ctx = ps._pipeline_mem_ctx
    _pipeline_sess_msgs = ps._pipeline_sess_msgs
    _step_skipped = ps._step_skipped
    active_preset = ps.active_preset
    duo_config = ps.duo_config
    emit = ps.emit
    force_complexity = ps.force_complexity
    image_description = ps.image_description
    images = ps.images
    mode = ps.mode
    run_id = ps.run_id
    t_total = ps.t_total
    use_learned = ps.use_learned
    user_input = ps.user_input
    _done_event = ps._done_event
    _collect_done_metrics = ps._collect_done_metrics
    _complexity_source = ps._complexity_source
    _effective_task_type = ps._effective_task_type
    _judge_verdict = ps._judge_verdict
    _prepro_success = ps._prepro_success
    _task_type = ps._task_type

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
                yield await emit({"type": "status", "content": "-- Pre-Explore skipped."})
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
        intent_out = "".join(parts).strip()  # REMOVED: implicit _re.sub() - Thinking now explicitly controlled
        yield await emit({"type": "agent_done", "elapsed": round(time.time() - t, 1)})
        _state.memory.add_to_session("user", user_input)
        _state.memory.add_to_session("assistant", intent_out)
        _intent_stop_reason = "aborted" if _aborted() else "completed"
        yield await emit(_done_event(round(time.time() - t_total, 1), _intent_stop_reason, **_collect_done_metrics()))
        _unregister_abort(run_id)
        _unregister_step_skip(run_id)
        ps.handled = True
        return

    if mode == "automap" and not duo_config.agentic_mode:
        available   = list(S_models_cache) if S_models_cache else list(
            set(a.model for a in _state.pipeline.agents.values())
        )
        vision_type: str | None
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
        except (OSError, RuntimeError, ValueError, TypeError) as _jw_err:
            logger.debug("[JUDGE-PREFETCH] wait failed: %s", _jw_err)

    # Mode decision (with force_complexity + judge_bias support)
    _complexity_source = "judge"  # Default
    _judge_verdict: dict = {}     # VollstÃƒÂ¤ndiges Judge-Urteil (route, task_type, tool_model)
    _effective_task_type = _task_type

    logger.warning("[RUN-TRACE] vor mode-decision")
    logger.warning("[AGENTIC-RULE] mode=%s agentic=%s use_pipeline=%s force=%s",
                   mode, duo_config.agentic_mode, duo_config.use_pipeline, force_complexity)

    # ── sync back: everything the runner consumes after this block ──
    ps._complexity_source = _complexity_source
    ps._effective_task_type = _effective_task_type
    ps._judge_verdict = _judge_verdict
    ps._prepro_success = _prepro_success
    ps._task_type = _task_type
