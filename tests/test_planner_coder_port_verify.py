"""Planner=Coder fast-path port + ctx verification (2026-08-31 / 2026-09-06).

Live finding (1): the fast path "Planner=Coder -> no reload" blindly trusted the
planner port. On a phantom slot (dead port) the coder inherited the dead port
and crashed with ConnectError. Fix: port liveness check before cache adoption.

Live finding (2, 2026-09-06): the manager can silently DOWNSCALE the slot ctx
(PRE-FLIGHT 768-MiB-Marge -> 4096 bei knappem VRAM). Der Coder darf einen
degradierten Planner-Slot dann NICHT erben — Reuse nur, wenn der Slot mit
>= dem angeforderten ctx laeuft (slot_ctx_ok); sonst sauberer Reload mit vollem
Coder-ctx (strict).

Run: python tests/test_planner_coder_port_verify.py
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


def _fast_path_decision(planner_is_coder, port_available, port_alive, slot_ctx_ok):
    """Faithful reproduction of the duo_runner Planner=Coder fast-path guard."""
    if (planner_is_coder and port_available and port_alive and slot_ctx_ok):
        return "cache"      # adopt port, no reload
    return "load"           # normal coder load


# ── Entscheidungen ──────────────────────────────────────────────────────────
def test_dead_port_ignores_cache():
    if _fast_path_decision(True, True, False, True) == "load":
        ok("dead_port_ignores_cache (Planner=Coder aber Port tot -> normaler Load)")
    else:
        fail("dead_port_ignores_cache", "erwartet 'load'")


def test_alive_port_uses_cache():
    if _fast_path_decision(True, True, True, True) == "cache":
        ok("alive_port_uses_cache (Planner=Coder + Port lebt + ctx ok -> Cache)")
    else:
        fail("alive_port_uses_cache", "erwartet 'cache'")


def test_no_port_loads():
    if _fast_path_decision(True, False, False, False) == "load":
        ok("no_port_loads (kein Planner-Port -> normaler Load)")
    else:
        fail("no_port_loads", "erwartet 'load'")


def test_not_coder_loads():
    if _fast_path_decision(False, True, True, True) == "load":
        ok("not_coder_loads (Planner != Coder -> normaler Load)")
    else:
        fail("not_coder_loads", "erwartet 'load'")


def test_degraded_slot_forces_reload():
    # Kernfall aus dem Log: Planner-Slot laeuft nach ctx-down nur mit 4096,
    # Coder braucht 40960 -> KEIN Reuse, sonst klemmt [CTX-ACTUAL] den Coder
    # fuer den ganzen Run auf den zu kleinen Slot fest.
    if _fast_path_decision(True, True, True, False) == "load":
        ok("degraded_slot_forces_reload (Planner=Coder aber Slot-ctx < Coder-ctx -> Reload voller ctx)")
    else:
        fail("degraded_slot_forces_reload", "erwartet 'load'")


# ── Source-Guard ────────────────────────────────────────────────────────────
def test_source_guard_planner_coder_verify():
    src = (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(encoding="utf-8")
    _has_liveness = "_plan_port_alive" in src
    _has_ctx_ok = "_planner_ctx_ok" in src
    _gate = "and _planner_ctx_ok" in src
    _mismatch = "PLANNER=CODER-CTX-MISMATCH" in src
    if _has_liveness and _has_ctx_ok and _gate and _mismatch:
        ok("source_guard_planner_coder_verify (liveness + slot-ctx gate vor Cache-Adoption)")
    else:
        fail("source_guard_planner_coder_verify",
             f"liveness={_has_liveness} ctx_ok={_has_ctx_ok} gate={_gate} mismatch={_mismatch}")


if __name__ == "__main__":
    test_dead_port_ignores_cache()
    test_alive_port_uses_cache()
    test_no_port_loads()
    test_not_coder_loads()
    test_degraded_slot_forces_reload()
    test_source_guard_planner_coder_verify()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
