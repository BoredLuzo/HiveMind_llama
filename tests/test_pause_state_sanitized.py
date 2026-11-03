# -*- coding: utf-8 -*-
"""B8: pause_state must sanitize chat_id before building a filename."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from context import pause_state as P          # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


tmp = Path(tempfile.mkdtemp())
P.init_pause_state(tmp)

p = P._pause_path("../../evil")
check("1 traversal cannot leave the sessions dir",
      p is not None and p.parent == tmp, str(p))
check("2 the filename keeps only safe characters",
      p is not None and p.name == ".evil.pause_state.json", p.name if p else "?")
check("3 a hostile id with no safe characters is refused",
      P._pause_path("../..") is None)
check("4 a normal id is unchanged",
      P._pause_path("a1b2-c3").name == ".a1b2-c3.pause_state.json")

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
