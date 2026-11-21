# -*- coding: utf-8 -*-
"""Regression suite for Audit Wave 2B-1: core, duo, tooling, planner, vision & frontend fixes.

Covers all 10 verified Wave 2B-1 fixes hermetically (no live LLM, GPU, or network):
  1. core/duo/preflight.py + duo_runner.py: _await_with_hb exact timeout & cancel await; unbounded event queue & inner reconnect
  2. core/duo_runner.py: grace-round gate emits paired role="tool" before user nudge
  3. core/tool_loop.py + llm_transport.py: deferred JSON parse feedback & real ctx_overflow compression hook
  4. core/tool_executor.py: deadline UnboundLocalError + paired trailing tool_calls on deadline/wedge/err_cap/loop
  5. hive_functions/git_tools.py: validate files/scope before git reset --soft (no lost checkpoints)
  6. tools/handlers/file_ops.py + lsp.py: late capture_before right before write, LspError catch, CRLF preservation
  7. tools/handlers/exec_tools.py: install_package PowerShell '&' call quoting
  8. hive_functions/planner.py: run_inloop_planner honors aborted_fn immediately & mid-flight
  9. vision/preprocess.py: acquires get_port_lock and rejects non-200 HTTP status
 10. static/app.js + index.html: serialized postSettings, chat-switch epoch guard, TTS URL revocation, SSE/journal lifecycle
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


# ── 1. _await_with_hb exact timeout & duo_runner inner reconnect/queue ─────

def test_preflight_hb_and_duo_reconnect() -> None:
    from core.duo.preflight import _await_with_hb

    cancelled_cleanup = [False]

    async def _slow_coro():
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)
            cancelled_cleanup[0] = True
            raise

    async def _run_hb() -> tuple[bool, float]:
        t0 = time.monotonic()
        timed_out = False
        try:
            await _await_with_hb(
                _slow_coro,
                timeout=0.08,
                emit_fn=lambda _ev: asyncio.sleep(0),
                label="Test",
                interval=0.6,
            )
        except asyncio.TimeoutError:
            timed_out = True
        return timed_out, time.monotonic() - t0

    timed_out, elapsed = asyncio.run(_run_hb())
    check("_await_with_hb respects tight timeout without waiting full interval",
          timed_out and elapsed < 0.35, f"timed_out={timed_out} elapsed={elapsed:.3f}s")
    check("_await_with_hb awaits cancelled task cleanup before returning",
          cancelled_cleanup[0] is True)

    dr_src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
    check("duo_runner uses unbounded _coder_event_q (maxsize=0)",
          "asyncio.Queue(maxsize=0)" in dr_src)
    check("duo_runner catches transient httpx errors inside while _dr without outer continue",
          "except (\n                            httpx.ReadError," in dr_src
          and "ctx.exec_ctrl.sync_tool_rounds(_total_tool_rounds)\n                            _dr -= 1\n                            continue" in dr_src
          and "if _dce_type in (\"ReadError\"" not in dr_src)
    check("duo_runner grace round appends role='tool' rejection before user nudge",
          'if _gr.action in ("continue", "break"):' in dr_src
          and '"content": "[GRACE ROUND] Rejected — call ONLY task_complete."' in dr_src)


# ── 2. ToolLoop role pairing + real ctx_overflow hook ───────────────────────

def test_tool_loop_pairing_and_overflow() -> None:
    from core.tool_loop import ToolLoop, ToolLoopConfig

    # 2a: Multi-tool batch with invalid JSON args keeps all role="tool" before user feedback
    sent_payloads: list[dict] = []
    turn = [0]

    def _bad_json_handler(request: httpx.Request) -> httpx.Response:
        turn[0] += 1
        sent_payloads.append(json.loads(request.content.decode("utf-8") or "{}"))
        if turn[0] == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {"id": "tc_bad_1", "type": "function",
                                 "function": {"name": "read_file", "arguments": "{broken_json"}},
                                {"id": "tc_good_2", "type": "function",
                                 "function": {"name": "read_file", "arguments": '{"path": "ok.py"}'}},
                            ],
                        }
                    }]
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "recovered"}}]},
            request=request,
        )

    async def _run_batch() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_bad_json_handler)) as cli:
            loop = ToolLoop(
                ToolLoopConfig(
                    stream=False,
                    max_rounds=3,
                    max_post_attempts=2,
                    tools=[{"type": "function", "function": {"name": "read_file"}}],
                ),
                cli,
                9999,
                custom_executor=lambda _n, _a: asyncio.sleep(0, result="file-ok"),
            )
            async for _ev in loop.run([{"role": "user", "content": "read ok.py"}]):
                pass

    asyncio.run(_run_batch())
    msgs_after = sent_payloads[1]["messages"] if len(sent_payloads) >= 2 else []
    roles_after = [m.get("role") for m in msgs_after]
    # Expected sequence: user, assistant, tool(tc_bad_1), tool(tc_good_2), user(parse_error feedback)
    check("ToolLoop multi-tool invalid JSON emits ALL role='tool' before deferred user feedback",
          roles_after == ["user", "assistant", "tool", "tool", "user"]
          and msgs_after[2].get("tool_call_id") == "tc_bad_1"
          and "INVALID_JSON" in msgs_after[2].get("content", "")
          and msgs_after[3].get("tool_call_id") == "tc_good_2",
          f"roles={roles_after}")

    # 2b: ctx_overflow 400 actually invokes _on_compress, retries with shrunk payload, and syncs
    overflow_payloads: list[dict] = []
    ov_turn = [0]

    def _overflow_handler(request: httpx.Request) -> httpx.Response:
        ov_turn[0] += 1
        overflow_payloads.append(json.loads(request.content.decode("utf-8") or "{}"))
        if ov_turn[0] == 1:
            return httpx.Response(400, text="Prompt exceeds context window", request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "compressed-ok"}}]},
            request=request,
        )

    compress_calls: list[int] = []

    async def _fake_compress(msgs: list[dict], rnd: int) -> list[dict]:
        compress_calls.append(rnd)
        return [{"role": "user", "content": "short"}]

    async def _run_overflow() -> list[dict]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_overflow_handler)) as cli:
            loop = ToolLoop(
                ToolLoopConfig(
                    stream=False,
                    max_rounds=1,
                    max_post_attempts=3,
                    retry_on_context_overflow=True,
                    compress_enabled=False,
                ),
                cli,
                9999,
                on_compress=_fake_compress,
            )
            evs: list[dict] = []
            async for ev in loop.run([{"role": "user", "content": "X" * 5000}]):
                evs.append(ev)
            return evs

    ov_events = asyncio.run(_run_overflow())
    check("ctx_overflow 400 calls _on_compress and retries with shrunk messages",
          len(overflow_payloads) == 2
          and len(str(overflow_payloads[1]["messages"][0]["content"])) < 100
          and compress_calls == [-1]
          and any(e.get("type") == "ctx_overflow" for e in ov_events),
          f"calls={len(overflow_payloads)} compress={compress_calls}")

    # 2c: ctx_overflow without shrink / _on_compress fails fast on attempt 1
    no_hook_calls = [0]

    def _no_shrink_handler(request: httpx.Request) -> httpx.Response:
        no_hook_calls[0] += 1
        return httpx.Response(400, text="Prompt exceeds context window", request=request)

    async def _run_fail_fast() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_no_shrink_handler)) as cli:
            loop = ToolLoop(
                ToolLoopConfig(
                    stream=False,
                    max_rounds=1,
                    max_post_attempts=5,
                    retry_on_context_overflow=True,
                    compress_enabled=False,
                ),
                cli,
                9999,
                on_compress=None,
            )
            try:
                async for _ev in loop.run([{"role": "user", "content": "hi"}]):
                    pass
                return ""
            except RuntimeError as exc:
                return str(exc)

    err_msg = asyncio.run(_run_fail_fast())
    check("ctx_overflow without _on_compress fails fast instead of spamming 400s",
          no_hook_calls[0] == 1 and "context overflow" in err_msg.lower(),
          f"calls={no_hook_calls[0]} err={err_msg[:80]}")


# ── 3. execute_tool_round deadline & loop-break pairing ─────────────────────

def test_tool_executor_pairing_and_deadline() -> None:
    import core.tool_exec_helpers as _helpers
    import core.tool_executor as TE
    from core.agentic_duo_state import DuoRoundState
    from core.tool_exec_helpers import ToolRoundState
    from core.tool_executor import ToolExecHooks

    orig_run = TE._run_inline_tool
    orig_helpers_run = _helpers._run_inline_tool

    class _FakeLRU:
        default_ttl = 600
        def decay(self, _path): pass
        def register(self, _idx, **_kw): pass

    def _mk_trs(**over):
        kw = dict(
            tool_ctx_lru=_FakeLRU(),
            duo_deadline_at=time.monotonic() + 60,
            verify_mutation_serial=0, verify_last_ok_serial=0,
            last_run_bash_failure=None, changed_since_failure=set(),
            last_learned_insight_sig="", last_too_large_path=[None],
            attempts_per_file={}, tool_error_retries={}, call_sigs=[],
            recent_focus_paths=[], file_changes={}, duo_seen_web_queries=set(),
            cached_coder_port=[None], task_complete_blocked_count=None, total_tool_errors=None,
            allow_send_file=False,
        )
        kw.update(over)
        return ToolRoundState(**kw)

    async def _noop_emit(_ev):
        return ""

    async def _run_exec(tcs, trs, fake_handlers):
        dtool_msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": tcs},
        ]

        async def _fake_tool(name, args, **_kw):
            return fake_handlers.get(name, "ok")

        TE._run_inline_tool = _fake_tool
        _helpers._run_inline_tool = _fake_tool
        try:
            res = await TE.execute_tool_round(
                tool_calls=tcs,
                dtool_msgs=dtool_msgs,
                round_state=DuoRoundState(),
                hooks=ToolExecHooks(emit=_noop_emit, is_aborted=lambda _c: False),
                trs=trs,
                tool_mode="full",
                duo_ws=False,
                workspace_lock=str(ROOT),
                exec_model="m:1b",
                exec_has_thinking=False,
                tool_think_auto_mode="off",
                run_id_global="r1",
                chat_id="c1",
                subtask_index=0,
            )
            return res, dtool_msgs
        finally:
            TE._run_inline_tool = orig_run
            _helpers._run_inline_tool = orig_helpers_run

    def _tc(tid, name, args):
        return {"id": tid, "type": "function", "function": {"name": name, "arguments": args}}

    # 3a: deadline hit at _i == 0 pairs ALL tool calls and avoids UnboundLocalError
    res_dl, msgs_dl = asyncio.run(_run_exec(
        [_tc("dl1", "read_file", {"path": "a.py"}), _tc("dl2", "list_dir", {"path": "."})],
        _mk_trs(duo_deadline_at=time.monotonic() - 1.0),
        {},
    ))
    dl_tool_ids = [m.get("tool_call_id") for m in msgs_dl if m.get("role") == "tool"]
    check("execute_tool_round deadline at i=0 pairs all tool calls cleanly",
          res_dl.duo_timed_out and dl_tool_ids == ["dl1", "dl2"],
          f"dl_tool_ids={dl_tool_ids}")

    # 3b: run_bash ABAB loop pairs trailing unexecuted call BEFORE user nudge
    res_bl, msgs_bl = asyncio.run(_run_exec(
        [
            _tc("b1", "run_bash", {"cmd": "x"}),
            _tc("b2", "run_bash", {"cmd": "y"}),
            _tc("b3", "run_bash", {"cmd": "x"}),
            _tc("b4", "run_bash", {"cmd": "y"}),
            _tc("b5", "read_file", {"path": "after.py"}),
        ],
        _mk_trs(),
        {"run_bash": "ok"},
    ))
    bl_tail_roles = [(m.get("role"), m.get("tool_call_id")) for m in msgs_bl[3:]]
    check("run_bash loop break pairs trailing tool_call before user nudge",
          not res_bl.loop_detected
          and bl_tail_roles[-2] == ("tool", "b5")
          and bl_tail_roles[-1][0] == "user"
          and "[loop-detection: skipped]" in msgs_bl[-2].get("content", ""),
          f"tail={bl_tail_roles}")

    # 3c: non-run_bash loop abort does NOT duplicate tool_call_id and pairs trailing call
    res_nl, msgs_nl = asyncio.run(_run_exec(
        [
            _tc("n1", "read_file", {"path": "a.py"}),
            _tc("n2", "read_file", {"path": "b.py"}),
            _tc("n3", "read_file", {"path": "a.py"}),
            _tc("n4", "read_file", {"path": "b.py"}),
            _tc("n5", "list_dir", {"path": "."}),
        ],
        _mk_trs(),
        {"read_file": "content"},
    ))
    nl_tool_ids = [m.get("tool_call_id") for m in msgs_nl if m.get("role") == "tool"]
    check("non-bash loop abort keeps unique tool_call_ids and pairs trailing call",
          res_nl.loop_detected
          and nl_tool_ids == ["n1", "n2", "n3", "n4", "n5"]
          and any("[loop-detection: aborted]" in str(m.get("content", "")) for m in msgs_nl),
          f"ids={nl_tool_ids}")


# ── 4. git_tools pre-checks scope BEFORE git reset --soft ───────────────────

def test_git_squash_prechecks_before_reset() -> None:
    from hive_functions import git_tools as G

    orig_block = G.auto_commit_block_reason
    G.auto_commit_block_reason = lambda _ws: ""
    try:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)

            def _git(*args):
                return subprocess.run(
                    ["git", *args], cwd=repo, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                )

            _git("init", "-q")
            _git("config", "user.email", "test@example.invalid")
            _git("config", "user.name", "Test")
            (repo / "a.txt").write_text("v1\n", encoding="utf-8")
            _git("add", "a.txt")
            _git("commit", "-q", "-m", "init")
            marker = G._checkpoint_marker()
            for i in (2, 3):
                (repo / "a.txt").write_text(f"v{i}\n", encoding="utf-8")
                _git("add", "a.txt")
                _git("commit", "-q", "-m", f"{marker} cp{i}")

            head_before = _git("rev-parse", "HEAD").stdout.strip()
            count_before = len(_git("log", "--oneline").stdout.strip().splitlines())

            out_empty = asyncio.run(G.exec_git_squash_checkpoints(
                "squash", str(repo), consolidate_only=False, files=None,
            ))
            out_missing = asyncio.run(G.exec_git_squash_checkpoints(
                "squash", str(repo), consolidate_only=False, files=["nonexistent.py"],
            ))
            out_cons_missing = asyncio.run(G.exec_git_squash_checkpoints(
                "consolidate", str(repo), consolidate_only=True, files=["nonexistent.py"],
            ))

            head_after = _git("rev-parse", "HEAD").stdout.strip()
            count_after = len(_git("log", "--oneline").stdout.strip().splitlines())
            check("exec_git_squash_checkpoints preserves HEAD & checkpoints when scope invalid/empty",
                  head_before == head_after
                  and count_before == count_after == 3
                  and "refusing" in out_empty.lower()
                  and "nothing to fold" in out_missing.lower()
                  and "nothing to fold" in out_cons_missing.lower(),
                  f"head_eq={head_before == head_after} count={count_after}")
    finally:
        G.auto_commit_block_reason = orig_block


# ── 5. file_ops late capture_before + lsp_format LspError/CRLF/capture ──────

def test_file_ops_late_capture_and_lsp_format() -> None:
    from infra.lsp_client import LspError
    from tools.handlers import file_ops as FO
    from tools.handlers import lsp as LSP
    from tools.workspace import new_transaction

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        f_py = ws / "mod.py"
        orig_py = "VALUE = 1\n\ndef keep_me():\n    return VALUE\n"
        f_py.write_text(orig_py, encoding="utf-8")

        tx = new_transaction()
        tx.begin()

        # Rejected mutations must NOT populate tx._snapshots
        asyncio.run(FO._inline_tool_write_file({"path": str(f_py), "content": orig_py}, ws, str(ws)))
        asyncio.run(FO._inline_tool_write_file({"path": str(f_py), "content": "OTHER = 2\n"}, ws, str(ws)))
        asyncio.run(FO._inline_tool_edit_file({"path": str(f_py), "old_text": "NOT_THERE", "new_text": "X"}, ws, str(ws)))
        asyncio.run(FO._inline_tool_edit_file_batch({"path": str(f_py)}, f_py, ws, str(ws), "bad"))
        asyncio.run(FO._inline_tool_patch_file({"path": str(f_py), "old_str": "", "new_str": "x"}, ws, str(ws)))
        asyncio.run(FO._inline_tool_write_file_append({"path": str(f_py), "content": ""}, ws, str(ws)))

        check("rejected writes/edits do NOT record spurious capture_before snapshots",
              len(tx._snapshots) == 0, f"snapshots={list(tx._snapshots)}")

        # Valid edit DOES capture before writing & supports undo_path
        res_ed = asyncio.run(FO._inline_tool_edit_file(
            {"path": str(f_py), "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
            ws, str(ws),
        ))
        check("valid edit captures baseline before write",
              "replaced" in res_ed and len(tx._snapshots) == 1)
        ok_undo, _ = tx.undo_path(f_py)
        check("undo_path restores pre-edit content after valid capture",
              ok_undo and f_py.read_text(encoding="utf-8") == orig_py)

        # lsp_format LspError catch, CRLF preservation, and capture_before
        crlf_file = ws / "crlf.py"
        crlf_file.write_bytes(b"x=1\r\ny=2\r\n")
        tx_lsp = new_transaction()
        tx_lsp.begin()

        class _ErrMgr:
            async def format(self, _p):
                raise LspError("server crashed")

        class _FmtMgr:
            async def format(self, _p):
                return [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}},
                         "newText": "x = 1"}]

        with mock.patch.object(LSP, "_prepare", side_effect=[
            (_ErrMgr(), crlf_file, "python", None),
            (_FmtMgr(), crlf_file, "python", None),
        ]):
            err_res = asyncio.run(LSP._inline_tool_lsp_format({"path": str(crlf_file)}, ws, str(ws)))
            fmt_res = asyncio.run(LSP._inline_tool_lsp_format({"path": str(crlf_file)}, ws, str(ws)))

        raw_bytes = crlf_file.read_bytes()
        check("lsp_format catches LspError cleanly as LSP_FAILED",
              "TOOL_ERROR:LSP_FAILED" in err_res)
        check("lsp_format preserves CRLF line endings and captures undo baseline",
              "Formatted" in fmt_res
              and raw_bytes == b"x = 1\r\ny=2\r\n"
              and str(crlf_file.resolve()) in tx_lsp._snapshots,
              f"raw={raw_bytes!r}")


# ── 6. install_package PowerShell quoting ───────────────────────────────────

def test_install_package_powershell_quoting() -> None:
    import tools.runner as _runner
    from tools.handlers import exec_tools as ET

    captured: list[str] = []

    async def _fake_dispatch(name, args, **_kw):
        captured.append(str(args.get("cmd", "")))
        return "[run_bash: exit 0]\nok"

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        venv_py = ws / ".venv" / "Scripts" / "python.exe"
        venv_py.parent.mkdir(parents=True)
        venv_py.write_bytes(b"")

        orig_dispatch = _runner._run_inline_tool
        _runner._run_inline_tool = _fake_dispatch
        try:
            with mock.patch.object(ET, "_run_bash_shell_mode", return_value="powershell"):
                asyncio.run(ET._inline_tool_install_package({"manager": "pip", "packages": "pytest"}, ws, None))
            with mock.patch.object(ET, "_run_bash_shell_mode", return_value="bash"):
                asyncio.run(ET._inline_tool_install_package({"manager": "pip", "packages": "pytest"}, ws, None))
        finally:
            _runner._run_inline_tool = orig_dispatch

    check("install_package uses '&' PowerShell call operator in powershell mode and quotes in bash mode",
          len(captured) == 2
          and captured[0].startswith("& '") and "pytest" in captured[0]
          and captured[1].startswith('"'),
          f"captured={captured}")


# ── 7. run_inloop_planner honors aborted_fn ─────────────────────────────────

def test_planner_honors_aborted_fn() -> None:
    from hive_functions import planner as P

    post_calls = [0]

    class _AbortClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            post_calls[0] += 1
            await asyncio.sleep(30.0)
            raise AssertionError("unreachable")

    # Pre-loop immediate abort
    res_imm = asyncio.run(P.run_inloop_planner(
        task="t", explore_ctx="ctx", planner_model="m:1b", planner_port=9999,
        aborted_fn=lambda: True,
    ))
    check("run_inloop_planner aborts immediately when aborted_fn is True",
          res_imm == ("", "", True))

    # Mid-request abort cancels post task promptly
    abort_flag = [False]

    async def _flip_abort():
        await asyncio.sleep(0.05)
        abort_flag[0] = True

    async def _run_mid_abort():
        flipper = asyncio.create_task(_flip_abort())
        try:
            with mock.patch.object(P.httpx, "AsyncClient", _AbortClient):
                t0 = time.monotonic()
                out = await P.run_inloop_planner(
                    task="t", explore_ctx="ctx", planner_model="m:1b", planner_port=9999,
                    aborted_fn=lambda: abort_flag[0],
                )
                return out, time.monotonic() - t0
        finally:
            await flipper

    out_mid, elapsed = asyncio.run(_run_mid_abort())
    check("run_inloop_planner cancels hung post on mid-flight abort without waiting timeout",
          out_mid == ("", "", True) and post_calls[0] == 1 and elapsed < 2.5,
          f"out={out_mid} elapsed={elapsed:.2f}s")


# ── 8. vision/preprocess get_port_lock & HTTP status check ──────────────────

def test_vision_preprocess_lock_and_status() -> None:
    import vision.preprocess as VP
    from backend import llama_server_manager as LSM

    lock_entered = [0]

    class _FakeLock:
        async def __aenter__(self):
            lock_entered[0] += 1
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeResp:
        status_code = 500

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aread(self):
            return b"corrupt image payload"

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *_a, **_k):
            return _FakeResp()

    orig_cfg = dict(VP._vision_cfg)
    orig_settings = VP.settings
    orig_get_ctx = VP._get_num_ctx
    orig_valid = VP.is_valid_preprocessing_model
    try:
        VP._vision_cfg.update({"enabled": True, "model": "vlm:2b", "prompt": ""})
        VP.settings = {"duo_llm_slow_timeout_s": 5, "vision_preprocess_load_timeout_s": 5}
        VP._get_num_ctx = lambda *a, **k: 8192
        VP.is_valid_preprocessing_model = lambda _m: True
        with mock.patch.object(LSM.manager, "ensure_loaded", return_value=8105), \
             mock.patch.object(VP, "get_port_lock", return_value=_FakeLock()), \
             mock.patch.object(VP.httpx, "AsyncClient", _FakeClient):
            res_vp = asyncio.run(VP._preprocess_images_to_text(
                ["data:image/png;base64,iVBORw0KGgo="], "describe",
            )) or ""
    finally:
        VP._vision_cfg.clear()
        VP._vision_cfg.update(orig_cfg)
        VP.settings = orig_settings
        VP._get_num_ctx = orig_get_ctx
        VP.is_valid_preprocessing_model = orig_valid
    check("vision _preprocess_images_to_text acquires port lock and surfaces non-200 HTTP errors",
          lock_entered[0] == 1 and "HTTP 500" in res_vp and "corrupt image payload" in res_vp,
          f"locks={lock_entered[0]} res={res_vp!r}")


# ── 9. Frontend static/app.js + index.html behavioral JS checks ─────────────

def test_frontend_wave2b1_js_behavior() -> None:
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    check("index.html bumped app.js cache-buster to >= 20261121-04",
          re.search(r"/static/app\.js\?v=20261121-(0[4-9]|[1-9]\d)", html) is not None)

    node = shutil.which("node")
    if not node:
        check("Node.js available for app.js VM behavioral checks", False)
        return

    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    # Extract exact slices/functions from static/app.js so Node vm executes real production code!
    settings_slice = js[
        js.index("let _settingsPatchQueue = {};")
        :js.index("// WS-GUARD-FEEDBACK")
    ]
    tts_slice = (
        js[js.index("var _ttsAudio = null;"):js.index("async function _ttsFetchBlob")]
        + "\n"
        + js[js.index("function _ttsAudioFromBlob"):js.index("function _readAloudNext")]
    )
    new_chat_fn = js[js.index("async function newChat()"):js.index("function showInfo(")]
    load_chat_fn = js[js.index("async function loadChat(chatId)"):js.index("async function deleteChat(")]
    sse_tail_slice = (
        js[js.index("function _finalizeStreamUi()"):js.index("var _TOOL_ICONS =")]
        + "\n"
        + js[js.index("function _sseFeedLines"):js.index("function _journalInitAttach")]
    )
    send_slice = js[js.index("if (!res.ok) {\n      var _httpErr"):js.index("if (!S._sawDoneEvent && S.currentRunId")]

    probe = """
