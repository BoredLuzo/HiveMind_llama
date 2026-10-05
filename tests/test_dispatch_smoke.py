"""Eval: inline-tool dispatch (tools/runner._run_inline_tool) - offline smoke.

Verifies the central tool funnel deterministically: simple offline tools
return results, writes outside the workspace are blocked, unknown
tools return TOOL_NOT_FOUND. No LLM, no subprocess execution needed.
"""
import asyncio
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from tools.runner import _run_inline_tool
from tools.errors import parse_tool_error

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


async def main():
    ws = Path(tempfile.mkdtemp())

    r = await _run_inline_tool("get_datetime", {}, str(ws))
    check("dispatch get_datetime", isinstance(r, str) and len(r) > 0, f" ({str(r)[:40]})")

    f = ws / "hello.txt"
    f.write_text("hello eval content", encoding="utf-8")
    r = await _run_inline_tool("list_dir", {"path": str(ws)}, str(ws))
    check("dispatch list_dir lists file", "hello.txt" in r, f" ({str(r)[:60]})")

    r = await _run_inline_tool("read_file", {"path": "hello.txt"}, str(ws))
    check("dispatch read_file content", "hello eval content" in r)

    # write inside the workspace (new file) -> creates the file
    r = await _run_inline_tool("write_file", {"path": "new_out.txt", "content": "x"}, str(ws))
    check("write inside workspace ok", not r.startswith("[TOOL_ERROR") or "TOOL_ERROR" not in r[:60], f" ({str(r)[:50]})")
    check("write inside created file", (ws / "new_out.txt").exists())

    # write outside the workspace -> blocked, the file is NOT created
    outside = ws.parent / "escape_dispatch.txt"
    if outside.exists():
        outside.unlink()
    r = await _run_inline_tool("write_file", {"path": str(outside), "content": "x"}, str(ws))
    pe = parse_tool_error(r)
    check("write outside blocked", pe is not None, f" ({str(r)[:60]})")
    check("write outside file not created", not outside.exists())

    # unknown tool
    r = await _run_inline_tool("no_such_tool", {}, str(ws))
    pe = parse_tool_error(r)
    check("unknown tool -> TOOL_NOT_FOUND", pe and pe["code"] == "TOOL_NOT_FOUND", f" ({str(r)[:60]})")


asyncio.run(main())

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
