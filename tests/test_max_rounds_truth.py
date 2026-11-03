# -*- coding: utf-8 -*-
"""A4: a final answer in the last allowed round is not a round-limit hit."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.tool_loop import _final_stop_reason       # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


check("1 final answer in the last round stays completed",
      _final_stop_reason("completed", 6, 6, exhausted=False) == "completed")
check("2 an exhausted loop becomes max_tool_rounds",
      _final_stop_reason("completed", 6, 6, exhausted=True) == "max_tool_rounds")
check("3 an early answer is never rewritten",
      _final_stop_reason("completed", 3, 6, exhausted=False) == "completed")
check("4 non-completed reasons pass through",
      _final_stop_reason("aborted", 6, 6, exhausted=True) == "aborted")
check("5 a callback abort is preserved",
      _final_stop_reason("callback_abort", 6, 6, exhausted=False) == "callback_abort")

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
