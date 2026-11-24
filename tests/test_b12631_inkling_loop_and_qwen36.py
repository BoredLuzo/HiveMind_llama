# -*- coding: utf-8 -*-
"""Regression suite for llama.cpp b12631, Inkling-2-Mini looped-MoE offload,
reasoning_effort propagation, and Qwen3.6 / Inkling model configs."""
from __future__ import annotations

import asyncio
import json
import struct
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _pack_kv_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<Q", len(b)) + b


def _build_synthetic_inkling_gguf(path: Path) -> None:
    """Write a minimal valid GGUF v3 header for an inkling2_loop MoE model."""
    # Indices 5, 11, 17, 23 are full-attention (False); after loop 6..18 (3x):
    # outer [0..5] -> 1, core [6..18) * 3 -> 6, outer [18..24) -> 1 => 8 full-attn layers.
    swa_24 = [((i + 1) % 6) != 0 for i in range(24)]
    tpl = "{% if reasoning_effort == 'none' %}<|content_thinking|>{% endif %}"
    kvs: list[tuple[str, int, bytes]] = [
        ("general.architecture", 8, _pack_kv_str("inkling2_loop")),
        ("general.name", 8, _pack_kv_str("Inkling-Test")),
        ("inkling2_loop.block_count", 4, struct.pack("<I", 24)),
        ("inkling2_loop.embedding_length", 4, struct.pack("<I", 2304)),
        ("inkling2_loop.attention.key_length", 4, struct.pack("<I", 128)),
        ("inkling2_loop.attention.value_length", 4, struct.pack("<I", 128)),
        ("inkling2_loop.expert_count", 4, struct.pack("<I", 128)),
        ("inkling2_loop.expert_used_count", 4, struct.pack("<I", 8)),
        ("inkling2_loop.loop.count", 4, struct.pack("<I", 3)),
        ("inkling2_loop.loop.start", 4, struct.pack("<I", 6)),
        ("inkling2_loop.loop.end", 4, struct.pack("<I", 18)),
        ("inkling2_loop.attention.head_count", 9, struct.pack("<IQ24I", 4, 24, *([24] * 24))),
        ("inkling2_loop.attention.head_count_kv", 9, struct.pack("<IQ24I", 4, 24, *([8] * 24))),
        ("inkling2_loop.attention.sliding_window_pattern", 9, struct.pack("<IQ24B", 7, 24, *[int(x) for x in swa_24])),
        ("tokenizer.chat_template", 8, _pack_kv_str(tpl)),
    ]
    buf = bytearray()
    buf += b"GGUF"
    buf += struct.pack("<IQQ", 3, 0, len(kvs))
    for k, vt, payload in kvs:
        buf += _pack_kv_str(k)
        buf += struct.pack("<I", vt)
        buf += payload
    path.write_bytes(bytes(buf))


# ── Step 1: Pinned llama.cpp build ─────────────────────────────────────
print("-- Step 1: Pinned build tag --")
import deploy.fetch_llamacpp as FL  # noqa: E402
check("deploy/llama_build_pin.txt pins >= b12645",
      (_tag := FL._read_pinned_tag()).startswith("b") and _tag[1:].isdigit() and int(_tag[1:]) >= 12645,
      repr(FL._read_pinned_tag()))


# ── Step 2: GGUF metadata for inkling2_loop & reasoning_effort ─────────
print("-- Step 2: gguf_meta inkling2_loop --")
import backend.gguf_meta as GM  # noqa: E402
with tempfile.TemporaryDirectory() as _td:
    _gp = Path(_td) / "Inkling-Test.gguf"
    _build_synthetic_inkling_gguf(_gp)
    GM.clear_cache()
    _meta = GM.read_gguf_metadata(_gp)
    check("gguf_meta reads head_count list -> 24", _meta.get("head_count") == 24, repr(_meta.get("head_count")))
    check("gguf_meta reads loop_count/start/end == 3/6/18",
          _meta.get("loop_count") == 3 and _meta.get("loop_start") == 6 and _meta.get("loop_end") == 18,
          repr((_meta.get("loop_count"), _meta.get("loop_start"), _meta.get("loop_end"))))
    check("gguf_meta detects thinking from reasoning_effort / <|content_thinking|>", _meta.get("has_thinking") is True)
    check("gguf_meta unrolls loop SWA pattern for 8 full-attn layers (9216 B/tok q4)",
          _meta.get("kv_bytes_per_token_q4") == 9216 and _meta.get("kv_bytes_per_token_f16") == 32768,
          repr((_meta.get("kv_bytes_per_token_q4"), _meta.get("kv_bytes_per_token_f16"))))


