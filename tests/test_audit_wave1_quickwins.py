# -*- coding: utf-8 -*-
"""Regression suite for Audit Wave 1 high-impact quick-win fixes.

Covers all 12 verified Wave 1 fixes with zero live LLM / network calls:
  1. tools/patch_2_fuzzy_edit.py: >=20-line match not at line 0 + order-independent near-tie rejection
  2. static/app.js: _cpSyntaxHighlight single-pass tokenizer (no broken HTML attrs on strings/comments)
  3. tools/runner.py: relative path existence + diff_for resolved against workspace, not CWD
  4. tools/definitions.py: fit_tools_to_budget preserves all 5 Tier-0 core tools under tight budgets
  5. core/chat_tools_route.py: ToolLoopConfig receives require_tool_call=False
  6. core/pipeline_runner.py: session persistence runs on normal synthesizer path (not only skip path)
  7. core/duo_compress_cycle.py: _coder_real_at_msgs_len resets & inventory re-anchors on normal compress
  8. core/chat_run.py: _pt_key_for_phase("direct_tools") and ctx_meter emitted via emit() update peaks
  9. core/duo/_pre_explore.py: no "ctx.mode" typo key remains in emitted event dicts
 10. hive_functions/pre_explore/{contracts,partition}.py + planner.py: fields preserved, "no" stubbed, bool touched
 11. infra/mcp_server.py: edit_file TOOL_SCHEMAS uses array edits + old_text/new_text
 12. bench/runner.py + bench/external/run_external.py: _check_timeout_s >60s & cached_input_tokens
"""
from __future__ import annotations

import ast
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.ui_sources import read_all_ui_js  # noqa: E402
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


# ── 1. fuzzy_replace >=20L + order-independent ambiguity ─────────────────────

def test_fuzzy_replace_large_and_order() -> None:
    from tools.patch_2_fuzzy_edit import fuzzy_replace

    lines = [f"item_{i} = compute_step({i})" for i in range(60)]
    content = "\n".join(lines) + "\n"
    res0 = fuzzy_replace(content, "\n".join(lines[0:20]), "replaced_0 = True")
    res10 = fuzzy_replace(content, "\n".join(lines[10:30]), "replaced_10 = True")
    check("fuzzy_replace: 20-line match works at line 0 AND line 10",
          res0 is not None and "replaced_0 = True" in res0
          and res10 is not None and "replaced_10 = True" in res10
          and "item_9 = compute_step(9)" in res10 and "item_30 = compute_step(30)" in res10,
          f"res0={bool(res0)} res10={bool(res10)}")

    # Order-independent near-tie check (<20 lines)
    a = "def alpha_block():\n    step_one(1234567890)\n    return 1\n"
    b = "def alpha_block():\n    step_one(1234567891)\n    return 1\n"
    old_near = "def alpha_block():\n    step_one(1234567890)\n    return 2"
    check("fuzzy_replace: near-tie ambiguity rejected in BOTH A->B and B->A order",
          fuzzy_replace(a + "\n" + b, old_near, "X") is None
          and fuzzy_replace(b + "\n" + a, old_near, "X") is None)

    # Disjoint ambiguous >=20-line duplicates still rejected
    dup20 = "\n".join(f"shared_{i} = {i}" for i in range(20))
    doc_dup = dup20 + "\n\n# gap\n\n" + dup20 + "\n"
    check("fuzzy_replace: disjoint duplicate 20-line blocks rejected",
          fuzzy_replace(doc_dup, dup20, "REPLACED") is None)


# ── 2. _cpSyntaxHighlight single-pass tokenizer ──────────────────────────────

