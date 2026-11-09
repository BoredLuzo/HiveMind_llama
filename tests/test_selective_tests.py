# -*- coding: utf-8 -*-
"""D4 selective test impact: run_tests scopes to testmap-mapped test files."""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools import runner as _tr
from tools.handlers import _shared
from tools.handlers.exec_tools import _inline_tool_run_tests
from tests._harness import Harness

h = Harness(style="plain", pad=True)
check = h.check


class _Result:
    success = True
    language = "python"
    command = "pytest test_mod.py"
    failure_count = 0
    inject_msg = ""

    def is_clean(self):
        return True


_calls = []


async def _fake_suite(**kw):
    _calls.append(kw)
    return _Result()


_orig = _shared._run_test_suite
_shared._run_test_suite = _fake_suite

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (ws / "test_mod.py").write_text("def test_f():\n    assert True\n", encoding="utf-8")
    tok = _tr._files_written_in_run.set({str(ws / "mod.py")})
    try:
        out = asyncio.run(_inline_tool_run_tests({}, ws, str(ws)))
    finally:
        _tr._files_written_in_run.reset(tok)
    check("mapped written file scopes the suite",
          "All tests passed" in out and _calls and _calls[-1].get("paths") == ["test_mod.py"],
          f"({_calls[-1].get('paths') if _calls else None})")

    _calls.clear()
    tok = _tr._files_written_in_run.set({str(ws / "mod.py")})
    try:
        asyncio.run(_inline_tool_run_tests({"paths": ["test_mod.py"]}, ws, str(ws)))
    finally:
        _tr._files_written_in_run.reset(tok)
    check("explicit paths win over testmap",
          _calls and _calls[-1].get("paths") == ["test_mod.py"])

    _calls.clear()
    tok = _tr._files_written_in_run.set({str(ws / "page.html")})
    try:
        _out_static = asyncio.run(_inline_tool_run_tests({}, ws, str(ws)))
    finally:
        _tr._files_written_in_run.reset(tok)
    check("static-only run still skips before the suite",
          "test run skipped" in _out_static and not _calls)

_shared._run_test_suite = _orig
sys.exit(h.finish("selective_tests"))
