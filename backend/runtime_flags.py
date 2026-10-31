"""Push settings values into the llama module globals (2026-10-31).

The llama-server startup flags live as by-value imports in llama_config,
llama_server_manager and manager_load. The preset path synced them in a
30-line hand-rolled block that drifted (ubatch ignored duo_ubatch, three
int() casts were unguarded). One helper, guarded casts, one log line.
server.py keeps its own boot block (it runs before this module is imported).
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


def apply_runtime_flags(settings: dict) -> None:
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

    # Set the five startup flags on all three holders unconditionally: the
    # old preset block created the attributes on llama_server_manager even
    # where its import list did not carry them (tests pin that contract).
    for mod in (lc, lsm, ml):
        mod.CACHE_REUSE = cache_reuse
        mod.CACHE_RAM_MIB = cache_ram
        mod.CTX_CHECKPOINTS = checkpoints
        mod.MLOCK_MODEL = mlock
        mod.LLAMA_UBATCH = ubatch
        # LLAMA_BATCH is not a module global; --batch-size is derived from
        # LLAMA_UBATCH in the loader. `batch` stays in the return value.
    logger.info(
        "[FLAGS] cache_reuse=%d cache_ram=%d checkpoints=%d mlock=%s ubatch=%d",
        cache_reuse, cache_ram, checkpoints, mlock, ubatch)
