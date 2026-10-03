# -*- coding: utf-8 -*-
"""Tests: dynamische Output-Reserve (context/ctx_guard.clamp_request_max_tokens)
+ floor-basierte Kompressionsschwelle (resolve_compress_threshold mit Reserve-Cap).

Run: python tests/test_output_reserve.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from context.ctx_guard import (
    clamp_request_max_tokens as _clamp,
    resolve_compress_threshold as _thr,
)

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


# ── clamp_request_max_tokens ────────────────────────────────────────────────
# Volles Budget, solange viel Platz ist (est klein).
check("C1: viel Platz -> volles Budget",
      _clamp(est_tokens=10000, ctx_tokens=39936, max_output=12000) == 12000,
      f" got={_clamp(est_tokens=10000, ctx_tokens=39936, max_output=12000)}")
# Nahe der Schwelle (~78%, est ~25k real ~31k) -> Budget auf freien Platz geklemmt.
c2 = _clamp(est_tokens=25000, ctx_tokens=39936, max_output=12000)
check("C2: nahe Schwelle -> geklemmt (< Budget, > 0)",
      0 < c2 < 12000, f" got={c2}")
# Kritisch -> Floor 256.
check("C3: kritisch -> Min-Floor",
      _clamp(est_tokens=37000, ctx_tokens=39936, max_output=12000) == 256,
      f" got={_clamp(est_tokens=37000, ctx_tokens=39936, max_output=12000)}")
# max_output ist Obergrenze; nahe Cap wird darunter geklemmt.
c4 = _clamp(est_tokens=5000, ctx_tokens=8192, max_output=800)
check("C4: Budget ist Obergrenze", 0 < c4 <= 800, f" got={c4}")
# ctx 0 -> Budget-Fallback.
check("C5: ctx 0 -> Budget", _clamp(est_tokens=10, ctx_tokens=0, max_output=600) == 600)

# ── Floor-basierte Schwelle mit Reserve-Cap (P1 bindet) ─────────────────────
# ctx 39936, floor 0.78, Reserve-Cap 4096 + overflow 1024 -> P1 31150 bindet.
t1 = _thr(ctx_tokens=39936, num_predict=4096,
          auto_floor=0.78, overflow_reserve=1024)
check("T1: floor 0.78 bindet (31150)",
      t1 == int(0.78 * 39936), f" got={t1}")
# Grosse num_predict WUERDE ungekappt die Schwelle auf P2 (~67%) druecken -
# genau deshalb kappt der Caller (duo_runner) die Reserve auf ein kleines Cap,
# damit P1 (Floor) bindet.
t2 = _thr(ctx_tokens=39936, num_predict=12000, ui_threshold=0,
          auto_floor=0.78, overflow_reserve=1024)
check("T2: ungekappt 12000 -> P2 bindet (26912)",
      t2 == 39936 - (12000 + 1024), f" got={t2}")
# UI-Override bleibt exakt.
check("T3: UI-Override exakt",
      _thr(ctx_tokens=39936, ui_threshold=20000) == 20000)

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
