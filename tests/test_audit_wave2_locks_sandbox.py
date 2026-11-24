# -*- coding: utf-8 -*-
"""Wave 2 Workstream B1 regression suite: approvals, sandbox, containment & supervisor.

Covers:
  1. C5a/b: core/agentic_tool_loop.py clears staged pre-decisions on drift/truncation
     and binds staged cards/pre-decisions to canonical args hash.
  2. C13: tools/sandbox.py SetInformationJobObject argtypes + _int_setting(0);
     tools/handlers/exec_tools.py closes ToolJob in finally, forwards cwd to
     start_background, and blocks Windows `rm` on workspace root.
  3. C7/C8: tools/handlers/linting.py uses tabular exec argvs + scrubbed_env().
  4. C7/C8: hive_functions/test_runner.py uses create_subprocess_exec for built-in
     runners (preserving file.py::node_id) and keeps shell for custom commands.
  5. C7/C8: tools/handlers/code_intel.py enforces resolved containment on find_files.
  6. C7/C8: tools/browser.py deduplicates IP blocks, protects secrets/symlinks in
     file:// navigation and _QuietFileHandler, and fails closed in _apply_route_guard.
  7. C7/C8: infra/mcp_server.py validates/quotes memory keys, isolates
     _default_workspace, protects _read_resource against symlinks/secrets, and
     passes scrubbed_env() in tool_run_tests and exec helpers.
  8. C7/C8: infra/gateway_supervisor.py + server.py /gateway/{heartbeat,start,stop}
     enforce loopback client host, block forged status keys, validate pid/birth on
     stop, and run non-NT sh probe via asyncio.to_thread(..., timeout=20).
"""
from __future__ import annotations

import asyncio
from ctypes import wintypes as _wt
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check


# ── Helpers for C5a/b tests ──────────────────────────────────────────────────
class _FakeSSEResp:
    def __init__(self, chunks: list[dict], status_code: int = 200):
        self._chunks = chunks
        self.status_code = status_code
        self.text = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeAsyncClient:
    def __init__(self, handler):
        self._handler = handler

    def stream(self, method, url, json=None, timeout=None):
        return self._handler(method, url, json, timeout)


class _FakeCtx:
    pass


def _make_loop(chunks_or_fn):
    from core.agentic_duo_state import DuoRoundState
    from core.agentic_tool_loop import AgenticToolLoop
    from core.tool_loop import ToolLoopConfig

    emitted: list[dict] = []

    async def _emit(ev):
        emitted.append(ev)

    rs = DuoRoundState()
    rs.current_port = 8101
    rs.exec_model = "test-coder"
    cfg = ToolLoopConfig(model="test-coder", max_post_attempts=2)
    client = _FakeAsyncClient(lambda *a, **kw: _FakeSSEResp([]))
    loop = AgenticToolLoop(cfg, client, round_state=rs, workspace=".", run_id="c5-run", emit_fn=_emit)
    return loop, emitted


