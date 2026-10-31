# -*- coding: utf-8 -*-
"""duo_tool_rounds default 50 (2026-10-31).

A fresh install shipped 0, and _tool_round_base requires > 0 - agentic chunk
runs therefore never attached tools and "implementations" were plain text
(live Groot run, 7/7 rounds). 50 keeps the loop on by default; an explicit 0
still disables it.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import settings as _settings
from core.duo_config import DuoConfig

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


check("settings default is 50",
      _settings.DEFAULT_SETTINGS.get("duo_tool_rounds") == 50,
      _settings.DEFAULT_SETTINGS.get("duo_tool_rounds"))
check("DuoConfig default is 50", DuoConfig().tool_rounds == 50)
_srv = (ROOT / "server.py").read_text(encoding="utf-8")
check("server fallback is 50", 'settings.get("duo_tool_rounds", 50)' in _srv)
_duo = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
check("gate still requires > 0 (explicit 0 = off)",
      "ctx.duo_config.tool_rounds > 0" in _duo)

print(f"\ntool_round_default: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
