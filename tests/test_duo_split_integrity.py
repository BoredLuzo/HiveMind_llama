# -*- coding: utf-8 -*-
"""Split-integrity test for duo_runner (audit item 4, 2026-10-30).

`run_code_duo` was split into 11 module-level helpers (~1 400 lines). The
existing `test_duo_split` only checks structure (exists / async / fields).
The real risk of such a split is **silent state loss**: a helper writes into
its state object, nobody reads the field back, and the loop keeps its old
behaviour without an error.

This test enforces the contract the split actually uses:
  1. helpers do NOT use `nonlocal` (state travels through the `ps` object),
  2. every `ps.<field>` a helper assigns is a declared field of a state
     dataclass (no typos),
  3. every field a helper assigns is read again in `run_code_duo`
     (no dead writes).

Pure AST, no model, no imports of the module body.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SRC = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
# split 2.1-2.7: helpers live across the split modules now
_TREES = [TREE]
for _mod in ("duo_finalize", "duo_epilogue", "duo_finalgate", "duo_round_input",
             "duo_dtool_setup", "duo_inloop_planner", "duo_coder_load",
             "duo_compress_cycle", "duo_coder_preloop", "duo_task_complete_gate",
             "duo_loop_guards"):
    _p = ROOT / "core" / (_mod + ".py")
    if _p.exists():
        _TREES.append(ast.parse(_p.read_text(encoding="utf-8")))

HELPERS = [
    "_duo_planner_phase", "_duo_critic_review", "_duo_exec_round_task",
    "_duo_wedge_handoff", "_duo_ctx_pressure_check",
    "_duo_query_actual_slot_ctx", "_duo_resolve_coder_port_cache",
    "_duo_resolve_tool_budgets", "_duo_final_cleanup", "_duo_final_test",
    "_duo_disconnect_park",
    # 2026-11-08: the newer split seams must honor the same contract.
    "_duo_coder_round_input", "_duo_compress_cycle", "_duo_coder_preloop",
    "_duo_task_complete_gate", "_duo_loop_guards",
]

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="plain")
check = h.check

# ---- declared state fields --------------------------------------------
declared: dict[str, set[str]] = {}
for _tree in _TREES:
  for node in _tree.body:
    if isinstance(node, ast.ClassDef) and node.name.endswith("State"):
        fields = set()
        for sub in node.body:
            if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                fields.add(sub.target.id)
            elif isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if isinstance(t, ast.Name):
                        fields.add(t.id)
        declared[node.name] = fields
all_fields = set().union(*declared.values()) if declared else set()

# ---- helpers: nonlocal + ps writes ------------------------------------
helper_nodes = {n.name: n for _tree in _TREES
                for n in _tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

for name in HELPERS:
    node = helper_nodes.get(name)
    check(f"{name} exists as a module-level function", node is not None)
    if node is None:
        continue
    nonlocals = [x for n in ast.walk(node)
                 if isinstance(n, ast.Nonlocal) for x in n.names]
    check(f"{name}: no nonlocal (state travels via ps)", not nonlocals,
          f"({nonlocals})" if nonlocals else "")

writes: dict[str, set[str]] = {}
for name in HELPERS:
    node = helper_nodes.get(name)
    if node is None:
        continue
    w = set()
    for n in ast.walk(node):
        targets = []
        if isinstance(n, ast.Assign):
            targets = n.targets
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            targets = [n.target]
        for t in targets:
            if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                    and t.value.id == "ps"):
                w.add(t.attr)
    writes[name] = w

unknown = {f for fs in writes.values() for f in fs} - all_fields
check("every ps.<field> a helper writes is a declared state field",
      not unknown, f"(unbekannt: {sorted(unknown)})" if unknown else
      f"({len(set().union(*writes.values()) if writes else set())} Felder)")

# ---- every written field is read back in run_code_duo -----------------
run_node = helper_nodes.get("run_code_duo")
read_back = set()


def _is_state_name(expr) -> bool:
    """The state objects travel under per-helper names: ps, _ps, _budg_ps,
    _pl_ps, _cr_ps ... so any *ps/state holder counts."""
    if not isinstance(expr, ast.Name):
        return False
    return expr.id == "ps" or expr.id.endswith("ps") or expr.id == "state"


if run_node is not None:
    for n in ast.walk(run_node):
        if isinstance(n, ast.Attribute) and _is_state_name(n.value):
            read_back.add(n.attr)
dead = {f for fs in writes.values() for f in fs} - read_back
check("every field a helper writes is read back in run_code_duo",
      not dead, f"(tote Writes: {sorted(dead)})" if dead else
      f"({len(read_back)} gelesene Felder)")

# ---- the loop variables B023 flagged are not state --------------------
# sanity: the helpers do not reference the loop counter of run_code_duo
leaks = set()
for name in HELPERS:
    node = helper_nodes.get(name)
    if node is None:
        continue
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id in ("_dr", "_dr_loop"):
            leaks.add(name)
check("no helper reaches into run_code_duo's round counter", not leaks,
      f"({sorted(leaks)})" if leaks else "")

# ---- F1: compress-cycle exits sync through one helper ------------------
# Deep audit 2026-11-07: the cycle synced 18 fields at the natural end but
# only 5/11 at its two stop returns. Every exit must call _sync_state(), and
# no other direct ps write may bypass it (ps.stop is the signal, not state).
_cc_src = (ROOT / "core" / "duo_compress_cycle.py").read_text(
    encoding="utf-8", errors="replace")
_cc_tree = ast.parse(_cc_src)
_cc_fn = next((n for n in _cc_tree.body
               if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "_duo_compress_cycle"), None)
check("compress cycle exists", _cc_fn is not None)
if _cc_fn is not None:
    _sync_fn = next((n for n in ast.walk(_cc_fn)
                     if isinstance(n, ast.FunctionDef)
                     and n.name == "_sync_state"), None)
    check("compress cycle: _sync_state helper exists", _sync_fn is not None)
    _sync_lines = [n.lineno for n in ast.walk(_cc_fn)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_sync_state"]
    _returns = [n.lineno for n in ast.walk(_cc_fn)
                if isinstance(n, ast.Return) and n.value is None]
    check("compress cycle: every stop return syncs first",
          len(_sync_lines) >= 3
          and all(any(line < ret for line in _sync_lines) for ret in _returns),
          f"(sync={_sync_lines}, returns={_returns})")
    _sync_nodes = set(ast.walk(_sync_fn)) if _sync_fn is not None else set()
    _direct = []
    for _node in ast.walk(_cc_fn):
        if _node in _sync_nodes:
            continue
        if isinstance(_node, ast.Assign):
            for _tgt in _node.targets:
                if (isinstance(_tgt, ast.Attribute)
                        and isinstance(_tgt.value, ast.Name)
                        and _tgt.value.id == "ps" and _tgt.attr != "stop"):
                    _direct.append(_tgt.attr)
    check("compress cycle: no direct ps write bypasses the sync helper",
          not _direct, f"({sorted(set(_direct))})" if _direct else "")

sys.exit(h.finish("duo_split_integrity"))
