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
check("deploy/llama_build_pin.txt pins b12631", FL._read_pinned_tag() == "b12631", repr(FL._read_pinned_tag()))


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
    check("_phase_moe emits --moe-cache-cpu-share 0.5 alongside --moe-cache-mib",
          "--moe-cache-mib" in res.cmd and "--moe-cache-cpu-share" in res.cmd
          and res.cmd[res.cmd.index("--moe-cache-cpu-share") + 1] == "0.5",
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
import model_configs.models_registry as MR  # noqa: E402

_ink_prof = MR.get_profile("inkling-2-mini") or {}
check("inkling-2-mini profile shipped with moe_cpu_experts=14, ubatch=256, kv=9216",
      MR.get_profile_source("inkling-2-mini") == "shipped"
      and _ink_prof.get("moe_cpu_experts") == 14
      and _ink_prof.get("ubatch") == 256
      and _ink_prof.get("kv_bytes_per_token") == 9216
      and _ink_prof.get("mmproj_filename") == "mmproj-Inkling-2-Mini-F16.gguf",
      repr(_ink_prof))

_ac = json.loads((_ROOT / "model_configs" / "arch_compat.json").read_text(encoding="utf-8"))
check("arch_compat registers inkling-2-mini in moe_expert_counts, moe_table, and model_profiles",
      _ac["moe_expert_counts"].get("inkling-2-mini") == 14
      and _ac["moe_table"].get("inkling-2-mini", {}).get("calibrated_n_cpu_moe") == 14
      and _ac["model_profiles"].get("inkling-2-mini", {}).get("vision") is True)

_q36_mtp = json.loads((_ROOT / "model_configs" / "models" / "qwen3.6_35b-a3b-mtp.json").read_text(encoding="utf-8"))
_q36_ud = json.loads((_ROOT / "model_configs" / "models" / "qwen3.6_35b-a3b-ud.json").read_text(encoding="utf-8"))
check("qwen3.6_35b-a3b-mtp preserves mtp=True, ncmoe=35, ub=1024 and adds _notes",
      _q36_mtp.get("mtp") is True and _q36_mtp.get("moe_cpu_experts") == 35
      and _q36_mtp.get("ubatch") == 1024 and "_notes" in _q36_mtp)
check("qwen3.6_35b-a3b-ud aligns mmap, kv=6272 and preserves ubatch=512, mtp=False",
      _q36_ud.get("mtp") is False and _q36_ud.get("ubatch") == 512
      and _q36_ud.get("load_mode") == "mmap" and _q36_ud.get("kv_bytes_per_token") == 6272)

sys.exit(h.finish("b12631_inkling_loop_and_qwen36"))
