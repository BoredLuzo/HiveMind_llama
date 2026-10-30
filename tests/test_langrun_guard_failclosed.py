# -*- coding: utf-8 -*-
"""P7 (Phase-A letzte Nachbesserung, 2026-10-29): the INVERSE guard in
langrun_load.py must abort on llama_process_count() == -1 too.

The guard loads a RUNNING engine, so it aborts when count == 0 ("no
llama-server running"). But -1 (tasklist failed = UNKNOWN) passes the
current check `if _pf_procs() == 0:` — a load run against a possibly
half-alive engine would start on unknown state (fail-open).

DESIRED: count <= 0 aborts (0 = none, -1 = unknown). REWORK ONLY AFTER
USER'S WORD — this test is deliberately RED until then and NOT registered
in run_regressions.py.
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT = Path(r"C:\Users\NtheP\Desktop\HiveMind_dev\langrun_load.py")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


src = SCRIPT.read_text(encoding="utf-8")
guard = re.search(r"if _pf_procs\(\)\s*(==|<=|<|!=)\s*(-?\d+)", src)
if not guard:
    check("inverse guard present in langrun_load.py", False, "(pattern not found)")
else:
    op, num = guard.group(1), guard.group(2)
    # desired: the guard fires for 0 AND -1  ->  op must be <=/< with 1 or 0,
    # or an explicit `in (0, -1)`; today it is `== 0`, which lets -1 through
    fires_on_minus1 = (op in ("<=", "<") and num in ("1", "0")) or op == "!="
    check("inverse guard aborts on count == -1 (fail-closed)",
          fires_on_minus1,
          f"(today: `if _pf_procs() {op} {num}` — -1 = UNKNOWN passes and "
          f"the load run would start on unknown engine state)")

print()
print("PROPOSAL (rework only after user's word):")
print("  langrun_load.py: change `if _pf_procs() == 0:` to")
print("  `if _pf_procs() <= 0:` (0 = no engine, -1 = tasklist unknown -> abort).")

print(f"\nlangrun_guard fail-closed: {passed} passed, {failed} failed "
      f"(expected: 1 failed = current fail-open proven)")
sys.exit(0 if failed == 0 else 2)
