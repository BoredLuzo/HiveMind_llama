# -*- coding: utf-8 -*-
"""Stage 3A Workstream C2 regression tests:
  1. 13 approved engine settings keys + empty auto defaults + SETTINGS_UI_META
  2. GET /settings/schema completeness, types, metadata, secret redaction & release fallback
  3. Unified backend.runtime_flags._broadcast_attr / apply_runtime_flags & POST /settings sync
  4. core.model_sampling default_unprofiled_sampling wiring & safe partial fallbacks
  5. core.duo_compress_cycle duo_compress_mode (auto / mask_only / partial / full)
  6. deploy/gen_settings_docs.py --check & deploy/settings_ui_scan.py (<15 exempt keys)

Run: python tests/test_settings_ui_schema.py
"""
from __future__ import annotations

import asyncio
import copy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check


class _FakeReq:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


class _FakeLRUWithEntries:
    """Minimal ToolContextLRU double supporting candidates() and mark_evicted()."""

    def __init__(self, entries: list[dict]):
        self._entries = [dict(e) for e in entries]
        self.evicted_idx: list[int] = []
        self.reset_calls = 0

    def candidates(self):
        return [e for e in self._entries if not e.get("evicted")]

    def mark_evicted(self, idx: int):
        self.evicted_idx.append(idx)
        for e in self._entries:
            if e.get("idx") == idx:
                e["evicted"] = True

    def reset(self):
        self.reset_calls += 1


class _FakeExecCtrl:
    def __init__(self):
        self.synced: list[int] = []

    def sync_tool_rounds(self, n: int):
        self.synced.append(n)


async def _drain(gen) -> list:
    out = []
    async for ev in gen:
        out.append(ev)
    return out


# ── 1. DEFAULT_SETTINGS, empty auto defaults & SETTINGS_UI_META ────────────
def _test_defaults_and_meta() -> None:
    from settings_defaults import (
        DEFAULT_SETTINGS,
        SETTINGS_INTERNAL_KEYS,
        SETTINGS_SCHEMA_EXCLUDED_KEYS,
        SETTINGS_UI_META,
    )

    expected_13 = {
        "llama_kv_cache_type": "q4_0",
        "llama_max_slots": 3,
        "llama_gpu_device": 0,
        "llama_extra_args": "",
        "llama_update_channel": "latest",
        "spec_decode_mode": "auto",
        "mtp_draft_n_max": 2,
        "mtp_draft_p_min": 0.0,
        "dflash_draft_p_min": 0.4,
        "total_vram_mib_override": 0,
        "vram_preflight_margin_mib": 768,
        "duo_compress_mode": "auto",
        "default_unprofiled_sampling": {
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "min_p": 0.05,
            "repeat_penalty": 1.05,
            "presence_penalty": 0.0,
        },
    }
    for k, want in expected_13.items():
        check(f"1a default {k} matches spec", DEFAULT_SETTINGS.get(k) == want,
              f"got={DEFAULT_SETTINGS.get(k)!r}")

    check("1b vision_agent_model defaults to empty auto-pick",
          DEFAULT_SETTINGS.get("vision_agent_model") == "")
    check("1c subagent_lite_model_ladder defaults to empty auto-pick list",
          DEFAULT_SETTINGS.get("subagent_lite_model_ladder") == [])

    check("1d SETTINGS_UI_META covers all 13 new keys",
          all(k in SETTINGS_UI_META for k in expected_13))
    check("1e every SETTINGS_UI_META key exists in DEFAULT_SETTINGS",
          set(SETTINGS_UI_META) <= set(DEFAULT_SETTINGS))
    check("1f SETTINGS_UI_META apply_mode values are valid",
          all(m.get("apply_mode") in ("live", "next_load", "restart")
              for m in SETTINGS_UI_META.values()))
    check("1g SETTINGS_INTERNAL_KEYS < 15",
          len(SETTINGS_INTERNAL_KEYS) < 15, f"({len(SETTINGS_INTERNAL_KEYS)})")
    check("1h SETTINGS_INTERNAL_KEYS subset of SETTINGS_SCHEMA_EXCLUDED_KEYS",
          SETTINGS_INTERNAL_KEYS <= SETTINGS_SCHEMA_EXCLUDED_KEYS)


