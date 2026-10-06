"""Planner-slot reuse must satisfy the CODER ctx (2026-10-06).

Owner decision: in a normal agentic run the planner is evicted before the
coder loads — ALWAYS. The old reuse check compared the planner slot's ctx
against the PLANNER's ctx target only, so a planner at ctx=8192 handed the
coder an 8192 slot while the coder budget said 80896 (live 13:03:
"[Planner=Coder] Model stays in VRAM - no reload needed" with exactly that
gap) — the same cached-port ctx mismatch class that produced the historic
LD-SET-2497 run deaths. With the honest check, a normal agentic run always
frees the planner slot before the coder loads: same model → the manager's
ctx-mismatch kill+reload, different model → SERIAL-SLOTS evict.

Run: python tests/test_planner_reuse_ctx.py
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


def _reuse_decision(slot_ctx, plan_ctx_req, coder_ctx_need):
    """Faithful reproduction of the fixed _planner_ctx_ok check."""
    if not slot_ctx:
        return False
    if slot_ctx < plan_ctx_req:
        return False
    if coder_ctx_need > 0 and slot_ctx < coder_ctx_need:
        return False
    return True


def test_small_planner_slot_is_not_reused():
    # THE FIX: live 13:03 gap — planner ctx 8192, coder needs 80896
    if _reuse_decision(8192, 8192, 80896) is False:
        ok("planner ctx 8192 < coder need 80896 -> NO reuse (planner evicted)")
    else:
        fail("small_slot", "reuse despite coder ctx gap (the old bug)")


def test_big_planner_slot_is_reused():
    if _reuse_decision(81728, 8192, 80896) is True:
        ok("slot ctx 81728 >= coder need 80896 -> reuse stays possible")
    else:
        fail("big_slot", "legitimate reuse broken")


def test_unknown_coder_need_falls_back():
    if _reuse_decision(8192, 8192, 0) is True:
        ok("coder need unknown (0) -> planner-only check (legacy behavior)")
    else:
        fail("fallback", "fallback path broken")


def test_query_error_never_reuses():
    if _reuse_decision(0, 8192, 80896) is False:
        ok("slot ctx query failed (0) -> conservatively no reuse")
    else:
        fail("query_error", "0-ctx slot reused")


def test_ast_honest_cached_ctx():
    src = (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(encoding="utf-8")
    if "_cached_coder_port_ctx = _plan_port_actual_ctx" in src \
            and "_plan_port_actual_ctx >= _coder_ctx_need" in src:
        ok("duo_runner: cached coder ctx is the REAL slot ctx; reuse checks coder need")
    else:
        fail("ast", "honest cached ctx or coder-need check missing")




def test_always_vision_toggle_pins():
    src = _src_duo()
    if 'duo_coder_always_vision' in src             and 'not _always_vision or _plan_port_vision' in src             and src.count('_always_vision))') >= 1:
        ok("always-vision toggle: gates reuse, ORs into both coder loads")
    else:
        fail("toggle", "toggle wiring incomplete")


def _src_duo():
    return (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(encoding="utf-8")

if __name__ == "__main__":
    test_small_planner_slot_is_not_reused()
    test_big_planner_slot_is_reused()
    test_unknown_coder_need_falls_back()
    test_query_error_never_reuses()
    test_ast_honest_cached_ctx()
    test_always_vision_toggle_pins()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
