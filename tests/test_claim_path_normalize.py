# -*- coding: utf-8 -*-
"""Live 2026-11-03: task_complete claims must resolve "/C:/..." paths."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.duo_task_complete_gate import _claim_abs_path     # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


ws = tempfile.mkdtemp()
(target := Path(ws) / "big.py").write_text("x", encoding="utf-8")

check("1 a relative claim resolves inside the workspace",
      Path(_claim_abs_path("big.py", ws)) == target)
check("2 an absolute claim is kept",
      _claim_abs_path(str(target), ws) == str(target))

if os.name == "nt":
    slash = "/" + str(target).replace("\\", "/")
    check("3 a Windows /C:/ claim is normalized",
          Path(_claim_abs_path(slash, ws)) == target, _claim_abs_path(slash, ws))
else:
    print("  SKIP 3 (Windows-only path shape)")

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
