"""The injection budget must actually stop hint stacking.

`injection_budget` / `injection_msg_cap` were computed in core/budgets.py and
printed in the [BUDGETS] line, but nothing read them: the only limiter for the
~73 user-role hint sites was "3 hint phrases in the last 6 messages"
(_recovery_saturated). This suite pins the budget as a second saturation rule.

Pins:
  (a) the phrase rule still fires (3 hints in the window)
  (b) the budget rule fires on text volume alone (2 hints, big user text)
  (c) quiet history stays unsaturated
  (d) with budgets unavailable the phrase rule decides (no crash)
  (e) the real 8k budget number is the one used

Run: python tests/test_injection_budget.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


from core.tool_exec_helpers import _recovery_saturated      # noqa: E402
import core.budgets as B                                    # noqa: E402

_b16 = B.resolve(16384, 5.92, 2000, {})
B.set_active(_b16)
BUDGET = int(_b16.injection_budget)
check("active run budget is in place", BUDGET > 0, f" ({BUDGET} chars)")

HINT = "Do NOT retry the same command."

# ── (a) phrase rule ─────────────────────────────────────────────────────────
_msgs = [{"role": "system", "content": "sys"}]
for _ in range(3):
    _msgs.append({"role": "user", "content": HINT})
check("(a) 3 hint phrases in the window -> saturated", _recovery_saturated(_msgs) is True)

_msgs2 = [{"role": "system", "content": "sys"},
          {"role": "user", "content": HINT},
          {"role": "user", "content": "ordinary user turn"}]
check("(a) one hint only -> not saturated", _recovery_saturated(_msgs2) is False)

# ── (b) budget rule ─────────────────────────────────────────────────────────
_msgs3 = [{"role": "system", "content": "sys"},
          {"role": "user", "content": HINT},
          {"role": "user", "content": "x" * (BUDGET + 10)}]
check("(b) big user text saturates without 3 phrases",
      _recovery_saturated(_msgs3) is True)
check("(b) the reason is volume, not phrases",
      sum(1 for m in _msgs3 if HINT in str(m.get("content", ""))) == 1)

_msgs4 = [{"role": "system", "content": "sys"},
          {"role": "user", "content": HINT},
          {"role": "user", "content": "y" * (BUDGET // 2)}]
check("(b) text under the budget does not saturate",
      _recovery_saturated(_msgs4) is False)

# ── (c) quiet history ───────────────────────────────────────────────────────
check("(c) empty history", _recovery_saturated([]) is False)
check("(c) plain turns only",
      _recovery_saturated([{"role": "user", "content": "hello"},
                           {"role": "assistant", "content": "hi"}]) is False)

# ── (d) no active run -> the 16k fallback budget applies (same as handlers) ─
B.set_active(None)
_fallback = int(B.active().injection_budget)
_expected = int(B.resolve(16384, B.CPT_DEFAULT, 4096, {}).injection_budget)
check("(d) fallback budget is the 16k resolve",
      _fallback == _expected and _fallback > 0, f" ({_fallback})")
check("(d) phrase rule still decides",
      _recovery_saturated(_msgs) is True)
_small = [{"role": "system", "content": "sys"},
          {"role": "user", "content": "w" * max(100, _fallback // 3)}]
check("(d) small text stays under the fallback budget",
      _recovery_saturated(_small) is False)

# ── (e) the 8k budget is smaller, so it saturates earlier ───────────────────
_b8 = B.resolve(8192, 3.92, 2000, {})
B.set_active(_b8)
_mid = [{"role": "system", "content": "sys"},
        {"role": "user", "content": "z" * (int(_b8.injection_budget) + 1)}]
check("(e) 8k budget saturates at the smaller size",
      _recovery_saturated(_mid) is True
      and int(_b8.injection_budget) < BUDGET,
      f" (8k={int(_b8.injection_budget)} < 16k={BUDGET})")

B.set_active(None)
print()
print(f"SUMMARY injection_budget: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
