# -*- coding: utf-8 -*-
"""Profile fields must reach the llama-server launch flags.

Two fixes, one suite (2026-10-27):

1. kv_cache_type applies to DENSE models too. Only the MoE path consulted
   the model profile (moe_kv_cache_types); dense read the static family
   table and fell to the global q4_0 - a spark-x2.6:4b profile with q8_0
   silently ran q4_0. Fix: dense_kv_cache_type (llama_config) with the
   profile first; manager_load's dense branch calls it.
2. Profile num_ctx reaches /vram/load. core.state._get_num_ctx read ONLY
   ctx_overrides and returned None otherwise, so the load route started
   new/local models at the 4096 llama-server default (live: cybertiel
   profile 65536) while the chat path resolved the profile fine.
   Fix: _get_num_ctx_with_source (settings > profile > table) with the
   source reported; the load route logs [VRAM-LOAD] ... ctx_source=...

Behavioral checks via monkeypatched resolvers (test_audit_b pattern) -
no model load, no GPU."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── 1. dense kv_cache_type ───────────────────────────────────────────────
import backend.llama_config as LC
import model_configs.models_registry as mr

_gp_orig = mr.get_profile


def _gp_kv(m):
    profiles = {
        "spark-fixture:4b": {"kv_cache_type": "q8_0"},
        # explicit profile beats the curated family table
        "minicpm5:2b-sharp": {"kv_cache_type": "q4_0"},
        # -ud REGRESSION (2026-10-28): a substring strip turned
        # "study-ud-model:7b" into "study-model:7b" and pulled THIS foreign
        # profile. It must NOT match.
        "study-model:7b": {"kv_cache_type": "q8_0"},
    }
    return profiles.get(m, {})


mr.get_profile = _gp_kv
try:
    check("1a dense profile q8_0 honored",
          LC.dense_kv_cache_type("spark-fixture:4b") == "q8_0")
    check("1b explicit profile beats the family table",
          LC.dense_kv_cache_type("minicpm5:2b-sharp") == "q4_0")
    check("1c -ud-stripped profile key",
          LC.dense_kv_cache_type("spark-fixture:4b-ud") == "q8_0")
finally:
    mr.get_profile = _gp_orig

# table + global layers (profile absent everywhere)
check("1d no profile -> family table (minicpm5 q8_0)",
      LC.dense_kv_cache_type("minicpm5:2b-sharp", global_default="q4_0") == "q8_0")
check("1e no profile -> full-key table entry (k2-horizon)",
      LC.dense_kv_cache_type("k2-horizon:3.7b", global_default="q4_0") == "q8_0")
check("1f no profile, unknown model -> global default",
      LC.dense_kv_cache_type("unknown-dense:7b", global_default="q4_0") == "q4_0")
check("1g f16/empty global -> no flags (empty value)",
      LC.dense_kv_cache_type("unknown-dense:7b", global_default="") == "")

_ml = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8", errors="replace")
check("1h dense branch calls the profile-aware helper",
      "_dense_kv = dense_kv_cache_type(" in _ml)
check("1i old static-only lookup gone",
      "_DENSE_KV_CACHE_TYPES.get(" not in _ml)

# ── 2. profile num_ctx chain with source ─────────────────────────────────
import core.state as cs
import hive_functions.num_ctx_config as ncc

_ov_orig = cs.settings.get("ctx_overrides")
_reg_orig = mr.get_num_ctx
_ncc_orig = ncc.get_num_ctx


def _reg_num_ctx(m, r=None):
    if m == "fixture-prof:7b":
        return {"duo_coder": 22222, "analyst": 11111}.get(r, 32768)
    return None


def _table_num_ctx(m, r=None):
    if m == "fixture-table:7b":
        return 8192
    return None


try:
    # 2a settings override wins (nested models shape) + source
    cs.settings["ctx_overrides"] = {"models": {"fixture-ovr:7b": 12345}}
    check("2a settings override wins with source",
          cs._get_num_ctx_with_source("fixture-ovr:7b") == (12345, "settings"))

    # 2a2 legacy flat shape still works
    cs.settings["ctx_overrides"] = {"fixture-ovr:7b": 999}
    check("2a2 legacy flat override shape",
          cs._get_num_ctx("fixture-ovr:7b") == 999)

    # 2b profile num_ctx (no override) + role variant
    cs.settings["ctx_overrides"] = {}
    mr.get_num_ctx = _reg_num_ctx
    check("2b profile num_ctx resolves with source",
          cs._get_num_ctx_with_source("fixture-prof:7b") == (32768, "profile"))
    check("2b2 profile role variant honored",
          cs._get_num_ctx_with_source("fixture-prof:7b", "duo_coder") == (22222, "profile"))

    # 2c table fallback when the profile is silent
    ncc.get_num_ctx = _table_num_ctx
    check("2c table fallback with source",
          cs._get_num_ctx_with_source("fixture-table:7b") == (8192, "table"))

    # 2d nothing matches -> (None, None), wrapper returns None
    check("2d unknown model -> (None, None), caller keeps its default",
          cs._get_num_ctx_with_source("unknown:7b") == (None, None)
          and cs._get_num_ctx("unknown:7b") is None)
finally:
    mr.get_num_ctx = _reg_orig
    ncc.get_num_ctx = _ncc_orig
    if _ov_orig is None:
        cs.settings.pop("ctx_overrides", None)
    else:
        cs.settings["ctx_overrides"] = _ov_orig

# structural: the load route uses the source resolver and logs it
_vr = (ROOT / "routers" / "vram.py").read_text(encoding="utf-8", errors="replace")
check("2e load route uses the source resolver",
      "_get_num_ctx_with_source(model)" in _vr)
check("2f load route logs the ctx source",
      '"[VRAM-LOAD] %s ctx=%d ctx_source=%s"' in _vr)
_st = (ROOT / "core" / "state.py").read_text(encoding="utf-8", errors="replace")
check("2g core.state resolver falls through to profile/table",
      "from model_configs.models_registry import get_num_ctx" in _st
      and "from hive_functions.num_ctx_config import get_num_ctx" in _st)

print(f"\nprofile_launch_flags: {passed} passed, {failed} failed")

# SECTION 3: -ud suffix handling (2026-10-28) - one trailing -ud strip,
# applied to the profile AND the family table lookup.
check("3a -ud name reaches the family table entry",
      LC.dense_kv_cache_type("k2-horizon:3.7b-ud", "q4_0") == "q8_0",
      f"({LC.dense_kv_cache_type('k2-horizon:3.7b-ud', 'q4_0')})")
check("3b -ud substring no longer pulls a foreign profile",
      LC.dense_kv_cache_type("study-ud-model:7b", "q4_0") == "q4_0",
      f"({LC.dense_kv_cache_type('study-ud-model:7b', 'q4_0')})")
_moe_orig = LC._MOE_KV_CACHE_TYPES.copy()
LC._MOE_KV_CACHE_TYPES.clear()
LC._MOE_KV_CACHE_TYPES.update({"k2-horizon:3.7b": "q5_k_m"})
try:
    check("3c moe -ud name reaches its table entry",
          LC.moe_kv_cache_types("k2-horizon:3.7b-ud") == "q5_k_m",
          f"({LC.moe_kv_cache_types('k2-horizon:3.7b-ud')})")
finally:
    LC._MOE_KV_CACHE_TYPES.clear()
    LC._MOE_KV_CACHE_TYPES.update(_moe_orig)

sys.exit(1 if failed else 0)
