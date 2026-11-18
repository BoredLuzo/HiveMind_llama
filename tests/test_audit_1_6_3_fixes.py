# -*- coding: utf-8 -*-
"""Regression suite for v1.6.3 concurrency, performance, and deep-audit fixes."""
from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


# ── 1. core/llm_transport.py: per-port lock + pooled local client ────────────
async def _test_transport_locks_and_pool():
    from core import llm_transport as T

    l1 = T.get_port_lock(8081)
    l2 = T.get_port_lock(8081)
    l3 = T.get_port_lock(8082)
    check("get_port_lock reuses lock per port & isolates distinct ports",
          l1 is l2 and l1 is not l3)

    active_8081 = 0
    max_8081 = 0
    active_any = 0
    max_any = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_8081, max_8081, active_any, max_any
        port = request.url.port
        active_any += 1
        max_any = max(max_any, active_any)
        if port == 8081:
            active_8081 += 1
            max_8081 = max(max_8081, active_8081)
        await asyncio.sleep(0.05)
        if port == 8081:
            active_8081 -= 1
        active_any -= 1
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": f"ok:{port}"}}]
        }, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        r1, r2 = await asyncio.gather(
            T.post_chat_json(8081, {"messages": []}, client=client),
            T.post_chat_json(8081, {"messages": []}, client=client),
        )
        check("same-port post_chat_json serializes to max_in_flight=1",
              max_8081 == 1 and r1.content_text == "ok:8081" and r2.content_text == "ok:8081",
              f" max_8081={max_8081}")

        max_any = 0
        await asyncio.gather(
            T.post_chat_json(8081, {"messages": []}, client=client),
            T.post_chat_json(8082, {"messages": []}, client=client),
        )
        check("different-port post_chat_json calls run concurrently",
              max_any == 2, f" max_any={max_any}")

    c_a = T.get_shared_local_client()
    c_b = T.get_shared_local_client()
    check("get_shared_local_client returns a pooled singleton within a loop",
          c_a is c_b and not c_a.is_closed)


asyncio.run(_test_transport_locks_and_pool())

# Verify cross-event-loop safety (second asyncio.run creates fresh loop locks cleanly)
asyncio.run(asyncio.sleep(0))


async def _test_transport_cross_loop():
    from core import llm_transport as T
    lock = T.get_port_lock(8081)
    async with lock:
        c = T.get_shared_local_client()
        check("port lock and shared client adapt cleanly across new event loops",
              not c.is_closed)


asyncio.run(_test_transport_cross_loop())


