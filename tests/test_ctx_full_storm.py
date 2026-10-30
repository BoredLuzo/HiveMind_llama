# -*- coding: utf-8 -*-
"""CTX-FULL storm guard (2026-10-30) - the live 22.10. loop fired 40 force
compressions in 56 s because local-only compression never got ahead of the
tool-output inflow. The fix throttles force compressions to one per
MIN_GAP rounds (pure function) - the caller falls back to the existing
tiny-budget clamp in between.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from context.ctx_guard import allow_force_compress  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


check("first ever force fires (last=-10)", allow_force_compress(7, -10))
check("same round never re-fires", not allow_force_compress(5, 5))
check("gap 1 blocked", not allow_force_compress(6, 5))
check("gap 2 blocked", not allow_force_compress(7, 5))
check("gap 3 fires (min_gap honored)", allow_force_compress(8, 5))
check("gap 5 fires", allow_force_compress(10, 5))
check("min_gap parameter honored (gap 1 ok with min_gap=1)",
      allow_force_compress(6, 5, min_gap=1))
# storm replay: the 22.10 pattern - force at r, tool output pushes est back
# at r+1 -> must clamp at r+1 and r+2, fire again at r+3
seq = []
last = -10
for rnd in range(1, 9):
    if allow_force_compress(rnd, last, 3):
        seq.append("FORCE")
        last = rnd
    else:
        seq.append("clamp")
check("22.10 storm replay: no two consecutive FORCE",
      all(not (a == "FORCE" and b == "FORCE") for a, b in zip(seq, seq[1:])))
check("storm replay: exactly 3 FORCE in 8 rounds (was 8)", seq.count("FORCE") == 3)
print(f"    sequence: {seq}")

print(f"\nctx_full_storm: {passed} passed, {failed} failed")
# --- NOTFALL (Audit 30.10) ---------------------------------------------
# The brake must NEVER block when the context is really full: a throttled
# round would send max_tokens 512 against head <= 0, the server truncates or
# answers 400 and the tool-call history is poisoned.
check("emergency overrides an active brake",
      allow_force_compress(6, 5, 3, emergency=True))
check("emergency overrides a just-fired compression",
      allow_force_compress(5, 5, 3, emergency=True))
check("no emergency -> brake still applies",
      not allow_force_compress(6, 5, 3, emergency=False))
check("emergency is opt-in (default False keeps the brake)",
      not allow_force_compress(6, 5, 3))

sys.exit(1 if failed else 0)