def _run_c5_stream(chunks: list[dict], *, on_chunk=None, on_chunks_done=None, raise_exc=None):
    import core.agentic_tool_loop as atl
    import tools.runner as tr
    from core.state import settings as st_settings

    saved_gate = st_settings.get("duo_action_approval_enabled", False)
    st_settings["duo_action_approval_enabled"] = True
    tok_run = tr._current_run_id.set("c5-run")
    tr._clear_staged_approval("c5-run")
    tr._approval_memory.clear()
    tr._approval_once_paths.clear()
    tr._approval_free_pass.clear()
    tr._approval_expired.clear()

    async def _fake_iter_sse(_resp):
        for idx, ch in enumerate(chunks):
            yield ch
            if on_chunk is not None:
                on_chunk(idx, ch)
        if on_chunks_done is not None:
            on_chunks_done()
        if raise_exc is not None:
            raise raise_exc

    orig_iter = atl._t_iter_sse
    orig_port_lock = atl._t_port_lock

    class _NoopLock:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return False

    atl._t_iter_sse = _fake_iter_sse
    atl._t_port_lock = lambda _p: _NoopLock()
    try:
        loop, emitted = _make_loop(chunks)
        payload = {"model": "test-coder", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        dtool_msgs = list(payload["messages"])
        if raise_exc is not None:
            loop.cfg.max_post_attempts = 1
        res = asyncio.run(loop.post_with_retry(payload, dtool_msgs=dtool_msgs, ctx=_FakeCtx(), _parts=[]))
        return res, emitted, dict(tr._approval_pre_decisions), dict(tr._card_staged), dict(tr._pending_approvals)
    finally:
        atl._t_iter_sse = orig_iter
        atl._t_port_lock = orig_port_lock
        st_settings["duo_action_approval_enabled"] = saved_gate
        tr._clear_staged_approval("c5-run")
        tr._current_run_id.reset(tok_run)


# ── 1. C5a/b: agentic_tool_loop drift / truncation / canonical hash binding ──
def test_c5_approval_staging() -> None:
    import httpx
    import tools.runner as tr

    # 1a: stream drift clears staged pre-decision, card_staged, pending_approvals
    # Raw JSON-escaped stream carries literal "\n" separators; >=1024 chars & >=30 identical lines triggers drift
    drift_line = "repeated line of content that drifts endlessly\\n"
    drift_chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "write_file", "arguments": '{"path":"a.py","content":"'}}]}}]},
    ] + [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": drift_line}}]}}]}
        for _ in range(36)
    ]

    def _inject_pre_on_0(idx, _ch):
        if idx == 0:
            tr._approval_pre_decisions["c5-run"] = {"answer": "1", "note": "", "tool": "write_file"}

    res_d, _em_d, pre_d, staged_d, pend_d = _run_c5_stream(drift_chunks, on_chunk=_inject_pre_on_0)
    check("1a drift sets finish_reason=drift and clears staged pre-decision",
          res_d.get("dr_finish_reason") == "drift"
          and "c5-run" not in pre_d
          and "c5-run" not in staged_d
          and "c5-run" not in pend_d,
          f"fr={res_d.get('dr_finish_reason')} pre={pre_d} staged={staged_d}")

    # 1b: finish_reason=length clears staged pre-decision
    len_chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "write_file", "arguments": '{"path":"a.py","content":"abc'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    ]
    res_l, _em_l, pre_l, staged_l, pend_l = _run_c5_stream(len_chunks, on_chunk=_inject_pre_on_0)
    check("1b finish_reason=length clears staged pre-decision",
          res_l.get("dr_finish_reason") == "length"
          and "c5-run" not in pre_l
          and "c5-run" not in staged_l
          and "c5-run" not in pend_l)

    # 1c: malformed/truncated JSON args even without finish_reason=length clears staged pre-decision
    bad_json_chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "run_bash", "arguments": '{"cmd": "echo hi'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]

    def _inject_bash_pre(idx, _ch):
        if idx == 0:
            tr._approval_pre_decisions["c5-run"] = {"answer": "1", "note": "", "tool": "run_bash"}

    _res_b, _em_b, pre_b, staged_b, pend_b = _run_c5_stream(bad_json_chunks, on_chunk=_inject_bash_pre)
    check("1c truncated/malformed JSON args clears staged pre-decision",
          "c5-run" not in pre_b and "c5-run" not in staged_b and "c5-run" not in pend_b)

    # 1d: unbound early approve ("1") while args still streaming is dropped (never retroactively bound),
    # whereas early deny ("3") or a bound pre-decision binds to canonical args hash and rejects mutated args.
    clean_chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "run_bash", "arguments": '{"cmd":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "echo safe"}'}}]}}],
         "finish_reason": None},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    _res_ok, em_ok, pre_ok, staged_ok, pend_ok = _run_c5_stream(clean_chunks, on_chunk=_inject_bash_pre)
    expected_hash = tr._canonical_args_hash("run_bash", {"cmd": "echo safe"})
    expected_key = tr._canonical_approval_key("run_bash", {"cmd": "echo safe"})
    check("1d1 unbound early approve dropped while staged card binds canonical hash",
          "c5-run" not in pre_ok
          and staged_ok.get("c5-run", {}).get("args_hash") == expected_hash
          and staged_ok.get("c5-run", {}).get("approval_key") == expected_key,
          f"staged={staged_ok} pre={pre_ok}")

    def _inject_bash_deny_or_bound(idx, _ch):
        if idx == 0:
            tr._approval_pre_decisions["c5-run"] = {"answer": "0|check note", "note": "check note", "tool": "run_bash"}

    _res_d2, _em_d2, pre_d2, staged_d2, _pend_d2 = _run_c5_stream(clean_chunks, on_chunk=_inject_bash_deny_or_bound)
    check("1d2 non-approve early pre-decision binds canonical hash and _pre_decision_matches rejects mutated args",
          pre_d2.get("c5-run", {}).get("args_hash") == expected_hash
          and pre_d2.get("c5-run", {}).get("approval_key") == expected_key
          and tr._pre_decision_matches("run_bash", expected_hash, expected_key, pre_d2["c5-run"], staged_d2["c5-run"])
          and not tr._pre_decision_matches(
              "run_bash",
              tr._canonical_args_hash("run_bash", {"cmd": "rm -rf /tmp/evil"}),
              tr._canonical_approval_key("run_bash", {"cmd": "rm -rf /tmp/evil"}),
              pre_d2["c5-run"],
              staged_d2["c5-run"],
          ),
          f"staged={staged_d2} pre={pre_d2}")

    # 1e: without pre-decision, pending card is refreshed with real preview & approval_key
    _res_p, em_p, _pre_p, staged_p, pend_p = _run_c5_stream(clean_chunks)
    appr_evs = [e for e in em_p if e.get("type") == "approval_request"]
    check("1e pending card refreshed with real preview, approval_key, and args_hash",
          len(appr_evs) >= 2
          and appr_evs[-1].get("approval_key") == expected_key
          and "echo safe" in str(appr_evs[-1].get("preview", ""))
          and pend_p.get("c5-run", {}).get("args_hash") == expected_hash
          and staged_p.get("c5-run", {}).get("args_hash") == expected_hash,
          f"evs={appr_evs}")

    # 1f: ReadTimeout mid-stream clears staged approval before raising
    timeout_raised = False
    try:
        _run_c5_stream(
            [clean_chunks[0]],
            on_chunk=_inject_bash_pre,
            raise_exc=httpx.ReadTimeout("stream dropped"),
        )
    except httpx.ReadTimeout:
        timeout_raised = True
    check("1f ReadTimeout mid-stream clears staged approval",
          timeout_raised
          and "c5-run" not in tr._approval_pre_decisions
          and "c5-run" not in tr._card_staged
          and "c5-run" not in tr._pending_approvals)


