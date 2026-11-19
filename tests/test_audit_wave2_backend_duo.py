# -*- coding: utf-8 -*-
"""Wave 2 Workstream B4 standalone verification suite (C14 + C15).

Covers:
  1. Duo-runner cleanup ordering (`finally:` after final_test -> epilogue -> finalize)
     and cleanup guarantees on error, early return, and cancellation.
  2. Outer coder retry invalidating stale `_cached_coder_port`.
  3. Resolved workspace containment on `[Written: ...]` paths and critic reads.
  4. `_VRAMPreFlightError` cascade in `duo_coder_preloop` only setting `_duo_hard_stop`
     after the `ctx/2` cascade is exhausted.
  5. Shared `WRITE_TOOLS`, `WRITE_STATUSES` (incl. `"patched"`), and `_looks_like_stub`.
  6. Unified single `_vision_cfg` object across `vision.preprocess`, `core.state`,
     and `routers.vision`, keeping `_VISION_POISON_MARKERS` intact.
  7. Stale `_last_judge_verdict` reset in `core.chat_route_decision` and encoder
     planner label mapping + German stem matchers intact in `routing.decider`.
  8. Windows `_wrap_windows_shell` quoting hardening and `_LspConnection` / `LspManager`
     protections (`length < 0`, `_stdin_lock`, child reaping on `close`/`reap_idle`/`close_all`).
  9. `routers/soul.py` endpoint fixes (`/current`, `/learned` GET/POST/DELETE, `/evolve`, `/skills`).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


def _make_run_ctx(ws: Path, settings_dict: dict, events: list):
    from core.run_context import RunContext
    from core.duo_config import DuoConfig
    from infra.phase_timer import PhaseTimer

    class _Mem:
        def add_to_session(self, *a, **k):
            return None

        def get_session_messages(self, *a, **k):
            return []

        def __getattr__(self, _name):
            return lambda *a, **k: None

    class _Agents(dict):
        def __missing__(self, key):
            cfg = NS(model="", max_tokens=400, temperature=0.3,
                     thinking=False, thinking_budget=0)
            self[key] = cfg
            return cfg

    agents = _Agents()
    agents["duo_coder"] = NS(model="qwen3.5:4b", max_tokens=800, temperature=0.3,
                             thinking=False, thinking_budget=0)
    agents["duo_planner"] = NS(model="qwen3.5:4b", max_tokens=800, temperature=0.3,
                               thinking=False, thinking_budget=0)

    async def _emit(ev):
        events.append(ev)
        return ""

    return RunContext(
        user_input="Create hello.py that prints hello",
        complexity="code_duo",
        chat_id=None,
        run_id="wave2-b4-run",
        t_total=1000.0,
        workspace=str(ws),
        request_workspace="",
        workspace_commit_allowed=False,
        duo_config=DuoConfig(
            tool_rounds=3, use_pipeline=False, coding_mode=True,
            chunking=False, planner=False, pre_explore=False,
            agentic_mode=True, agentic_thinking=False, thinking_per_chunk=False,
            test_feedback_chunk=False, test_feedback_final=False,
            until_finished=False, runtime_profile="balanced",
            websearch_enabled=False, git_autocommit=False,
            evict_on_pause=False, pause_timeout_s=600,
            pass_explore_files="touched", parallel_preexplore=False,
        ),
        settings=settings_dict,
        memory=_Mem(),
        registry={"duo_coder": "qwen3.5:4b"},
        vram_budget=7.0,
        phase_timer=PhaseTimer(),
        exec_ctrl=NS(abort=lambda *a, **k: None,
                     reset_stuck_detection=lambda: None,
                     sync_tool_rounds=lambda *a, **k: None,
                     state=NS(value="idle"),
                     stop_reason=None,
                     verify_fail_streak=0),
        emit=_emit,
        aborted=lambda: False,
        is_aborted_chat=lambda *a: False,
        step_skipped=lambda: False,
        clear_step_skip=lambda: None,
        collect_done_metrics=lambda: {},
        done_event=lambda *a, **kw: {"type": "done",
                                     "elapsed": a[0] if a else 0,
                                     "stop_reason": a[1] if len(a) > 1 else "",
                                     **kw},
        registry_get=lambda *a: "qwen3.5:4b",
        get_num_ctx=lambda *a, **kw: 8192,
        model_profile=lambda *a, **kw: {"tool_call": True, "temperature": 0.3,
                                        "top_p": 0.95, "top_k": 20},
        get_effective_prompt_with_override=lambda *a, **kw: "",
        make_messages=lambda *a, **kw: [],
        pipeline=NS(ollama=NS(_client=None, chat_stream=None),
                    agents=agents, memory=None),
        websearch_available=False,
        resolve_duo_run_timeout_seconds=lambda *a: 300,
        resolve_duo_runtime_profile=lambda *a, **kw: "balanced",
    )


def _sse(*chunks: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


def test_duo_cleanup_order_and_retry():
    import httpx
    import backend.llama_server_manager as LSM
    import core.duo_runner as DR
    from settings import DEFAULT_SETTINGS
    from utils.workspace_resolve import _LAST_WS_FILE

    lwsf_existed = _LAST_WS_FILE.exists()
    lwsf_backup = _LAST_WS_FILE.read_bytes() if lwsf_existed else None

    orig_httpx = DR.httpx
    orig_mgr = getattr(LSM, "manager", None)
    orig_final_test = DR._duo_final_test
    orig_epilogue = DR._duo_session_epilogue
    orig_finalize = DR._duo_run_finalize
    orig_cleanup = DR._duo_final_cleanup
    orig_preloop = DR._duo_coder_preloop

    ws = Path(tempfile.mkdtemp(prefix="w2_b4_duo_"))
    try:
        # 1a. Normal completion ordering + error/early-return/cancel cleanup
        order: list[str] = []
        mode_flags = {"raise_epi": False, "stop_preloop": False}

        def _tc_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/chat/completions"):
                tc = {
                    "id": "c1", "type": "function", "index": 0,
                    "function": {"name": "task_complete",
                                 "arguments": json.dumps({"completed": True, "blockers": [],
                                                          "build_status": "passing"})},
                }
                return httpx.Response(200, content=_sse(
                    {"choices": [{"delta": {"tool_calls": [tc]}}]},
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                     "usage": {"completion_tokens": 10, "prompt_tokens": 100}},
                ))
            if request.url.path.endswith("/props"):
                return httpx.Response(200, json={
                    "default_generation_settings": {"n_ctx": 8192}, "model": "qwen3.5:4b"})
            return httpx.Response(200, json={})

        class _ShimClient(httpx.AsyncClient):
            def __init__(self, *a, **kw):
                kw.setdefault("transport", httpx.MockTransport(_tc_handler))
                super().__init__(*a, **kw)

        class _HttpxShim:
            AsyncClient = _ShimClient

            def __getattr__(self, name):
                return getattr(httpx, name)

        _slot = NS(port=18101, _num_ctx=8192, model="qwen3.5:4b", pinned=False,
                   is_running=True, _loading=False, slot_id=0, last_used=0.0,
                   mark_in_flight=lambda: None, clear_in_flight=lambda: None,
                   kill=lambda: None)

        class _FakeMgr:
            def __init__(self):
                self._slots = [_slot]
                self._pending_prefetch = []
                self._planner_critical_phase = False

            async def ensure_loaded(self, *a, **kw):
                return 18101

            def _find_loaded(self, model):
                return _slot

            def unpin_all(self):
                return 0

            async def evict(self, *a, **kw):
                return None

            def can_fit(self, *a, **k):
                return True

            def clear_pin(self, *a, **kw):
                return None

        DR.httpx = _HttpxShim()
        LSM.manager = _FakeMgr()

        async def _spy_final_test(*a, **kw):
            order.append("final_test")
            if False:
                yield {}

        async def _spy_epilogue(*a, **kw):
            order.append("session_epilogue")
            if mode_flags["raise_epi"]:
                raise RuntimeError("injected epilogue error")
            if False:
                yield {}

        async def _spy_finalize(*a, **kw):
            order.append("run_finalize")
            async for ev in orig_finalize(*a, **kw):
                yield ev

        async def _spy_cleanup(*a, **kw):
            order.append("final_cleanup")
            await orig_cleanup(*a, **kw)

        async def _spy_preloop(ctx, ps, **kw):
            if mode_flags["stop_preloop"]:
                ps.stop = True
                return
            async for ev in orig_preloop(ctx, ps, **kw):
                yield ev

        DR._duo_final_test = _spy_final_test
        DR._duo_session_epilogue = _spy_epilogue
        DR._duo_run_finalize = _spy_finalize
        DR._duo_final_cleanup = _spy_cleanup
        DR._duo_coder_preloop = _spy_preloop

        async def _drive_once():
            events: list = []
            st = dict(DEFAULT_SETTINGS)
            st.update({
                "agents": {"duo_coder": {"model": "qwen3.5:4b"}},
                "duo_coder_model": "qwen3.5:4b",
                "duo_runtime_profile": "balanced",
                "vram_budget_gb": 7.0,
                "workspace": str(ws),
            })
            ctx = _make_run_ctx(ws, st, events)
            async for ev in DR.run_code_duo(ctx):
                events.append(ev)
            return events

        asyncio.run(_drive_once())
        check("1a _duo_final_cleanup runs AFTER final_test -> epilogue -> finalize",
              order == ["final_test", "session_epilogue", "run_finalize", "final_cleanup"],
              f"(order={order})")

        order.clear()
        mode_flags["raise_epi"] = True
        caught = False
        try:
            asyncio.run(_drive_once())
        except RuntimeError:
            caught = True
        check("1b _duo_final_cleanup still runs when epilogue raises",
              caught and order == ["final_test", "session_epilogue", "final_cleanup"],
              f"(caught={caught}, order={order})")

        order.clear()
        mode_flags["raise_epi"] = False
        mode_flags["stop_preloop"] = True
        asyncio.run(_drive_once())
        check("1c _duo_final_cleanup runs on early return without running final_test/finalize",
              order == ["final_cleanup"], f"(order={order})")

        # Cancel path
        mode_flags["stop_preloop"] = False
        order.clear()

        async def _cancel_preloop(ctx, ps, **kw):
            if False:
                yield {}
            raise asyncio.CancelledError()

        DR._duo_coder_preloop = _cancel_preloop
        cancelled_ok = False
        try:
            asyncio.run(_drive_once())
        except asyncio.CancelledError:
            cancelled_ok = True
        check("1d _duo_final_cleanup still runs on CancelledError",
              cancelled_ok and order == ["final_cleanup"],
              f"(cancelled={cancelled_ok}, order={order})")

        # 2. Outer coder retry invalidates stale _cached_coder_port
        DR._duo_coder_preloop = orig_preloop
        DR._duo_final_test = orig_final_test
        DR._duo_session_epilogue = orig_epilogue
        DR._duo_run_finalize = orig_finalize
        DR._duo_final_cleanup = orig_cleanup

        seen_ports: list[int] = []
        ensure_calls = [0]
        evict_calls: list[str] = []

        class _RetryMgr(_FakeMgr):
            async def ensure_loaded(self, *a, **kw):
                ensure_calls[0] += 1
                port = 18101 if ensure_calls[0] <= 1 else 18102
                _slot.port = port
                return port

            async def evict(self, mdl, *a, **kw):
                evict_calls.append(mdl)
                return None

        def _retry_handler(request: httpx.Request) -> httpx.Response:
            port = request.url.port or 0
            if request.url.path.endswith("/chat/completions"):
                seen_ports.append(port)
                tc = {
                    "id": "c1", "type": "function", "index": 0,
                    "function": {"name": "task_complete",
                                 "arguments": json.dumps({"completed": True, "blockers": [],
                                                          "build_status": "passing"})},
                }
                return httpx.Response(200, content=_sse(
                    {"choices": [{"delta": {"tool_calls": [tc]}}]},
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                     "usage": {"completion_tokens": 10, "prompt_tokens": 100}},
                ))
            if request.url.path.endswith("/props"):
                return httpx.Response(200, json={
                    "default_generation_settings": {"n_ctx": 8192}, "model": "qwen3.5:4b"})
            return httpx.Response(200, json={})

        class _RetryClient(httpx.AsyncClient):
            def __init__(self, *a, **kw):
                kw.setdefault("transport", httpx.MockTransport(_retry_handler))
                super().__init__(*a, **kw)

        class _RetryHttpxShim:
            AsyncClient = _RetryClient

            def __getattr__(self, name):
                return getattr(httpx, name)

        LSM.manager = _RetryMgr()
        DR.httpx = _RetryHttpxShim()
        orig_post_wait = DR._duo_coder_post_wait
        orig_sleep = DR.asyncio.sleep
        post_wait_attempts = [0]

        async def _retry_post_wait(ctx_arg, wait_ps, **kw):
            post_wait_attempts[0] += 1
            seen_ports.append(int(kw.get("_dport") or 0))
            if post_wait_attempts[0] == 1:
                raise httpx.ConnectError("stale port 18101 dead")
            async for ev in orig_post_wait(ctx_arg, wait_ps, **kw):
                yield ev

        async def _fast_sleep(_sec):
            await orig_sleep(0 if float(_sec) >= 1.5 else _sec)

        DR._duo_coder_post_wait = _retry_post_wait
        DR.asyncio.sleep = _fast_sleep
        try:
            asyncio.run(asyncio.wait_for(_drive_once(), timeout=15.0))
        finally:
            DR._duo_coder_post_wait = orig_post_wait
            DR.asyncio.sleep = orig_sleep

        check("2 outer coder retry invalidates stale _cached_coder_port (18101 -> 18102)",
              len(seen_ports) >= 3
              and seen_ports[0] == 18101
              and all(p == 18102 for p in seen_ports[1:])
              and len(evict_calls) >= 1,
              f"(seen_ports={seen_ports}, evicts={evict_calls})")
    finally:
        DR.httpx = orig_httpx
        if orig_mgr is not None:
            LSM.manager = orig_mgr
        DR._duo_final_test = orig_final_test
        DR._duo_session_epilogue = orig_epilogue
        DR._duo_run_finalize = orig_finalize
        DR._duo_final_cleanup = orig_cleanup
        DR._duo_coder_preloop = orig_preloop
        shutil.rmtree(ws, ignore_errors=True)
        if lwsf_existed and lwsf_backup is not None:
            _LAST_WS_FILE.write_bytes(lwsf_backup)
        elif not lwsf_existed and _LAST_WS_FILE.exists():
            _LAST_WS_FILE.unlink()


def test_written_path_containment():
    from core.duo_runner import _resolve_contained_written_path

    tmp = Path(tempfile.mkdtemp(prefix="w2_b4_ws_"))
    try:
        ws = tmp / "ws"
        ws.mkdir()
        sub = ws / "pkg"
        sub.mkdir()
        inside = sub / "mod.py"
        inside.write_text("x = 1\n", encoding="utf-8")
        outside = tmp / "secret.txt"
        outside.write_text("TOP_SECRET\n", encoding="utf-8")

        check("3a relative workspace path resolves inside ws",
              _resolve_contained_written_path("pkg/mod.py", ws) == inside.resolve())
        check("3b absolute workspace path resolves inside ws",
              _resolve_contained_written_path(str(inside), ws) == inside.resolve())
        check("3c parent-directory traversal is rejected",
              _resolve_contained_written_path("../secret.txt", ws) is None)
        check("3d outside absolute path is rejected",
              _resolve_contained_written_path(str(outside), ws) is None)
        check("3e empty path and workspace root itself are rejected",
              _resolve_contained_written_path("", ws) is None
              and _resolve_contained_written_path(".", ws) is None)

        link_path = ws / "escape_link.txt"
        try:
            os.symlink(outside, link_path)
            symlink_tested = True
        except (OSError, NotImplementedError):
            symlink_tested = False
        if symlink_tested:
            check("3f escaping symlink inside workspace is rejected",
                  _resolve_contained_written_path("escape_link.txt", ws) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_vram_preflight_cascade_hard_stop():
    import backend.llama_server_manager as LSM
    from backend.llama_server_manager import VRAMPreFlightError
    import core.duo_coder_preloop as DCP
    from settings import DEFAULT_SETTINGS

    tmp = Path(tempfile.mkdtemp(prefix="w2_b4_vram_"))
    orig_mgr = getattr(LSM, "manager", None)
    orig_sleep = DCP.asyncio.sleep
    orig_resolve_ctx = DCP.resolve_ctx
    orig_pre_resume = DCP._pre_loop_resume
    orig_abort_preloop = DCP._abort_preloop
    try:
        async def _fast_sleep(_sec):
            await orig_sleep(0 if float(_sec) >= 1.5 else _sec)

        DCP.asyncio.sleep = _fast_sleep

        # Case A: attempt 1 raises VRAMPreFlightError at 32768, attempt 2 at 16384 succeeds
        ctx_tries: list[int] = []
        DCP.resolve_ctx = lambda val, *_a, **_kw: int(val or 32768)

        class _MgrOkOnSecond:
            def _find_loaded(self, *a, **kw):
                return None

            def pin(self, *a, **kw):
                return None

            def unpin_all(self):
                return 0

            async def evict(self, *a, **kw):
                return None

            async def list_loaded(self):
                return []

            async def ensure_loaded(self, _mdl, num_ctx=8192, **kw):
                ctx_tries.append(int(num_ctx))
                if len(ctx_tries) == 1:
                    raise VRAMPreFlightError(
                        "qwen3.5:4b", int(num_ctx), 9000.0, 1200.0,
                        "preflight", 300, False, "VRAM preflight blocked",
                    )
                return 18200

        LSM.manager = _MgrOkOnSecond()
        events_ok: list = []
        st = dict(DEFAULT_SETTINGS)
        st.update({
            "agents": {"duo_coder": {"model": "qwen3.5:4b"}},
            "duo_coder_ctx_agentic": 32768,
            "duo_evict_planner_on_coder": False,
        })
        ctx = _make_run_ctx(tmp, st, events_ok)
        ps = DCP._DuoCoderPreloopState(
            ws_str=str(tmp),
            coder_mdl="qwen3.5:4b",
            exec_mdl="qwen3.5:4b",
            duo_coder_tok=2048,
            workers_were_loaded=True,
            skip_coder_pin=True,
            duo_coder_sys="sys",
            img_plan={"coder": "none"},
        )

        async def _run_preloop():
            out = []
            async for ev in DCP._duo_coder_preloop(ctx, ps):
                out.append(ev)
            return out

        asyncio.run(asyncio.wait_for(_run_preloop(), timeout=10.0))
        check("4a _VRAMPreFlightError retry at ctx/2 leaves duo_hard_stop False",
              ctx_tries == [32768, 16384]
              and ps.duo_hard_stop is False
              and ps.stop is False
              and ps.coder_port == 18200
              and not any("No fallback loaded" in str(e.get("content", "")) for e in events_ok if isinstance(e, dict)),
              f"(ctx_tries={ctx_tries}, hard_stop={ps.duo_hard_stop}, stop={ps.stop}, port={ps.coder_port})")

        # Case B: exhausted at floor 8192 -> sets duo_hard_stop=True and calls _pre_loop_resume
        resume_calls = [0]

        def _fake_resume(*a, **kw):
            resume_calls[0] += 1

        async def _fake_abort_preloop(*a, **kw):
            if False:
                yield {}

        DCP._pre_loop_resume = _fake_resume
        DCP._abort_preloop = _fake_abort_preloop

        class _MgrAlwaysVpf:
            def _find_loaded(self, *a, **kw):
                return None

            def pin(self, *a, **kw):
                return None

            def unpin_all(self):
                return 0

            async def evict(self, *a, **kw):
                return None

            async def list_loaded(self):
                return []

            async def ensure_loaded(self, _mdl, num_ctx=8192, **kw):
                raise VRAMPreFlightError(
                    "qwen3.5:4b", int(num_ctx), 9000.0, 1200.0,
                    "preflight", 300, False, "VRAM preflight blocked",
                )

        LSM.manager = _MgrAlwaysVpf()
        events_fail: list = []
        st2 = dict(st)
        st2["duo_coder_ctx_agentic"] = 8192
        ctx2 = _make_run_ctx(tmp, st2, events_fail)
        ps2 = DCP._DuoCoderPreloopState(
            ws_str=str(tmp),
            coder_mdl="qwen3.5:4b",
            exec_mdl="qwen3.5:4b",
            duo_coder_tok=2048,
            workers_were_loaded=True,
            skip_coder_pin=True,
            duo_coder_sys="sys",
            img_plan={"coder": "none"},
        )

        async def _run_preloop2():
            out = []
            async for ev in DCP._duo_coder_preloop(ctx2, ps2):
                out.append(ev)
            return out

        asyncio.run(asyncio.wait_for(_run_preloop2(), timeout=10.0))
        check("4b exhausted _VRAMPreFlightError sets duo_hard_stop=True & calls _pre_loop_resume",
              ps2.duo_hard_stop is True
              and ps2.stop is True
              and resume_calls[0] == 1
              and any("No fallback loaded" in str(e.get("content", "")) for e in events_fail if isinstance(e, dict)),
              f"(hard_stop={ps2.duo_hard_stop}, resume_calls={resume_calls[0]})")
    finally:
        if orig_mgr is not None:
            LSM.manager = orig_mgr
        DCP.asyncio.sleep = orig_sleep
        DCP.resolve_ctx = orig_resolve_ctx
        DCP._pre_loop_resume = orig_pre_resume
        DCP._abort_preloop = orig_abort_preloop
        shutil.rmtree(tmp, ignore_errors=True)


def test_write_tools_statuses_and_stub_echo():
    import core.tool_executor as TE
    import core.duo_runner as DR

    check("5a WRITE_TOOLS contains all 6 write/edit/patch tools",
          {"write_file", "write_file_append", "edit_file",
           "patch_file", "replace_lines", "apply_patch"} <= TE.WRITE_TOOLS)
    check("5b WRITE_STATUSES explicitly includes 'patched' and core ops",
          "patched" in TE.WRITE_STATUSES
          and {"created", "rewrote", "edited", "patched", "write", "append"} <= TE.WRITE_STATUSES)
    check("5c duo_runner imports shared WRITE_TOOLS, WRITE_STATUSES, _looks_like_stub",
          DR.WRITE_TOOLS is TE.WRITE_TOOLS
          and DR.WRITE_STATUSES is TE.WRITE_STATUSES
          and DR._looks_like_stub is TE._looks_like_stub)

    stubs = [
        '{"path": "a.py", "content": "[executed write_file: 12000 arg chars - content NOT in context anymore]"}',
        '{"path": "a.py", "new_text": "[applied diff: a.py (+5/-2 lines)]"}',
        '{"path": "a.py", "content": "[applied write: a.py (+12 lines)]"}',
        '{"path": "a.py", "replacement": "[applied edit: a.py]"}',
        '{"patch": "this content never reached the file"}',
        '{"content": "[write_file attempt FAILED/cut at the output limit (15000 arg chars)]"}',
    ]
    check("5d _looks_like_stub matches all stub variants and ignores real code",
          all(TE._looks_like_stub(s) for s in stubs)
          and not TE._looks_like_stub('{"path": "a.py", "content": "def add(a, b):\\n    return a + b\\n"}'))

    # End-to-end block check via execute_tool_round on apply_patch + write_file + replace_lines
    from core.tool_exec_helpers import ToolRoundState
    from core.agentic_duo_state import DuoRoundState

    called_exec: list[str] = []
    orig_exec_inline = TE._run_inline_tool

    async def _fake_inline(name, _args, **_kw):
        called_exec.append(name)
        return "ok"

    async def _noop_emit(_ev):
        return None

    class _LRUStub:
        def decay(self, _p):
            pass

        def register(self, _i, **_k):
            pass

    TE._run_inline_tool = _fake_inline
    try:
        hooks = TE.ToolExecHooks(
            emit=_noop_emit,
            is_aborted=lambda _cid: False,
        )
        trs = ToolRoundState(
            tool_ctx_lru=_LRUStub(),
            duo_deadline_at=10**12,
            verify_mutation_serial=0,
            verify_last_ok_serial=0,
            last_run_bash_failure=None,
            changed_since_failure=set(),
            last_learned_insight_sig="",
            last_too_large_path=[None],
            attempts_per_file={},
            tool_error_retries={},
            call_sigs=[],
            recent_focus_paths=[],
            file_changes={},
            duo_seen_web_queries=set(),
            cached_coder_port=[None],
            task_complete_blocked_count=None,
            total_tool_errors=None,
            allow_send_file=False,
        )
        dtool_msgs: list = []
        tcs = [
            {"id": "t1", "function": {"name": "apply_patch", "arguments": json.dumps({"patch": "[applied diff: x (+1/-1)]"})}},
            {"id": "t2", "function": {"name": "write_file", "arguments": json.dumps({"path": "a.py", "content": "[applied write: a.py (+3 lines)]"})}},
            {"id": "t3", "function": {"name": "replace_lines", "arguments": json.dumps({"path": "a.py", "content": "this content never reached the file"})}},
        ]
        asyncio.run(TE.execute_tool_round(
            tool_calls=tcs,
            dtool_msgs=dtool_msgs,
            round_state=DuoRoundState(),
            hooks=hooks,
            trs=trs,
            tool_mode="tool_agent",
            duo_ws=False,
            workspace_lock=str(ROOT),
            exec_model="qwen3.5:4b",
            auto_test_before_complete=False,
            exec_has_thinking=False,
            tool_think_auto_mode="off",
            run_id_global="w2-stub",
            chat_id="w2-stub",
            subtask_index=0,
        ))
        blocked_msgs = [m for m in dtool_msgs if m.get("role") == "tool" and "STUB_ECHO_BLOCKED" in str(m.get("content", ""))]
        check("5e execute_tool_round blocks echoed stubs for apply_patch/write_file/replace_lines",
              len(blocked_msgs) == 3 and not called_exec,
              f"(blocked={len(blocked_msgs)}, called={called_exec})")
    finally:
        TE._run_inline_tool = orig_exec_inline


def test_vision_cfg_unification():
    import core.state as CS
    import vision.preprocess as VP
    import routers.vision as RV

    check("6a _vision_cfg is ONE shared dict object across state, preprocess, router",
          VP._vision_cfg is CS._vision_cfg and RV._vision_cfg is CS._vision_cfg)

    tmp = Path(tempfile.mkdtemp(prefix="w2_b4_vis_"))
    orig_file = VP.VISION_MODEL_FILE
    orig_snapshot = dict(VP._vision_cfg)
    try:
        cfg_file = tmp / "vision_model.json"
        cfg_file.write_text(json.dumps({"model": "initial-vl", "enabled": False, "prompt": "P0"}), encoding="utf-8")
        VP.VISION_MODEL_FILE = cfg_file

        VP._vision_cfg["stale_extra_key"] = "should_be_cleared"

        class _Req:
            def __init__(self, payload):
                self._payload = payload

            async def json(self):
                return self._payload

        res_get = asyncio.run(RV.get_vision_config())
        check("6b GET /vision/config clears stale keys in-place while preserving identity",
              res_get is VP._vision_cfg
              and "stale_extra_key" not in VP._vision_cfg
              and CS._vision_cfg.get("model") == "initial-vl")

        res_post = asyncio.run(RV.set_vision_config(_Req({"model": "new-vl:8b", "enabled": True, "prompt": "Describe"})))
        check("6c POST /vision/config updates shared dict in-place and persists",
              res_post.get("ok") is True
              and res_post.get("config") is VP._vision_cfg
              and VP._vision_cfg.get("model") == "new-vl:8b"
              and CS._vision_cfg.get("enabled") is True
              and json.loads(cfg_file.read_text(encoding="utf-8")).get("model") == "new-vl:8b")

        markers = VP._VISION_POISON_MARKERS
        check("6d _VISION_POISON_MARKERS preserved intact",
              isinstance(markers, tuple)
              and "ich bin hivemind" in markers
              and "[nutzer]\n" in markers
              and "i am hivemind" in markers
              and "[user]\n" in markers)
    finally:
        VP.VISION_MODEL_FILE = orig_file
        VP._vision_cfg.clear()
        VP._vision_cfg.update(orig_snapshot)
        shutil.rmtree(tmp, ignore_errors=True)


def test_route_decision_and_decider():
    import core.state as CS
    import core.chat_route_decision as CRD
    import routing.decider as RD

    # 7a Stale _last_judge_verdict must not hijack an encoder-routed decision
    orig_pipe = CS.pipeline
    try:
        CS.pipeline = NS(_last_judge_verdict={"route": "duo", "task_type": "code"})

        async def _fake_decide(_txt):
            # Simulates chat_run._decide_route reading CS.pipeline._last_judge_verdict
            return RD.Decision(
                route="direct",
                planner=False,
                source="decider",
                confidence=0.92,
                latency_ms=1.0,
                details={"judge_verdict": dict(getattr(CS.pipeline, "_last_judge_verdict", {}) or {})},
            )

        async def _emit(_ev):
            return ""

        ps = CRD._ChatRouteDecisionState(
            _decide_route=_fake_decide,
            _prepro_success=False,
            _run_settings={"routing_decider": "on"},
            _sess_compress_threshold=50,
            _task_type="general",
            agentic_force=False,
            duo_config=NS(agentic_mode=False),
            emit=_emit,
            force_complexity="",
            images=[],
            mode="auto",
            model_overrides={},
            user_input="Explain what TCP handshakes do",
            _judge_verdict={"route": "pipeline"},
        )

        async def _drain():
            async for _ in CRD._chat_route_decision(ps):
                pass

        asyncio.run(_drain())
        check("7a stale _last_judge_verdict reset before route decision (stays simple)",
              CS.pipeline._last_judge_verdict == {}
              and ps._judge_verdict == {}
              and ps.complexity == "simple",
              f"(verdict={CS.pipeline._last_judge_verdict}, complexity={ps.complexity})")

        # When _decide_route actually sets a fresh judge_verdict, it still applies
        CS.pipeline._last_judge_verdict = {"route": "duo", "task_type": "code"}

        async def _fake_decide_with_judge(_txt):
            CS.pipeline._last_judge_verdict = {"route": "pipeline", "task_type": "analysis"}
            return RD.Decision(
                route="direct",
                planner=False,
                source="judge",
                confidence=0.5,
                latency_ms=1.0,
                details={"judge_verdict": dict(CS.pipeline._last_judge_verdict)},
            )

        ps2 = CRD._ChatRouteDecisionState(
            _decide_route=_fake_decide_with_judge,
            _prepro_success=False,
            _run_settings={"routing_decider": "on"},
            _sess_compress_threshold=50,
            _task_type="general",
            agentic_force=False,
            duo_config=NS(agentic_mode=False),
            emit=_emit,
            force_complexity="",
            images=[],
            mode="auto",
            model_overrides={},
            user_input="Analyze these system logs",
        )

        async def _drain2():
            async for _ in CRD._chat_route_decision(ps2):
                pass

        asyncio.run(_drain2())
        check("7b fresh judge_verdict computed inside _decide_route still applies",
              ps2.complexity == "complex" and ps2._effective_task_type == "analysis",
              f"(complexity={ps2.complexity}, task_type={ps2._effective_task_type})")
    finally:
        CS.pipeline = orig_pipe

    # 7b Encoder PLANNER_LABELS mapping + German stem matchers intact
    class _FakeEnc:
        model_name = "fake"

        def __init__(self, route, label):
            self._route = route
            self._label = label

        def decide(self, _text, want_planner=False):
            return self._route, 0.91, {"planner": {"label": self._label}, "inference_ms": 1.0}

    dec_pos = [
        RD._encoder_decision_sync(_FakeEnc("agentic", lbl), "build app", True)
        for lbl in ("several_files", "unclear_cause", "from_scratch", "yes")
    ]
    dec_neg = [
        RD._encoder_decision_sync(_FakeEnc("agentic", "one_small_edit"), "fix typo", True),
        RD._encoder_decision_sync(_FakeEnc("agentic", "no"), "fix typo", True),
        RD._encoder_decision_sync(_FakeEnc("direct", "several_files"), "what is python", True),
    ]
    check("7c _encoder_decision_sync maps PLANNER_LABELS positives and gates direct",
          all(d is not None and d.planner is True for d in dec_pos)
          and all(d is not None and d.planner is False for d in dec_neg))

    async def _test_async_decider():
        orig_backend = RD.backend
        orig_cache = RD.cache
        RD.cache = lambda: RD.DecisionCache(size=8)
        try:
            st_enc = {
                "routing_decider": "on",
                "routing_planner": "decider",
                "routing_judge": "off",
                "routing_decider_threshold": 0.65,
            }
            RD.backend = lambda _m="": _FakeEnc("agentic", "from_scratch")
            d1 = await RD.decide_async(st_enc, "Build a snake game in python")
            RD.backend = lambda _m="": _FakeEnc("agentic", "one_small_edit")
            d2 = await RD.decide_async(st_enc, "Rename var foo to bar in main.py")
            return d1.planner, d2.planner
        finally:
            RD.backend = orig_backend
            RD.cache = orig_cache

    p1, p2 = asyncio.run(_test_async_decider())
    check("7d decide_async respects encoder PLANNER_LABELS ('from_scratch'->True, 'one_small_edit'->False)",
          p1 is True and p2 is False, f"(p1={p1}, p2={p2})")

    # German stem matchers check (AGENTS.md invariant)
    de_agentic = RD.rule_decision("Schreibe eine Funktion im Skript die JSON parst")
    de_direct = RD.rule_decision("Was ist der Unterschied zwischen TCP und UDP?")
    check("7e routing/decider German stem matchers remain intact",
          de_agentic.route == "agentic" and de_direct.route == "direct",
          f"(de_agentic={de_agentic.route}, de_direct={de_direct.route})")


def test_lsp_hardening():
    import infra.lsp_registry as LR
    import infra.lsp_client as LC

    orig_plat = LR.sys.platform
    try:
        LR.sys.platform = "win32"
        check("8a _wrap_windows_shell leaves empty argv and non-.cmd/.bat unchanged",
              LR._wrap_windows_shell([]) == []
              and LR._wrap_windows_shell(["python.exe", "a & b"]) == ["python.exe", "a & b"])

        wrapped = LR._wrap_windows_shell([
            r"C:\sp dir\srv.cmd",
            "--flag",
            r"C:\a b\"",
            r'x\"y',
            "a&whoami",
            "x=1;y=2",
            "",
        ])
        check("8b _wrap_windows_shell escapes trailing backslashes, embedded quotes, metacharacters, empty arg",
              wrapped[:3] == ["cmd", "/d", "/c"]
              and wrapped[3] == r'"C:\sp dir\srv.cmd"'
              and wrapped[4] == "--flag"
              and wrapped[5] == r'"C:\a b\\"""'
              and wrapped[6] == r'"x\\""y"'
              and wrapped[7] == '"a&whoami"'
              and wrapped[8] == '"x=1;y=2"'
              and wrapped[9] == '""',
              f"(wrapped={wrapped})")

        trailing_bs = LR._wrap_windows_shell([r"C:\dir with space\srv.cmd", r"C:\folder with space\\"[0:-1]])
        check("8c trailing backslash before closing quote is doubled",
              trailing_bs[4] == r'"C:\folder with space\\"',
              f"(trailing_bs={trailing_bs})")

        bad_ctrl_caught = 0
        for bad in ("a\nb", "a\rb", "a\x00b"):
            try:
                LR._wrap_windows_shell(["srv.cmd", bad])
            except ValueError:
                bad_ctrl_caught += 1
        check("8d _wrap_windows_shell rejects newlines and NUL with ValueError",
              bad_ctrl_caught == 3, f"(caught={bad_ctrl_caught}/3)")
    finally:
        LR.sys.platform = orig_plat

    # Real Windows .cmd execution via _LspConnection.start when on win32
    if sys.platform == "win32":
        tmp_cmd = Path(tempfile.mkdtemp(prefix="w2 b4 cmd "))
        try:
            cmdp = tmp_cmd / "echo_lsp.cmd"
            py_exe = sys.executable
            script = (
                "import sys, json\n"
                "raw = sys.stdin.buffer.read(10)\n"
                "# Not needed, just exit after writing args\n"
            )
            _ = script
            cmdp.write_text(
                f'@echo off\r\n"{py_exe}" -c "import sys; print(repr(sys.argv[1:]))" %*\r\n',
                encoding="utf-8",
            )

            async def _run_cmd_shim():
                argv = LR._wrap_windows_shell([str(cmdp), "hello world", "C:\\a b\\", "a&echo PWNED", "a>NUL", ""])
                conn = LC._LspConnection("test", argv, tmp_cmd, 2.0)
                try:
                    await conn.start()
                except LC.LspError:
                    pass
                finally:
                    await conn.close()
                return True

            # Verify directly via create_subprocess_shell path used inside _LspConnection.start
            async def _verify_cmd_argv():
                argv = LR._wrap_windows_shell([str(cmdp), "hello world", "C:\\a b\\", "a&echo PWNED", "a>NUL", ""])
                proc = await asyncio.create_subprocess_shell(
                    " ".join(argv[3:]),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await proc.communicate()
                tr = getattr(proc, "_transport", None)
                if tr is not None:
                    tr.close()
                await asyncio.sleep(0)
                return proc.returncode, out.decode("utf-8", "replace").strip(), err.decode("mbcs", "replace")

            rc, out_s, _err_s = asyncio.run(_verify_cmd_argv())
            check("8e Windows .cmd execution with spaces/trailing-bs/metachars/empty arg preserves exact argv",
                  rc == 0 and out_s == "['hello world', 'C:\\\\a b\\\\', 'a&echo PWNED', 'a>NUL', '']",
                  f"(rc={rc}, out={out_s!r})")
            asyncio.run(_run_cmd_shim())
        finally:
            shutil.rmtree(tmp_cmd, ignore_errors=True)

    # _LspConnection._read_message rejects negative and oversized Content-Length BEFORE readexactly
    async def _test_neg_len():
        results = []
        for hdr in (b"Content-Length: -5\r\n\r\n", f"Content-Length: {LC.MAX_MESSAGE_BYTES + 1}\r\n\r\n".encode()):
            conn = LC._LspConnection("py", ["noop"], ROOT, 1.0)
            readexactly_called = [False]

            class _FakeStdout:
                async def readuntil(self, _sep):
                    return hdr

                async def readexactly(self, _n):
                    readexactly_called[0] = True
                    return b""

            conn._proc = NS(stdout=_FakeStdout(), returncode=None)
            msg = await conn._read_message()
            results.append((msg is None, conn.dead is True, readexactly_called[0] is False))
        return results

    neg_res = asyncio.run(_test_neg_len())
    check("8f _read_message rejects negative and oversized Content-Length before readexactly",
          all(all(t) for t in neg_res), f"(results={neg_res})")

    # _stdin_lock serializes concurrent _send writes cleanly
    async def _test_stdin_lock():
        conn = LC._LspConnection("py", ["noop"], ROOT, 1.0)
        buf = bytearray()

        class _SlowStdin:
            def is_closing(self):
                return False

            def write(self, data: bytes):
                mid = len(data) // 2
                buf.extend(data[:mid])
                self._rest = data[mid:]

            async def drain(self):
                await asyncio.sleep(0.005)
                buf.extend(self._rest)

        conn._proc = NS(stdin=_SlowStdin(), returncode=None)
        await asyncio.gather(*[conn._send({"i": i, "pad": "x" * 20}) for i in range(6)])
        pos = 0
        decoded = []
        raw = bytes(buf)
        while pos < len(raw):
            hdr_end = raw.index(b"\r\n\r\n", pos)
            length = LC._content_length(raw[pos:hdr_end + 4])
            start = hdr_end + 4
            body = raw[start:start + length]
            decoded.append(json.loads(body.decode("utf-8")))
            pos = start + length
        return sorted(d["i"] for d in decoded)

    sent_ids = asyncio.run(_test_stdin_lock())
    check("8g _stdin_lock serializes concurrent _send frames without interleaving",
          sent_ids == [0, 1, 2, 3, 4, 5], f"(sent_ids={sent_ids})")

    # Child reaping on close, dead connection reap_idle, and close_all
    async def _test_reaping():
        mgr = LC.LspManager(ROOT, settings={"lsp_idle_timeout_s": 1.0})
        # 1) Normal connection closed directly via conn.close()
        c1 = LC._LspConnection("py1", [sys.executable, "-c", "import time; time.sleep(30)"], ROOT, 2.0)
        c1._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(30)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        c1.dead = True  # simulate already marked dead without reap
        p1 = c1._proc
        # 2) Already-exited process left un-reaped inside a dead conn via manager.reap_idle()
        c2 = LC._LspConnection("py2", [sys.executable], ROOT, 2.0)
        c2._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import sys; sys.exit(0)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        c2.dead = True
        p2 = c2._proc
        mgr._conns["py1"] = c1
        mgr._last_used["py1"] = 0.0
        mgr._conns["py2"] = c2
        mgr._last_used["py2"] = 0.0

        reaped = mgr.reap_idle(now=100.0)
        await mgr.close_all()
        return (
            set(reaped) == {"py1", "py2"}
            and p1.returncode is not None
            and p2.returncode is not None
            and c1._proc is None
            and c2._proc is None
            and not mgr._bg_tasks
        )

    check("8h reap_idle + close_all reap dead/live children and flush background close tasks",
          asyncio.run(_test_reaping()))


def test_soul_endpoints():
    import routers.soul as RS
    import hive_functions.soul_engine as SE
    import core.state as CS

    tmp = Path(tempfile.mkdtemp(prefix="w2_b4_soul_"))
    orig_base = RS._BASE_FILE
    orig_load_settings = RS.load_settings
    orig_run_counter = RS._load_run_counter
    orig_reg_get = CS.registry_get
    orig_reg_all = CS.registry_all
    orig_pipeline = CS.pipeline
    orig_maybe = RS.maybe_evolve_soul

    try:
        RS._BASE_FILE = tmp / "server.py"
        RS.load_settings = lambda: {"learning_preset_mode": True}
        RS._load_run_counter = lambda: 5
        CS.registry_get = lambda role: "reflect-model" if role == "direct" else ""
        CS.registry_all = lambda: {"direct": "reflect-model"}

        class _Req:
            def __init__(self, payload):
                self._payload = payload

            async def json(self):
                return self._payload

        # /current falls back to original when learned soul text is empty
        cur_empty = asyncio.run(RS.get_soul_current())
        check("9a GET /soul/current falls back to original when learned soul is empty",
              cur_empty.get("source") == "original" and bool(cur_empty.get("text")))

        # POST /soul/learned rejects < 50 chars
        short_res = asyncio.run(RS.save_soul_learned(_Req({"text": "too short"})))
        check("9b POST /soul/learned rejects < 50 char payloads with 400",
              getattr(short_res, "status_code", 200) == 400)

        # POST /soul/learned saves valid text, bumps version & count
        valid_text = "HiveMind custom learned soul self-understanding text that easily exceeds fifty chars."
        save_res = asyncio.run(RS.save_soul_learned(_Req({"text": valid_text, "reason": "Manual update"})))
        learned_get = asyncio.run(RS.get_soul_learned_text())
        cur_learned = asyncio.run(RS.get_soul_current())
        check("9c POST /soul/learned updates soul, GET /learned exposes 'text', GET /current serves 'learned'",
              save_res.get("ok") is True
              and save_res["status"]["version"] == 2
              and learned_get.get("text") == valid_text
              and learned_get.get("selbstverstaendnis") == valid_text
              and cur_learned.get("source") == "learned"
              and cur_learned.get("text") == valid_text)

        # DELETE /soul/learned resets text/version AND preserves evolution_log!
        del_res = asyncio.run(RS.reset_soul_learned(_Req({})))
        after_reset = SE.load_soul(RS._BASE_FILE)
        check("9d DELETE /soul/learned resets soul text & version while preserving evolution_log",
              del_res.get("ok") is True
              and after_reset.get("selbstverstaendnis") == ""
              and after_reset.get("version") == 1
              and len(after_reset.get("evolution_log", [])) == 1
              and after_reset["evolution_log"][0].get("reason") == "Manually reset via UI",
              f"(after_reset={after_reset})")

        # POST /soul/evolve handles missing _state.pipeline cleanly (503)
        CS.pipeline = None
        no_pipe_res = asyncio.run(RS.trigger_soul_evolution_ep(_Req({"force": True})))
        check("9e POST /soul/evolve guards missing _state.pipeline with 503",
              getattr(no_pipe_res, "status_code", 200) == 503)

        # POST /soul/evolve force=True passes total_runs > persisted run_count
        seed = SE.load_soul(RS._BASE_FILE)
        seed["run_count"] = 200
        SE.save_soul(RS._BASE_FILE, seed)

        captured_total_runs = []

        async def _fake_maybe_evolve(*, total_runs, **kw):
            captured_total_runs.append(total_runs)
            s = SE.load_soul(RS._BASE_FILE)
            if s.get("run_count", 0) >= total_runs:
                return None
            s["selbstverstaendnis"] = valid_text
            s["version"] += 1
            s["run_count"] = total_runs
            SE.save_soul(RS._BASE_FILE, s)
            return s

        CS.pipeline = NS(ollama=object())
        RS.maybe_evolve_soul = _fake_maybe_evolve
        ev_res = asyncio.run(RS.trigger_soul_evolution_ep(_Req({"force": True})))
        check("9f POST /soul/evolve force=True avoids clobbering run_count above total_runs",
              ev_res.get("ok") is True
              and captured_total_runs == [201],
              f"(ev_res={ev_res}, captured={captured_total_runs})")

        # GET /soul/skills handles empty **Pfade:** cleanly without bleeding lines
        skills_dir = SE._skills_dir(RS._BASE_FILE)
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "skill_00_demo.md").write_text(
            "# Skill: Demo\n\n"
            "**Relevanz:** 0.85  **Gesehen:** 3x  **Quelle:** auto\n\n"
            "Always read files first.\n\n"
            "**Pfade:** \n",
            encoding="utf-8",
        )
        skills = asyncio.run(RS.get_soul_skills())
        check("9g GET /soul/skills parses fields and empty Pfade cleanly",
              len(skills) == 1
              and skills[0]["relevance_score"] == 0.85
              and skills[0]["merge_count"] == 3
              and skills[0]["source"] == "auto"
              and skills[0]["trigger_paths"] == [],
              f"(skills={skills})")
    finally:
        RS._BASE_FILE = orig_base
        RS.load_settings = orig_load_settings
        RS._load_run_counter = orig_run_counter
        CS.registry_get = orig_reg_get
        CS.registry_all = orig_reg_all
        CS.pipeline = orig_pipeline
        RS.maybe_evolve_soul = orig_maybe
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_duo_cleanup_order_and_retry()
    test_written_path_containment()
    test_vram_preflight_cascade_hard_stop()
    test_write_tools_statuses_and_stub_echo()
    test_vision_cfg_unification()
    test_route_decision_and_decider()
    test_lsp_hardening()
    test_soul_endpoints()
    sys.exit(h.finish("audit_wave2_backend_duo"))


if __name__ == "__main__":
    main()
