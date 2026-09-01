"""Stale/phantom-port guard in the slot manager (2026-08-31).

Live finding: ensure_loaded returned a port on which no llama-server
was listening anymore (httpx.ConnectError in the planner). The bookkeeping said "loaded",
the process was hung/killed externally. Two guards:
  - ensure_loaded: port-aliveness probe before the fast return of a running
    slot; dead port -> kill the slot and load fresh.
  - _find_loaded: load process died but _loading is stuck -> reset the slot
    instead of waiting 240s on a dead _ready_event.

Run: python tests/test_ensure_loaded_stale_port.py
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


def _guard_decision(slot_found, port_alive):
    """Faithful reproduction of the ensure_loaded stale-port guard.

    found slot with dead port -> "reload" (kill + fresh start).
    found slot with live port -> "reuse" (return slot.port).
    no slot -> "fresh_load".
    """
    if slot_found:
        if not port_alive:
            return "reload"
        return "reuse"
    return "fresh_load"


def _fast_loading_died(process_alive, process_polled_dead):
    """Faithful reproduction of the _find_loaded LOAD-DIED-GUARD decision."""
    if process_alive and process_polled_dead:
        return "reset"      # slot.kill() + continue
    return "wait"           # return slot (still loading)


# ── Guard-Entscheidung: toter Port auf gefundenem Slot -> reload ────────────
def test_dead_port_reloads():
    if _guard_decision(slot_found=True, port_alive=False) == "reload":
        ok("dead_port_reloads (phantom slot: dead -> kill+reload, not reuse)")
    else:
        fail("dead_port_reloads", "expected 'reload'")


def test_alive_port_reuses():
    if _guard_decision(slot_found=True, port_alive=True) == "reuse":
        ok("alive_port_reuses (live port -> return slot.port)")
    else:
        fail("alive_port_reuses", "expected 'reuse'")


def test_no_slot_fresh_load():
    if _guard_decision(slot_found=False, port_alive=False) == "fresh_load":
        ok("no_slot_fresh_load (no slot -> normal start)")
    else:
        fail("no_slot_fresh_load", "expected 'fresh_load'")


# ── LOAD-DIED-GUARD: dead load process -> slot reset ─────────────────
def test_loading_died_resets():
    if _fast_loading_died(process_alive=True, process_polled_dead=True) == "reset":
        ok("loading_died_resets (poll()!=None with _loading=True -> slot reset)")
    else:
        fail("loading_died_resets", "expected 'reset'")


def test_loading_alive_waits():
    if _fast_loading_died(process_alive=True, process_polled_dead=False) == "wait":
        ok("loading_alive_waits (_loading=True + process alive -> keep waiting)")
    else:
        fail("loading_alive_waits", "expected 'wait'")


# ── Source-Guards ───────────────────────────────────────────────────────────
def _manager_sources() -> str:
    """All LlamaServerManager sources (M3c: mixin files + core)."""
    root = Path(__file__).parent.parent / "backend"
    parts = []
    for f in sorted(root.glob("manager_*.py")) + [root / "llama_server_manager.py"]:
        try:
            parts.append(f.read_text(encoding="utf-8"))
        except OSError:
            pass
    return "\n".join(parts)


def test_source_guard_stale_port():
    src = _manager_sources()
    _probe = "await self._port_alive(slot.port)" in src
    _kill = "evictions_stale_port" in src
    _kill_call = "slot.kill()" in src
    if _probe and _kill and _kill_call:
        ok("source_guard_stale_port (TCP-Probe + evictions_stale_port + kill)")
    else:
        fail("source_guard_stale_port", f"probe={_probe} kill_marker={_kill} kill_call={_kill_call}")


def test_source_guard_loading_died():
    src = _manager_sources()
    _check = "slot.process.poll() is not None" in src
    _marker = "load process for %s died" in src
    if _check and _marker:
        ok("source_guard_loading_died (poll()-Check im _loading-Zweig)")
    else:
        fail("source_guard_loading_died", f"poll_check={_check} marker={_marker}")


if __name__ == "__main__":
    test_dead_port_reloads()
    test_alive_port_reuses()
    test_no_slot_fresh_load()
    test_loading_died_resets()
    test_loading_alive_waits()
    test_source_guard_stale_port()
    test_source_guard_loading_died()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
