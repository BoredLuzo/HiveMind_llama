# -*- coding: utf-8 -*-
"""Every setting is either reachable from the UI or explicitly settings-only.

The engine owns settings.json and rewrites it on the next save. A key with no
UI control therefore looks editable but silently resets - the reason this test
exists. Adding a key to DEFAULT_SETTINGS now requires one of:

  * a control / feature-search entry in index.html or static/app.js, or
  * an entry in deploy/settings_ui_exempt.txt (a product decision).

A stale exemption (key now in the UI) also fails, so the list cannot rot.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from settings import DEFAULT_SETTINGS              # noqa: E402
from deploy.settings_ui_scan import (              # noqa: E402
    exempt_keys, in_ui, orphans,
)

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


keys = set(DEFAULT_SETTINGS)
exposed = in_ui(keys)
exempt = exempt_keys()
missing = orphans(keys)

check("1a every key has a UI control or an exemption", not missing,
      f"({len(missing)} missing)")
if missing:
    print("      missing:", ", ".join(sorted(missing)))
check("1b no exemption names a key that no longer exists",
      not (exempt - keys), f"({len(exempt - keys)} stale names)")
check("1c no key is both exposed and exempt",
      not (exposed & exempt), f"({len(exposed & exempt)} overlaps)")
check("2a the UI scan actually finds settings", len(exposed) > 100,
      f"({len(exposed)} exposed)")
check("2b the exemption file is small enough to review", len(exempt) < 130,
      f"({len(exempt)} exempt)")

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
