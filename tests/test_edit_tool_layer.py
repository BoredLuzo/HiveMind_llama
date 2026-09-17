"""Edit-tool layer: edit_file line mode + consolidated advertising (2026-09-17).

Consolidation: replace_lines and edit_ast are no longer advertised (dead
weight: replace_lines was a strict guard-subset of edit_file, edit_ast had
zero usage and bypassed guards/undo/telemetry). Both stay dispatched for
old sessions. edit_file gains optional start_line/end_line — the line-
precise mode replaces the old replace_lines escape hatch, WITH noop and
shrink guards the old path lacked.

Run: python tests/test_edit_tool_layer.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.handlers.file_ops import _inline_tool_edit_file
from tools.definitions import _get_inline_tools

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


async def _run(cases):
    ws = Path(tempfile.mkdtemp(prefix="hvm_edlayer_"))
    body = "".join(f"line {i}\n" for i in range(1, 51))
    await _inline_tool_edit_file(
        {"path": "c.js", "edits": body, "_tool_name": "write_file"}, ws, str(ws))
    out = {}
    for label, kw in cases:
        out[label] = await _inline_tool_edit_file(
            dict(kw, _tool_name="write_file"), ws, str(ws))
    return ws, out


def test_line_mode():
    ws, o = asyncio.run(_run([
        ("replace", {"path": "c.js", "edits": "REPLACED LINE",
                     "start_line": 10, "end_line": 12}),
    ]))
    lines = (ws / "c.js").read_text().splitlines()
    if "lines 10-12 replaced" in o["replace"] and lines[9] == "REPLACED LINE" and len(lines) == 48:
        ok("Line-Mode ersetzt exakt die Range (50 -> 48 Zeilen)")
    else:
        fail("line_mode", o["replace"][:120])


def test_line_mode_guards():
    ws, o = asyncio.run(_run([
        ("inverted", {"path": "c.js", "edits": "x", "start_line": 20, "end_line": 10}),
        ("oob", {"path": "c.js", "edits": "x", "start_line": 999, "end_line": 1001}),
        ("noop", {"path": "c.js", "edits": "line 5", "start_line": 5, "end_line": 5}),
        ("shrink", {"path": "c.js", "edits": "tiny", "start_line": 1, "end_line": 50}),
    ]))
    checks = [
        ("inverted", "INVALID_ARGS", o),
        ("oob", "INVALID_ARGS", o),
        ("noop", "NOOP", o),
        ("shrink", "SUSPICIOUS_SHRINK", o),
    ]
    bad = [lbl for lbl, needle, o in checks if needle not in o.get(lbl, "")]
    if not bad:
        ok("Guards im Line-Modus: inverted/oob/noop/shrink")
    else:
        fail("line_guards", f"fehlt: {bad}")


def test_advertising_consolidated():
    tools = _get_inline_tools(include_websearch=True, mode="duo_full")
    names = {t["function"]["name"] for t in tools}
    if "replace_lines" not in names and "edit_ast" not in names and "edit_file" in names:
        ok("replace_lines/edit_ast nicht mehr advertised (edit_file bleibt)")
    else:
        fail("advertising", f"names={sorted(names)}")


def test_dispatch_still_works_old_sessions():
    from tools.runner import _run_inline_tool
    ws = Path(tempfile.mkdtemp(prefix="hvm_edlayer_old_"))
    (ws / "old.txt").write_text("a\nb\nc\n", encoding="utf-8")
    r = asyncio.run(_run_inline_tool(
        "replace_lines",
        {"path": "old.txt", "start_line": 1, "end_line": 2, "replacement": "B\n"},
        workspace_lock=str(ws), tool_mode="duo_full", include_websearch=False))
    if "replace_lines" in r and "B" in (ws / "old.txt").read_text():
        ok("replace_lines bleibt dispatchbar (alte Sessions)")
    else:
        fail("dispatch_old", r[:120])


def test_hint_updated():
    src = (Path(__file__).parent.parent / "tools" / "handlers" / "file_ops.py").read_text(encoding="utf-8")
    if "start_line/end_line" in src and "switch to replace_lines" not in src:
        ok("Fehler-Hint verweist auf start_line/end_line statt replace_lines")
    else:
        fail("hint", "alter replace_lines-Hint noch da")


if __name__ == "__main__":
    test_line_mode()
    test_line_mode_guards()
    test_advertising_consolidated()
    test_dispatch_still_works_old_sessions()
    test_hint_updated()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