# ── 2. infra/run_control.py + core/chat_run.py: top-level run lock & cleanup ─
async def _test_top_run_lock_and_cleanup():
    import core.chat_run as CR
    import core.pipeline_runner as PR
    import core.state as CS
    import infra.run_control as RC
    from utils.workspace_resolve import WorkspaceForceInvalid

    # Lightweight stubs for lazy `from server import ...` and `state.pipeline`
    srv_stub = types.ModuleType("server")
    srv_stub.S_models_cache = []
    srv_stub.THIS_FILE = ROOT / "server.py"
    srv_stub._COMPLEX_DIRECT_MODELS = ()
    srv_stub._SIMPLE_DIRECT_MODELS = ()
    srv_stub._VRAM_LOOKUP_GB = {}
    srv_stub._WEBSEARCH_AVAILABLE = False
    srv_stub._flush_prefetch_settings = lambda: None
    srv_stub._get_avg_elapsed = lambda _a: 1.0
    srv_stub._maybe_trigger_soul_evolution = lambda *_a, **_k: None
    srv_stub._registry = {}
    srv_stub._update_prefetch_lead = lambda *_a, **_k: None
    srv_stub._vision_cfg = {}
    srv_stub.apply_learned_configs_for_run = lambda: None
    srv_stub.get_effective_prompt_with_override = lambda *_a, **_k: ""

    fake_pipe = types.SimpleNamespace(
        agents={k: types.SimpleNamespace(model=f"m_{k}", temperature=0.2,
                                        max_tokens=512, thinking=False,
                                        thinking_budget=0, name=k)
                for k in ("analyst", "refiner", "critic", "synthesizer",
                          "vision", "direct", "planner", "coder", "judge")},
        memory=types.SimpleNamespace(as_context_string=lambda: ""),
        _is_list_memory_request=lambda _t: False,
        _is_forget_request=lambda _t: False,
        _is_memory_request=lambda _t: False,
        _is_self_question=lambda _t: False,
    )
    fake_mem = types.SimpleNamespace(
        _session_chat_id="",
        get_session_messages=lambda *a, **k: [],
        add_to_session=lambda *a, **k: None,
        clear_session=lambda: None,
        seed_session=lambda msgs: None,
        list_memories=lambda: [],
        remember=lambda *a: None,
        forget=lambda *a: None,
        get_all=lambda: {},
    )
    prev_pipe = CS.pipeline
    prev_mem = CS.memory
    CS.init_state(pipeline_obj=fake_pipe, memory_obj=fake_mem)

    ws = Path(tempfile.mkdtemp(prefix="hm_top_lock_"))
    in_flight = 0
    max_in_flight = 0
    run_ids: list[str] = []

    async def _fake_orch(ctx):
        nonlocal in_flight, max_in_flight
        run_ids.append(ctx.run_id)
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.08)
        yield await ctx.emit({"type": "token", "content": "done"})
        in_flight -= 1
        yield await ctx.emit(ctx.done_event(0.01, "completed"))

    async def _drive():
        evs = []
        async for ev in CR.run_stream("hi", [], "chat", 1, None,
                                      force_complexity="simple",
                                      request_workspace=str(ws)):
            evs.append(ev)
        return evs

    try:
        with mock.patch.object(CR, "run_stream_orchestrated", new=_fake_orch), \
                mock.patch("core.chat_run.threading.Thread") as th_mock, \
                mock.patch.object(PR, "run_peer_ratings", new=lambda *a, **k: asyncio.sleep(0)), \
                mock.patch.dict(sys.modules, {"server": srv_stub}):
            th_mock.return_value = types.SimpleNamespace(start=lambda: None)
            ev_a, ev_b = await asyncio.gather(_drive(), _drive())

        check("concurrent run_stream calls serialize (max_in_flight == 1)",
              max_in_flight == 1 and len(run_ids) == 2,
              f" max_in_flight={max_in_flight} run_ids={run_ids}")
        check("queued run emits waiting status and both clean up abort registry",
              any("waiting in queue" in str(e) for e in (ev_a + ev_b))
              and all(RC._abort_event(rid) is None and RC._step_skip_event(rid) is None for rid in run_ids)
              and RC.get_active_top_run_id() == "")

        ev_bad = []
        with mock.patch("utils.workspace_resolve.resolve_workspace",
                        side_effect=WorkspaceForceInvalid("bad")), \
                mock.patch.dict(sys.modules, {"server": srv_stub}):
            async for ev in CR.run_stream("hi", [], "chat", 1, None,
                                          force_complexity="simple",
                                          request_workspace=str(ws)):
                ev_bad.append(ev)
        check("blocked/error run releases top_run_lock cleanly",
              not RC.get_top_run_lock().locked() and any("Workspace invalid" in str(e) for e in ev_bad))
    finally:
        CS.init_state(pipeline_obj=prev_pipe, memory_obj=prev_mem)


asyncio.run(_test_top_run_lock_and_cleanup())


# ── 3. core/telemetry.py & core/agentic_tool_loop.py schema cache ────────────
def _test_telemetry_and_schema_cache():
    from core import telemetry as _tel

    with tempfile.TemporaryDirectory(prefix="hm_tel_") as tmp:
        old_dir = _tel._LOG_DIR
        old_created = _tel._LOG_DIR_CREATED
        try:
            _tel._LOG_DIR = Path(tmp) / "tel"
            _tel._LOG_DIR_CREATED = None
            _tel.append_round({"model": "test:1b", "prompt_tokens": 10})
            first_ready = _tel._LOG_DIR_CREATED
            _tel.append_round({"model": "test:1b", "prompt_tokens": 20})
            jsonl = list(_tel._LOG_DIR.glob("*-test_1b.jsonl"))
            check("telemetry caches _LOG_DIR_CREATED and appends both rows",
                  first_ready == _tel._LOG_DIR and len(jsonl) == 1
                  and len(jsonl[0].read_text(encoding="utf-8").splitlines()) == 2)
        finally:
            _tel._LOG_DIR = old_dir
            _tel._LOG_DIR_CREATED = old_created

    atl_src = (ROOT / "core" / "agentic_tool_loop.py").read_text(encoding="utf-8")
    check("AgenticToolLoop wraps stream with _t_port_lock & caches _schema_chars_cache",
          "_t_port_lock(result[\"dport\"])" in atl_src
          and "_schema_chars_cache" in atl_src
          and "await asyncio.to_thread(_tel.append_round" in atl_src)


