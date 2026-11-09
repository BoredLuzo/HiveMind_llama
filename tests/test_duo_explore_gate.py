# -*- coding: utf-8 -*-
"""Behavior test: the explore-only round gate (F13, 2026-11-08)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.duo_explore_gate import _explore_round_gate  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


class _Ctx:
    def __init__(self):
        self.events = []

    async def emit(self, data):
        self.events.append(data)
        return data


async def _snapshot(*_a):
    return {}


def _run(**over):
    ctx = _Ctx()
    kw = dict(
        ctx=ctx, round_tool_names={"read_file"},
        dr_tcs=[{"function": {"name": "read_file",
                              "arguments": {"path": "old.py"}}}],
        seen_explore_paths={"old.py#rng:full"}, explore_only_rounds=0,
        contracts_raw=[], explore_ctx="ctx", exec_mdl="m",
        until_finished=False, snap_before=None, ws_str="/ws",
        pre_explore_msgs=[], written_files=[], ld_setter=lambda _l: None,
        snapshot_fn=_snapshot,
    )
    kw.update(over)
    res = asyncio.run(_explore_round_gate(**kw))
    return ctx, res


_ctx, _r = _run(round_tool_names={"edit_file"})
check("write tool resets the explore streak",
      _r.explore_only_rounds == 0 and _r.action == "proceed"
      and not _r.loop_detected)

_ctx, _r = _run(round_tool_names={"run_bash"}, explore_only_rounds=5)
check("run_bash without a snapshot also resets",
      _r.explore_only_rounds == 0 and _r.action == "proceed")

_ctx, _r = _run(
    dr_tcs=[{"function": {"name": "read_file",
                          "arguments": {"path": "new.py"}}}],
    seen_explore_paths=set(), explore_only_rounds=3)
check("a new explore path does not increment the streak",
      _r.explore_only_rounds == 3 and not _r.loop_detected)

_ld: list[int] = []
_ctx, _r = _run(explore_only_rounds=5, ld_setter=_ld.append)
check("threshold reached breaks and stamps the ledger",
      _r.explore_only_rounds == 6 and _r.loop_detected is True
      and _r.action == "break" and _ld == [3408])
check("both abort events are emitted",
      [e["type"] for e in _ctx.events] == ["status", "token"]
      and "explore-only" in _ctx.events[1]["content"])

_ctx, _r = _run(round_tool_names={"web_search"}, explore_only_rounds=2,
                dr_tcs=[])
check("non-explore/non-write tools leave the streak untouched",
      _r.explore_only_rounds == 2 and _r.action == "proceed")

sys.exit(h.finish("duo_explore_gate"))
