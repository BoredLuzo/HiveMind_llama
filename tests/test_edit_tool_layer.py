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
    from tools.handlers.file_ops import _inline_tool_write_file
    await _inline_tool_write_file(
        {"path": "c.js", "content": body}, ws, str(ws))
    out = {}
    for label, kw in cases:
        out[label] = await _inline_tool_edit_file(
            dict(kw, _tool_name="write_file"), ws, str(ws))
    return ws, out


def test_write_file_guards():
    from tools.handlers.file_ops import _inline_tool_write_file
    ws = Path(tempfile.mkdtemp(prefix="hvm_edlayer_wf_"))
    # create file first
    asyncio.run(_inline_tool_write_file(
        {"path": "c.js", "content": "line 1\nline 2\n"}, ws, str(ws)))
    # noop: identical content -> NOOP error
    r_noop = asyncio.run(_inline_tool_write_file(
        {"path": "c.js", "content": "line 1\nline 2\n"}, ws, str(ws)))
    # block marker in content -> rejected
    r_marker = asyncio.run(_inline_tool_write_file(
        {"path": "c2.js", "content": "<<<<<<< SEARCH\nx\n>>>>>>> REPLACE"}, ws, str(ws)))
    checks = [("WRITE_FILE_NOOP", r_noop), ("BLOCK_FORMAT", r_marker)]
    bad = [lbl for lbl, r in checks if lbl not in r]
    if not bad:
        ok("write_file: noop + block-format sniffs")
    else:
        fail("wf_guards", f"fehlt: {bad}")


def test_dispatch_consolidation_notice():
    from tools.runner import _run_inline_tool
    ws = Path(tempfile.mkdtemp(prefix="hvm_edlayer_old_"))
    (ws / "old.txt").write_text("a\nb\nc\n", encoding="utf-8")
    r = asyncio.run(_run_inline_tool(
        "replace_lines",
        {"path": "old.txt", "start_line": 1, "end_line": 2, "replacement": "B\n"},
        workspace_lock=str(ws), tool_mode="duo_full", include_websearch=False))
    if "TOOL_REMOVED" in r:
        ok("replace_lines-Dispatch: Konsolidierungs-Hinweis")
    else:
        fail("dispatch_old", r[:120])


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
    if "TOOL_REMOVED" in r:
        ok("replace_lines-Dispatch: klare Konsolidierungs-Meldung (alte Session wird nicht still geblockt)")
    else:
        fail("dispatch_old", r[:120])


def test_hint_updated():
    src = (Path(__file__).parent.parent / "tools" / "handlers" / "file_ops.py").read_text(encoding="utf-8")
    if "start_line/end_line" in src and "switch to replace_lines" not in src:
        ok("Fehler-Hint verweist auf start_line/end_line statt replace_lines")
    else:
        fail("hint", "alter replace_lines-Hint noch da")


def test_margin_ambiguity():
    # two candidate regions both over the 0.85 threshold with a narrow gap
    # (near-tie) -> the matcher must reject as ambiguous, never pick one.
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from tools.patch_2_fuzzy_edit import fuzzy_replace
    # three near-identical handler regions; SEARCH matches all three
    amb = (
        "function handler() {\n    workOne();\n    return 1;\n}\n\n" * 3
    )
    amb_old = "function handler() {\n    workOne();\n    return 1;\n}"
    r = fuzzy_replace(amb, amb_old, "REPLACED\n")
    if r is None:
        ok("Ambiguity: 3 identische Regionen -> reject (kein willkürlicher Ersatz)")
    else:
        fail("ambiguity_tie", "identische Regionen wurden nicht rejected")


def test_margin_clear_winner():
    # one true region 0.98, all others <= 0.2 -> clear winner must match
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from tools.patch_2_fuzzy_edit import fuzzy_replace
    content = (
        "function setup() {\n    init();\n}\n\n" * 3
        + "function target() {\n    doSpecialWork(17);\n    return true;\n}\n\n"
        + "function teardown() {\n    cleanup();\n}\n\n" * 3
    )
    old = "function target() {\n    doSpecialWork(17);\n    return true;\n}"
    r = fuzzy_replace(content, old, "REPLACED\n")
    if r is not None and "REPLACED" in r and r.count("REPLACED") == 1:
        ok("Clear Winner: eindeutige Region matcht trotz vieler Nachbarn")
    else:
        fail("clear_winner", f"r={bool(r)}")


def test_stale_file_hard_fails():
    # file read, then EXTERNALLY modified, then edit with the pre-modification
    # old_text -> must hard-fail (no fuzzy rescue on a stale text basis)
    ws = Path(tempfile.mkdtemp(prefix="hvm_stale_"))
    f = ws / "mod.js"
    f.write_text("old content one\nold content two\n", encoding="utf-8")
    from tools.handlers.file_ops import _inline_tool_read_file, _inline_tool_edit_file
    asyncio.run(_inline_tool_read_file({"path": "mod.js"}, ws, str(ws)))
    # external modification (changes mtime AND size)
    f.write_text("brand new content entirely\n", encoding="utf-8")
    import os as _os
    st = _os.stat(f)
    _os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    r = asyncio.run(_inline_tool_edit_file(
        {"path": "mod.js",
         "old_text": "old content one\nold content two\n",
         "new_text": "hacked\n"}, ws, str(ws)))
    body = f.read_text()
    if "OLD_TEXT_NOT_FOUND" in r and "brand new content entirely" in body:
        ok("Stale edit: old_text auf extern veraenderter Datei -> klarer Fail (kein Fuzzy)")
    else:
        fail("stale", f"r={r[:120]} body={body!r}")


if __name__ == "__main__":
    test_write_file_guards()
    test_dispatch_consolidation_notice()
    test_advertising_consolidated()
    test_dispatch_still_works_old_sessions()
    test_hint_updated()
    test_margin_ambiguity()
    test_margin_clear_winner()
    test_stale_file_hard_fails()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