# ── Step 3: Looped-MoE Core-on-GPU --override-tensor & CPU share ──────
print("-- Step 3: llama_start_phases looped MoE & moe-cache-cpu-share --")
import backend.llama_start_phases as LSP  # noqa: E402
import backend.manager_load as ML  # noqa: E402

_ot12 = LSP._build_looped_moe_cpu_ot(24, 6, 18, 12)
_ot14 = LSP._build_looped_moe_cpu_ot(24, 6, 18, 14)
check("_build_looped_moe_cpu_ot(24, 6, 18, 12) puts only outer 0..5,18..23 on CPU",
      _ot12 == r"blk\.(0|1|2|3|4|5|18|19|20|21|22|23)\.ffn_.*_exps\.=CPU", repr(_ot12))
check("_build_looped_moe_cpu_ot(24, 6, 18, 14) puts outer + edge core 6,17 on CPU",
      _ot14 == r"blk\.(0|1|2|3|4|5|6|17|18|19|20|21|22|23)\.ffn_.*_exps\.=CPU", repr(_ot14))
check("_build_looped_moe_cpu_ot boundary 0 and 24 return None",
      LSP._build_looped_moe_cpu_ot(24, 6, 18, 0) is None and LSP._build_looped_moe_cpu_ot(24, 6, 18, 24) is None)
check("_parse_looped_moe_ot_count roundtrips 14", LSP._parse_looped_moe_ot_count(_ot14 or "") == 14)


class _DummyMgr:
    _moe_flag_supported = True
    _load_mode_supported = True
    _kv_flag_supported = True
    _slots: list = []

    def can_fit(self, model, num_ctx, **kwargs):
        return SimpleNamespace(ok=False, needed_mib=9000.0, free_mib=7500.0, margin_mib=768)


async def _test_phase_moe_looped() -> None:
    _mgr = _DummyMgr()
    _slot = SimpleNamespace(slot_id=0, model=None, _moe_cache_used=False)
    _fake_meta = {"architecture": "inkling2_loop", "block_count": 24, "loop_count": 3, "loop_start": 6, "loop_end": 18}

    async def _fake_help(flag: str) -> bool:
        return flag in ("--override-tensor", "--moe-cache-mib", "--moe-cache-cpu-share")

    with mock.patch.object(ML, "_help_supports", new=_fake_help), \
         mock.patch("backend.gguf_meta.read_gguf_metadata", return_value=_fake_meta), \
         mock.patch.object(LSP, "_should_lift_moe_to_all_gpu", new=mock.AsyncMock(return_value=(False, 0.0, "no"))), \
         mock.patch.object(LSP._llama_cfg, "MOE_CPU_EXPERTS", {"inkling-2-mini": 14}), \
         mock.patch.object(LSP._llama_cfg, "MOE_CACHE_MIB", 1024):
        res = await LSP._phase_moe(
            _mgr, _slot, ["--fit", "on", "--threads", "16", "--threads-batch", "16"],
            "inkling-2-mini", 32768, True, 16, 16, gguf_path=Path("Inkling-2-Mini.gguf"),
        )
    check("_phase_moe emits --override-tensor instead of --n-cpu-moe for looped MoE",
          "--override-tensor" in res.cmd and _ot14 in res.cmd and "--n-cpu-moe" not in res.cmd, repr(res.cmd))
    check("_phase_moe emits --moe-cache-cpu-share 0.9 alongside --moe-cache-mib",
          "--moe-cache-mib" in res.cmd and "--moe-cache-cpu-share" in res.cmd
          and res.cmd[res.cmd.index("--moe-cache-cpu-share") + 1] == "0.9",
          repr(res.cmd))


