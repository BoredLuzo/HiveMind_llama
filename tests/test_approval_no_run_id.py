# -*- coding: utf-8 -*-
"""B2: the approval gate must fail closed when there is no run to ask.

With the approval feature on, a context-less caller (/internal/tool/exec)
previously skipped the card (`if not run_id: return None`) while the
destructive gate 600 lines below denied. Mutating/exec calls now deny too;
read-only calls keep working.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools.runner as R                    # noqa: E402

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
_saved = R._approval_gate_on
try:
    R._approval_gate_on = lambda: True       # approval feature ON
    R._current_run_id.set(None)

    deny = asyncio.run(R._check_action_approval(
        "write_file", {"path": "x.txt", "content": "x"}, ws))
    check("1a write_file without a run id is denied",
          isinstance(deny, tuple) and deny[0] == "DENY"
          and "GATE_NO_RUN_ID" in deny[1], str(deny)[:90])

    deny = asyncio.run(R._check_action_approval(
        "run_bash", {"cmd": "echo hi"}, ws))
    check("1b run_bash without a run id is denied",
          isinstance(deny, tuple) and deny[0] == "DENY"
          and "GATE_NO_RUN_ID" in deny[1], str(deny)[:90])

    deny = asyncio.run(R._check_action_approval(
        "mcp_some_tool", {"x": 1}, ws, gated=True))
    check("1c a gated MCP tool without a run id is denied",
          isinstance(deny, tuple) and deny[0] == "DENY", str(deny)[:90])

    allow = asyncio.run(R._check_action_approval(
        "read_file", {"path": "x.txt"}, ws))
    check("1d read_file still passes", allow is None, str(allow)[:90])
finally:
    R._approval_gate_on = _saved

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