_test_telemetry_and_schema_cache()


# ── 4. core/subagent_lite.py + tools/handlers/misc.py ───────────────────────
async def _test_subagent_isolation():
    from core import subagent_lite as SL
    from backend.llama_server_manager import manager as LSM

    fake_busy = types.SimpleNamespace(
        model="lfm3:1.7b-qad", is_running=True, _loading=False, pinned=True, in_flight=1,
        in_flight_active=lambda: True,
    )
    fake_free = types.SimpleNamespace(
        model=None, is_running=False, _loading=False, pinned=False, in_flight=0,
        in_flight_active=lambda: False,
    )

    with mock.patch.object(LSM, "_slots", [fake_busy, fake_free]), \
            mock.patch.object(LSM, "can_fit", return_value=types.SimpleNamespace(ok=True)), \
            mock.patch.object(SL, "_available_ram_gb", return_value=12.0), \
            mock.patch.object(SL, "_gguf_size_gb", return_value=1.5), \
            mock.patch.object(SL, "_cfg", return_value={"subagent_lite_model_ladder": ["lfm3:1.7b-qad", "lfm2.5:2.6b"]}):
        picked, _ctx = await SL.pick_delegate_model()
        check("pick_delegate_model skips busy coder model & selects free smaller ladder model",
              picked == "lfm2.5:2.6b", f" picked={picked}")

    all_busy = [
        types.SimpleNamespace(model="a:1b", is_running=True, _loading=False, pinned=True, in_flight=0),
        types.SimpleNamespace(model="b:2b", is_running=True, _loading=False, pinned=False, in_flight=1),
    ]
    blocked_msg = ""
    with mock.patch.object(LSM, "_slots", all_busy), \
            mock.patch.object(SL, "_available_ram_gb", return_value=12.0), \
            mock.patch.object(SL, "_cfg", return_value={}):
        try:
            await SL.pick_delegate_model()
        except SL.GateBlocked as gb:
            blocked_msg = gb.reason
    check("pick_delegate_model blocks when no free/idle slot can open",
          "no free slot" in blocked_msg, f" reason={blocked_msg!r}")

    evicted_calls: list[str] = []

    async def _fake_evict(m):
        evicted_calls.append(m)

    with mock.patch.object(LSM, "_slots", [fake_free]), \
            mock.patch.object(LSM, "ensure_loaded", side_effect=RuntimeError("boom")), \
            mock.patch.object(LSM, "evict", side_effect=_fake_evict):
        try:
            await SL._run_sub_loop("task", None, "lfm2.5:2.6b", 4096)
        except RuntimeError:
            pass
    check("_run_sub_loop evicts newly claimed slot even when ensure_loaded raises",
          evicted_calls == ["lfm2.5:2.6b"], f" evicted={evicted_calls}")

    evicted_calls.clear()
    fake_existing = types.SimpleNamespace(model="lfm2.5:2.6b", is_running=True, _loading=False, pinned=False, in_flight=0)
    with mock.patch.object(LSM, "_slots", [fake_existing]), \
            mock.patch.object(LSM, "ensure_loaded", return_value=8082), \
            mock.patch.object(LSM, "evict", side_effect=_fake_evict), \
            mock.patch.object(SL, "_call_model", return_value={"role": "assistant", "content": "done", "tool_calls": []}):
        out = await SL._run_sub_loop("task", None, "lfm2.5:2.6b", 4096)
    check("_run_sub_loop does not evict a pre-loaded model & returns summary",
          evicted_calls == [] and "done" in out)

    misc_src = (ROOT / "tools" / "handlers" / "misc.py").read_text(encoding="utf-8")
    check("tools/handlers/misc.py uses English inline fallback note",
          "research inline." in misc_src and "recherchiere" not in misc_src)


asyncio.run(_test_subagent_isolation())


