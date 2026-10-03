# -*- coding: utf-8 -*-
"""run_python UTF-8 child-IO test (2026-09-18).

Live bug: the run_python subprocess inherited the Windows console code
page (cp1252) — any snippet printing non-ASCII ("✓", "✗", umlauts)
crashed with UnicodeEncodeError on a cosmetic character. The spawn now
forces PYTHONIOENCODING=utf-8 + PYTHONUTF8=1 in the child environment.

Run: python tests/test_run_python_encoding.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

passed = 0
failed = 0


def check(name, cond, msg=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {msg}")


def _run_gate():
    try:
        from core import state as _st
        _ = _st.settings
        return True
    except ImportError:
        return False


async def _run_snippet(code):
    from tools.handlers.exec_tools import _inline_tool_run_python
    ws = Path(__file__).parent
    return await _inline_tool_run_python({"code": code}, ws, None)


def test_non_ascii_print():
    out = asyncio.run(_run_snippet('print("\\u2713 ok \\u2717 fail \\u00fcber")'))
    check("non-ascii print succeeds", not out.startswith("[TOOL_ERROR:"), out[:140])
    check("non-ascii content decoded", "\u2713" in out and "\u00fcber" in out, out[:140])


def test_ascii_still_works():
    out = asyncio.run(_run_snippet("print('plain ascii')"))
    check("ascii snippet unchanged", "plain ascii" in out, out[:140])


def test_stderr_path_still_reports():
    out = asyncio.run(_run_snippet("import sys; sys.stderr.write('boom\\n')"))
    check("stderr-only still yields RUN_PYTHON_EXEC_ERROR",
          "RUN_PYTHON_EXEC_ERROR" in out, out[:140])


if __name__ == "__main__":
    if not _run_gate():
        print("  SKIP  core.state not importable in this environment")
        sys.exit(0)
    test_non_ascii_print()
    test_ascii_still_works()
    test_stderr_path_still_reports()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