asyncio.run(_test_phase_moe_looped())

# Step-up auto CPU offload for --override-tensor
class _StepMgr:
    def can_fit(self, model, num_ctx, **kwargs):
        extra = float(kwargs.get("extra_needed_mib") or 0.0)
        ok = extra <= -150.0
        return SimpleNamespace(ok=ok, needed_mib=8000.0 + extra, free_mib=7500.0, margin_mib=768)


_step_cmd = ["--override-tensor", _ot14 or ""]
_step_slot = SimpleNamespace(slot_id=0)
_step_meta = {"block_count": 24, "loop_count": 3, "loop_start": 6, "loop_end": 18}
_init_fit = SimpleNamespace(ok=False, needed_mib=8000.0, free_mib=7500.0, margin_mib=768)

def _fake_vram_of_moe(key, ctx, n_cpu_moe=None):
    if n_cpu_moe is None:
        return 7.8
    return 7.8 - 0.1 * max(0, int(n_cpu_moe) - 14)

with mock.patch("backend.gguf_meta.read_gguf_metadata", return_value=_step_meta), \
     mock.patch.object(LSP, "vram_of_moe", side_effect=_fake_vram_of_moe):
    _f_step, _, _, _cand = LSP._auto_step_moe_cpu_offload(
        _StepMgr(), _step_slot, _step_cmd, "inkling-2-mini", Path("Inkling.gguf"), 32768,
        _init_fit, 0.0, 256, 256, "profile", 0.0,
    )
    check("_auto_step_moe_cpu_offload steps --override-tensor to 16 layers cleanly",
          _f_step.ok and _cand == 16 and "--override-tensor" in _step_cmd
          and LSP._parse_looped_moe_ot_count(_step_cmd[1]) == 16,
          repr((_cand, _step_cmd)))

_full_cmd = ["--override-tensor", _ot14 or ""]
with mock.patch("backend.gguf_meta.read_gguf_metadata", return_value=_step_meta), \
     mock.patch.object(LSP, "vram_of_moe", side_effect=lambda _k, _c, n_cpu_moe=None: 7.8 if (n_cpu_moe or 14) < 24 else 4.0):
    _f_full, _, _, _cand_full = LSP._auto_step_moe_cpu_offload(
        _StepMgr(), _step_slot, _full_cmd, "inkling-2-mini", Path("Inkling.gguf"), 32768,
        _init_fit, 0.0, 256, 256, "profile", 0.0,
    )
    check("_auto_step_moe_cpu_offload converts 24/24 step to --n-cpu-moe 24",
          _f_full.ok and _cand_full == 24 and _full_cmd == ["--n-cpu-moe", "24"],
          repr(_full_cmd))

import model_configs.models_registry as MR  # noqa: E402

check("models_registry exposes moe_cache_mib=2560 and share=0.95 for qwen3.6:35b-a3b-mtp-ud",
      MR.get_moe_cache_mib("qwen3.6:35b-a3b-mtp-ud") == 2560
      and MR.get_moe_cache_cpu_share("qwen3.6:35b-a3b-mtp-ud") == 0.95)
check("models_registry exposes moe_cache_mib=0 and share=None for inkling-2-mini",
      MR.get_moe_cache_mib("inkling-2-mini") == 0
      and MR.get_moe_cache_cpu_share("inkling-2-mini") is None)


