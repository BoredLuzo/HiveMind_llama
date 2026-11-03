# -*- coding: utf-8 -*-
"""A1: the verify nudge must be one complete string.

The constant was split across adjacent lines without parentheses, so only the
first line was assigned and lines 105-113 became no-op string expressions. The
model then received "…has not been followed by a successful test run." with no
instruction, three times (veto budget 3).
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "core" / "duo_runner.py"

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


tree = ast.parse(SRC.read_text(encoding="utf-8"))
value, node_line = None, 0
for node in tree.body:
    if (isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "_VERIFY_REQUIRED_PROMPT"
                    for t in node.targets)):
        value, node_line = ast.literal_eval(node.value), node.lineno
        break

check("1a the prompt is assigned once", isinstance(value, str), f"(line {node_line})")
text = value or ""
check("1b it contains the ordered instruction", "Do this NOW" in text)
check("1c it names run_tests", "run_tests" in text)
check("1d it closes the loop with task_complete", "task_complete again" in text)
check("1e no orphan string statements in the module",
      not any(isinstance(n, ast.Expr)
              and isinstance(n.value, ast.Constant)
              and isinstance(n.value.value, str)
              for n in tree.body[1:]))

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
