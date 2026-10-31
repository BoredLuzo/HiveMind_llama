"""Push settings values into the llama module globals (2026-10-31).

The llama-server startup flags live as by-value imports in llama_config,
llama_server_manager and manager_load. server.py and routers/config.py used to
sync them in four hand-rolled blocks; those drifted (ubatch ignored
duo_ubatch, three int() casts were unguarded, a preset load was 30 lines).
One helper, guarded casts, one log line.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("llama_flags")


def _as_int(settings: dict, key: str, default) -> int:
    raw = settings.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("[FLAGS] %s=%r is not an integer - keeping %r",
                       key, raw, default)
        return int(default)


def apply_runtime_flags(settings: dict) -> dict:
    """Apply cache/checkpoint/mlock/ubatch to all three flag holders."""
    from backend import llama_config as lc
    from backend import llama_server_manager as lsm
    from backend import manager_load as ml

    cache_reuse = max(0, _as_int(settings, "llama_cache_reuse", lc.CACHE_REUSE))
    cache_ram = max(0, _as_int(settings, "llama_cache_ram_mib", lc.CACHE_RAM_MIB))
    checkpoints = max(0, _as_int(settings, "llama_ctx_checkpoints",
                                 lc.CTX_CHECKPOINTS))
    mlock = bool(settings.get("llama_mlock", True))
    ubatch, batch = lc.resolve_ubatch_sizes(settings)

    applied = {
        "llama_cache_reuse": cache_reuse,
        "llama_cache_ram_mib": cache_ram,
        "llama_ctx_checkpoints": checkpoints,
        "llama_mlock": mlock,
        "ubatch": ubatch,
        "batch": batch,
    }
    # Set the five startup flags on all three holders unconditionally: the
    # old preset block created the attributes on llama_server_manager even
    # where its import list did not carry them (tests pin that contract).
    for mod in (lc, lsm, ml):
        mod.CACHE_REUSE = cache_reuse
        mod.CACHE_RAM_MIB = cache_ram
        mod.CTX_CHECKPOINTS = checkpoints
        mod.MLOCK_MODEL = mlock
        mod.LLAMA_UBATCH = ubatch
        if hasattr(mod, "LLAMA_BATCH"):
            mod.LLAMA_BATCH = batch
    logger.info(
        "[FLAGS] cache_reuse=%d cache_ram=%d checkpoints=%d mlock=%s "
        "ubatch=%d batch=%d", cache_reuse, cache_ram, checkpoints, mlock,
        ubatch, batch)
    return applied
