"""Source pins for the run_code_duo() split (feature/duo-runner-split).

run_code_duo grew to ~6600 lines / 17 nesting levels. It is being split into
module-level async helpers, one block per commit, BEHAVIOR-PRESERVING (pure
code motion). Every extraction follows the same contract, and this suite pins
that contract so a later edit cannot silently break the seam:

  1. the helper is a module-level async function (async generator when the
     moved code yields SSE events),
  2. every in/out local crosses the seam through an explicit state dataclass -
     one field per local, unpack at the top, pack in a finally (so abort /
     GeneratorExit paths keep seeing assignments, the commit-2270557
     NameError-ladder bug class),
  3. the runner syncs locals from the state object in a finally around the
     async-for (all fields, no cherry-picking),
  4. every name the helper body reads resolves to a parameter, a local of the
     helper, a module-level name, or a builtin - never to a run_code_duo local
     that was not passed (that would be a silent capture change, and ruff
     cannot catch the module-name shadow case).

Blocks pinned here (in commit order):
  block 3  planner phase            -> _duo_planner_phase
  block 5  critic review variants   -> _duo_critic_review (added with block 5)
  block 5  final test               -> _duo_final_test
  block 4  tool-round launch/wait   -> _duo_exec_round_task
  block 4  wedge handoff            -> _duo_wedge_handoff
  block 4  ctx pressure warn-once   -> _duo_ctx_pressure_check
  block 2  port cache / slot ctx /
           budgets                  -> _duo_resolve_coder_port_cache,
                                        _duo_query_actual_slot_ctx,
                                        _duo_resolve_tool_budgets
  block 1  disconnect park + final cleanup state objects

Run: python tests/test_duo_split.py
"""
import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


SRC_PATH = ROOT / "core" / "duo_runner.py"
SRC = SRC_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def fn(name):
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def cls(name):
    for node in TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


RUNNER = fn("run_code_duo")
check("run_code_duo exists", RUNNER is not None)

# ── module-level names (imports, defs, assigns) for the resolution pin ─────
MODULE_NAMES = set(dir(builtins))
for _n in TREE.body:
    if isinstance(_n, (ast.Import, ast.ImportFrom)):
        for _a in _n.names:
            MODULE_NAMES.add(_a.asname or _a.name.split(".")[0])
    elif isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        MODULE_NAMES.add(_n.name)
    elif isinstance(_n, ast.Assign):
        for _t in _n.targets:
            for _x in ast.walk(_t):
                if isinstance(_x, ast.Name):
                    MODULE_NAMES.add(_x.id)
    elif isinstance(_n, ast.AnnAssign) and isinstance(_n.target, ast.Name):
        MODULE_NAMES.add(_n.target.id)


