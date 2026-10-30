# -*- coding: utf-8 -*-
"""Test: MoE sizing from the LOCAL model profile (backend/llama_vram_table).

`_profile_moe_cfg` / `moe_cfg_for` size a model from its own registry
profile (moe_cpu_experts + vram_gb_override + kv_bytes_per_token) when the
static _MOE_TABLE is silent — the path that used to require a hand-written
table line per new MoE. Pins what the code DOES (get_profile is patched, no
disk registry, no models needed):

  1. A profile with moe_cpu_experts yields a config whose expert count and
     total VRAM differ from the table's calibrated default row.
  2. kv_bytes_per_token flows through as measured_bytes_per_token (ctx
     scaling in vram_of_moe uses the profile number, not the 12 KB default).
  3. A missing profile falls back without raising (cfg None, dense path
     answers vram_of_moe).
  4. A profile WITHOUT moe_cpu_experts behaves like the table: table rows
     keep precedence, non-table models get None (dense stays authoritative).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import model_configs.models_registry as _registry
from backend.llama_vram_table import (  # noqa: E402
    moe_cfg_for, _profile_moe_cfg, vram_of_moe, vram_of_with_ctx,
    _MOE_TABLE,
)

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


_ORIG_GET_PROFILE = _registry.get_profile


def _with_profile(prof):
    """Patch registry.get_profile to return `prof` for every name."""
    def _fake(name):
        return dict(prof)
    _registry.get_profile = _fake


def _restore():
    _registry.get_profile = _ORIG_GET_PROFILE


TABLE_ROW = _MOE_TABLE["qwen3.6:35b-a3b"]  # calibrated default: 35 experts, 4.66 GB
TEST_MOE = "testmoe:9b-a2b"  # no table row: exact, -ud-strip and prefix all miss

try:
    # ── 1. profile with moe_cpu_experts sizes from the profile ──────────
    _with_profile({"moe_cpu_experts": 10, "vram_gb_override": 6.4,
                   "kv_bytes_per_token": 10240})
    cfg = moe_cfg_for(TEST_MOE)
    check("1a profile cfg found for a table-less model", cfg is not None,
          f" ({cfg})")
    check("1b cfg marked source=profile with n_cpu_moe",
          cfg.get("source") == "profile" and cfg.get("n_cpu_moe") == 10)
    check("1c expert share differs from the table default (35)",
          cfg.get("n_cpu_moe") != TABLE_ROW.get("calibrated_n_cpu_moe"))
    check("1d total differs from the table default (4.66)",
          cfg.get("active_gpu_gb") == 6.4
          and cfg["active_gpu_gb"] != TABLE_ROW["active_gpu_gb"])
    check("1e vram_of_moe @4096 is the profile total",
          vram_of_moe(TEST_MOE, 4096) == 6.4
          and vram_of_moe(TEST_MOE, 4096) != vram_of_moe("qwen3.6:35b-a3b", 4096))

    # ── 2. kv_bytes_per_token drives the ctx scaling ─────────────────────
    _exp_profile = round(6.4 + (8192 * 10240) / (1024 ** 3), 2)
    _exp_table = round(4.66 + (8192 * TABLE_ROW["measured_bytes_per_token"]) / (1024 ** 3), 2)
    check("2a @8192 scales with the profile kv_bytes_per_token",
          vram_of_moe(TEST_MOE, 8192) == _exp_profile,
          f" ({vram_of_moe(TEST_MOE, 8192)} == {_exp_profile})")
    check("2b and stays distinct from the table row @8192",
          vram_of_moe(TEST_MOE, 8192) != vram_of_moe("qwen3.6:35b-a3b", 8192),
          f" (table: {_exp_table})")
    check("2c -ud variant still resolves via the profile",
          (moe_cfg_for(TEST_MOE + "-ud") or {}).get("source") == "profile")

    # ── 3. missing profile: clean fallback, no raise ─────────────────────
    _with_profile({})  # model unknown to the registry
    # no try/except: a raise here is the failure signal itself
    raised = None
    cfg_none = moe_cfg_for(TEST_MOE)
    dense = vram_of_moe(TEST_MOE, 4096)
    check("3a no raise on a missing profile", raised is None,
          f" ({raised!r})" if raised else "")
    check("3b moe_cfg_for -> None (dense path authoritative)",
          cfg_none is None and _profile_moe_cfg(TEST_MOE) is None)
    check("3c vram_of_moe falls back to the dense estimate",
          isinstance(dense, float) and dense > 0
          and dense == vram_of_with_ctx(TEST_MOE, 4096),
          f" ({dense})")

    # ── 4. profile without moe_cpu_experts behaves like the table ────────
    _with_profile({"vram_gb_override": 6.4})  # dense model with an override
    check("4a no moe_cpu_experts -> profile path declines",
          _profile_moe_cfg(TEST_MOE) is None)
    check("4b non-table model: cfg stays None",
          moe_cfg_for(TEST_MOE) is None)
    check("4c table row keeps precedence over the profile",
          moe_cfg_for("qwen3.6:35b-a3b") is _MOE_TABLE["qwen3.6:35b-a3b"]
          and moe_cfg_for("qwen3.6:35b-a3b")["active_gpu_gb"] == 4.66
          and "source" not in moe_cfg_for("qwen3.6:35b-a3b"))
    _with_profile({"moe_cpu_experts": 10, "vram_gb_override": 0})
    check("4d experts but zero override -> declines (no dense harm)",
          moe_cfg_for(TEST_MOE) is None)
finally:
    _restore()

print()
print(f"{'=' * 50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'=' * 50}")
sys.exit(0 if failed == 0 else 1)
