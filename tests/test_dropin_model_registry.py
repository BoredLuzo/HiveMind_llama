# -*- coding: utf-8 -*-
"""Regression suite for C1: Two-Layer Model Registry & GGUF-First Auto-Discovery."""
from __future__ import annotations

import asyncio
import json
import struct
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _pack_gguf_v3(
    path: Path,
    *,
    arch: str = "llama",
    name: str = "DropInModel",
    chat_template: str | None = None,
    has_think_token: bool = False,
    vision_blocks: int = 0,
    expert_count: int = 0,
    block_count: int = 32,
    head_count: int = 32,
    head_count_kv: int = 4,
    emb_len: int = 2048,
) -> Path:
    """Write a minimal valid GGUF v3 file with requested metadata KV entries."""
    kv_entries: list[tuple[str, int, bytes]] = []

    def _s(text: str) -> bytes:
        b = text.encode("utf-8")
        return struct.pack("<Q", len(b)) + b

    def add_str(key: str, val: str) -> None:
        kv_entries.append((key, 8, _s(val)))

    def add_u32(key: str, val: int) -> None:
        kv_entries.append((key, 4, struct.pack("<I", val)))

    add_str("general.architecture", arch)
    add_str("general.name", name)
    add_u32(f"{arch}.block_count", block_count)
    add_u32(f"{arch}.context_length", 32768)
    add_u32(f"{arch}.embedding_length", emb_len)
    add_u32(f"{arch}.attention.head_count", head_count)
    add_u32(f"{arch}.attention.head_count_kv", head_count_kv)
    if expert_count > 0:
        add_u32(f"{arch}.expert_count", expert_count)
        add_u32(f"{arch}.expert_used_count", min(2, expert_count))
    if vision_blocks > 0:
        add_u32(f"{arch}.vision.block_count", vision_blocks)
    if chat_template is not None:
        add_str("tokenizer.chat_template", chat_template)
    if has_think_token:
        toks = [b"hello", b"<think>", b"world"]
        arr_payload = struct.pack("<IQ", 8, len(toks))
        for tok in toks:
            arr_payload += struct.pack("<Q", len(tok)) + tok
        kv_entries.append(("tokenizer.ggml.tokens", 9, arr_payload))

    out = bytearray()
    out += b"GGUF"
    out += struct.pack("<IQQ", 3, 0, len(kv_entries))
    for k, vtype, payload in kv_entries:
        out += _s(k)
        out += struct.pack("<I", vtype)
        out += payload

    path.write_bytes(bytes(out))
    return path


