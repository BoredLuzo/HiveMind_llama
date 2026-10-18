"""Behavioural test for the extracted stuck handler (not a source scan).

The lift to core/duo/preflight.py introduced an UnboundLocalError that the source
pins could not see: `loop_detected` is only assigned on the stuck branches but was
returned on every path. This suite calls the real function, so that class of bug
fails here instead of in a live run.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.duo.preflight import stuck_handler  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


class _Ctrl:
    max_repeats = 3

    def __init__(self, stuck=False):
        self._stuck = stuck
        self.resets = 0
        self.aborted = None

    def is_stuck(self):
        return self._stuck

    def transition(self, _s):
        pass

    def record_output(self, *_a, **_k):
        pass

    def reset_stuck_detection(self):
        self.resets += 1

    def abort(self, reason):
        self.aborted = reason


class _Ctx:
    def __init__(self, stuck=False):
        self.exec_ctrl = _Ctrl(stuck)
        self.emitted = []

    async def emit(self, ev):
        self.emitted.append(ev)
        return None


def run(coro):
    return asyncio.run(coro)


def call(tn, ta, tr, ctx, *, total=1, counts=None, lwr=0, rwf=False):
    return run(stuck_handler(tn, ta, tr, ctx=ctx, ws_str=".",
                             total_tool_rounds=total, read_counts=counts if counts is not None else {},
                             last_write_round=lwr, round_wrote_file=rwf,
                             ld_setter=lambda n=0: None))


# ── the plain path (this is what raised UnboundLocalError) ────────────────
ctx = _Ctx()
handled, ld, lwr, rwf = call("write_file", {"path": "x.py"}, "ok", ctx, total=3)
check("write_file non-stuck returns False", handled is False)
check("write_file reports no loop", ld is False)
check("write_file advances last_write_round", lwr == 3, f"(got {lwr})")
check("write_file marks progress", rwf is True)
check("write_file resets the stuck history", ctx.exec_ctrl.resets == 1)

# ── the stuck branch aborts ───────────────────────────────────────────────
# NB: only the edit/patch branch calls is_stuck() - the write_file branch only
# resets and records progress, so edit_file is the branch that can abort.
ctx = _Ctx(stuck=True)
handled, ld, _lwr, _rwf = call("edit_file", {"path": "x.py"}, "ok", ctx, total=4)
check("stuck edit_file is handled", handled is True)
check("stuck edit_file sets loop_detected", ld is True)
check("stuck edit_file aborts the control", ctx.exec_ctrl.aborted is not None)

# ── read loop counting uses the range-aware key ───────────────────────────
counts = {}
call("read_file", {"path": "x.py", "start_line": 1, "end_line": 10}, "body", _Ctx(),
     total=1, counts=counts)
check("read_file counts a range-aware key", any("#rng:" in k for k in counts),
      f"({list(counts)})")
check("read_file counts once", sum(counts.values()) == 1)

# ── run_tests success resets ─────────────────────────────────────────────
ctx = _Ctx()
call("run_tests", {}, "3 passed", ctx, total=2)
check("green run_tests resets the stuck history", ctx.exec_ctrl.resets == 1)

print()
print(f"SUMMARY stuck_handler: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
