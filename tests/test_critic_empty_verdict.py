# -*- coding: utf-8 -*-
"""A8: an empty critic response must not fabricate a fix round.

The critic occasionally returns no text (token cap / reasoning only). The old
fallback built {"approved": False, "issues": [""]}, so the run entered a fix
round on an empty issue. Empty means "no objection"; the verify gate still
blocks unverified writes.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


check("1 the empty branch exists",
      'if not _critic_raw:' in SRC and '"verdict": "empty"' in SRC)
check("2 it stays unapproved (fail-closed)",
      '"approved": False' in SRC and '"approved": True, "issues": [], "verdict": "empty"' not in SRC)
check("3 the issue is explicit, not empty",
      "Critic returned no verdict" in SRC)
check("4 it is logged", "[CRITIC] empty verdict" in SRC)
check("5 reasoning-only critic output is read",
      '_cmsg.get("reasoning_content")' in SRC)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