# ── 2. C13: sandbox.py + background + exec_tools.py ──────────────────────────
def test_c13_sandbox_and_exec() -> None:
    import tools.sandbox as sb
    import tools.background as bg
    import tools.handlers.exec_tools as et
    from core.state import settings as st_settings
    import settings as settings_mod

    # 2a: SetInformationJobObject restype & argtypes on Windows
    if sys.platform == "win32":
        k = sb._k32()
        check("2a SetInformationJobObject restype/argtypes configured",
              k.SetInformationJobObject.restype == _wt.BOOL
              and list(k.SetInformationJobObject.argtypes) == [_wt.HANDLE, _wt.INT, _wt.LPVOID, _wt.DWORD])
    else:
        sb_src = (ROOT / "tools" / "sandbox.py").read_text(encoding="utf-8")
        check("2a SetInformationJobObject restype/argtypes present in source",
              "SetInformationJobObject.restype = _wt.BOOL" in sb_src
              and "SetInformationJobObject.argtypes = [_wt.HANDLE, _wt.INT, _wt.LPVOID, _wt.DWORD]" in sb_src)

    # 2b: _int_setting returns explicit 0 instead of default
    with mock.patch.object(settings_mod, "load_settings", return_value={"duo_tool_sandbox_max_mem_mb": 0, "duo_tool_sandbox_max_procs": "0"}):
        check("2b _int_setting preserves explicit 0",
              sb._int_setting("duo_tool_sandbox_max_mem_mb", 4096) == 0
              and sb._int_setting("duo_tool_sandbox_max_procs", 64) == 0)
    with mock.patch.object(settings_mod, "load_settings", return_value={"duo_tool_sandbox_max_mem_mb": None, "empty": ""}):
        check("2b2 _int_setting falls back to default for None/missing/empty",
              sb._int_setting("duo_tool_sandbox_max_mem_mb", 4096) == 4096
              and sb._int_setting("missing_key", 64) == 64
              and sb._int_setting("empty", 12) == 12)

    # 2c: run_python closes ToolJob in finally on normal exit as well as errors
    closed_jobs: list[bool] = []

    class _FakeJob:
        def terminate(self):
            pass

        def close(self):
            closed_jobs.append(True)

    with tempfile.TemporaryDirectory(prefix="hm_b1_py_") as td:
        ws = Path(td)
        with mock.patch.object(sb.ToolJob, "confine", return_value=_FakeJob()):
            out_ok = asyncio.run(et._inline_tool_run_python({"code": "print('ok_job')"}, ws, str(ws)))
            out_err = asyncio.run(et._inline_tool_run_python({"code": "import sys; sys.exit(2)"}, ws, str(ws)))
    check("2c run_python closes ToolJob in finally on normal and error exit",
          "ok_job" in out_ok and "exited with code 2" in out_err and len(closed_jobs) == 2,
          f"closed={len(closed_jobs)} out_ok={out_ok!r}")

    # 2d: _inline_tool_start_background passes cwd=workspace and start_background forwards cwd + scrubbed env
    with tempfile.TemporaryDirectory(prefix="hm_b1_bg_") as td:
        ws = Path(td)
        captured_calls: list[dict] = []

        def _fake_start_bg(cmd, cwd=None):
            captured_calls.append({"cmd": cmd, "cwd": cwd})
            return {"ok": True, "handle": "abc123def456", "pid": 9999}

        with mock.patch.object(bg, "start_background", side_effect=_fake_start_bg):
            bg_res = asyncio.run(et._inline_tool_start_background({"cmd": "python -m http.server"}, ws, str(ws)))
        check("2d1 _inline_tool_start_background forwards cwd=str(workspace)",
              "[background started]" in bg_res
              and captured_calls == [{"cmd": "python -m http.server", "cwd": str(ws)}],
              f"calls={captured_calls} res={bg_res!r}")

        # Verify start_background itself passes cwd and scrubbed env to Popen
        popen_args: list[dict] = []

        class _FakeProc:
            pid = 4321
            stdout = iter([])

            def poll(self):
                return 0

        def _fake_popen(cmd, **kw):
            popen_args.append({"cmd": cmd, **kw})
            return _FakeProc()

        with mock.patch.dict(os.environ, {"HIVEMIND_INTERNAL_TOKEN": "topsecret-tok"}, clear=False), \
                mock.patch.object(bg.subprocess, "Popen", side_effect=_fake_popen), \
                mock.patch.object(sb.ToolJob, "confine", return_value=None):
            res_bg2 = bg.start_background("echo hello", cwd=ws)
            if res_bg2.get("ok"):
                bg.stop_background(res_bg2["handle"])
        check("2d2 tools/background.start_background passes cwd and scrubbed env to Popen",
              res_bg2.get("ok") is True
              and popen_args
              and popen_args[-1].get("cwd") == str(ws)
              and "HIVEMIND_INTERNAL_TOKEN" not in (popen_args[-1].get("env") or {}),
              f"popen={popen_args}")

    # 2e: Windows rm guard protects workspace root
    with tempfile.TemporaryDirectory(prefix="hm_b1_rm_") as td:
        ws = Path(td) / "ws"
        ws.mkdir()
        keep = ws / "keep.txt"
        keep.write_text("important", encoding="utf-8")
        sub = ws / "sub"
        sub.mkdir()
        (sub / "temp.txt").write_text("bye", encoding="utf-8")

        orig_plat = et.sys.platform
        et.sys.platform = "win32"
        try:
            for bad_rm in ("rm -r .", "rm -r ./", "rm -r -f .", "rm -r sub/..", f'rm -r "{ws}"'):
                rm_res = asyncio.run(et._inline_tool_run_bash({"cmd": bad_rm}, ws, str(ws)))
                check(f"2e Windows rm refuses workspace root ({bad_rm})",
                      ws.exists() and keep.exists() and ("RUN_BASH_BLOCKED" in rm_res or "rm error" in rm_res),
                      f"res={rm_res!r}")
            ok_rm = asyncio.run(et._inline_tool_run_bash({"cmd": "rm -r sub"}, ws, str(ws)))
            check("2e2 Windows rm still removes subdirectory inside workspace",
                  not sub.exists() and ws.exists() and keep.exists() and "removed:" in ok_rm,
                  f"res={ok_rm!r}")
        finally:
            et.sys.platform = orig_plat
    _ = st_settings  # referenced cleanly


