"""Behavioral test: READ-CHUNK-LOOP-FIX - range-aware read-loop key.

Verifies the fix against the false-positive read loop (live finding Run d09fb708,
2026-08-24): main.js (789 lines) is blocked from full reads by the FILE_TOO_LARGE_NEED_RANGE guard;
the coder reads in chunks (1-100, 100-250, ...). The old
path-only key (`_read_counts[fp]`) caused an abort after 4 chunk reads
(coder 4m53s, loop_detected, written_files=0).

Covered:
  - read_loop_key: range progress, error key, SKIP->None, full read.
  - the real 4-read flow (1 error + 3 chunks) stays UNDER the threshold.
  - a real loop (same range 4x / same error 4x) reaches the threshold.
  - source structure: duo_runner.py uses read_loop_key, _collect_new_explore_paths
    is range-aware.

Run: python tests/test_loop_detect_chunk_read.py
Exit 0 = all pass, Exit 1 = failures.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.duo_helpers import read_loop_key  # noqa: E402

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


FP = "main.js"
_CHUNK_OK = lambda s, e: f"[C:/workspace/samplegame/main.js lines {s}-{e} / 789]\n// code"
_ERR_TOO_LARGE = "[TOOL_ERROR:FILE_TOO_LARGE_NEED_RANGE] read_file: File 'C:/workspace/samplegame/main.js' is too large (789 lines)"


def _counts(reads):
    c = {}
    for path, s, e, r in reads:
        k = read_loop_key(path, s, e, r)
        if k is not None:
            c[k] = c.get(k, 0) + 1
    return c


def test_range_progress():
    k1 = read_loop_key(FP, 1, 100, _CHUNK_OK(1, 100))
    k2 = read_loop_key(FP, 100, 250, _CHUNK_OK(100, 250))
    k3 = read_loop_key(FP, 250, 450, _CHUNK_OK(250, 450))
    if len({k1, k2, k3}) == 3:
        ok("R1: different ranges -> different keys (progress)")
    else:
        fail("R1: ranges collapse", f"{k1} / {k2} / {k3}")
    if read_loop_key(FP, 1, 100, _CHUNK_OK(1, 100)) == k1:
        ok("R2: same range repeated -> same key")
    else:
        fail("R2: same range differs")


def test_full_and_skip():
    kfull = read_loop_key("small.js", None, None, "[small.js total lines: 50]\nlet x")
    if kfull and kfull.endswith("#rng:full"):
        ok("F1: full read without range -> #rng:full")
    else:
        fail("F1: full-read key wrong", kfull)
    if read_loop_key(FP, None, None, "[SKIP: already read in this session]") is None:
        ok("F2: [SKIP: -> None (not counted)")
    else:
        fail("F2: SKIP not None")


def test_error_key():
    e1 = read_loop_key(FP, None, None, _ERR_TOO_LARGE)
    e2 = read_loop_key(FP, None, None, _ERR_TOO_LARGE)
    if e1 == e2 and "err:FILE_TOO_LARGE_NEED_RANGE" in e1:
        ok("E1: same error -> same err key")
    else:
        fail("E1: err key wrong", f"{e1} / {e2}")
    e3 = read_loop_key(FP, None, None, "[TOOL_ERROR:BINARY_FILE] read_file: ...")
    if e3 != e1 and "err:BINARY_FILE" in e3:
        ok("E2: different error code -> different key")
    else:
        fail("E2: error codes collapse", e3)


def test_no_false_positive():
    reads = [
        (FP, None, None, _ERR_TOO_LARGE),
        (FP, 1, 100, _CHUNK_OK(1, 100)),
        (FP, 100, 250, _CHUNK_OK(100, 250)),
        (FP, 250, 450, _CHUNK_OK(250, 450)),
    ]
    mx = max(_counts(reads).values())
    if mx < 4:
        ok("N1: 1 error + 3 chunks -> max count < 4 (no false positive)")
    else:
        fail("N1: false positive remains", f"max={mx}")


def test_true_loops():
    mx_range = max(_counts([(FP, 1, 100, _CHUNK_OK(1, 100))] * 4).values())
    if mx_range >= 4:
        ok("T1: same range 4x -> threshold reached (real loop)")
    else:
        fail("T1: range loop not detected", f"max={mx_range}")
    mx_err = max(_counts([(FP, None, None, _ERR_TOO_LARGE)] * 4).values())
    if mx_err >= 4:
        ok("T2: same error 4x (hint ignored) -> threshold reached")
    else:
        fail("T2: error loop not detected", f"max={mx_err}")


def test_source_structure():
    dr = Path(__file__).parent.parent / "core" / "duo_runner.py"
    text = dr.read_text(encoding="utf-8")
    if "read_loop_key(fp, ta.get(\"start_line\"), ta.get(\"end_line\"), tr)" in text:
        ok("S1: duo_runner uses read_loop_key in the read_file branch")
    else:
        fail("S1: read_loop_key call missing")
    if "_read_counts[fp] = _read_counts.get(fp, 0) + 1" in text:
        fail("S2: old path-only key still present")
    else:
        ok("S2: old path-only key replaced")
    if "read_loop_key," in text:
        ok("S3: read_loop_key imported")
    else:
        fail("S3: import missing")
    dh = Path(__file__).parent.parent / "core" / "duo_helpers.py"
    text_h = dh.read_text(encoding="utf-8")
    if "def read_loop_key(" in text_h:
        ok("S4: read_loop_key defined in duo_helpers")
    else:
        fail("S4: read_loop_key missing")
    if "_norm = f\"{_norm}#rng:{_read_range_key(_args)}\"" in text_h:
        ok("S5: _collect_new_explore_paths is range-aware")
    else:
        fail("S5: _collect_new_explore_paths not range-aware")


def main():
    print("\n=== READ-CHUNK-LOOP-FIX (Range-aware read-loop key) ===\n")
    test_range_progress()
    test_full_and_skip()
    test_error_key()
    test_no_false_positive()
    test_true_loops()
    print("\n-- Source structure --")
    test_source_structure()
    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
