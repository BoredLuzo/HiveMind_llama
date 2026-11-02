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
  llama_mlock       -> llama_config.MLOCK_MODEL
  vram_budget_gb    -> llama_server_manager.VRAM_BUDGET_GB

It deliberately does NOT import server.py: doing that from a request or a
preset apply runs the whole server module (model index validation included)
and changes behaviour under tests.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("hivemind.runtime_sync")


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

    # mlock intent (the resolver turns it into mmap+mlock / mmap / none)
    try:
        _mlock = bool(settings.get("llama_mlock", True))
        _lc.MLOCK_MODEL = _mlock
        _lsm.MLOCK_MODEL = _mlock
    except (TypeError, ValueError):
        pass

    # VRAM budget for the pre-flight gate
    try:
        _raw = settings.get("vram_budget_gb")
        if _raw is not None:
            _lsm.VRAM_BUDGET_GB = float(_raw)
    except (TypeError, ValueError):
        logger.warning("runtime sync: vram_budget_gb ignored (bad value)")
