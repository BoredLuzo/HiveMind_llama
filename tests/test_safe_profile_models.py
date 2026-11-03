# -*- coding: utf-8 -*-
"""P1 (presets audit 2026-11-03): the shipped safe-profile matrix must only
name models the installer actually downloads.

A fresh install got qwen3.5:4b-ud / qwen3.5:2b written into its agent cards at
boot; those tags are not in the download catalog, so the first run failed with
"model not found".
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from deploy.fetch_models import ESSENTIAL_KEYS      # noqa: E402

MATRIX = json.loads((ROOT / "model_configs" / "safe_profile_matrix.json")
                    .read_text(encoding="utf-8"))

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


for profile, cfg in MATRIX.items():
    overrides = (cfg or {}).get("model_overrides") or {}
    check(f"{profile}: has model overrides", bool(overrides))
    bad = {role: (spec or {}).get("model")
           for role, spec in overrides.items()
           if (spec or {}).get("model") not in ESSENTIAL_KEYS}
    check(f"{profile}: every role model is installed by the catalog",
          not bad, str(bad))

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