def test_cp_syntax_highlight() -> None:
    node = shutil.which("node")
    if not node:
        check("cpSyntaxHighlight: node skipped", True)
        return
    app_js = read_all_ui_js()
    idx_esc = app_js.index("function _escHtml(")
    idx_hl = app_js.index("function _cpSyntaxHighlight(")
    end_hl = app_js.index("\n// Findet den _cpFiles-Key", idx_hl)
    snippet = app_js[idx_esc:idx_esc + 450].split("\n\n")[0] + "\n" + app_js[idx_hl:end_hl]
    driver = snippet + r'''
const samples = [
  'const x = "hello"; # trailing comment',
  'const t = """docstring # hash """;',
  'if (a < b && c > d) { foo("x"); }',
  "// line comment with 'quotes' and 123"
];
const out = samples.map(_cpSyntaxHighlight);
console.log(JSON.stringify(out));
'''
    proc = subprocess.run([node, "-e", driver], capture_output=True, text=True, check=True)
    rendered = json.loads(proc.stdout)
    s0, s1, s2, _s3 = rendered
    balanced = all(r.count("<span ") == r.count("</span>") for r in rendered)
    check("cpSyntaxHighlight: balanced spans, no corrupted style attribute, escapes HTML",
          balanced
          and 'color:<span' not in s0
          and 'color:<span' not in s1
          and 'style="color:#9a8">&quot;hello&quot;</span>' in s0
          and 'style="color:#5a7a5a"># trailing comment</span>' in s0
          and 'style="color:#7ea">&quot;&quot;&quot;docstring # hash &quot;&quot;&quot;</span>' in s1
          and "&lt; b &amp;&amp; c &gt;" in s2,
          f"s0={s0!r} s1={s1!r} s2={s2!r}")


# ── 3. tools/runner.py workspace-relative diff_for ───────────────────────────

def test_runner_workspace_diff_for() -> None:
    from tools.runner import _run_inline_tool, _files_read_in_run, _files_seen_in_run
    from tools.workspace import new_transaction

    with tempfile.TemporaryDirectory(prefix="hm_w1_diff_", ignore_cleanup_errors=True) as td:
        ws = Path(td)
        (ws / "sub").mkdir()
        target = ws / "sub" / "mod.py"
        target.write_text("val = 1\n", encoding="utf-8")
        # CWD stays repo root (!= ws) so CWD-relative diff_for would have returned ""
        assert Path.cwd().resolve() != ws.resolve()
        tok_r = _files_read_in_run.set(set())
        tok_s = _files_seen_in_run.set(set())
        try:
            tx = new_transaction()
            tx.begin()
            res_edit = asyncio.run(_run_inline_tool(
                "edit_file",
                {"path": "sub/mod.py", "old_text": "val = 1", "new_text": "val = 2"},
                workspace_lock=str(ws), tool_mode="duo_full",
            ))
            tx.commit()
            tx2 = new_transaction()
            tx2.begin()
            res_patch = asyncio.run(_run_inline_tool(
                "apply_patch",
                {"patch": "--- a/sub/mod.py\n+++ b/sub/mod.py\n@@ -1,1 +1,1 @@\n-val = 2\n+val = 3\n"},
                workspace_lock=str(ws), tool_mode="duo_full",
            ))
            tx2.commit()
        finally:
            _files_read_in_run.reset(tok_r)
            _files_seen_in_run.reset(tok_s)
        check("runner diff_for: edit_file and apply_patch emit diff when workspace != CWD",
              "[Diff" in res_edit and "+val = 2" in res_edit
              and "[Diff" in res_patch and "+val = 3" in res_patch,
              f"edit={res_edit[-120:]!r} patch={res_patch[-120:]!r}")


# ── 4. fit_tools_to_budget keeps all 5 Tier-0 core tools ─────────────────────

def test_tier0_tools_preserved() -> None:
    from tools.definitions import _get_inline_tools

    shrunk = _get_inline_tools(include_websearch=True, mode="duo_full", budget_chars=500)
    names = {t["function"]["name"] for t in shrunk}
    expected = {"read_file", "edit_file", "write_file", "run_bash", "task_complete"}
    check("fit_tools_to_budget: preserves all 5 Tier-0 tools even at 500 chars",
          expected <= names, f"names={sorted(names)}")


