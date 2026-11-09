"""Plan-tracker round policy (F13, 2026-11-08).

The per-round PlanTracker update was the largest remaining inline block in
`run_code_duo`: advance, tick, deviation/replan ladder, critical-stall
escalation and the one-shot replan bonus. Moved VERBATIM behind the
established state seam; the tracker and message list are mutated in place,
the runner keeps the `continue` and syncs the five scalars in a finally.
"""
from __future__ import annotations

import dataclasses
import logging

from hive_functions.prompts import PROMPTS

logger = logging.getLogger("hivemind.duo")


@dataclasses.dataclass
class _DuoPlanRoundState:
    """In/out scalars of one plan-tracker round."""
    replan_count: int = 0
    replan_bonus_granted: bool = False
    max_tool_rounds: int = 0
    accumulated_replan_bonus: int = 0
    action: str = "none"             # "continue" = replan cap hit


async def _duo_plan_tracker_round(
    ctx, tracker, *, last_tool_name, last_tool_result, file_changes,
    written_files, dtool_msgs, max_tool_rounds_cfg,
    ps: _DuoPlanRoundState,
):
    _replan_count = ps.replan_count
    _replan_bonus_granted = ps.replan_bonus_granted
    _max_tool_rounds = ps.max_tool_rounds
    _accumulated_replan_bonus = ps.accumulated_replan_bonus
    _action = ps.action
    try:
        _touched = list((file_changes or {}).keys())
        if last_tool_name and tracker.should_advance(
                last_tool_name, last_tool_result,
                touched_paths=_touched):
            tracker.advance(last_tool_name)
            logger.info(
                "[PlanTracker] Step advanced after '%s' -> step %d/%d",
                last_tool_name,
                tracker._current_step_index() + 1,
                tracker.total,
            )
        tracker.tick(
            tool_name=last_tool_name,
            tool_result=last_tool_result,
            touched_paths=_touched,
            file_changes_count=len(file_changes or {}),
        )
        if tracker.needs_replan():
            _replan_count += 1
            if _replan_count >= 4:
                tracker._stall_ticks += 1
                if tracker._stall_ticks >= 6:
                    yield await ctx.emit({
                        "type": "status",
                        "content": (
                            "⛔ [CRITICAL STALL] Maximum replans reached, "
                            "no progress detected for 6+ rounds. "
                            "Call task_complete(status='blocked', reason='max_replans_exhausted')."
                        ),
                    })
                    dtool_msgs.append({"role": "user", "content": (
                        "[CRITICAL STALL] All replans exhausted without progress. "
                        "Call task_complete(status='blocked', reason='max_replans_exhausted') "
                        "immediately. Do NOT read or write files."
                    )})
                else:
                    yield await ctx.emit({
                        "type": "status",
                        "content": (
                            f"⛔ Replan cap reached ({_replan_count}/4) — "
                            f"tracking stalls ({tracker._stall_ticks}/6). "
                            "No further replan directives."
                        ),
                    })
                tracker._initial_read_phase = False
                tracker._plan.deviation.status = "NONE"
                tracker._plan.deviation.streak = 0
                tracker._plan.deviation.reasons.clear()
                _action = "continue"
                return
            _replan_msg = tracker.replan_prompt(
                written_files=written_files,
                file_changes=file_changes,
            )
            _streak = tracker._plan.deviation.streak
            logger.warning(
                "[PlanTracker] Deviation streak %d -- replan triggered. Last tool: '%s'",
                _streak,
                last_tool_name,
            )
            yield await ctx.emit({
                "type": "status",
                "content": (
                    f"⚠️ Plan deviation streak {_streak}/3 — "
                    "injecting replan directive"
                ),
            })
            if not _replan_bonus_granted:
                _replan_bonus_granted = True
                _bonus = min(4, max_tool_rounds_cfg // 4)
                _max_tool_rounds = min(_max_tool_rounds + _bonus, max_tool_rounds_cfg)
                _accumulated_replan_bonus += _bonus
                logger.info("[REPLAN] Granted %d extra rounds. New budget: %d",
                            _bonus, _max_tool_rounds)
            if dtool_msgs and dtool_msgs[-1].get("role") == "tool":
                _replan_directive = PROMPTS.get("duo_coder_replan", "")
                if _replan_directive:
                    dtool_msgs.append({
                        "role": "user",
                        "content": _replan_directive,
                    })
                dtool_msgs.append({
                    "role": "user",
                    "content": _replan_msg,
                })
            # Reset deviation state so the tracker does not re-trigger on the
            # next round; real deviations re-emerge naturally.
            tracker._plan.deviation.status = "NONE"
            tracker._plan.deviation.streak = 0
            tracker._plan.deviation.reasons.clear()
            tracker._initial_read_phase = True
            tracker._initial_reads = 0
            ctx.exec_ctrl.reset_stuck_detection()
        else:
            _p1_reminder = tracker.reminder()
            if _p1_reminder:
                logger.info(
                    "[PlanTracker] Reminder: %s",
                    (_p1_reminder[:120] + '...') if len(_p1_reminder) > 120 else _p1_reminder,
                )
            if (_p1_reminder
                    and dtool_msgs
                    and dtool_msgs[-1].get("role") == "tool"
                    and not tracker.is_finished):
                dtool_msgs.append({
                    "role": "user",
                    "content": _p1_reminder.strip(),
                })
    finally:
        ps.replan_count = _replan_count
        ps.replan_bonus_granted = _replan_bonus_granted
        ps.max_tool_rounds = _max_tool_rounds
        ps.accumulated_replan_bonus = _accumulated_replan_bonus
        ps.action = _action
