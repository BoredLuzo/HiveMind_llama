"""Planner=Coder fast-path port verification (2026-08-31).

Live finding: the fast path "Planner=Coder -> no reload" blindly trusted the
planner port. On a phantom slot (dead port) the coder inherited the
dead port and crashed with ConnectError. Fix: port liveness check before
cache adoption; dead port -> discard cache, normal coder load.

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


def _fast_path_decision(planner_is_coder, port_available, port_alive):
    """Faithful reproduction of the duo_runner Planner=Coder fast-path guard."""
    if planner_is_coder and port_available and port_alive:
        return "cache"      # adopt port, no reload
    return "load"           # normal coder load


# ── Entscheidungen ──────────────────────────────────────────────────────────
def test_dead_port_ignores_cache():
    if _fast_path_decision(True, True, False) == "load":
        ok("dead_port_ignores_cache (Planner=Coder aber Port tot -> normaler Load)")
    else:
        fail("dead_port_ignores_cache", "erwartet 'load'")


def test_alive_port_uses_cache():
    if _fast_path_decision(True, True, True) == "cache":
        ok("alive_port_uses_cache (Planner=Coder + Port lebt -> Cache)")
    else:
        fail("alive_port_uses_cache", "erwartet 'cache'")


def test_no_port_loads():
    if _fast_path_decision(True, False, False) == "load":
        ok("no_port_loads (kein Planner-Port -> normaler Load)")
    else:
        fail("no_port_loads", "erwartet 'load'")


def test_not_coder_loads():
    if _fast_path_decision(False, True, True) == "load":
        ok("not_coder_loads (Planner != Coder -> normaler Load)")
    else:
        fail("not_coder_loads", "erwartet 'load'")


# ── Source-Guard ────────────────────────────────────────────────────────────
def test_source_guard_planner_coder_verify():
    src = (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(encoding="utf-8")
    _has_liveness = "_plan_port_alive" in src
    _gate = "if _planner_is_coder and _plan_port_available and _plan_port_alive:" in src
    _warns = "not _plan_port_alive" in src
    if _has_liveness and _gate and _warns:
        ok("source_guard_planner_coder_verify (liveness gate before cache adoption)")
    else:
        fail("source_guard_planner_coder_verify",
             f"liveness={_has_liveness} gate={_gate} warns={_warns}")


if __name__ == "__main__":
    test_dead_port_ignores_cache()
    test_alive_port_uses_cache()
    test_no_port_loads()
    test_not_coder_loads()
    test_source_guard_planner_coder_verify()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
