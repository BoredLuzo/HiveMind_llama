# -*- coding: utf-8 -*-
"""PREFLIGHT FAIL-CLOSED — Phase-A-Rest P9 (2026-10-29).

Demand: an UNKNOWN model (alias/path that resolves to nothing) and a
FAILING tasklist must BLOCK the guard (fail-closed). Current behavior is
fail-open — these two tests demonstrate that (they FAIL on purpose until
the rework is approved). NOT registered in run_regressions.py until then.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from unittest import mock
from backend import llama_preflight as LP

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# 1. unknown model must block (today: gguf_size_gb -> 0.0, guard is then a
#    bare 8 GB check and PASSES at 22 GB free = fail-open)
blocks_unknown = LP.ram_guard_blocks("definitely-not-a-known-model-xyz", 22.5)
check("ram_guard blocks an UNKNOWN model (fail-closed)", blocks_unknown,
      "(today: False = fail-open, guard would let a bench with an unknown "
      "model start without any size check)")

# 2. failing tasklist must block (today: exception swallowed, count stays
#    0 = "free" = fail-open). We simulate a broken tasklist.
def _boom(*a, **k):
    raise OSError("tasklist broken (simulated)")

with mock.patch.object(LP.subprocess, "run", side_effect=_boom):
    count = LP.llama_process_count()
check("llama_process_count reports UNKNOWN (-1) when tasklist fails",
      count == -1,
      f"(today: {count} = fail-open, a second llama process would be "
      f"overlooked)")

# the desired composed behavior: preflight must ABORT when either leg is unknown
print()
print("PROPOSAL (rework only after user's word):")
print("  1. ram_guard_blocks: return True when gguf_size_gb() == 0.0")
print("     (unknown model -> cannot verify size -> block).")
print("  2. llama_process_count: on OSError/SubprocessError return -1")
print("     (unknown, not 0); preflight blocks when count != 0.")
print("  3. keep the two positive paths tested in test_load_preflight.py.")

print(f"\nfail-closed: {passed} passed, {failed} failed "
      f"(expected: 2 failed = current fail-open proven)")
sys.exit(0 if failed == 0 else 2)  # exit 2 marks the known red state