# ── 5. core/chat_tools_route require_tool_call=False ─────────────────────────

def test_chat_tools_route_no_forced_tool_retry() -> None:
    import core.chat_tools_route as CTR
    import core.state as S

    captured_cfgs = []

    class _FakeToolLoop:
        def __init__(self, *, config, **kwargs):
            captured_cfgs.append(config)
            self.state = types.SimpleNamespace(
                completion_tokens=10, prompt_tokens=20, cached_tokens=0, tool_calls_made=0,
            )

        async def run(self, msgs):
            yield {"type": "token", "content": "Direct plain-text answer."}

    async def _async_emit(ev):
        return ev

    ps = CTR._ChatToolsRouteState(
        judge_toolsize="small",
        models_cache=["qwen3.5:2b"],
        user_input="hi",
        ws_str=str(ROOT),
        run_id="r-w1",
        emit=_async_emit,
        done_event=lambda e, r, **k: {"type": "done", "stop_reason": r},
        collect_done_metrics=lambda: {},
    )

    async def _run():
        out = []
        async for ev in CTR._chat_tools_route(ps):
            out.append(ev)
        return out

    fake_pipe = types.SimpleNamespace(agents={"direct": types.SimpleNamespace(model="qwen3.5:2b")})
    fake_mem = types.SimpleNamespace(add_to_session=lambda *a: None)
    with mock.patch.object(S, "pipeline", fake_pipe), \
            mock.patch.object(S, "memory", fake_mem), \
            mock.patch("backend.llama_server_manager.manager.ensure_loaded", new=mock.AsyncMock(return_value=8101)), \
            mock.patch("core.tool_loop.ToolLoop", new=_FakeToolLoop), \
            mock.patch.object(CTR, "_make_messages", return_value=[{"role": "user", "content": "hi"}]):
        events = asyncio.run(_run())

    check("chat_tools_route: sets require_tool_call=False on ToolLoopConfig",
          len(captured_cfgs) == 1
          and captured_cfgs[0].require_tool_call is False
          and any(e.get("content") == "Direct plain-text answer." for e in events if isinstance(e, dict)),
          f"n={len(captured_cfgs)}")


# ── 6. core/pipeline_runner persists session on synthesizer path ─────────────

