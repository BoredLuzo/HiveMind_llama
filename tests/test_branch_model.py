# -*- coding: utf-8 -*-
"""Branch-Modell: main ist der Release-Stand, dev die Integration.

Anlass: CONTRIBUTING beschrieb das Modell, aber main stand auf 1.3.x, waehrend
1.4.6 bis 1.4.10 nur auf dev lagen. Es fehlte der ausfuehrbare Schritt.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


README = (ROOT / "README.md").read_text(encoding="utf-8", errors="replace")
CONTRIB = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8", errors="replace")
SCRIPT_PATH = ROOT / "deploy" / "promote_main.bat"
SCRIPT = SCRIPT_PATH.read_text(encoding="ascii", errors="replace") if SCRIPT_PATH.exists() else ""

# ── 1. README pinnt keine Version mehr ─────────────────────────────────
check("1a no pinned release line", "Current release:" not in README)
check("1b no hardcoded zip name", "HiveMind_v1." not in README)
check("1c the release badges stay", "img.shields.io/github/v/release" in README)
check("1d the description stays", "Local multi-agent coding assistant" in README)

# ── 2. CONTRIBUTING nennt das Modell und den Schritt ───────────────────
check("2a main is documented as the released state", "Released state" in CONTRIB)
check("2b dev is documented as integration", "Integration" in CONTRIB)
check("2c the promotion step is named", "promote_main.bat" in CONTRIB)

# ── 3. Das Skript ist sicher ───────────────────────────────────────────
check("3a the promote script exists", SCRIPT_PATH.exists())
check("3b it fast-forwards only", "--ff-only" in SCRIPT)
check("3c it never forces", "--force" not in SCRIPT and "push -f" not in SCRIPT)
check("3d it returns to dev", "git switch dev" in SCRIPT)
check("3e it stops on error", ":fail" in SCRIPT and "exit /b 1" in SCRIPT)

print()
print(f"SUMMARY branch_model: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
