# -*- coding: utf-8 -*-
"""MoE expert cache (--moe-cache-mib): opt-in, budgeted, retried without tag."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from settings import DEFAULT_SETTINGS              # noqa: E402

ML = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
ME = (ROOT / "backend" / "manager_evict.py").read_text(encoding="utf-8")
HTML = (ROOT / "index.html").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


check("1 moe_cache_mib defaults to off", DEFAULT_SETTINGS.get("moe_cache_mib") == 0)
check("2 the loader passes --moe-cache-mib", '"--moe-cache-mib"' in ML)
check("3 the flag is capability-checked",
      '_help_supports("--moe-cache-mib")' in ML)
check("4 the cache is part of the VRAM pre-flight",
      "extra_needed_mib=_moe_cache_used" in ML
      and "extra_needed_mib" in ME)
check("5 a failed load retries without the cache", "moe_cache=False" in ML)
check("6 MoE threads use half of the logical CPUs", "_logical // 2" in ML)
check("7 the model tab has the input", 'id="moe-cache-mib"' in HTML)
check("8 the UI explains the trade-off", "moe-cache-mib" in HTML
      and "#30697" in HTML)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