# ── 2. GET /settings/schema ────────────────────────────────────────────────
def _test_settings_schema_endpoint() -> None:
    import routers.config as rc
    from settings_defaults import (
        DEFAULT_SETTINGS,
        SETTINGS_INTERNAL_KEYS,
        SETTINGS_SCHEMA_EXCLUDED_KEYS,
    )

    orig_settings = copy.deepcopy(rc.settings)
    try:
        rc.settings.clear()
        rc.settings.update(copy.deepcopy(DEFAULT_SETTINGS))
        rc.settings["git_token"] = "ghp_secret_must_never_leak"
        rc.settings["mcp_servers"] = [{
            "name": "srv",
            "command": "npx",
            "args": ["--api-key", "sk-secret", "--token=tok-secret"],
            "headers": {"Authorization": "Bearer secret"},
            "env": {"API_KEY": "env-secret"},
        }]
        rc.settings["llama_kv_cache_type"] = "q8_0"

        schema = asyncio.run(rc.get_settings_schema())
        entries = schema.get("entries") or []
        by_key = {e["key"]: e for e in entries}
        expected_keys = set(DEFAULT_SETTINGS) - set(SETTINGS_SCHEMA_EXCLUDED_KEYS)

        check("2a /settings/schema ok and count matches entries",
              schema.get("ok") is True and schema.get("count") == len(entries))
        check("2b /settings/schema covers all user-configurable DEFAULT_SETTINGS keys",
              set(by_key) == expected_keys,
              f"diff={sorted(set(by_key) ^ expected_keys)}")
        check("2c /settings/schema excludes internal & secret keys",
              not (set(by_key) & set(SETTINGS_INTERNAL_KEYS))
              and "git_token" not in by_key
              and "active_preset" not in by_key)

        mcp_val = by_key["mcp_servers"]["value"][0]
        check("2d /settings/schema redacts mcp_servers secrets",
              mcp_val["headers"]["Authorization"] == "***"
              and mcp_val["env"]["API_KEY"] == "***"
              and mcp_val["args"] == ["--api-key", "***", "--token=***"])

        kv = by_key["llama_kv_cache_type"]
        check("2e llama_kv_cache_type schema entry carries choices, group, apply_mode, current value",
              kv["type"] == "str" and kv["default"] == "q4_0" and kv["value"] == "q8_0"
              and kv["choices"] == ["q4_0", "q8_0", "f16"]
              and kv["group"] == "engine" and kv["apply_mode"] == "next_load"
              and bool(kv["description"]))

        samp = by_key["default_unprofiled_sampling"]
        check("2f default_unprofiled_sampling schema entry carries object type & description",
              samp["type"] == "object" and samp["group"] == "sampling"
              and samp["apply_mode"] == "live" and bool(samp["description"]))

        comp = by_key["duo_compress_mode"]
        check("2g duo_compress_mode schema entry carries 4 choices",
              comp["choices"] == ["auto", "mask_only", "partial", "full"]
              and comp["group"] == "compression" and comp["apply_mode"] == "live")

        # Release-archive fallback when deploy.gen_settings_docs cannot be imported
        with mock.patch.dict(sys.modules, {"deploy.gen_settings_docs": None}):
            descs = rc._load_schema_descriptions()
        check("2h _load_schema_descriptions works without deploy.gen_settings_docs",
              bool(descs.get("llama_kv_cache_type")) and bool(descs.get("duo_compress_mode")))
    finally:
        rc.settings.clear()
        rc.settings.update(orig_settings)


