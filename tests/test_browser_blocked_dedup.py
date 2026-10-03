# -*- coding: utf-8 -*-
"""Browser blocked-navigation dedup test (2026-09-18).

Live bug: with the action-approval preview unreachable, the coder retried
navigate against the same blocked server three times (localhost, 127.0.0.1,
host/path variants) before giving up. The run-scoped memory now escalates
on the second attempt at the same block.

Run: python tests/test_browser_blocked_dedup.py
Exit 0 = all pass, Exit 1 = failures.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.browser import _check_blocked_repeat, _blocked_navigations

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


def test_escalation():
    _blocked_navigations.set({})
    err = "loopback/private IPs are not navigable (only the tool's own file server origin)"

    r1 = _check_blocked_repeat("http://localhost:8080/", err)
    check("first attempt: normal error", r1 is None, str(r1)[:80])

    r2 = _check_blocked_repeat("http://localhost:8080/index.html", err)
    check("second attempt (path variant): escalation",
          r2 is not None and "attempt #2" in r2 and "STOP using the browser" in r2, str(r2)[:140])

    # a different host+error combination is a DIFFERENT block (first attempt)
    r3 = _check_blocked_repeat("http://127.0.0.1:9000/", "private IPs are not navigable")
    check("different block: counted separately", r3 is None, str(r3)[:80])

    r4 = _check_blocked_repeat("http://127.0.0.1:9000/x", "private IPs are not navigable")
    check("different block: escalates on its own second attempt",
          r4 is not None and "attempt #2" in r4, str(r4)[:100])


def test_independence():
    _blocked_navigations.set({})
    check("reset works", _check_blocked_repeat(
        "http://localhost:1/", "e") is None)


if __name__ == "__main__":
    test_escalation()
    test_independence()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
