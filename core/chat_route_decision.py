"""Chat-run route decision (2026-11-06 split 3.5).

The mode decision tail: the auto-decider (complexity bias + persisted
agentic flag guard), the judge-route application (pipeline downgrade,
duo route with the session re-window), the learned routing hint and
the direct-tools route decision. Verbatim code motion — no logic
changes. Outputs flow back through `ps` (complexity/_route_as_tool
drive the dispatch below).
"""
from __future__ import annotations

import dataclasses
import logging

from core import state as _state
from routing import decider as _routing_decider
from core.state import settings
from utils.token import window_by_budget

logger = logging.getLogger("hivemind.chat_run")


@dataclasses.dataclass
class _ChatRouteDecisionState:
    """Route-decision state (split 3.5); `carry` holds the runner
    closures (_decide_route, emit) and the gen-level inputs."""
    _decide_route: object = None
    _prepro_success: object = None
    _run_settings: object = None
    _sess_compress_threshold: object = None
    _task_type: object = None
    agentic_force: object = None
    duo_config: object = None
    emit: object = None
    force_complexity: object = None
    images: object = None
    mode: object = None
    model_overrides: object = None
    user_input: object = None
    _complexity_source: str = ""
    _effective_task_type: str = ""
    _judge_toolsize: str = ""
    _judge_verdict: dict = dataclasses.field(default_factory=dict)
    _pipeline_sess_msgs: list = dataclasses.field(default_factory=list)
    _route_as_tool: bool = False
    complexity: str = ""
    chat_id: str = ""


