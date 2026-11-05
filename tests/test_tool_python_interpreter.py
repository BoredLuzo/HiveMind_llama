"""Tools must find Python even when the system PATH has none.

A uv-managed install may only provide python3.14.exe (or nothing named
`python`). run_python must use the engine venv interpreter, and run_bash /
run_tests must prepend the venv bin directory to the child PATH.

Run: python tests/test_tool_python_interpreter.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from infra.child_env import tool_env  # noqa: E402
from tools.handlers.exec_tools import (  # noqa: E402
    _inline_tool_run_bash, _inline_tool_run_python)

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def main() -> int:
    ws = Path(tempfile.mkdtemp(prefix="hmtoolpy_"))
    py_dir = str(Path(sys.executable).parent)
    env = tool_env()
    check("tool env prepends the running interpreter",
          py_dir in env.get("PATH", "").split(os.pathsep))
    check("tool env forces UTF-8 child IO",
          env.get("PYTHONIOENCODING") == "utf-8")

    out = asyncio.run(_inline_tool_run_python(
        {"code": "print(6 * 7)"}, ws, str(ws)))
    check("run_python runs without a system 'python'", "42" in out, out[:80])

    out2 = asyncio.run(_inline_tool_run_bash(
        {"cmd": 'python -c "print(7 * 6)"'}, ws, str(ws)))
    check("run_bash resolves 'python' from the venv PATH",
          "42" in out2, out2[:120])

    print(f"\nSUMMARY tool_python_interpreter: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
