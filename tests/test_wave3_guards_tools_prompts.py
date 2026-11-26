# -*- coding: utf-8 -*-
"""Regression suite for Wave 3 (guard polish, handler edge cases, schemas & prompts).

Covers:
  1. tool_error_retries & attempts_per_file reset on success + escalation preserves anchor/output.
  2. run_bash wedge resets instead of handing command verbs to file-only fix_agent; exit-0 resets.
  3. auto_test_before_complete avoids duplicate tool_call_id, matches parallel run_bash by ID,
     ignores failed writes in _last_edit_idx, and recognizes [run_python: executed successfully].
  4. call_sigs canonicalizes dict key order; chunking.compute_bash_changed detects new files and
     distinguishes empty-before/empty-after; _collect_new_explore_paths handles bare list_dir().
  5. _is_destructive_bash narrows TRUNCATE TABLE / shutdown, catches iex "payload", respects Git
     Bash chmod +x; _is_destructive_python catches .unlink()/.rmdir()/Path.write_text; _destructive_gate
     accepts "1|note"; read-guard covers edit_ast/lsp_format & doesn't leak _any_read_set on error.
  6. _prefetch_readonly_tools skips run_tests when approval enabled and skips invalid-JSON rounds.
  7. install_package avoids double approval via ContextVar, keeps workspace fallback, relabels timeouts.
  8. test_runner rejects leading '-' targets safely.
  9. _read_binary_error interpolates filename & respects Git Bash; write_file flags directories;
     edit_file keeps literal <<<<<<< SEARCH when new_text is non-empty; _unique_ws_normalized_replace
     handles asymmetric newlines; fuzzy_replace keeps trailing blank lines and reindents uniformly.
 10. start_background evicts exited entries first; browser URL-encodes paths, isolates blocked state,
     and resolves screenshot None / POSIX paths; code_intel & web_tools reject non-int caps cleanly.
 11. core/send_files blocks gateway.toml/soul.json/memories_db.json/sessions/chats/logs/ADS.
 12. Schemas (allow_overwrite, paths, 45s timeout, blocked build_status), task_complete reason folding,
     and prompt consistency (relative paths, valid blocked status format, no SEARCH/REPLACE/edit_file dupe).

Run: python tests/test_wave3_guards_tools_prompts.py
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
from core import duo_dtool_setup as DDS  # noqa: E402
from core import duo_helpers as DH  # noqa: E402
from core import duo_plan_round as DPR  # noqa: E402
from core import send_files as SF  # noqa: E402
from core import tool_exec_helpers as TEH  # noqa: E402
from core import tool_executor as TE  # noqa: E402
from core import wedge_handoff as WH  # noqa: E402
from core.agentic_duo_state import DuoRoundState  # noqa: E402
from core.plan_tracker import Plan, PlanStep, PlanTracker  # noqa: E402
from hive_functions import chunking as CH  # noqa: E402
from hive_functions import prompts as PR  # noqa: E402
from hive_functions import test_runner as TRN  # noqa: E402
from hive_functions.memory import ToolContextLRU  # noqa: E402
from infra import run_control as RC  # noqa: E402
from tools import background as BG  # noqa: E402
from tools import browser as BR  # noqa: E402
from tools import definitions as DEF  # noqa: E402
from tools import patch_2_fuzzy_edit as P2  # noqa: E402
from tools import runner as TR  # noqa: E402
from tools.handlers import code_intel as CI  # noqa: E402
from tools.handlers import exec_tools as ET  # noqa: E402
from tools.handlers import file_ops as FO  # noqa: E402
from tools.handlers import misc as MISC  # noqa: E402
from tools.handlers import web_tools as WT  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check


# ── 1. Guard hint preservation & counter resets ─────────────────────────────
_err_msgs: list[dict] = []
_attempts = {"pf_not_found:a.py": 2}
_orig_anchor = "[TOOL_ERROR:EDIT_FILE_OLD_TEXT_NOT_FOUND] edit_file: Anchor around line 12: def foo(): pass"
_new_res, _matched = TEH._inject_tool_error_hints(
    "edit_file", {"path": "a.py"}, _orig_anchor, {"id": "tc_anchor"},
    _err_msgs, _attempts, {},
)
check("1a escalation keeps original anchor snippet in tool message",
      _matched and len(_err_msgs) == 1 and "Anchor around line 12" in _err_msgs[0]["content"]
      and "Switch strategy" in _err_msgs[0]["content"])

_tl_msgs: list[dict] = []
asyncio.run(TEH._handle_too_large(
    "write_file", {"path": "big.py"}, "too big", {"id": "tc_big"},
    _tl_msgs, [""], DuoRoundState(), SimpleNamespace(emit=lambda _e: asyncio.sleep(0)),
    tool_mode="duo_core",
))
check("1b _handle_too_large recommends write_file (not edit_file) for chunk 1",
      "1. write_file(path, <first segment>)" in _tl_msgs[0]["content"]
      and "1. edit_file(" not in _tl_msgs[0]["content"])

_py_to = TEH._read_required_and_python_hints(
    "run_python", {},
    '[TOOL_ERROR:RUN_PYTHON_TIMEOUT] run_python: Python execution timed out.\n'
    '[stdout]\nslept after 2s\n[TOOL_ERROR_META] {"timeout_s": 60}',
    {},
)
check("1c run_python timeout hint reads timeout_s from META and ignores stdout 'after 2s'",
      "timed out after 60s" in _py_to and "after 2s" not in _py_to.rsplit("timed out after", 1)[-1])


# ── 2. Executor: run_bash reset, wedge non-handoff, auto_test, canonical call_sigs ─
async def _noop_emit(_ev):
    return None


def _make_trs(**over):
    kw = dict(
        tool_ctx_lru=ToolContextLRU(default_ttl=3),
        duo_deadline_at=time.monotonic() + 600,
        attempts_per_file={},
        tool_error_retries={},
        call_sigs=[],
        recent_focus_paths=[],
        file_changes={},
        duo_seen_web_queries=set(),
        cached_coder_port=[None],
        task_complete_blocked_count=[0],
        total_tool_errors=[0],
        edit_loop_recoveries=[0],
    )
    kw.update(over)
    return TEH.ToolRoundState(**kw)


with tempfile.TemporaryDirectory(prefix="hm_w3_exec_", ignore_cleanup_errors=True) as td:
    ws = Path(td)
    target = ws / "app.py"
    target.write_text("x = 1\n", encoding="utf-8")

    # 2a. run_bash exit 0 resets tool_error_retries and run_bash wedge streak
    _retries = {"RUN_BASH_NONZERO": 3, "RUN_BASH_TIMEOUT": 1, "RUN_BASH_BLOCKED": 1}
    _wstate = WH.new_wedge_state(threshold=4)
    WH.note_edit_failure(_wstate, "run_bash", "pytest", '{"cmd":"pytest -q"}', "[TOOL_ERROR:RUN_BASH_NONZERO]")

    async def _fake_exec_ok(name, args, **_kw):
        if name == "run_bash":
            return "[run_bash: exit 0]\n3 passed"
        if name == "edit_file":
            return "[edit_file: 'app.py' - 1/1 blocks applied (+1/-1)]"
        return "ok"

    _hooks = TE.ToolExecHooks(emit=_noop_emit, is_aborted=lambda _c: False, on_tool_result=None)
    _trs_rb = _make_trs(tool_error_retries=_retries, wedge_state=_wstate)
    with mock.patch.object(TEH, "_run_inline_tool", side_effect=_fake_exec_ok):
        _res_rb = asyncio.run(TE.execute_tool_round(
            tool_calls=[{"id": "c1", "function": {"name": "run_bash", "arguments": '{"cmd":"pytest -q"}'}}],
            dtool_msgs=[], round_state=DuoRoundState(), hooks=_hooks, trs=_trs_rb,
            tool_mode="duo_core", duo_ws=False, workspace_lock=str(ws),
            exec_model="m", exec_has_thinking=False, tool_think_auto_mode="",
            run_id_global="r1", chat_id="ch1", subtask_index=0,
        ))
    check("2a exit-0 run_bash resets RUN_BASH_* retries and wedge streak",
          _retries["RUN_BASH_NONZERO"] == 0 and _retries["RUN_BASH_TIMEOUT"] == 0
          and _retries["RUN_BASH_BLOCKED"] == 0 and WH.streak_of(_wstate, "pytest") == 0)

    # 2b. run_bash repeated timeout wedge does NOT set wedge_file to command verb
    _wstate2 = WH.new_wedge_state(threshold=4)
    WH.note_edit_failure(_wstate2, "run_bash", "pytest", '{"cmd":"pytest"}', "[TOOL_ERROR:RUN_BASH_TIMEOUT]")

    async def _fake_exec_to(name, args, **_kw):
        return "[TOOL_ERROR:RUN_BASH_TIMEOUT] run_bash: timed out after 60s"

    _trs_to = _make_trs(wedge_state=_wstate2)
    _msgs_to: list[dict] = []
    with mock.patch.object(TEH, "_run_inline_tool", side_effect=_fake_exec_to):
        _res_to = asyncio.run(TE.execute_tool_round(
            tool_calls=[{"id": "c2", "function": {"name": "run_bash", "arguments": '{"cmd":"pytest"}'}}],
            dtool_msgs=_msgs_to, round_state=DuoRoundState(), hooks=_hooks, trs=_trs_to,
            tool_mode="duo_core", duo_ws=False, workspace_lock=str(ws),
            exec_model="m", exec_has_thinking=False, tool_think_auto_mode="",
            run_id_global="r1", chat_id="ch1", subtask_index=0,
        ))
    check("2b run_bash wedge never sets wedge_file to command verb",
          _res_to.wedge_file == "" and "[WEDGE] run_bash 'pytest'" in _msgs_to[-1]["content"])

    # 2c. successful write clears attempts_per_file for target
    _att_clear = {"app.py": 2, "pf_not_found:app.py": 2, "pf_non_unique:app.py": 1}
    _trs_cl = _make_trs(attempts_per_file=_att_clear)
    with mock.patch.object(TEH, "_run_inline_tool", side_effect=_fake_exec_ok):
        asyncio.run(TE.execute_tool_round(
            tool_calls=[{"id": "c3", "function": {"name": "edit_file", "arguments": '{"path":"app.py","old_text":"x = 1","new_text":"x = 2"}'}}],
            dtool_msgs=[], round_state=DuoRoundState(), hooks=_hooks, trs=_trs_cl,
            tool_mode="duo_core", duo_ws=False, workspace_lock=str(ws),
            exec_model="m", exec_has_thinking=False, tool_think_auto_mode="",
            run_id_global="r1", chat_id="ch1", subtask_index=0,
        ))
    check("2c successful write clears attempts_per_file for target",
          "pf_not_found:app.py" not in _att_clear and "app.py" not in _att_clear)

    # 2d. auto_test_before_complete: 1:1 tool_call_id, parallel run_bash match, ignores failed write
    _at_msgs = [
        {"role": "assistant", "tool_calls": [
            {"id": "tc_w1", "function": {"name": "write_file", "arguments": '{"path":"app.py","content":"x=1"}'}},
            {"id": "tc_ls", "function": {"name": "run_bash", "arguments": '{"cmd":"ls"}'}},
            {"id": "tc_py", "function": {"name": "run_bash", "arguments": '{"cmd":"pytest"}'}},
            {"id": "tc_e_fail", "function": {"name": "edit_file", "arguments": '{"path":"app.py"}'}},
        ]},
        {"role": "tool", "tool_call_id": "tc_w1", "name": "write_file", "content": "[write_file: created 'app.py' (+1)]"},
        {"role": "tool", "tool_call_id": "tc_ls", "name": "run_bash", "content": "[run_bash: exit 0]\napp.py"},
        {"role": "tool", "tool_call_id": "tc_py", "name": "run_bash", "content": "[run_bash: exit 0]\n1 passed"},
        {"role": "tool", "tool_call_id": "tc_e_fail", "name": "edit_file", "content": "[TOOL_ERROR:EDIT_FILE_OLD_TEXT_NOT_FOUND] miss"},
    ]
    _at_called = []

    async def _fake_tc(name, args, **_kw):
        if name == "run_tests":
            _at_called.append(name)
        return "[task_complete] Build: passing"

    _trs_at = _make_trs(verify_mutation_serial=1)
    with mock.patch.object(TE, "_run_inline_tool", side_effect=_fake_tc):
        _res_at = asyncio.run(TE.execute_tool_round(
            tool_calls=[{"id": "tc_done", "function": {"name": "task_complete", "arguments": '{"status":{"completed":["x"],"blockers":[],"build_status":"passing"}}'}}],
            dtool_msgs=_at_msgs, round_state=DuoRoundState(), hooks=_hooks, trs=_trs_at,
            tool_mode="duo_core", duo_ws=False, workspace_lock=str(ws),
            exec_model="m", exec_has_thinking=False, tool_think_auto_mode="",
            run_id_global="r1", chat_id="ch1", subtask_index=0, auto_test_before_complete=True,
        ))
    check("2d parallel run_bash('pytest') matched by tool_call_id & failed edit ignored",
          _res_at.task_complete_called is True and _at_called == [])

    # Also test auto_test_before_complete runs when no test ran and never duplicates tool_call_id
    _at_msgs2 = [
        {"role": "tool", "tool_call_id": "tc_w2", "name": "write_file", "content": "[write_file: created 'app.py' (+1)]"},
    ]

    async def _fake_tc_auto(name, args, **_kw):
        if name == "run_tests":
            return "[TEST-RESULT] ✅ All tests passed (python)"
        return "[task_complete] Build: untested"

    _trs_at2 = _make_trs(verify_mutation_serial=1)
    with mock.patch.object(TE, "_run_inline_tool", side_effect=_fake_tc_auto):
        _res_at2 = asyncio.run(TE.execute_tool_round(
            tool_calls=[{"id": "tc_done2", "function": {"name": "task_complete", "arguments": '{"status":{"completed":["x"],"blockers":[],"build_status":"untested"}}'}}],
            dtool_msgs=_at_msgs2, round_state=DuoRoundState(), hooks=_hooks, trs=_trs_at2,
            tool_mode="duo_core", duo_ws=False, workspace_lock=str(ws),
            exec_model="m", exec_has_thinking=False, tool_think_auto_mode="",
            run_id_global="r1", chat_id="ch1", subtask_index=0, auto_test_before_complete=True,
        ))
    _tc_ids = [m.get("tool_call_id") for m in _at_msgs2 if m.get("role") == "tool"]
    check("2e auto_test appends footer without duplicate tool_call_id",
          _res_at2.task_complete_called is True and _tc_ids.count("tc_done2") == 1
          and "[AUTO-TEST]" in _at_msgs2[-1]["content"])

    # 2f. run_python [run_python: executed successfully ...] counts as verified exec
    _rp_msgs = [
        {"role": "tool", "tool_call_id": "rp1", "name": "run_python", "content": "[run_python: executed successfully — no output produced]"},
    ]
    _trs_rp = _make_trs()
    with mock.patch.object(TE, "_run_inline_tool", side_effect=_fake_tc):
        _res_rp = asyncio.run(TE.execute_tool_round(
            tool_calls=[{"id": "tc_rp", "function": {"name": "task_complete", "arguments": '{"status":{"completed":["x"],"blockers":[],"build_status":"passing"}}'}}],
            dtool_msgs=_rp_msgs, round_state=DuoRoundState(), hooks=_hooks, trs=_trs_rp,
            tool_mode="duo_core", duo_ws=False, workspace_lock=str(ws),
            exec_model="m", exec_has_thinking=False, tool_think_auto_mode="",
            run_id_global="r1", chat_id="ch1", subtask_index=0, auto_test_before_complete=False,
        ))
    check("2f silent exit-0 run_python counts as verified exec for task_complete",
          _res_rp.task_complete_called is True)

    # 2g. call_sigs canonicalizes dict argument ordering
    _trs_sig = _make_trs()
    with mock.patch.object(TEH, "_run_inline_tool", return_value="no match"):
        asyncio.run(TE.execute_tool_round(
            tool_calls=[
                {"id": "s1", "function": {"name": "search_code", "arguments": '{"pattern":"foo","path":"."}'}},
                {"id": "s2", "function": {"name": "search_code", "arguments": '{"path":".","pattern":"foo"}'}},
            ],
            dtool_msgs=[], round_state=DuoRoundState(), hooks=_hooks, trs=_trs_sig,
            tool_mode="duo_core", duo_ws=False, workspace_lock=str(ws),
            exec_model="m", exec_has_thinking=False, tool_think_auto_mode="",
            run_id_global="r1", chat_id="ch1", subtask_index=0,
        ))
    check("2g call_sigs canonicalizes JSON key order",
          len(_trs_sig.call_sigs) == 2 and _trs_sig.call_sigs[0] == _trs_sig.call_sigs[1])


# ── 3. chunking & explore paths ─────────────────────────────────────────────
check("3a compute_bash_changed detects brand-new files in snap_after",
      CH.compute_bash_changed({"a.py": {"mtime": 1.0, "size": 1}}, {"a.py": {"mtime": 1.0, "size": 1}, "b.py": {"mtime": 2.0, "size": 5}}) is True)
check("3b compute_bash_changed returns False when both snaps empty",
      CH.compute_bash_changed({}, {}) is False and CH.compute_bash_changed({}, {"a.py": {"mtime": 1.0, "size": 1}}) is None)

_seen_exp: set[str] = set()
_first_ld = DH._collect_new_explore_paths([{"function": {"name": "list_dir", "arguments": {}}}], _seen_exp)
_second_ld = DH._collect_new_explore_paths([{"function": {"name": "list_dir", "arguments": {}}}], _seen_exp)
_first_ff = DH._collect_new_explore_paths([{"function": {"name": "find_files", "arguments": {}}}], _seen_exp)
check("3c _collect_new_explore_paths treats bare list_dir() as new once then repeat",
      _first_ld == ["."] and _second_ld == [] and _first_ff == ["."])


# ── 4. Destructive gate, Python patterns, answer note, read-guard ───────────
check("4a TRUNCATE/Shutdown narrowed; iex quotes gated",
      TR._is_destructive_bash("pytest -k truncate") is None
      and TR._is_destructive_bash("grep shutdown server.py") is None
      and TR._is_destructive_bash("TRUNCATE TABLE logs;") is not None
      and TR._is_destructive_bash("shutdown /s") is not None
      and TR._is_destructive_bash('iex "Invoke-Payload"') is not None)

with mock.patch("tools.handlers.exec_tools._run_bash_shell_mode", return_value="bash"):
    check("4b chmod +x allowed on Git Bash while chmod -w and -R stay gated",
          TR._is_destructive_bash("chmod +x run.sh") is None
          and TR._is_destructive_bash("chmod -w run.sh") is not None
          and TR._is_destructive_bash("chmod -R 777 .") is not None)

check("4c _is_destructive_python catches Path.unlink/rmdir and abs Path.write_text",
      TR._is_destructive_python("from pathlib import Path\nPath('a.py').unlink()") is not None
      and TR._is_destructive_python("from pathlib import Path\nPath('dir').rmdir()") is not None
      and TR._is_destructive_python("Path('/etc/hosts').write_text('x')") is not None
      and TR._is_destructive_python("Path('rel.txt').write_text('x')") is None)


async def _gate_with_note():
    orig_wait = RC.wait_for_resume

    async def _mock_wait(*_a, **_k):
        return "1|keep stdout clean"

    RC.wait_for_resume = _mock_wait
    tok = TR._current_run_id.set("gate_w3_note")
    TR._pause_timeout_s.set(60)
    try:
        return await TR._destructive_gate("run_bash", {"cmd": "rm -rf /tmp/x"})
    finally:
        RC.wait_for_resume = orig_wait
        RC.cleanup_pause("gate_w3_note")
        TR._current_run_id.reset(tok)


check("4d _destructive_gate accepts '1|note' confirmation",
      asyncio.run(_gate_with_note()) is None)


# ── 5. _prefetch_readonly_tools & install_package single-approval ───────────
async def _test_prefetch():
    called = []

    async def _fake(name, args, **_kw):
        called.append(name)
        return f"ok:{name}"

    orig_rit = TEH._run_inline_tool
    TEH._run_inline_tool = _fake
    try:
        with mock.patch("tools.runner._needs_approval", return_value=True):
            res_appr = await TEH._prefetch_readonly_tools(
                [{"function": {"name": "run_tests", "arguments": "{}"}}],
                "m", "ws", "duo_core", False,
            )
        with mock.patch("tools.runner._needs_approval", return_value=False):
            res_bad = await TEH._prefetch_readonly_tools(
                [{"function": {"name": "read_file", "arguments": "{bad json"}}],
                "m", "ws", "duo_core", False,
            )
    finally:
        TEH._run_inline_tool = orig_rit
    return res_appr, res_bad, called


_pf_appr, _pf_bad, _pf_called = asyncio.run(_test_prefetch())
check("5a prefetch skips run_tests when approval enabled & skips bad JSON rounds",
      _pf_appr == {} and _pf_bad == {} and _pf_called == [])


with tempfile.TemporaryDirectory(prefix="hm_w3_inst_") as td_i:
    wsi = Path(td_i)
    (wsi / ".venv" / "Scripts").mkdir(parents=True)
    (wsi / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    orig_rit2 = TR._run_inline_tool
    seen_funnel = []

    async def _mock_rit_inst(name, args, workspace_lock=None, **_kw):
        seen_funnel.append((TR._in_approved_funnel.get(), workspace_lock))
        return '[TOOL_ERROR:RUN_BASH_TIMEOUT] run_bash: run_bash timed out after 90s.\n[TOOL_ERROR_META] {"tool": "run_bash"}'

    TR._run_inline_tool = _mock_rit_inst
    try:
        out_inst = asyncio.run(ET._inline_tool_install_package(
            {"manager": "pip", "packages": "requests"}, wsi, None,
        ))
    finally:
        TR._run_inline_tool = orig_rit2
    check("5b install_package sets _in_approved_funnel, forwards workspace, relabels TIMEOUT",
          seen_funnel == [(True, str(wsi))] and "[TOOL_ERROR:INSTALL_TIMEOUT]" in out_inst
          and '"tool": "install_package"' in out_inst)


# ── 6. test_runner CLI flag safety ──────────────────────────────────────────
with tempfile.TemporaryDirectory(prefix="hm_w3_tr_") as td_tr:
    ws_tr = Path(td_tr)
    (ws_tr / "test_ok.py").write_text("def test_one(): pass\n", encoding="utf-8")
    _blocked_targets: list[str] = []
    for _bad_tgt in ("--version", "./--version"):
        try:
            TRN._resolve_scoped_target(_bad_tgt, str(ws_tr))
        except ValueError:
            _blocked_targets.append(_bad_tgt)
    _argv = TRN._build_builtin_test_argv(
        "python", "python -m pytest -x -q {paths}", ["test_ok.py"], str(ws_tr),
    )
    check("6 test_runner rejects leading '-' and './--...' targets cleanly",
          len(_blocked_targets) == 2 and _argv is not None and _argv[-1] == "test_ok.py")


# ── 7. file_ops, fuzzy_replace, background, browser, code_intel, web_tools ──
with mock.patch("tools.handlers.exec_tools._run_bash_shell_mode", return_value="bash"):
    _bin_msg = FO._read_binary_error(Path("assets/logo.png"))
check("7a _read_binary_error interpolates filename & avoids PowerShell hint in Git Bash",
      "{p}" not in _bin_msg and "logo.png" in _bin_msg and "Get-Content" not in _bin_msg)

with tempfile.TemporaryDirectory(prefix="hm_w3_fo_") as td_fo:
    ws_fo = Path(td_fo)
    (ws_fo / "subdir").mkdir()
    _dir_err = asyncio.run(FO._inline_tool_write_file(
        {"path": "subdir", "content": "abc"}, ws_fo, str(ws_fo),
    ))
    check("7b write_file rejects directory target with WRITE_FILE_IS_DIR",
          "WRITE_FILE_IS_DIR" in _dir_err)

    lit = ws_fo / "parser.py"
    lit.write_text(
        'A = """\n<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n"""\n',
        encoding="utf-8",
    )
    _lit_res = asyncio.run(FO._inline_tool_edit_file(
        {
            "path": "parser.py",
            "old_text": "<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE",
            "new_text": "<<<<<<< SEARCH\nx2\n=======\ny2\n>>>>>>> REPLACE",
        },
        ws_fo, str(ws_fo),
    ))
    check("7c edit_file keeps literal <<<<<<< SEARCH when new_text is non-empty",
          "TOOL_ERROR" not in _lit_res and "x2" in lit.read_text(encoding="utf-8"))

_asym = FO._unique_ws_normalized_replace(
    "def f():\n    x = 1   \n\n    return x\n",
    "    x = 1\n",
    "    x = 2",
)
check("7d _unique_ws_normalized_replace preserves blank line on asymmetric newline",
      _asym == "def f():\n    x = 2\n\n    return x\n", f"got={_asym!r}")

_fz_blank = P2.fuzzy_replace("foo\n\nbar\n", "foo\n\n", "baz\n")
_fz_indent = P2.fuzzy_replace(
    "class C:\n    def f(self, value):\n        total = value + 1\n        scaled = total * 2\n        checked = scaled - 1\n        return checked\n",
    "def f(self, value):\n    total = value + 1\n    scaled = total * 2\n    checked = scaled - 9\n    return checked\n",
    "def f(self, value):\n    total = value + 10\n    scaled = total * 2\n    checked = scaled - 9\n    return checked\n",
)
check("7e fuzzy_replace keeps trailing blank line and reindents shifted block",
      _fz_blank == "baz\nbar\n" and _fz_indent is not None and "        total = value + 10" in _fz_indent,
      f"blank={_fz_blank!r} indent={_fz_indent!r}")

# start_background evicts exited before running via real BG.start_background()
_live_p = SimpleNamespace(pid=101, poll=lambda: None, terminate=lambda: None, kill=lambda: None, wait=lambda timeout=None: None, stdout=iter([]))
_dead_p = SimpleNamespace(pid=102, poll=lambda: 0, terminate=lambda: None, kill=lambda: None, wait=lambda timeout=None: None, stdout=iter([]))
_new_bg_p = SimpleNamespace(pid=999, poll=lambda: None, terminate=lambda: None, kill=lambda: None, wait=lambda timeout=None: None, stdout=iter([]))
with BG._LOCK:
    _saved_reg = dict(BG._REGISTRY)
    BG._REGISTRY.clear()
try:
    BG._REGISTRY["live0"] = {"proc": _live_p, "cmd": "server", "started": 1.0, "buf": ""}
    for _k in range(1, BG._MAX_PROCESSES - 1):
        BG._REGISTRY[f"live{_k}"] = {"proc": _live_p, "cmd": "worker", "started": 10.0 + _k, "buf": ""}
    BG._REGISTRY["dead_mid"] = {"proc": _dead_p, "cmd": "done", "started": 50.0, "buf": ""}
    with mock.patch("tools.background.subprocess.Popen", return_value=_new_bg_p), \
         mock.patch("tools.sandbox.ToolJob.confine", return_value=None), \
         mock.patch("tools.sandbox.kill_tree"):
        _bg_ev_res = BG.start_background("echo fresh")
finally:
    with BG._LOCK:
        BG._REGISTRY.clear()
        BG._REGISTRY.update(_saved_reg)
check("7f start_background eviction prefers exited entry over oldest live server",
      _bg_ev_res.get("ok") is True and _bg_ev_res.get("evicted", {}).get("handle") == "dead_mid")

# browser URL encoding & screenshot path resolution & contextvar isolation
with tempfile.TemporaryDirectory(prefix="hm_w3_br_") as td_br:
    ws_br = Path(td_br)
    (ws_br / "index #1.html").write_text("<h1>hi</h1>", encoding="utf-8")
    with mock.patch.object(BR, "_ensure_file_server", return_value=12345):
        _url_q, _url_err = BR._plan_file_navigation((ws_br / "index #1.html").as_uri(), str(ws_br))
    check("7g browser _plan_file_navigation percent-encodes #, ?, %",
          not _url_err and _url_q.endswith("/index%20%231.html"), f"got={_url_q!r} err={_url_err!r}")

    _fake_page = SimpleNamespace(
        screenshot=lambda path, full_page=False: Path(path).write_bytes(b"PNG"),
    )
    with mock.patch.object(BR, "_ensure_page", return_value=_fake_page):
        _sc1 = asyncio.run(BR.browser_tool({"action": "screenshot", "path": None}, ws_br, str(ws_br)))
        _sc2 = asyncio.run(BR.browser_tool({"action": "screenshot", "path": str((ws_br / "shot.png").resolve())}, ws_br, str(ws_br)))
    check("7h browser screenshot handles path=None and workspace-absolute path cleanly",
          (ws_br / "screenshot.png").is_file() and (ws_br / "shot.png").is_file()
          and "None" not in _sc1 and "OUTSIDE" not in _sc2,
          f"sc1={_sc1!r} sc2={_sc2!r}")

# ContextVar isolation across browser_tool calls
with mock.patch.object(BR, "_ensure_page", return_value=SimpleNamespace()):
    _b1 = asyncio.run(BR.browser_tool({"action": "navigate", "url": "http://127.0.0.1:9999/"}, Path("."), "."))
    _b2 = asyncio.run(BR.browser_tool({"action": "navigate", "url": "http://127.0.0.1:9999/"}, Path("."), "."))
check("7i browser blocked-navigation tracker is run/call-context isolated (no permanent leak on executor thread)",
      "attempt #2" not in _b1 and "attempt #2" not in _b2)

# code_intel & web_tools bad int coercion
with tempfile.TemporaryDirectory(prefix="hm_w3_ci_") as td_ci:
    ws_ci = Path(td_ci)
    _gs_res = asyncio.run(CI._inline_tool_get_signatures({"path": "missing.py", "max_items": "all"}, ws_ci, str(ws_ci)))
    _fr_res = asyncio.run(CI._inline_tool_find_references({"symbol": "foo", "max_items": "10.5"}, ws_ci, str(ws_ci)))
with mock.patch.object(WT._shared, "_WEBSEARCH_AVAILABLE", True), \
     mock.patch.object(WT._shared, "_safe_web_search", new=mock.AsyncMock(return_value="[]"), create=True):
    _ws_res = asyncio.run(WT._inline_tool_web_search({"query": "x", "max_results": "many"}, Path("."), None))
check("7j non-int max_items/max_results fall back safely instead of raising ValueError",
      ("missing.py" in _gs_res or "OUTSIDE" in _gs_res or "FAILED" in _gs_res or "UNAVAILABLE" in _gs_res or "exists" in _gs_res)
      and bool(_fr_res) and _ws_res == "[]")


# ── 8. send_files hardened secret & junction checks ─────────────────────────
with tempfile.TemporaryDirectory(prefix="hm_w3_sf_") as td_sf:
    ws_sf = Path(td_sf) / "chats" / "workspace"  # parent ancestor named 'chats'
    ws_sf.mkdir(parents=True)
    (ws_sf / "report.txt").write_text("ok", encoding="utf-8")
    (ws_sf / "gateway.toml").write_text("secret=1", encoding="utf-8")
    (ws_sf / "sessions").mkdir()
    (ws_sf / "sessions" / "c1.json").write_text("{}", encoding="utf-8")
    check("8a send_files allows normal file even if parent path contains 'chats'",
          SF.resolve_workspace_file(ws_sf, "report.txt").name == "report.txt")
    for _bad_sf in ("gateway.toml", "sessions/c1.json", "report.txt:stream"):
        try:
            SF.resolve_workspace_file(ws_sf, _bad_sf)
            _sf_blocked = False
        except SF.SendFileError:
            _sf_blocked = True
        check(f"8b send_files blocks {_bad_sf}", _sf_blocked)


# ── 9. Schemas, task_complete reason folding & prompts ──────────────────────
_tool_map = {t["function"]["name"]: t["function"] for t in DEF._INLINE_CODING_TOOLS}
check("9a schemas expose allow_overwrite, run_tests.paths, 45s timeout, blocked build_status",
      "allow_overwrite" in _tool_map["write_file"]["parameters"]["properties"]
      and "allow_overwrite" in _tool_map["edit_file"]["parameters"]["properties"]
      and "paths" in _tool_map["run_tests"]["parameters"]["properties"]
      and "45s timeout" in _tool_map["run_python"]["description"]
      and "blocked" in _tool_map["task_complete"]["parameters"]["properties"]["status"]["properties"]["build_status"]["enum"])

_tc_out = asyncio.run(MISC._inline_tool_task_complete(
    {"status": "blocked", "reason": "missing API key"}, Path("."), None,
))
check("9b task_complete preserves legacy status='blocked' + reason='...'",
      "Build: blocked" in _tc_out and "missing API key" in _tc_out)

_rp_txt = PlanTracker(plan=Plan(steps=[PlanStep(id="1", intent="fix")], current_step_id="1")).replan_prompt()
check("9c prompts/setup/replan use valid formats without contradictory instructions",
      "Prefix every file path" not in Path(DDS.__file__).read_text(encoding="utf-8")
      and "status='blocked'" not in _rp_txt
      and "edit_file or edit_file" not in PR.AGENTIC_CODER_SYSTEM
      and "Git Bash or PowerShell on Windows" in PR.DUO_CODER_BASE
      and "Do NOT read more than 2 files" not in PR.DUO_CODER_BASE
      and "no re-read needed" not in PR.DUO_CODER_BASE
      and "status=blocked and reason" not in PR.DUO_CODER_AUTONOMOUS
      and "status=blocked and a clear reason" not in PR.DUO_CODER_REPLAN
      and "SEARCH/REPLACE edits" not in PR.DUO_CODER_NO_CHUNK
      and DPR._duo_plan_tracker_round is not None)

sys.exit(h.finish("wave3_guards_tools_prompts"))