# ── 1. Two-Layer Registry (user > shipped > gguf_auto > generic) ─────────────
import model_configs.models_registry as mr  # noqa: E402
from backend import gguf_meta as gm  # noqa: E402
from backend import llama_models as lm  # noqa: E402

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    tmp = Path(td)
    shipped_dir = tmp / "shipped"
    user_dir = tmp / "user"
    models_dir = tmp / "ggufs"
    shipped_dir.mkdir()
    user_dir.mkdir()
    models_dir.mkdir()

    (shipped_dir / "ship-test_7b.json").write_text(
        json.dumps({
            "model": "ship-test:7b",
            "reasoning": "on",
            "moe_cpu_experts": 30,
            "num_ctx": 16384,
            "capabilities": {"thinking": True, "vision": False, "tool_call": True},
        }),
        encoding="utf-8",
    )
    auto_gguf = _pack_gguf_v3(
        models_dir / "AutoDrop-3B-Q4_K_M.gguf",
        chat_template="{% if tools %}<tool_call>{% endif %}",
        has_think_token=True,
    )

    orig_models_dir = mr._MODELS_DIR
    orig_user_dir = mr.USER_DIR
    orig_lm_dir = lm.MODELS_DIR
    orig_index_cache = lm._index_cache
    orig_index_ts = lm._index_ts
    try:
        mr._MODELS_DIR = shipped_dir
        mr.USER_DIR = user_dir
        mr.refresh()
        lm.MODELS_DIR = models_dir
        lm.invalidate_caches()
        gm.clear_cache()

        check("1a shipped profile source & values",
              mr.get_profile_source("ship-test:7b") == "shipped"
              and mr.get_profile("ship-test:7b").get("moe_cpu_experts") == 30)

        saved_path = mr.save_user_profile(
            "ship-test:7b",
            {"moe_cpu_experts": 22, "extra_args": ["--custom-flag"]},
        )
        check("1b save_user_profile writes into USER_DIR atomically",
              saved_path.parent == user_dir
              and saved_path.is_file()
              and not list(user_dir.glob("*.tmp*")))

        merged = mr.get_profile("ship-test:7b")
        check("1c user profile overrides shipped and preserves un-overridden shipped fields",
              mr.get_profile_source("ship-test:7b") == "user"
              and merged.get("moe_cpu_experts") == 22
              and merged.get("reasoning") == "on"
              and merged.get("num_ctx") == 16384
              and merged.get("extra_args") == ["--custom-flag"],
              f"merged={merged}")

        check("1d delete_user_profile falls back to shipped",
              mr.delete_user_profile("ship-test:7b") is True
              and mr.delete_user_profile("ship-test:7b") is False
              and mr.get_profile_source("ship-test:7b") == "shipped"
              and mr.get_profile("ship-test:7b").get("moe_cpu_experts") == 30)

        bad_slugs_blocked = True
        for bad in ("", "   ", "../evil", "a/b", "a\\b", ".hidden", "bad;cmd", "x..y"):
            try:
                mr.save_user_profile(bad, {"num_ctx": 4096})
                bad_slugs_blocked = False
            except ValueError:
                pass
        check("1e save_user_profile slug validation blocks unsafe names", bad_slugs_blocked)

        # Direct drop into user_dir recognized via refresh()
        (user_dir / "manual-drop_2b.json").write_text(
            json.dumps({"model": "manual-drop:2b", "num_ctx": 8192}),
            encoding="utf-8",
        )
        mr.refresh()
        check("1f refresh picks up externally added user JSON",
              mr.get_profile_source("manual-drop:2b") == "user"
              and mr.get_profile("manual-drop:2b").get("num_ctx") == 8192)

        check("1g gguf_auto and generic profile sources",
              mr.get_profile_source(str(auto_gguf)) == "gguf_auto"
              and mr.get_profile_source("totally-absent:99b") == "generic")
    finally:
        mr._MODELS_DIR = orig_models_dir
        mr.USER_DIR = orig_user_dir
        mr.refresh()
        lm.MODELS_DIR = orig_lm_dir
        lm.invalidate_caches()
        gm.clear_cache()