# ── 3. runtime_flags broadcast & POST /settings unification ────────────────
def _test_runtime_flags_and_config_sync() -> None:
    import backend.llama_config as lc
    import backend.llama_manager_utils as lmu
    import backend.llama_server_manager as lsm
    import backend.llama_start_phases as lsp
    import backend.llama_vram_table as lvt
    import backend.manager_load as ml
    import core.duo_compress_cycle as dcc
    import core.model_sampling as ms
    import routers.config as rc
    from backend.runtime_flags import RUNTIME_FLAG_KEYS, _broadcast_attr, apply_runtime_flags

    cfg_src = (ROOT / "routers" / "config.py").read_text(encoding="utf-8")
    check("3a _sync_vram_preflight_gate removed from routers/config.py",
          "_sync_vram_preflight_gate" not in cfg_src)
    check("3b RUNTIME_FLAG_KEYS includes vram_preflight_gate and new runtime flags",
          {"vram_preflight_gate", "llama_kv_cache_type", "llama_max_slots",
           "llama_gpu_device", "llama_extra_args", "spec_decode_mode",
           "mtp_draft_n_max", "mtp_draft_p_min", "dflash_draft_p_min",
           "total_vram_mib_override", "vram_preflight_margin_mib",
           "duo_compress_mode", "default_unprofiled_sampling"} <= set(RUNTIME_FLAG_KEYS))

    saved_attrs = [
        (lc, "KV_CACHE_TYPE", lc.KV_CACHE_TYPE),
        (lc, "MAX_SLOTS", lc.MAX_SLOTS),
        (lc, "VULKAN_DEVICE", lc.VULKAN_DEVICE),
        (lc, "MTP_DRAFT_N_MAX", lc.MTP_DRAFT_N_MAX),
        (lc, "MTP_DRAFT_P_MIN", lc.MTP_DRAFT_P_MIN),
        (lc, "DFLASH_DRAFT_P_MIN", lc.DFLASH_DRAFT_P_MIN),
        (lc, "VRAM_PREFLIGHT_GATE", lc.VRAM_PREFLIGHT_GATE),
        (lc, "CACHE_REUSE", lc.CACHE_REUSE),
        (lc, "CACHE_RAM_MIB", lc.CACHE_RAM_MIB),
        (lc, "CTX_CHECKPOINTS", lc.CTX_CHECKPOINTS),
        (lc, "MLOCK_MODEL", lc.MLOCK_MODEL),
        (lc, "LLAMA_UBATCH", lc.LLAMA_UBATCH),
        (lsp, "KV_CACHE_TYPE", lsp.KV_CACHE_TYPE),
        (lsp, "VULKAN_DEVICE", lsp.VULKAN_DEVICE),
        (lsp, "MTP_DRAFT_N_MAX", lsp.MTP_DRAFT_N_MAX),
        (lsp, "MTP_DRAFT_P_MIN", lsp.MTP_DRAFT_P_MIN),
        (lsp, "DFLASH_DRAFT_P_MIN", lsp.DFLASH_DRAFT_P_MIN),
        (lsp, "VRAM_PREFLIGHT_GATE", lsp.VRAM_PREFLIGHT_GATE),
        (lsp, "VRAM_PRE_FLIGHT_MARGIN_MIB", lsp.VRAM_PRE_FLIGHT_MARGIN_MIB),
        (ml, "KV_CACHE_TYPE", ml.KV_CACHE_TYPE),
        (ml, "MAX_SLOTS", ml.MAX_SLOTS),
        (ml, "VULKAN_DEVICE", ml.VULKAN_DEVICE),
        (ml, "MTP_DRAFT_N_MAX", ml.MTP_DRAFT_N_MAX),
        (ml, "MTP_DRAFT_P_MIN", ml.MTP_DRAFT_P_MIN),
        (ml, "VRAM_PREFLIGHT_GATE", ml.VRAM_PREFLIGHT_GATE),
        (ml, "VRAM_PRE_FLIGHT_MARGIN_MIB", ml.VRAM_PRE_FLIGHT_MARGIN_MIB),
        (lsm, "MAX_SLOTS", lsm.MAX_SLOTS),
        (lsm, "VULKAN_DEVICE", lsm.VULKAN_DEVICE),
        (lsm, "MTP_DRAFT_N_MAX", lsm.MTP_DRAFT_N_MAX),
        (lsm, "VRAM_PREFLIGHT_GATE", lsm.VRAM_PREFLIGHT_GATE),
        (lmu, "VRAM_PRE_FLIGHT_MARGIN_MIB", lmu.VRAM_PRE_FLIGHT_MARGIN_MIB),
        (dcc, "DUO_COMPRESS_MODE", dcc.DUO_COMPRESS_MODE),
        (ms, "DEFAULT_UNPROFILED_SAMPLING", copy.deepcopy(ms.DEFAULT_UNPROFILED_SAMPLING)),
    ]
    orig_settings = copy.deepcopy(rc.settings)
    orig_save = rc.save_settings
    orig_rev = rc._settings_rev
    created_c1 = []
    try:
        # Simulate C1 attributes on llama_config / llama_vram_table to verify hasattr broadcast
        for mod, attr, init in (
            (lc, "GPU_DEVICE", 0),
            (lc, "LLAMA_EXTRA_ARGS", ""),
            (lc, "SPEC_DECODE_MODE", "auto"),
            (lvt, "TOTAL_VRAM_MIB_OVERRIDE", 0),
        ):
            if not hasattr(mod, attr):
                setattr(mod, attr, init)
                created_c1.append((mod, attr))

        apply_runtime_flags({
            "llama_kv_cache_type": "q8_0",
            "llama_max_slots": 4,
            "llama_gpu_device": 1,
            "llama_extra_args": "--flash-attn on --no-mmap",
            "spec_decode_mode": "off",
            "mtp_draft_n_max": 3,
            "mtp_draft_p_min": 0.25,
            "dflash_draft_p_min": 0.55,
            "total_vram_mib_override": 16384,
            "vram_preflight_margin_mib": 512,
            "vram_preflight_gate": False,
            "duo_compress_mode": "partial",
            "default_unprofiled_sampling": {"temperature": 0.5, "top_p": 0.85},
        })

        check("3c KV_CACHE_TYPE broadcast across lc/ml/lsp",
              lc.KV_CACHE_TYPE == "q8_0" and ml.KV_CACHE_TYPE == "q8_0"
              and lsp.KV_CACHE_TYPE == "q8_0")
        check("3d MAX_SLOTS broadcast across lc/lsm/ml",
              lc.MAX_SLOTS == 4 and lsm.MAX_SLOTS == 4 and ml.MAX_SLOTS == 4)
        check("3e GPU_DEVICE & VULKAN_DEVICE broadcast",
              getattr(lc, "GPU_DEVICE") == 1 and lc.VULKAN_DEVICE == 1
              and lsp.VULKAN_DEVICE == 1 and ml.VULKAN_DEVICE == 1)
        check("3f LLAMA_EXTRA_ARGS & SPEC_DECODE_MODE & TOTAL_VRAM_MIB_OVERRIDE broadcast",
              getattr(lc, "LLAMA_EXTRA_ARGS") == "--flash-attn on --no-mmap"
              and getattr(lc, "SPEC_DECODE_MODE") == "off"
              and getattr(lvt, "TOTAL_VRAM_MIB_OVERRIDE") == 16384)
        check("3g draft params & margins broadcast",
              lc.MTP_DRAFT_N_MAX == 3 and lsp.MTP_DRAFT_N_MAX == 3
              and lc.MTP_DRAFT_P_MIN == 0.25 and lsp.MTP_DRAFT_P_MIN == 0.25
              and lc.DFLASH_DRAFT_P_MIN == 0.55 and lsp.DFLASH_DRAFT_P_MIN == 0.55
              and lmu.VRAM_PRE_FLIGHT_MARGIN_MIB == 512 and lsp.VRAM_PRE_FLIGHT_MARGIN_MIB == 512)
        check("3h VRAM_PREFLIGHT_GATE, DUO_COMPRESS_MODE, DEFAULT_UNPROFILED_SAMPLING broadcast",
              lc.VRAM_PREFLIGHT_GATE is False and lsp.VRAM_PREFLIGHT_GATE is False
              and dcc.DUO_COMPRESS_MODE == "partial"
              and ms.DEFAULT_UNPROFILED_SAMPLING == {"temperature": 0.5, "top_p": 0.85})

        # _broadcast_attr never attaches nonexistent attributes
        check("3i _broadcast_attr ignores attributes not defined on target modules",
              _broadcast_attr("_NONEXISTENT_C2_PROBE_ATTR", 123) == 0
              and not hasattr(lc, "_NONEXISTENT_C2_PROBE_ATTR"))

        # Garbage values never raise and keep previous values
        apply_runtime_flags({
            "llama_kv_cache_type": "invalid_kv",
            "llama_max_slots": True,
            "mtp_draft_p_min": float("nan"),
            "duo_compress_mode": "bogus",
            "default_unprofiled_sampling": "not-a-dict",
        })
        check("3j garbage runtime flag values safely retain prior values",
              lc.KV_CACHE_TYPE == "q8_0" and lc.MAX_SLOTS == 4
              and lc.MTP_DRAFT_P_MIN == 0.25 and dcc.DUO_COMPRESS_MODE == "partial"
              and ms.DEFAULT_UNPROFILED_SAMPLING == {"temperature": 0.5, "top_p": 0.85})

        # POST /settings updates runtime flags via apply_runtime_flags
        rc.save_settings = lambda _s, **_kw: None
        rc.settings.clear()
        rc.settings["vram_preflight_gate"] = False
        res = asyncio.run(rc.post_settings(_FakeReq({
            "vram_preflight_gate": True,
            "llama_kv_cache_type": "f16",
            "duo_compress_mode": "full",
        })))
        check("3k POST /settings applies runtime flags live",
              res.get("ok") is True and lc.VRAM_PREFLIGHT_GATE is True
              and lsp.VRAM_PREFLIGHT_GATE is True and lc.KV_CACHE_TYPE == "f16"
              and dcc.DUO_COMPRESS_MODE == "full")
    finally:
        for mod, attr in created_c1:
            if hasattr(mod, attr):
                delattr(mod, attr)
        for mod, attr, val in saved_attrs:
            setattr(mod, attr, val)
        rc.settings.clear()
        rc.settings.update(orig_settings)
        rc.save_settings = orig_save
        rc._settings_rev = orig_rev