async def _test_profile_moe_cache() -> None:
    _mgr = _DummyMgr()
    _slot = SimpleNamespace(slot_id=0, model=None, _moe_cache_used=False, _moe_cache_mib=0)

    async def _help_both(flag: str) -> bool:
        return flag in ("--moe-cache-mib", "--moe-cache-cpu-share")

    async def _help_no_cs(flag: str) -> bool:
        return flag == "--moe-cache-mib"

    with mock.patch.object(ML, "_help_supports", new=_help_both), \
         mock.patch("backend.gguf_meta.read_gguf_metadata", return_value={}), \
         mock.patch.object(LSP, "_should_lift_moe_to_all_gpu", new=mock.AsyncMock(return_value=(False, 0.0, "no"))), \
         mock.patch.object(LSP._llama_cfg, "MOE_CPU_EXPERTS", {}), \
         mock.patch.object(LSP._llama_cfg, "MOE_CACHE_MIB", 0):
        res_prof = await LSP._phase_moe(
            _mgr, _slot, ["--fit", "on", "--threads", "16", "--threads-batch", "16"],
            "qwen3.6:35b-a3b-mtp", 16384, True, 16, 16, gguf_path=None,
        )
    check("_phase_moe emits profile ncmoe=40, cache=2560, share=0.95 when global MOE_CACHE_MIB==0",
          "--n-cpu-moe" in res_prof.cmd
          and res_prof.cmd[res_prof.cmd.index("--n-cpu-moe") + 1] == "40"
          and "--moe-cache-mib" in res_prof.cmd
          and res_prof.cmd[res_prof.cmd.index("--moe-cache-mib") + 1] == "2560"
          and "--moe-cache-cpu-share" in res_prof.cmd
          and res_prof.cmd[res_prof.cmd.index("--moe-cache-cpu-share") + 1] == "0.95"
          and getattr(_slot, "_moe_cache_mib", 0) == 2560,
          repr(res_prof.cmd))

    _slot2 = SimpleNamespace(slot_id=0, model=None, _moe_cache_used=False, _moe_cache_mib=0)
    with mock.patch.object(ML, "_help_supports", new=_help_no_cs), \
         mock.patch("backend.gguf_meta.read_gguf_metadata", return_value={}), \
         mock.patch.object(LSP, "_should_lift_moe_to_all_gpu", new=mock.AsyncMock(return_value=(False, 0.0, "no"))), \
         mock.patch.object(LSP._llama_cfg, "MOE_CPU_EXPERTS", {}), \
         mock.patch.object(LSP._llama_cfg, "MOE_CACHE_MIB", 0):
        res_noshare = await LSP._phase_moe(
            _mgr, _slot2, ["--fit", "on", "--threads", "16", "--threads-batch", "16"],
            "qwen3.6:35b-a3b-mtp", 16384, True, 16, 16, gguf_path=None,
        )
    check("_phase_moe skips profile --moe-cache-mib when binary lacks --moe-cache-cpu-share",
          "--moe-cache-mib" not in res_noshare.cmd and res_noshare.moe_cache_used == 0
          and getattr(_slot2, "_moe_cache_mib", 0) == 0,
          repr(res_noshare.cmd))


asyncio.run(_test_profile_moe_cache())


class _TrimMgr:
    def __init__(self, max_extra_mib: float):
        self.max_extra_mib = float(max_extra_mib)

    def can_fit(self, model, num_ctx, **kwargs):
        extra = float(kwargs.get("extra_needed_mib") or 0.0)
        ok = extra <= self.max_extra_mib
        return SimpleNamespace(ok=ok, needed_mib=5000.0 + extra, free_mib=5000.0 + self.max_extra_mib, margin_mib=768)


_trim_slot = SimpleNamespace(slot_id=0, _moe_cache_used=True, _moe_cache_mib=2560)
_trim_cmd = ["--n-cpu-moe", "40", "--moe-cache-mib", "2560", "--moe-cache-cpu-share", "0.95"]
_trim_init = SimpleNamespace(ok=False, needed_mib=7760.0, free_mib=6700.0, margin_mib=768)
_f_trim, _new_extra = LSP._try_trim_moe_cache_mib(
    _TrimMgr(1736.0), _trim_slot, _trim_cmd, "qwen3.6:35b-a3b-mtp", 16384, _trim_init, 2760.0,
)
check("_try_trim_moe_cache_mib trims 2560 -> 1536 preserving other_extra",
      _f_trim.ok and _trim_cmd[3] == "1536" and "--moe-cache-cpu-share" in _trim_cmd
      and _new_extra == 1736.0 and _trim_slot._moe_cache_mib == 1536,
      repr((_trim_cmd, _new_extra, _trim_slot._moe_cache_mib)))