# ── 2. GGUF Metadata: has_tool_call & Sibling Auto-Detection ──────────────────
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    d = Path(td)
    single_dir = d / "single"
    multi_dir = d / "multi"
    single_dir.mkdir()
    multi_dir.mkdir()
    gm.clear_cache()

    t_gguf = _pack_gguf_v3(
        single_dir / "NovaVision-7B-Q4_K_M.gguf",
        chat_template="{%- if tools %}<|tool_call|>{{ tools }}{%- endif %}",
        has_think_token=True,
    )
    plain_gguf = _pack_gguf_v3(
        multi_dir / "PlainChat-4B-Q4_K_M.gguf",
        chat_template="{% for m in messages %}{{ m.content }}{% endfor %}",
        has_think_token=False,
    )
    other_gguf = _pack_gguf_v3(
        multi_dir / "AlphaCoder-8B-Q4_K_M.gguf",
        chat_template="<|call:fn|>",
        expert_count=64,
    )

    (single_dir / "mmproj-NovaVision-7B-F16.gguf").write_bytes(b"GGUF" + b"\x00" * 64)
    (single_dir / "mtp-NovaVision-7B-Q8_0.gguf").write_bytes(b"GGUF" + b"\x00" * 64)
    (single_dir / "NovaVision-7B-DFlash-Q6_K.gguf").write_bytes(b"GGUF" + b"\x00" * 64)
    (single_dir / "NovaVision-1.2B-DSpark-Q4_K_M.gguf").write_bytes(b"GGUF" + b"\x00" * 64)

    (multi_dir / "mmproj-AlphaCoder-8B-F16.gguf").write_bytes(b"GGUF" + b"\x00" * 64)
    (multi_dir / "AlphaCoder-8B-DFlash-Q6_K.gguf").write_bytes(b"GGUF" + b"\x00" * 64)
    (multi_dir / "mmproj-F16.gguf").write_bytes(b"GGUF" + b"\x00" * 64)

    t_meta = gm.read_gguf_metadata(t_gguf)
    p_meta = gm.read_gguf_metadata(plain_gguf)
    o_meta = gm.read_gguf_metadata(other_gguf)

    check("2a has_tool_call detected accurately from chat_template",
          t_meta.get("has_tool_call") is True
          and t_meta.get("has_thinking") is True
          and p_meta.get("has_tool_call") is False
          and o_meta.get("has_tool_call") is True
          and o_meta.get("is_moe") is True)

    sibs = t_meta.get("siblings") or {}
    check("2b sibling auto-detection finds mmproj, mtp, dflash, dspark",
          bool(sibs.get("mmproj")) and "mmproj-NovaVision" in sibs["mmproj"]
          and bool(sibs.get("mtp")) and "mtp-NovaVision" in sibs["mtp"]
          and bool(sibs.get("dflash")) and "DFlash" in sibs["dflash"]
          and bool(sibs.get("dspark")) and "DSpark" in sibs["dspark"]
          and t_meta.get("has_vision") is True,
          f"sibs={sibs}")

    check("2c multi-model directory isolates named and generic siblings",
          p_meta.get("has_vision") is False
          and (p_meta.get("siblings") or {}).get("mmproj") is None
          and (p_meta.get("siblings") or {}).get("dflash") is None
          and bool((o_meta.get("siblings") or {}).get("mmproj"))
          and bool((o_meta.get("siblings") or {}).get("dflash")))

    # llama_models indexes drop-in GGUF + resolves sibling mmproj & drafters without allowlists
    orig_lm_dir2 = lm.MODELS_DIR
    try:
        lm.MODELS_DIR = single_dir
        lm.invalidate_caches()
        listed = lm.list_available_models(force_refresh=True)
        mm = lm.resolve_mmproj_path("novavision:7b")
        dr_df = lm.resolve_sibling_drafter("novavision:7b", "dflash")
        dr_ds = lm.resolve_sibling_drafter("novavision:7b", "dspark")
        dr_mtp = lm.resolve_sibling_drafter("novavision:7b", "mtp")
        check("2d llama_models lists arbitrary drop-in GGUF & resolves all siblings",
              "novavision:7b" in listed
              and all(x not in listed for x in ("mtp-novavision:7b",))
              and mm is not None and mm.name == "mmproj-NovaVision-7B-F16.gguf"
              and dr_df is not None and dr_df.name == "NovaVision-7B-DFlash-Q6_K.gguf"
              and dr_ds is not None and dr_ds.name == "NovaVision-1.2B-DSpark-Q4_K_M.gguf"
              and dr_mtp is not None and dr_mtp.name == "mtp-NovaVision-7B-Q8_0.gguf",
              f"listed={listed}, mm={mm}, df={dr_df}, ds={dr_ds}, mtp={dr_mtp}")
    finally:
        lm.MODELS_DIR = orig_lm_dir2
        lm.invalidate_caches()
        gm.clear_cache()


# ── 3. Data-Driven Compat Views & Boy-Scout Checks ───────────────────────────
import backend.llama_config as lc  # noqa: E402
import backend.llama_vram_table as lvt  # noqa: E402
import backend.llama_manager_utils as lmu  # noqa: E402
import backend.llama_start_phases as lsp  # noqa: E402
import routing.model_automap as ma  # noqa: E402
import routing.model_picker as mp  # noqa: E402

