"""Behavioral test: STUCK-BASH-RESET — stuck-detector reset on good tool calls
+ higher threshold (live finding sampleproj run 2026-08-26 06:34).

Finding: the coder explored the external sampleproj workspace via run_bash probing
(identical maze-grid dumps because pre-explore crashed and no file
content was delivered). 3x identical run_bash output without a write in between ->
_stuck_handler (duo_runner, run_bash branch) -> is_stuck() -> STUCK_IN_LOOP ->
the run aborted although the model was in the middle of its analysis ("Let me map these
properly with coordinates:", 90s after a previous successful edit).

Fix (2026-08-26):
  1. Default threshold max_repeats_stuck 3 -> 5 (was 1-2 calls too early).
  2. Successful run_tests (green verification = progress) invalidate
     the stuck history like successful writes.
  3. Stuck messages use the configured threshold instead of a hard "3x".

Run: python tests/test_stuck_bash_reset_threshold.py
Exit 0 = all pass, Exit 1 = failures.
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


# ── A: Behavioral - ExecutionController threshold ────────────────────────────

def test_threshold_not_early():
    """T1: 4x identical run_bash output -> NOT stuck (old: 3x -> stuck)."""
    from hive_functions.loop_machine import ExecutionController
    ctrl = ExecutionController(max_iterations=10)
    for _ in range(4):
        ctrl.record_output("10 0 1 4 0 1 2 1 1", tool_name="run_bash")
    if ctrl.is_stuck():
        fail("T1: 4x same bash output -> stuck (too early)")
    else:
        ok("T1: 4x same bash output -> NOT stuck")


def test_threshold_real_loop():
    """T2: 5x identical output -> stuck (real loop still detected)."""
    from hive_functions.loop_machine import ExecutionController
    ctrl = ExecutionController(max_iterations=10)
    for _ in range(5):
        ctrl.record_output("10 0 1 4 0 1 2 1 1", tool_name="run_bash")
    if ctrl.is_stuck():
        ok("T2: 5x same bash output -> stuck")
    else:
        fail("T2: 5x identical bash output must be stuck")


def test_reset_between_calls():
    """T3: reset in between (good tool call) -> counter starts fresh."""
    from hive_functions.loop_machine import ExecutionController
    ctrl = ExecutionController(max_iterations=10)
    for _ in range(2):
        ctrl.record_output("dump", tool_name="run_bash")
    ctrl.reset_stuck_detection()  # simulates a successful write / green tests
    for _ in range(4):
        ctrl.record_output("dump", tool_name="run_bash")
    if ctrl.is_stuck():
        fail("T3: reset in between -> 4x after must not be stuck")
    else:
        ok("T3: reset in between -> not stuck")


# ── B: Wiring - duo_runner _stuck_handler ───────────────────────────────────

def test_run_tests_reset_wired():
    """W1: run_tests success resets the stuck history (like writes)."""
    src = (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(encoding="utf-8")
    _start = src.find('if tn == "run_bash":')
    assert _start > 0, "run_bash branch missing in _stuck_handler"
    _seg = src[_start:src.find("async def _evict_model_handler", _start)]
    assert '"run_tests"' in _seg, "run_tests branch missing in _stuck_handler"
    assert "reset_stuck_detection()" in _seg, "run_tests reset missing"
    assert "tool_call_failed" in _seg, "run_tests error guard missing"
    ok("W1: run_tests reset wired")


def test_stuck_message_dynamic():
    """W2: the stuck message uses max_repeats instead of a hard '3x'."""
    src = (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(encoding="utf-8")
    for needle in ('"Same test output 3', '"Same {tn} on same file 3'):
        if needle in src:
            fail(f"W2: hard '3x' message still present: {needle}")
            return
    if "max_repeats" in src:
        ok("W2: messages use max_repeats")
    else:
        fail("W2: max_repeats missing")


def test_default_threshold_in_class():
    """W3: the ExecutionController default is 5 (commented)."""
    src = (Path(__file__).parent.parent / "hive_functions" / "loop_machine.py").read_text(encoding="utf-8")
    if "max_repeats_stuck: int = 5" in src:
        ok("W3: default max_repeats_stuck = 5")
    else:
        fail("W3: default is not 5")


async def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t()
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