# ── 5. tools/errors.py + core/tool_exec_helpers.py + core/duo_runner.py ─────
def _test_tool_errors_and_hints():
    from tools.errors import tool_call_failed, tool_error_response
    from core.tool_exec_helpers import _inject_tool_error_hints

    check("tool_call_failed recognizes [SYSTEM] escalated hints as failures",
          tool_call_failed("[SYSTEM] edit_file produced no change on 'a.py' 3x.", "edit_file") is True)

    attempts: dict = {}
    retries: dict = {}
    msgs: list = []
    # Trigger RUN_BASH_NONZERO 4x escalation using real tool_error_response format
    res = tool_error_response("RUN_BASH_NONZERO", "exit=1", tool="run_bash")
    for idx in range(4):
        msgs_round = [{"role": "assistant", "content": None, "tool_calls": [{"id": f"c{idx}"}]}]
        out_res, matched = _inject_tool_error_hints(
            "run_bash", {"cmd": "false"}, res, {"id": f"c{idx}"},
            msgs_round, attempts, retries,
        )
        msgs = msgs_round
    check("RUN_BASH_NONZERO 4x delivers escalated [SYSTEM] + NONZERO token in dtool_msgs",
          matched is True
          and any(m.get("role") == "tool"
                  and "[SYSTEM] run_bash produced a non-zero exit code 4x." in m.get("content", "")
                  and "RUN_BASH_NONZERO" in m.get("content", "") for m in msgs),
          f" out={out_res!r}")

    for code, tool in (
        ("WEBSEARCH_FAILED", "web_search"),
        ("WEBFETCH_FAILED", "web_fetch"),
        ("GIT_STATUS_FAILED", "git_status"),
        ("TOOL_NOT_FOUND", "foo"),
        ("REPLACE_LINES_FAILED", "replace_lines"),
    ):
        _, m = _inject_tool_error_hints(
            tool, {}, tool_error_response(code, "fail", tool=tool), {"id": code}, [], {}, {},
        )
        check(f"_inject_tool_error_hints sets _matched=True for {code}", m is True)

    dr_src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
    check("core/duo_runner.py persists wide_read_streak, last_test_outcome & superseded_paths",
          "wide_read_streak=_wide_read_streak" in dr_src
          and "last_test_outcome=_last_test_outcome" in dr_src
          and "superseded_paths=_superseded_paths" in dr_src)


_test_tool_errors_and_hints()


# ── 6. planner.py, partition.py, llama_vram_table.py, llama_start_phases.py ─
async def _test_backend_and_hive_fixes():
    from hive_functions.planner import _strip_toolcall_markup
    cleaned = _strip_toolcall_markup("step 1\n\n\n\n<tool_calls>bad</tool_calls>\n\n\n\nstep 2")
    check("_strip_toolcall_markup collapses newlines without literal brackets",
          "[" not in cleaned and "\n\n\n" not in cleaned and "step 1" in cleaned and "step 2" in cleaned,
          f" cleaned={cleaned!r}")

    from hive_functions.pre_explore import partition as EP
    seen_prompts: list[str] = []

    async def _fake_llm(_mdl, _prt, msgs, **_kw):
        seen_prompts.append(msgs[0]["content"])
        if len(seen_prompts) == 1:
            return {"content": "", "tool_calls": [{"name": "read_file", "args": {"path": "a.py"}}]}
        return {"content": "[contract]\nsummary=\"ok\"", "tool_calls": []}

    with tempfile.TemporaryDirectory(prefix="hm_part_") as ws:
        (Path(ws) / "a.py").write_text("x = 1\n", encoding="utf-8")
        with mock.patch.object(EP, "_llm", side_effect=_fake_llm):
            pres = await EP._explore_partition(
                part={"label": "root", "paths": ["a.py"]},
                slot={"model": "m:1b", "port": 8081},
                worker_idx=0, worker_total=1, sid="w0",
                workspace=ws, tree_ctx="a.py", msg_cap=8,
                aborted_fn=lambda: False, emit_fn=None,
                task="Fix alpha bug", max_tool_rounds=10,
            )
    check("_explore_partition forwards task prompt & counts read_calls",
          seen_prompts and "Fix alpha bug" in seen_prompts[0]
          and pres.get("read_calls", 0) >= 1 and "n_files_read" in pres,
          f" res={pres}")
    ep_src = Path(EP.__file__).read_text(encoding="utf-8")
    check("_worker_drain passes task & max_tool_rounds to _explore_partition",
          "task=task,\n            max_tool_rounds=max_tool_rounds," in ep_src)

    from backend.llama_vram_table import vram_of_moe
    base_4k = vram_of_moe("qwen3.6:35b-a3b-mtp-ud", 4096)
    at_8k = vram_of_moe("qwen3.6:35b-a3b-mtp-ud", 8192)
    at_16k = vram_of_moe("qwen3.6:35b-a3b-mtp-ud", 16384)
    # Pinned contract: measured_bytes_per_token scales linearly across full num_ctx on top of calibrated base
    d1 = round(at_8k - base_4k, 3)
    d2 = round(at_16k - base_4k, 3)
    check("vram_of_moe scales monotonically with num_ctx",
          d1 > 0 and at_16k > at_8k and abs(d2 - 2 * d1) <= 0.02,
          f" base={base_4k} 8k={at_8k} 16k={at_16k}")

    lsp_src = (ROOT / "backend" / "llama_start_phases.py").read_text(encoding="utf-8")
    check("_phase_model_flags uses cached _help_supports for --spec-draft-n-max",
          'await _help_supports("--spec-draft-n-max")' in lsp_src
          and 'subprocess.run, [cmd[0], "--help"]' not in lsp_src)


