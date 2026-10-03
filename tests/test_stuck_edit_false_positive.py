"""Tests: Loop-Detect-Stuck-False-Positive (Live-Befund Generalprobe Run 7, 2026-08-05).

Befund: [EXEC-LOOP-DIAG] loop_detected via on_tool_result hook (tool=edit_file).
Der _stuck_handler zaehlte GEBLOCKTE edit_file-Calls (READ_REQUIRED) in den
Stuck-Buffer: 2x BLOCKED + 1x erfolgreicher Edit = "3x identische Signatur" ->
is_stuck() -> fertiger Run stempelte sich zu loop_detected, obwohl der Coder
exactly the behavior the system demanded (read first, then edit).

Fix: failed edits (tool_call_failed) are no longer counted.

Standalone, kein pytest. Run: python tests/test_stuck_edit_false_positive.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

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


async def test_stuck_edit_failed_guard_wired():
    """Fix: der edit_file-Zweig des _stuck_handler recordet nur erfolgreiche Edits
    bzw. invalidiert die Stuck-Historie bei erfolgreichem Write (Run 14)."""
    src = (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(encoding="utf-8")
    _start = src.find("elif tn in (\"patch_file\", \"edit_file\"):")
    assert _start > 0, "edit_file-Stuck-Zweig fehlt"
    _seg = src[_start:src.find("elif tn == \"read_file\":", _start)]
    assert "tool_call_failed as _tcf_stuck" in _seg, \
        "tool_call_failed-Guard fehlt (geblockte Edits duerfen nicht zaehlen)"
    assert "if not _tcf_stuck(tr, tn):" in _seg, \
        "Failed-Edit-Check fehlt"
    assert "reset_stuck_detection()" in _seg, \
        "a successful write must invalidate the stuck history (Run-14 fix)"
    ok("test_stuck_edit_failed_guard_wired")


async def test_stuck_edit_failed_behavioral():
    """Behavioral: ExecutionController zaehlt nur erfolgreiche Signatur-Wiederholungen.

    Simulates the Run-7 case: BLOCKED edit (not recorded) + OK edit + OK edit
    auf derselben Datei -> Buffer enthaelt nur 2 Eintraege -> is_stuck False.
    Und der echte Loop-Fall: 3x OK-edit auf derselben Datei -> is_stuck True.
    """
    from hive_functions.loop_machine import ExecutionController
    from tools.errors import tool_call_failed

    # Run-7-Simulation: 1x BLOCKED (READ_REQUIRED) + 2x OK
    ctrl = ExecutionController(max_iterations=10, max_repeats_stuck=3)
    blocked = "[TOOL_ERROR: READ_REQUIRED]\nAction: edit_file BLOCKED on 'app.py'"
    ok_edit = "[edit_file: 'app.py' - 1/1 blocks applied (+1 lines)]"
    for _result in (blocked, ok_edit, ok_edit):
        _tn = "edit_file"
        if not tool_call_failed(_result, _tn):
            ctrl.record_output(f"edit_file:app.py", tool_name=_tn)
    if not ctrl.is_stuck():
        ok("run7-fall: BLOCKED+2xOK -> NICHT stuck")
    else:
        fail("run7-fall: BLOCKED+2xOK darf nicht stuck sein")

    # Echter Loop-Fall: 3x identischer OK-Edit -> stuck
    ctrl2 = ExecutionController(max_iterations=10, max_repeats_stuck=3)
    for _ in range(3):
        ctrl2.record_output("edit_file:app.py", tool_name="edit_file")
    if ctrl2.is_stuck():
        ok("echter-loop: 3x gleicher OK-Edit -> stuck")
    else:
        fail("real-loop: 3x same OK edit must be stuck")

    # Kontrolle: tool_call_failed erkennt den BLOCKED-Fall
    if tool_call_failed(blocked, "edit_file"):
        ok("tool_call_failed: READ_REQUIRED-Block erkannt")
    else:
        fail("tool_call_failed: READ_REQUIRED-Block NICHT erkannt")
    if not tool_call_failed(ok_edit, "edit_file"):
        ok("tool_call_failed: OK edit is not an error")
    else:
        fail("tool_call_failed: OK edit wrongly an error")



async def test_call_sigs_skip_guard_wired():
    """Fix (Run 11): Read-Guard-SKIPs duerfen nicht in call_sigs (ABAB-False-Positive)."""
    src = (Path(__file__).parent.parent / "core" / "tool_executor.py").read_text(encoding="utf-8")
    _start = src.find("# ── Loop detection ──")
    assert _start > 0, "Loop-Detection-Block fehlt"
    _seg = src[_start:src.find("_last2_identical", _start)]
    assert 'startswith("[SKIP:")' in _seg, \
        "SKIP-Guard fehlt (gelesene Dateien duerfen nicht als Loop-Signale zaehlen)"
    assert _seg.find('startswith("[SKIP:")') < _seg.find("call_sigs.append"), \
        "SKIP guard must come before call_sigs.append"
    ok("test_call_sigs_skip_guard_wired")


async def test_read_skip_not_loop_counted():
    """Fix (Run 13): Read-Guard-SKIPs zaehlen nicht als Read-Loop-Signale.

    READ-CHUNK-LOOP-FIX (2026-08-24): the SKIP guard now lives in
    read_loop_key (duo_helpers.py), which is called in the read_file branch.
    Semantics unchanged: [SKIP: -> None -> not counted.
    """
    src = (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(encoding="utf-8")
    _start = src.find('elif tn == "read_file":')
    assert _start > 0, "read_file-Stuck-Zweig fehlt"
    _seg = src[_start:src.find('elif tn in ("write_file"', _start)]
    assert "read_loop_key(fp, ta.get(\"start_line\"), ta.get(\"end_line\"), tr)" in _seg, \
        "read_file-Zweig delegiert nicht an read_loop_key"
    assert "if _rk is not None:" in _seg, \
        "None guard missing (SKIP must not be counted)"
    dh = (Path(__file__).parent.parent / "core" / "duo_helpers.py").read_text(encoding="utf-8")
    _hstart = dh.find("def read_loop_key(")
    assert _hstart > 0, "read_loop_key fehlt"
    _hseg = dh[_hstart:dh.find("def ", _hstart + 1)]
    assert 'txt.startswith("[SKIP:")' in _hseg and "return None" in _hseg, \
        "SKIP-Guard fehlt in read_loop_key"
    ok("test_read_skip_not_loop_counted")

async def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            await t()
        except AssertionError as e:
            fail(t.__name__, str(e))
        except Exception as e:
            fail(t.__name__, f"EXCEPTION: {type(e).__name__}: {e}")

    print(f"\n{'='*60}")
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print(f"{'='*60}")
    return failed


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(1 if rc else 0)
