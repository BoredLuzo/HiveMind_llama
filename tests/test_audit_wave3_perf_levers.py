# -*- coding: utf-8 -*-
"""Regression suite for Audit Wave 3: high-ROI performance levers.

Covers every Wave 3 performance optimization hermetically:
  1. core/duo/_pre_explore.py: no blind 3.0s/2.5s sleeps, conditional force_kill_all, backend-aware settle
  2. core/duo/preflight.py: in-place unpin_duo_models without ctx-mismatch reloads
  3. model_configs/models_registry.py: zero-deepcopy scalar accessors + mutation isolation + invalidation
  4. routers/models.py: GET /models offloads _build_models_profiles via asyncio.to_thread + hoisted imports
  5. tools/sandbox.py: kill_tree_and_reap offloads kill_tree via asyncio.to_thread
  6. backend VRAM probes: async paths offload get_live_gpu_free_mib / can_fit via asyncio.to_thread
  7. core/duo_dtool_setup.py: _dedup_additional_explore_history drops native/touched duplicates
  8. static/app.js + index.html: single cumulative .cp snapshot/restore, coalesced think scroll, Settings-gated tg poll
"""
from __future__ import annotations

import ast
import asyncio
import copy
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.ui_sources import read_all_ui_js  # noqa: E402
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