compat_json = ROOT / "model_configs" / "arch_compat.json"
check("3a arch_compat.json exists & compatibility views populated dynamically",
      compat_json.is_file()
      and lc.BINARY_MIN_BUILD.get("lfm3", 0) >= 12001
      and lc._MOE_EXPERT_COUNTS.get("qwen3.6:35b-a3b") == 35
      and lvt._MOE_TABLE.get("qwen3.6:35b-a3b", {}).get("calibrated_n_cpu_moe") == 35
      and ma.MODEL_PROFILES.get("lfm3", {}).get("tool_call") is True
      and bool(mp._SIMPLE_DIRECT_MODELS)
      and bool(mp._COMPLEX_DIRECT_MODELS))

# Dynamic reload picks up newly added user profile into compat tables without Python edits
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    u_dir = Path(td)
    orig_u = mr.USER_DIR
    try:
        mr.USER_DIR = u_dir
        mr.save_user_profile(
            "zenith-moe:18b-a3b",
            {
                "moe_cpu_experts": 20,
                "vram_gb_override": 4.15,
                "capabilities": {"thinking": True, "vision": False, "tool_call": True},
            },
        )
        lc.reload_compat_views()
        lvt.reload_compat_views()
        ma.reload_compat_views()
        check("3b user drop-in profile dynamically populates compat dicts",
              lc._MOE_EXPERT_COUNTS.get("zenith-moe:18b-a3b") == 20
              and lvt._MOE_TABLE.get("zenith-moe:18b-a3b", {}).get("active_gpu_gb") == 4.15
              and ma.MODEL_PROFILES.get("zenith-moe:18b-a3b", {}).get("tool_call") is True)
    finally:
        mr.USER_DIR = orig_u
        mr.refresh()
        lc.reload_compat_views()
        lvt.reload_compat_views()
        ma.reload_compat_views()

# Boy-Scout checks: _try_registry_field + single get_profile lookup in _phase_model_flags
check("3c _try_registry_field helper & 7 wrapper exports intact",
      callable(getattr(lc, "_try_registry_field", None))
      and all(callable(getattr(lc, fn, None)) for fn in (
          "_try_registry_moe", "_try_registry_gpu_layers", "_try_registry_mtp",
          "_try_registry_dspark_draft", "_try_registry_dflash_draft",
          "_try_registry_mtp_draft", "_try_registry_flash_attn_off",
      )))

get_prof_calls = 0
_orig_gp = mr.get_profile


def _counting_gp(name: str) -> dict:
    global get_prof_calls
    get_prof_calls += 1
    return _orig_gp(name)


import types  # noqa: E402


def _make_fake_mgr(**overrides):
    attrs = dict(
        _reasoning_override=None,
        _binary_build_number=12500,
        _dspark_flag_supported=True,
        _device_flag_supported=True,
        _kv_flag_supported=True,
        _moe_flag_supported=True,
        _load_mode_supported=True,
        _backend_dlls_ok=True,
        _backend_devices_ok=True,
    )
    attrs.update(overrides)
    return type("FakeMgr", (), attrs)()


_fake_mgr = _make_fake_mgr()
with mock.patch.object(mr, "get_profile", side_effect=_counting_gp), \
        mock.patch.object(lsp, "_resolve_spec_config", new=mock.AsyncMock(return_value=None)), \
        mock.patch.object(lsp, "_needs_mmproj", return_value=False), \
        mock.patch("backend.gguf_meta.read_gguf_metadata", return_value={}):
    asyncio.run(lsp._phase_model_flags(_fake_mgr, [], "unknown-boyscout:7b", "unknown-boyscout", Path("x.gguf"), False))
check("3d _phase_model_flags performs single get_profile lookup (reduced deepcopies)",
      get_prof_calls == 1, f"calls={get_prof_calls}")


# ── 4. Runtime-Configurable Backend Attributes ────────────────────────────────
check("4a runtime attributes exported with expected defaults",
      lc.KV_CACHE_TYPE == "q4_0"
      and lc.MAX_SLOTS == 3
      and lc.GPU_DEVICE == 0
      and lc.LLAMA_EXTRA_ARGS == ""
      and lc.SPEC_DECODE_MODE == "auto"
      and lc.MTP_DRAFT_N_MAX == 2
      and lc.MTP_DRAFT_P_MIN == 0.0
      and lc.DFLASH_DRAFT_P_MIN == 0.4
      and lc.TOTAL_VRAM_MIB_OVERRIDE == 0
      and lmu.VRAM_PRE_FLIGHT_MARGIN_MIB == 768)

