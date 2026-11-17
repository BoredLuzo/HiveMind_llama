# -*- coding: utf-8 -*-
"""Regression suite for MoE expert-count VRAM scaling, adaptive CPU offload,
and Pre-Flight MiB unit accuracy (2026-11-16 live finding: air:26b-a4b blocked
at ~6.1 GB free VRAM despite 10 MoE blocks still on GPU).
"""
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import backend.gguf_meta as GM  # noqa: E402
import backend.llama_config as LC  # noqa: E402
import backend.llama_start_phases as LSP  # noqa: E402
import backend.llama_vram_table as LVT  # noqa: E402
from backend.llama_manager_utils import VRAM_PRE_FLIGHT_MARGIN_MIB  # noqa: E402
from backend.llama_slots import ModelSlot  # noqa: E402
from backend.manager_evict import LlamaEvictMixin  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

_AIR_META = {"block_count": 30, "file_size_gb": 12.545}


class _Mgr(LlamaEvictMixin):
    def __init__(self, slot: ModelSlot):
        self._slots = [slot]

    def _metric_inc(self, key, amount=1):
        pass


# ── 1. Override-aware MoE VRAM calculation ───────────────────────────────────
with mock.patch.object(GM, "read_gguf_metadata", return_value=_AIR_META), \
     mock.patch.object(LC, "MOE_CPU_EXPERTS", {}):
    v_def_4k = LVT.vram_of_moe("air:26b-a4b", 4096)
    v_def_50k = LVT.vram_of_moe("air:26b-a4b", 50000)
    v_cpu30_4k = LVT.vram_of_moe("air:26b-a4b", 4096, n_cpu_moe=30)
    v_cpu30_50k = LVT.vram_of_moe("air:26b-a4b", 50000, n_cpu_moe=30)
    v_cpu10_4k = LVT.vram_of_moe("air:26b-a4b", 4096, n_cpu_moe=10)
    check("1a default air@4096 and @50000 preserved",
          v_def_4k == 4.26 and v_def_50k == 4.56,
          f" (4k={v_def_4k}, 50k={v_def_50k})")
    check("1b raising n_cpu_moe to 30 reduces GPU VRAM below 4 GB safely",
          2.6 <= v_cpu30_4k < 3.8 and 3.0 <= v_cpu30_50k < 4.3,
          f" (30@4k={v_cpu30_4k}, 30@50k={v_cpu30_50k})")
    check("1c lowering n_cpu_moe to 10 increases GPU VRAM above default",
          v_cpu10_4k > 8.0, f" (10@4k={v_cpu10_4k})")

with mock.patch.object(GM, "read_gguf_metadata", return_value=_AIR_META), \
     mock.patch.object(LC, "MOE_CPU_EXPERTS", {"air:26b-a4b": 30}):
    v_ovr = LVT.vram_of("air:26b-a4b")
    v_ovr_50k = LVT.vram_of_moe("air:26b-a4b", 50000)
    check("1d explicit MOE_CPU_EXPERTS override reflects in vram_of and vram_of_moe",
          v_ovr == v_cpu30_4k and v_ovr_50k == v_cpu30_50k,
          f" (vram_of={v_ovr}, 50k={v_ovr_50k})")

with mock.patch.object(GM, "read_gguf_metadata", return_value={}), \
     mock.patch.object(LC, "MOE_CPU_EXPERTS", {"air:26b-a4b": 30}):
    check("1e missing GGUF metadata stays conservative at calibrated base",
          LVT.vram_of_moe("air:26b-a4b", 4096) == 4.26)


# ── 2. Auto MoE CPU-expert step-up on tight live VRAM (~5000 MiB free) ───────
def _run_stepup(free_mib: float, *, ub_eff: int = 512, ub_base: int = 256):
    slot = ModelSlot(0)
    mgr = _Mgr(slot)
    ub_delta = 72.0 if ub_eff > ub_base else 0.0
    cmd = [
        "llama-server", "--ctx-size", "50000", "--batch-size", "1024",
        "--ubatch-size", str(ub_eff), "--n-cpu-moe", "26",
    ]
    with mock.patch.object(GM, "read_gguf_metadata", return_value=_AIR_META), \
         mock.patch.object(LC, "MOE_CPU_EXPERTS", {}), \
         mock.patch.object(LC, "LLAMA_UBATCH", ub_eff), \
         mock.patch("backend.manager_evict.get_live_gpu_free_mib", return_value=free_mib):
        init_fit = mgr.can_fit("air:26b-a4b", 50000, exclude_slot_id=slot.slot_id, extra_needed_mib=ub_delta)
        res_fit, res_extra, res_ub_delta, res_moe = LSP._auto_step_moe_cpu_offload(
            mgr, slot, cmd, "air:26b-a4b", "C:/fake/Air-26B-A4B.gguf",
            50000, init_fit, ub_delta, ub_base, ub_eff, "test", ub_delta,
        )
    return init_fit, res_fit, cmd, res_extra, res_ub_delta, res_moe


init_fit, res_fit, cmd_ok, _, _, res_moe = _run_stepup(5000.0)
check("2a air:26b-a4b @50k fails at 26 experts with 5000 MiB free",
      init_fit.ok is False
      and init_fit.needed_mib + VRAM_PRE_FLIGHT_MARGIN_MIB > 5000.0,
      f" (needed={init_fit.needed_mib})")
check("2b auto MoE CPU step-up fits @50k with full margin by bumping --n-cpu-moe",
      res_fit.ok is True
      and res_fit.margin_mib == VRAM_PRE_FLIGHT_MARGIN_MIB
      and 26 < res_moe <= 30
      and cmd_ok[cmd_ok.index("--n-cpu-moe") + 1] == str(res_moe)
      and cmd_ok[cmd_ok.index("--ubatch-size") + 1] == "512",
      f" (moe={res_moe}, needed={res_fit.needed_mib}, margin={res_fit.margin_mib})")

# Tighter VRAM (~4050 MiB): uses full 30/30 CPU offload + base ubatch / reduced margin
_, tight_fit, cmd_tight, _, tight_ub_delta, tight_moe = _run_stepup(4050.0)
check("2c tighter 4050 MiB free still fits full 50k context at <=30/30 experts",
      tight_fit.ok is True and 26 < tight_moe <= 30,
      f" (moe={tight_moe}, needed={tight_fit.needed_mib}, margin={tight_fit.margin_mib}, ubatch={cmd_tight[cmd_tight.index('--ubatch-size') + 1]})")

# Impossible VRAM (1500 MiB free): leaves cmd at 26, does not pretend fit
_, bad_fit, cmd_bad, _, _, _ = _run_stepup(1500.0)
check("2d impossibly low VRAM stays blocked and does not corrupt cmd",
      bad_fit.ok is False and cmd_bad[cmd_bad.index("--n-cpu-moe") + 1] == "26",
      f" (ok={bad_fit.ok}, cmd={cmd_bad})")

# Unit check: L1214 fixed_cost_dominant uses MiB (* 1024), not raw GB (e.g. 5.8 + 256)
_phases_src = (Path(__file__).resolve().parent.parent / "backend" / "llama_start_phases.py").read_text(encoding="utf-8")
check("3a preflight _current_own and _min_needed convert GB to MiB (* 1024)",
      "* 1024" in _phases_src.split("_current_own =", 1)[1][:220]
      and "(_base_4k_gb * 1024) + VRAM_PRE_FLIGHT_REDUCED_MARGIN_MIB" in _phases_src)

sys.exit(h.finish("moe_auto_cpu"))
