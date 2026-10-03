# -*- coding: utf-8 -*-
"""Wedge-handoff tests (2026-09-17): trigger, handover format, return path.

Trigger semantics under test:
  - streak: N consecutive edit-family failures on ONE file, reset ONLY by a
    successful write (a read alone never resets).
  - repeat: the same call (tool + args-hash) failing twice within a streak
    fires immediately — even non-consecutively.
  - the executor integrates both BEFORE the global >=6 error cap, and skips
    everything when wedge_state is None (feature off).

Run: python tests/test_wedge_handoff.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import wedge_handoff as wh
from core.fix_agent import parse_verdict, result_from_verdict

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


def check(name, cond, msg=""):
    if cond:
        ok(name)
    else:
        fail(name, msg)


# ── 1. Streak trigger (pure) ────────────────────────────────────────────────
def test_streak_threshold():
    ws = wh.new_wedge_state(threshold=4)
    trigs = []
    for i in range(4):
        rep = wh.note_edit_failure(ws, "edit_file", "a.js", f'{{"old_text":"x{i}"}}', "[TOOL_ERROR:E] fail")
        trigs.append(wh.detect_wedge(ws, "a.js", rep))
    check("streak: keine Trigger bei 1-3 Fails", not any(trigs[:3]), str(trigs))
    check("streak: Trigger beim 4. Fail", trigs[3], str(trigs))


def test_reset_on_success():
    ws = wh.new_wedge_state(threshold=4)
    for i in range(3):
        wh.note_edit_failure(ws, "edit_file", "a.js", f'{{"old_text":"x{i}"}}', "[TOOL_ERROR:E] fail")
    wh.note_edit_success(ws, "a.js")
    check("reset: History beim Erfolg geleert", wh.attempts_of(ws, "a.js") == [])
    rep = wh.note_edit_failure(ws, "edit_file", "a.js", '{"old_text":"y"}', "[TOOL_ERROR:E] fail")
    check("reset: Erfolg setzt Streak zurueck (1 Fail danach -> kein Trigger)",
          not wh.detect_wedge(ws, "a.js", rep) and wh.streak_of(ws, "a.js") == 1,
          f"streak={wh.streak_of(ws, 'a.js')}")
    check("reset: History nur noch den neuen Eintrag", len(wh.attempts_of(ws, "a.js")) == 1)


def test_identical_repeat_fires_immediately():
    ws = wh.new_wedge_state(threshold=4)
    same_args = '{"old_text": "same broken text", "new_text": "x"}'
    r1 = wh.note_edit_failure(ws, "edit_file", "a.js", same_args, "[TOOL_ERROR:E] not found")
    r2 = wh.note_edit_failure(ws, "edit_file", "a.js", same_args, "[TOOL_ERROR:E] not found")
    check("repeat: 1. identischer Fail triggert nicht", not wh.detect_wedge(ws, "a.js", r1))
    check("repeat: 2. identischer Fail triggert sofort", wh.detect_wedge(ws, "a.js", r2))


def test_repeat_within_streak_not_consecutive():
    ws = wh.new_wedge_state(threshold=4)
    a = '{"old_text": "AAA"}'
    b = '{"old_text": "BBB"}'
    r1 = wh.note_edit_failure(ws, "edit_file", "a.js", a, "[TOOL_ERROR:E]")
    r2 = wh.note_edit_failure(ws, "edit_file", "a.js", b, "[TOOL_ERROR:E]")
    r3 = wh.note_edit_failure(ws, "edit_file", "a.js", a, "[TOOL_ERROR:E]")
    check("repeat: nicht-konsekutiv innerhalb des Streaks triggert (3. Call)",
          (not wh.detect_wedge(ws, "a.js", r1)) and (not wh.detect_wedge(ws, "a.js", r2))
          and wh.detect_wedge(ws, "a.js", r3))


def test_different_files_do_not_cross_trigger():
    ws = wh.new_wedge_state(threshold=4)
    for i in range(3):
        wh.note_edit_failure(ws, "edit_file", "a.js", f'{{"o":"a{i}"}}', "[TOOL_ERROR:E]")
    for i in range(3):
        rep = wh.note_edit_failure(ws, "edit_file", "b.js", f'{{"o":"b{i}"}}', "[TOOL_ERROR:E]")
    check("isolation: 3 Fails auf a.js + 3 auf b.js -> kein Trigger auf b.js",
          not wh.detect_wedge(ws, "b.js", rep) and not wh.detect_wedge(ws, "a.js", False))


# ── 2. Handover-Format ──────────────────────────────────────────────────────
def test_handover_format():
    ws = wh.new_wedge_state(threshold=4)
    long_result = "[TOOL_ERROR:EDIT_FILE_OLD_TEXT_NOT_FOUND] edit_file (duo_full): " + ("x" * 500)
    for i in range(5):
        wh.note_edit_failure(ws, "edit_file", "src/app.js", f'{{"old_text":"broken {i}"}}', long_result)
    ho = wh.build_handover(
        path="src/app.js",
        intent="Plan chunk 2/5: add input validation to the handler",
        attempts=wh.attempts_of(ws, "src/app.js"),
        streak=wh.streak_of(ws, "src/app.js"),
        threshold=4)
    checks = [
        ("File-Zeile", "File: src/app.js" in ho),
        ("Intent-Zeile", "Intent: Plan chunk 2/5" in ho),
        ("Streak-Kontext", "5x in a row" in ho),
        ("Attempts-Liste", "Failed attempts (last 3):" in ho),
        ("nur letzte 3", ho.count("edit_file =>") == 3),
        ("Results gekappt", "x" * 300 not in ho),
        ("Frisch-Read-Regel", "read_file the target FRESH" in ho),
        ("CRLF-Regel", "CRLF" in ho),
        ("Verdict-Vertrag", "VERDICT: FIXED" in ho and "VERDICT: NO_FIX_NEEDED" in ho
         and "VERDICT: FAILED" in ho),
        ("kein Dateiinhalt", "SENTINEL_FULL_CONTENT_9137" not in ho),
    ]
    bad = [n for n, c in checks if not c]
    if not bad:
        ok("handover: Format-Check (Felder, Caps, Regeln, Verdict-Vertrag)")
    else:
        fail("handover", f"fehlt: {bad}")
    empty = wh.build_handover(path="p.js", intent="", attempts=[], streak=0, threshold=4)
    check("handover: leerer Intent/Fallback", "(unspecified" in empty and "(none recorded)" in empty)


def test_notices():
    res = wh.build_resolved_notice("src/app.js", "old_text mismatch: header had moved", no_fix=False)
    esc = wh.build_escalated_notice("src/app.js", "context window exceeded")
    nf = wh.build_resolved_notice("src/app.js", "file already validates input", no_fix=True)
    checks = [
        ("resolved: Pfad + Summary + re-read", "src/app.js" in res and "header had moved" in res
         and "read_file it again" in res),
        ("no_fix: als Erfolg formuliert", "already satisfies the intent" in nf),
        ("escalated: Grund + Stopp", "context window exceeded" in esc and "stopping" in esc),
        ("Summary gekappt", "y" * 500 not in wh.build_resolved_notice("p", "y" * 500)),
    ]
    bad = [n for n, c in checks if not c]
    if not bad:
        ok("notices: resolved / no_fix_needed / escalated")
    else:
        fail("notices", f"fehlt: {bad}")


# ── 3. Rückgabepfad: Verdict-Mapping ───────────────────────────────────────
def test_verdict_parsing():
    checks = [
        (("VERDICT: FIXED — added the missing guard", True), ("resolved", True)),
        (("VERDICT: NO_FIX_NEEDED — file already correct", False), ("no_fix_needed", False)),
        (("VERDICT: FAILED — old_text never matched", False), ("failed", False)),
        (("no verdict here", True), ("resolved", True)),
        (("no verdict here", False), ("failed", False)),
        (("bla\nVERDICT: FIXED — x", False), ("failed", False)),  # FIXED ohne Aenderung = Fail
        (("bla\nVERDICT: no-fix-needed  - reason here", True), ("no_fix_needed", True)),
    ]
    bad = []
    for (text, changed), (want_status, want_changed) in checks:
        got = result_from_verdict(text, changed)
        if got["status"] != want_status or got["changed"] != want_changed:
            bad.append(f"{text[:30]!r} -> {got}")
    if not bad:
        ok("verdict: Parser + Integritaets-Mapping (FIXED ohne Diff = Fail)")
    else:
        fail("verdict", "; ".join(bad))
    s, r = parse_verdict("multi\nline\nVERDICT: FAILED — last one wins")
    check("verdict: letzter Match gewinnt", s == "failed" and r == "last one wins", f"{s}/{r}")


def test_invalidate_read_signature():
    from tools import runner as _tr
    from utils.file import normalize_tool_path as _ntp
    ws = Path(tempfile.mkdtemp(prefix="hvm_wedge_sig_"))
    fp = (ws / "sig.js")
    fp.write_text("a\n", encoding="utf-8")
    key = _ntp(str(fp.resolve()), ws)
    _tr._read_signatures[key] = (111, 222)
    check("sig: Treffer invalidiert", wh.invalidate_read_signature("sig.js", str(ws)) is True)
    check("sig: Key weg", key not in _tr._read_signatures)
    check("sig: Fehlender Key -> False", wh.invalidate_read_signature("sig.js", str(ws)) is False)


# ── 4. Executor-Integration (echte execute_tool_round) ─────────────────────
def test_executor_wedge_trigger():
    try:
        from core.tool_executor import execute_tool_round, ToolExecHooks
        from core.tool_exec_helpers import ToolRoundState
        from core.agentic_duo_state import DuoRoundState
        from hive_functions.memory import ToolContextLRU
    except Exception as e:
        print(f"  SKIP  executor-integration ({type(e).__name__}: {e})")
        return

    ws = Path(tempfile.mkdtemp(prefix="hvm_wedge_exec_"))
    body = "".join(f"line {i}\n" for i in range(1, 51))
    (ws / "wedged.js").write_text(body, encoding="utf-8")

    async def _noop_emit(ev):
        return ""

    hooks = ToolExecHooks(emit=_noop_emit, is_aborted=lambda cid: False, on_tool_result=None)
    wstate = wh.new_wedge_state(threshold=4)
    total_errors = [0]

    async def one_round(args_str):
        trs = ToolRoundState(
            tool_ctx_lru=ToolContextLRU(default_ttl=3),
            duo_deadline_at=time.time() + 600,
            wedge_state=wstate,
            total_tool_errors=total_errors,
        )
        return await execute_tool_round(
            tool_calls=[{"id": "c1", "function": {"name": "edit_file", "arguments": args_str}}],
            dtool_msgs=[],
            round_state=DuoRoundState(),
            hooks=hooks,
            trs=trs,
            tool_mode="duo_full",
            duo_ws=False,
            workspace_lock=str(ws),
            exec_model="test-model",
            exec_has_thinking=False,
            tool_think_auto_mode="",
            run_id_global="r1",
            chat_id="chat1",
            subtask_index=0,
        )

    def fail_args(i):
        import json as _j
        return _j.dumps({"path": "wedged.js", "old_text": f"NO SUCH TEXT {i}", "new_text": "x"})

    results = [asyncio.run(one_round(fail_args(i))) for i in range(4)]
    check("exec: 1-3 Fails -> kein Wedge", all(r.wedge_file == "" for r in results[:3]))
    check("exec: 4. Fail -> Wedge-Signal", results[3].wedge_file != "",
          f"wf={results[3].wedge_file!r}")
    check("exec: Wedge-Pfad ist die Zieldatei", "wedged.js" in results[3].wedge_file.replace("\\", "/"))
    check("exec: loop_detected NICHT gesetzt (Handoff statt Abbruch)",
          not results[3].loop_detected)
    check("exec: History fuer Handover gefuellt", len(wh.attempts_of(wstate, results[3].wedge_file)) == 3)

    # Reset: erfolgreicher Edit zwischen den Fehlschlaegen
    wstate2 = wh.new_wedge_state(threshold=4)
    total_errors2 = [0]

    async def one_round2(args_str):
        trs = ToolRoundState(
            tool_ctx_lru=ToolContextLRU(default_ttl=3),
            duo_deadline_at=time.time() + 600,
            wedge_state=wstate2,
            total_tool_errors=total_errors2,
        )
        return await execute_tool_round(
            tool_calls=[{"id": "c1", "function": {"name": "edit_file", "arguments": args_str}}],
            dtool_msgs=[], round_state=DuoRoundState(), hooks=hooks, trs=trs,
            tool_mode="duo_full", duo_ws=False, workspace_lock=str(ws),
            exec_model="test-model", exec_has_thinking=False, tool_think_auto_mode="",
            run_id_global="r1", chat_id="chat1", subtask_index=0)

    import json as _j
    ok_args = _j.dumps({"path": "wedged.js", "old_text": "line 7", "new_text": "line 7 patched"})
    seq = [fail_args(0), fail_args(1), ok_args, fail_args(2), fail_args(3)]
    results2 = [asyncio.run(one_round2(a)) for a in seq]
    check("exec: Erfolg in der Mitte resettet (2 Fails danach -> kein Wedge)",
          all(r.wedge_file == "" for r in results2),
          f"wf={[r.wedge_file for r in results2]}")

    # Feature off: wedge_state=None -> nie ein Signal
    async def one_round_off(args_str):
        trs = ToolRoundState(
            tool_ctx_lru=ToolContextLRU(default_ttl=3),
            duo_deadline_at=time.time() + 600,
            wedge_state=None,
            total_tool_errors=[0],
        )
        return await execute_tool_round(
            tool_calls=[{"id": "c1", "function": {"name": "edit_file", "arguments": args_str}}],
            dtool_msgs=[], round_state=DuoRoundState(), hooks=hooks, trs=trs,
            tool_mode="duo_full", duo_ws=False, workspace_lock=str(ws),
            exec_model="test-model", exec_has_thinking=False, tool_think_auto_mode="",
            run_id_global="r1", chat_id="chat1", subtask_index=0)

    results3 = [asyncio.run(one_round_off(fail_args(i))) for i in range(5)]
    check("exec: Feature off (wedge_state=None) -> kein Wedge-Signal",
          all(r.wedge_file == "" for r in results3))


if __name__ == "__main__":
    test_streak_threshold()
    test_reset_on_success()
    test_identical_repeat_fires_immediately()
    test_repeat_within_streak_not_consecutive()
    test_different_files_do_not_cross_trigger()
    test_handover_format()
    test_notices()
    test_verdict_parsing()
    test_invalidate_read_signature()
    test_executor_wedge_trigger()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