asyncio.run(_test_backend_and_hive_fixes())


# ── 7. ctx_guard, decider, tree_scout, bridge, chats, vram, server, tool, app.js
async def _test_remaining_fixes():
    from context.ctx_guard import plan_partial_cut_index, _CPT
    chars_per_msg = int(100 * _CPT)
    msgs = [{"role": "system", "content": "sys"}] + [
        {"role": "assistant" if i % 2 == 0 else "tool", "content": "x" * chars_per_msg}
        for i in range(6)
    ]
    # total=500, target=int(1000*0.3)=300 => to_remove = 200 tokens.
    # Each non-sys msg contributes 100 * 2.0 = 200 tokens, so linear _ratio cuts at idx 2
    # (squared _ratio computed to_remove=400 and cut at idx 3).
    cut = plan_partial_cut_index(
        msgs, ctx_tokens=1000, guard_tokens=500,
        target_post_fraction=0.3, min_tail_msgs=2, token_ratio=2.0,
    )
    check("plan_partial_cut_index applies linear token_ratio (not squared)",
          cut == 2, f" cut={cut}")

    from routing import decider as D
    seen_wp: list[bool] = []
    fake_enc = types.SimpleNamespace(
        model_name="fake-enc",
        decide=lambda text, want_planner=False: (seen_wp.append(want_planner) or ("agentic", 0.9, {})),
    )
    fake_cache = types.SimpleNamespace(get=lambda _k: None, put=lambda _k, _v: None)
    with mock.patch.object(D, "backend", return_value=fake_enc), \
            mock.patch.object(D, "cache", return_value=fake_cache):
        await D.decide_async({"routing_decider": "on", "routing_planner": "auto"}, "build feature x")
        await D.decide_async({"routing_decider": "on", "routing_planner": "decider"}, "build feature y")
    check("decide_async passes want_planner only when routing_planner=='decider'",
          seen_wp == [False, True], f" seen_wp={seen_wp}")

    from hive_functions.tree_scout import partition_tree
    with tempfile.TemporaryDirectory(prefix="hm_ts_") as ws:
        (Path(ws) / ".git").mkdir()
        (Path(ws) / ".git" / "huge").write_text("x", encoding="utf-8")
        (Path(ws) / "main.py").write_text("print(1)\n", encoding="utf-8")
        walk_visited: list[str] = []
        orig_walk = importlib.import_module("os").walk

        def _spy_walk(top, *a, **kw):
            for root, dirs, files in orig_walk(top, *a, **kw):
                walk_visited.append(Path(root).name)
                yield root, dirs, files

        with mock.patch("hive_functions.tree_scout.os.walk", side_effect=_spy_walk):
            partition_tree("main.py", workspace_root=ws)
        check("partition_tree prunes .git via os.walk instead of rglob('*')",
              ".git" not in walk_visited, f" visited={walk_visited}")

    bridge_src = (ROOT / "hivemind_gateway" / "bridge.py").read_text(encoding="utf-8")
    check("bridge allows /tools mcp and deduplicates agent & planner_result branches",
          '"mcp"' in bridge_src
          and bridge_src.count('elif ev.get("type") == "agent":') == 1
          and bridge_src.count('elif ev.get("type") == "planner_result":') == 1)

    chats_src = (ROOT / "routers" / "chats.py").read_text(encoding="utf-8")
    check("routers/chats.py _disk_bytes counts _blob_dir(cid)",
          "_blob_dir(cid)" in chats_src)

    vram_src = (ROOT / "routers" / "vram.py").read_text(encoding="utf-8")
    check("routers/vram.py initializes model and _load_ctx before try:",
          '    model = ""\n    _load_ctx = None\n    try:' in vram_src)

    srv_src = (ROOT / "server.py").read_text(encoding="utf-8")
    check("server.py pins force_complexity='simple' and calls _sup.clear() on missing pid",
          'force_complexity="simple"' in srv_src
          and 'if _verdict != "py":\n        _sup.clear()' in srv_src)

    tool_util_src = (ROOT / "utils" / "tool.py").read_text(encoding="utf-8")
    check("utils/tool.py passes model (not _vname) to resolve_write_char_limits",
          "resolve_write_char_limits(model, token_budget" in tool_util_src)

    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    check("static/app.js fixes supervisor interval stack, refreshTgSupStatus, _lh attach, hm_run_id & visibility guards",
          "window.refreshTgSupStatus = gatewaySupervisorTick;" in app_js
          and "clearInterval(window._tgSupIv)" in app_js
          and "hdr.appendChild(_lh);" in app_js
          and "sessionStorage.removeItem('hm_run_id')" in app_js
          and "if (document.hidden || !S.currentRunId) return;" in app_js)