_drop_slot = SimpleNamespace(slot_id=0, _moe_cache_used=True, _moe_cache_mib=1024)
_drop_cmd = ["--n-cpu-moe", "40", "--moe-cache-mib", "1024", "--moe-cache-cpu-share", "0.95"]
_f_drop, _drop_extra = LSP._try_trim_moe_cache_mib(
    _TrimMgr(300.0), _drop_slot, _drop_cmd, "qwen3.6:35b-a3b-mtp", 16384, _trim_init, 1224.0,
)
check("_try_trim_moe_cache_mib strips cache & cpu-share cleanly when dropping to 0",
      _f_drop.ok and _drop_cmd == ["--n-cpu-moe", "40"]
      and _drop_extra == 200.0 and _drop_slot._moe_cache_used is False and _drop_slot._moe_cache_mib == 0,
      repr((_drop_cmd, _drop_extra, _drop_slot)))


# ── Step 4: reasoning_effort="none" propagation ────────────────────────
print("-- Step 4: reasoning_effort propagation --")
import backend.llama_client as LC  # noqa: E402
import core.llm_transport as LT  # noqa: E402

_pl = LC._build_payload([{"role": "user", "content": "hi"}], 1.0, 64, False, thinking=False)
check("_build_payload sets reasoning_effort='none' when thinking=False",
      _pl.get("reasoning_effort") == "none" and _pl.get("thinking_budget") == 0)

_dis = LT.disable_thinking_in_payload({"messages": []})
check("disable_thinking_in_payload sets reasoning_effort='none'",
      _dis.get("reasoning_effort") == "none" and _dis.get("thinking") is False and _dis.get("thinking_budget") == 0)

_stripped = LT.strip_payload_extras(_dis)
check("strip_payload_extras strips reasoning_effort",
      "reasoning_effort" not in _stripped and "thinking_budget" not in _stripped)


# ── Step 5: Model profiles & arch_compat ───────────────────────────────
print("-- Step 5: Shipped Inkling & Qwen3.6 configs --")
_ink_prof = MR.get_profile("inkling-2-mini") or {}
check("inkling-2-mini profile shipped with moe_cpu_experts=15, moe_cache_mib=0, ubatch=512, kv=9216",
      MR.get_profile_source("inkling-2-mini") == "shipped"
      and _ink_prof.get("moe_cpu_experts") == 15
      and _ink_prof.get("moe_cache_mib") == 0
      and _ink_prof.get("load_mode") == "none"
      and _ink_prof.get("ubatch") == 512
      and _ink_prof.get("kv_bytes_per_token") == 9216
      and _ink_prof.get("mmproj_filename") == "mmproj-Inkling-2-Mini-F16.gguf",
      repr(_ink_prof))

_ac = json.loads((_ROOT / "model_configs" / "arch_compat.json").read_text(encoding="utf-8"))
check("arch_compat registers inkling-2-mini (ncmoe=15, active_gpu_gb=5.4) and qwen3.6-mtp at ncmoe=40",
      _ac["moe_expert_counts"].get("inkling-2-mini") == 15
      and _ac["moe_table"].get("inkling-2-mini", {}).get("calibrated_n_cpu_moe") == 15
      and _ac["moe_table"].get("inkling-2-mini", {}).get("active_gpu_gb") == 5.4
      and _ac["model_profiles"].get("inkling-2-mini", {}).get("vision") is True
      and _ac["moe_expert_counts"].get("qwen3.6:35b-a3b-mtp") == 40
      and _ac["moe_table"].get("qwen3.6:35b-a3b-mtp", {}).get("calibrated_n_cpu_moe") == 40)

# ── Step 6: Mid-sized MoE load_mode=none headroom, ubatch & VRAM calibration ─
print("-- Step 6: Mid-sized MoE load_mode, ubatch & VRAM calibration --")
from backend.llama_config import resolve_model_ubatch  # noqa: E402
from backend.llama_vram_table import vram_of_moe  # noqa: E402

