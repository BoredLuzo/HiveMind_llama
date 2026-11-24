# -*- coding: utf-8 -*-
"""Regression suite for the combined final release batch (GLM Slice A + Non-GLM blockers).

Run: python tests/test_final_release_batch.py
Exit 0 = all pass, Exit 1 = failures.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("HIVEMIND_NO_NOTIFY", "1")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


def _write_test_gguf(
    path: Path,
    *,
    arch: str = "llama",
    kv_entries: dict[str, tuple[int, object]] | None = None,
) -> None:
    """Build a minimal valid v3 GGUF file with custom metadata KV pairs."""
    import struct

    def _pack_str(s: str) -> bytes:
        b = s.encode("utf-8")
        return struct.pack("<Q", len(b)) + b

    def _pack_val(vtype: int, val: object) -> bytes:
        if vtype == 4:  # uint32
            return struct.pack("<I", int(val))
        if vtype == 8:  # string
            return _pack_str(str(val))
        if vtype == 9:  # array of uint32
            items = list(val)  # type: ignore[arg-type]
            return struct.pack("<IQ", 4, len(items)) + b"".join(struct.pack("<I", int(x)) for x in items)
        raise ValueError(f"unsupported vtype {vtype}")

    entries: list[tuple[str, int, object]] = [("general.architecture", 8, arch)]
    for k, (vt, v) in (kv_entries or {}).items():
        entries.append((k, vt, v))
    buf = bytearray()
    buf += b"GGUF"
    buf += struct.pack("<IQQ", 3, 0, len(entries))
    for k, vt, v in entries:
        buf += _pack_str(k)
        buf += struct.pack("<I", vt)
        buf += _pack_val(vt, v)
    path.write_bytes(bytes(buf))


# ── 1. Sanitizer + ToolLoop + Subagent (GLM P1-1 & P2-3) ────────────────────
def test_toolcall_sanitizer_and_loops() -> None:
    import httpx
    import core.toolcall_sanitize as TS
    import core.tool_loop as TL
    import core.subagent_lite as SL

    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c_empty", "type": "function", "function": {"name": "list_dir", "arguments": ""}},
            {"id": "c_str", "type": "function", "function": {"name": "read_file", "arguments": '"Berlin"'}},
            {"id": "c_arr", "type": "function", "function": {"name": "read_file", "arguments": "[1, 2]"}},
            {"id": "", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "x'}},
            {"id": "c_ok", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}},
            {"id": "c_none", "type": "function", "function": {"name": "list_dir", "arguments": None}},
        ]},
        {"role": "tool", "tool_call_id": "c_empty", "content": "err"},
        {"role": "tool", "tool_call_id": "c_str", "content": "err"},
        {"role": "tool", "tool_call_id": "c_arr", "content": "err"},
        {"role": "tool", "tool_call_id": "c_ok", "content": "ok"},
    ]
    n_rem = TS.sanitize_invalid_tool_call_history(msgs)
    kept_tcs = [tc["id"] for tc in (msgs[0].get("tool_calls") or [])]
    kept_tools = [m.get("tool_call_id") for m in msgs if m.get("role") == "tool"]
    check(
        "sanitizer drops empty, scalar, array, id-less bad JSON args and keeps {} + None",
        n_rem == 4 and kept_tcs == ["c_ok", "c_none"] and kept_tools == ["c_ok"],
        f"n_rem={n_rem} kept_tcs={kept_tcs} kept_tools={kept_tools}",
    )

    # ToolLoop: strips prior bad history before POST and aborts truncated tool_calls on finish_reason=length
    seen_payloads: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_payloads.append(body)
        idx = len(seen_payloads)
        if idx == 1:
            return httpx.Response(200, json={
                "choices": [{
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "tc_len", "type": "function",
                                        "function": {"name": "list_dir", "arguments": "{}"}}],
                    },
                }],
            })
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "length",
                "message": {"role": "assistant", "content": "partial text", "tool_calls": []},
            }],
        })

    async def _run_tl():
        client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        executed: list[str] = []

        async def _exec(name: str, _args: dict) -> str:
            executed.append(name)
            return "ok"

        loop = TL.ToolLoop(
            TL.ToolLoopConfig(
                stream=False, max_rounds=4, model="fake:1b",
                tools=[{"type": "function", "function": {"name": "list_dir", "parameters": {"type": "object"}}}],
            ),
            client,
            9999,
            custom_executor=_exec,
        )
        seed = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "bad1", "type": "function", "function": {"name": "list_dir", "arguments": '"Berlin"'}},
            ]},
            {"role": "tool", "tool_call_id": "bad1", "content": "bad"},
            {"role": "user", "content": "go"},
        ]
        evs = []
        async for ev in loop.run(seed):
            evs.append(ev)
        await client.aclose()
        return loop.state, executed, evs

    tl_state, tl_exec, tl_evs = asyncio.run(_run_tl())
    first_sent_tcs = [m.get("tool_calls") for m in seen_payloads[0]["messages"] if m.get("role") == "assistant"]
    check(
        "ToolLoop sanitizes history before POST, skips length-truncated tool call, records length stop",
        len(seen_payloads) == 2
        and first_sent_tcs == [None]
        and tl_exec == []
        and tl_state.stop_reason == "length"
        and any("truncated" in str(e.get("content", "")).lower() for e in tl_evs if e.get("type") == "status"),
        f"payloads={len(seen_payloads)} first_tcs={first_sent_tcs} exec={tl_exec} stop={tl_state.stop_reason}",
    )

    # subagent_lite: invalid JSON/non-dict args return INVALID_JSON without tool exec; length skips tool calls
    sub_calls: list[list[dict]] = []
    sub_exec: list[tuple[str, dict]] = []

    async def _fake_sub_call(_port: int, _model: str, messages: list[dict]):
        sub_calls.append(json.loads(json.dumps(messages)))
        step = len(sub_calls)
        if step == 1:
            return ({"role": "assistant", "content": "", "tool_calls": [
                {"id": "sa_len", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}},
            ]}, "length")
        if step == 2:
            return ({"role": "assistant", "content": "", "tool_calls": [
                {"id": "sa_bad", "type": "function", "function": {"name": "list_dir", "arguments": '"Berlin"'}},
            ]}, "stop")
        return ({"role": "assistant", "content": "sub done", "tool_calls": []}, "stop")

    async def _fake_inline(name: str, args: dict, **_kw):
        sub_exec.append((name, args))
        return "dir"

    fake_mgr = SimpleNamespace(
        _slots=[],
        ensure_loaded=mock.AsyncMock(return_value=8123),
        evict=mock.AsyncMock(return_value=None),
    )
    with mock.patch("backend.llama_server_manager.manager", fake_mgr), \
         mock.patch.object(SL, "_call_model_with_meta", side_effect=_fake_sub_call), \
         mock.patch("tools.runner._run_inline_tool", side_effect=_fake_inline):
        sub_out = asyncio.run(SL._run_sub_loop("check files", str(ROOT), "fake:1b", 4096))

    third_call_tcs = [m.get("tool_calls") for m in sub_calls[2] if m.get("role") == "assistant"]
    check(
        "subagent_lite skips length-truncated calls, rejects non-dict args, sanitizes before next round",
        len(sub_calls) == 3
        and sub_exec == []
        and "[TRUNCATED]" in str(sub_calls[1][-1].get("content"))
        and third_call_tcs == [None]
        and "sub done" in sub_out,
        f"calls={len(sub_calls)} exec={sub_exec} tcs={third_call_tcs} out={sub_out!r}",
    )


# ── 2. Direct Prompt Cache Clock & Session Summary Cache (GLM P1-2 & P2-4) ─
def test_prompt_cache_and_session_summary_cache() -> None:
    import context.chat_util as CU
    import context.compression as CMP

    dr_src = (ROOT / "core" / "direct_runner.py").read_text(encoding="utf-8")
    check("direct_runner system prompt no longer embeds per-minute HH:MM clock",
          "Current local time:" not in dr_src)

    fake_pipe = SimpleNamespace(memory=SimpleNamespace(as_context_string=lambda: ""))
    msgs = CU._make_messages(fake_pipe, "SYS", "What time is it?", [], False, False)
    check(
        "chat_util puts live HH:MM local time in trailing user system note (trail stays date-only)",
        "local time =" in msgs[-1]["content"] and "local time" not in msgs[-2]["content"],
        f"tail={msgs[-1]['content']!r}",
    )

    CMP.init_compression(settings_obj={"session_compress_threshold": 10})
    llm_calls = [0]

    async def _fake_stream(model, _prompt_msgs, _t, _mx):
        llm_calls[0] += 1
        yield f"cached summary #{llm_calls[0]}"

    CMP._pipeline_chat_stream = _fake_stream
    CMP._registry_get = lambda _k: "direct:1b"

    def _mk_sess(n: int) -> list[dict]:
        return [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i} payload"}
            for i in range(n)
        ]

    out1 = asyncio.run(CMP._compress_chat_session(_mk_sess(20)))
    out2 = asyncio.run(CMP._compress_chat_session(_mk_sess(22)))
    out3 = asyncio.run(CMP._compress_chat_session(_mk_sess(24)))
    out4 = asyncio.run(CMP._compress_chat_session(_mk_sess(28)))
    check(
        "session compression reuses cached summary within <6 new old messages and refreshes at >=6",
        llm_calls[0] == 2
        and len(out2) == CMP.SESSION_COMPRESS_KEEP + 2
        and len(out3) == CMP.SESSION_COMPRESS_KEEP + 4
        and len(out4) == CMP.SESSION_COMPRESS_KEEP
        and "cached summary #1" in out1[0]["content"]
        and "cached summary #1" in out2[0]["content"]
        and "cached summary #2" in out4[0]["content"],
        f"llm_calls={llm_calls[0]} lens={[len(out1), len(out2), len(out3), len(out4)]}",
    )
    CMP.init_compression(settings_obj={})


# ── 3. P2-1 Approval Staging Binding & Bridge Generating Filter ─────────────
def test_approval_awaiting_args_and_bridge() -> None:
    import routers.core as RC_ROUTER
    import tools.runner as TR
    from hivemind_gateway.bridge import RunBridge
    from hivemind_gateway.config import GatewayConfig
    from hivemind_gateway.state import GatewayState

    class _FakeReq:
        def __init__(self, body: dict):
            self._body = body

        async def json(self):
            return self._body

    rid = "final-batch-appr"
    TR._clear_staged_approval(rid)
    TR._approval_expired.pop(rid, None)
    TR._pending_approvals[rid] = {
        "tool": "run_bash",
        "preview": "(generating, content streams in the panel)",
        "approval_key": "",
        "args_hash": "",
    }
    try:
        res_approve = asyncio.run(RC_ROUTER.approval_decide(rid, _FakeReq({"answer": "1"})))
        pre_after_approve = dict(TR._approval_pre_decisions)
        res_repo = asyncio.run(RC_ROUTER.approval_decide(rid, _FakeReq({"answer": "2"})))
        pre_after_repo = dict(TR._approval_pre_decisions)
        res_deny = asyncio.run(RC_ROUTER.approval_decide(rid, _FakeReq({"answer": "3|stop"})))
        check(
            "approval_decide refuses blind approve/repo while awaiting args and permits early deny",
            res_approve == {"routed": "awaiting_args", "run_id": rid}
            and res_repo == {"routed": "awaiting_args", "run_id": rid}
            and rid not in pre_after_approve
            and rid not in pre_after_repo
            and res_deny == {"routed": "preview", "run_id": rid}
            and TR._approval_pre_decisions.get(rid, {}).get("answer") == "3",
            f"approve={res_approve} repo={res_repo} deny={res_deny}",
        )
    finally:
        TR._clear_staged_approval(rid)

    # Gateway mirror skips generating-stage approval card without decision_id/key/hash
    sent: list[str] = []

    class _MockMs:
        async def send_message(self, text, **_kw):
            sent.append(text)
            return {"message_id": len(sent)}

        async def edit_message(self, *_a, **_kw):
            pass

    class _MockHive:
        async def journal(self, _rid=None, after=0):
            return {"run_id": "mir1", "active": True, "frames": [], "n": 0}

        async def pending_approval(self, _rid):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "active": True,
                    "kind": "approval",
                    "tool": "run_bash",
                    "preview": "(generating, content streams in the panel)",
                    "decision_id": "",
                    "approval_key": "",
                },
            )

    with tempfile.TemporaryDirectory() as td:
        st = GatewayState(Path(td) / "gw.json")
        br = RunBridge(_MockHive(), st, GatewayConfig(), _MockMs())
        br._mirror().update({
            "run_id": "mir1", "after": 0, "status_msg_id": None,
            "last_status": "", "approval_sig": None, "approval_decision_id": "",
            "approval_card_did": "", "approval_tool": "", "other_run_noted": "",
            "last_phase": "", "answer_parts": [], "ctx_marks": set(),
            "ctx_compression_noted": False, "answer_sent_upto": 0,
            "last_tick": 0.0,
        })
        asyncio.run(br.mirror_tick())
        check(
            "gateway mirror does not relay blind (generating...) approval card",
            len(sent) == 0 and br._mirror().get("approval_sig") is None,
            f"sent={sent} sig={br._mirror().get('approval_sig')}",
        )


# ── 4. Destructive-Gate Expanded Patterns ───────────────────────────────────
def test_destructive_gate_expanded_patterns() -> None:
    import tools.runner as TR

    must_gate = [
        "git push origin +main",
        "git stash drop",
        "git stash clear",
        "git branch -D main",
        "git branch --delete --force feat",
        "dd if=/dev/zero of=/dev/sda",
        "wipefs -a /dev/sda",
        "poweroff",
        "reboot",
        "sudo poweroff",
        "systemctl poweroff",
        "crontab -r",
        "rsync -a --delete empty/ ~/",
    ]
    missing = [c for c in must_gate if TR._is_destructive_bash(c) is None]
    must_allow = [
        "git branch -d merged-branch",
        'git commit -m "fix reboot logic"',
        "rsync -av src/ dst/",
    ]
    false_pos = [c for c in must_allow if TR._is_destructive_bash(c) is not None]
    check(
        "destructive gate blocks all 10+ bypass forms while allowing safe commands",
        not missing and not false_pos,
        f"missing={missing} false_pos={false_pos}",
    )


# ── 5. German Windows netstat + Watchdog + Autofit BOM/Error ───────────────
def test_windows_netstat_watchdog_autofit() -> None:
    import backend.llama_manager_utils as LMU
    import deploy.hardware_autofit as HAF

    netstat_out = "\n".join([
        "  Proto  Lokale Adresse         Remoteadresse          Status           PID",
        "  TCP    127.0.0.1:8081         0.0.0.0:0              ABHÖREN          4242",
        "  TCP    [::1]:8082             [::]:0                 ABH\ufffdREN        4343",
        "  TCP    127.0.0.1:8081         127.0.0.1:50000        HERGESTELLT      5555",
    ])
    calls: list[list[str]] = []

    def _fake_run(argv, **_kw):
        calls.append(list(argv))
        if argv[0] == "netstat":
            return SimpleNamespace(returncode=0, stdout=netstat_out, stderr="")
        if argv[0] == "tasklist":
            return SimpleNamespace(returncode=0, stdout='"llama-server.exe","4242","Console","1","10 K"', stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(sys, "platform", "win32"), \
         mock.patch.object(LMU.subprocess, "run", side_effect=_fake_run):
        LMU._kill_port_sync(8081)
    killed = [c for c in calls if c and c[0] == "taskkill"]
    check(
        "_kill_port_sync handles German Windows ABHÖREN netstat state",
        len(killed) == 1 and "4242" in killed[0],
        f"calls={calls}",
    )

    wd_path = ROOT / "watchdog_engine.bat"
    wd_bytes = wd_path.read_bytes()
    wd_text = wd_bytes.decode("ascii")
    check(
        "watchdog_engine.bat launches detached without blocking call or stdin timeout spin",
        "call start_engine_headless.bat" not in wd_text.lower()
        and "timeout /t" not in wd_text.lower()
        and "Start-Process -FilePath $env:WDHEAD" in wd_text
        and "ping -n 21 127.0.0.1" in wd_text
        and b"\n" not in wd_bytes.replace(b"\r\n", b""),
    )

    with tempfile.TemporaryDirectory() as td:
        sf_bom = Path(td) / "bom_settings.json"
        sf_bom.write_bytes(b"\xef\xbb\xbf" + json.dumps({
            "workspace": "C:/kept/ws",
            "server_port": 8123,
        }).encode("utf-8"))
        res_bom = HAF.apply_hardware_profile(
            sf_bom, profile="autofit", backend="cuda", vram_gb=24.0, ram_gb=64.0,
        )
        saved_bom = json.loads(sf_bom.read_text(encoding="utf-8"))
        check(
            "hardware_autofit reads UTF-8 BOM settings.json and preserves user keys",
            saved_bom.get("workspace") == "C:/kept/ws"
            and saved_bom.get("server_port") == 8123
            and bool(res_bom.get("tier")),
        )

        sf_bad = Path(td) / "bad_settings.json"
        raw_bad = "{corrupt json!!!"
        sf_bad.write_text(raw_bad, encoding="utf-8")
        raised_bad = False
        try:
            HAF.apply_hardware_profile(
                sf_bad, profile="autofit", backend="cuda", vram_gb=24.0, ram_gb=64.0,
            )
        except RuntimeError:
            raised_bad = True
        check(
            "hardware_autofit refuses to overwrite corrupt settings.json",
            raised_bad and sf_bad.read_text(encoding="utf-8") == raw_bad,
        )


# ── 6. LlamaSlot & LlamaClient Slot Recovery / In-Flight Accounting ─────────
def test_slots_and_client_recovery() -> None:
    import httpx
    import backend.llama_slots as LS
    import backend.llama_client as LC

    slot = LS.ModelSlot(0)
    slot.model = "m:1b"
    slot.port = 8101
    with mock.patch("time.monotonic", return_value=10_000.0):
        slot.in_flight = 3
        slot.in_flight_since = 10_000.0 - 3600.0
        slot.mark_in_flight()
        check(
            "ModelSlot.mark_in_flight resets expired leaked count to 1",
            slot.in_flight == 1 and slot.in_flight_active(),
            f"in_flight={slot.in_flight}",
        )

    # _recover_model_port skips evict if another running slot already exists on a new port
    running_new = SimpleNamespace(model="m:1b", port=8102, is_running=True)
    fake_mgr = SimpleNamespace(
        _slots=[running_new],
        _same_model=lambda a, b: a == b,
        get_slot_by_port=lambda p: running_new if p == 8102 else None,
        evict=mock.AsyncMock(),
        ensure_loaded=mock.AsyncMock(return_value=8103),
    )
    client = LC.LlamaClient()
    with mock.patch.object(LC, "manager", fake_mgr):
        rec_port = asyncio.run(client._recover_model_port("m:1b", 4096, old_port=8101))
    check(
        "_recover_model_port adopts already-recovered running slot without calling evict",
        rec_port == 8102 and fake_mgr.evict.call_count == 0 and fake_mgr.ensure_loaded.call_count == 0,
    )

    # chat & chat_stream acquire + release in_flight even on early close
    real_slot = LS.ModelSlot(1)
    real_slot.model = "m:1b"
    real_slot.port = 8105
    real_slot._orphan_port = 8105  # makes is_running=True
    mgr2 = SimpleNamespace(
        _slots=[real_slot],
        get_slot_by_port=lambda p: real_slot if p == 8105 else None,
        ensure_loaded=mock.AsyncMock(return_value=8105),
        evict=mock.AsyncMock(),
    )

    def _chat_handler(req: httpx.Request) -> httpx.Response:
        if b'"stream": true' in req.content or b'"stream":true' in req.content:
            return httpx.Response(
                200,
                text='data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def _exercise_client():
        import contextlib
        c = LC.LlamaClient()
        c._client = httpx.AsyncClient(transport=httpx.MockTransport(_chat_handler))
        with mock.patch.object(LC, "manager", mgr2):
            res_chat = await c.chat("m:1b", [{"role": "user", "content": "hi"}])
            after_chat = real_slot.in_flight
            tokens = []
            async with contextlib.aclosing(c.chat_stream("m:1b", [{"role": "user", "content": "hi"}])) as st_iter:
                async for tok in st_iter:
                    tokens.append(tok)
                    break  # early exit tests finally cleanup under aclosing
            after_stream = real_slot.in_flight
        await c._client.aclose()
        await client._client.aclose()
        return res_chat, after_chat, tokens, after_stream

    rc, af_c, toks, af_s = asyncio.run(_exercise_client())
    check(
        "LlamaClient chat & chat_stream balance mark_in_flight / clear_in_flight & touch",
        rc == "ok" and af_c == 0 and toks == ["hi"] and af_s == 0 and real_slot.last_used > 0,
        f"rc={rc!r} af_c={af_c} toks={toks} af_s={af_s}",
    )


# ── 7. Scheduler Run-Now & Interval Clamp + 4 Mini Fixes ───────────────────
def test_scheduler_and_mini_fixes() -> None:
    import routers.automations as RA
    import scheduler.manager as SM
    import routing.agent_intent as AI
    import backend.gguf_meta as GM
    import learning.peer_ratings as LPR
    import deploy.fetch_models as FM

    with tempfile.TemporaryDirectory() as td:
        mgr = SM.SchedulerManager(Path(td) / "sched.json", "http://127.0.0.1:1")
        now = time.time()
        once_job = mgr.store.add({
            "name": "once1", "prompt": "hi",
            "schedule": {"kind": "once", "interval_s": 600},
            "next_ts": now + 600,
        })
        mgr.lane_busy = lambda: False
        mgr._ensure_chat = mock.AsyncMock(return_value="chat-1")
        mgr._append_turn = mock.AsyncMock()
        mgr._run_job = mock.AsyncMock(return_value=("ok", None))
        asyncio.run(mgr._fire_locked(mgr.store.get(once_job["id"]), now, reason="manual"))
        after_manual = mgr.store.get(once_job["id"])
        check(
            "SchedulerManager._fire_locked manual run preserves once job next_ts",
            abs(float(after_manual["next_ts"]) - (now + 600)) < 1e-6 and after_manual["fires"] == 1,
            f"after_manual={after_manual}",
        )

        due_job = mgr.store.add({
            "name": "due1", "prompt": "hi",
            "schedule": {"kind": "every", "interval_s": 300},
            "next_ts": now - 10,
        })
        mgr.lane_busy = lambda: True
        asyncio.run(mgr._fire_locked(mgr.store.get(due_job["id"]), now, reason="due"))
        after_due_busy = mgr.store.get(due_job["id"])
        check(
            "SchedulerManager._fire_locked advances next_ts when due hits busy lane",
            after_due_busy["last_status"] == "skipped_busy" and float(after_due_busy["next_ts"]) > now,
            f"after_due_busy={after_due_busy}",
        )

    sched_clamped = RA._schedule_from({"schedule": {"kind": "every", "interval_s": 1}})
    sched_zero = RA._schedule_from({"schedule": {"kind": "every", "interval_s": 0}})
    check(
        "routers/automations._schedule_from clamps interval_s=1 to 60 and rejects <=0",
        sched_clamped == {"kind": "every", "interval_s": 60} and sched_zero is None,
    )

    # ReDoS immunity
    t0 = time.perf_counter()
    evil_res = AI.detect_tool_request("/a" * 30 + "!")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    check(
        "agent_intent.detect_tool_request immune to ReDoS (<50 ms) and detects Unix path",
        elapsed_ms < 50.0 and evil_res is False and AI.detect_tool_request("open /tmp/foo/bar.py") is True,
        f"elapsed_ms={elapsed_ms:.2f}",
    )

    # gguf_meta loop_count cap
    with tempfile.TemporaryDirectory() as td:
        gp = Path(td) / "evil_loop.gguf"
        _write_test_gguf(gp, kv_entries={
            "llama.block_count": (4, 8),
            "llama.attention.head_count": (4, 8),
            "llama.attention.head_count_kv": (4, 4),
            "llama.embedding_length": (4, 512),
            "llama.context_length": (4, 4096),
            "llama.loop.count": (4, 10**9),
            "llama.loop.start": (4, 2),
            "llama.loop.end": (4, 6),
            "llama.attention.sliding_window_pattern": (9, [0] * 8),
        })
        meta = GM._parse_gguf_file(gp, gp.stat().st_size)
        check(
            "gguf_meta caps absurd loop_count without OOM",
            meta.get("block_count") == 8 and isinstance(meta.get("kv_bytes_per_token_q4"), int),
            f"meta={meta}",
        )

    # peer_ratings trend sign on newest-first logs
    declining_newest_first = [
        {"event": "peer_rating", "rated_agent": "coder", "score": 0.35},
        {"event": "peer_rating", "rated_agent": "coder", "score": 0.55},
        {"event": "peer_rating", "rated_agent": "coder", "score": 0.85},
    ]
    improving_newest_first = list(reversed(declining_newest_first))
    saved_decline: list[dict] = []
    saved_improve: list[dict] = []
    with mock.patch.object(LPR, "get_learned_config", return_value={"temperature": 0.5, "max_tokens": 500}), \
         mock.patch.object(LPR, "get_base_config", return_value={}), \
         mock.patch.object(LPR, "append_learning_log"):
        with mock.patch.object(LPR, "read_learning_log", return_value=declining_newest_first), \
             mock.patch.object(LPR, "save_learned_config", side_effect=lambda _f, _m, _a, c: saved_decline.append(c)):
            asyncio.run(LPR._maybe_adapt_config(
                "m:1b", "coder", 0.35, {"suggested_temp_delta": -0.1, "suggested_tokens_delta": 0, "suggested_prompt_hint": ""},
            ))
        with mock.patch.object(LPR, "read_learning_log", return_value=improving_newest_first), \
             mock.patch.object(LPR, "save_learned_config", side_effect=lambda _f, _m, _a, c: saved_improve.append(c)):
            asyncio.run(LPR._maybe_adapt_config(
                "m:1b", "coder", 0.85, {"suggested_temp_delta": -0.1, "suggested_tokens_delta": 0, "suggested_prompt_hint": ""},
            ))
    check(
        "peer_ratings detects declining newest-first scores and ignores improving scores",
        len(saved_decline) == 1 and saved_decline[0].get("temperature") == 0.4 and saved_improve == [],
        f"decline={saved_decline} improve={saved_improve}",
    )

    # fetch_models cross-origin Authorization strip
    hdr = FM._EtagRedirectHandler()
    orig_req = urllib.request.Request(
        "https://huggingface.co/org/repo/resolve/main/model.gguf",
        headers={"Authorization": "Bearer hf_secret_tok"},
    )
    orig_req.add_unredirected_header("Authorization", "Bearer hf_secret_tok")
    cross_req = hdr.redirect_request(
        orig_req, None, 302, "Found", {"X-Linked-Etag": '"abc123"'},
        "https://cdn-lfs.huggingface.co/blob/model.gguf",
    )
    same_req = hdr.redirect_request(
        orig_req, None, 302, "Found", {},
        "https://huggingface.co:443/org/repo/resolve/main/real.gguf",
    )
    has_auth = lambda r: any(k.lower() == "authorization" for k in getattr(r, "headers", {})) or any(  # noqa: E731
        k.lower() == "authorization" for k in getattr(r, "unredirected_hdrs", {})
    )
    check(
        "_EtagRedirectHandler strips Authorization on cross-origin redirect and preserves on same-origin",
        hdr.etag == '"abc123"' and cross_req is not None and not has_auth(cross_req)
        and same_req is not None and has_auth(same_req),
    )


# ── 8. Frontend Node VM Behavior Checks ─────────────────────────────────────
def test_frontend_js_fixes() -> None:
    node = shutil.which("node")
    if not node:
        check("node available for frontend VM checks", False, "node missing")
        return

    js_test = r"""
