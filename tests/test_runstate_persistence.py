"""Run-persistent round-state refs (2026-09-11).

Live (00:04-00:10): the smoke-test nudge fired TEN times in one run
("[AUTO-TEST] No test suite — nudging ..." repeated every round) because
ToolRoundState is re-created EVERY tool round and `at_nosuite_nudged` was a
plain bool on it — "once per RUN" was in fact once per ROUND. The same trap
killed three more guards: TC-DE-NAG (tc_consecutive) and the read-ladder
trio (consecutive_reads / last_read_path / read_ladder_fired, whose
LADDER-PERSIST comment was already a lie for 1-tool-call-per-round runs).

Fix: these five fields are one-element list refs created once per run in
duo_runner and passed into each round's ToolRoundState — the same pattern
task_complete_blocked_count / cached_coder_port already used.

Run: python tests/test_runstate_persistence.py
Exit 0 = all pass, Exit 1 = failures.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.tool_exec_helpers import ToolRoundState, _update_read_ladder

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


# ── Faithful simulation: nudge once per RUN, not per round ──────────────────
def test_nudge_once_per_run():
    # Simulate 3 rounds, each with a FRESH ToolRoundState but SHARED refs
    # (exactly how duo_runner constructs rounds).
    nudged_ref = [False]
    fired_rounds = 0
    for _round in range(3):
        trs = ToolRoundState(at_nosuite_nudged=nudged_ref)
        if not trs.at_nosuite_nudged[0]:
            trs.at_nosuite_nudged[0] = True
            fired_rounds += 1
    if fired_rounds == 1:
        ok("SMOKE-NUDGE fires once per run across fresh round states (was: 3x)")
    else:
        fail("nudge_once", f"fired {fired_rounds}x, erwartet 1x")


def test_tc_denag_accumulates_across_rounds():
    tc_ref = [0]
    accepted_at = None
    for round_no in range(1, 4):
        trs = ToolRoundState(tc_consecutive=tc_ref)
        trs.tc_consecutive[0] += 1          # task_complete call
        if trs.tc_consecutive[0] >= 2:
            accepted_at = round_no
            break
        trs.tc_consecutive[0] = 0 if round_no == 99 else trs.tc_consecutive[0]
    if accepted_at == 2:
        ok("TC-DE-NAG akkumuliert über Runden (2. aufeinanderfolgende task_complete wird akzeptiert)")
    else:
        fail("tc_denag", f"accepted_at={accepted_at}, erwartet 2")


def test_read_ladder_persists_across_rounds():
    reads_ref, path_ref, fired_ref = [0], [""], [False]
    fired = False
    for path in ("a.py", "a.py", "a.py"):   # same path 3 rounds in a row
        trs = ToolRoundState(consecutive_reads=reads_ref, last_read_path=path_ref,
                             read_ladder_fired=fired_ref)
        _update_read_ladder(trs, "read_file", False, path)
        if trs.consecutive_reads[0] >= 3 and not trs.read_ladder_fired[0]:
            fired = True
            trs.read_ladder_fired[0] = True
            trs.consecutive_reads[0] = 0
    if fired:
        ok("READ-LADDER eskaliert über 3 Ein-Runde-Reads desselben Pfads")
    else:
        fail("ladder_persist", "Ladder ist nie gefeuert (Reset-_bug wiederaufgetreten)")


def test_read_ladder_path_reset_across_rounds():
    reads_ref, path_ref, fired_ref = [0], [""], [False]
    for path in ("a.py", "b.py", "c.py"):   # different paths = legit exploration
        trs = ToolRoundState(consecutive_reads=reads_ref, last_read_path=path_ref,
                             read_ladder_fired=fired_ref)
        _update_read_ladder(trs, "read_file", False, path)
    if reads_ref[0] == 1:
        ok("PATH-RESET über Runden: anderer Pfad setzt den Zähler auf 1 zurück")
    else:
        fail("path_reset", f"consecutive_reads={reads_ref[0]}, erwartet 1")


# ── AST guard: fields must be list refs, duo_runner must share them ─────────
def test_ast_fields_are_list_refs():
    src = (Path(__file__).parent.parent / "core" / "tool_exec_helpers.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    defaults = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ToolRoundState":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    defaults[stmt.target.id] = ast.unparse(stmt.value)
    bad = []
    for f in ("tc_consecutive", "at_nosuite_nudged", "consecutive_reads",
              "last_read_path", "read_ladder_fired"):
        d = defaults.get(f, "")
        is_ref = ("default_factory" in d
                  and any(pat in d.replace('"', "'") for pat in
                          ("lambda: [0]", "lambda: [False]", "lambda: ['']")))
        if not is_ref:
            bad.append(f"{f}={d!r}")
    if not bad:
        ok("AST: alle 5 Felder sind List-ref defaults ([0]/[False]/[\"\"])")
    else:
        fail("ast_refs", f"{bad}")


def test_duo_runner_shares_refs():
    src = (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(encoding="utf-8")
    ok_needles = [
        ("_tc_consecutive = [0]", "TC-DE-NAG ref"),
        ("_at_nosuite_nudged = [False]", "SMOKE-NUDGE ref"),
        ("_consecutive_reads = [0]", "LADDER ref"),
        ("at_nosuite_nudged=_at_nosuite_nudged", "ref an ToolRoundState übergeben"),
        ("read_ladder_fired=_read_ladder_fired", "ref an ToolRoundState übergeben"),
    ]
    missing = [label for needle, label in ok_needles if needle not in src]
    if not missing:
        ok("duo_runner erzeugt die Refs einmal pro Run und übergibt sie")
    else:
        fail("duo_refs", f"fehlt: {missing}")


if __name__ == "__main__":
    test_nudge_once_per_run()
    test_tc_denag_accumulates_across_rounds()
    test_read_ladder_persists_across_rounds()
    test_read_ladder_path_reset_across_rounds()
    test_ast_fields_are_list_refs()
    test_duo_runner_shares_refs()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