# SPEC_DECODE_MODE="off" gates spec decode cleanly
with mock.patch.object(lc, "SPEC_DECODE_MODE", "off"):
    _spec_off = asyncio.run(lsp._resolve_spec_config(_fake_mgr, "qwen3.5:4b-mtp"))
check("4b SPEC_DECODE_MODE='off' disables spec decoding", _spec_off is None)

# LLAMA_EXTRA_ARGS parsed via shlex.split cleanly in _phase_model_flags
with mock.patch.object(lc, "LLAMA_EXTRA_ARGS", "--threads-http 2 --prio 1"), \
        mock.patch.object(lsp, "_resolve_spec_config", new=mock.AsyncMock(return_value=None)), \
        mock.patch.object(lsp, "_needs_mmproj", return_value=False), \
        mock.patch("backend.gguf_meta.read_gguf_metadata", return_value={}):
    ph_extra = asyncio.run(lsp._phase_model_flags(
        _fake_mgr, [], "unknown-extra:7b", "unknown-extra", Path("x.gguf"), False
    ))
check("4c LLAMA_EXTRA_ARGS wired into command via shlex.split",
      "--threads-http" in ph_extra.cmd and "2" in ph_extra.cmd and "--prio" in ph_extra.cmd)

# GPU_DEVICE honored in _phase_capabilities
with mock.patch.object(lc, "GPU_DEVICE", 2), \
        mock.patch.object(lsp, "GPU_BACKEND", "vulkan"):
    ph_dev = asyncio.run(lsp._phase_capabilities(_fake_mgr, ["llama-server"], False))
check("4d GPU_DEVICE wired into --device flag",
      ph_dev.cmd[-2:] == ["--device", "VULKAN2"], f"cmd={ph_dev.cmd}")

# TOTAL_VRAM_MIB_OVERRIDE honored in _effective_total_vram_mib
with mock.patch.object(lvt, "TOTAL_VRAM_MIB_OVERRIDE", 16384):
    check("4e TOTAL_VRAM_MIB_OVERRIDE honored when > 0",
          lvt._effective_total_vram_mib() == 16384)

# VRAM_PRE_FLIGHT_MARGIN_MIB honored live in resolve_ctx_fit
with mock.patch.object(lmu, "VRAM_PRE_FLIGHT_MARGIN_MIB", 1024):
    fit_blocked = lmu.resolve_ctx_fit(16384, 5800.0, lambda c: 5000.0, allow_graceful=False)
    fit_ok = lmu.resolve_ctx_fit(16384, 6100.0, lambda c: 5000.0, allow_graceful=False)
check("4f live VRAM_PRE_FLIGHT_MARGIN_MIB honored by resolve_ctx_fit",
      fit_blocked == (16384, 256) and fit_ok == (16384, 1024),
      f"blocked={fit_blocked} ok={fit_ok}")


