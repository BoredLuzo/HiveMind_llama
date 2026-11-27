# -*- coding: utf-8 -*-
"""Regression suite for Wave 1 (P0 + guard-sync) deep-audit fixes.

Covers:
  1. [SKIP: already read] deadlock fix (mtime/size check, _record_written_path,
     evict_stale_reads_for_path cache_horizon, undo_last clear, 1x soft recovery).
  2. superseded_paths cleared after emitting [CTX-HORIZON] (no _recovery_saturated poison).
  3. _dr_invalid_tool_retries restricted to invalid-tool/schema error codes.
  4. Verify-gate closure: run_bash_failed, run_bash_confirmed_exit_zero,
     _run_bash_fail_fix_pass_insight, _green_exec_depth.
  5. taint.wrap_result skips [TOOL_ERROR responses without tainting run.
  6. Read-Ladder: range-read neutrality, failed/SKIP exclusion, run_tests/edit_ast/lsp_format
     reset, suppressed saturation gating.
  7. edit_loop_recoveries reset on write + write_file soft recovery enabled.
  8. apply_patch/edit_ast in preflight, duo_explore_gate, plan_tracker + _EDIT_NO_CHANGE_MARKERS.
  9. lsp_format._apply_text_edits EOF without trailing newline & same-position order.
 10. filter_secret_search_rows non-greedy match (URL port leak) & workspace forwarding.
 11. Windows rm shim: junction safety (rmdir without target recursion) & rmtree error surfacing.
 12. tools/sandbox.py:_posix_preexec gated by duo_tool_sandbox / duo_tool_sandbox_posix_rlimits.

Run: python tests/test_wave1_guards_and_tools.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402
from context.compression import evict_stale_reads_for_path  # noqa: E402
from core import duo_explore_gate as DEG  # noqa: E402
from core import plan_tracker as PT  # noqa: E402
from core import tool_exec_helpers as TEH  # noqa: E402
from core import tool_executor as TE  # noqa: E402
from core.tool_exec_helpers import ToolRoundState  # noqa: E402
from core.agentic_duo_state import DuoRoundState  # noqa: E402
from core.duo_task_complete_gate import _green_exec_depth  # noqa: E402
from hive_functions.memory import ToolContextLRU  # noqa: E402
from tools import errors as ER  # noqa: E402
from tools import sandbox as SB  # noqa: E402
from tools import taint as TAINT  # noqa: E402
from tools import runner as TR  # noqa: E402
from tools.handlers import exec_tools as ET  # noqa: E402
from tools.handlers.lsp import _apply_text_edits  # noqa: E402
from utils import file as UFILE  # noqa: E402
from utils import tool as UTOOL  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


async def _noop_emit(_ev):
    return None


def _make_hooks():
    return SimpleNamespace(
        emit=_noop_emit,
        request_stop=lambda *a, **k: None,
        on_tool_result=None,
        is_aborted=lambda _c: False,
        remember_insight=None,
    )


def test_1_skip_deadlock_and_cross_round_invalidation() -> None:
    """Fix 1: [SKIP] allows re-read after disk edit or cache_horizon supersession."""
    orig_cwd = os.getcwd()
    try:
        with tempfile.TemporaryDirectory(prefix="hm_w1_skip_", ignore_cleanup_errors=True) as td:
            ws = Path(td)
            target = ws / "app.py"
            target.write_text("x = 1\n", encoding="utf-8")

            TR._files_read_in_run.set(set())
            TR._files_seen_in_run.set(set())
            TR._files_written_in_run.set(set())
            TR._read_signatures.clear()

            r1 = asyncio.run(TR._run_inline_tool("read_file", {"path": "app.py"}, workspace_lock=str(ws)))
            check("1a initial read_file succeeds", "x = 1" in r1, r1[:120])

            r2 = asyncio.run(TR._run_inline_tool("read_file", {"path": "app.py"}, workspace_lock=str(ws)))
            check("1b unchanged second read_file hits [SKIP]", r2.startswith("[SKIP:"), r2[:120])

            # Modify on disk externally (size changed -> immediate stale detection in SKIP guard)
            target.write_text("x = 12345\n", encoding="utf-8")
            r3 = asyncio.run(TR._run_inline_tool("read_file", {"path": "app.py"}, workspace_lock=str(ws)))
            check("1c modified file bypasses [SKIP] and returns fresh content",
                  not r3.startswith("[SKIP:") and "x = 12345" in r3, r3[:120])

            # edit_file also discards from _read_set
            e1 = asyncio.run(TR._run_inline_tool(
                "edit_file", {"path": "app.py", "old_text": "x = 12345", "new_text": "x = 99"},
                workspace_lock=str(ws)))
            check("1d edit_file succeeds", not ER.tool_call_failed(e1, "edit_file"), e1[:120])
            r4 = asyncio.run(TR._run_inline_tool("read_file", {"path": "app.py"}, workspace_lock=str(ws)))
            check("1e read_file right after edit_file is NOT blocked by [SKIP]",
                  not r4.startswith("[SKIP:") and "x = 99" in r4, r4[:120])

            # cache_horizon supersession drops read guard even when evicted == 0
            key = UFILE.normalize_tool_path(str(target), ws)
            TR._files_read_in_run.get().add(key)
            msgs = [
                {"role": "system", "content": "sys"},
                {"role": "tool", "name": "read_file", "content": "x = 99"},
            ]
            lru = ToolContextLRU(default_ttl=3)
            lru.register(1, path=key, size_chars=6, kind="read_file", ttl_override=1)
            sup: list[str] = []
            ev = evict_stale_reads_for_path(
                messages=msgs, lru=lru, path=key, cache_horizon=10, superseded=sup,
            )
            check("1f cache_horizon supersession releases read_guard even when evicted==0",
                  ev == 0 and key in sup and key not in TR._files_read_in_run.get(),
                  f"ev={ev} sup={sup} guard={TR._files_read_in_run.get()}")
            os.chdir(orig_cwd)
    finally:
        os.chdir(orig_cwd)


def test_2_superseded_paths_cleared_after_emit() -> None:
    """Fix 2: superseded_paths clears after emitting [CTX-HORIZON] once."""
    msgs = [{"role": "system", "content": "sys"}]
    state = DuoRoundState()
    trs = ToolRoundState(
        tool_ctx_lru=ToolContextLRU(default_ttl=3),
        duo_deadline_at=time.monotonic() + 300,
        superseded_paths=["src/a.py"],
    )
    calls = [{"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": '{"path": "."}'}}]
    msgs.append({"role": "assistant", "content": "", "tool_calls": calls})

    async def _fake_one(*_a, **_kw):
        return "a.py"

    with mock.patch.object(TE, "_execute_one_tool", side_effect=_fake_one):
        asyncio.run(TE.execute_tool_round(
            tool_calls=calls, dtool_msgs=msgs, round_state=state, hooks=_make_hooks(),
            trs=trs, tool_mode="duo_full", duo_ws=False, workspace_lock=str(ROOT),
            exec_model="test-model", auto_test_before_complete=False,
            exec_has_thinking=False, tool_think_auto_mode="off",
            run_id_global="r1", chat_id="c1", subtask_index=0,
        ))
    hz_count = sum(1 for m in msgs if "[CTX-HORIZON]" in str(m.get("content", "")))
    check("2a [CTX-HORIZON] emitted once and superseded_paths cleared",
          hz_count == 1 and trs.superseded_paths == [],
          f"hz={hz_count} sup={trs.superseded_paths}")
    check("2b _recovery_saturated stays False after single [CTX-HORIZON]",
          not TEH._recovery_saturated(msgs))


def test_3_invalid_tool_retries_classification() -> None:
    """Fix 3: duo_runner _dr_invalid_tool_retries ignores execution errors."""
    dr_src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
    check("3a _INVALID_TOOL_CODES allowlist guards _dr_invalid_tool_retries",
          "_INVALID_TOOL_CODES" in dr_src
          and "INVALID_ARGUMENT" in dr_src
          and "TOOL_NOT_FOUND" in dr_src
          and "RUN_BASH_NONZERO" not in dr_src.split("_INVALID_TOOL_CODES")[1][:300])


def test_4_verify_gate_closure() -> None:
    """Fix 4: no-match grep, shim errors, declined gate, and CHAIN errors cannot pass verify."""
    check("4a tool_error_has_code accepts empty-tool Format B when tool_name given",
          ER.tool_error_has_code("[TOOL_ERROR: SHELL_CHAIN_UNSUPPORTED]\nmsg", "SHELL_CHAIN_UNSUPPORTED", "run_bash"))
    check("4b tool_error_has_code still rejects mismatched non-empty tool",
          not ER.tool_error_has_code("[TOOL_ERROR:RETRY] run_python: m", "RETRY", "run_bash"))

    bad_verify = (
        "[TOOL_ERROR: SHELL_CHAIN_UNSUPPORTED]\nrun_bash does not chain",
        "[rm error]: missing.txt: no such file",
        "[mkdir error]: bad",
        "[touch error]: bad",
        "[DESTRUCTIVE GATE: DECLINED] run_bash: blocked",
        "[USER INPUT — tool NOT executed]: hello",
    )
    for txt in bad_verify:
        check(f"4c run_bash_failed True: {txt[:32]!r}", UTOOL.run_bash_failed(txt))
        check(f"4d confirmed_exit_zero False: {txt[:32]!r}", not UTOOL.run_bash_confirmed_exit_zero(txt))

    for non_verify in (
        "[run_bash: exit 1 — no matches found]",
        "[exit 0 - removed: tmp.txt]",
        "[exit 0 - created 1 directory: sub]",
        "[exit 0 - touched 1 file(s)]",
        "",
    ):
        check(f"4e confirmed_exit_zero False for non-exec: {non_verify!r}",
              not UTOOL.run_bash_confirmed_exit_zero(non_verify))

    check("4f confirmed_exit_zero True for [run_bash: exit 0] even if stdout prints [exit code: 1]",
          not UTOOL.run_bash_failed("[run_bash: exit 0]\nsaw [exit code: 1] in test")
          and UTOOL.run_bash_confirmed_exit_zero("[run_bash: exit 0]\nsaw [exit code: 1] in test"))

    r_ns = SimpleNamespace(verify_last_ok_serial=0, verify_mutation_serial=3,
                           last_run_bash_failure=None, changed_since_failure=set(),
                           last_learned_insight_sig="")
    for res in ("[run_bash: exit 1 — no matches found]", "[rm error]: x", "[DESTRUCTIVE GATE: DECLINED] nope"):
        asyncio.run(TEH._run_bash_fail_fix_pass_insight("run_bash", {"cmd": "x"}, res, r_ns, None, None, 0))
    check("4g _run_bash_fail_fix_pass_insight does not bump verify_last_ok_serial on non-zero/shim/declined",
          r_ns.verify_last_ok_serial == 0, f"serial={r_ns.verify_last_ok_serial}")

    msgs_fake_green = [
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": "run_bash", "arguments": '{"cmd": "grep foo a.py"}'}},
            {"id": "c2", "function": {"name": "run_bash", "arguments": '{"cmd": "rm x"}'}},
            {"id": "c3", "function": {"name": "run_bash", "arguments": '{"cmd": "rm -rf a"}'}},
        ]},
        {"role": "tool", "name": "run_bash", "tool_call_id": "c1", "content": "[run_bash: exit 1 — no matches found]"},
        {"role": "tool", "name": "run_bash", "tool_call_id": "c2", "content": "[rm error]: x: no such file"},
        {"role": "tool", "name": "run_bash", "tool_call_id": "c3", "content": "[DESTRUCTIVE GATE: DECLINED] nope"},
    ]
    check("4h _green_exec_depth rejects no-match grep, rm error, and DECLINED",
          _green_exec_depth(msgs_fake_green) == (0, False),
          str(_green_exec_depth(msgs_fake_green)))


def test_5_taint_skips_tool_errors() -> None:
    """Fix 5: taint.wrap_result leaves [TOOL_ERROR unwrapped and does not taint."""
    TAINT.clear()
    err = "[TOOL_ERROR:WEB_FETCH_FAILED] web_fetch: timeout"
    wrapped = TAINT.wrap_result("web_fetch", err)
    check("5a wrap_result returns [TOOL_ERROR verbatim", wrapped == err, wrapped)
    check("5b failed untrusted call does not taint run", not TAINT.is_tainted())
    check("5c tool_call_failed sees wrapped error as failure", ER.tool_call_failed(wrapped, "web_fetch"))
    TAINT.clear()


def test_6_and_7_read_ladder_and_edit_loop_recovery() -> None:
    """Fix 6 & 7: Read-Ladder range/skip/saturation + edit_loop_recoveries reset & write_file."""
    trs = ToolRoundState(tool_ctx_lru=ToolContextLRU(default_ttl=3))
    for _ in range(10):
        TEH._update_read_ladder(trs, "read_file", False, "big.py", range_read=True)
        TEH._update_read_ladder(trs, "read_file", False, "big.py", failed_or_skipped=True)
    check("6a range reads and failed/SKIP reads do not increment wide or consecutive streaks",
          trs.wide_read_streak[0] == 0 and trs.consecutive_reads[0] == 0)

    TEH._update_read_ladder(trs, "read_file", False, "a.py")
    TEH._update_read_ladder(trs, "run_tests", False, "")
    check("6b run_tests resets Read-Ladder streaks", trs.wide_read_streak[0] == 0 and trs.consecutive_reads[0] == 0)

    TEH._update_read_ladder(trs, "read_file", False, "a.py")
    TEH._update_read_ladder(trs, "edit_ast", False, "a.py")
    check("6c edit_ast resets Read-Ladder streaks", trs.wide_read_streak[0] == 0)

    # Cross-file reads must NEVER emit a Read-Ladder UI banner or set read_ladder_fired
    emitted: list = []

    async def _cap_emit(ev):
        emitted.append(ev)

    hooks = SimpleNamespace(emit=_cap_emit, request_stop=lambda *a, **k: None,
                          on_tool_result=None, is_aborted=lambda _c: False, remember_insight=None)
    trs2 = ToolRoundState(
        tool_ctx_lru=ToolContextLRU(default_ttl=3),
        duo_deadline_at=time.monotonic() + 300,
    )
    sat_msgs = [
        {"role": "system", "content": "sys"},
    ]
    calls = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "z.py"}'}}]
    sat_msgs.append({"role": "assistant", "content": "", "tool_calls": calls})
    with mock.patch.object(TE, "_execute_one_tool", return_value="content"):
        asyncio.run(TE.execute_tool_round(
            tool_calls=calls, dtool_msgs=sat_msgs, round_state=DuoRoundState(), hooks=hooks,
            trs=trs2, tool_mode="duo_full", duo_ws=False, workspace_lock=str(ROOT),
            exec_model="test-model", auto_test_before_complete=False,
            exec_has_thinking=False, tool_think_auto_mode="off",
            run_id_global="r2", chat_id="c2", subtask_index=0,
        ))
    check("6d no Read-Ladder UI banner emitted on normal file read",
          not trs2.read_ladder_fired[0]
          and not any("Read-Ladder:" in str(e.get("content", "")) for e in emitted),
          f"fired={trs2.read_ladder_fired[0]} emitted={emitted}")

    # 7a: write_file gets 1x soft recovery; 7b: successful write resets edit_loop_recoveries
    trs3 = ToolRoundState(
        tool_ctx_lru=ToolContextLRU(default_ttl=3),
        duo_deadline_at=time.monotonic() + 300,
    )
    wf_calls = [
        {"id": f"w{i}", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "a.py", "content": "x"}'}}
        for i in range(3)
    ]
    wf_msgs = [{"role": "system", "content": "sys"}, {"role": "assistant", "content": "", "tool_calls": wf_calls}]
    with mock.patch.object(TE, "_execute_one_tool", return_value="[TOOL_ERROR:WRITE_FILE_NOOP] no change"):
        res_wf = asyncio.run(TE.execute_tool_round(
            tool_calls=wf_calls, dtool_msgs=wf_msgs, round_state=DuoRoundState(), hooks=_make_hooks(),
            trs=trs3, tool_mode="duo_full", duo_ws=False, workspace_lock=str(ROOT),
            exec_model="test-model", auto_test_before_complete=False,
            exec_has_thinking=False, tool_think_auto_mode="off",
            run_id_global="r3", chat_id="c3", subtask_index=0,
        ))
    check("7a 3x write_file loop triggers 1x soft recovery without aborting",
          not res_wf.loop_detected and trs3.edit_loop_recoveries[0] == 1,
          f"loop={res_wf.loop_detected} rec={trs3.edit_loop_recoveries}")

    ok_calls = [{"id": "w_ok", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "a.py", "content": "y"}'}}]
    wf_msgs.append({"role": "assistant", "content": "", "tool_calls": ok_calls})
    with mock.patch.object(TE, "_execute_one_tool", return_value="wrote a.py"):
        asyncio.run(TE.execute_tool_round(
            tool_calls=ok_calls, dtool_msgs=wf_msgs, round_state=DuoRoundState(), hooks=_make_hooks(),
            trs=trs3, tool_mode="duo_full", duo_ws=False, workspace_lock=str(ROOT),
            exec_model="test-model", auto_test_before_complete=False,
            exec_has_thinking=False, tool_think_auto_mode="off",
            run_id_global="r3", chat_id="c3", subtask_index=1,
        ))
    check("7b successful write resets edit_loop_recoveries to 0",
          trs3.edit_loop_recoveries[0] == 0, f"rec={trs3.edit_loop_recoveries}")


def test_8_allowlists_and_noop_markers() -> None:
    """Fix 7/8: apply_patch & edit_ast in core allowlists + modern NOOP markers."""
    pf_src = (ROOT / "core" / "duo" / "preflight.py").read_text(encoding="utf-8")
    check("8a preflight stuck_handler recognizes apply_patch/edit_ast",
          '"apply_patch", "edit_ast", "lsp_format"' in pf_src)
    check("8b duo_explore_gate _WRITE_TOOLS includes apply_patch/edit_ast/run_tests",
          {"apply_patch", "edit_ast", "lsp_format", "run_tests"} <= DEG._WRITE_TOOLS)
    check("8c plan_tracker maps apply_patch & advances on it",
          PT._TOOL_OP_MAP.get("apply_patch") == "edit"
          and PT._TOOL_OP_MAP.get("run_tests") == "run"
          and "apply_patch" in PT.PlanTracker._ADVANCING_TOOLS)
    for code in ("EDIT_FILE_OLD_TEXT_NOT_FOUND", "EDIT_FILE_NOT_UNIQUE", "WRITE_FILE_NOOP", "APPLY_PATCH_FAILED"):
        tool_name = "apply_patch" if "PATCH" in code else ("write_file" if "WRITE" in code else "edit_file")
        check(f"8d _is_no_change_result detects {code}",
              TEH._is_no_change_result(tool_name, f"[TOOL_ERROR:{code}] msg"))


def test_9_lsp_apply_text_edits() -> None:
    """Fix 9: _apply_text_edits handles EOF without newline & same-position order."""
    txt_no_nl = "x=1\ny=2"
    formatted = _apply_text_edits(txt_no_nl, [{
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 2, "character": 0}},
        "newText": "x = 1\ny = 2",
    }])
    check("9a EOF full-document edit on file without trailing newline does not duplicate last line",
          formatted == "x = 1\ny = 2", repr(formatted))

    same_ins = _apply_text_edits("world", [
        {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}, "newText": "A"},
        {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}, "newText": "B"},
    ])
    check("9b tied zero-width insertions keep array order", same_ins == "ABworld", repr(same_ins))

    ins_plus_rep = _apply_text_edits("hello world", [
        {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}, "newText": "PREFIX_"},
        {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}}, "newText": "HI"},
    ])
    check("9c zero-width insertion + replacement at same start stays clean",
          ins_plus_rep == "PREFIX_HI world", repr(ins_plus_rep))


def test_10_filter_secret_search_rows() -> None:
    """Fix 10: filter_secret_search_rows non-greedy match & workspace forwarding."""
    rows = (
        "C:\\ws\\.env:1:DB=postgres://u:secret@host:5432:db\n"
        "app/config:stream.key:4:PRIVATE\n"
        "C:\\projects\\chats\\myproj\\main.py:1:x = 1\n"
    )
    out = UFILE.filter_secret_search_rows(rows, workspace="C:\\projects\\chats\\myproj")
    check("10a drops .env URL:port row and ADS secret row",
          "postgres://u:secret" not in out and "PRIVATE" not in out, repr(out))
    check("10b keeps workspace file even under parent named 'chats'",
          "main.py:1:x = 1" in out, repr(out))


def test_11_rm_shim_junction_and_error_surfacing() -> None:
    """Fix 11: Windows rm shim removes junction without target recursion and surfaces rmtree errors."""
    with tempfile.TemporaryDirectory(prefix="hm_w1_rm_") as td:
        ws = Path(td)
        real_pkg = ws / "real_pkg"
        real_pkg.mkdir()
        precious = real_pkg / "precious.txt"
        precious.write_text("keep me", encoding="utf-8")
        junc = ws / "junc"
        junc.mkdir()  # simulated junction directory removed by os.rmdir(junc) if _is_junction True

        orig_plat = ET.sys.platform
        ET.sys.platform = "win32"
        try:
            # Test rm junc/file.txt rejection when ancestor is a junction
            (junc / "sub.txt").write_text("x", encoding="utf-8")
            with mock.patch.object(UFILE, "_is_junction", side_effect=lambda p: Path(p).name == "junc"), \
                    mock.patch.object(ET, "_is_junction", side_effect=lambda p: Path(p).name == "junc"):
                r_inside = asyncio.run(ET._inline_tool_run_bash({"cmd": "rm junc/sub.txt"}, ws, str(ws)))
            check("11a rm junc/file refused before .resolve()",
                  "PATH_JUNCTION_DENIED" in r_inside and (junc / "sub.txt").exists(), r_inside)
            (junc / "sub.txt").unlink()

            # Test rm -rf junc calls os.rmdir(junc) and leaves real_pkg/precious.txt untouched
            with mock.patch.object(ET, "_is_junction", side_effect=lambda p: Path(p) == junc):
                r_junc = asyncio.run(ET._inline_tool_run_bash({"cmd": "rm -rf junc"}, ws, str(ws)))
            check("11b rm -rf junction removes reparse dir only and leaves target intact",
                  "[exit 0 - removed:" in r_junc and not junc.exists() and precious.exists(), r_junc)

            # Test shutil.rmtree failure surfaces in [rm error] instead of [exit 0 - removed]
            locked_dir = ws / "locked"
            locked_dir.mkdir()
            (locked_dir / "f.txt").write_text("x", encoding="utf-8")
            with mock.patch.object(ET.shutil, "rmtree", side_effect=PermissionError("locked in use")):
                r_err = asyncio.run(ET._inline_tool_run_bash({"cmd": "rm -rf locked"}, ws, str(ws)))
            check("11c rmtree PermissionError reported cleanly via [rm error]",
                  "[rm error]:" in r_err and "locked" in r_err and "removed:" not in r_err, r_err)
        finally:
            ET.sys.platform = orig_plat


def test_12_posix_sandbox_gate() -> None:
    """Fix 12: _posix_preexec gated on duo_tool_sandbox and duo_tool_sandbox_posix_rlimits."""
    import settings as settings_mod

    with mock.patch.object(settings_mod, "load_settings", return_value={
        "duo_tool_sandbox": True,
        "duo_tool_sandbox_max_mem_mb": 4096,
        "duo_tool_sandbox_max_procs": 64,
        "duo_tool_sandbox_max_cpu_s": 0,
    }):
        check("12a default Windows Job Object settings keep _posix_preexec disabled",
              SB._posix_preexec() is None)

    with mock.patch.object(settings_mod, "load_settings", return_value={
        "duo_tool_sandbox": False,
        "duo_tool_sandbox_posix_rlimits": True,
        "duo_tool_sandbox_max_mem_mb": 1024,
        "duo_tool_sandbox_max_procs": 32,
        "duo_tool_sandbox_max_cpu_s": 10,
    }):
        check("12b duo_tool_sandbox=False disables _posix_preexec",
              SB._posix_preexec() is None)

    fake_resource = SimpleNamespace(
        RLIMIT_AS=9,
        RLIMIT_NPROC=6,
        RLIMIT_CPU=0,
        RLIM_INFINITY=-1,
        getrlimit=lambda _r: (100, 100),
        setrlimit=lambda *_a: None,
    )
    with mock.patch.dict(sys.modules, {"resource": fake_resource}), \
            mock.patch.object(settings_mod, "load_settings", return_value={
                "duo_tool_sandbox": True,
                "duo_tool_sandbox_max_cpu_s": 30,
            }):
        fn = SB._posix_preexec()
        check("12c duo_tool_sandbox_max_cpu_s > 0 returns preexec callable", callable(fn))

    sb_src = (ROOT / "tools" / "sandbox.py").read_text(encoding="utf-8")
    apply_block = sb_src.split("def _apply()")[1].split("def spawn_kwargs")[0]
    check("12d _apply contains zero logging calls (fork-safe)", "_lg." not in apply_block and "logging." not in apply_block)


def main() -> int:
    test_1_skip_deadlock_and_cross_round_invalidation()
    test_2_superseded_paths_cleared_after_emit()
    test_3_invalid_tool_retries_classification()
    test_4_verify_gate_closure()
    test_5_taint_skips_tool_errors()
    test_6_and_7_read_ladder_and_edit_loop_recovery()
    test_8_allowlists_and_noop_markers()
    test_9_lsp_apply_text_edits()
    test_10_filter_secret_search_rows()
    test_11_rm_shim_junction_and_error_surfacing()
    test_12_posix_sandbox_gate()
    return h.finish("wave1_guards_and_tools")


if __name__ == "__main__":
    raise SystemExit(main())