const assert = require('assert');
const vm = require('vm');
const fs = require('fs');
const path = require('path');

const ROOT = process.cwd();
const transportSrc = fs.readFileSync(path.join(ROOT, 'static', 'js', 'stream_transport.js'), 'utf8');
const settingsSrc = fs.readFileSync(path.join(ROOT, 'static', 'js', 'settings_io.js'), 'utf8');

function mkEl(tag) {
  const el = {
    tagName: tag.toUpperCase(),
    id: '',
    className: '',
    dataset: {},
    style: {},
    children: [],
    attributes: {},
    textContent: '',
    value: '',
    disabled: false,
    title: '',
    isConnected: true,
    setAttribute(k, v) { this.attributes[k] = String(v); },
    appendChild(c) { c._parent = this; this.children.push(c); return c; },
    insertBefore(c, _ref) { this.children.unshift(c); return c; },
    remove() { if (this._parent) this._parent.children = this._parent.children.filter(x => x !== this); },
    querySelector(sel) {
      if (sel === '[data-ap-label]') return this._find(x => x.attributes['data-ap-label'] === '1');
      if (sel === '[data-ap-preview]') return this._find(x => x.attributes['data-ap-preview'] === '1');
      if (sel === 'input') return this._find(x => x.tagName === 'INPUT');
      return null;
    },
    querySelectorAll(sel) {
      const out = [];
      const walk = (n) => {
        for (const c of n.children || []) {
          if (sel === 'button' && c.tagName === 'BUTTON') out.push(c);
          if (sel === 'button[data-ap-approve="1"]' && c.tagName === 'BUTTON' && c.dataset.apApprove === '1') out.push(c);
          walk(c);
        }
      };
      walk(this);
      return out;
    },
    _find(pred) {
      for (const c of this.children) {
        if (pred(c)) return c;
        const sub = c._find ? c._find(pred) : null;
        if (sub) return sub;
      }
      return null;
    },
    addEventListener() {},
  };
  return el;
}

