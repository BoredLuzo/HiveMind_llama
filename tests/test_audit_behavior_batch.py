# -*- coding: utf-8 -*-
"""Behavior regressions for post-1.5.5 deep-audit fixes (2026-11-12).

Covers real runtime paths (no live LLM or GPU needed):
  1. ToolLoop: settings forwarding, resolve_presence_penalty override vs cap,
     explicit temperature=0.0 preserved, allow_send_file=False -> SEND_FILE_UNAVAILABLE
  2. model_sampling: symmetric repeat_penalty / repetition_penalty normalization
  3. llama_config: is_moe_model alias/case normalization & sync_runtime_backend
  4. llama_start_phases: ubatch downgrade runs after peer-evict loop
  5. add_model / models_registry / routers.config: _resolve_gguf_path relative paths,
     _canonical_from_file multi-underscore stems, _validate_model_profile_key whitelist
  6. duo_dtool_setup & duo_coder_preloop: _normalize_path_set, settings_dict forwarded
     into deep retrieval, retrieval block suppresses [PRE-EXPLORE MISSING], and
     duo_lean omits apply_patch/write_file_append from WRITE RULES
  7. bench.runner._hivemind_body: pinned settings & direct_ctx_target propagation
"""
import asyncio
import json
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True)
check = h.check


# ── 1. ToolLoop: temperature=0.0, presence_penalty cap/override, allow_send_file ──
from core.tool_loop import ToolLoop, ToolLoopConfig  # noqa: E402


