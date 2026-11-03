# -*- coding: utf-8 -*-
"""A3 behavioral proof: run_bash must execute on POSIX.

The AST test pins the binding, but only a real POSIX run proves the command
path. This suite is a no-op on Windows and the Linux CI check.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    print("SKIP: POSIX-only behavioral test")
    sys.exit(0)

from tools.handlers.exec_tools import _inline_tool_run_bash   # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


ws = Path(tempfile.mkdtemp())
out = asyncio.run(_inline_tool_run_bash({"cmd": "printf 'posix-ok'"}, ws, None))
check("1 a POSIX command runs", "posix-ok" in out, str(out)[:120])
check("2 it is not an exec error",
      "RUN_BASH_EXEC_ERROR" not in out and "NameError" not in out,
      str(out)[:120])

out = asyncio.run(_inline_tool_run_bash({"cmd": "echo one && echo two"}, ws, None))
check("3 shell chaining works", "one" in out and "two" in out, str(out)[:120])

out = asyncio.run(_inline_tool_run_bash(
    {"cmd": "printf 'xx' > made.txt && cat made.txt"}, ws, None))
check("4 writes land in the workspace",
      (ws / "made.txt").read_text(encoding="utf-8") == "xx", str(out)[:120])

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
