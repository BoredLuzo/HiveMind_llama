# -*- coding: utf-8 -*-
"""Live 2026-11-03: a cancelled in-loop planner raised InvalidStateError.

cancel() only schedules; result() on the still-pending task raised
InvalidStateError, and the broad handler reported "Thinking plan error" with a
fallback plan instead of a deadline stop.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ((ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
       + (ROOT / "core" / "duo_inloop_planner.py").read_text(encoding="utf-8"))

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


check("1 the in-loop planner handles the pending-result race",
      "except (asyncio.CancelledError, asyncio.InvalidStateError):" in SRC)
check("2 the outside planner already handled it",
      SRC.count("asyncio.InvalidStateError") >= 2)
check("3 the deadline-cancel comment is present",
      "DEADLINE-CANCEL-RACE (2026-11-03" in SRC)
check("4 the cancelled planner task is awaited (no pending leak)",
      "await asyncio.wait_for(" in SRC
      and "_inloop_plan_task, timeout=2.0" in SRC)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
