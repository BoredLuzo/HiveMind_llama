"""Duo inloop planner (2026-11-05 split 2.6).

The per-round thinking gate (_inloop_plan_should_run) and the inloop
planner execution: model port, thinking budget, event-bridge queue,
plan-tracker rebuild, the plan-for-coder distillation into _dtool_msgs
and the fallback ladder. Verbatim code motion — no logic changes.
Outputs flow back through `ps` (_run_thinking_now for the PLAN-TO-EXEC
log, _inloop_plan_text for the session epilogue carry).
"""
from __future__ import annotations

import asyncio
import httpx
import logging
import time
from functools import partial

from hive_functions.loop_machine import AgentState
from hive_functions.planner import (
    fallback_planner_steps, make_thinking_planner_sys, run_inloop_planner,
)
from core.duo.preflight import _distilled_execution_steps, inloop_plan_emit_fn
from core.duo_helpers import (
    _calculate_thinking_tokens, inloop_plan_should_run as _inloop_plan_should_run,
)

logger = logging.getLogger("hivemind.duo")

# runner aliases kept for verbatim call names
_make_duo_thinking_planner_sys = make_thinking_planner_sys
_fallback_planner_steps = fallback_planner_steps


async def _duo_inloop_planner(ctx, ps):
    exec_mdl = ps.carry["exec_mdl"]
    coder_mdl = ps.carry["coder_mdl"]
    _planner_step_cap = ps.carry["_planner_step_cap"]
    _exec_has_thinking = ps.carry["_exec_has_thinking"]
    _fup_task_ctx = ps.carry["_fup_task_ctx"]
    _has_plan = ps.carry["_has_plan"]
    _duo_deadline_at = ps.carry.get("_duo_deadline_at")
    _duo_run_timeout_s = ps.carry.get("_duo_run_timeout_s")
    _dtool_msgs = ps._dtool_msgs
    _dtool_ctx = ps._dtool_ctx
    _coder_input = ps._coder_input
    _explore_ctx = ps._explore_ctx
    _plan_tracker = ps._plan_tracker
    _is_first_outer_round = ps._is_first_outer_round
    _goal_summary = ps._goal_summary
    _duo_timed_out = ps._duo_timed_out
    _inloop_plan_text = ""

    # ctx.duo_config.thinking_per_chunk=True: Thinking in EVERY chunk.
    # New:
    #   - ctx.duo_config.thinking_per_chunk=True → thinking before EVERY chunk (chunking active)
    # PLAN-DUP-GUARD (2026-11-03): the gate now also requires that
    # this run has NO planner plan yet (_has_plan: subtasks /
    # briefing / plan_content from the planner phase or a resume).
    # The old gate only checked the planner TOGGLE, so
    # chunking=True + planner=False + UI-forced agentic_thinking
    # ran the planner phase AND then asked the coder model to
    # produce a SECOND plan in the planner's markdown format
    # ("## Plan: / ### Open Tasks") via run_inloop_planner —
    # streamed as visible answer text, not reasoning.
    _run_thinking_now = _inloop_plan_should_run(
        ctx.duo_config.agentic_thinking,
        ctx.duo_config.thinking_per_chunk,
        ctx.aborted(),
        ctx.duo_config.planner,
        _has_plan,
        chunking_enabled=ctx.duo_config.chunking,
    )
    if _run_thinking_now:
        ctx.exec_ctrl.transition(AgentState.PLANNING)
        ctx.exec_ctrl.reset_stuck_detection()  # BUG-NO-RESET FIX
        yield await ctx.emit({"type": "status",
            "content": f"🧠 Planning ({coder_mdl.split(':')[0]}, Thinking)…"})
        # OUTSOURCED: Inloop planner delegated to run_inloop_planner().
        _plan_seed_task = _coder_input or ctx.user_input
        if _is_first_outer_round and _fup_task_ctx:
            _plan_seed_task = (
                _plan_seed_task
                + "\n\n[Previous task in this chat — context for this follow-up]\n"
                + _fup_task_ctx
            )
        _plan_seed_ctx = _explore_ctx if _is_first_outer_round else ""
        _plan_port_inloop = None
        try:
            from backend.llama_server_manager import manager as _lsm3
            _plan_port_inloop = await _lsm3.ensure_loaded(exec_mdl, num_ctx=_dtool_ctx, n_parallel=1, ctx_graceful=False, pin=True)
        except (OSError, RuntimeError, ValueError, TypeError) as _port_err:
            logger.warning("Inloop planner: port not available (%s)", _port_err)
        _inloop_plan_text = ""
        _inloop_plan_thinking = ""
        _inloop_plan_fallback = False
        if _plan_port_inloop is not None:
            # Compute thinking budget
            _exec_thinking_budget = _calculate_thinking_tokens(
                exec_mdl,
                ctx.settings,
                input_tokens=len((_make_duo_thinking_planner_sys(_planner_step_cap, model_name=exec_mdl) or "") + (_plan_seed_task or "")) // 3,
                available_ctx=_dtool_ctx,
                agent_name="planner",
            )
            # Event-Bridge: run_inloop_planner emit_fn → asyncio.Queue → yield await ctx.emit
            _inloop_event_q: asyncio.Queue = asyncio.Queue(maxsize=50)
            try:
                _inloop_plan_task = asyncio.create_task(
                    run_inloop_planner(
                        task=_plan_seed_task,
                        explore_ctx=_plan_seed_ctx,
                        planner_model=exec_mdl,
                        planner_port=_plan_port_inloop,
                        step_cap=_planner_step_cap,
                        use_thinking=_exec_has_thinking,
                        thinking_budget=_exec_thinking_budget,
                        planner_ctx=_dtool_ctx,
                        # Hard-cap the content budget; thinking budget comes in planner.py
                        max_output_tokens=min(_dtool_ctx, 4096),
                        websearch_available=ctx.settings.get("duo_websearch_enabled", False) and ctx.websearch_available,
                        settings=ctx.settings,
                        aborted_fn=ctx.aborted,
                        emit_fn=partial(inloop_plan_emit_fn, _inloop_event_q),
                    )
                )
                # Yield events from the bridge while planner runs
                while not _inloop_plan_task.done() or not _inloop_event_q.empty():
                    if _duo_deadline_at and time.monotonic() >= _duo_deadline_at:
                        _duo_timed_out = True
                        logger.warning(
                            "[INLOOP-PLANNER] Run deadline exceeded - planner aborted (run timeout %ss)",
                            _duo_run_timeout_s,
                        )
                        _inloop_plan_task.cancel()
                        # P2 (deep audit 2026-11-03): cancel() is
                        # only scheduled; awaiting the task here
                        # prevents a pending-task leak and makes
                        # result() a clean CancelledError.
                        try:
                            await asyncio.wait_for(
                                _inloop_plan_task, timeout=2.0)
                        except (asyncio.CancelledError,
                                asyncio.TimeoutError):
                            pass
                        break
                    while not _inloop_event_q.empty():
                        try:
                            _evt = _inloop_event_q.get_nowait()
                            yield await ctx.emit(_evt)
                        except asyncio.QueueEmpty:
                            break
                    await asyncio.sleep(0.3)
                try:
                    _inloop_plan_text, _inloop_plan_thinking, _inloop_plan_fallback = _inloop_plan_task.result()
                except (asyncio.CancelledError, asyncio.InvalidStateError):
                    # DEADLINE-CANCEL-RACE (2026-11-03, live): after
                    # cancel() the task is still pending, so
                    # result() raised InvalidStateError and the
                    # broad handler below turned a deadline stop
                    # into "Thinking plan error - fallback plan".
                    _inloop_plan_text = ""
                    _inloop_plan_thinking = ""
                    _inloop_plan_fallback = True
            except (OSError, RuntimeError, ValueError, TypeError, KeyError,
                    IndexError, AttributeError, asyncio.TimeoutError,
                    httpx.HTTPError) as _plan_err:
                _plan_err_name = type(_plan_err).__name__
                _plan_err_msg = (str(_plan_err).strip() or repr(_plan_err))[:220]
                logger.warning(
                    "Thinking-planning step error (%s): %s",
                    _plan_err_name,
                    _plan_err_msg,
                )
                _inloop_plan_text = _fallback_planner_steps(
                    _goal_summary or ctx.user_input,
                    max_steps=max(3, min(6, _planner_step_cap)),
                )
                _inloop_plan_fallback = True
                yield await ctx.emit({"type": "status",
                    "content": (
                        f"⚠ Thinking plan error ({_plan_err_name})"
                        + (f": {_plan_err_msg[:90]}" if _plan_err_msg else "")
                        + " - fallback plan active"
                    )})
        else:
            _inloop_plan_text = _fallback_planner_steps(
                _goal_summary or ctx.user_input,
                max_steps=max(3, min(6, _planner_step_cap)),
            )
            _inloop_plan_fallback = True
            yield await ctx.emit({"type": "status",
                "content": "⚠ Planner: no model port available — fallback plan active"})
        _plan_label = "Plan (fallback)" if _inloop_plan_fallback else "Plan"
        if _inloop_plan_thinking and not _inloop_plan_fallback:
            _plan_label = "Plan (from thinking)" if not _inloop_plan_text else "Plan"

        # PATCH-1: rebuild the plan tracker from inloop planner output.
        if (_plan_tracker is not None
                and _inloop_plan_text
                and not _inloop_plan_fallback):
            try:
                _plan_tracker.rebuild_from_plan(_inloop_plan_text)
                logger.debug("[PlanTracker] Rebuilt from inloop planner: %d steps", _plan_tracker.total)
                logger.info(
                    "[PlanTracker] Inloop planner produced %d parseable step(s) (fallback=%s)",
                    _plan_tracker.total,
                    _inloop_plan_fallback,
                )
            except (GeneratorExit, asyncio.CancelledError,
                    KeyboardInterrupt, SystemExit):
                raise
            except (OSError, RuntimeError, ValueError, TypeError,
                    KeyError, AttributeError, IndexError) as _err:
                logger.debug(
                    "[DUO] Suppressed error at plan-tracker rebuild: %s",
                    _err, exc_info=True
                )

        # PLAN-CONTRACT (2026-11-04): the visible plan stays raw
        # for the UI/phone, but the coder gets concrete steps
        # without planner markdown headings - otherwise it
        # analyzes the plan as if another model wrote it.
        _plan_for_coder = (
            _distilled_execution_steps(None, _inloop_plan_text)
            or _inloop_plan_text)
        _dtool_msgs.append({"role": "assistant",
                            "content": _plan_for_coder})
        _dtool_msgs.append({"role": "user", "content":
            "Good. Now execute this plan step by step using the tools."})
        yield await ctx.emit({"type": "token",
            "content": "\n**" + _plan_label + ":**\n" + _inloop_plan_text + "\n\n"})
        yield await ctx.emit({
            "type": "planner_done",
            "summary": "Planning complete" + (" (fallback)" if _inloop_plan_fallback else " — execution starts"),
        })
        if _inloop_plan_fallback:
            logger.warning(
                "[PlanTracker] Inloop planner fallback active -- no parseable plan from the 35B "
                "(plan_text_len=%d, plan_thinking_len=%d)",
                len(_inloop_plan_text) if _inloop_plan_text else 0,
                len(_inloop_plan_thinking) if _inloop_plan_thinking else 0,
            )

    # ── sync back: everything the runner consumes after this block ──
    ps._run_thinking_now = _run_thinking_now
    ps._inloop_plan_text = _inloop_plan_text
    ps._dtool_msgs = _dtool_msgs
    ps._duo_timed_out = _duo_timed_out