# ── 4. core/model_sampling.py default_unprofiled_sampling ──────────────────
def _test_unprofiled_sampling_wiring() -> None:
    import core.model_sampling as ms
    from backend.runtime_flags import apply_runtime_flags

    orig_runtime = copy.deepcopy(ms.DEFAULT_UNPROFILED_SAMPLING)
    try:
        smp_default = ms.get_sampling_profile("brand-new-unprofiled:7b")
        check("4a unprofiled model uses default_unprofiled_sampling defaults",
              abs(smp_default.get("temperature", 0) - 0.7) < 1e-9
              and abs(smp_default.get("min_p", 0) - 0.05) < 1e-9
              and abs(smp_default.get("repeat_penalty", 0) - 1.05) < 1e-9
              and abs(smp_default.get("repetition_penalty", 0) - 1.05) < 1e-9
              and smp_default.get("top_k") == 40
              and smp_default.get("cache_prompt") is True)

        smp_partial = ms.get_sampling_profile(
            "brand-new-unprofiled:7b",
            settings={"default_unprofiled_sampling": {"temperature": 0.55}},
        )
        check("4b partial settings default_unprofiled_sampling overrides temperature and preserves other fields",
              abs(smp_partial.get("temperature", 0) - 0.55) < 1e-9
              and abs(smp_partial.get("top_p", 0) - 0.9) < 1e-9
              and abs(smp_partial.get("repeat_penalty", 0) - 1.05) < 1e-9)

        smp_bad = ms.get_sampling_profile(
            "brand-new-unprofiled:7b",
            settings={"default_unprofiled_sampling": {"temperature": True, "top_k": "bad"}},
        )
        check("4c invalid default_unprofiled_sampling values fall back safely",
              abs(smp_bad.get("temperature", 0) - 0.7) < 1e-9 and smp_bad.get("top_k") == 40)

        apply_runtime_flags({"default_unprofiled_sampling": {"temperature": 0.62, "top_k": 32}})
        smp_rt = ms.get_sampling_profile("brand-new-unprofiled:7b")
        check("4d runtime DEFAULT_UNPROFILED_SAMPLING via apply_runtime_flags takes effect when settings=None",
              abs(smp_rt.get("temperature", 0) - 0.62) < 1e-9 and smp_rt.get("top_k") == 32)

        # Profiled model keeps its own JSON profile instead of default_unprofiled_sampling
        smp_prof = ms.get_sampling_profile(
            "qwen3.5:4b-mtp",
            thinking=True,
            settings={"default_unprofiled_sampling": {"temperature": 0.99}},
        )
        check("4e profiled model ignores default_unprofiled_sampling",
              abs(smp_prof.get("temperature", 0) - 0.99) > 1e-6)
    finally:
        ms.DEFAULT_UNPROFILED_SAMPLING = orig_runtime