# ── 3. C7/C8: tools/handlers/linting.py ──────────────────────────────────────
def test_c7_linting() -> None:
    import tools.handlers.linting as lint

    with tempfile.TemporaryDirectory(prefix="hm_b1_lint_") as td:
        ws = Path(td)
        evil_py = ws / "safe'; Remove-Item evil'.py"
        evil_py.write_text("x = 1\n", encoding="utf-8")
        exec_calls: list[dict] = []

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def _fake_exec(*argv, **kw):
            exec_calls.append({"argv": list(argv), "kw": kw})
            return _FakeProc()

        async def _forbidden_shell(*a, **kw):
            raise AssertionError("create_subprocess_shell must never be called by linting")

        with mock.patch.dict(os.environ, {"HIVEMIND_INTERNAL_TOKEN": "sec1", "OPENAI_API_KEY": "sk-sec"}, clear=False), \
                mock.patch.object(lint, "_resolve_pyright_cmd", return_value=None), \
                mock.patch.object(lint.asyncio, "create_subprocess_exec", side_effect=_fake_exec), \
                mock.patch.object(lint.asyncio, "create_subprocess_shell", side_effect=_forbidden_shell):
            res_py = asyncio.run(lint._auto_lint_result(evil_py, ws))

        check("3a python py_compile uses create_subprocess_exec + scrubbed_env (no shell)",
              res_py == ""
              and len(exec_calls) == 1
              and exec_calls[0]["argv"][1:4] == ["-m", "py_compile", str(evil_py)]
              and "HIVEMIND_INTERNAL_TOKEN" not in exec_calls[0]["kw"].get("env", {})
              and "OPENAI_API_KEY" not in exec_calls[0]["kw"].get("env", {}),
              f"calls={exec_calls}")

        exec_calls.clear()
        evil_js = ws / "evil$(whoami).js"
        evil_js.write_text("const a = 1;\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HIVEMIND_INTERNAL_TOKEN": "sec1"}, clear=False), \
                mock.patch.object(lint.asyncio, "create_subprocess_exec", side_effect=_fake_exec), \
                mock.patch.object(lint.asyncio, "create_subprocess_shell", side_effect=_forbidden_shell):
            res_js = asyncio.run(lint._auto_lint_result(evil_js, ws))
        check("3b js node --check uses create_subprocess_exec + scrubbed_env",
              res_js == ""
              and len(exec_calls) == 1
              and exec_calls[0]["argv"][1:] == ["--check", str(evil_js)]
              and "HIVEMIND_INTERNAL_TOKEN" not in exec_calls[0]["kw"].get("env", {}),
              f"calls={exec_calls}")

        exec_calls.clear()
        with mock.patch.dict(os.environ, {"HIVEMIND_INTERNAL_TOKEN": "sec1"}, clear=False), \
                mock.patch.object(lint.asyncio, "create_subprocess_exec", side_effect=_fake_exec):
            asyncio.run(lint._pyright_lint_result(["pyright"], evil_py, ws))
        check("3c _pyright_lint_result passes scrubbed_env",
              len(exec_calls) == 1
              and "HIVEMIND_INTERNAL_TOKEN" not in exec_calls[0]["kw"].get("env", {}))