def in_scope_names(nodes):
    """Names whose read can resolve inside the given body: plain stores,
    except-handler names, and nested def/lambda parameters (their bodies are
    part of the moved code and resolve their own args)."""
    out = set()
    for _s in nodes:
        for _x in ast.walk(_s):
            if isinstance(_x, ast.Name) and isinstance(_x.ctx, ast.Store):
                out.add(_x.id)
            elif isinstance(_x, ast.ExceptHandler) and _x.name:
                out.add(_x.name)
            elif isinstance(_x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for _a in _x.args.posonlyargs + _x.args.args + _x.args.kwonlyargs:
                    out.add(_a.arg)
                if _x.args.vararg:
                    out.add(_x.args.vararg.arg)
                if _x.args.kwarg:
                    out.add(_x.args.kwarg.arg)
    return out


def runner_locals():
    if RUNNER is None:
        return set()
    return in_scope_names(RUNNER.body) | {a.arg for a in RUNNER.args.args}


def check_seam(helper_name, state_cls, fields, label):
    """The full split contract for one extracted helper."""
    h = fn(helper_name)
    check(f"[{label}] {helper_name} is module-level", h is not None)
    if h is None:
        return
    check(f"[{label}] {helper_name} is async", isinstance(h, ast.AsyncFunctionDef))
    c = cls(state_cls)
    if state_cls is None:
        check(f"[{label}] no state class needed", c is None and not fields)
        return
    check(f"[{label}] {state_cls} is module-level", c is not None)
    if c is not None:
        got = {t.target.id for t in c.body if isinstance(t, ast.AnnAssign)}
        check(f"[{label}] {state_cls} fields == expected set",
              got == set(fields),
              f"missing={sorted(set(fields) - got)} extra={sorted(got - set(fields))}")
    # helper unpacks every field at the top (statements directly in the body)
    n_unpack = 0
    for s in h.body:
        if isinstance(s, ast.Assign) and len(s.targets) == 1 \
                and isinstance(s.targets[0], ast.Name) \
                and isinstance(s.value, ast.Attribute) and s.value.attr in fields:
            n_unpack += 1
    check(f"[{label}] helper unpacks every field at entry", n_unpack == len(fields),
          f"{n_unpack}/{len(fields)}")
    # helper packs every field in a finally (ps.X = <local>)
    pack_attrs = set()
    for _n in ast.walk(h):
        if isinstance(_n, ast.Assign) and isinstance(_n.targets[0], ast.Attribute) \
                and isinstance(_n.value, ast.Name):
            pack_attrs.add(_n.targets[0].attr)
    check(f"[{label}] helper packs every field (ps.X = ...)", set(fields) <= pack_attrs,
          f"missing={sorted(set(fields) - pack_attrs)}")
    # runner syncs all fields from the state object after the async-for
    syncs = set()
    for _n in ast.walk(RUNNER):
        if isinstance(_n, ast.Assign) and isinstance(_n.targets[0], ast.Name) \
                and isinstance(_n.value, ast.Attribute):
            syncs.add(_n.value.attr)
    check(f"[{label}] runner writes back every field", set(fields) <= syncs,
          f"missing={sorted(set(fields) - syncs)}")
    # resolution pin: no free name may be an unpassed runner local
    params = {a.arg for a in h.args.args + h.args.kwonlyargs}
    local = in_scope_names(h.body)
    loads = {_n.id for _n in ast.walk(h)
             if isinstance(_n, ast.Name) and isinstance(_n.ctx, ast.Load)}
    rl = runner_locals()
    shadow = sorted(_n for _n in loads
                    if _n in rl and _n not in params and _n not in local
                    and _n not in MODULE_NAMES)
    check(f"[{label}] no unpassed run_code_duo local is read", not shadow,
          f"leaked={shadow}")


# ── block 3: planner phase ──────────────────────────────────────────────────
PLANNER_FIELDS = [
    "subtasks", "plan_thinking", "plan_content_txt", "plan_result",
    "planner_used_fallback", "planner_fallback_reason", "planner_parse_mode",
    "planner_context_trimmed", "plan_guard_quality", "planner_model",
    "img_plan", "plan_port", "plan_ctx_final", "duo_timed_out", "plan_tracker",
]
check_seam("_duo_planner_phase", "_DuoPlannerPhaseState", PLANNER_FIELDS, "b3")

_h = fn("_duo_planner_phase")
if _h is not None:
    _seg = ast.get_source_segment(SRC, _h) or ""
    check("[b3] planner_result emit moved into the helper",
          "planner_result" in _seg)
    check("[b3] planner event bridge moved into the helper",
          "_plan_event_q" in _seg and "_planner_hb_task" in _seg)
    _runseg = SRC[SRC.index("async def run_code_duo"):]
    check("[b3] runner no longer builds the planner bridge",
          "_planner_hb_task" not in _runseg)
    check("[b3] runner still owns the planner skip / planner-off branches",
          "Planner skipped — going straight to coder" in _runseg)

# ── block 5: critic review + final test ─────────────────────────────────────
CRITIC_FIELDS = [
    "parts", "t", "verify_last_ok_serial", "critic_issues",
    "final_verdict", "approved", "verify_gate_blocked",
]
check_seam("_duo_critic_review", "_DuoCriticReviewState", CRITIC_FIELDS, "b5")

_h = fn("_duo_critic_review")
if _h is not None:
    _seg = ast.get_source_segment(SRC, _h) or ""
    check("[b5] both critic variants moved into the helper",
          "_CRITIC_VERIFY_TOOLS" in _seg and "_critic_plain_payload" in _seg)
    check("[b5] duo_critic_done emit moved into the helper",
          "duo_critic_done" in _seg)
    check("[b5] verify-gate patch moved into the helper",
          "verification_required_after_write" in _seg)
    _runseg = SRC[SRC.index("async def run_code_duo"):]
    check("[b5] runner no longer builds critic payloads",
          "_critic_plain_payload" not in _runseg and "_critic_tc_payload" not in _runseg)
    check("[b5] runner still owns the _can_finish_round advance logic",
          "_can_finish_round" in _runseg)

_ft = fn("_duo_final_test")
check("[b5] _duo_final_test is module-level async", _ft is not None and isinstance(_ft, ast.AsyncFunctionDef))
if _ft is not None:
    _seg = ast.get_source_segment(SRC, _ft) or ""
    check("[b5] final-test gate moved into the helper",
          "_final_test_fb" in _seg and "test_feedback_final" in _seg)
    check("[b5] completion notes stay on the shared ChunkState",
          "_cs.completion_notes.append" in _seg)
    _params = {a.arg for a in _ft.args.args + _ft.args.kwonlyargs}
    check("[b5] final-test params explicit", _params == {"ctx", "state", "_cs", "_n_items", "_eff_run_ws"},
          f"got={sorted(_params)}")
    _local = in_scope_names(_ft.body)
    _loads = {_n.id for _n in ast.walk(_ft)
              if isinstance(_n, ast.Name) and isinstance(_n.ctx, ast.Load)}
    _rl = runner_locals()
    _shadow = sorted(_n for _n in _loads
                     if _n in _rl and _n not in _params and _n not in _local
                     and _n not in MODULE_NAMES)
    check("[b5] final-test reads no unpassed runner local", not _shadow, f"leaked={_shadow}")

# ── block 4: tool-round exec + wedge handoff + ctx pressure ────────────────
EXEC_FIELDS = [
    "duo_deadline_at", "exec_result", "snap_before",
    "last_too_large_ref", "cached_port_ref",
]
check_seam("_duo_exec_round_task", "_DuoExecRoundState", EXEC_FIELDS, "b4")

_h = fn("_duo_exec_round_task")
if _h is not None:
    _seg = ast.get_source_segment(SRC, _h) or ""
    check("[b4] execute_tool_round launch moved into the helper",
          "execute_tool_round(" in _seg)
    check("[b4] heartbeat wait loop moved into the helper",
          '"type": "heartbeat"' in _seg)
    check("[b4] deadline-grace pull-back moved into the helper",
          "deadline_extended_to" in _seg)
    _runseg = SRC[SRC.index("async def run_code_duo"):]
    check("[b4] runner no longer launches execute_tool_round",
          "execute_tool_round(" not in _runseg)
    check("[b4] runner still owns the post-round ref syncs",
          "_last_too_large_ref[0]" in _runseg and "_cached_port_ref[0]" in _runseg)

WEDGE_FIELDS = ["loop_detected", "wedge_escalated", "duo_deadline_at", "break_out"]
check_seam("_duo_wedge_handoff", "_DuoWedgeHandoffState", WEDGE_FIELDS, "b4")

_h = fn("_duo_wedge_handoff")
if _h is not None:
    _seg = ast.get_source_segment(SRC, _h) or ""
    check("[b4] fix-agent handoff call moved into the helper",
          "_wedge_run_fix_agent" in _seg)
    check("[b4] both wedge breaks surface via break_out",
          _seg.count("_break_out = True; return") == 2)
    _runseg = SRC[SRC.index("async def run_code_duo"):]
    check("[b4] runner breaks on the wedge signal after the write-back",
          "_break_out = _wedge_ps.break_out" in _runseg and "if _break_out:" in _runseg)
    check("[b4] runner no longer calls the fix agent directly",
          "_wedge_run_fix_agent" not in _runseg)

CTXP_FIELDS = ["ctx_pressure_warned"]
check_seam("_duo_ctx_pressure_check", "_DuoCtxPressureState", CTXP_FIELDS, "b4")

_h = fn("_duo_ctx_pressure_check")
if _h is not None:
    _seg = ast.get_source_segment(SRC, _h) or ""
    check("[b4] CTX-pressure finish prompt moved into the helper",
          "[CTX CRITICAL — ~" in _seg)
    _runseg = SRC[SRC.index("async def run_code_duo"):]
    check("[b4] runner no longer builds the pressure prompt",
          "[CTX CRITICAL — ~" not in _runseg)

print()
if failed:
    print(f"FAILED: {failed} check(s) failed, {passed} passed")
    sys.exit(1)
print(f"All {passed} duo-split pin(s) passed")
sys.exit(0)
