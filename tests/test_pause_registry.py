# -*- coding: utf-8 -*-
"""P1 (edge audit 2026-11-03): a resolved pause must not eat later aborts.

`_RESUME_SIGNALS` survived a resume, so /abort returned "abort_during_pause"
and never set the real abort event - the run was unkillable from the UI.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from infra import run_control as rc            # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# A manual pause whose resume has NOT been signalled is still pending.
rc.get_resume_signal("run-waiting")
check("1 an unset resume signal is a pending pause",
      rc.is_pause_pending("run-waiting"))

# After the resume the pause is over; the leftover signal must not count.
rc.signal_resume("run-waiting")
check("2 a signalled resume is NOT a pending pause",
      not rc.is_pause_pending("run-waiting"))

rc.cleanup_pause("run-waiting")
check("3 cleanup removes the resume signal",
      "run-waiting" not in rc._RESUME_SIGNALS)
check("4 cleanup removes the abort-during-pause signal",
      "run-waiting" not in rc._ABORT_DURING_PAUSE_SIGNALS)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
