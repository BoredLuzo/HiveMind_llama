"""SCHEMA-TRUTH (2026-10-14): the tool schema text must state the SAME limits
the handlers enforce — resolved from the run's budgets (core/budgets), not
module constants. Pins handler behavior, resolved budgets and schema text
together; the budget redesign removed the fixed staffels (200/400-line lies
were the original offenders).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.budgets import resolve, set_active  # noqa: E402

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


def _schema_text(name, budget_chars=None):
    from tools.definitions import _get_inline_tools
    for t in _get_inline_tools(budget_chars=budget_chars):
        if t.get("function", {}).get("name") == name:
            return json.dumps(t)
    raise AssertionError(f"tool '{name}' missing from schema list")


print("=== schema truth (resolved budgets vs handlers vs schema text) ===")

_b = resolve(16384, 3.92, 4096, {})
set_active(_b)

_txt = _schema_text("read_file", budget_chars=_b.schema_budget)
check("read_file schema has no stale 200-line cap",
      "capped at 200 lines" not in _txt and " 200 lines" not in _txt)
check("read_file schema states THIS run's resolved read cap",
      str(_b.read_char_cap) in _txt)
check("read_file schema has no fixed line staffel",
      "1200 lines" not in _txt and "400 lines" not in _txt)
_generic = _schema_text("read_file")
check("generic (no-budget) schema carries no numbers",
      str(_b.read_char_cap) not in _generic)

import tools.handlers.file_ops as _fo
cap = _b.read_char_cap
check("BIG_FILE_NOTE has no stale 400 number",
      "400" not in _fo._BIG_FILE_NOTE.format(_lines=9999, _abort=cap))
check("BIG_FILE_NOTE says the disk file is complete", "complete" in _note.lower()
      if (_note := _fo._BIG_FILE_NOTE.format(_lines=9999, _abort=cap)) else False)

import core.tool_executor as _te
_capped = _te._cap_tool_result("x" * (_b.tool_result_cap + 5000))
check("executor cap follows the resolved budget",
      len(_capped) <= _b.tool_result_cap + 200 and "truncated at" in _capped)

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
