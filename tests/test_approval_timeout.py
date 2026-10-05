"""Approval timeout: fail-closed deny, no crash (2026-10-03 review fix).

The old code never created the auto-off Event, so ANY gated call with
duo_action_approval_timeout_s > 0 crashed the gate wait (AttributeError
on None.wait) - and the timeout then auto-APPROVED unseen actions.
Pinned here:
  - with timeout_s=1 and no answer: the gate returns DENY (fail closed),
    not a crash, not an approval
  - the card checkbox (auto_timeout_off) switches to manual wait without
    crashing either
  - a late decision after the deny is discarded once (_approval_expired)

Run: python tests/test_approval_timeout.py
"""
import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

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


def main():
    import core.state as _state
    import infra.run_control as rc
    from tools import runner as tr

    wd = Path(tempfile.mkdtemp(prefix="hvm_apprto_"))
    try:
        _state.settings.update({
            "duo_action_approval_enabled": True,
            "duo_action_approval_timeout_s": 1,
        })
        tr._approval_pre_decisions.clear()
        tr._approval_expired.clear()
        tr._approval_auto_off_events.clear()
        tr._current_run_id.set("apto-run-1")
        tr._tool_loop_emit.set(None)

        t0 = time.monotonic()
        try:
            verdict = asyncio.run(tr._check_action_approval(
                "run_bash", {"cmd": "echo hi"}, str(wd)))
            kind = verdict[0] if isinstance(verdict, tuple) else verdict
            txt = str(verdict[1]) if isinstance(verdict, tuple) and len(verdict) > 1 else ""
            dt = time.monotonic() - t0
            if kind == "DENY" and "APPROVAL_TIMEOUT" in txt and dt < 10:
                ok(f"timeout after {dt:.1f}s -> DENY (fail closed, no crash)")
            else:
                fail("fail-closed deny", f"kind={kind} dt={dt:.1f}s txt={txt[:80]}")
        except AttributeError as e:
            fail("crash instead of deny", f"AttributeError: {e}")
            return failed

        if tr._approval_expired.get("apto-run-1"):
            ok("late-decision guard armed (next click discarded once)")
        else:
            fail("approval_expired flag")

        # checkbox path: auto_timeout_off must not crash, must keep waiting
        tr._approval_expired.clear()
        rc._pause_events.clear()
        rc._user_answers.clear()

        async def _uncheck_then_answer():
            task = asyncio.create_task(tr._check_action_approval(
                "run_bash", {"cmd": "echo second"}, str(wd)))
            await asyncio.sleep(0.3)
            # simulate the decide endpoint's auto_timeout_off branch
            _ev = tr._approval_auto_off_events.get("apto-run-2")
            if _ev:
                _ev.set()
                ok("auto-off event EXISTS and is settable (was: never created)")
            else:
                fail("auto-off event missing")
            await asyncio.sleep(0.3)
            rc._user_answers["apto-run-2"] = "3|not now"
            rc._pause_events["apto-run-2"].set()
            return await task

        tr._current_run_id.set("apto-run-2")
        try:
            verdict2 = asyncio.run(_uncheck_then_answer())
            kind2 = verdict2[0] if isinstance(verdict2, tuple) else verdict2
            if kind2 == "DENY" and "did not approve" in str(verdict2[1]):
                ok("checkbox path: manual wait survives, deny applied")
            else:
                fail("checkbox path", f"kind={kind2}")
        except AttributeError as e:
            fail("checkbox path crash", f"AttributeError: {e}")
    finally:
        _state.settings.update({
            "duo_action_approval_enabled": False,
            "duo_action_approval_timeout_s": 0,
        })
        shutil.rmtree(str(wd), ignore_errors=True)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