def test_pipeline_runner_persists_session() -> None:
    import core.pipeline_runner as PR

    mutated = []

    def _fake_mutate(chat_id, fn):
        d = {}
        fn(d)
        mutated.append((chat_id, d))

    class _Mem:
        def __init__(self):
            self.msgs = []

        def add_to_session(self, role, txt):
            self.msgs.append((role, txt))

        def get_session_messages(self, limit=40, **kw):
            return [{"role": r, "content": t} for r, t in self.msgs[-limit:]]

    class _Ctx:
        chat_id = "chat-pipe-1"
        user_input = "Explain module"
        images = []
        mode = "pipeline"
        iterations = 1
        active_preset = None
        constraint_mode = False
        skip_agents = frozenset()
        use_learned = False
        run_id = "r-pipe-1"
        t_total = 0.0
        vram_budget = 50.0
        vram_lookup_gb = {}
        vram_cache = set()
        models_cache = []
        websearch_available = False
        settings = {"smart_preload_enabled": False}
        registry = {"judge": ""}
        pipeline = types.SimpleNamespace(agents={
            k: types.SimpleNamespace(model="m", temperature=0.2, max_tokens=200,
                                     thinking=False, thinking_budget=0, name=k, key=k)
            for k in ("analyst", "refiner", "critic", "synthesizer", "vision")
        })
        pipeline_soul = ""
        pipeline_mem_ctx = ""
        pipeline_sess_msgs = []
        effective_task_type = "code"
        this_file = "server.py"
        image_description = None
        exec_ctrl = None
        agent_elapsed = {}
        prefetch_state = {}
        prefetch_run_active = False
        memory = _Mem()

        def model_profile(self, _m):
            return {}

        def build_soul_prompt_layer(self, soul, style_only=True):
            return ""

        def registry_get(self, k):
            return "m"

        def get_num_ctx(self, m, r=None):
            return 8192

        def get_effective_prompt_with_override(self, *a):
            return "sys"

        def make_messages(self, *a, **k):
            return [{"role": "user", "content": "x"}]

        def aborted(self):
            return False

        def step_skipped(self):
            return False

        def clear_step_skip(self, r):
            pass

        def is_truncated(self, o):
            return False

        def schedule_prefetch(self, *a):
            pass

        def update_prefetch_lead(self, *a, **kw):
            pass

        def append_learning_log(self, *a):
            pass

        def increment_run_counter(self):
            return 1

        async def maybe_trigger_soul_evolution(self, n):
            return None

        async def emit(self, d):
            return d

        def done_event(self, e, r, **kw):
            return {"type": "done", "stop_reason": r}

        def collect_done_metrics(self):
            return {}

        def unregister_abort(self, r):
            pass

        def unregister_step_skip(self, r):
            pass

        def flush_prefetch_settings(self, s):
            pass

        async def pipeline_chat_stream(self, model, msgs, temp, max_tok, **kw):
            yield f"out-{kw.get('agent_role')}"

    async def _drive(ctx):
        with mock.patch.object(PR, "_bk_evict", new=mock.AsyncMock()), \
                mock.patch.object(PR, "_bk_load", new=mock.AsyncMock()), \
                mock.patch.object(PR, "_get_loaded_models_set", new=mock.AsyncMock(return_value=set())), \
                mock.patch.object(PR, "_refresh_judge_keepalive", new=mock.AsyncMock()), \
                mock.patch.object(PR, "spawn", new=lambda coro, **kw: coro.close() if asyncio.iscoroutine(coro) else None), \
                mock.patch.object(PR, "run_peer_ratings", new=mock.AsyncMock()), \
                mock.patch("context.chat._mutate_chat_context", side_effect=_fake_mutate):
            async for _ev in PR.run_pipeline(ctx):
                pass

    asyncio.run(_drive(_Ctx()))
    check("pipeline_runner: persists session on normal synthesizer run",
          len(mutated) == 1
          and mutated[0][0] == "chat-pipe-1"
          and any(m["content"] == "out-synthesizer" for m in mutated[0][1]["session"]),
          f"mutated={mutated!r}")


# ── 7. duo_compress_cycle: _coder_real_at_msgs_len reset + inventory re-anchor

