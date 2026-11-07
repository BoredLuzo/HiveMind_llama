# -*- coding: utf-8 -*-
"""Behavior test: core/duo_loop_guards._duo_loop_guards (W4a, 2026-11-07).

Pins the extracted pre-compression guards: GUARD C must end the tool-round
loop (action="break") with _loop_detected=True when the degraded slot cannot
hold the round-1 context, and must NOT fire when the slot is healthy or big
enough. Health-check/repo-map paths are disabled (settings/round=0).
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.duo_loop_guards import _DuoLoopGuardsState, _duo_loop_guards  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}{extra}")
    else:
        failed += 1
        print(f"  FAIL {name}{extra}")


class _Ctx:
    settings = {"duo_pin_static_map": False}

    def __init__(self):
        self.events = []

    async def emit(self, ev):
        self.events.append(ev)
        return ""


def _run(**over):
    ctx = _Ctx()
    ps = _DuoLoopGuardsState(
        exec_mdl="test:1b", actual_ctx=4096, dtool_ctx_requested=8192,
        slot_degraded=True, total_tool_rounds=0,
        dtool_msgs=[{"role": "user", "content": "x" * 20000}],
        coder_real_prompt_tokens=[0], coder_est_at_real=[0],
        coder_real_at_msgs_len=[0],
    )
    for k, v in over.items():
        setattr(ps, k, v)

    async def _go():
        async for _ in _duo_loop_guards(ctx, ps):
            pass

    asyncio.run(_go())
    return ps, ctx.events


ps, events = _run()
check("degraded slot + big round-1 context -> break action",
      ps.action == "break", f" ({ps.action})")
check("guard C sets loop_detected", ps.loop_detected is True)
check("guard C emits a visible status",
      any(e.get("type") == "status" and "does not fit" in str(e.get("content", ""))
          for e in events), f" ({len(events)} events)")
check("calibrated estimate is exposed to the runner", ps.est_tokens >= 3500,
      f" ({ps.est_tokens})")

ps2, events2 = _run(slot_degraded=False)
check("healthy slot -> proceed", ps2.action == "proceed" and ps2.loop_detected is False)

ps3, events3 = _run(actual_ctx=16384)
check("slot above the ctx floor -> proceed", ps3.action == "proceed")

print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
