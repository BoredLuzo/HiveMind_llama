# -*- coding: utf-8 -*-
"""MoE expert cache (--moe-cache-mib): opt-in, budgeted, retried without tag.

B3 (2026-11-09): the threads pin ("_logical // 2") and the capability-check
pin were converted to real _phase_moe probes (fake manager, patched
cpu_count/help probe - no binary, no GPU). The remaining checks pin wiring
(UI markup, loader CMD string, pre-flight extra) that has no cheap probe.
"""
import asyncio
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from settings import DEFAULT_SETTINGS              # noqa: E402

ML = ((ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
      + (ROOT / "backend" / "llama_start_phases.py").read_text(encoding="utf-8"))
ME = (ROOT / "backend" / "manager_evict.py").read_text(encoding="utf-8")
HTML = (ROOT / "index.html").read_text(encoding="utf-8")

import backend.llama_config as _LC          # noqa: E402
import backend.llama_start_phases as _P     # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness(style="results", pad=True)
check = h.check


check("1 moe_cache_mib defaults to off", DEFAULT_SETTINGS.get("moe_cache_mib") == 0)
check("2 the loader passes --moe-cache-mib", '"--moe-cache-mib"' in ML)
check("4 the cache is part of the VRAM pre-flight",
      "extra_needed_mib=_moe_cache_used" in ML
      and "extra_needed_mib" in ME)
check("5 a failed load retries without the cache", "moe_cache=False" in ML)
check("7 the model tab has the input", 'id="moe-cache-mib"' in HTML)
check("8 the UI explains the trade-off", "moe-cache-mib" in HTML
      and "#30697" in HTML)

# ── B3: the pins above as behavior probes (_phase_moe, fake manager) ──────
class _MoeMgr:
    _moe_flag_supported = True
    _load_mode_supported = True
    _kv_flag_supported = True


class _MoeSlot:
    _moe_cache_used = False


def _run_moe(moe_cache, cmd=None, cpu_count=8, help_ok=True):
    slot = _MoeSlot()
    with mock.patch.object(_P, "_strip_alias", return_value="m"), \
         mock.patch.object(_P, "_MOE_EXPERT_COUNTS", {"m": 8}), \
         mock.patch.object(_LC, "MOE_CPU_EXPERTS", {}), \
         mock.patch.object(_LC, "_try_registry_moe", return_value=0), \
         mock.patch.object(_LC, "MOE_CACHE_MIB", 512), \
         mock.patch.object(_P, "GPU_BACKEND", "vulkan"), \
         mock.patch.object(_P, "moe_kv_cache_types", return_value="q4_0"), \
         mock.patch("backend.llama_vram_table.moe_cfg_for", return_value={}), \
         mock.patch("backend.manager_load._help_supports",
                    new=mock.AsyncMock(return_value=help_ok)), \
         mock.patch("os.cpu_count", return_value=cpu_count):
        phase = asyncio.run(_P._phase_moe(_MoeMgr(), slot, list(cmd or []),
                                          "m:1b", 4096, moe_cache, 16, 8))
    return phase, slot


def _flag_value(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


# (was pin 3 "the flag is capability-checked")
ph_nohelp, slot_nohelp = _run_moe(True, help_ok=False)
check("3 binary without --moe-cache-mib: cache skipped, slot flag stays off",
      "--moe-cache-mib" not in ph_nohelp.cmd
      and ph_nohelp.moe_cache_used == 0
      and slot_nohelp._moe_cache_used is False
      and "--n-cpu-moe" in ph_nohelp.cmd,
      f" ({ph_nohelp.cmd}/{ph_nohelp.moe_cache_used})")

# (was pin 6 "MoE threads use half of the logical CPUs")
ph_thr, _slot_thr = _run_moe(False, ["--threads", "16", "--threads-batch", "8"],
                             cpu_count=8)
ph_hi, _slot_hi = _run_moe(False, ["--threads", "16", "--threads-batch", "8"],
                           cpu_count=32)
check("6 8 logical CPUs -> decode threads halved to 4, batch keeps 16",
      _flag_value(ph_thr.cmd, "--threads") == "4"
      and _flag_value(ph_thr.cmd, "--threads-batch") == "16",
      f" ({ph_thr.cmd})")
check("6b half >= default threads (32 CPUs) leaves the cmd untouched",
      _flag_value(ph_hi.cmd, "--threads") == "16"
      and _flag_value(ph_hi.cmd, "--threads-batch") == "8",
      f" ({ph_hi.cmd})")

sys.exit(h.finish())
