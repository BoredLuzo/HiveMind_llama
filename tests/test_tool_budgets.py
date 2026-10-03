"""Eval: Per-Run-Tool-Budgets (tools/runner.py) - websearch + install.

Audit point: "no global rate limits". Verifies the existing
Governor-Zaehler deterministisch (ContextVar-basiert, kein Server noetig).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import core.state
import tools.runner as R

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


def reset():
    R._web_search_count.set(None)
    R._install_count.set(None)


_orig_settings = core.state.settings

# ── Websearch-Budget (default 20) ───────────────────────────────────────
reset()
vals = [R._consume_web_search_budget() for _ in range(21)]
check("web default: calls 1..20 allowed", not any(vals[:20]))
check("web default: call 21 blocked", vals[20] is True)

# ── Websearch-Budget mit Settings-Override ──────────────────────────────
core.state.settings = dict(_orig_settings or {})
core.state.settings["duo_websearch_max_calls"] = 2
reset()
v = [R._consume_web_search_budget() for _ in range(3)]
check("web override 2: 1,2 allowed", v[0] is False and v[1] is False)
check("web override 2: 3 blocked", v[2] is True)
del core.state.settings["duo_websearch_max_calls"]

# ── Install-Budget (default 3) ──────────────────────────────────────────
reset()
vals = [R._consume_install_budget() for _ in range(4)]
check("install default: 1..3 allowed", not any(vals[:3]))
check("install default: 4 blocked", vals[3] is True)

core.state.settings["duo_install_max_calls"] = 1
reset()
v = [R._consume_install_budget() for _ in range(2)]
check("install override 1: 1 allowed", v[0] is False)
check("install override 1: 2 blocked", v[1] is True)
del core.state.settings["duo_install_max_calls"]

core.state.settings = _orig_settings

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
