# -*- coding: utf-8 -*-
"""B2 follow-up: MCP/internal writes must still work without a run_id.

The approval gate is only consulted by the chat tool loop. The MCP server and
the token-gated /internal/tool/exec path dispatch tools directly, so the
fail-closed B2 change must not break them.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["MCP_ALLOW_WRITE"] = "1"

from infra import mcp_server as M               # noqa: E402
from tools import runner as R                   # noqa: E402

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
M.WORKSPACE = ws

allowed, why = M._tool_allowed("write_file", "http")
check("1 the MCP write tier is open with MCP_ALLOW_WRITE=1", allowed, why)

out = asyncio.run(M.tool_write_file("mcp.txt", "mcp-hi"))
check("2 the MCP tool writes without a run_id", (ws / "mcp.txt").exists(), out[:60])
check("3 the content is intact",
      (ws / "mcp.txt").read_text(encoding="utf-8") == "mcp-hi")

R._approval_gate_on = lambda: True              # approval feature ON
R._current_run_id.set(None)                      # and no run context
# routers/core.py sets this after the token/CSRF guard, exactly around the
# dispatch this test exercises.
_tok = R._external_dispatch.set(True)
try:
    out = asyncio.run(R._run_inline_tool(
        "write_file", {"path": "eng.txt", "content": "eng-hi"},
        workspace_lock=str(ws)))
finally:
    R._external_dispatch.reset(_tok)
check("4 the internal exec path writes without a run_id",
      (ws / "eng.txt").exists(), str(out)[:70])
check("5 its content is intact",
      (ws / "eng.txt").read_text(encoding="utf-8") == "eng-hi")

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