const vm = require('vm');
const assert = require('assert');

(async () => {
  // (A) postSettings serialization + per-batch waiters + S.settingsRev forwarding
  {
    const fetchCalls = [];
    let inflight = 0;
    let maxInflight = 0;
    const ctx = vm.createContext({
      S: { settingsRev: 1 },
      console, setTimeout, clearTimeout,
      showInfo: () => {},
      refreshVoiceFlags: () => {},
      fetch: (url, opts) => {
        inflight++;
        if (inflight > maxInflight) maxInflight = inflight;
        const body = JSON.parse(opts.body);
        fetchCalls.push(body);
        return new Promise((resolve) => setTimeout(() => {
          inflight--;
          resolve({
            ok: true,
            json: async () => ({ settings_rev: (body.ui_rev || 1) + 1 }),
          });
        }, 35));
      },
    });
    vm.runInContext(SETTINGS_CODE, ctx);
    let doneA = false, doneB = false;
    const pA = ctx.postSettings({ a: 1 }).then(() => { doneA = true; });
    await new Promise(r => setTimeout(r, 200)); // batch A now in-flight
    const pB = ctx.postSettings({ b: 2 }).then(() => { doneB = true; });
    await Promise.all([pA, pB]);
    assert.strictEqual(maxInflight, 1, 'fetches must never overlap');
    assert.strictEqual(fetchCalls.length, 2, 'two sequential batches expected');
    assert.strictEqual(fetchCalls[0].ui_rev, 1);
    assert.strictEqual(fetchCalls[1].ui_rev, 2, 'batch B must carry updated settings_rev=2');
    assert.ok(doneA && doneB);
  }

  // (B) newChat / loadChat epoch guard ignores stale async resolves
  {
    let chatDom = 'initial';
    let lastShown = '';
    const ctx = vm.createContext({
      S: { streaming: false, workspace: '/ws', currentChatId: 'old', currentChatMessages: [] },
      console, setTimeout, clearTimeout,
      confirm: () => true,
      document: {
        getElementById: (id) => id === 'chat'
          ? { get innerHTML() { return chatDom; }, set innerHTML(v) { chatDom = v; } }
          : { style: {}, value: '' },
        querySelectorAll: () => [],
      },
      _readAloudStop: () => {},
      _cleanupLoadTimers: () => {},
      _cpReset: () => {},
      setPauseBtnState: () => {},
      setStopBtnState: () => {},
      stopAskUserCountdown: () => {},
      _setPresetHeader: () => {},
      showInfo: (t) => { lastShown = t; },
      showStatus: () => {},
      loadChatHistory: () => {},
      scrollBtm: () => {},
      addUserMsg: () => {},
      fetch: (url) => {
        if (url === '/chats/slow') {
          return new Promise(r => setTimeout(() => r({ json: async () => ({ id: 'slow', title: 'SlowChat', messages: [], rev: 1 }) }), 50));
        }
        if (url === '/chats/fast') {
          return new Promise(r => setTimeout(() => r({ json: async () => ({ id: 'fast', title: 'FastChat', messages: [], rev: 2 }) }), 5));
        }
        return Promise.resolve({ ok: true, json: async () => ({ active: false }) });
      },
    });
    vm.runInContext(NEW_CHAT_CODE + '\\n' + LOAD_CHAT_CODE, ctx);
    const pSlow = ctx.loadChat('slow');
    const pFast = ctx.loadChat('fast');
    await Promise.all([pSlow, pFast]);
    assert.strictEqual(ctx.S.currentChatId, 'fast', 'stale slow loadChat must not overwrite fast switch');
    assert.ok(lastShown.includes('FastChat'), 'UI should reflect fast chat');
  }

  // (C) TTS URL.createObjectURL + URL.revokeObjectURL on ended and stop
  {
    const created = [], revoked = [];
    class FakeAudio {
      constructor(url) { this.src = url; this.listeners = {}; this.volume = 1; }
      addEventListener(ev, fn) { this.listeners[ev] = fn; }
      pause() {}
    }
    const ctx = vm.createContext({
      S: { ttsVolume: 0.75 },
      URL: { createObjectURL: () => { const u = 'blob://' + created.length; created.push(u); return u; }, revokeObjectURL: (u) => revoked.push(u) },
      Audio: FakeAudio,
      document: { querySelectorAll: () => [] },
    });
    vm.runInContext(TTS_CODE, ctx);
    const a1 = ctx._ttsAudioFromBlob({});
    a1.listeners.ended();
    assert.strictEqual(revoked.length, 1, 'ended event must revoke object URL');
    ctx._ttsAudio = ctx._ttsAudioFromBlob({});
    ctx._voiceStopAudio();
    assert.strictEqual(revoked.length, 2, '_voiceStopAudio must revoke active object URL');
  }

  // (D) SSE checks res.ok, flushes EOF buffer, journal tail releases UI via _finalizeStreamUi
  {
    const events = [];
    let sendBtn = { disabled: true };
    const ctx = vm.createContext({
      S: { streaming: true, currentRunId: 'r1', mode: 'direct', chatAutosave: false },
      _vramPending: false,
      _vramOpen: false,
      window: {},
      sessionStorage: { removeItem: () => {} },
      handleEvent: (ev) => events.push(ev),
      _toolGenDone: () => {},
      _syncSpeakButtons: () => {},
      _cleanupLoadTimers: () => {},
      _updateSendState: () => {},
      setPauseBtnState: () => {},
      setStopBtnState: () => {},
      stopAskUserCountdown: () => {},
      _drainFollowUpQueue: () => {},
      rmEl: () => {},
      loadMemory: () => {},
      console, setTimeout, clearTimeout, clearInterval,
      document: {
        hidden: false,
        getElementById: (id) => id === 'send' ? sendBtn : { className: '', style: {}, classList: { remove: () => {} } },
        querySelectorAll: () => [],
      },
    });
    vm.runInContext(SSE_TAIL_CODE, ctx);
    ctx._sseBuf = '';
    ctx._sseFeedLines('data: {"type":"token","content":"hi"}');
    assert.strictEqual(events.length, 0, 'unterminated line stays buffered');
    if (ctx.S._sseBuf && ctx.S._sseBuf.indexOf('data:') === 0) ctx._sseFeedLines('\\n');
    assert.strictEqual(events.length, 1, 'EOF flush emits buffered trailing data event');
    ctx.S._journalTailing = 'r1';
    ctx._journalStopTail();
    assert.strictEqual(ctx.S.streaming, false, '_journalStopTail releases S.streaming');
    assert.strictEqual(sendBtn.disabled, false, '_journalStopTail re-enables #send button');
  }

  console.log('JS_OK');
})().catch((e) => { console.error(e); process.exit(1); });
"""
    env_payload = json.dumps({
        "SETTINGS_CODE": settings_slice,
        "TTS_CODE": tts_slice,
        "NEW_CHAT_CODE": new_chat_fn,
        "LOAD_CHAT_CODE": load_chat_fn,
        "SSE_TAIL_CODE": sse_tail_slice,
    })
    runner_js = (
        f"const _P = {env_payload};\n"
        + probe.replace("SETTINGS_CODE", "_P.SETTINGS_CODE")
               .replace("TTS_CODE", "_P.TTS_CODE")
               .replace("NEW_CHAT_CODE", "_P.NEW_CHAT_CODE")
               .replace("LOAD_CHAT_CODE", "_P.LOAD_CHAT_CODE")
               .replace("SSE_TAIL_CODE", "_P.SSE_TAIL_CODE")
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(runner_js)
        tf_path = Path(tf.name)
    try:
        proc = subprocess.run([node, str(tf_path)], capture_output=True, text=True, timeout=15)
        check("app.js Node VM behavioral tests (postSettings, chat epoch, TTS revoke, EOF/journal)",
              proc.returncode == 0 and "JS_OK" in proc.stdout,
              f"rc={proc.returncode} out={proc.stdout.strip()} err={proc.stderr.strip()}")
    finally:
        tf_path.unlink(missing_ok=True)

    check("sendMsg checks res.ok before res.body.getReader() and guards finally with !S._journalTailing",
          "if (!res.ok)" in send_slice
          and "var _tail = dec.decode();" in send_slice
          and "if (!S._journalTailing) {\n      _finalizeStreamUi();" in js)


if __name__ == "__main__":
    test_preflight_hb_and_duo_reconnect()
    test_tool_loop_pairing_and_overflow()
    test_tool_executor_pairing_and_deadline()
    test_git_squash_prechecks_before_reset()
    test_file_ops_late_capture_and_lsp_format()
    test_install_package_powershell_quoting()
    test_planner_honors_aborted_fn()
    test_vision_preprocess_lock_and_status()
    test_frontend_wave2b1_js_behavior()
    sys.exit(h.finish("audit_wave2b1_core_ui_tools"))