def test_duo_compress_cycle_reanchor_and_reset() -> None:
    import core.duo_compress_cycle as CC

    with tempfile.TemporaryDirectory(prefix="hm_w1_cc_", ignore_cleanup_errors=True) as td:
        ws = Path(td)
        (ws / "src").mkdir()
        (ws / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")

        class _LRU:
            def reset(self):
                pass

        async def _fake_good_compress(**kwargs):
            return (
                [{"role": "system", "content": "SYS"},
                 {"role": "user", "content": "Summary of src/main.py"}],
                ["src/main.py"],
                {"completion_tokens": 20, "prompt_tokens": 100},
            )

        ctx = types.SimpleNamespace(
            user_input="task",
            settings={},
            duo_config=types.SimpleNamespace(until_finished=False),
            exec_ctrl=types.SimpleNamespace(sync_tool_rounds=lambda n: None),
        )

        async def _emit(d):
            return d

        ctx.emit = _emit
        msgs = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "x" * 6000},
        ]
        real_at_len = [42]
        ps = CC._DuoCompressCycleState(
            _call_sigs=[],
            _coder_est_at_real=[1200],
            _coder_real_prompt_tokens=[1500],
            _coder_real_at_msgs_len=real_at_len,
            _comp_llm_cfg="",
            _comp_llm_timeout_s=30,
            _compress_local_only=False,
            _compress_threshold=2000,
            _di=0,
            _done_tasks=[],
            _dport=8101,
            _dtc=None,
            _dtool_ctx=8192,
            _dtool_sys_eff="SYS",
            _explore_ctx="",
            _goal_pin_msg=None,
            _guard_decision=types.SimpleNamespace(reason="threshold"),
            _guard_tokens=2000.0,
            _last_test_status="",
            _lifetime_tool_rounds=2,
            _max_tool_rounds=20,
            _n_items=1,
            _partial_compression=False,
            _plan_result=None,
            _plan_tracker=None,
            _read_counts={},
            _seen_explore_paths=set(),
            _subtasks=[],
            _tool_ctx_lru=_LRU(),
            _written_files=[],
            _ws_str=str(ws),
            _MAX_COMPRESSIONS=5,
            _dtool_msgs=list(msgs),
            _est_tokens=2000,
            carry={"exec_mdl": "m"},
        )

        async def _run():
            with mock.patch.object(CC, "_compress_tool_context", side_effect=_fake_good_compress), \
                    mock.patch("context.compression._validate_compression_summary", return_value=True):
                async for _ in CC._duo_compress_cycle(ctx, ps):
                    pass

        asyncio.run(_run())
        has_inv = any("[PROJECT FILES" in str(m.get("content", "")) and "src/main.py" in str(m.get("content", ""))
                      for m in ps._dtool_msgs)
        check("duo_compress_cycle: resets _coder_real_at_msgs_len and appends inventory on normal compress",
              real_at_len[0] == 0 and has_inv,
              f"real_at_len={real_at_len} has_inv={has_inv}")


# ── 8. chat_run: ctx_meter via emit() + direct_tools phase timer key ─────────

