# -*- coding: utf-8 -*-
"""A7: the read-only prefetch must not run after the round is over.

_prefetch_readonly_tools can execute run_tests (up to 90 s). It used to fire
before the per-round deadline and abort checks, so a dead round still ran
tests and discarded the results.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "core" / "tool_executor.py").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


fn_src = ""
tree = ast.parse(SRC)
for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute_tool_round":
        fn_src = ast.get_source_segment(SRC, node) or ""
        break

check("1 execute_tool_round exists", bool(fn_src))
_idx = fn_src.find("_prefetch_readonly_tools(")
check("2 the prefetch call exists", _idx != -1)
# The call itself sits inside the guard, so the deadline test and the abort
# test must both appear before it.
check("3 the deadline is checked before the prefetch",
      0 <= fn_src.find("time.monotonic() < trs.duo_deadline_at") < _idx)
check("3b an unset deadline (0.0) does not block the prefetch",
      "not trs.duo_deadline_at" in fn_src)
check("4 the abort is checked before the prefetch",
      0 <= fn_src.find("hooks.is_aborted(chat_id)", 0, _idx) < _idx)
check("5 a skipped prefetch is logged", "[PREFETCH] skipped" in fn_src)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