# ── 1. core/duo/_pre_explore.py blind sleeps & force_kill_all gate ───────────
def test_pre_explore_evict_timing() -> None:
    src = (ROOT / "core" / "duo" / "_pre_explore.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    evict_start = src.index("# Batch 1.1: Triple")
    evict_end = src.index("_pre_explore_msgs = (", evict_start)
    evict_block = src[evict_start:evict_end]

    check(
        "_pre_explore post-explore eviction removes blind 3.0s and 2.5s sleeps",
        "sleep(3.0)" not in evict_block and "sleep(2.5)" not in evict_block,
    )
    check(
        "_pre_explore gates force_kill_all on remaining loaded models",
        "if _pe_still_loaded:" in evict_block
        and "if _pe_evicted > 0 or _pe_force_killed:" in evict_block
        and "GPU_BACKEND" in evict_block,
    )

    # Confirm AST structure: force_kill_all() call inside post-explore function is gated inside an If node.
    fka_gated = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            if isinstance(node.test, ast.Name) and node.test.id == "_pe_still_loaded":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "force_kill_all":
                        fka_gated = True
    check(
        "_pre_explore AST: force_kill_all is strictly nested under `if _pe_still_loaded`",
        fka_gated,
    )


# ── 2. core/duo/preflight.py in-place unpin_duo_models ───────────────────────
def test_unpin_duo_models_in_place() -> None:
    import core.duo.preflight as pf

    fake_ctx = SimpleNamespace(get_num_ctx=lambda _m: 32768)

    class _FakeMgr:
        def __init__(self, loaded: set[str]):
            self.loaded = loaded
            self.unpinned: list[tuple[str, float]] = []
            self.evicted: list[str] = []

        def get_port_for(self, m: str):
            return 8081 if m in self.loaded else None

        async def unpin(self, m: str, keep_alive_seconds: float = 600.0):
            self.unpinned.append((m, keep_alive_seconds))

        async def evict(self, m: str):
            self.evicted.append(m)

    bk_calls: list[tuple] = []

    async def _fake_bk_load(model: str, keep_alive: str, num_ctx=None):
        bk_calls.append((model, keep_alive, num_ctx))

    async def _run():
        mgr = _FakeMgr({"loaded-coder", "other"})
        with mock.patch("backend.llama_server_manager.manager", mgr), \
             mock.patch.object(pf, "_bk_load", _fake_bk_load):
            # 10m keep_alive -> in-place unpin(600.0), zero _bk_load, unloaded skipped
            await pf.unpin_duo_models(fake_ctx, ["loaded-coder", "evicted-coder"], "10m")
            c1_unpinned = list(mgr.unpinned)
            c1_evicted = list(mgr.evicted)

            # 0 keep_alive -> direct evict
            mgr.unpinned.clear()
            mgr.evicted.clear()
            await pf.unpin_duo_models(fake_ctx, ["loaded-coder"], "0")
            c2_evicted = list(mgr.evicted)
            c2_unpinned = list(mgr.unpinned)

            # -1 permanent -> stay running/pinned in place
            mgr.unpinned.clear()
            mgr.evicted.clear()
            await pf.unpin_duo_models(fake_ctx, ["loaded-coder"], "-1")
            c3_unpinned = list(mgr.unpinned)
            c3_evicted = list(mgr.evicted)

        return c1_unpinned, c1_evicted, c2_evicted, c2_unpinned, c3_unpinned, c3_evicted

    c1_unp, c1_ev, c2_ev, c2_unp, c3_unp, c3_ev = asyncio.run(_run())
    check(
        "unpin_duo_models uses manager.unpin(600.0) in-place & skips evicted models without _bk_load",
        c1_unp == [("loaded-coder", 600.0)] and c1_ev == [] and bk_calls == [],
        f"(unp={c1_unp}, bk={bk_calls})",
    )
    check(
        "unpin_duo_models(ka='0') calls manager.evict without _bk_load",
        c2_ev == ["loaded-coder"] and c2_unp == [] and bk_calls == [],
        f"(ev={c2_ev}, unp={c2_unp})",
    )
    check(
        "unpin_duo_models(ka='-1') leaves loaded model pinned in place without reload",
        c3_unp == [] and c3_ev == [] and bk_calls == [],
    )


# ── 3. model_configs/models_registry.py read-only cache ──────────────────────
def test_models_registry_readonly_cache() -> None:
    import model_configs.models_registry as reg

    reg.refresh()
    all_profs = reg.iter_all_profiles()
    sample_name = next((k for k, v in all_profs.items() if v.get("sampling")), next(iter(all_profs)))

    # Deep copy preserved on public get_profile
    p1 = reg.get_profile(sample_name)
    p1["__mutated__"] = True
    if isinstance(p1.get("capabilities"), dict):
        p1["capabilities"]["thinking"] = not p1["capabilities"].get("thinking")
    p2 = reg.get_profile(sample_name)
    check(
        "get_profile still returns isolated deepcopy",
        "__mutated__" not in p2 and p2 is not p1,
    )

    # Scalar getters must NOT call copy.deepcopy
    deepcopy_calls = 0
    real_deepcopy = copy.deepcopy

    def _spy_deepcopy(x, memo=None):
        nonlocal deepcopy_calls
        deepcopy_calls += 1
        return real_deepcopy(x, memo) if memo is not None else real_deepcopy(x)

    with mock.patch.object(reg.copy, "deepcopy", side_effect=_spy_deepcopy):
        _ = reg.is_mtp(sample_name)
        _ = reg.get_num_ctx(sample_name)
        _ = reg.get_load_mode(sample_name)
        _ = reg.get_thinking_budget(sample_name)
        _ = reg.get_flash_attn(sample_name)
        _ = reg.get_capabilities(sample_name)
        _ = reg.get_extra_args(sample_name)
        _ = reg.get_moe_cpu_experts(sample_name)
        _ = reg.get_ubatch(sample_name)
        _ = reg.get_kv_cache_type(sample_name)
        _ = reg.get_mmproj_filename(sample_name)
        _ = reg.get_vram_gb_override(sample_name)

    check(
        "scalar & capability accessors execute 0 copy.deepcopy calls",
        deepcopy_calls == 0,
        f"(deepcopy_calls={deepcopy_calls})",
    )

    real_ctx = reg.get_num_ctx(sample_name)
    with mock.patch.object(reg, "get_profile", return_value={"num_ctx": 98765}):
        mocked_ctx = reg.get_num_ctx(sample_name)
    restored_ctx = reg.get_num_ctx(sample_name)
    check(
        "monkeypatching get_profile overrides scalar accessors without polluting cache",
        mocked_ctx == 98765 and restored_ctx == real_ctx,
        f"(real={real_ctx}, mocked={mocked_ctx}, restored={restored_ctx})",
    )

    # get_sampling returns isolated copy
    s1 = reg.get_sampling(sample_name)
    if s1:
        first_key = next(iter(s1))
        if isinstance(s1[first_key], dict):
            s1[first_key]["__leak__"] = 123
        s2 = reg.get_sampling(sample_name)
        check(
            "get_sampling nested dict mutations do not leak back into cache",
            "__leak__" not in (s2.get(first_key) or {}),
        )
    else:
        check("get_sampling empty dict baseline ok", s1 == {})

    # Cache invalidation on refresh() and direct _shipped_cache replacement
    _ = reg._get_profile_readonly(sample_name)
    check("resolved cache populated after read", len(reg._resolved_profile_cache) >= 1)
    reg.refresh()
    check("refresh() clears _resolved_profile_cache", len(reg._resolved_profile_cache) == 0)

    old_shipped = reg._shipped_cache
    old_user = reg._user_cache
    old_ts = reg._cache_ts
    old_prof = reg._profile_cache
    try:
        reg._cache_ts = 9e12
        reg._profile_cache = {"fake:1b": {"num_ctx": 4096}}
        reg._shipped_cache = {"fake:1b": {"num_ctx": 4096}}
        reg._user_cache = {}
        got_fake = reg.get_num_ctx("fake:1b")
        reg._shipped_cache = {"fake:1b": {"num_ctx": 16384}}
        got_updated = reg.get_num_ctx("fake:1b")
        check(
            "replacing _shipped_cache invalidates _resolved_profile_cache",
            got_fake == 4096 and got_updated == 16384,
            f"(got_fake={got_fake}, got_updated={got_updated})",
        )
    finally:
        reg._shipped_cache = old_shipped
        reg._user_cache = old_user
        reg._cache_ts = old_ts
        reg._profile_cache = old_prof
        reg.refresh()


# ── 4. routers/models.py asyncio.to_thread & hoisted imports ─────────────────
def test_routers_models_offload() -> None:
    import routers.models as rm

    src = (ROOT / "routers" / "models.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    hoisted_ok = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_models_profiles":
            # Ensure no ImportFrom inside the For loop
            in_loop_import = any(
                isinstance(sub, (ast.Import, ast.ImportFrom))
                for child in ast.walk(node)
                if isinstance(child, ast.For)
                for sub in ast.walk(child)
            )
            hoisted_ok = not in_loop_import
    check("_build_models_profiles hoists registry imports outside per-model loop", hoisted_ok)

    thread_targets: list[str] = []
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(fn, *args, **kwargs):
        thread_targets.append(getattr(fn, "__name__", repr(fn)))
        return await real_to_thread(fn, *args, **kwargs)

    async def _run():
        with mock.patch("backend.llama_models.list_available_models", return_value=["qwen3.5:4b"]), \
             mock.patch.object(rm, "get_model_display_map", return_value={"qwen3.5:4b": {"tags": [], "vram_gb": 3.0}}), \
             mock.patch.object(rm._gguf_meta, "read_gguf_metadata", return_value={}), \
             mock.patch.object(rm.asyncio, "to_thread", side_effect=_spy_to_thread):
            return await rm.get_models()

    res = asyncio.run(_run())
    check(
        "GET /models offloads _build_models_profiles via asyncio.to_thread",
        "_build_models_profiles" in thread_targets and res["models"] == ["qwen3.5:4b"] and len(res["profiles"]) == 1,
        f"(targets={thread_targets})",
    )


# ── 5. tools/sandbox.py kill_tree_and_reap offload ───────────────────────────
def test_sandbox_kill_tree_offload() -> None:
    import tools.sandbox as sb

    waited = False

    class _FakeProc:
        pid = 12345

        async def wait(self):
            nonlocal waited
            waited = True

    calls: list[tuple] = []
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        return await real_to_thread(fn, *args, **kwargs)

    def _fake_kill_tree(proc, taskkill_timeout=5):
        calls.append(("ran_kill_tree", getattr(proc, "pid", None), taskkill_timeout))

    async def _run():
        with mock.patch.object(sb, "kill_tree", _fake_kill_tree), \
             mock.patch.object(sb.asyncio, "to_thread", side_effect=_spy_to_thread):
            await sb.kill_tree_and_reap(_FakeProc(), taskkill_timeout=3)

    asyncio.run(_run())
    check(
        "kill_tree_and_reap dispatches kill_tree via asyncio.to_thread and awaits wait()",
        waited and any(c[0] is _fake_kill_tree for c in calls if len(c) == 3)
        and ("ran_kill_tree", 12345, 3) in calls,
        f"(waited={waited}, calls={calls})",
    )


# ── 6. Async VRAM probes offloaded via asyncio.to_thread ─────────────────────
def test_vram_probe_offload() -> None:
    import backend.llama_vram_table as lvt
    import backend.manager_evict as me
    from backend.llama_manager_utils import CanFitResult

    reclaim_calls: list[object] = []
    real_to_thread = asyncio.to_thread

    async def _spy_vram_thread(fn, *args, **kwargs):
        reclaim_calls.append(fn)
        return await real_to_thread(fn, *args, **kwargs)

    async def _run_reclaim():
        with mock.patch.object(lvt, "get_live_gpu_free_mib", return_value=4096.0) as mocked_probe, \
             mock.patch.object(asyncio, "to_thread", side_effect=_spy_vram_thread), \
             mock.patch("backend.llama_config.GPU_BACKEND", "vulkan"):
            return await lvt.wait_for_vram_reclaim(2048, timeout_sec=5, poll_interval=0.05), mocked_probe

    ok_reclaim, mocked_probe = asyncio.run(_run_reclaim())
    check(
        "wait_for_vram_reclaim probes get_live_gpu_free_mib via asyncio.to_thread",
        ok_reclaim is True and reclaim_calls.count(mocked_probe) >= 2,
        f"(ok={ok_reclaim}, count={reclaim_calls.count(mocked_probe)})",
    )

    class _ProbeMgr(me.LlamaEvictMixin):
        def __init__(self):
            self._slots = []

    grace_calls: list[str] = []

    async def _spy_evict_thread(fn, *args, **kwargs):
        grace_calls.append(getattr(fn, "__name__", repr(fn)))
        return await real_to_thread(fn, *args, **kwargs)

    async def _run_grace():
        mgr = _ProbeMgr()
        slot = SimpleNamespace(slot_id=0)
        with mock.patch.object(me, "get_live_gpu_free_mib", return_value=24000.0), \
             mock.patch.object(me.asyncio, "to_thread", side_effect=_spy_evict_thread), \
             mock.patch("backend.llama_config.GPU_BACKEND", "cuda"):
            return await mgr._pre_flight_grace_recheck("qwen3.5:4b", 4096, slot, grace_s=1.0, poll_interval=0.05)

    fit = asyncio.run(_run_grace())
    check(
        "_pre_flight_grace_recheck offloads self.can_fit via asyncio.to_thread",
        isinstance(fit, CanFitResult) and fit.ok and "can_fit" in grace_calls,
        f"(fit={fit}, grace_calls={grace_calls})",
    )

    # Ensure zero direct un-offloaded calls to get_live_gpu_free_mib or _free_mib remain in llama_start_phases.py
    sp_src = (ROOT / "backend" / "llama_start_phases.py").read_text(encoding="utf-8")
    sp_tree = ast.parse(sp_src)
    direct_calls: list[int] = []
    for node in ast.walk(sp_tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in ("get_live_gpu_free_mib", "_free_mib"):
                direct_calls.append(getattr(node, "lineno", 0))
    check(
        "llama_start_phases.py has zero direct sync get_live_gpu_free_mib/_free_mib calls",
        direct_calls == [],
        f"(direct_call_lines={direct_calls})",
    )


# ── 7. core/duo_dtool_setup.py explore history deduplication ─────────────────
def test_dtool_setup_dedup() -> None:
    from core.duo_dtool_setup import _dedup_additional_explore_history

    a1 = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "tc1", "function": {"name": "read_file", "arguments": '{"path": "app/main.py"}'}},
            {"id": "tc2", "function": {"name": "read_file", "arguments": '{"path": "app/other.py"}'}},
        ],
    }
    t1 = {"role": "tool", "name": "read_file", "tool_call_id": "tc1", "content": "main body"}
    t2 = {"role": "tool", "name": "read_file", "tool_call_id": "tc2", "content": "other body"}
    a2 = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "tc3", "function": {"name": "grep_search", "arguments": '{"query": "foo"}'}}],
    }
    t3 = {"role": "tool", "name": "grep_search", "tool_call_id": "tc3", "content": "foo hits"}

    full = [a1, t1, t2, a2, t3]
    included = [a2, t3]
    out_with_touched = _dedup_additional_explore_history(full, included, {"app/main.py"})
    check(
        "_dedup_additional_explore_history excludes native-included & touched read_file msgs, keeps non-touched",
        out_with_touched == [a1, t2],
        f"(got={out_with_touched})",
    )

    out_no_touched = _dedup_additional_explore_history(full, included, set())
    check(
        "_dedup_additional_explore_history preserves read_file outputs when touched_paths is empty",
        out_no_touched == [a1, t1, t2],
        f"(got={out_no_touched})",
    )


