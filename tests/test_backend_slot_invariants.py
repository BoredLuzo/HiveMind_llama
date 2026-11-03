# -*- coding: utf-8 -*-
"""A9c-e: backend slot invariants the audit found broken.

A9c _evict_lru ignored the in_flight guard (killing a slot mid-round).
A9d force_kill_all never reset in_flight (slot permanently non-evictable).
A9e manager_load logged `slot.num_ctx`; ModelSlot only has `_num_ctx`, and the
    AttributeError was swallowed so [VRAM-TIGHT] never fired.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def _func_src(path, name):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


mp = _func_src(ROOT / "backend" / "manager_process.py", "_evict_lru")
check("1a _evict_lru exists", bool(mp))
check("1b both candidate lists guard in_flight", mp.count("in_flight") >= 2,
      f"({mp.count('in_flight')})")

mh = _func_src(ROOT / "backend" / "manager_health.py", "force_kill_all")
check("2a force_kill_all exists", bool(mh))
check("2b the reset loop clears in_flight", "s.in_flight = 0" in mh)

ml_src = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
check("3a the VRAM-TIGHT log uses the private ctx field",
      "slot.num_ctx" not in ml_src and "slot._num_ctx" in ml_src)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