with mock.patch("backend.llama_preflight.gguf_size_gb", return_value=10.86):
    _lm_17 = ML._resolve_load_mode(
        "inkling-2-mini-ud", spec_planned=False, mlock_now=True,
        profile_lm="none", free_ram_gb=17.0,
        large_moe=False, moe_cpu_offload=True,
    )
    _lm_14 = ML._resolve_load_mode(
        "inkling-2-mini-ud", spec_planned=False, mlock_now=True,
        profile_lm="none", free_ram_gb=14.0,
        large_moe=False, moe_cpu_offload=True,
    )
    _lm_unk = ML._resolve_load_mode(
        "inkling-2-mini-ud", spec_planned=False, mlock_now=True,
        profile_lm="none", free_ram_gb=-1.0,
        large_moe=False, moe_cpu_offload=True,
    )
check("mid-sized MoE profile load_mode='none' allowed at 17 GB free, blocked at 14 GB / -1 GB",
      _lm_17 == "none" and _lm_14 == "mmap" and _lm_unk == "mmap",
      repr((_lm_17, _lm_14, _lm_unk)))

with mock.patch("backend.llama_preflight.gguf_size_gb", return_value=18.0):
    _lm_big_25 = ML._resolve_load_mode(
        "hermes3.6:35b-a3b", spec_planned=False, mlock_now=True,
        profile_lm="none", free_ram_gb=25.0,
        large_moe=True, moe_cpu_offload=True,
    )
    _lm_big_26 = ML._resolve_load_mode(
        "hermes3.6:35b-a3b", spec_planned=False, mlock_now=True,
        profile_lm="none", free_ram_gb=26.5,
        large_moe=True, moe_cpu_offload=True,
    )
check("large MoE still enforces full 8 GB headroom for profile load_mode='none'",
      _lm_big_25 == "mmap" and _lm_big_26 == "none",
      repr((_lm_big_25, _lm_big_26)))

_ub_ink = resolve_model_ubatch("inkling-2-mini-ud", 256, 0)
check("resolve_model_ubatch('inkling-2-mini-ud', 256, 0) -> (512, 'profile')",
      _ub_ink == (512, "profile"), repr(_ub_ink))

_vram_65k_15 = vram_of_moe("inkling-2-mini-ud", 65000)
with mock.patch("backend.gguf_meta.read_gguf_metadata",
                return_value={"file_size_gb": 10.86, "block_count": 24}):
    _vram_65k_16 = vram_of_moe("inkling-2-mini-ud", 65000, n_cpu_moe=16)
check("vram_of_moe('inkling-2-mini-ud', 65000) == 5.96 and steps < 5.75 at ncmoe=16",
      _vram_65k_15 == 5.96 and _vram_65k_16 < 5.75,
      repr((_vram_65k_15, _vram_65k_16)))

_q36_mtp = json.loads((_ROOT / "model_configs" / "models" / "qwen3.6_35b-a3b-mtp.json").read_text(encoding="utf-8"))
_q36_ud = json.loads((_ROOT / "model_configs" / "models" / "qwen3.6_35b-a3b-ud.json").read_text(encoding="utf-8"))
check("qwen3.6_35b-a3b-mtp enables mtp=True, ncmoe=40, cache=2560 (0.95 share), ub=1024",
      _q36_mtp.get("mtp") is True and _q36_mtp.get("moe_cpu_experts") == 40
      and _q36_mtp.get("moe_cache_mib") == 2560
      and _q36_mtp.get("moe_cache_cpu_share") == 0.95
      and _q36_mtp.get("ubatch") == 1024 and "_notes" in _q36_mtp)
check("qwen3.6_35b-a3b-ud aligns mmap, kv=6272 and preserves ubatch=512, mtp=False",
      _q36_ud.get("mtp") is False and _q36_ud.get("ubatch") == 512
      and _q36_ud.get("load_mode") == "mmap" and _q36_ud.get("kv_bytes_per_token") == 6272)

sys.exit(h.finish("b12631_inkling_loop_and_qwen36"))
