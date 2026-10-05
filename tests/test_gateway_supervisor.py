# -*- coding: utf-8 -*-
"""P8-lite supervisor store: heartbeat freshness + clear (pure logic).

The running gateway heartbeats every 5 s; /gateway/status derives
liveness from freshness (<=15 s = ~3 missed beats). start/stop subprocess
wrappers live in server.py and are intentionally not covered here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from infra.gateway_supervisor import clear, note_heartbeat, status

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


def main() -> int:
    clear()
    check("no heartbeat -> stopped", status(now=1000.0)["running"] is False)

    note_heartbeat(42, {"paired": True}, now=1000.0)
    st = status(now=1005.0)
    check("fresh heartbeat -> running with pid",
          st["running"] is True and st["pid"] == 42)
    check("heartbeat data merged when running", st.get("paired") is True)

    st = status(now=1000.0 + 15.0)
    check("exactly max age still runs", st["running"] is True)
    st = status(now=1000.0 + 16.0)
    check("stale heartbeat (>max age) -> stopped",
          st["running"] is False and st["pid"] == 0
          and "paired" not in st)

    note_heartbeat(7, {}, now=2000.0)
    clear()
    check("clear() stops immediately",
          status(now=2001.0)["running"] is False)

    print()
    print(f"passed={passed} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
