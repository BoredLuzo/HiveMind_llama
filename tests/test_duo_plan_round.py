# -*- coding: utf-8 -*-
"""Behavior test: the plan-tracker round seam (F13, 2026-11-08).

The 111-line PlanTracker block in run_code_duo was moved verbatim into
core/duo_plan_round.py. These checks pin advance/tick, the reminder append,
the one-shot replan bonus, the 4x replan cap, the 6x critical-stall
escalation and the f-string fix for the stall counter.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.duo_plan_round import (  # noqa: E402
    _DuoPlanRoundState, _duo_plan_tracker_round,
)
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


class _Tracker:
    def __init__(self):
        self.total = 5
        self._stall_ticks = 0
        self._initial_read_phase = True
        self._initial_reads = 3
        self._plan = SimpleNamespace(
            deviation=SimpleNamespace(status="NONE", streak=0, reasons=[]))
        self.should = True
        self.needs = False
        self.reminder_text = None
        self.advanced = None
        self.tick_kw = None

    @property
    def is_finished(self):
        return False

    def should_advance(self, name, result, *, touched_paths):
        self.touched = list(touched_paths)
        return self.should

    def advance(self, name):
        self.advanced = name

    def _current_step_index(self):
        return 0

    def tick(self, **kw):
        self.tick_kw = kw

    def needs_replan(self):
        return self.needs

    def replan_prompt(self, *, written_files, file_changes):
        return "REPLAN"

    def reminder(self):
        return self.reminder_text


class _Ctx:
    def __init__(self):
        self.events = []
        self.reset_calls = 0
        self.exec_ctrl = SimpleNamespace(reset_stuck_detection=self._reset)

    def _reset(self):
        self.reset_calls += 1

    async def emit(self, data):
        self.events.append(data)
        return data


def _run(*, tracker=None, messages=None, ps=None, cfg=12):
    tracker = tracker or _Tracker()
    messages = messages if messages is not None else [{"role": "tool"}]
    ps = ps or _DuoPlanRoundState(max_tool_rounds=10)
    ctx = _Ctx()

    async def _go():
        evs = []
        async for ev in _duo_plan_tracker_round(
            ctx, tracker,
            last_tool_name="edit_file", last_tool_result={"ok": True},
            file_changes={"a.py": 1}, written_files=["a.py"],
            dtool_msgs=messages, max_tool_rounds_cfg=cfg, ps=ps,
        ):
            evs.append(ev)
        return evs

    evs = asyncio.run(_go())
    return ctx, tracker, messages, ps, evs


# advance + tick
_ctx, _tr, _msgs, _ps, _evs = _run()
check("advance uses the touched paths and logs no event",
      _tr.advanced == "edit_file" and _tr.touched == ["a.py"] and not _evs)
check("tick receives the tool result and file count",
      _tr.tick_kw == {"tool_name": "edit_file", "tool_result": {"ok": True},
                      "touched_paths": ["a.py"], "file_changes_count": 1})

# reminder
_tr = _Tracker()
_tr.reminder_text = "  reminder text  "
_ctx, _tr, _msgs, _ps, _evs = _run(tracker=_tr)
check("reminder is appended to a trailing tool message",
      _msgs[-1] == {"role": "user", "content": "reminder text"})
_msgs2 = [{"role": "user"}]
_ctx, _tr, _msgs2, _ps, _evs = _run(tracker=_tr, messages=_msgs2)
check("reminder is not appended after a non-tool message", len(_msgs2) == 1)

# replan + one-shot bonus
_tr = _Tracker()
_tr.needs = True
_tr._plan.deviation.streak = 2
_ctx, _tr, _msgs, _ps, _evs = _run(tracker=_tr)
check("replan increments the count and applies the one-shot bonus",
      _ps.replan_count == 1 and _ps.replan_bonus_granted is True
      and _ps.max_tool_rounds == 12 and _ps.accumulated_replan_bonus == 3)
check("replan appends directive and prompt",
      any(m.get("content") == "REPLAN" for m in _msgs)
      and len(_msgs) == 3 and len(_evs) == 1)
check("replan resets deviation and the read window",
      _tr._plan.deviation.streak == 0 and _tr._initial_read_phase is True
      and _tr._initial_reads == 0 and _ctx.reset_calls == 1)

_tr = _Tracker()
_tr.needs = True
_ctx, _tr, _msgs, _ps, _evs = _run(
    tracker=_tr, ps=_DuoPlanRoundState(replan_bonus_granted=True, max_tool_rounds=12))
check("replan bonus is one-shot", _ps.max_tool_rounds == 12
      and _ps.accumulated_replan_bonus == 0)

# cap branch
_tr = _Tracker()
_tr.needs = True
_ps = _DuoPlanRoundState(replan_count=3, max_tool_rounds=10)
_ctx, _tr, _msgs, _ps, _evs = _run(tracker=_tr, ps=_ps)
check("fourth replan hits the cap and returns continue",
      _ps.action == "continue" and _ps.replan_count == 4
      and _tr._stall_ticks == 1 and len(_evs) == 1)
check("cap branch keeps enforcement and skips the replan messages",
      _tr._initial_read_phase is False and len(_msgs) == 1
      and _ctx.reset_calls == 0)
check("cap status renders the stall counter as a number",
      "tracking stalls (1/6)" in _evs[0]["content"]
      and "{_plan_tracker" not in _evs[0]["content"])

# critical stall
_tr = _Tracker()
_tr.needs = True
_tr._stall_ticks = 5
_ps = _DuoPlanRoundState(replan_count=3, max_tool_rounds=10)
_ctx, _tr, _msgs, _ps, _evs = _run(tracker=_tr, ps=_ps)
check("sixth stall escalates to the critical message",
      _tr._stall_ticks == 6 and "CRITICAL STALL" in _evs[0]["content"]
      and "max_replans_exhausted" in _msgs[-1]["content"]
      and _ps.action == "continue")

sys.exit(h.finish("duo_plan_round"))
