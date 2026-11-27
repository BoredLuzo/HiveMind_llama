# -*- coding: utf-8 -*-
"""Regression suite for Gemma-4 external MTP sidecar, per-profile flash_attn,
Nemotron DFlash Q8_0 alternate resolution, Cyber-Tiel MoE profile, and
condensed installer hardware tiers.

All cases are hermetic (temp dirs + mocks; no GPU / network / downloads).
Run: python tests/test_catalog_tiers_and_wiring.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check

import backend.llama_config as lc  # noqa: E402
import backend.llama_start_phases as lsp  # noqa: E402
import backend.manager_load as ml  # noqa: E402
from backend.llama_vram_table import vram_of_with_ctx  # noqa: E402
from model_configs import models_registry as mr  # noqa: E402
sys.path.insert(0, str(ROOT / "deploy"))
import fetch_models as fm  # noqa: E402


async def _run_async_checks() -> None:
    with tempfile.TemporaryDirectory(prefix="hm_cat_wir_") as td:
        mdir = Path(td)
        g4 = mdir / "gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf"
        g4_draft = mdir / "mtp-gemma-4-E4B-it.gguf"
        g4.write_bytes(b"GGUF")
        g4_draft.write_bytes(b"GGUF")

        class _Mgr(ml.LlamaLoadMixin):
            _binary_build_number = 12001
            _device_flag_supported = True
            _kv_flag_supported = True
            _moe_flag_supported = True
            _load_mode_supported = True
            _dspark_flag_supported = True
            _backend_dlls_ok = True
            _backend_devices_ok = True
            _reasoning_override = None

            def __init__(self) -> None:
                self._slots = []

        mgr = _Mgr()

        async def _help_ok(flag: str) -> bool:
            return flag in ("--spec-draft-p-min", "--spec-draft-ngl", "draft-dflash")

        # 1) Gemma-4 external MTP sidecar present -> --model-draft + draft-mtp + ngl=999
        with (
            mock.patch.object(lsp, "MODELS_DIR", mdir),
            mock.patch.object(ml, "_help_supports", side_effect=_help_ok),
        ):
            res_on = await lsp._phase_model_flags(
                mgr, ["llama-server"], "gemma-4:e4b-it-qat", "gemma-4", g4, False
            )
            cmd_on = res_on.cmd
            check("Gemma-4 external MTP appends --spec-type draft-mtp + --model-draft + ngl=999",
                  "--spec-type" in cmd_on and "draft-mtp" in cmd_on
                  and "--model-draft" in cmd_on and str(g4_draft) in cmd_on
                  and cmd_on[cmd_on.index("--spec-draft-n-max") + 1] == "2"
                  and (lsp.GPU_BACKEND == "cpu" or ("--spec-draft-ngl" in cmd_on and cmd_on[cmd_on.index("--spec-draft-ngl") + 1] == "999")),
                  f"({cmd_on})")
            check("_speculation_planned returns True when Gemma-4 MTP sidecar exists",
                  mgr._speculation_planned("gemma-4:e4b-it-qat") is True)

        # 2) Gemma-4 MTP sidecar missing -> gracefully omitted (never crashes)
        g4_draft.unlink()
        with (
            mock.patch.object(lsp, "MODELS_DIR", mdir),
            mock.patch.object(ml, "_help_supports", side_effect=_help_ok),
        ):
            res_off = await lsp._phase_model_flags(
                mgr, ["llama-server"], "gemma-4:e4b-it-qat", "gemma-4", g4, False
            )
            check("Gemma-4 omits --spec-type draft-mtp cleanly when sidecar is absent",
                  "--spec-type" not in res_off.cmd and "--model-draft" not in res_off.cmd,
                  f"({res_off.cmd})")
            check("_speculation_planned returns False when external MTP sidecar is absent",
                  mgr._speculation_planned("gemma-4:e4b-it-qat") is False)

        # 3) Nemotron DFlash resolves alternate Unsloth Q8_0 drafter when BF16 absent
        n_gguf = mdir / "NVIDIA-Nemotron-3.5-Bolt-E4B-UD-Q4_K_XL.gguf"
        n_alt = mdir / "dflash-NVIDIA-Nemotron-3.5-Bolt-E4B-Q8_0.gguf"
        n_gguf.write_bytes(b"GGUF")
        n_alt.write_bytes(b"GGUF")
        with (
            mock.patch.object(lsp, "MODELS_DIR", mdir),
            mock.patch.object(ml, "_help_supports", side_effect=_help_ok),
        ):
            res_nem = await lsp._phase_model_flags(
                mgr, ["llama-server"], "nvidia-nemotron-3.5-bolt:e4b", "nvidia-nemotron-3.5-bolt", n_gguf, False
            )
            check("Nemotron DFlash falls back to Unsloth Q8_0 drafter when BF16 absent",
                  "--spec-type" in res_nem.cmd and "draft-dflash" in res_nem.cmd
                  and str(n_alt) in res_nem.cmd,
                  f"({res_nem.cmd})")
            check("Nemotron DFlash exposes calibrated draft_overhead_mib=950 without vram_gb_override",
                  mr.get_vram_gb_override("nvidia-nemotron-3.5-bolt:e4b") is None
                  and mr.get_draft_overhead_mib("nvidia-nemotron-3.5-bolt:e4b") == 950.0)

        # 4) Per-profile flash_attn=False emits --flash-attn off AND forces f16 KV
        async def _capture_cli(model_name: str, num_ctx: int = 8192) -> list[str]:
            class _Stop(RuntimeError):
                def __init__(self, c: list[str]):
                    self.built = list(c)

            slot = SimpleNamespace(
                slot_id=0, port=8299, model=model_name, is_running=False,
                pinned=False, _num_ctx=0, _loading=False, _moe_cache_used=False,
                _orphan_port=None, last_used=0.0, _ready_event=asyncio.Event(),
            )
            with (
                mock.patch.object(lsp, "resolve_model_path", return_value=str(g4)),
                mock.patch.object(lc, "LLAMA_FIT", False),
                mock.patch.object(ml, "_help_supports", new=mock.AsyncMock(return_value=True)),
                mock.patch.object(ml, "_probe_all", return_value=(12001, True, True, True, True, False)),
                mock.patch.object(lsp, "_probe_backend_dlls", return_value=True),
                mock.patch.object(lsp, "_probe_backend_devices", return_value=True),
                mock.patch.object(ml, "_phase_model_flags", side_effect=lambda *_a, **_k: _raise_stop(_a[1])),
            ):
                def _raise_stop(c):
                    raise _Stop(c)
                try:
                    await mgr._start_process(slot, model_name, num_ctx)
                except _Stop as caught:
                    return caught.built
            raise AssertionError("did not reach _phase_model_flags")

        cmd_ling = await _capture_cli("ling-3.0-tiny")
        cmd_mellum = await _capture_cli("mellum2.1:12b-a2.5b-thinking")
        cmd_twil = await _capture_cli("twil:8b")
        check("ling-3.0-tiny emits --flash-attn off and forces --cache-type-k f16",
              cmd_ling[cmd_ling.index("--flash-attn") + 1] == "off"
              and cmd_ling[cmd_ling.index("--cache-type-k") + 1] == "f16",
              f"({cmd_ling})")
        check("mellum2.1 emits --flash-attn off, --cache-type-k f16, --ubatch-size 1024",
              cmd_mellum[cmd_mellum.index("--flash-attn") + 1] == "off"
              and cmd_mellum[cmd_mellum.index("--cache-type-k") + 1] == "f16"
              and cmd_mellum[cmd_mellum.index("--ubatch-size") + 1] == "1024",
              f"({cmd_mellum})")
        check("twil:8b keeps --flash-attn on and q4_0 KV",
              cmd_twil[cmd_twil.index("--flash-attn") + 1] == "on"
              and cmd_twil[cmd_twil.index("--cache-type-k") + 1] == "q4_0",
              f"({cmd_twil})")


asyncio.run(_run_async_checks())

# 5) Cyber-Tiel-Coder MoE profile + hardware tiers
check("cyber-tiel-coder:35b-a3b-mtp-ud resolves ncmoe=35, mtp=True, ubatch=1024, vram < 5.0 GB",
      mr.get_moe_cpu_experts("cyber-tiel-coder:35b-a3b-mtp-ud") == 35
      and mr.is_mtp("cyber-tiel-coder:35b-a3b-mtp-ud") is True
      and mr.get_ubatch("cyber-tiel-coder:35b-a3b-mtp-ud") == 1024
      and vram_of_with_ctx("cyber-tiel-coder:35b-a3b-mtp-ud", 16384) < 5.0)
check("gemma-4:e4b-it-qat SWA f16 KV stays < 6.5 GB even @64k ctx",
      vram_of_with_ctx("gemma-4:e4b-it-qat", 16384) < 5.0
      and vram_of_with_ctx("gemma-4:e4b-it-qat", 65536) < 6.5,
      f"(@16k={vram_of_with_ctx('gemma-4:e4b-it-qat', 16384)}, @64k={vram_of_with_ctx('gemma-4:e4b-it-qat', 65536)})")

check("Hardware tier selection matches VRAM x RAM boundaries cleanly",
      fm.detect_hardware_tier(4.0, 16.0)["id"] == "low_vram"
      and fm.detect_hardware_tier(8.0, 16.0)["id"] == "8gb_16gb"
      and fm.detect_hardware_tier(8.0, 32.0)["id"] == "8gb_32gb"
      and fm.detect_hardware_tier(12.0, 32.0)["id"] == "12gb_16gb"
      and fm.detect_hardware_tier(24.0, 64.0)["id"] == "24gb_plus")

plan_str = fm.format_hardware_plan(vram_gb=8.0, ram_gb=32.0, gpu_name="Test GPU")
check("format_hardware_plan includes detected tier and Nemotron/Qwen3.6 upgrades",
      "nvidia-nemotron-3.5-bolt:e4b" in plan_str and "qwen3.6:35b-a3b-mtp" in plan_str
      and "32+ GB System RAM" in plan_str and "visual guidance only" in plan_str,
      f"({plan_str})")

_specs_by_key = {sp["key"]: sp for sp in fm.SPECS}
check("model_fit_badge labels MoE RAM-offload on 8GB+32GB and FITS VRAM on 8GB dense",
      "32GB RAM OFFLOAD" in fm.model_fit_badge(_specs_by_key["qwen3.6:35b-a3b-mtp"], 8.0, 32.0)
      and "FITS VRAM" in fm.model_fit_badge(_specs_by_key["nvidia-nemotron-3.5-bolt:e4b"], 8.0, 16.0)
      and "RECOMMENDED DEFAULT" in fm.model_fit_badge(_specs_by_key["qwen3.5:4b-mtp"], 8.0, 16.0))

import recommend_models as rm  # noqa: E402
_sample_rows = [
    {"tag": "qwen3.5:0.8b-ud", "gb": 0.5, "moe_cpu_experts": 0, "measured_tps": 0.0, "score": 3},
    {"tag": "lfm3:1.7b-qad", "gb": 1.0, "moe_cpu_experts": 0, "measured_tps": 0.0, "score": 3},
    {"tag": "minicpm5:2b-sharp", "gb": 2.0, "moe_cpu_experts": 0, "measured_tps": 0.0, "score": 3},
    {"tag": "qwen3.5:4b-mtp", "gb": 2.6, "moe_cpu_experts": 0, "measured_tps": 0.0, "score": 3},
    {"tag": "gemma-4:e4b-it-qat", "gb": 3.9, "moe_cpu_experts": 0, "measured_tps": 0.0, "score": 3},
    {"tag": "qwen3.6:35b-a3b-mtp", "gb": 17.0, "moe_cpu_experts": 35, "measured_tps": 0.0, "score": 2},
]
_roles_8gb32 = rm._pick_roles_from_rows(
    _sample_rows, 8.0, 7.0, 32.0, fm.detect_hardware_tier(8.0, 32.0)
)
check("recommend_models keeps core GPU defaults and surfaces MoE RAM offload as optional upgrade",
      _roles_8gb32["direct"] == "gemma-4:e4b-it-qat"
      and _roles_8gb32["judge"] == "lfm3:1.7b-qad"
      and _roles_8gb32["duo_coder"] == "qwen3.5:4b-mtp"
      and _roles_8gb32["planner"] == "qwen3.5:4b-mtp"
      and _roles_8gb32["duo_coder_heavy_moe"] == "qwen3.6:35b-a3b-mtp",
      f"({_roles_8gb32})")

sys.exit(h.finish("catalog_tiers_and_wiring"))