# ── 4. C7/C8: hive_functions/test_runner.py ──────────────────────────────────
def test_c7_test_runner() -> None:
    import hive_functions.test_runner as tr_mod

    with tempfile.TemporaryDirectory(prefix="hm_b1_tr_") as td:
        ws = Path(td)
        (ws / "tests").mkdir()
        (ws / "tests" / "test_a.py").write_text("def test_fn():\n    assert True\n", encoding="utf-8")
        (ws / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

        # 4a: pytest node_id splitting & containment
        check("4a1 _quote_path preserves file.py::node_id",
              tr_mod._quote_path("tests/test_a.py::test_fn", str(ws)) == "tests/test_a.py::test_fn"
              and tr_mod._quote_path("tests/test_a.py::Cls::test_m", str(ws)) == "tests/test_a.py::Cls::test_m")
        rejected_node = False
        try:
            tr_mod._quote_path("../outside.py::test_fn", str(ws))
        except ValueError:
            rejected_node = True
        check("4a2 _quote_path rejects traversal with ::node_id", rejected_node)

        # 4b: built-in pytest runner uses create_subprocess_exec with ::node_id preserved
        exec_calls: list[list[str]] = []
        shell_calls: list[str] = []

        class _FakeTestProc:
            returncode = 0

            async def communicate(self):
                return b"1 passed\n", b""

        async def _fake_exec(*argv, **kw):
            exec_calls.append(list(argv))
            return _FakeTestProc()

        async def _fake_shell(cmd, **kw):
            shell_calls.append(cmd)
            return _FakeTestProc()

        with mock.patch.object(tr_mod.asyncio, "create_subprocess_exec", side_effect=_fake_exec), \
                mock.patch.object(tr_mod.asyncio, "create_subprocess_shell", side_effect=_fake_shell):
            res = asyncio.run(tr_mod.run_tests(str(ws), paths=["tests/test_a.py::test_fn"]))
        check("4b built-in pytest runner uses create_subprocess_exec and preserves ::node_id",
              res.is_clean()
              and not shell_calls
              and len(exec_calls) == 1
              and exec_calls[0][1:] == ["-m", "pytest", "-x", "-q", "tests/test_a.py::test_fn"],
              f"exec={exec_calls} shell={shell_calls}")

        # 4c: custom shell test_cmd continues to use create_subprocess_shell
        exec_calls.clear()
        shell_calls.clear()
        orig_py_cmd = tr_mod.LANGUAGE_RUNNERS["python"]["test_cmd"]
        try:
            tr_mod.LANGUAGE_RUNNERS["python"]["test_cmd"] = "echo custom && pytest -q"
            with mock.patch.object(tr_mod.asyncio, "create_subprocess_exec", side_effect=_fake_exec), \
                    mock.patch.object(tr_mod.asyncio, "create_subprocess_shell", side_effect=_fake_shell):
                res_custom = asyncio.run(tr_mod.run_tests(str(ws)))
            check("4c custom shell test_cmd still uses create_subprocess_shell",
                  res_custom.is_clean()
                  and not exec_calls
                  and shell_calls == ["echo custom && pytest -q"],
                  f"exec={exec_calls} shell={shell_calls}")
        finally:
            tr_mod.LANGUAGE_RUNNERS["python"]["test_cmd"] = orig_py_cmd

        # 4d: workspace .venv Python wins over sys.executable in _build_builtin_test_argv
        ws_venv_py = (
            ws / ".venv" / "Scripts" / "python.exe"
            if sys.platform == "win32"
            else ws / ".venv" / "bin" / "python"
        )
        ws_venv_py.parent.mkdir(parents=True, exist_ok=True)
        ws_venv_py.write_text("", encoding="utf-8")
        argv_venv = tr_mod._build_builtin_test_argv("python", "python -m pytest -x -q 2>&1", None, str(ws))
        check("4d _build_builtin_test_argv prefers workspace .venv over sys.executable",
              argv_venv is not None and argv_venv[0] == str(ws_venv_py),
              f"argv={argv_venv}")
        shutil.rmtree(ws / ".venv", ignore_errors=True)

        # 4e: no-tests-ran output must NOT emit contradictory "0 test failure(s)" / "Fix the above failures"
        no_test_msg = tr_mod._build_inject_message(
            success=False, lang="python", cmd="python -m pytest -x -q 2>&1",
            failure_count=0, error_lines=[], raw="no tests ran in 0.00s\n",
        )
        check("4e no-tests inject message is clean without contradictory failure footer",
              "No tests found" in no_test_msg
              and "0 test failure(s)" not in no_test_msg
              and "Fix the above failures" not in no_test_msg,
              f"msg={no_test_msg!r}")

        # 4f: run_bash rejects empty cmd cleanly instead of silent exit 0
        from tools.handlers.exec_tools import _inline_tool_run_bash
        empty_res = asyncio.run(_inline_tool_run_bash({}, ws, str(ws)))
        check("4f run_bash rejects empty cmd with INVALID_ARGUMENT",
              "[TOOL_ERROR:INVALID_ARGUMENT]" in empty_res, f"res={empty_res!r}")

        # 4g: _stream_proc handles >64KB single-line output without LimitOverrunError
        long_res = asyncio.run(_inline_tool_run_bash(
            {"cmd": 'python -c "print(\'x\' * 100000)"'}, ws, str(ws)))
        check("4g run_bash handles >64KB single line without crashing",
              "[run_bash: exit 0]" in long_res and "RUN_BASH_EXEC_ERROR" not in long_res,
              f"res={long_res[:160]!r}")

        # 4h: python3 in run_bash resolves to the same active Python interpreter as python
        py_res = asyncio.run(_inline_tool_run_bash(
            {"cmd": 'python -c "import sys; print(sys.executable)"'}, ws, str(ws)))
        py3_res = asyncio.run(_inline_tool_run_bash(
            {"cmd": 'python3 -c "import sys; print(sys.executable)"'}, ws, str(ws)))
        check("4h run_bash python3 matches active python interpreter",
              "[run_bash: exit 0]" in py_res
              and "[run_bash: exit 0]" in py3_res
              and py_res.strip().splitlines()[-1].lower() == py3_res.strip().splitlines()[-1].lower(),
              f"py={py_res!r} py3={py3_res!r}")

        # 4i: run_bash passes stdin=DEVNULL so stdin.read() gets instant EOF instead of hanging
        stdin_res = asyncio.run(_inline_tool_run_bash(
            {"cmd": 'python -c "import sys; print(repr(sys.stdin.read()))"', "timeout": 10}, ws, str(ws)))
        check("4i run_bash gives immediate EOF on stdin",
              "[run_bash: exit 0]" in stdin_res and "''" in stdin_res,
              f"res={stdin_res!r}")

        # 4j: cd subdir && preserves subdir cwd (never stripped to workspace root)
        sub = ws / "subproj"
        sub.mkdir()
        (sub / "marker.txt").write_text("from_sub", encoding="utf-8")
        cd_res = asyncio.run(_inline_tool_run_bash(
            {"cmd": 'cd subproj && python -c "import os; print(os.path.basename(os.getcwd()), os.path.exists(\'marker.txt\'))"'},
            ws, str(ws)))
        check("4j run_bash preserves cd subdir && working directory",
              "[run_bash: exit 0]" in cd_res and "subproj True" in cd_res,
              f"res={cd_res!r}")

        if sys.platform == "win32":
            import tools.handlers.exec_tools as _et_mod
            with mock.patch.object(_et_mod, "_windows_bash_path", return_value=""):
                cd_ps_res = asyncio.run(_inline_tool_run_bash(
                    {"cmd": 'cd subproj && python -c "import os; print(os.path.basename(os.getcwd()), os.path.exists(\'marker.txt\'))"'},
                    ws, str(ws)))
            check("4k PowerShell fallback preserves cd subdir && working directory",
                  "[run_bash: exit 0]" in cd_ps_res and "subproj True" in cd_ps_res,
                  f"res={cd_ps_res!r}")

        # 4l: RUN_BASH_NONZERO keeps final error line inside the 4000-char UI cap
        # and aligns the sliced tail to a clean line boundary
        nz_res = asyncio.run(_inline_tool_run_bash(
            {"cmd": 'python -c "import sys; [print(f\'noise_line_{i:04d}_\' + \'a\'*40, file=sys.stderr) for i in range(120)]; print(\'FINAL_ROOT_CAUSE_ERROR\', file=sys.stderr); sys.exit(1)"'},
            ws, str(ws)))
        check("4l RUN_BASH_NONZERO keeps final error line within 4000-char SSE cap and clean line start",
              "RUN_BASH_NONZERO" in nz_res
              and "FINAL_ROOT_CAUSE_ERROR" in nz_res[:4000]
              and "\n...\nnoise_line_" in nz_res,
              f"head={nz_res[:200]!r} has_end={'FINAL_ROOT_CAUSE_ERROR' in nz_res[:4000]}")


# ── 5. C7/C8: tools/handlers/code_intel.py find_files containment ────────────
def test_c7_code_intel_find_files() -> None:
    from tools.handlers.code_intel import _inline_tool_find_files

    with tempfile.TemporaryDirectory(prefix="hm_b1_ci_") as td:
        tmp = Path(td)
        ws = tmp / "ws"
        ws.mkdir()
        outside = tmp / "outside"
        outside.mkdir()
        (outside / "leak.txt").write_text("SECRET_OUTSIDE", encoding="utf-8")
        (ws / "ok.txt").write_text("ok", encoding="utf-8")
        (ws / ".env").write_text("KEY=1", encoding="utf-8")

        # Pattern traversal (../*) must never escape workspace
        out_trav = asyncio.run(_inline_tool_find_files({"pattern": "../*/*"}, ws, str(ws)))
        check("5a find_files blocks ../*/* pattern traversal",
              "leak.txt" not in out_trav, out_trav)

        # Junction or symlink inside workspace pointing outside must not leak files
        link_dir = ws / "_escape_link"
        link_created = False
        if sys.platform == "win32":
            r = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_dir), str(outside)],
                capture_output=True, text=True,
            )
            link_created = r.returncode == 0
        else:
            os.symlink(outside, link_dir)
            link_created = True

        if link_created:
            try:
                out_flat = asyncio.run(_inline_tool_find_files({"pattern": "*/*"}, ws, str(ws)))
                out_deep = asyncio.run(_inline_tool_find_files({"pattern": "**/*"}, ws, str(ws)))
                check("5b find_files blocks junction/symlink escape in glob and os.walk",
                      "leak.txt" not in out_flat and "leak.txt" not in out_deep,
                      f"flat={out_flat!r} deep={out_deep!r}")
            finally:
                if sys.platform == "win32":
                    subprocess.run(["cmd", "/c", "rmdir", str(link_dir)], capture_output=True)
                elif link_dir.is_symlink():
                    link_dir.unlink()

        out_ok = asyncio.run(_inline_tool_find_files({"pattern": "**/*"}, ws, str(ws)))
        check("5c find_files still lists normal workspace files and excludes .env",
              "ok.txt" in out_ok and ".env" not in out_ok, out_ok)