def test_chat_run_ctx_meter_via_emit() -> None:
    import core.chat_run as CR
    import core.state as S
    import infra.run_control as RC
    import infra.token_stats as TS
    import infra.notify as NTF
    import utils.workspace_resolve as WSR

    records = []

    async def _orch(ctx):
        yield await ctx.emit({"type": "ctx_meter", "est_tokens": 2500, "ctx_limit": 10000})
        yield await ctx.emit({
            "type": "usage_meta", "phase": "direct_tools",
            "prompt_tokens": 100, "completion_tokens": 50, "gen_ms": 1000.0,
        })
        yield await ctx.emit({"type": "token", "content": "hi"})
        yield {"type": "metrics", "metrics": dict(ctx.collect_done_metrics())}
        yield await ctx.emit(ctx.done_event(0.5, "completed"))

    fake_pipe = types.SimpleNamespace(
        agents={k: types.SimpleNamespace(model="m", temperature=0.2, max_tokens=256,
                                         thinking=False, thinking_budget=0, name=k)
                for k in ("analyst", "refiner", "critic", "synthesizer",
                          "vision", "direct", "planner", "coder", "judge")},
        memory=types.SimpleNamespace(as_context_string=lambda: ""),
        _is_list_memory_request=lambda t: False,
        _is_forget_request=lambda t: False,
        _is_memory_request=lambda t: False,
        _is_self_question=lambda t: False,
    )
    fake_mem = types.SimpleNamespace(
        _session_chat_id="", get_session_messages=lambda *a, **k: [],
        add_to_session=lambda *a, **k: None, clear_session=lambda: None,
        seed_session=lambda m: None, list_memories=lambda: [],
        remember=lambda *a: None, forget=lambda *a: None, get_all=lambda: {},
    )
    srv = types.ModuleType("server")
    srv.S_models_cache = []
    srv.THIS_FILE = ROOT / "server.py"
    srv._COMPLEX_DIRECT_MODELS = []
    srv._SIMPLE_DIRECT_MODELS = []
    srv._VRAM_LOOKUP_GB = {}
    srv._WEBSEARCH_AVAILABLE = False
    srv._flush_prefetch_settings = lambda s: None
    srv._get_avg_elapsed = lambda a: 0.0
    srv._maybe_trigger_soul_evolution = lambda n: None
    srv._registry = {"judge": "", "direct": "m"}
    srv._update_prefetch_lead = lambda *a, **k: None
    srv._vision_cfg = {}
    srv.apply_learned_configs_for_run = lambda: None
    srv.get_effective_prompt_with_override = lambda *a, **k: ""

    async def _drive(ws_dir):
        evs = []
        with mock.patch.object(S, "pipeline", fake_pipe), \
                mock.patch.object(S, "memory", fake_mem), \
                mock.patch.dict(sys.modules, {"server": srv}), \
                mock.patch.dict(S.settings, {"smart_preload_enabled": False}), \
                mock.patch.object(CR, "run_stream_orchestrated", new=_orch), \
                mock.patch.object(WSR, "save_last_workspace", new=lambda ws: None), \
                mock.patch.object(WSR, "sync_env_workspace", new=lambda ws: None), \
                mock.patch.object(TS, "_record_run", side_effect=lambda rec: records.append(rec)), \
                mock.patch.object(NTF, "notify_run_completed", new=lambda *a: None), \
                mock.patch.object(NTF, "notify_run_stopped", new=lambda *a: None):
            async for ev in CR.run_stream(
                "hi", [], "chat", 1, None, force_complexity="simple", request_workspace=str(ws_dir),
            ):
                evs.append(ev)
            await asyncio.sleep(0.25)
        for ev in evs:
            if isinstance(ev, str) and ev.startswith("data: "):
                d = json.loads(ev[6:])
                if d.get("type") == "run_id" and d.get("run_id"):
                    RC._unregister_abort(d["run_id"])
                    RC._unregister_step_skip(d["run_id"])
        return evs

    with tempfile.TemporaryDirectory(prefix="hm_w1_cr_", ignore_cleanup_errors=True) as td:
        evs = asyncio.run(_drive(Path(td)))
    metrics = next((e["metrics"] for e in evs if isinstance(e, dict) and e.get("type") == "metrics"), {})
    rec = records[0] if records else {}
    check("chat_run: ctx_meter via emit() populates peak metrics & direct_tools maps off coder",
          metrics.get("metrics_ctx_peak_tokens") == 2500
          and metrics.get("metrics_ctx_limit") == 10000
          and metrics.get("metrics_ctx_pressure_peak") == 0.25
          and "coder" not in (rec.get("phases_ms") or {}),
          f"metrics={metrics} phases_ms={rec.get('phases_ms')}")


# ── 9. _pre_explore no "ctx.mode" typo ───────────────────────────────────────

def test_pre_explore_no_ctx_mode_typo() -> None:
    src = (ROOT / "core" / "duo" / "_pre_explore.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "ctx.mode"
    ]
    check("_pre_explore: zero 'ctx.mode' typo keys remain",
          bad == [] and '"mode": "parallel"' in src and '"mode": "sequential"' in src,
          f"bad_lines={bad}")


# ── 10. contracts + planner + partition touched flag ─────────────────────────

