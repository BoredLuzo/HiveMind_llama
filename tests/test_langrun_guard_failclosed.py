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
import os
import re
import sys
from pathlib import Path




sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def _pick(env_name: str, *candidates):
    """First existing candidate (or the env override) - no user name needed."""
    _v = os.environ.get(env_name)
    if _v:
        return Path(_v)
    for _c in candidates:
        if _c.exists():
            return _c
    return candidates[0]


_ROOT = Path(__file__).resolve().parent.parent
_INSTALL = _pick("HIVEMIND_INSTALL_DIR", _ROOT.parent / "live", _ROOT / "live")
_MODELS = _pick("HIVEMIND_MODELS_DIR", _ROOT.parent.parent / "Models",
                _ROOT.parent / "Models")
_DEV = _pick("HIVEMIND_DEV_DIR", _ROOT.parent.parent / "HiveMind_dev",
             _ROOT.parent / "HiveMind_dev")

SCRIPT = _DEV / "langrun_load.py"

if not SCRIPT.exists():
    # Developer-tree script, not part of the repo (2026-11-07 CI audit).
    print("  SKIP langrun_load.py (script lives outside the repo)")
    print("\nSUMMARY langrun_guard_failclosed: 0 passed, 0 failed (skipped)")
    sys.exit(0)

sys.path.insert(0, str(_ROOT))
from tests._harness import Harness  # noqa: E402

h = Harness(style="plain", fail_code=2, pad=True)
check = h.check


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

sys.exit(h.finish("langrun_guard fail-closed",
                  suffix="(expected: 1 failed = current fail-open proven)"))