asyncio.run(_test_remaining_fixes())


# ── 8. UI / Critic dropdown / Code label / Decode t/s / .msg-stats / smoke noise ──
def _test_ui_and_metrics_points():
    idx_html = (ROOT / "index.html").read_text(encoding="utf-8")
    check("index.html renames top mode button to Code and adds Critic dropdown in #duo-pair-row",
          ">&#9889; Code</div>" in idx_html
          and ">&#9889; Agentic</div>" not in idx_html
          and 'id="duo-critic-model-row"' in idx_html
          and 'id="duo-critic-model-sel"' in idx_html
          and 'onchange="onDuoCriticModelChange(this.value)"' in idx_html)

    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    check("static/app.js wires Critic selector in duo-pair-row, live decode fallback, and multi-turn msg-stats",
          "function onDuoCriticModelChange(model)" in app_js
          and "duoCriticModel: ''," in app_js
          and "document.getElementById('duo-critic-model-row')" in app_js
          and "querySelectorAll('#chat .ablock.duo-coder:not([data-turn-done=\"1\"])')" in app_js
          and "el.setAttribute('data-turn-done', '1');" in app_js
          and "var lastBlock = blocks[blocks.length - 1];" in app_js
          and "lastBlock.querySelector(':scope > .amsg-foot')" in app_js
          and "else if (d.type === 'planner_token') {\n    _perfOnToken(d.content);" in app_js
          and "var _liveDec = _perfRealRate() || Number(S.perfTokRate || 0);" in app_js)

    # Verify startDuoAgent appends a footer element (.amsg-foot)
    duo_fn = app_js[app_js.index("function startDuoAgent("):app_js.index("function _renderThinkCodeBlocks(")]
    check("startDuoAgent attaches .amsg-foot to each new duo block",
          "_speakWrap.className = 'amsg-foot';" in duo_fn
          and "wrap.appendChild(_speakWrap);" in duo_fn)

    # Backend decode_tps & direct error stop_reason & smoke_test gating
    pl_src = (ROOT / "hive_functions" / "planner.py").read_text(encoding="utf-8")
    dr_src = (ROOT / "core" / "direct_runner.py").read_text(encoding="utf-8")
    cr_src = (ROOT / "core" / "chat_run.py").read_text(encoding="utf-8")
    srv_src = (ROOT / "server.py").read_text(encoding="utf-8")
    check("planner, direct_runner & chat_run emit decode_tps and guard synthetic Direct error / smoke_test runs",
          '"decode_tps": round(_p_dtps, 2)' in pl_src
          and '"decode_tps": round(_dt_tps, 2)' in dr_src
          and '"decode_tps": round(_d_tps_emit, 2)' in dr_src
          and '_direct_had_err and _d_ct_sum == 0 and _d_pt_sum == 0' in dr_src
          and '"decode_tps": round(_p_tps, 2)' in cr_src
          and 'not str(data["content"]).startswith("[Direct error:")' in cr_src
          and 'not bool(_run_settings.get("smoke_test"))' in cr_src
          and 'if "smoke_test" in body:' in srv_src)

    # Behavioral test for ToolLoop._add_usage timings + gen_ms fallback
    from core.tool_loop import ToolLoop, ToolLoopConfig
    tl = ToolLoop(config=ToolLoopConfig(), http_client=None, port=8080, workspace=".")
    tl._add_usage({"prompt_tokens": 100, "completion_tokens": 50}, timings={"predicted_per_second": 41.2}, gen_ms=1200.0)
    check("ToolLoop._add_usage records gen_ms and predicted_per_second",
          abs(tl.state.gen_ms - 1200.0) < 0.1 and abs(tl.state.decode_tps - 41.2) < 0.1,
          f" gen_ms={tl.state.gen_ms} decode_tps={tl.state.decode_tps}")

    # Behavioral test for token_stats noise filtering
    import infra.token_stats as ts
    smoke_err = {"tokens": 45, "requests": 0, "prompt_tokens": 0, "phases": {}, "elapsed_s": 48.8,
                 "models": {"direct": "ling-3.0-tiny"}, "mode": "direct", "stop_reason": "completed"}
    smoke_flag = {"tokens": 92, "requests": 1, "prompt_tokens": 2357, "phases": {"direct_tools": 92},
                  "elapsed_s": 5.3, "models": {"direct": "ling-3.0-tiny"}, "smoke_test": True}
    real_run = {"tokens": 120, "requests": 1, "prompt_tokens": 2100, "phases": {"coder": 120},
                "phases_ms": {"coder": 3000}, "elapsed_s": 4.2, "models": {"coder": "qwen3.5:4b-mtp"},
                "mode": "code_duo", "stop_reason": "completed"}
    daily_probe = {"2026-11-18": {"tokens": 257, "runs": 3, "run_list": [smoke_err, smoke_flag, real_run]}}
    by_mdl = ts.aggregate_by_model(daily_probe)
    by_mod = ts.aggregate_by_mode(daily_probe)
    check("_is_noise_run filters smoke_test & 45-tok 0-req preflight noise while preserving real runs",
          ts._is_noise_run(smoke_err) is True
          and ts._is_noise_run(smoke_flag) is True
          and ts._is_noise_run(real_run) is False
          and len(by_mdl) == 1 and by_mdl[0]["model"] == "qwen3.5:4b-mtp"
          and len(by_mod) == 1 and by_mod[0]["mode"] == "code_duo",
          f" by_mdl={by_mdl} by_mod={by_mod}")