class _MockToolServer:
    def __init__(self, *, emit_send_file: bool = False):
        self.emit_send_file = emit_send_file
        self.bodies: list[dict] = []
        self._calls = 0

    def start(self):
        parent = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                parent.bodies.append(json.loads(raw))
                parent._calls += 1
                if parent.emit_send_file and parent._calls == 1:
                    msg = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call_sf_1",
                            "type": "function",
                            "function": {
                                "name": "send_file",
                                "arguments": json.dumps({"path": "out.txt"}),
                            },
                        }],
                    }
                else:
                    msg = {"role": "assistant", "content": "OK"}
                payload = json.dumps({
                    "choices": [{"message": msg}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv


async def _drive_tool_loop(port: int, cfg: ToolLoopConfig, ws: str = ""):
    events = []
    async with httpx.AsyncClient() as client:
        loop = ToolLoop(config=cfg, http_client=client, port=port, workspace=ws)
        async for ev in loop.run([{"role": "user", "content": "go"}]):
            events.append(ev)
        return loop.state, events


# 1a: explicit temperature=0.0 & settings override presence_penalty=0.65 & send_file denied
_srv1 = _MockToolServer(emit_send_file=True)
_httpd1 = _srv1.start()
try:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _td:
        (Path(_td) / "out.txt").write_text("hi", encoding="utf-8")
        _send_file_schema = [{
            "type": "function",
            "function": {
                "name": "send_file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }]
        _st1, _evs1 = asyncio.run(_drive_tool_loop(
            _httpd1.server_address[1],
            ToolLoopConfig(
                stream=False,
                max_rounds=3,
                max_post_attempts=1,
                model="fake:1b",
                temperature=0.0,
                tools=_send_file_schema,
                tool_mode="duo_core",
                allow_send_file=False,
                settings={"_model_sampling_overrides": {"fake:1b": {"presence_penalty": 0.65}}},
                require_tool_call=False,
            ),
            ws=_td,
        ))
    _tr_ev = [e for e in _evs1 if e.get("type") == "tool_result"]
    check("ToolLoop: explicit temperature=0.0 reaches upstream payload",
          bool(_srv1.bodies) and _srv1.bodies[0].get("temperature") == 0.0,
          f" ({_srv1.bodies[0].get('temperature') if _srv1.bodies else None})")
    check("ToolLoop: settings override presence_penalty=0.65 honored",
          bool(_srv1.bodies) and _srv1.bodies[0].get("presence_penalty") == 0.65,
          f" ({_srv1.bodies[0].get('presence_penalty') if _srv1.bodies else None})")
    check("ToolLoop: allow_send_file=False emits SEND_FILE_UNAVAILABLE",
          any("SEND_FILE_UNAVAILABLE" in f"{e.get('text', '')} {e.get('full', '')}" for e in _tr_ev),
          f" ({_tr_ev})")
finally:
    _httpd1.shutdown()
    _httpd1.server_close()

# 1b: built-in profile presence_penalty > 0.2 capped at 0.2 when not overridden
_srv2 = _MockToolServer(emit_send_file=False)
_httpd2 = _srv2.start()
try:
    with mock.patch(
        "core.tool_loop.get_sampling_profile",
        return_value={"temperature": 0.6, "presence_penalty": 0.9},
    ):
        asyncio.run(_drive_tool_loop(
            _httpd2.server_address[1],
            ToolLoopConfig(
                stream=False,
                max_rounds=1,
                max_post_attempts=1,
                model="fake:1b",
                tools=[],
                tool_mode="",
                settings={},
                require_tool_call=False,
            ),
        ))
    check("ToolLoop: unoverridden high profile presence_penalty capped at 0.2",
          bool(_srv2.bodies) and _srv2.bodies[0].get("presence_penalty") == 0.2,
          f" ({_srv2.bodies[0].get('presence_penalty') if _srv2.bodies else None})")
finally:
    _httpd2.shutdown()
    _httpd2.server_close()


# ── 2. model_sampling: symmetric repeat_penalty / repetition_penalty ────────────
from core.model_sampling import _pick_profile_subdict, get_sampling_profile  # noqa: E402

_smp_rep = get_sampling_profile(
    "qwen3.5:4b",
    settings={"_model_sampling_overrides": {"qwen3.5:4b": {"repetition_penalty": 1.09}}},
)
_smp_rpt = get_sampling_profile(
    "qwen3.5:4b",
    settings={"_model_sampling_overrides": {"qwen3.5:4b": {"repeat_penalty": 1.04}}},
)
check("model_sampling: repetition_penalty override syncs to repeat_penalty",
      _smp_rep.get("repetition_penalty") == 1.09 and _smp_rep.get("repeat_penalty") == 1.09,
      f" ({_smp_rep.get('repeat_penalty')}/{_smp_rep.get('repetition_penalty')})")
check("model_sampling: repeat_penalty override syncs to repetition_penalty",
      _smp_rpt.get("repeat_penalty") == 1.04 and _smp_rpt.get("repetition_penalty") == 1.04,
      f" ({_smp_rpt.get('repeat_penalty')}/{_smp_rpt.get('repetition_penalty')})")


# ── 3. llama_config: is_moe_model normalization & sync_runtime_backend ─────────
import backend.llama_config as _lc               # noqa: E402
import backend.llama_server_manager as _lsm       # noqa: E402
import backend.manager_load as _ml                # noqa: E402
import backend.llama_start_phases as _lsp         # noqa: E402

with mock.patch.dict(_lc._MOE_EXPERT_COUNTS, {"foomodel": 8}, clear=False), \
     mock.patch("model_configs.models_registry.get_moe_pin", return_value=None):
    check("is_moe_model normalizes :latest and #slot case-insensitively",
          _lc.is_moe_model("FOOMODEL:latest") is True and _lc.is_moe_model("FooModel#2") is True)

_orig_gb, _orig_bin = _lc.GPU_BACKEND, _lc.LLAMA_BIN
try:
    _dummy_bin = Path("C:/nonexistent/bin/llama-server")
    _lc.sync_runtime_backend("cuda", _dummy_bin)
    check("sync_runtime_backend updates GPU_BACKEND and LLAMA_BIN across modules",
          _lc.GPU_BACKEND == "cuda"
          and _lsm.GPU_BACKEND == "cuda"
          and _ml.GPU_BACKEND == "cuda"
          and _lsp.GPU_BACKEND == "cuda"
          and Path(_lsp.LLAMA_BIN) == _dummy_bin,
          f" ({_lc.GPU_BACKEND}/{_lsm.GPU_BACKEND}/{_ml.GPU_BACKEND}/{_lsp.GPU_BACKEND})")
finally:
    _lc.sync_runtime_backend(_orig_gb, _orig_bin)


# ── 4. llama_start_phases: ubatch downgrade runs AFTER peer eviction loop ───────
class _StopAfterPreflight(RuntimeError):
    """Sentinel halting _phase_preflight_and_spawn before opening log files."""


def _make_preflight_mgr(can_fit_fn, peers):
    slot = types.SimpleNamespace(
        slot_id=0, model="m", is_running=False, pinned=False, _loading=False,
        port=8199, _num_ctx=4096, last_used=0.0, in_flight=0,
    )
    mgr = types.SimpleNamespace(
        _slots=[slot, *peers],
        can_fit=can_fit_fn,
        _metric_inc=lambda *_a: None,
        _adopt_orphan_slots=mock.AsyncMock(return_value=0),
        _pre_flight_grace_recheck=mock.AsyncMock(
            side_effect=lambda *a, **kw: can_fit_fn(a[0], a[1], extra_needed_mib=kw.get("extra_needed_mib", 0.0))
        ),
        _last_orphan_scan_ts=0.0,
    )
    return mgr, slot


# 4a: eviction frees VRAM so raised ub_eff=1024 stays intact
_evicted_ids: list[int] = []
_peer_slot = types.SimpleNamespace(
    slot_id=1, model="peer", is_running=True, pinned=False, _loading=False,
    port=8200, _num_ctx=4096, last_used=1.0, in_flight=0,
)


def _fit_after_evict(_m, _ctx, **_kw):
    ok = 1 in _evicted_ids
    return types.SimpleNamespace(ok=ok, needed_mib=3000.0, margin_mib=512, free_mib=4000.0 if ok else 2000.0, source="test")


async def _fake_kill(victim):
    _evicted_ids.append(victim.slot_id)
    victim.is_running = False


_cmd_keep = ["llama", "--ubatch-size", "1024", "--batch-size", "1024"]
_mgr_a, _slot_a = _make_preflight_mgr(_fit_after_evict, [_peer_slot])
with mock.patch.object(_lc, "VRAM_PREFLIGHT_GATE", True), \
     mock.patch.object(_lsp, "_kill_slot_async", side_effect=_fake_kill), \
     mock.patch.object(_lsp, "wait_for_vram_reclaim", new=mock.AsyncMock(return_value=False)), \
     mock.patch("backend.manager_load._pre_flight_evict_candidates",
                side_effect=lambda slots, ex: [s for s in slots if s is not ex and s.is_running]), \
     mock.patch("backend.manager_load._archive_old_slot_logs", side_effect=_StopAfterPreflight()):
    try:
        asyncio.run(_lsp._phase_preflight_and_spawn(
            _mgr_a, _slot_a, _cmd_keep, "m:1b", Path("m.gguf"), 4096,
            False, 1, True, "m", "none", None, 300.0, False,
            ub_base=512, ub_eff=1024, ub_src="test", ub_delta_mib=300.0,
        ))
    except _StopAfterPreflight:
        pass
check("ubatch stays 1024 when peer eviction frees enough VRAM",
      _evicted_ids == [1] and _cmd_keep[_cmd_keep.index("--ubatch-size") + 1] == "1024",
      f" (evicted={_evicted_ids}, cmd={_cmd_keep})")


# 4b: still tight after eviction loop -> falls back to ub_base=512
def _fit_only_base(_m, _ctx, **kw):
    extra = float(kw.get("extra_needed_mib", 0.0) or 0.0)
    ok = extra <= 50.0
    return types.SimpleNamespace(ok=ok, needed_mib=3000.0 + extra, margin_mib=512, free_mib=3600.0, source="test")


_cmd_fb = ["llama", "--ubatch-size", "1024", "--batch-size", "1024"]
_mgr_b, _slot_b = _make_preflight_mgr(_fit_only_base, [])
with mock.patch.object(_lc, "VRAM_PREFLIGHT_GATE", True), \
     mock.patch.object(_lsp, "wait_for_vram_reclaim", new=mock.AsyncMock(return_value=False)), \
     mock.patch("backend.manager_load._pre_flight_evict_candidates", return_value=[]), \
     mock.patch("backend.manager_load._archive_old_slot_logs", side_effect=_StopAfterPreflight()):
    try:
        asyncio.run(_lsp._phase_preflight_and_spawn(
            _mgr_b, _slot_b, _cmd_fb, "m:1b", Path("m.gguf"), 4096,
            False, 1, True, "m", "none", None, 300.0, False,
            ub_base=512, ub_eff=1024, ub_src="test", ub_delta_mib=300.0,
        ))
    except _StopAfterPreflight:
        pass
check("ubatch falls back to baseline 512 after eviction loop when only baseline fits",
      _cmd_fb[_cmd_fb.index("--ubatch-size") + 1] == "512"
      and _cmd_fb[_cmd_fb.index("--batch-size") + 1] == "1024",
      f" (cmd={_cmd_fb})")


# ── 5. add_model relative GGUF path, _canonical_from_file, _validate_model_profile_key ──
from deploy.add_model import _resolve_gguf_path              # noqa: E402
from model_configs.models_registry import _canonical_from_file  # noqa: E402
from routers.config import _validate_model_profile_key      # noqa: E402

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _td_m:
    _mdir = Path(_td_m)
    (_mdir / "sub").mkdir()
    _gguf = _mdir / "sub" / "demo.gguf"
    _gguf.write_bytes(b"GGUF")
    _resolved_gguf = _resolve_gguf_path("sub/demo.gguf", _mdir)
    check("add_model _resolve_gguf_path resolves relative GGUF inside search root",
          _resolved_gguf == _gguf.resolve(), f" ({_resolved_gguf})")

check("_canonical_from_file uses rsplit('_', 1) for multi-underscore filenames",
      _canonical_from_file(Path("my_custom_v2_Q4.json"), {}) == "my_custom_v2:Q4")
check("_validate_model_profile_key blocks reserved identity key 'model'",
      isinstance(_validate_model_profile_key("model", "x"), str))
check("_validate_model_profile_key blocks unknown profile keys",
      isinstance(_validate_model_profile_key("arbitrary_bad_key", 1), str))
check("_validate_model_profile_key rejects mismatched field types",
      isinstance(_validate_model_profile_key("moe", "yes"), str))
check("_validate_model_profile_key accepts valid typed field",
      _validate_model_profile_key("moe", True) is None)


# ── 6. duo_dtool_setup & duo_coder_preloop ────────────────────────────────────
from core.duo_dtool_setup import (  # noqa: E402
    _DuoDtoolSetupState,
    _duo_dtool_setup,
    _normalize_path_set,
)
from core.duo_coder_preloop import _DuoCoderPreloopState, _duo_coder_preloop  # noqa: E402

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _td_ds:
    _norm = _normalize_path_set(["./alpha.py", str(Path(_td_ds) / "pkg" / "beta.py")], _td_ds)
    _exp_norm = {
        (Path(_td_ds) / "alpha.py").resolve().as_posix().lower(),
        (Path(_td_ds) / "pkg" / "beta.py").resolve().as_posix().lower(),
    }
    check("_normalize_path_set unifies rel and abs workspace paths",
          _norm == _exp_norm, f" ({_norm})")

    _captured_ds_settings = []

    def _fake_deep_ctx(ws, task_text, *, settings_dict=None, **_kw):
        _captured_ds_settings.append(settings_dict)
        return ("## Deep Retrieval\nalpha.py: def f()", ["alpha.py"])

    _sentinel_settings = {"duo_deep_retrieval": True, "marker": "yes"}
    _ctx_ds = types.SimpleNamespace(
        duo_config=types.SimpleNamespace(
            until_finished=False,
            pass_explore_files="all",
            important_task=False,
            chunking=False,
        ),
        pipeline=types.SimpleNamespace(agents={"duo_coder": types.SimpleNamespace(max_tokens=2048)}),
        settings=_sentinel_settings,
        use_learned=False,
        user_input="Update alpha function",
        chat_id="",
        emit=mock.AsyncMock(return_value={"type": "status"}),
    )
    _ps_ds = _DuoDtoolSetupState(
        _di=0,
        _coder_caps=types.SimpleNamespace(explore_inject=2000, goal_pin=200),
        _coder_msgs=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Update alpha function"},
        ],
        _explore_ctx="",
        _pre_explore_msgs=[],
        _contracts_raw=[],
        _touched_paths=set(),
        _tree_ctx="",
        _is_first_outer_round=True,
        _ws_str=_td_ds,
        carry={
            "coder_mdl": "qwen3.5:4b",
            "_duo_coder_sys": "sys",
            "_duo_coder_tok": 2048,
            "_duo_runtime_profile": "default",
            "_coder_ctx_eff": 8192,
            "_coder_map_budget": 1000,
        },
    )

    async def _run_ds():
        with mock.patch("core.duo_dtool_setup._build_preseed_context", return_value=("", [])), \
             mock.patch("core.duo_dtool_setup._build_deep_retrieval_context", side_effect=_fake_deep_ctx), \
             mock.patch("tools.runner._set_write_budget"):
            async for _ in _duo_dtool_setup(_ctx_ds, _ps_ds):
                pass

    asyncio.run(_run_ds())
    _joined_msgs = "\n".join(str(m.get("content", "")) for m in _ps_ds._dtool_msgs)
    check("duo_dtool_setup forwards ctx.settings as settings_dict to deep retrieval",
          _captured_ds_settings == [_sentinel_settings], f" ({_captured_ds_settings})")
    check("duo_dtool_setup retrieval block suppresses [PRE-EXPLORE MISSING]",
          "## Deep Retrieval" in _joined_msgs and "[PRE-EXPLORE MISSING]" not in _joined_msgs,
          f" ({_joined_msgs[:160]!r})")


async def _run_preloop(tier: str) -> str:
    _fake_timer = types.SimpleNamespace(skip=lambda *_a: None, start=lambda *_a: None, stop=lambda *_a: None)
    _fake_cfg = types.SimpleNamespace(
        agentic_mode=False,
        tool_thinking=False,
        coder_images_off=True,
        until_finished=False,
    )
    _ctx_p = types.SimpleNamespace(
        settings={"duo_coder_tool_tier": tier, "duo_evict_planner_on_coder": False},
        duo_config=_fake_cfg,
        pipeline=types.SimpleNamespace(
            agents={"duo_coder": types.SimpleNamespace(max_tokens=2048)},
        ),
        user_input="fix bug",
        run_id="r1",
        chat_id="",
        t_total=0.0,
        phase_timer=_fake_timer,
        emit=mock.AsyncMock(return_value={"type": "status"}),
        aborted=lambda: True,
        is_aborted_chat=lambda _cid: False,
        done_event=lambda *a, **kw: {"type": "done"},
        collect_done_metrics=lambda: {},
    )
    _ps_p = _DuoCoderPreloopState(
        coder_mdl="qwen3.5:4b",
        exec_mdl="qwen3.5:4b",
        planner_model="",
        duo_coder_tok=2048,
        ws_str=".",
    )
    _fake_lsm = types.SimpleNamespace(
        _port_alive=mock.AsyncMock(return_value=False),
        list_loaded=mock.AsyncMock(return_value=[]),
        evict=mock.AsyncMock(),
        _find_loaded=mock.AsyncMock(return_value=None),
        ensure_loaded=mock.AsyncMock(return_value=8100),
    )
    with mock.patch("backend.llama_server_manager.manager", _fake_lsm), \
         mock.patch("core.duo_coder_preloop.resolve_ctx", return_value=8192), \
         mock.patch("core.duo_coder_preloop._pre_loop_resume"):
        async for _ in _duo_coder_preloop(_ctx_p, _ps_p):
            pass
    return _ps_p.duo_coder_sys


_sys_lean = asyncio.run(_run_preloop("lean"))
_sys_core = asyncio.run(_run_preloop("core"))
check("duo_coder_preloop omits apply_patch and write_file_append in duo_lean WRITE RULES",
      "apply_patch" not in _sys_lean and "write_file_append" not in _sys_lean,
      f" (len={len(_sys_lean)})")
check("duo_coder_preloop includes apply_patch and write_file_append in duo_core WRITE RULES",
      "apply_patch" in _sys_core and "write_file_append" in _sys_core,
      f" (len={len(_sys_core)})")


# ── 6b. run_context.is_ctx_aborted helper ────────────────────────────────────
from core.run_context import is_ctx_aborted  # noqa: E402

check("is_ctx_aborted detects aborted() directly",
      is_ctx_aborted(types.SimpleNamespace(aborted=lambda: True, chat_id="c1", is_aborted_chat=lambda _: False)) is True)
check("is_ctx_aborted detects chat_id abort via is_aborted_chat",
      is_ctx_aborted(types.SimpleNamespace(aborted=lambda: False, chat_id="c1", is_aborted_chat=lambda cid: cid == "c1")) is True)
check("is_ctx_aborted returns False when neither aborted",
      is_ctx_aborted(types.SimpleNamespace(aborted=lambda: False, chat_id="c1", is_aborted_chat=lambda _: False)) is False)


# ── 7. bench/runner.py _hivemind_body pinned keys + direct_ctx_target ─────────
from bench.runner import Task, _hivemind_body  # noqa: E402

_t_demo = Task(id="demo", prompt="fix", check="pytest")
_body_dir = _hivemind_body(_t_demo, Path("."), duo=False, model="qwen3.5:4b", ctx_tokens=65536)
_body_duo = _hivemind_body(_t_demo, Path("."), duo=True, model="qwen3.5:4b", ctx_tokens=65536)
_expected_shared = (
    "lsp_auto_diagnostics",
    "lsp_auto_warm_on_write",
)
_expected_duo_pins = (
    "duo_pre_explore",
    "duo_parallel_preexplore",
    "duo_git_autocommit",
    "duo_agentic_thinking",
    "duo_thinking_per_chunk",
    "duo_coder_tool_thinking",
    "duo_coder_tool_thinking_auto_mode",
    "duo_critic_tools",
    "duo_runtime_profile",
    "duo_pass_explore_files",
    "duo_planner_ctx_target",
)
_missing_dir = [k for k in (*_expected_shared, "direct_ctx_target") if k not in _body_dir]
_missing_duo = [k for k in (*_expected_shared, *_expected_duo_pins) if k not in _body_duo]
check("bench/runner _hivemind_body sets direct_ctx_target for direct runs",
      not _missing_dir and _body_dir.get("direct_ctx_target") == 65536,
      f" (missing={_missing_dir}, direct_ctx_target={_body_dir.get('direct_ctx_target')})")
check("bench/runner _hivemind_body sets all duo fairness pins for code_duo runs",
      not _missing_duo, f" (missing={_missing_duo})")


# ── 8. MCP mode filter, tick(), LSP/patch/fuzzy/findstr, sampling/MoE/relative ──
import time as _t  # noqa: E402
from backend.llama_config import detect_moe_count  # noqa: E402
from bench.relative import build_relative  # noqa: E402
from core.tool_exec_helpers import _run_bash_fail_fix_pass_insight  # noqa: E402
from scheduler.manager import SchedulerManager  # noqa: E402
from tools.definitions import _filter_tools_for_mode  # noqa: E402
from tools.handlers import code_intel as _ci_mod  # noqa: E402
from tools.handlers.file_ops import _try_convert_json_edits  # noqa: E402
from tools.handlers.lsp import merge_post_write_diagnostics  # noqa: E402
from tools.patch_2_fuzzy_edit import fuzzy_replace  # noqa: E402
from tools.patch_diff import patch_targets  # noqa: E402

_st = [{"type": "function", "function": {"name": "read_file"}},
       {"type": "function", "function": {"name": "mcp_demo_echo"}}]
_names = lambda m: [t["function"]["name"] for t in _filter_tools_for_mode(_st, mode=m)]  # noqa: E731
check("_filter_tools_for_mode preserves mcp_* only in MCP-capable modes",
      "mcp_demo_echo" in _names("duo_mcp") and "mcp_demo_echo" in _names("duo_full")
      and "mcp_demo_echo" not in _names("duo_core") and "mcp_demo_echo" not in _names("duo_readonly"))

with tempfile.TemporaryDirectory() as _td_t:
    _mgr = SchedulerManager(Path(_td_t) / "jobs.json", "http://127.0.0.1:1")
    _now = _t.time()
    _jq = _mgr.store.add({"name": "Q", "prompt": "p", "enabled": True, "policy": "queue",
                          "schedule": {"kind": "every", "interval_s": 60}, "next_ts": _now - 10})
    _js = _mgr.store.add({"name": "S", "prompt": "p", "enabled": True, "policy": "skip",
                          "schedule": {"kind": "every", "interval_s": 60}, "next_ts": _now - 10})
    _mgr.lane_busy = lambda: True
    asyncio.run(_mgr.tick())
    _jqa, _jsa = _mgr.store.get(_jq["id"]), _mgr.store.get(_js["id"])
    check("SchedulerManager.tick advances all due jobs under lane_busy",
          bool(_jqa and _jqa.get("last_status") == "queued" and _jsa
               and _jsa.get("last_status") == "skipped_busy" and float(_jsa.get("next_ts") or 0) > _now))

_merged = merge_post_write_diagnostics(
    "ok\n[lint/python WARNING]\n  x:1\n[AUTO-TEST] ✅ passed\n\n[Diff — what changed]\n```diff\n+a\n```",
    "[LSP OK]", True)
check("merge_post_write_diagnostics keeps [AUTO-TEST] and [Diff]",
      "[lint/" not in _merged and "[AUTO-TEST] ✅ passed" in _merged and "[Diff — what changed]" in _merged)
check("patch_targets excludes created-then-deleted path when include_delete=False",
      patch_targets("--- /dev/null\n+++ b/e.py\n@@ -0,0 +1 @@\n+x\n--- a/e.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n",
                    include_delete=False) == [])
check("fuzzy_replace with empty new_str deletes cleanly",
      fuzzy_replace("a\n    target_line_value = 123\n    second_line_item = 456\nb\n",
                    "    target_line_value = 123\n    second_line_itm = 456", "") == "a\nb\n")
check("_try_convert_json_edits keeps empty deletion replacement",
      "=======\n\n>>>>>>> REPLACE" in _try_convert_json_edits(json.dumps([{"old_str": "x", "new_str": ""}])))

with tempfile.TemporaryDirectory() as _td_s:
    Path(_td_s, "a.txt").write_text("hello /F:bar world\n", encoding="utf-8")
    _cmds: list[list[str]] = []
    async def _fake_sub(*argv, **_kw):  # noqa: E306
        _cmds.append(list(argv))
        return types.SimpleNamespace(communicate=mock.AsyncMock(return_value=(b"", b"")))
    with mock.patch.object(_ci_mod.sys, "platform", "win32"), \
         mock.patch("asyncio.create_subprocess_exec", side_effect=_fake_sub):
        _out = asyncio.run(_ci_mod._inline_tool_search_code({"pattern": "/F:bar", "path": "a.txt"}, Path(_td_s), None))
    check("search_code passes /L and /C:<pat> to findstr on win32",
          any("/L" in c and "/C:/F:bar" in c for c in _cmds) and "hello /F:bar world" in _out)

_sw = types.SimpleNamespace(verify_last_ok_serial=0, verify_mutation_serial=4, last_run_bash_failure=None, changed_since_failure=set())
asyncio.run(_run_bash_fail_fix_pass_insight("run_tests", {}, "[TEST-RESULT] ⚠️ No tests found", _sw, None, None, 0))
check("run_tests warning does not bump verify_last_ok_serial", _sw.verify_last_ok_serial == 0)
check("detect_moe_count normalizes case, :latest, and #slot", detect_moe_count("Qwen3.6:35B-A3B:latest#2") == 35)
check("_pick_profile_subdict with thinking=False avoids thinking fallback",
      _pick_profile_subdict({"thinking": {"temperature": 0.99}, "non_thinking": {"temperature": 0.21}},
                            "sampling_thinking_code", False).get("temperature") == 0.21
      and get_sampling_profile("unknown-demo:1b", thinking=False, mode="sampling_thinking_code")
      == get_sampling_profile("unknown-demo:1b", thinking=False))
_rel = build_relative([
    {"condition": "raw@m1", "task": "t1", "ok": False, "tokens": 100, "cached_tokens": 0, "net_tokens": 100, "rounds": 1},
    {"condition": "aider@m1", "task": "t1", "ok": True, "tokens": 200, "cached_tokens": 200, "net_tokens": 0, "rounds": 2},
    {"condition": "hm@m1", "task": "t1", "ok": True, "tokens": 100, "cached_tokens": 50, "net_tokens": 50, "rounds": 1},
])
check("bench/relative handles @model tags and zero net_tokens",
      "raw" in _rel.get("baselines_present", []) and _rel["conditions"]["hm@m1"]["delta_vs_raw"] == 1.0
      and _rel["conditions"]["aider@m1"]["tokens"]["net_total"] == 0)

# ── 8. Wave-1 modularity, VRAM dense overrides, 120B parse, edit_file hints, cache & stream guards ──
from core.model_sampling import DEFAULT_PROFILE  # noqa: E402
from backend.llama_models import _parse_gguf_filename  # noqa: E402
from backend.llama_vram_table import _extract_size_gb, vram_of, vram_of_with_ctx  # noqa: E402
from learning.peer_ratings import _parse_rating_tune  # noqa: E402
from explore.cache import _pre_explore_cache, _pre_explore_cache_set  # noqa: E402
from core.tool_exec_helpers import _inject_tool_error_hints, _track_file_changes  # noqa: E402
from core.duo_ctx_meter import _ctx_shrink_round  # noqa: E402
from backend.llama_client import LlamaClient  # noqa: E402

check("DEFAULT_PROFILE enables cache_prompt for drop-in models",
      DEFAULT_PROFILE["thinking"]["cache_prompt"] is True
      and DEFAULT_PROFILE["non_thinking"]["cache_prompt"] is True)

check(">=100B model size parses in both _extract_size_gb and _parse_gguf_filename",
      _extract_size_gb("gpt-oss:120b-ud") == 120.0
      and "gpt-oss:120b" in _parse_gguf_filename("gpt-oss-120B-Q4_K_M.gguf"))

with mock.patch("model_configs.models_registry.get_profile",
                return_value={"vram_gb_override": 3.4, "kv_bytes_per_token": 10240}):
    _dv_exp = round(3.4 + (4096 * 10240) / (1024 ** 3), 2)
    check("dense vram_gb_override and kv_bytes_per_token honored in vram_of / vram_of_with_ctx",
          vram_of("custom-dense:4b") == 3.4 and vram_of_with_ctx("custom-dense:4b", 8192) == _dv_exp)

_rt = _parse_rating_tune(
    "score=0.72 strengths=[clean_code] weaknesses=[too_short] temp_delta=-0.05 tokens_delta=100 hint=Check_imports_first reason=better_accuracy"
)
check("_parse_rating_tune returns populated dict incl. hint & reason",
      isinstance(_rt, dict) and _rt.get("score") == 0.72
      and _rt.get("suggested_prompt_hint") == "Check imports first"
      and _rt.get("adapt_reason") == "better_accuracy")

_ck_demo = ("_wave1_ws_test", "m", "t")
_pre_explore_cache.pop(_ck_demo, None)
asyncio.run(_pre_explore_cache_set(_ck_demo, {"ctx": "x" * 40, "msgs": [], "ts": 9e12}))
check("_pre_explore_cache_set caches entries keyed with 'ctx'",
      _ck_demo in _pre_explore_cache)
_pre_explore_cache.pop(_ck_demo, None)

_fc_events = []
_fc_map = {}
_fc_res = types.SimpleNamespace(last_run_bash_failure=None, changed_since_failure=set())
_fc_hooks = types.SimpleNamespace(emit=mock.AsyncMock(side_effect=lambda e: _fc_events.append(e)))
asyncio.run(_track_file_changes(
    "edit_file", {"path": "a.py"}, "[edit_file: 'a.py' applied 3 edits (+4 lines)]",
    _fc_res, _fc_map, [], _fc_hooks, False,
))
check("_track_file_changes tracks batch 'applied N edits' format",
      _fc_map.get("a.py", {}).get("blocks") == 3 and _fc_map.get("a.py", {}).get("lines_added") == 4
      and bool(_fc_events and _fc_events[-1].get("op") == "edited"))

from tools.errors import tool_error_response as _ter  # noqa: E402
_eh_msgs = []
_eh_attempts = {}
_, _handled = _inject_tool_error_hints(
    "edit_file", {"path": "x.py"},
    _ter("EDIT_FILE_OLD_TEXT_NOT_FOUND", "no match", tool="edit_file"),
    {"id": "tc1"}, _eh_msgs, _eh_attempts, {},
)
check("edit_file OLD_TEXT_NOT_FOUND emits active old_text hint (no SEARCH/REPLACE)",
      _handled and any("old_text" in str(m.get("content", "")) and "SEARCH/REPLACE" not in str(m.get("content", ""))
                       for m in _eh_msgs))

_mask_msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
              {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]},
              {"role": "tool", "tool_call_id": "c1", "content": "x" * 1000}]
_mask_res = _ctx_shrink_round(
    dtool_ctx=8192, compress_threshold=500, est_tokens=6500, guard_tokens=6500,
    can_compress=True, force_compress_next=False, total_tool_rounds=1,
    dtool_msgs=_mask_msgs, tool_ctx_lru={"a.py": 1},
    coder_real_prompt_tokens=[6500], coder_est_at_real=[5000],
    recent_focus_paths=set(), mask_enabled=True,
)
check("mask-first does not emit 'no summary needed' when real tokens still exceed threshold",
      _mask_res.compress_ok is True
      and not any("no summary needed" in str(ev.get("content", "")) for ev in _mask_res.events))


import ast  # noqa: E402
_pe_ast = ast.parse((ROOT / "core" / "duo" / "_pre_explore.py").read_text(encoding="utf-8"))
_xloop_rets = [
    len(n.value.elts) if isinstance(n.value, ast.Tuple) else 1
    for fn in ast.walk(_pe_ast)
    if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "_xloop_task"
    for n in ast.walk(fn)
    if isinstance(n, ast.Return) and n.value is not None
]
check("_pre_explore _xloop_task returns consistent 4-tuples",
      bool(_xloop_rets) and all(k == 4 for k in _xloop_rets), str(_xloop_rets))


class _Wave1StreamHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        self.send_response(200)
        if req.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'data: {"choices":[{"delta":null}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                b'data: [DONE]\n\n'
            )
        else:
            body = json.dumps({"choices": []}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


_w1_srv = ThreadingHTTPServer(("127.0.0.1", 0), _Wave1StreamHandler)
threading.Thread(target=_w1_srv.serve_forever, daemon=True).start()
try:
    async def _run_cli_checks():
        _w1_cli = LlamaClient()
        try:
            with mock.patch("backend.llama_client.manager.ensure_loaded",
                            new=mock.AsyncMock(return_value=_w1_srv.server_port)):
                _c_out = await _w1_cli.chat("demo:1b", [{"role": "user", "content": "hi"}])
                _s_out = []
                async for _tok in _w1_cli.chat_stream("demo:1b", [{"role": "user", "content": "hi"}]):
                    _s_out.append(_tok)
                return _c_out, "".join(_s_out)
        finally:
            await _w1_cli._client.aclose()
    _w1_cout, _w1_sout = asyncio.run(_run_cli_checks())
    check("LlamaClient handles empty choices and chunk delta=None cleanly",
          _w1_cout == "" and _w1_sout == "ok")
finally:
    _w1_srv.shutdown()
    _w1_srv.server_close()

sys.exit(h.finish("audit_behavior_batch"))
