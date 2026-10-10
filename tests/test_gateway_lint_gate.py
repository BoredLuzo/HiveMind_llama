"""Gateway WP1: lint gate coverage PROOF (brief: WORKING RULES).

"The lint gate no_new_silent_excepts MUST include hivemind_gateway/;
prove that in WP1 with an intentionally violating test case."

Part 1 proves the gate SEES the package: a temp file inside
hivemind_gateway/ with `except Exception: pass` is counted by the same
collector the regression suite uses, and flips the gate decision to
FAIL. Part 2 proves the package is currently CLEAN against the baseline.
The probe file is removed in a finally block.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_no_new_silent_excepts import BASELINE, _count_file, collect

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


PKG = ROOT / "hivemind_gateway"

# ── 1. the gate sees hivemind_gateway/ ──────────────────────────────────
probe = PKG / "_lint_probe_violation.py"
try:
    probe.write_text(
        "def f():\n"
        "    try:\n"
        "        x = 1\n"
        "    except Exception:\n"
        "        pass\n"
        "    try:\n"
        "        y = 2\n"
        "    except:  # noqa - intentionally bare\n"
        "        pass\n",
        encoding="utf-8")
    n_pass, n_broad = _count_file(probe)
    check("collector counts except-pass", n_pass == 2, f" (got {n_pass})")
    check("collector counts broad except", n_broad == 2, f" (got {n_broad})")
    current, parse_errors = collect()
    rel = probe.relative_to(ROOT).as_posix()
    check("collect() includes the probe file", rel in current)
    baseline_entry = json.loads(BASELINE.read_text(encoding="utf-8")) \
        if BASELINE.exists() else {}
    old = baseline_entry.get(rel, {"except_pass": 0, "broad_except": 0})
    gate_fails = (current[rel]["except_pass"] > old["except_pass"]
                  or current[rel]["broad_except"] > old["broad_except"])
    check("gate decision flips to FAIL on violation", gate_fails)
finally:
    try:
        probe.unlink()
    except OSError:
        pass
check("probe cleaned up", not probe.exists())

# ── 2. the package is currently clean vs baseline ───────────────────────
current, parse_errors = collect()
baseline = json.loads(BASELINE.read_text(encoding="utf-8")) \
    if BASELINE.exists() else {}
bad = []
for p in sorted(PKG.glob("*.py")):
    rel = p.relative_to(ROOT).as_posix()
    cur = current.get(rel, {"except_pass": 0, "broad_except": 0})
    old = baseline.get(rel, {"except_pass": 0, "broad_except": 0})
    if cur["except_pass"] > old["except_pass"] \
            or cur["broad_except"] > old["broad_except"]:
        bad.append(rel)
check("hivemind_gateway/ introduces no silent-except debt", bad == [],
      f" ({bad})" if bad else "")

print()
print(f"passed={passed} failed={failed}")
sys.exit(0 if failed == 0 else 1)