(async () => {
  // (A) Staged approval disables Approve while generating, enables on finalize, poll skips stale runId
  {
    const chat = mkEl('div');
    chat.id = 'chat';
    let pollFn = null;
    const pollResolvers = [];
    const decPosts = [];
    const ctx = {
      S: { currentRunId: 'run-A' },
      console, setTimeout, clearTimeout, Date,
      setInterval(fn) { pollFn = fn; return 1; },
      clearInterval,
      sessionStorage: { getItem: () => '', setItem() {} },
      MutationObserver: function() { return { observe() {}, disconnect() {} }; },
      scrollBtmIfNearBottom() {},
      _readAloud() {},
      document: {
        hidden: false,
        body: mkEl('body'),
        createElement: mkEl,
        getElementById(id) {
          if (id === 'chat') return chat;
          const search = (n) => {
            for (const c of n.children || []) {
              if (c.id === id) return c;
              const hit = search(c);
              if (hit) return hit;
            }
            return null;
          };
          return search(chat);
        },
      },
      fetch(url, opts) {
        if (String(url).startsWith('/approval/pending/')) {
          return new Promise((res) => pollResolvers.push(() => res({ json: async () => ({ active: false }) })));
        }
        if (String(url).startsWith('/approval/decide/')) {
          decPosts.push(JSON.parse(opts.body));
          return Promise.resolve({ json: async () => ({ routed: 'awaiting_args' }) });
        }
        return Promise.resolve({ ok: true, json: async () => ({}) });
      },
    };
    vm.runInNewContext(transportSrc, ctx);
    ctx._renderApprovalCard({ run_id: 'run-A', tool: 'run_bash', preview: '(generating, content streams in the panel)' });
    const card = ctx.document.getElementById('approval-request-card');
    assert.ok(card, 'card rendered');
    const approveBtns = card.querySelectorAll('button[data-ap-approve="1"]');
    assert.strictEqual(approveBtns.length, 2);
    assert.ok(approveBtns.every(b => b.disabled === true), 'approve buttons must be disabled while generating');
    const allBtns = card.querySelectorAll('button');
    assert.strictEqual(allBtns[2].disabled, false, 'deny must stay enabled');

    // Finalize preview -> buttons re-enabled
    ctx._renderApprovalCard({ run_id: 'run-A', tool: 'run_bash', preview: 'echo hi', approval_key: 'k1', decision_id: 'd1' });
    assert.ok(approveBtns.every(b => b.disabled === false), 'approve buttons must re-enable after finalize');

    // Stale poll test: poll for run-A resolves AFTER switching to run-B
    pollFn();
    ctx.S.currentRunId = 'run-B';
    ctx._renderApprovalCard({ run_id: 'run-B', tool: 'write_file', preview: 'a.py', approval_key: 'k2', decision_id: 'd2' });
    pollResolvers[0]();
    await new Promise(r => setTimeout(r, 15));
    const cardAfter = ctx.document.getElementById('approval-request-card');
    assert.ok(cardAfter && cardAfter.dataset.runId === 'run-B', 'stale poll must not remove new run card');
  }

  // (B) settings_io.js sends ui_rev: 0 and reports !ok HTTP errors
  {
    const mLoad = settingsSrc.match(/S\.mcpClientEnabled[\s\S]*?if \(s\.settings_rev[\s\S]*?\}\n/);
    const mQueue = settingsSrc.match(/let _settingsPatchQueue[\s\S]*?\nfunction postSettings[\s\S]*?\n\}/);
    assert.ok(mLoad && mQueue, 'settings slices found');
    const fetchedBodies = [];
    const infos = [];
    let failNext = false;
    const ctx = {
      S: {},
      console, setTimeout, clearTimeout,
      showInfo(msg) { infos.push(msg); },
      refreshVoiceFlags() {},
      fetch(_url, opts) {
        fetchedBodies.push(JSON.parse(opts.body));
        if (failNext) {
          return Promise.resolve({ ok: false, status: 409, json: async () => ({ error: 'conflict' }) });
        }
        return Promise.resolve({ ok: true, json: async () => ({ settings_rev: 0 }) });
      },
    };
    vm.runInNewContext(mQueue[0], ctx);
    vm.runInNewContext('var s = { settings_rev: 0, mcp_client_enabled: false };\n' + mLoad[0], ctx);
    assert.strictEqual(ctx.S.settingsRev, 0, 'settingsRev=0 must be preserved on load');
    await ctx.postSettings({ server_port: 8001 });
    assert.strictEqual(fetchedBodies[0].ui_rev, 0, 'ui_rev=0 must be sent');
    failNext = true;
    await ctx.postSettings({ server_port: 8002 });
    assert.ok(infos.some(m => m.includes('conflict')), 'HTTP !ok must surface error via showInfo');
  }
})().catch((e) => { console.error(e); process.exit(2); });
"""
    r = subprocess.run([node, "-e", js_test], cwd=str(ROOT), capture_output=True, text=True, timeout=20)
    check(
        "stream_transport.js & settings_io.js VM behavior tests pass",
        r.returncode == 0,
        f"rc={r.returncode} stderr={r.stderr[-400:]}",
    )


if __name__ == "__main__":
    test_toolcall_sanitizer_and_loops()
    test_prompt_cache_and_session_summary_cache()
    test_approval_awaiting_args_and_bridge()
    test_destructive_gate_expanded_patterns()
    test_windows_netstat_watchdog_autofit()
    test_slots_and_client_recovery()
    test_scheduler_and_mini_fixes()
    test_frontend_js_fixes()
    sys.exit(h.finish("final_release_batch"))
