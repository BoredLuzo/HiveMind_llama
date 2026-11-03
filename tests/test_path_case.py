# -*- coding: utf-8 -*-
"""P1 (Linux audit 2026-11-03): path normalization must not fold case on POSIX."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.file import normalize_tool_path      # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


a = normalize_tool_path("/tmp/App.py")
b = normalize_tool_path("/tmp/app.py")
if os.name == "nt":
    check("1 Windows treats both spellings as one file", a == b)
else:
    check("1 POSIX keeps App.py and app.py distinct", a != b, f"{a} vs {b}")

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