# ── 5. Dynamic Auto-Resolve & Fallbacks ───────────────────────────────────────
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    d = Path(td)
    small_vis = _pack_gguf_v3(d / "TinyVis-1.5B-Q4_K_M.gguf", vision_blocks=4, chat_template="{% if tools %}t{% endif %}")
    big_vis = _pack_gguf_v3(d / "HugeVis-8B-Q4_K_M.gguf", vision_blocks=8, chat_template="{% if tools %}t{% endif %}")
    small_tool = _pack_gguf_v3(d / "PocketCoder-1B-Q4_K_M.gguf", chat_template="{% if tools %}<tool_call>{% endif %}")
    # Make big_vis larger on disk than small_vis and PocketCoder tiny
    with small_tool.open("ab") as fh:
        fh.write(b"\x00" * 100)
    with small_vis.open("ab") as fh:
        fh.write(b"\x00" * 500)
    with big_vis.open("ab") as fh:
        fh.write(b"\x00" * 5000)

    orig_lm_d = lm.MODELS_DIR
    try:
        lm.MODELS_DIR = d
        lm.invalidate_caches()
        gm.clear_cache()

        vis_pick = ma.pick_installed_vision_model()
        tool_ladder = ma.pick_installed_small_tool_models()
        check("5a pick_installed_vision_model chooses smallest installed vision model",
              vis_pick == "tinyvis:1.5b", f"vis_pick={vis_pick}")
        check("5b pick_installed_small_tool_models orders smallest-first",
              bool(tool_ladder) and tool_ladder[0] == "pocketcoder:1b",
              f"tool_ladder={tool_ladder}")

        # subagent_lite auto-resolves empty ladder while respecting explicit list
        from core import subagent_lite as sl
        explicit = sl._resolve_subagent_ladder({"subagent_lite_model_ladder": ["explicit:2b"]})
        auto_resolved = sl._resolve_subagent_ladder({"subagent_lite_model_ladder": []})
        check("5c subagent_lite preserves explicit ladder & auto-resolves empty ladder",
              explicit == ["explicit:2b"] and auto_resolved == tool_ladder,
              f"explicit={explicit} auto={auto_resolved}")

        # model_picker picks installed custom models dynamically in automap mode
        _fake_pipe = types.SimpleNamespace(agents={"direct": types.SimpleNamespace(model="")})
        _op, _os = mp.pipeline, mp.settings
        try:
            mp.init_model_picker(_fake_pipe, {"mode": "automap", "automap_excluded": []})
            picked_vis = mp._pick_direct_model("simple", "vision", ["pocketcoder:1b", "tinyvis:1.5b"])
            picked_code = mp._pick_direct_model("simple", "code", ["pocketcoder:1b"])
            check("5d _pick_direct_model selects installed drop-ins dynamically",
                  picked_vis == "tinyvis:1.5b" and picked_code == "pocketcoder:1b",
                  f"picked_vis={picked_vis} picked_code={picked_code}")
        finally:
            mp.init_model_picker(_op, _os)

        # server._auto_resolve_empty_model_defaults fills empty vision_agent_model cleanly
        import server as srv
        test_cfg = {"vision_agent_model": "", "subagent_lite_model_ladder": []}
        srv._auto_resolve_empty_model_defaults(test_cfg)
        check("5e server._auto_resolve_empty_model_defaults fills empty defaults cleanly",
              test_cfg["vision_agent_model"] == "tinyvis:1.5b"
              and test_cfg["subagent_lite_model_ladder"] == tool_ladder)
    finally:
        lm.MODELS_DIR = orig_lm_d
        lm.invalidate_caches()
        gm.clear_cache()

# Clean no-crash handling when ZERO vision models exist
check("5f pick_installed_vision_model returns empty cleanly when none installed",
      ma.pick_installed_vision_model([]) == "")


# ── 6. Installer / Updater / Gitignore Protection ─────────────────────────────
gi_txt = (ROOT / ".gitignore").read_text(encoding="utf-8")
up_txt = (ROOT / "deploy" / "updater.py").read_text(encoding="utf-8")
sh_txt = (ROOT / "deploy" / "install_linux.sh").read_text(encoding="utf-8")
ps_txt = (ROOT / "deploy" / "update_prune.ps1").read_text(encoding="utf-8")

check("6a .gitignore protects model_configs/user/",
      "model_configs/user/" in gi_txt)
check("6b deploy/updater.py excludes model_configs/user in apply_cmd",
      'str(exroot / "model_configs" / "user")' in up_txt)
check("6c deploy/install_linux.sh excludes model_configs/user in rsync & tar",
      "--exclude '/model_configs/user'" in sh_txt
      and "--exclude='./model_configs/user'" in sh_txt)
check("6d deploy/update_prune.ps1 protects model_configs\\user",
      "model_configs\\user" in ps_txt)

sys.exit(h.finish("dropin_model_registry"))
