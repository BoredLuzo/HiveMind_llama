"""Error-cap decay tests (2026-09-18).

Live complaint: the run aborted at 6 accumulated tool errors even though
the model was actively switching strategy — errors from an abandoned
approach (browser preview blocks, run_bash timeouts) stayed counted
forever, and a run with ZERO successful writes never reset the counter.

Semantics of _note_successful_write now:
  - successful write/edit        -> counter reset to 0 (real progress)
  - successful non-write call    -> counter -1 (floor 0)
  - failed call                  -> counter unchanged
A no-progress run (6 fails, nothing in between) still reaches the cap.

Run: python tests/test_error_cap_decay.py
Exit 0 = all pass, Exit 1 = failures.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.tool_exec_helpers import _note_successful_write

passed = 0
failed = 0


def check(name, cond, msg=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {msg}")


class _RS:
    parse_errors = 0


def apply(dname, dresult, errs):
    result = type("R", (), {"verify_mutation_serial": 0})()
    rs = _RS()
    rs.parse_errors = 0
    total = [errs]
    _note_successful_write(dname, dresult, result, rs, total)
    return total[0], result, rs


def test_decay():
    # successful non-write call decrements by 1
    n, _, _ = apply("run_python", "ok output", 5)
    check("non-write success: -1", n == 4, n)
    n, _, _ = apply("read_file", "content...", 3)
    check("read success: -1", n == 2, n)
    n, _, _ = apply("browser", "navigated", 1)
    check("browser success: floor 0", n == 0, n)
    n, _, _ = apply("run_python", "ok", 0)
    check("already 0: stays 0", n == 0, n)

    # successful write resets entirely
    n, r, _ = apply("write_file", "[write_file: created 'x' (+10 lines)]", 5)
    check("write success: reset 0", n == 0, n)
    check("write success: mutation serial bumped", r.verify_mutation_serial == 1)

    # failed calls leave the counter alone
    n, _, _ = apply("run_python", "[TOOL_ERROR:RUN_PYTHON_EXEC_ERROR] boom", 4)
    check("failed call: unchanged", n == 4, n)
    n, r, _ = apply("edit_file", "[TOOL_ERROR:EDIT_FILE_OLD_TEXT_NOT_FOUND] x", 4)
    check("failed write: unchanged, no serial", n == 4 and r.verify_mutation_serial == 0, n)


if __name__ == "__main__":
    test_decay()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