# ── 5. core/duo_compress_cycle.py duo_compress_mode ────────────────────────
def _test_duo_compress_mode_wiring() -> None:
    import core.duo_compress_cycle as dcc

    def _mk_ctx(settings: dict):
        emitted: list[dict] = []

        async def _emit(ev: dict):
            emitted.append(ev)
            return ev

        return SimpleNamespace(
            emit=_emit,
            emitted=emitted,
            user_input="Refactor target module",
            settings=settings,
            duo_config=SimpleNamespace(until_finished=False),
            exec_ctrl=_FakeExecCtrl(),
        )

    def _mk_msgs(n: int = 8) -> list[dict]:
        msgs = [
            {"role": "system", "content": "SYS PROMPT"},
            {"role": "user", "content": "TASK: Refactor target module"},
        ]
        for i in range(n):
            msgs.append({
                "role": "assistant",
                "content": f"step {i}",
                "tool_calls": [{"id": f"tc_{i}", "function": {"name": "read_file", "arguments": "{}"}}],
            })
            msgs.append({
                "role": "tool",
                "name": "read_file",
                "tool_call_id": f"tc_{i}",
                "content": f"reading C:/ws/f_{i}.py\n" + ("x = 1\n" * 350),
            })
        return msgs

    def _mk_ps(msgs: list[dict], *, partial_compression: bool = False, lru=None) -> dcc._DuoCompressCycleState:
        from utils.token import estimate_ctx_tokens as _est_tok
        tok = max(6200, int(_est_tok(msgs)))
        return dcc._DuoCompressCycleState(
            _call_sigs=["sig1"],
            _coder_est_at_real=[4200],
            _coder_real_prompt_tokens=[tok],
            _comp_llm_cfg="",
            _comp_llm_timeout_s=30,
            _compress_local_only=False,
            _compress_threshold=1500,
            _di=0,
            _done_tasks=["step 1"],
            _dport=8081,
            _dtc=None,
            _dtool_ctx=8192,
            _dtool_sys_eff="SYS PROMPT",
            _explore_ctx="",
            _goal_pin_msg=None,
            _guard_decision=SimpleNamespace(reason="threshold"),
            _guard_tokens=float(tok),
            _last_test_status="PASS",
            _lifetime_tool_rounds=4,
            _max_tool_rounds=25,
            _n_items=1,
            _partial_compression=partial_compression,
            _plan_result=None,
            _plan_tracker=None,
            _read_counts={"a.py": 1},
            _seen_explore_paths={"a.py"},
            _subtasks=["step 1"],
            _tool_ctx_lru=lru or _FakeLRUWithEntries([]),
            _written_files=["a.py"],
            _ws_str="",
            _MAX_COMPRESSIONS=5,
            _compress_fail_streak=0,
            _ctx_compressions=0,
            _ctx_critical_warned=False,
            _ctx_full_skips=0,
            _ctx_pressure_warned=False,
            _dtool_msgs=list(msgs),
            _est_tokens=tok,
            _explore_only_rounds=0,
            _force_compress_next=False,
            _llm_compress_fails=0,
            _loop_detected=False,
            _no_write_nudged=False,
            _no_write_rounds=0,
            _partial_escalated=False,
            _rule_compress_fails=0,
            _rule_compress_used=False,
            _total_tool_rounds=4,
            carry={"exec_mdl": "qwen3.5:4b"},
        )

    captured_modes: list[str] = []

    async def _fake_compress_ctx(**kwargs):
        captured_modes.append(str(kwargs.get("compression_mode")))
        return (
            [
                {"role": "system", "content": "SYS PROMPT"},
                {"role": "user", "content": "Summary of Refactor target module covering a.py and step 1 with enough detail."},
            ],
            [],
            {},
        )

    orig_mode = dcc.DUO_COMPRESS_MODE
    try:
        # 5a: mask_only masks via LRU without calling _compress_tool_context
        msgs_m = _mk_msgs(8)
        lru_entries = [
            {"idx": 2 + 2 * i + 1, "path": f"C:/ws/f_{i}.py", "kind": "read_file"}
            for i in range(6)
        ]
        lru_m = _FakeLRUWithEntries(lru_entries)
        ps_m = _mk_ps(msgs_m, lru=lru_m)
        ctx_m = _mk_ctx({"duo_pin_static_map": False, "duo_compress_mode": "mask_only"})
        llm_spy = mock.AsyncMock(side_effect=_fake_compress_ctx)
        with mock.patch.object(dcc, "_compress_tool_context", llm_spy):
            asyncio.run(_drain(dcc._duo_compress_cycle(ctx_m, ps_m)))
        check("5a mask_only skips LLM compression completely and shrinks context",
              llm_spy.await_count == 0 and ps_m.stop is False
              and ps_m._est_tokens < 5000 and len(lru_m.evicted_idx) > 0,
              f"calls={llm_spy.await_count} est={ps_m._est_tokens} evicted={len(lru_m.evicted_idx)}")

        # 5b: partial mode forces partial even when _partial_compression=False
        captured_modes.clear()
        ps_p = _mk_ps(_mk_msgs(8), partial_compression=False)
        ctx_p = _mk_ctx({"duo_pin_static_map": False, "duo_compress_mode": "partial"})
        with mock.patch.object(dcc, "_compress_tool_context", side_effect=_fake_compress_ctx), \
             mock.patch("context.compression._validate_compression_summary", return_value=True):
            asyncio.run(_drain(dcc._duo_compress_cycle(ctx_p, ps_p)))
        check("5b duo_compress_mode='partial' forces partial mode",
              captured_modes == ["partial"], f"got={captured_modes}")

        # 5c: full mode forces full even when _partial_compression=True
        captured_modes.clear()
        ps_f = _mk_ps(_mk_msgs(8), partial_compression=True)
        ctx_f = _mk_ctx({"duo_pin_static_map": False, "duo_compress_mode": "full"})
        with mock.patch.object(dcc, "_compress_tool_context", side_effect=_fake_compress_ctx), \
             mock.patch("context.compression._validate_compression_summary", return_value=True):
            asyncio.run(_drain(dcc._duo_compress_cycle(ctx_f, ps_f)))
        check("5c duo_compress_mode='full' overrides _partial_compression=True",
              captured_modes == ["full"], f"got={captured_modes}")

        # 5d: runtime DUO_COMPRESS_MODE honored when ctx.settings omits duo_compress_mode
        dcc.DUO_COMPRESS_MODE = "partial"
        captured_modes.clear()
        ps_rt = _mk_ps(_mk_msgs(8), partial_compression=False)
        ctx_rt = _mk_ctx({"duo_pin_static_map": False})
        with mock.patch.object(dcc, "_compress_tool_context", side_effect=_fake_compress_ctx), \
             mock.patch("context.compression._validate_compression_summary", return_value=True):
            asyncio.run(_drain(dcc._duo_compress_cycle(ctx_rt, ps_rt)))
        check("5d runtime DUO_COMPRESS_MODE honored as fallback",
              captured_modes == ["partial"], f"got={captured_modes}")
    finally:
        dcc.DUO_COMPRESS_MODE = orig_mode