async def _chat_route_decision(ps):
    from routing.agent_intent import detect_tool_request, is_simple_tool_prompt
    from routing.model_automap import get_routing_suggestion
    from core.llm_stream import _history_budget_tokens
    _decide_route = ps._decide_route
    _prepro_success = ps._prepro_success
    _run_settings = ps._run_settings
    _sess_compress_threshold = ps._sess_compress_threshold
    _task_type = ps._task_type
    agentic_force = ps.agentic_force
    duo_config = ps.duo_config
    emit = ps.emit
    force_complexity = ps.force_complexity
    images = ps.images
    mode = ps.mode
    model_overrides = ps.model_overrides
    user_input = ps.user_input
    _complexity_source = ps._complexity_source
    _effective_task_type = ps._effective_task_type
    _judge_toolsize = ps._judge_toolsize
    _judge_verdict = ps._judge_verdict
    _pipeline_sess_msgs = ps._pipeline_sess_msgs
    _route_as_tool = ps._route_as_tool
    complexity = ps.complexity

    # AUTO-DECIDER (2026-10-21): the persisted agentic flag must not hijack
    # mode="auto" while the decider is on - that is how a simple question
    # started a coder run. An explicit request (gateway /mode agentic, body
    # field agentic_force) still forces agentic.
    _decider_on = _routing_decider.normalise_mode(
        (model_overrides or {}).get("routing_decider") or settings.get("routing_decider")
    ) != "off"
    # A3 (2026-11-07): a persisted agentic flag must not turn a simple
    # read/list prompt into a Code Duo run. Narrow and coding-verb guarded;
    # an explicit agentic_force (gateway /mode agentic) still wins.
    _simple_tool = is_simple_tool_prompt(user_input)
    if duo_config.agentic_mode and mode in ("auto", "automap") and (agentic_force or not _decider_on):
        if _simple_tool and not agentic_force:
            complexity = "simple"
            _complexity_source = "tool_intent"
            yield await emit({"type": "status",
                "content": "Simple tool request - direct tools (Code Duo is reserved for coding tasks)"})
        else:
            complexity = "code_duo"
            _complexity_source = "agentic"
            yield await emit({"type": "status",
                "content": "Agentic mode active - direct to Pre-Explore/Planner/Coder (no judge routing)"})
    elif force_complexity in ("simple", "complex"):
        complexity = force_complexity
        _complexity_source = "manual"
        yield await emit({"type": "status", "content": f">> complexity manually: {complexity.upper()}"})
    elif mode in ("simple", "chat"):
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
            _decider_active = _routing_decider.normalise_mode(
                _run_settings.get("routing_decider")) != "off"
            if (not _decider_active and _prepro_success and _word_count <= 12
                    and _task_type in ("general", "creative", "factual", "reasoning")
                    and not _va_wants_pipeline):
                complexity = "simple"
                _complexity_source = "shortcut"
                _effective_task_type = "factual"
            elif (not _decider_active and not _prepro_success
                    and _task_type in ("general", "creative") and _word_count <= 6
                    and not _va_wants_pipeline):
                complexity = "trivial"
                _complexity_source = "shortcut"
            else:
                yield await emit({"type": "status", "content": "Automap: checking complexity..."})
                _decision = await _decide_route(user_input)
                complexity = _routing_decider.complexity_for(_decision)
                _judge_verdict = dict(_decision.details.get('judge_verdict') or {})
                _complexity_source = _decision.source
                # ROUTING-VISIBILITY (2026-10-22, user request): the phone
                # should see WHAT the decider decided, not just "auto".
                yield await emit({"type": "status", "content": (
                    f"🧭 Routing: {_decision.route} "
                    f"({_decision.source}, conf {_decision.confidence:.2f})"
                    + (f" · planner: yes" if _decision.planner else ""))})
    else:
        if images and not _prepro_success:
            complexity = "simple"
            _effective_task_type = "vision"
            _complexity_source = "image_fallback"
        else:
            _word_count = len(user_input.split())
            _va_wants_pipeline = (settings.get("vision_agent_enabled", False) and bool(images))
            _decider_active = _routing_decider.normalise_mode(
                _run_settings.get("routing_decider")) != "off"
            if (not _decider_active and _task_type in ("general", "creative")
                    and _word_count <= 10 and not _va_wants_pipeline):
                complexity = "trivial"
                _complexity_source = "shortcut"
            else:
                yield await emit({"type": "status", "content": "Assessing complexity..."})
                _decision = await _decide_route(user_input)
                complexity = _routing_decider.complexity_for(_decision)
                _judge_verdict = dict(_decision.details.get('judge_verdict') or {})
                _complexity_source = _decision.source
                # ROUTING-VISIBILITY (2026-10-22, user request): the phone
                # should see WHAT the decider decided, not just "auto".
                yield await emit({"type": "status", "content": (
                    f"🧭 Routing: {_decision.route} "
                    f"({_decision.source}, conf {_decision.confidence:.2f})"
                    + (f" · planner: yes" if _decision.planner else ""))})
    # A3 (2026-11-07): the decider may classify a short "list files" prompt
    # as agentic/code_duo too. Keep the same narrow tool-intent override;
    # explicit modes and force_complexity are untouched.
    if (_simple_tool and complexity == "code_duo"
            and mode in ("auto", "automap") and not agentic_force):
        complexity = "simple"
        _complexity_source = "tool_intent"
        yield await emit({"type": "status",
            "content": "Simple tool request - direct tools (Code Duo is reserved for coding tasks)"})

    _judge_route    = _judge_verdict.get("route", "")           # "direct"|"tool"|"pipeline"|""
    _judge_tasktype = _judge_verdict.get("task_type", "") or _task_type
    _judge_toolsize = _judge_verdict.get("tool_model", "small")  # "small"|"large"

    if _judge_route == "pipeline" and complexity not in ("complex",):
        complexity = "complex"

    if (_judge_route == "duo" and mode not in ("simple", "pipeline", "code_duo")
            and not _simple_tool):
        complexity = "code_duo"
        _complexity_source = "judge"
        if len(_state.memory.get_session_messages()) > _sess_compress_threshold:
            _pipeline_sess_msgs = window_by_budget(
                _state.memory.get_session_messages(
                    limit=40, user_cap=8000, assistant_cap=8000,
                ),
                _history_budget_tokens(settings),
            )  # fresh, uncompressed

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
    # of the separate tool-agent path -- one unified chat context/session.
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

    # ── sync back: everything the runner consumes after this block ──
    ps._complexity_source = _complexity_source
    ps._effective_task_type = _effective_task_type
    ps._judge_toolsize = _judge_toolsize
    ps._judge_verdict = _judge_verdict
    ps._pipeline_sess_msgs = _pipeline_sess_msgs
    ps._route_as_tool = _route_as_tool
    ps.complexity = complexity
    # D6 OUTCOME HINT (2026-11-09): chat-scoped run_end history can flag a
    # route with a clearly worse success rate. Hint only — never override an
    # explicit mode or the judge; the owner can act on it.
    try:
        if getattr(ps, "chat_id", ""):
            from core.run_audit import load_run_audit
            from routing.outcome_hints import suggest_route
            _oh = suggest_route(load_run_audit(ps.chat_id), complexity)
            if _oh:
                yield await emit({
                    "type": "status",
                    "content": (
                        f"ℹ Outcome hint: {_oh['from']} succeeded "
                        f"{_oh['from_rate']:.0%} ({_oh['runs']['from']} runs), "
                        f"{_oh['to']} {_oh['to_rate']:.0%} "
                        f"({_oh['runs']['to']} runs)."
                    ),
                })
    except (ImportError, OSError, ValueError, TypeError, KeyError):
        pass