_test_ui_and_metrics_points()


# ── 9. Wave 1-3 Audit, Wiring & Simplification Regressions ───────────────────
async def _test_wave_1_to_3_fixes():
    from routing import model_picker as mp
    _fake_pipe = types.SimpleNamespace(agents={"direct": types.SimpleNamespace(model="ling-3.0-tiny")})
    _op, _os, _osi, _oco = mp.pipeline, mp.settings, mp._SIMPLE_DIRECT_MODELS, mp._COMPLEX_DIRECT_MODELS
    try:
        mp.init_model_picker(_fake_pipe, {"mode": "direct", "automap_excluded": []},
                             simple_direct={"chat": "qwen3.5:4b-mtp"}, complex_direct={"chat": "qwen3.5:4b-mtp"})
        check("_pick_direct_model honors configured direct model when mode != automap",
              mp._pick_direct_model("simple", "chat", ["qwen3.5:4b-mtp", "ling-3.0-tiny"]) == "ling-3.0-tiny")
        mp.init_model_picker(_fake_pipe, {"mode": "automap", "automap_excluded": []})
        check("_pick_direct_model uses automap table in automap mode when direct not excluded",
              mp._pick_direct_model("simple", "chat", ["qwen3.5:4b-mtp", "ling-3.0-tiny"]) == "qwen3.5:4b-mtp")
    finally:
        mp.init_model_picker(_op, _os or {}, _osi, _oco)

    srv_src = (ROOT / "server.py").read_text(encoding="utf-8")
    check("_v1_direct_mode forwards model_overrides, conversation history, and real token usage",
          '_direct_overrides["direct_model"] = _v1_model' in srv_src
          and '_direct_overrides["agents_user_sampling_choice"] = {"direct": ["temperature"]}' in srv_src
          and "Conversation history:" in srv_src and 'if _d.get("type") == "usage_meta":' in srv_src
          and 'model_overrides=_direct_overrides or None' in srv_src)

    vram_router_src = (ROOT / "routers" / "vram.py").read_text(encoding="utf-8")
    check("POST /vram/budget calls sync_loader_settings",
          "from backend.runtime_sync import sync_loader_settings" in vram_router_src
          and "def llama_kill_all" not in vram_router_src)

    from context.compression import _compress_tool_context
    captured: list[dict] = []

    async def _comp_handler(request: httpx.Request) -> httpx.Response:
        import json as _j
        captured.append(_j.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "<think>internal</think>Concise summary."}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 12},
        }, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_comp_handler)) as cl:
        new_msgs, _, _ = await _compress_tool_context(
            [{"role": "user", "content": "build app"}, {"role": "assistant", "content": "x " * 40},
             {"role": "assistant", "content": "tail"}],
            "qwen3.5:4b-mtp", 8081, cl, "sys", "build app", ["a.py"], ["step 1"], keep_recent_msgs=1)
    summary_text = "\n".join(str(m.get("content", "")) for m in new_msgs)
    check("_compress_tool_context sends enable_thinking=False, reasoning_format=none and strips <think>",
          len(captured) == 1 and captured[0].get("reasoning_format") == "none"
          and captured[0].get("chat_template_kwargs") == {"enable_thinking": False}
          and "Concise summary." in summary_text and "<think>" not in summary_text,
          f" summary={summary_text[:120]!r}")

    import json as _j
    from tools.definitions import fit_tools_to_budget, _INLINE_CODING_TOOLS
    tight = fit_tools_to_budget(list(_INLINE_CODING_TOOLS), 2500, mode="duo_core")
    med = fit_tools_to_budget(list(_INLINE_CODING_TOOLS), 25000, mode="duo_core")
    check("fit_tools_to_budget keeps core tools & fits realistic budget cleanly",
          {"read_file", "edit_file"}.issubset({t["function"]["name"] for t in tight})
          and len(tight) < len(_INLINE_CODING_TOOLS) and len(_j.dumps(med)) <= 25000,
          f" tight_count={len(tight)} med_len={len(_j.dumps(med))}")

    from core.subagent_lite import build_tool_schemas, _tool_schemas as _sub_ts
    from core.fix_agent import _tool_schemas as _fix_ts
    from core.duo_coder_preloop import _abort_preloop
    from core.pipeline_runner import _stream_stage_once
    from tools.runner import _apply_pre_decision, _consume_budget
    from tools.handlers.file_ops import _check_edit_shrink, _check_edit_symbol_loss
    check("subagent_lite & fix_agent share build_tool_schemas and extracted helpers exist",
          {t["function"]["name"] for t in _sub_ts()} == {"read_file", "list_dir", "search_code"}
          and "edit_file" in {t["function"]["name"] for t in _fix_ts()}
          and all(callable(fn) for fn in (build_tool_schemas, _abort_preloop, _stream_stage_once,
                                          _apply_pre_decision, _consume_budget,
                                          _check_edit_shrink, _check_edit_symbol_loss)))

    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    check("static/app.js applyAgent syncs duoCtxAgentic/Normal/Critic and engine twin inputs",
          "if (ctx > 0 && key === 'duo_coder')" in app_js
          and "else if (ctx > 0 && key === 'duo_critic')" in app_js
          and "function syncEngineTwinInputs" in app_js)


asyncio.run(_test_wave_1_to_3_fixes())

sys.exit(h.finish("audit_1_6_3_fixes"))
