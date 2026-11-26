# -*- coding: utf-8 -*-
"""Loader settings that must apply without a restart (2026-11-02).

`server._sync_backend_runtime_config()` pushes settings into the backend module
globals - but it ran ONCE at startup. A changed `moe_cpu_experts` or
`llama_mlock` therefore did nothing until the next engine restart: the loader
kept reading the boot-time values (live symptom: the per-model "MoE CPU
Experts" field never reached `-n-cpu-moe`, every run used the profile value).

This module holds exactly the keys the LOAD path reads and must follow a
settings save:

  moe_cpu_experts   -> llama_config.MOE_CPU_EXPERTS (read live by the loader)
  moe_cache_mib     -> llama_config.MOE_CACHE_MIB (--moe-cache-mib)
  llama_mlock       -> llama_config.MLOCK_MODEL
  vram_budget_gb    -> llama_server_manager.VRAM_BUDGET_GB

It deliberately does NOT import server.py: doing that from a request or a
preset apply runs the whole server module (model index validation included)
and changes behaviour under tests.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger("hivemind.runtime_sync")


def _sync_loaded_backend_attr(mod_names: tuple[str, ...], attr: str, value) -> None:
    """Propagate an updated global onto any already-imported backend submodules."""
    for mod_name in mod_names:
        mod = sys.modules.get(f"backend.{mod_name}")
        if mod is not None and hasattr(mod, attr):
            setattr(mod, attr, value)


def sync_loader_settings(settings: dict) -> None:
    """Apply the load-time settings from `settings` to the backend globals."""
    if not isinstance(settings, dict):
        return
    try:
        import backend.llama_config as _lc
        import backend.llama_server_manager as _lsm
    except ImportError as exc:            # backend unavailable (bare tests)
        logger.debug("runtime sync skipped: %s", exc)
        return

    # MoE experts per model: the loader reads llama_config live (the manager
    # module only holds a by-value copy from boot).
    try:
        _moe_cfg = settings.get("moe_cpu_experts", {})
        _moe_map = ({str(k): int(v or 0) for k, v in _moe_cfg.items()}
                    if isinstance(_moe_cfg, dict) else {})
        _lc.MOE_CPU_EXPERTS = dict(_moe_map)
        _lsm.MOE_CPU_EXPERTS = _moe_map
    except (TypeError, ValueError):
        logger.warning("runtime sync: moe_cpu_experts ignored (bad value)")

    # Expert cache (MiB) for the MoE offload path
    try:
        _cache = int(settings.get("moe_cache_mib", 0) or 0)
        _lc.MOE_CACHE_MIB = _cache
        _lsm.MOE_CACHE_MIB = _cache
    except (TypeError, ValueError):
        logger.warning("runtime sync: moe_cache_mib ignored (bad value)")

    # --fit (layers/ctx sized to the free VRAM) & Vulkan WDDM VRAM pinning
    try:
        _lc.LLAMA_FIT = bool(settings.get("llama_fit", True))
        _lc.LLAMA_FIT_TARGET_MIB = int(settings.get("llama_fit_target_mib", 0) or 0)
        _lc.VK_DISABLE_HOST_VISIBLE_VIDMEM = bool(
            settings.get("llama_vk_disable_host_visible_vidmem", True)
        )
        _lsm.LLAMA_FIT = _lc.LLAMA_FIT
        _lsm.LLAMA_FIT_TARGET_MIB = _lc.LLAMA_FIT_TARGET_MIB
        _lsm.VK_DISABLE_HOST_VISIBLE_VIDMEM = _lc.VK_DISABLE_HOST_VISIBLE_VIDMEM
    except (TypeError, ValueError):
        logger.warning("runtime sync: llama_fit ignored (bad value)")

    # mlock intent (the resolver turns it into mmap+mlock / mmap / none)
    try:
        _mlock = bool(settings.get("llama_mlock", True))
        _lc.MLOCK_MODEL = _mlock
        _lsm.MLOCK_MODEL = _mlock
    except (TypeError, ValueError):
        pass

    # Optional global kv_cache_type override (canonical llama_kv_cache_type > legacy kv_cache_type)
    try:
        _kv_raw = str(
            settings.get("llama_kv_cache_type") or settings.get("kv_cache_type") or ""
        ).strip().lower()
        if _kv_raw in ("f16", "bf16", "q8_0", "q4_0", "q4_1", "q5_0", "q5_1"):
            _lc.KV_CACHE_TYPE = _kv_raw
            _sync_loaded_backend_attr(
                ("llama_server_manager", "manager_load", "llama_start_phases", "llama_vram_table"),
                "KV_CACHE_TYPE", _kv_raw,
            )
    except (TypeError, ValueError, AttributeError):
        logger.debug("runtime sync: kv_cache_type skipped")

    try:
        _raw = settings.get("vram_budget_gb")
        if _raw is not None:
            _budget = float(_raw)
            import backend.llama_manager_utils as _lmu
            _lmu.VRAM_BUDGET_GB = _budget
            _sync_loaded_backend_attr(
                ("llama_server_manager", "manager_load", "manager_evict",
                 "manager_health", "manager_prefetch", "manager_process",
                 "llama_start_phases"),
                "VRAM_BUDGET_GB", _budget,
            )
    except (TypeError, ValueError):
        logger.warning("runtime sync: vram_budget_gb ignored (bad value)")