# ── 8. static/app.js + index.html performance invariants ─────────────────────
def test_frontend_perf_invariants() -> None:
    app_js = read_all_ui_js()
    idx_html = (ROOT / "index.html").read_text(encoding="utf-8")

    autosave_start = app_js.index("function _autosaveChatMessage()")
    autosave_end = app_js.index("function chatAutosaveToggle(", autosave_start)
    autosave_body = app_js[autosave_start:autosave_end]
    check(
        "_autosaveChatMessage drops per-bubble `cp: _cpSnapshot()` and saves `.cp` once after the loop",
        "cp: _cpSnapshot()" not in autosave_body and autosave_body.count("_cpSnapshot()") == 1,
    )

    restore_start = app_js.index("// Restore chat DOM from saved messages")
    restore_end = app_js.index("loadChatHistory();", restore_start)
    restore_body = app_js[restore_start:restore_end]
    loop_pos = restore_body.index("(chat.messages || []).forEach")
    cp_pos = restore_body.index("_cpRestoreSnapshot(_lastCp)")
    check(
        "loadChat restores `_cpRestoreSnapshot` once before the message loop",
        cp_pos < loop_pos and restore_body.count("_cpRestoreSnapshot(") == 1,
    )

    check(
        "static/app.js defines coalesced _scrollElToBottomSoon and applies it to thinking scroll sites",
        "function _scrollElToBottomSoon(el)" in app_js
        and "_scrollElToBottomSoon(thinkBodyEl);" in app_js
        and "_scrollElToBottomSoon(_thbEl);" in app_js
        and "thinkBodyEl.scrollTop = thinkBodyEl.scrollHeight" not in app_js
        and "_thbEl.scrollTop = _thbEl.scrollHeight" not in app_js,
    )

    tg_start = app_js.index("window._tgSupIv = setInterval(")
    tg_end = app_js.index("}, 5000);", tg_start)
    tg_body = app_js[tg_start:tg_end]
    check(
        "_tgSupIv gates gatewaySupervisorTick on #tg-sup-status visibility",
        "tg-sup-status" in tg_body and "offsetParent !== null" in tg_body,
    )

    _m_js = re.search(r"/static/app\.js\?v=(\d{8}-\d{2})", idx_html)
    check(
        "index.html bumps app.js cache-buster to >= 20261121-05",
        bool(_m_js and _m_js.group(1) >= "20261121-05"),
    )


# ── 9. backend/llama_slots.py fast loopback wait_ready client ─────────────────
def test_wait_ready_fast_loopback_client() -> None:
    slots_src = (ROOT / "backend" / "llama_slots.py").read_text(encoding="utf-8")
    check(
        "wait_ready constructs fallback AsyncClient with verify=not self.url.startswith(\"http://\") and trust_env=False",
        'verify=not self.url.startswith("http://")' in slots_src
        and "trust_env=False" in slots_src,
    )


if __name__ == "__main__":
    test_pre_explore_evict_timing()
    test_unpin_duo_models_in_place()
    test_models_registry_readonly_cache()
    test_routers_models_offload()
    test_sandbox_kill_tree_offload()
    test_vram_probe_offload()
    test_dtool_setup_dedup()
    test_frontend_perf_invariants()
    test_wait_ready_fast_loopback_client()
    sys.exit(h.finish("audit_wave3_perf_levers"))