def test_contracts_and_planner_toml() -> None:
    from hive_functions.pre_explore.contracts import _validate_contract
    from hive_functions.pre_explore.partition import _is_touched_flag
    from hive_functions.planner import slice_toml_ctx_inline

    c = _validate_contract({
        "partition": "p1",
        "imports_external": ["httpx"],
        "config": "PORT=8000",
        "hint": "Check timeout",
        "entry_points": ["server.py:main"],
        "imports_needed": ["utils"],
        "skipped_files": ["big.bin"],
        "touched_by_task": False,
    }, "p1")
    check("contracts: _validate_contract retains extended fields",
          c.get("imports_external") == ["httpx"]
          and c.get("config") == "PORT=8000"
          and c.get("hint") == "Check timeout"
          and c.get("entry_points") == ["server.py:main"]
          and c.get("imports_needed") == ["utils"]
          and c.get("skipped_files") == ["big.bin"]
          and c.get("touched_by_task") == "no",
          f"c={c!r}")

    toml_ctx = (
        "## Overview\n\n"
        '```toml\n[contract]\npartition = "keep_me"\nfile = "core/keep.py"\ntouched_by_task = "yes"\nbody = "'
        + ("K" * 160)
        + '"\n```\n\n'
        '```toml\n[contract]\npartition = "drop_me"\nfile = "docs/noise.md"\ntouched_by_task = "no"\nbody = "'
        + ("N" * 160)
        + '"\n```\n'
    )
    sliced = slice_toml_ctx_inline(toml_ctx, max_chars=420)
    check("planner: slice_toml_ctx_inline keeps 'yes' block and stubs 'no' block",
          'partition = "keep_me"' in sliced
          and 'partition = "drop_me"' not in sliced
          and "# unlikely: docs/noise.md" in sliced
          and _is_touched_flag("no") is False
          and _is_touched_flag("yes") is True,
          f"sliced={sliced!r}")


# ── 11. MCP edit_file schema ─────────────────────────────────────────────────

def test_mcp_edit_file_schema() -> None:
    from infra import mcp_server as mcp

    ed = next((t for t in mcp.TOOL_SCHEMAS if t.get("name") == "edit_file"), None)
    props = (ed or {}).get("inputSchema", {}).get("properties", {})
    req = (ed or {}).get("inputSchema", {}).get("required", [])
    check("mcp_server: edit_file schema uses array edits + old_text/new_text",
          ed is not None
          and props.get("edits", {}).get("type") == "array"
          and "old_text" in props and "new_text" in props
          and req == ["path"],
          f"ed={ed!r}")


# ── 12. bench _check_timeout_s + codex cached_input_tokens ───────────────────

def test_bench_timeouts_and_codex_cache() -> None:
    from bench import runner as br
    from bench.external import run_external as rext

    with tempfile.TemporaryDirectory(prefix="hm_w1_br_", ignore_cleanup_errors=True) as td:
        cfg = br.Config(tasks_dir=Path(td), out=Path(td) / "o.json", model="m", task_timeout_s=300)
        t_long = br.Task("deepswe_x", "p", "pytest", timeout_s=1800)
        t_short = br.Task("short_x", "p", "pytest", timeout_s=30)
        check("bench/runner: _check_timeout_s preserves 1800s & floors short tasks at 120s",
              br._check_timeout_s(t_long, cfg) == 1800 and br._check_timeout_s(t_short, None) == 120,
              f"got={(br._check_timeout_s(t_long, cfg), br._check_timeout_s(t_short, None))}")

    codex_out = json.dumps({
        "type": "token_count",
        "info": {"total_token_usage": {
            "input_tokens": 600, "cached_input_tokens": 400,
            "output_tokens": 200, "total_tokens": 1200,
        }},
    })
    parsed = rext.parse_codex(codex_out, "")
    check("bench/external: parse_codex reads cached_input_tokens",
          parsed["cached_tokens"] == 400 and parsed["net_tokens"] == 800,
          f"parsed={parsed!r}")


if __name__ == "__main__":
    test_fuzzy_replace_large_and_order()
    test_cp_syntax_highlight()
    test_runner_workspace_diff_for()
    test_tier0_tools_preserved()
    test_chat_tools_route_no_forced_tool_retry()
    test_pipeline_runner_persists_session()
    test_duo_compress_cycle_reanchor_and_reset()
    test_chat_run_ctx_meter_via_emit()
    test_pre_explore_no_ctx_mode_typo()
    test_contracts_and_planner_toml()
    test_mcp_edit_file_schema()
    test_bench_timeouts_and_codex_cache()
    sys.exit(h.finish("audit_wave1_quickwins"))