# ── 6. C7/C8: tools/browser.py hardening ─────────────────────────────────────
def test_c7_browser() -> None:
    import socket
    import tools.browser as B

    # 6a: _guard_browser_url IP literal + DNS resolution + unparseable IP fail-closed
    check("6a1 blocks CGNAT 100.64.0.1", bool(B._guard_browser_url("http://100.64.0.1/")))
    check("6a2 blocks 0.0.0.0", bool(B._guard_browser_url("http://0.0.0.0:8001/")))
    check("6a3 blocks ipv4-mapped loopback", bool(B._guard_browser_url("http://[::ffff:127.0.0.1]:8001/")))
    with mock.patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.1.2", 0))]):
        check("6a4 blocks DNS resolving to CGNAT", bool(B._guard_browser_url("http://rebind.example/")))
    with mock.patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 0))]):
        check("6a5 fails closed on unparseable getaddrinfo result",
              bool(B._guard_browser_url("http://weird.example/")))

    # 6b + 6c: _plan_file_navigation & _QuietFileHandler secret/symlink/listing protection
    with tempfile.TemporaryDirectory(prefix="hm_b1_br_") as td:
        tmp = Path(td)
        ws = tmp / "ws"
        outside = tmp / "outside"
        ws.mkdir()
        outside.mkdir()
        (ws / "app").mkdir()
        (ws / "app" / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
        (ws / "empty_dir").mkdir()
        (ws / ".env").write_text("SECRET=xyz", encoding="utf-8")
        (ws / "settings.json").write_text('{"tok":"xyz"}', encoding="utf-8")
        (ws / "sessions").mkdir()
        (ws / "sessions" / "c1.json").write_text('{"msg":"private"}', encoding="utf-8")
        (outside / "secret.html").write_text("outside", encoding="utf-8")

        http_url, err = B._plan_file_navigation((ws / "app" / "index.html").as_uri(), str(ws))
        check("6b1 normal workspace HTML maps cleanly", not err and http_url.endswith("/app/index.html"), err)

        for secret_p in (ws / ".env", ws / "settings.json", ws / "sessions" / "c1.json"):
            _, s_err = B._plan_file_navigation(secret_p.as_uri(), str(ws))
            check(f"6b2 _plan_file_navigation rejects secret {secret_p.name}", bool(s_err), s_err)

        junc = ws / "junc_dir"
        junc_ok = False
        if sys.platform == "win32":
            junc_ok = subprocess.run(["cmd", "/c", "mklink", "/J", str(junc), str(outside)], capture_output=True).returncode == 0
        else:
            os.symlink(outside, junc)
            junc_ok = True

        # Probe _QuietFileHandler directly over HTTP
        base_url = http_url.rsplit("/app/index.html", 1)[0]
        try:
            with urllib.request.urlopen(http_url, timeout=5) as r:
                check("6c1 _QuietFileHandler serves valid file", r.status == 200 and b"<h1>ok</h1>" in r.read())

            for blocked_path in ("/.env", "/settings.json", "/settings.json.", "/sessions/c1.json", "/empty_dir/"):
                got_code = 0
                try:
                    urllib.request.urlopen(base_url + blocked_path, timeout=5)
                except urllib.error.HTTPError as he:
                    got_code = he.code
                check(f"6c2 _QuietFileHandler blocks {blocked_path} with 403", got_code == 403, f"code={got_code}")

            if junc_ok:
                _, j_err = B._plan_file_navigation((junc / "secret.html").as_uri(), str(ws))
                check("6b3 _plan_file_navigation blocks junction/symlink", bool(j_err), j_err)
                j_code = 0
                try:
                    urllib.request.urlopen(base_url + "/junc_dir/secret.html", timeout=5)
                except urllib.error.HTTPError as he:
                    j_code = he.code
                check("6c3 _QuietFileHandler blocks junction/symlink with 403", j_code == 403, f"code={j_code}")
        finally:
            if junc_ok:
                if sys.platform == "win32":
                    subprocess.run(["cmd", "/c", "rmdir", str(junc)], capture_output=True)
                elif junc.is_symlink():
                    junc.unlink()
            B._stop_file_server()

    # 6d: _apply_route_guard fails closed on URL/resolution/guard errors
    actions: list[str] = []

    class _FakeRoute:
        def __init__(self, url):
            self.request = SimpleNamespace(url=url)

        def abort(self):
            actions.append("abort")

        def continue_(self):
            actions.append("continue")

    with mock.patch.object(B, "_guard_browser_url", side_effect=RuntimeError("boom")):
        B._apply_route_guard(_FakeRoute("https://example.com"))
    check("6d1 _apply_route_guard aborts when guard raises", actions == ["abort"], str(actions))

    actions.clear()
    B._apply_route_guard(_FakeRoute("http://127.0.0.1:8001/"))
    check("6d2 _apply_route_guard aborts blocked loopback URL", actions == ["abort"], str(actions))


# ── 7. C7/C8: infra/mcp_server.py hardening ──────────────────────────────────
def test_c7_mcp_server() -> None:
    import infra.mcp_server as mcp

    # 7a: tool_memory_set rejects invalid keys and quotes valid keys
    for bad_key in ("", "   ", "../shutdown", "a/b", "a\\b", ".", "..", "k?x=1", "k#frag"):
        out = asyncio.run(mcp.tool_memory_set(bad_key, "val"))
        check(f"7a1 memory_set rejects {bad_key!r}", "INVALID_ARGUMENT" in out, out[:80])

    posted_urls: list[str] = []

    class _FakeMemResp:
        status_code = 200

    class _FakeMemClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            posted_urls.append(url)
            return _FakeMemResp()

    with mock.patch.object(mcp.httpx, "AsyncClient", _FakeMemClient):
        ok_mem = asyncio.run(mcp.tool_memory_set("user pref", "dark"))
    check("7a2 memory_set URL-encodes valid keys",
          ok_mem.startswith("Saved:") and posted_urls and posted_urls[0].endswith("/memory/user%20pref"),
          f"urls={posted_urls}")

    # 7b: _default_workspace without HIVEMIND_WORKSPACE isolates to default_workspace (not install root)
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HIVEMIND_WORKSPACE", None)
        dw = mcp._default_workspace()
    check("7b _default_workspace avoids install root when HIVEMIND_WORKSPACE is unset",
          not (dw / "server.py").exists() and dw.name == "default_workspace",
          f"dw={dw}")

    # 7c: _read_resource containment, symlinks/junctions, and secret denylist
    orig_ws = mcp.WORKSPACE
    with tempfile.TemporaryDirectory(prefix="hm_b1_mcp_") as td:
        tmp = Path(td)
        ws = tmp / "ws"
        outside = tmp / "outside"
        ws.mkdir()
        outside.mkdir()
        (ws / "readme.txt").write_text("hello mcp", encoding="utf-8")
        (ws / "sessions").mkdir()
        (ws / "sessions" / "c1.json").write_text("private chat", encoding="utf-8")
        (outside / "ext.txt").write_text("outside secret", encoding="utf-8")
        mcp.WORKSPACE = ws

        check("7c1 _read_resource reads normal file",
              mcp._read_resource("hivemind://workspace/readme.txt")["text"] == "hello mcp")

        for label, bad_uri in (
            ("sessions transcript", "hivemind://workspace/sessions/c1.json"),
            ("remote file host", "file://evil-host/share/x.txt"),
            ("NUL byte", "hivemind://workspace/a\x00b.txt"),
        ):
            raised_err = False
            try:
                mcp._read_resource(bad_uri)
            except mcp._ResourceError:
                raised_err = True
            check(f"7c2 _read_resource blocks {label}", raised_err)

        junc = ws / "junc_out"
        junc_ok = False
        if sys.platform == "win32":
            junc_ok = subprocess.run(["cmd", "/c", "mklink", "/J", str(junc), str(outside)], capture_output=True).returncode == 0
        else:
            os.symlink(outside, junc)
            junc_ok = True
        if junc_ok:
            try:
                j_blocked = False
                try:
                    mcp._read_resource((junc / "ext.txt").as_uri())
                except mcp._ResourceError:
                    j_blocked = True
                check("7c3 _read_resource blocks symlink/junction escape", j_blocked)
            finally:
                if sys.platform == "win32":
                    subprocess.run(["cmd", "/c", "rmdir", str(junc)], capture_output=True)
                elif junc.is_symlink():
                    junc.unlink()

        # 7d: tool_run_tests and _run_git pass scrubbed_env()
        seen_envs: list[dict] = []

        class _FakeMCPProc:
            returncode = 0

            async def communicate(self):
                return b"ok\n", b""

        async def _fake_mcp_sub(*a, **kw):
            seen_envs.append(dict(kw.get("env") or {}))
            return _FakeMCPProc()

        with mock.patch.dict(os.environ, {"HIVEMIND_INTERNAL_TOKEN": "tok-mcp", "OPENAI_API_KEY": "sk-mcp"}, clear=False), \
                mock.patch.object(mcp.asyncio, "create_subprocess_exec", side_effect=_fake_mcp_sub), \
                mock.patch.object(mcp.asyncio, "create_subprocess_shell", side_effect=_fake_mcp_sub):
            asyncio.run(mcp.tool_run_tests(cmd="python -V", path="."))
            asyncio.run(mcp._run_git(["status"], ws))
        check("7d tool_run_tests and _run_git scrub secret env vars",
              len(seen_envs) == 2
              and all("HIVEMIND_INTERNAL_TOKEN" not in e and "OPENAI_API_KEY" not in e for e in seen_envs),
              f"envs={len(seen_envs)}")
    mcp.WORKSPACE = orig_ws


# ── 8. C7/C8: gateway_supervisor + server.py /gateway/* routes ───────────────
def test_c7_gateway_supervisor_and_routes() -> None:
    import infra.gateway_supervisor as sup
    import server as srv
    from starlette.responses import JSONResponse

    # 8a: forged data keys cannot overwrite running/pid/age_s
    sup.clear()
    try:
        sup.note_heartbeat(42, {"pid": 9999, "running": False, "age_s": 0.0, "paired": True}, now=1000.0)
        st = sup.status(now=1002.0)
        check("8a1 status prevents forged data keys from overwriting top-level fields",
              st["running"] is True and st["pid"] == 42 and st["age_s"] == 2.0 and st.get("paired") is True,
              f"st={st}")
        # Even direct mutation of _HB["data"] cannot spoof top-level fields
        sup._HB["data"]["pid"] = 777
        sup._HB["data"]["running"] = False
        st2 = sup.status(now=1002.0)
        check("8a2 status enforces authoritative top-level keys",
              st2["running"] is True and st2["pid"] == 42)
    finally:
        sup.clear()

    def _req(host: str, body: dict | None = None):
        async def _json():
            return body if body is not None else {}

        return SimpleNamespace(client=SimpleNamespace(host=host), json=_json)

    # 8b: loopback client-host enforcement on /gateway/{heartbeat,start,stop}
    try:
        for ep_name, fn, r_obj in (
            ("heartbeat", srv.gateway_heartbeat, _req("192.168.1.50", {"pid": 42})),
            ("start", srv.gateway_start, _req("192.168.1.50")),
            ("stop", srv.gateway_stop, _req("192.168.1.50")),
        ):
            resp = asyncio.run(fn(r_obj))
            check(f"8b /gateway/{ep_name} rejects non-loopback client with 403",
                  isinstance(resp, JSONResponse) and resp.status_code == 403)

        # 8c: /gateway/heartbeat validates pid > 0 and birth
        for bad_body, label in (
            ({"pid": 0}, "pid=0"),
            ({"pid": -5}, "pid=-5"),
            ({"pid": True}, "pid=True"),
            ({"pid": 123, "data": {"birth": "bad;calc"}}, "non-int birth"),
            ({"pid": 123, "data": {"birth": -1}}, "negative birth"),
        ):
            resp = asyncio.run(srv.gateway_heartbeat(_req("127.0.0.1", bad_body)))
            check(f"8c /gateway/heartbeat rejects {label} with 400",
                  isinstance(resp, JSONResponse) and resp.status_code == 400)

        # 8d: /gateway/stop validates pid > 0 and birth
        sup.clear()
        sup.note_heartbeat(0, {"birth": 123})
        resp_stop_nopid = asyncio.run(srv.gateway_stop(_req("127.0.0.1")))
        check("8d1 /gateway/stop rejects pid<=0 with 409",
              isinstance(resp_stop_nopid, JSONResponse) and resp_stop_nopid.status_code == 409)

        sup.note_heartbeat(999999, {"birth": "1; evil"})
        resp_stop_badbirth = asyncio.run(srv.gateway_stop(_req("127.0.0.1")))
        check("8d2 /gateway/stop rejects invalid birth with 400",
              isinstance(resp_stop_badbirth, JSONResponse) and resp_stop_badbirth.status_code == 400)

        # 8e: non-NT sh probe in gateway_start runs via asyncio.to_thread(..., timeout=20)
        sup.clear()
        sup.note_heartbeat(12345, {"birth": 100})
        to_thread_calls: list[dict] = []

        async def _fake_to_thread(fn, *args, **kwargs):
            to_thread_calls.append({"fn": fn, "args": args, "kwargs": kwargs})
            return SimpleNamespace(stdout="py\n", stderr="", returncode=0)

        with mock.patch.object(srv.os, "name", "posix"), \
                mock.patch.object(srv.asyncio, "to_thread", side_effect=_fake_to_thread):
            resp_start = asyncio.run(srv.gateway_start(_req("127.0.0.1")))
        check("8e non-NT sh probe runs via asyncio.to_thread with timeout=20 and returns 409",
              isinstance(resp_start, JSONResponse)
              and resp_start.status_code == 409
              and len(to_thread_calls) == 1
              and to_thread_calls[0]["kwargs"].get("timeout") == 20
              and to_thread_calls[0]["args"][0][0] == "sh",
              f"calls={to_thread_calls}")
    finally:
        sup.clear()


if __name__ == "__main__":
    test_c5_approval_staging()
    test_c13_sandbox_and_exec()
    test_c7_linting()
    test_c7_test_runner()
    test_c7_code_intel_find_files()
    test_c7_browser()
    test_c7_mcp_server()
    test_c7_gateway_supervisor_and_routes()
    sys.exit(h.finish("audit_wave2_locks_sandbox"))
