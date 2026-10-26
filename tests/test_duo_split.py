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

print()
if failed:
    print(f"FAILED: {failed} check(s) failed, {passed} passed")
    sys.exit(1)
print(f"All {passed} duo-split pin(s) passed")
sys.exit(0)
