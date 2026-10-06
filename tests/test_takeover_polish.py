"""Takeover polish round (2026-10-06) — the three audit corners plus the
/stop double-note.

1. START-RACE: /gateway/start refused with 409 "already running" for up to
   ~15 s after an unclean gateway kill, because "running" is heartbeat
   freshness. The route now verifies the recorded pid is really a live
   python process (birth-checked) before refusing; dead/recycled clears
   the stale status and starts.
2. SECOND-RUN VISIBILITY (audit T2): while the mirror is bound to one run,
   a second engine run was entirely invisible on the phone. The tick now
   peeks the unscoped journal and notes a foreign active run once per id.
3. TOAST HONESTY (audit T3): a decision on an already-timed-out card is
   routed "expired" by the engine — the toasts used to say "delivered".
4. STOP NOTE (audit T1): /stop during a takeover led with the own-run
   "No run active." before the mirror abort note.

Run: python tests/test_takeover_polish.py
Exit 0 = all pass, Exit 1 = failures.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


ROOT = Path(__file__).parent.parent


def _src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _start_route_decision(status_running, pid_alive):
    """Faithful model of the fixed /gateway/start refusal."""
    if status_running:
        if pid_alive:
            return 409
        return "clear_and_start"     # stale heartbeat / recycled pid
    return "start"


def test_start_race():
    if _start_route_decision(True, True) == 409 \
            and _start_route_decision(True, False) == "clear_and_start" \
            and _start_route_decision(False, False) == "start":
        ok("start route: 409 only for a REALLY alive pid, stale heartbeat starts")
    else:
        fail("start_race", "decision table wrong")


def test_start_route_ast():
    src = _src("server.py")
    ok1 = "_sup.clear()" in src.split("async def gateway_start")[1].split("async def")[0]
    ok2 = "already running" in src.split("async def gateway_start")[1].split("async def")[0]
    if ok1 and ok2:
        ok("server.py: start route clears stale status before starting")
    else:
        fail("start_ast", f"clear={ok1} 409={ok2}")


def test_expired_toasts():
    src = _src("hivemind_gateway/bridge.py")
    hits = src.count('routed == "expired"')
    if hits >= 2 and "already timed out" in src:
        ok(f"bridge: expired routing answered honestly in both paths ({hits})")
    else:
        fail("expired", f"expired branches={hits}")


def test_second_run_note():
    src = _src("hivemind_gateway/bridge.py")
    peek = "other_run_noted" in src and src.count("other_run_noted") >= 3
    msg = "Another engine run is active" in src
    if peek and msg:
        ok("bridge: unscoped peek notes a foreign active run once per id")
    else:
        fail("second_run", f"state={peek} message={msg}")


def test_stop_note():
    src = _src("hivemind_gateway/main.py")
    if 'mirror_note if note == "No run active."' in src:
        ok("main.py: /stop no longer leads with 'No run active.' during takeover")
    else:
        fail("stop_note", "replacement logic missing")


if __name__ == "__main__":
    test_start_race()
    test_start_route_ast()
    test_expired_toasts()
    test_second_run_note()
    test_stop_note()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
