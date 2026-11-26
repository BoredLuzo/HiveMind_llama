# -*- coding: utf-8 -*-
"""Wave 5 performance, deduplication, and modularization regression suite.

Run: python tests/test_perf_and_cleanup_wave5.py
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402
from backend import gguf_meta as GM  # noqa: E402
from backend import llama_models as LM  # noqa: E402
from core import duo_runner as DR  # noqa: E402
from core import model_sampling as MS  # noqa: E402
from core import tool_executor as TE  # noqa: E402
from deploy import _common as DCOM  # noqa: E402
from hive_functions.memory import Memory  # noqa: E402
from hivemind_gateway import bridge as GWB  # noqa: E402
from hivemind_gateway.bridge_commands import _BridgeCommandsMixin  # noqa: E402
from hivemind_gateway.bridge_relay import _BridgeRelayMixin  # noqa: E402
from hivemind_gateway.bridge_voice import _BridgeVoiceMixin  # noqa: E402
from infra import child_env as CE  # noqa: E402
from infra import proc_kill as PK  # noqa: E402
from model_configs import profile_validation as PV  # noqa: E402
import routers  # noqa: E402
import server as SRV  # noqa: E402
from tests import ui_sources as UI  # noqa: E402
from tools import runner as TR  # noqa: E402
from tools import sandbox as SBX  # noqa: E402
from tools.handlers import _win_shims as WS  # noqa: E402
from tools.handlers import exec_tools as ET  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check


# ── Phase 1: Hot-path caches & invalidations ────────────────────────────────
ov1 = LM._load_overrides()
ov2 = LM._load_overrides()
check("llama_models._load_overrides caches equal dicts", isinstance(ov1, dict) and ov1 == ov2)
LM.invalidate_caches()
check("llama_models.invalidate_caches clears override cache cleanly",
      LM._overrides_cache is None and isinstance(LM._load_overrides(), dict))

with tempfile.TemporaryDirectory(prefix="hm_w5_gguf_") as tmp:
    tdir = Path(tmp)
    gguf_p = tdir / "Demo-Q4_K_M.gguf"
    mmproj_p = tdir / "mmproj-F16.gguf"
    gguf_p.write_bytes(b"GGUF")
    mmproj_p.write_bytes(b"MMPR")
    GM.clear_cache()
    sib1 = GM.detect_sibling_files(gguf_p)
    sib2 = GM.detect_sibling_files(gguf_p)
    check("gguf_meta.detect_sibling_files populates dir cache",
          sib1 == sib2 and sib1.get("mmproj") == str(mmproj_p) and bool(GM._SIBLING_DIR_CACHE))
    GM.clear_cache()
    check("gguf_meta.clear_cache clears _SIBLING_DIR_CACHE", not GM._SIBLING_DIR_CACHE)

check("child_env lru_caches expose cache_info",
      hasattr(CE._engine_venv_dir_cached, "cache_info")
      and hasattr(CE._clean_path_entries, "cache_info"))
_c1 = CE._clean_path_entries("C:/a;C:/b;C:/a", "")
_c2 = CE._clean_path_entries("C:/a;C:/b;C:/a", "")
check("child_env._clean_path_entries caches hits",
      _c1 == _c2 and CE._clean_path_entries.cache_info().hits >= 1)

check("tools.sandbox._k32 uses maxsize=1 lru_cache",
      hasattr(SBX._k32, "cache_info") and SBX._k32.cache_info().maxsize == 1)

mem = Memory()
rec_a = {"insight": "alpha beta shader compilation cache"}
rec_b = {"insight": "gamma delta vulkan queue sync"}
snap1 = [rec_a, rec_b]
r1 = mem._fts_rank_cached("alpha shader", snap1, 5)
conn1 = mem._fts_conn
r2 = mem._fts_rank_cached("gamma vulkan", snap1, 5)
conn2 = mem._fts_conn
check("Memory._fts_rank_cached reuses SQLite FTS conn on unchanged snapshot",
      conn1 is not None and conn1 is conn2 and r1 and r1[0][0] == 0 and r2 and r2[0][0] == 1)
rec_c = {"insight": "epsilon router extraction helper"}
snap2 = [rec_a, rec_b, rec_c]
r3 = mem._fts_rank_cached("epsilon router", snap2, 5)
check("Memory._fts_rank_cached rebuilds index when snapshot changes",
      r3 and r3[0][0] == 2)


# ── Phase 2: Shared helpers & deduplication ─────────────────────────────────
_kill_calls: list = []


async def _fake_to_thread(fn, *args, **kwargs):
    _kill_calls.append((fn, args, kwargs))
    return None


asyncio.run(PK.kill_tree(SimpleNamespace(pid=None), to_thread=_fake_to_thread))
asyncio.run(PK.kill_tree(SimpleNamespace(pid=424242), force=True, to_thread=_fake_to_thread))
check("infra.proc_kill.kill_tree handles empty pid and forwards kill action",
      inspect.iscoroutinefunction(PK.kill_tree)
      and (len(_kill_calls) == 1 if sys.platform == "win32" else len(_kill_calls) == 0))

check("profile_validation accepts valid sampling block and rejects out-of-range",
      PV.validate_sampling_block({"thinking": {"temperature": 0.6, "reasoning_effort": "low"}}) is None
      and PV.validate_sampling_block({"thinking": {"temperature": 99.0}}) is not None
      and PV.validate_sampling_block({"thinking": {"reasoning_effort": "turbo"}}) is not None)
caps_ok, caps_err = PV.validate_capabilities_dict({"thinking": True, "vision": None}, drop_none=True)
caps_bad, caps_bad_err = PV.validate_capabilities_dict({"thinking": "yes"})
check("profile_validation.validate_capabilities_dict validates bools",
      caps_err is None and caps_ok == {"thinking": True} and caps_bad is None and caps_bad_err is not None)

with tempfile.TemporaryDirectory(prefix="hm_w5_json_") as tmp:
    jf = Path(tmp) / "cfg.json"
    DCOM.atomic_write_text(jf, json.dumps({"ok": True, "v": 5}))
    loaded_ok = DCOM.load_json_dict(jf)
    jf.write_text("{corrupt", encoding="utf-8")
    loaded_bad = DCOM.load_json_dict(jf)
    check("deploy._common atomic_write_text + load_json_dict round-trip and recover {}",
          loaded_ok == {"ok": True, "v": 5} and loaded_bad == {})

check("model_sampling.format_reasoning_effort_status formats none/positive/unsupported",
      "none (composer override" in (MS.format_reasoning_effort_status("inkling", "none", supported=True) or "")
      and "high (composer override)" in (MS.format_reasoning_effort_status("inkling", "high", supported=True) or "")
      and "does not support reasoning" in (MS.format_reasoning_effort_status("lfm", "high", supported=False) or "")
      and MS.format_reasoning_effort_status("inkling", "auto", supported=True) is None)

p_dis_empty = {"reasoning_effort": "high"}
MS.set_disabled_reasoning_effort(p_dis_empty, "", empty_keeps_none=True)
p_dis_strip = {"reasoning_effort": "high"}
MS.set_disabled_reasoning_effort(p_dis_strip, "", empty_keeps_none=False)
check("model_sampling.set_disabled_reasoning_effort handles empty model flag",
      p_dis_empty.get("reasoning_effort") == "none" and "reasoning_effort" not in p_dis_strip)

sc_skip = TE._scan_verify_history([
    {"role": "tool", "name": "edit_file", "content": "ok"},
    {"role": "tool", "name": "run_tests", "content": "[TEST-RESULT] ⏭ No testable code changed"},
])
sc_fail = TE._scan_verify_history([
    {"role": "tool", "name": "write_file", "content": "ok"},
    {"role": "tool", "name": "run_bash", "content": "[TOOL_ERROR: RUN_BASH_TIMEOUT]\ntimed out"},
    {"role": "tool", "name": "write_file_append", "content": "ok"},
])
check("tool_executor._scan_verify_history tracks edit, skip, timeout, and append",
      sc_skip[0] == 0 and sc_skip[1] == 1 and sc_skip[2] is False and sc_skip[3] is True
      and sc_fail[0] == 2 and sc_fail[4] is True and sc_fail[6] == 2)


# ── Phase 3: Modular splits & persistent state ──────────────────────────────
route_paths: set[str] = set()
for _r in SRV.app.routes:
    _p = getattr(_r, "path", "")
    if _p:
        route_paths.add(_p)
    _orig = getattr(_r, "original_router", None)
    if _orig is not None:
        for _sr in getattr(_orig, "routes", ()) or ():
            _sp = getattr(_sr, "path", "")
            if _sp:
                route_paths.add(_sp)
check("routers export and mount openai_compat, voice, and gateway routers",
      all(n in routers.__all__ for n in ("openai_compat_router", "voice_router", "gateway_router"))
      and {"/v1/chat/completions", "/tts", "/stt", "/gateway/status"} <= route_paths
      and SRV.v1_chat_completions is not None
      and SRV.gateway_start is not None
      and SRV.tts_speak is not None)

check("tools.handlers._win_shims extracted and re-exported on exec_tools",
      ET._normalize_win_bash_venv_paths is WS._normalize_win_bash_venv_paths
      and ET._build_win_shell_cmd is WS._build_win_shell_cmd
      and ET._tail_slice is WS._tail_slice)

check("tools.runner decomposes _run_inline_tool into 3 helpers",
      callable(TR._prepare_inline_call)
      and callable(TR._enforce_read_write_guards)
      and inspect.iscoroutinefunction(TR._post_dispatch_inline_hooks))

check("RunBridge composes voice, relay, and command mixins with re-exported constants",
      issubclass(GWB.RunBridge, (_BridgeVoiceMixin, _BridgeRelayMixin, _BridgeCommandsMixin))
      and GWB.FINAL_CHUNK_LIMIT == 3
      and bool(GWB.STOP_REASON_TEXT)
      and "auto" in GWB.MODE_CHOICES)

duo_exec_src = inspect.getsource(DR._duo_exec_round_task)
check("duo_runner._duo_exec_round_task builds fresh ToolRoundState per round",
      "_chunk_trs" not in duo_exec_src and "_trs = ToolRoundState(" in duo_exec_src)

preloop_src = (ROOT / "core" / "duo_coder_preloop.py").read_text(encoding="utf-8")
check("duo_coder_preloop pins _coder_ctx_override when VRAM/timeout cascade downshifts ctx",
      "if _coder_ctx_try < _coder_ctx_post:\n                                _coder_ctx_override = _coder_ctx_try" in preloop_src
      and preloop_src.index("_coder_ctx_override = _coder_ctx_try") < preloop_src.index("_coder_ctx_post = _coder_ctx_try"))

# LlamaClient slot_ref cleanup when _recover_model_port raises on 404 or ConnectError
from backend import llama_client as LC  # noqa: E402


class _CountingSlot:
    def __init__(self, port: int):
        self.port = port
        self.touches = 0
        self.marks = 0
        self.clears = 0

    def touch(self):
        self.touches += 1

    def mark_in_flight(self):
        self.marks += 1

    def clear_in_flight(self):
        self.clears += 1


async def _test_llama_slot_ref_no_double_release():
    client = LC.LlamaClient()
    slot_a = _CountingSlot(8891)
    orig_get_slot = LC.manager.get_slot_by_port
    orig_ensure = LC.manager.ensure_loaded
    orig_recover = client._recover_model_port
    orig_post = client._client.post

    async def _fake_ensure(*a, **kw):
        return 8891

    async def _failing_recover(*a, **kw):
        raise RuntimeError("recover_boom")

    async def _post_404(*a, **kw):
        return SimpleNamespace(status_code=404, text="not found")

    try:
        LC.manager.get_slot_by_port = lambda p: slot_a if p == 8891 else None
        LC.manager.ensure_loaded = _fake_ensure
        client._recover_model_port = _failing_recover
        client._client.post = _post_404
        caught = ""
        try:
            await client.chat("demo:1b", [{"role": "user", "content": "hi"}])
        except RuntimeError as exc:
            caught = str(exc)
        sref = [_CountingSlot(8891)]
        exc_caught = ""
        try:
            await client._handle_transient_exc(
                kind="chat", req_id="r1", model="demo:1b", ctx=4096,
                port=8891, slot_ref=sref, vision=False,
                exc=LC.httpx.ConnectError("down"), attempt=0, max_attempts=3,
            )
        except RuntimeError as exc2:
            exc_caught = str(exc2)
        return (
            caught == "recover_boom"
            and slot_a.marks == 1
            and slot_a.clears == 1
            and exc_caught == "recover_boom"
            and sref[0] is None
        )
    finally:
        LC.manager.get_slot_by_port = orig_get_slot
        LC.manager.ensure_loaded = orig_ensure
        client._recover_model_port = orig_recover
        client._client.post = orig_post
        await client._client.aclose()


check("LlamaClient releases slot once (no leak/double-release) if _recover_model_port fails",
      asyncio.run(_test_llama_slot_ref_no_double_release()))


# RunBridge concurrent approval latch + mixin patch resolution + partial thinking flush
class _MemState:
    def __init__(self):
        self.data = {}

    def save(self):
        return None


async def _test_gateway_fixes():
    sent_msgs: list[str] = []
    send_gate = asyncio.Event()
    send_started = asyncio.Event()

    class _SlowMs:
        owner_id = 1

        async def send_message(self, text, **kw):
            sent_msgs.append(str(text))
            send_started.set()
            await send_gate.wait()
            return {"message_id": len(sent_msgs)}

        async def send_document(self, data, filename):
            sent_msgs.append(f"DOC:{filename}")

    class _FakeHive:
        async def journal(self, *a, **kw):
            return {"active": True, "done": False, "aborted": False, "ts": 100.0, "run_id": "r-j"}

        async def settings(self):
            return {"telegram_result_mode": "text"}

        async def append_transcript(self, *a, **kw):
            return SimpleNamespace(status_code=200)

    br = GWB.RunBridge(
        _FakeHive(), _MemState(),
        SimpleNamespace(voice_out=False, force_approval_gate=True, status_min_interval_s=1.0),
        _SlowMs(),
    )
    ev = {"run_id": "r1", "decision_id": "d1", "tool": "run_bash", "preview": "ls -la", "kind": "approval"}
    t1 = asyncio.create_task(br._relay_own_approval(ev))
    await send_started.wait()
    latched_during_send = isinstance(br._open_own_approval, dict) and br._open_own_approval.get("rid") == "r1"
    await br._relay_own_approval(ev)  # concurrent call while first send_message is awaiting
    send_gate.set()
    await t1
    no_dup_card = latched_during_send and len(sent_msgs) == 1

    # Mixin resolves STALE_JOURNAL_S and time via hivemind_gateway.bridge
    orig_stale = GWB.STALE_JOURNAL_S
    orig_time_mod = GWB.time
    try:
        GWB.time = SimpleNamespace(time=lambda: 200.0, monotonic=orig_time_mod.monotonic)
        GWB.STALE_JOURNAL_S = 50.0
        busy_stale = await br._journal_busy()
        GWB.STALE_JOURNAL_S = 500.0
        busy_active = await br._journal_busy()
        mixin_patched = (busy_stale is None) and isinstance(busy_active, str) and "r-j" in busy_active
    finally:
        GWB.STALE_JOURNAL_S = orig_stale
        GWB.time = orig_time_mod

    # Partial _flush_thinking_pair clears planner_think even if Reasoning raises TelegramApiError
    flushed_labels: list[str] = []

    async def _partial_fail_flush(buf: str, *, label: str = ""):
        flushed_labels.append(label)
        if label == "Reasoning":
            raise GWB.TelegramApiError("sendMessage", "rate limit")

    br._flush_thinking = _partial_fail_flush  # type: ignore[method-assign]
    tp = ["plan_buf", "reason_buf"]
    raised_tg = False
    try:
        await br._flush_thinking_pair(tp)
    except GWB.TelegramApiError:
        raised_tg = True
    partial_cleared = raised_tg and flushed_labels == ["Planning", "Reasoning"] and tp[0] == "" and tp[1] == "reason_buf"
    return no_dup_card and mixin_patched and partial_cleared


check("RunBridge latches approval before send_message, respects bridge globals in mixins, clears sent planning think",
      asyncio.run(_test_gateway_fixes()))


# Router checks: mcp load_settings sensitivity, chats persist_chat _save_chat->None, git body whitespace fallback
import core.state as _cs  # noqa: E402
from routers import chats as RCH  # noqa: E402
from routers import git as RGIT  # noqa: E402
from routers import mcp as RMCP  # noqa: E402

_orig_cs_settings = _cs.settings
_orig_rmcp_load = RMCP.load_settings
try:
    _cs.settings = {"mcp_servers": []}
    RMCP.load_settings = lambda: {"mcp_client_enabled": False, "mcp_servers": [{"name": "srv1", "command": "echo"}]}
    mcp_res = asyncio.run(RMCP.mcp_servers())
    check("routers.mcp respects load_settings() even when core.state.settings is populated",
          isinstance(mcp_res.get("configured"), list) and len(mcp_res["configured"]) == 1)
finally:
    _cs.settings = _orig_cs_settings
    RMCP.load_settings = _orig_rmcp_load


async def _test_chats_and_git_router():
    import threading
    orig_ensure = RCH._ensure_cache
    orig_lock = RCH._cache_lock
    orig_load = RCH._load_chat_full
    orig_save = RCH._save_chat
    try:
        RCH._ensure_cache = lambda: None
        if RCH._cache_lock is None:
            RCH._cache_lock = threading.RLock()
        RCH._load_chat_full = lambda cid: {"id": cid, "title": "Old", "messages": [], "created_at": "2026-01-01T00:00:00"}
        RCH._save_chat = lambda cid, chat, base_rev=None: None
        req = SimpleNamespace(
            json=lambda: asyncio.sleep(0, result={
                "chat_id": "abcd1234", "messages": [{"role": "user", "content": "hi"}],
            })
        )
        res_persist = await RCH.persist_chat(req)
        chats_ok = res_persist.get("updated") is True and res_persist.get("created") is False
    finally:
        RCH._ensure_cache = orig_ensure
        RCH._cache_lock = orig_lock
        RCH._load_chat_full = orig_load
        RCH._save_chat = orig_save

    with tempfile.TemporaryDirectory(prefix="hm_w5_ws_") as tmp_ws:
        async def _fake_json():
            return {"workspace": "   "}

        req_git = SimpleNamespace(
            method="POST",
            json=_fake_json,
            query_params={"workspace": tmp_ws},
        )
        _body, resolved_ws, err_resp = await RGIT._git_body_ws(req_git)
        git_ok = (err_resp is None) and (Path(resolved_ws).resolve() == Path(tmp_ws).resolve())
    return chats_ok and git_ok


check("routers.chats avoids Create fallback on _save_chat->None and routers.git falls back past blank body workspace",
      asyncio.run(_test_chats_and_git_router()))

md_src = (ROOT / "static" / "js" / "markdown.js").read_text(encoding="utf-8")
check("markdown.js trailing timer clears _liveDirty and resets _liveMdTick",
      "body._liveDirty = false;" in md_src and "_liveMdTick = 0;" in md_src)

# Check openai_compat narrowed excepts include httpx.HTTPError, RuntimeError, AttributeError
oac_src = (ROOT / "routers" / "openai_compat.py").read_text(encoding="utf-8")
check("openai_compat catches httpx.HTTPError / RuntimeError / AttributeError in narrowed handlers",
      "except (json.JSONDecodeError, TypeError, KeyError, ValueError, AttributeError):" in oac_src
      and "except (httpx.HTTPError, OSError, ValueError, TypeError, RuntimeError, KeyError, AttributeError) as e:" in oac_src
      and "except (httpx.HTTPError, OSError, RuntimeError, json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError):" in oac_src
      and "except (httpx.HTTPError, OSError, ValueError, TypeError, RuntimeError, KeyError, AttributeError) as _e:" in oac_src)

ui_rels = [p.relative_to(ROOT).as_posix() for p in UI.ui_js_files()]
check("split UI modules registered in index.html load order with zero orphans",
      all(
          m in ui_rels
          for m in (
              "static/js/panels_learning.js",
              "static/js/panels_token_stats.js",
              "static/js/panels_automations.js",
              "static/js/panels_special_agents.js",
          )
      )
      and not UI.orphan_static_js_files())

sys.exit(h.finish("perf_and_cleanup_wave5"))
