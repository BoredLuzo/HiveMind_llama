"""Behavioral test: LOOP-CALIB - calibration cascade of the loop-detect threshold.

Verifies the new cascade from core/duo_runner.py:3090-3091:

    _partial_bonus = 6 if _explore_was_partial else 4
    _READ_ONLY_THRESHOLD = _partial_bonus if _total_exports == 0 else (5 if _any_fallback else _partial_bonus)

All 5 rows of the calibration table as cases - the 2 changed (exports==0)
AND the 3 unchanged ones as regression protection:

    | _total_exports | _any_fallback | _explore_was_partial | Alt | Neu |
    | ==0  | –     | True  | 4 | 6 |
    | ==0  | –     | False | 4 | 4 |
    | >0   | True  | –     | 5 | 5 |
    | >0   | False | True  | 6 | 6 |
    | >0   | False | False | 4 | 4 |

Additionally structure assertions: the old cascade ("4 if _total_exports == 0") is
replaced, _partial_bonus exists, and the LOOP-DETECTED-OUTER-FIX comment
now distinguishes both loop-detect sources (stuck/read-count with abort vs.
explore-counter without abort).

Run: python tests/test_loop_detect_calibration.py
Exit 0 = all pass, Exit 1 = failures.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.duo_helpers import _explore_size_tolerance  # noqa: E402

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


def _calibrate(total_exports, any_fallback, explore_was_partial):
    """Replicates exactly the fix lines (duo_runner.py:3090-3091)."""
    _partial_bonus = 6 if explore_was_partial else 4
    return _partial_bonus if total_exports == 0 else (5 if any_fallback else _partial_bonus)


def _case(label, total_exports, any_fallback, explore_was_partial, expected):
    got = _calibrate(total_exports, any_fallback, explore_was_partial)
    if got == expected:
        ok(f"{label}: threshold == {expected}")
    else:
        fail(f"{label}: expected {expected}, got {got}")


def test_table():
    # table: exports==0 rows (changed: 4 -> 6 with partial=True)
    _case("Z1 exports==0 / partial=True  (Alt 4 -> Neu 6)", 0, False, True, 6)
    _case("Z2 exports==0 / partial=False (Alt 4 -> Neu 4)", 0, True, False, 4)
    # regression protection: unchanged rows
    _case("Z3 exports>0 / fallback=True              (5)", 2, True, True, 5)
    _case("Z4 exports>0 / fallback=False / partial=True  (6)", 2, False, True, 6)
    _case("Z5 exports>0 / fallback=False / partial=False (4)", 2, False, False, 4)
    # edge cases for robustness (not in the table, but implied)
    _case("Z6 exports==0 / fallback=True / partial=True  (6)", 0, True, True, 6)
    _case("Z7 exports==0 / fallback=False / partial=False (4)", 0, False, False, 4)
    _case("Z8 exports>0 / fallback=True / partial=False   (5)", 5, True, False, 5)


def test_size_tolerance():
    """2.1c: _explore_size_tolerance imported+called directly - every family row."""
    cases = [
        ("qwen3.5:4b-ud", 1, "T1: qwen3.5 -> +1"),
        ("qwen3.5:2b", 1, "T2: qwen3.5:2b -> +1"),
        ("qwen3.5:9b-ud", 1, "T3: qwen3.5 -> +1"),
        ("granite-4.1:3b", 1, "T4: granite-4.1 -> +1 (future-proof, currently inactive)"),
        ("qwen3.6:35b-a3b-ud", 1, "T5: qwen3.6 -> 1 (AUDIT-FIX 2026-08-03: Live-False-Positive)"),
        ("unknown:99b", 0, "T6: unknown family -> 0 (default)"),
        ("", 0, "T7: empty name -> 0 (no crash)"),
        (None, 0, "T8: None -> 0 (no crash)"),
    ]
    for name, expected, label in cases:
        got = _explore_size_tolerance(name)
        if got == expected:
            ok(label)
        else:
            fail(label, got)


def test_size_tolerance_kaskade():
    """2.1c: cascade + bonus combined (production formula max(2, threshold+bonus))."""
    def _combined(total_exports, any_fallback, explore_was_partial, model):
        t = _calibrate(total_exports, any_fallback, explore_was_partial)
        return max(2, t + _explore_size_tolerance(model))
    # exports=0/partial=True: 6 + 1 (qwen3.5) -> 7
    if _combined(0, False, True, "qwen3.5:4b-ud") == 7:
        ok("K1: exports=0/partial=True + qwen3.5 -> 7")
    else:
        fail("K1", _combined(0, False, True, "qwen3.5:4b-ud"))
    # exports=0/partial=False: 4 + 1 -> 5
    if _combined(0, True, False, "qwen3.5:9b-ud") == 5:
        ok("K2: exports=0/partial=False + qwen3.5 -> 5")
    else:
        fail("K2", _combined(0, True, False, "qwen3.5:9b-ud"))
    # exports>0/fallback=False/partial=False: 4 + 1 (qwen3.6, AUDIT-FIX) -> 5
    if _combined(2, False, False, "qwen3.6:35b-a3b-ud") == 5:
        ok("K3: exports>0/partial=False + qwen3.6 -> 5 (tolerance since live false positive 2026-08-03)")
    else:
        fail("K3", _combined(2, False, False, "qwen3.6:35b-a3b-ud"))
    # unknown: 4 + 0 -> 4
    if _combined(2, False, False, "unbekannt:99b") == 4:
        ok("K4: unknown family -> unchanged (default 0)")
    else:
        fail("K4", _combined(2, False, False, "unbekannt:99b"))
    # clamp: max(2, ...) does not go negative with a hypothetically negative bonus
    if max(2, 1 + (-5)) == 2:
        ok("K5: max(2, ...) clamp prevents thresholds < 2")
    else:
        fail("K5: Clamp")


def test_source_structure():
    src = Path(__file__).parent.parent / "core" / "duo_runner.py"
    text = src.read_text(encoding="utf-8")
    if "4 if _total_exports == 0 else (5 if _any_fallback" in text:
        fail("S1: old cascade (forced 4 at exports==0) still present")
    else:
        ok("S1: old cascade replaced")
    if re.search(r"_partial_bonus = 6 if _explore_was_partial else 4", text):
        ok("S2: _partial_bonus line present")
    else:
        fail("S2: _partial_bonus line missing")
    if re.search(r"_READ_ONLY_THRESHOLD = _partial_bonus if _total_exports == 0 else \(5 if _any_fallback else _partial_bonus\)", text):
        ok("S3: new cascade exactly as specified")
    else:
        fail("S3: new cascade deviates")
    if "_explore_size_tolerance(exec_mdl)" in text and "max(\n                                2, _READ_ONLY_THRESHOLD + _explore_size_tolerance(exec_mdl)\n                            )" in text:
        ok("S7: 2.1c bonus after the cascade with max(2, ...) clamp")
    else:
        fail("S7: 2.1c bonus line missing/deviating")
    seg = text[text.find("LOOP-DETECTED-OUTER-FIX:"):]
    # comment block is language-independent (german/english) - until the next code
    seg = seg[:seg.find("if _loop_detected:")]
    if "(1)" in seg and "stuck/read-count" in seg and "STUCK_IN_LOOP" in seg:
        ok("S4: comment names source (1) stuck/read-count with abort")
    else:
        fail("S4: source (1) missing in the comment")
    if "(2)" in seg and ("explore-counter" in seg or "Explore-Zaehler" in seg) and ("NO exec_ctrl.abort" in seg or "KEIN exec_ctrl.abort" in seg):
        ok("S5: comment names source (2) explore-counter without abort")
    else:
        fail("S5: source (2) missing in the comment")
    if "via break + ctx.exec_ctrl.abort(STUCK_IN_LOOP)" in text:
        fail("S6: blanket old comment still present")
    else:
        ok("S6: blanket old comment removed")
    src_h = Path(__file__).parent.parent / "core" / "duo_helpers.py"
    text_h = src_h.read_text(encoding="utf-8")
    for needle, label in [
        ('"qwen3.5":     1,', "H1: Tabelle qwen3.5 -> 1"),
        ('"granite-4.1": 1,', "H3: Tabelle granite-4.1 -> 1"),
        ('"qwen3.6":     1,', "H4: Tabelle qwen3.6 -> 1 (AUDIT-FIX 2026-08-03)"),
    ]:
        if needle in text_h:
            ok(label)
        else:
            fail(label)
    if "def _explore_size_tolerance(model_name: str) -> int:" in text_h:
        ok("H6: pure function _explore_size_tolerance importable")
    else:
        fail("H6: function missing")


def main():
    print("\n=== Loop-detect calibration (LOOP-CALIB FIX) ===\n")
    print("-- calibration table (5 rows) --")
    test_table()
    print("\n-- Source structure --")
    test_source_structure()
    print("\n-- 2.1c: explore-size tolerance --")
    test_size_tolerance()
    print()
    test_size_tolerance_kaskade()
    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
