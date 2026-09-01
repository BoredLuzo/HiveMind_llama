"""Eval: Run-Audit-Trail (core/run_audit.py) - Append, Cap 40, Load.

Audit point: "no per-tool-call audit trail". This suite verifies that
bestehende Audit-Recording deterministisch (Chat-Context gemockt, kein Server).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import context.chat as _chat
from core.run_audit import record_run_audit, load_run_audit

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


# ── Chat-Context mocken (in-memory) ────────────────────────────────────
_store: dict = {}


def _fake_mutate(chat_id, mutator):
    ctx = _store.setdefault(chat_id, {})
    mutator(ctx)


def _fake_load(chat_id):
    return _store.get(chat_id)


_chat._mutate_chat_context = _fake_mutate
_chat._load_chat_context = _fake_load

# ── Append + Load ───────────────────────────────────────────────────────
record_run_audit("audit_eval", {"event": "run_start", "run_id": "r1"})
record_run_audit("audit_eval", {"event": "tool_call", "tool": "run_bash"})
audit = load_run_audit("audit_eval")
check("append + load: 2 entries", isinstance(audit, list) and len(audit) == 2, f" ({len(audit)})")
check("first entry event", audit and audit[0].get("event") == "run_start")
check("second entry tool", audit and audit[1].get("tool") == "run_bash")
check("entries have ts", audit and all(isinstance(x.get("ts"), (int, float)) for x in audit))

# ── Cap 40: oldest entries are dropped ─────────────────────────────────
for i in range(45):
    record_run_audit("audit_eval", {"event": "n", "i": i})
audit = load_run_audit("audit_eval")
check("cap: max 40 entries", len(audit) == 40, f" ({len(audit)})")
check("cap: newest kept (i=44)", audit[-1].get("i") == 44)
check("cap: oldest dropped (i=0 gone)", all(x.get("i") != 0 for x in audit))
check("cap: i=5 kept", any(x.get("i") == 5 for x in audit))

# ── No-Op ohne chat_id / unbekannt ──────────────────────────────────────
record_run_audit("", {"event": "x"})
check("no-op empty chat_id", True)
check("load unknown -> []", load_run_audit("does_not_exist") == [])

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
