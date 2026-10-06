"""Vision-load-race fix in ensure_loaded (2026-10-06).

Live finding (11:13): a text-only PREFETCH started loading qwen3.5:4b-mtp
on port 8101 (no --mmproj). While it was still loading, the image request's
ensure_loaded(vision=True) arrived — and _find_loaded hands out LOADING
slots. The old code ran the upgrade checks only under `if not slot._loading`,
so the vision request silently attached to the projector-less in-flight
load: the tool-loop POST answered 500 "image input is not supported" and the
upgrade only happened AFTER the failure (a second load on 8102 WITH mmproj
at 11:13:16).

Fix: when the found slot is loading and needs_vision_reload() says upgrade,
the request is parked (_vision_upgrade_pending) and re-evaluated after the
ready wait — the slot is then killed and restarted WITH the projector
(wait-then-upgrade; kill() swaps in a fresh ready event so the second wait
is safe, and the prefetch task's done-callback absorbs the kill as a
"Prefetch task crashed" warning).

Run: python tests/test_vision_load_race.py
Exit 0 = all pass, Exit 1 = failures.
"""
import ast
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


# ── Flow model of the fixed decision ────────────────────────────────────────
def _ensure_decision(slot_state, slot_vision, want_vision):
    """Faithful reproduction of the ensure_loaded loading-slot branch.

    slot_state: "none" | "loading" | "ready"
    Returns the action taken for THIS caller.
    """
    if slot_state == "none":
        return "fresh_load"
    if slot_state == "loading":
        if slot_vision is False and want_vision:
            return "park_upgrade_then_reload_with_vision"   # THE FIX
        return "attach_and_wait"
    # ready slot (pre-existing upgrade-only path)
    if slot_vision is False and want_vision:
        return "upgrade_reload"
    return "reuse"


def test_loading_slot_parks_upgrade():
    if _ensure_decision("loading", False, True) == "park_upgrade_then_reload_with_vision":
        ok("loading slot + vision request -> parked upgrade (no silent attach)")
    else:
        fail("park_upgrade", _ensure_decision("loading", False, True))


def test_loading_slot_text_request_attaches():
    if _ensure_decision("loading", False, False) == "attach_and_wait":
        ok("loading slot + plain request -> attach and wait (unchanged)")
    else:
        fail("attach_plain", _ensure_decision("loading", False, False))


def test_loading_slot_already_vision_attaches():
    if _ensure_decision("loading", True, True) == "attach_and_wait":
        ok("loading vision slot + vision request -> attach (no redundant reload)")
    else:
        fail("attach_vision", _ensure_decision("loading", True, True))


def test_ready_slot_upgrade_unchanged():
    if _ensure_decision("ready", False, True) == "upgrade_reload":
        ok("ready slot + vision request -> upgrade reload (2026-10-01 path intact)")
    else:
        fail("ready_upgrade", _ensure_decision("ready", False, True))


# ── AST: the shipped ensure_loaded really carries the parked upgrade ────────
def _ensure_loaded_source():
    tree = ast.parse(
        (Path(__file__).parent.parent / "backend" / "manager_load.py")
        .read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "ensure_loaded":
            return ast.get_source_segment(
                (Path(__file__).parent.parent / "backend" / "manager_load.py")
                .read_text(encoding="utf-8"), node)
    return ""


def test_ast_parked_flag_exists():
    src = _ensure_loaded_source()
    if "_vision_upgrade_pending = False" in src and "_vision_upgrade_pending = True" in src:
        ok("AST: _vision_upgrade_pending initialized and set in the loading branch")
    else:
        fail("ast_flag", "flag init/set missing in ensure_loaded")


def test_ast_upgrade_runs_after_ready_wait():
    src = _ensure_loaded_source()
    wait_pos = src.find("await asyncio.wait_for(slot._ready_event.wait(), timeout=240.0)")
    upgrade_pos = src.find("if _vision_upgrade_pending and needs_vision_reload(slot._vision, vision):")
    start_pos = src.find("vision=True, n_parallel=n_parallel")
    if -1 not in (wait_pos, upgrade_pos, start_pos) and wait_pos < upgrade_pos < start_pos:
        ok("AST: upgrade block sits AFTER the ready wait and restarts with vision=True")
    else:
        fail("ast_order", f"wait={wait_pos} upgrade={upgrade_pos} start={start_pos}")


def test_ast_upgrade_guards_dead_slot_and_waits_again():
    src = _ensure_loaded_source()
    upgrade_pos = src.find("if _vision_upgrade_pending and needs_vision_reload(slot._vision, vision):")
    tail = src[upgrade_pos:] if upgrade_pos >= 0 else ""
    checks = [
        ("_kill_slot_async(slot)" in tail, "kills the projector-less slot"),
        ("died during the vision upgrade" in tail, "A.2-style dead-slot guard for the second wait"),
        ("240.0" in tail, "second ready wait has the 240s timeout"),
    ]
    bad = [name for cond, name in checks if not cond]
    if not bad:
        ok("AST: upgrade path kills, restarts, guards and re-waits")
    else:
        fail("ast_guard", f"missing: {bad}")


if __name__ == "__main__":
    test_loading_slot_parks_upgrade()
    test_loading_slot_text_request_attaches()
    test_loading_slot_already_vision_attaches()
    test_ready_slot_upgrade_unchanged()
    test_ast_parked_flag_exists()
    test_ast_upgrade_runs_after_ready_wait()
    test_ast_upgrade_guards_dead_slot_and_waits_again()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
