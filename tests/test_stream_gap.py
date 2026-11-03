# -*- coding: utf-8 -*-
"""P0 (edge audit 2026-11-03): a dropped SSE frame must be marked, not hidden.

The server caps its queue at 5000 frames; under Telegram 429 backpressure the
bridge used to persist a mid-sentence answer as complete. The producer now
emits stream_gap and the bridge marks the answer incomplete.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
BRIDGE = (ROOT / "hivemind_gateway" / "bridge.py").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


check("1 the server emits a stream_gap marker",
      '"type": "stream_gap"' in SERVER)
check("2 the bridge handles it", 'etype == "stream_gap"' in BRIDGE)
check("3 the bridge passes the flag to _finish", "truncated=truncated" in BRIDGE)
check("4 the persisted answer carries the warning",
      "Answer incomplete" in BRIDGE)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