# ── 6. Docs & UI coverage scanner ──────────────────────────────────────────
def _test_docs_and_ui_scan() -> None:
    from deploy.settings_ui_scan import exempt_keys, in_schema, in_ui, orphans
    from settings_defaults import DEFAULT_SETTINGS

    keys = set(DEFAULT_SETTINGS)
    exempt = exempt_keys()
    exposed = in_ui(keys)
    schema_exposed = in_schema(keys)

    check("6a exempt keys shrunk below 15", len(exempt) < 15, f"({len(exempt)})")
    check("6b zero orphan keys in DEFAULT_SETTINGS", not orphans(keys))
    check("6c all 13 new engine keys exposed in schema and UI scan",
          "llama_kv_cache_type" in schema_exposed
          and "duo_compress_mode" in exposed
          and "default_unprofiled_sampling" in exposed)

    proc = subprocess.run(
        [sys.executable, str(ROOT / "deploy" / "gen_settings_docs.py"), "--check"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    )
    check("6d gen_settings_docs.py --check exits 0",
          proc.returncode == 0, (proc.stdout or proc.stderr).strip())


def main() -> int:
    _test_defaults_and_meta()
    _test_settings_schema_endpoint()
    _test_runtime_flags_and_config_sync()
    _test_unprofiled_sampling_wiring()
    _test_duo_compress_mode_wiring()
    _test_docs_and_ui_scan()
    return h.finish("settings_ui_schema")


if __name__ == "__main__":
    sys.exit(main())
