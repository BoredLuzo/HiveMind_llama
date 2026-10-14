"""SCHEMA-TRUTH (2026-10-14, audit phase 1): the tool schema text must state
the SAME limits the handlers enforce. The audit found the read_file schema
claiming "capped at 200 lines" while the handler aborts at 1201 lines, and a
stale 400-line note in write results — small models obey the text and
ritual-read in 200-line slices the handler no longer needs. limits.py is the
single source; this test pins handler behavior and schema text to it.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.limits import (
    READ_FULL_LINE_ABORT,
    READ_CHAR_CAP,
    EXEC_RESULT_CAP,
)

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}")


def _schema_text(name):
    from tools.definitions import _get_inline_tools
    for t in _get_inline_tools():
        if t.get("function", {}).get("name") == name:
            return json.dumps(t)
    raise AssertionError(f"tool '{name}' missing from schema list")


print("=== schema truth (limits.py vs handlers vs schema text) ===")

_txt = _schema_text("read_file")
check("read_file schema has no stale 200-line cap",
      "capped at 200 lines" not in _txt and " 200 lines" not in _txt)
check("read_file schema states the real line abort",
      str(READ_FULL_LINE_ABORT) in _txt)
check("read_file schema states the real char cap",
      str(READ_CHAR_CAP) in _txt)

import tools.handlers.file_ops as _fo
check("file_ops imports the limits constants",
      _fo._READ_LINE_ABORT == READ_FULL_LINE_ABORT
      and _fo._READ_CHAR_CAP == READ_CHAR_CAP)

_note = _fo._BIG_FILE_NOTE.format(_lines=9999, _abort=_fo._READ_LINE_ABORT)
check("BIG_FILE_NOTE has no stale 400 number", "400" not in _note)
check("BIG_FILE_NOTE states the real abort", str(READ_FULL_LINE_ABORT) in _note)
check("BIG_FILE_NOTE says the disk file is complete", "complete" in _note.lower())

import core.tool_executor as _te
_capped = _te._cap_tool_result("x" * (EXEC_RESULT_CAP + 5000))
check("executor cap still enforced at the constant",
      len(_capped) <= EXEC_RESULT_CAP + 200 and "truncated at" in _capped)

from tools.definitions import _get_inline_tools as _git
_names = {t["function"]["name"] for t in _git()}
check("replace_lines not advertised (consolidated)", "replace_lines" not in _names)
check("edit_ast not advertised (consolidated)", "edit_ast" not in _names)

_rb = _schema_text("run_bash")
check("run_bash schema still routes writes to write_file", "write_file" in _rb)
check("run_bash schema keeps the compact Windows trap", "UTF-16" in _rb)

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
