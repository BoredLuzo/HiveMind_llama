"""Push settings values into backend and core runtime module globals.

Unifies startup/preset/POST-settings flag synchronization around
`apply_runtime_flags(settings)` and `hasattr`-gated `_broadcast_attr(attr_name, value)`
so by-value imports across backend and core modules never drift.
"""
from __future__ import annotations

import importlib
import logging
import math
import sys

logger = logging.getLogger("llama_flags")

# The keys apply_runtime_flags reads. Callers that decide whether to call it
# (POST /settings, preset apply) import this instead of keeping a copy.
RUNTIME_FLAG_KEYS = frozenset({
    "llama_cache_reuse",
    "llama_cache_ram_mib",
    "llama_ctx_checkpoints",
    "llama_ubatch_size",
    "duo_ubatch",
    "llama_mlock",
    "vram_preflight_gate",
    "llama_kv_cache_type",
    "llama_max_slots",
    "llama_gpu_device",
    "llama_extra_args",
    "spec_decode_mode",
    "mtp_draft_n_max",
    "mtp_draft_p_min",
    "dflash_draft_p_min",
    "total_vram_mib_override",
    "vram_preflight_margin_mib",
    "duo_compress_mode",
    "default_unprofiled_sampling",
})

_TARGET_MODULES = (
    "backend.llama_config",
    "backend.llama_server_manager",
    "backend.manager_load",
    "backend.manager_evict",
    "backend.manager_health",
    "backend.manager_prefetch",
    "backend.manager_process",
    "backend.llama_start_phases",
    "backend.llama_manager_utils",
    "backend.llama_vram_table",
    "backend.llama_slots",
    "core.model_sampling",
    "core.duo_compress_cycle",
)

_KV_CACHE_CHOICES = frozenset({"q4_0", "q8_0", "f16", "bf16", "q4_1", "q5_0", "q5_1"})
_SPEC_MODE_CHOICES = frozenset({"auto", "off"})
_COMPRESS_MODE_CHOICES = frozenset({"auto", "mask_only", "partial", "full"})
_SAMPLING_KEYS = (
    "temperature", "top_p", "top_k", "min_p",
    "repeat_penalty", "repetition_penalty", "presence_penalty", "seed",
)


def _as_int(settings: dict, key: str, default: int) -> int:
    raw = settings.get(key, default)
    if isinstance(raw, bool):
        logger.warning("[FLAGS] %s=%r is a boolean, not an integer - keeping %r", key, raw, default)
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("[FLAGS] %s=%r is not an integer - keeping %r", key, raw, default)
        return int(default)


def _as_float(settings: dict, key: str, default: float) -> float:
    raw = settings.get(key, default)
    if isinstance(raw, bool):
        logger.warning("[FLAGS] %s=%r is a boolean, not a number - keeping %r", key, raw, default)
        return float(default)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning("[FLAGS] %s=%r is not a number - keeping %r", key, raw, default)
        return float(default)
    if not math.isfinite(val):
        logger.warning("[FLAGS] %s=%r is not finite - keeping %r", key, raw, default)
        return float(default)
    return val


def _as_choice(settings: dict, key: str, choices: frozenset[str], default: str) -> str:
    raw = settings.get(key, default)
    val = str(raw if raw is not None else default).strip().lower()
    if val in choices:
        return val
    logger.warning("[FLAGS] %s=%r invalid (expected %s) - keeping %r",
                   key, raw, "/".join(sorted(choices)), default)
    return default


def _as_extra_args(settings: dict, key: str, default: str) -> str:
    raw = settings.get(key, default)
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        joined = " ".join(str(x).strip() for x in raw if str(x).strip())
        return " ".join(joined.splitlines()).strip()
    if isinstance(raw, str):
        return " ".join(raw.splitlines()).strip()
    logger.warning("[FLAGS] %s=%r is not a string - keeping %r", key, raw, default)
    return str(default or "")


def _as_sampling_dict(settings: dict, key: str, default: dict) -> dict:
    raw = settings.get(key)
    if not isinstance(raw, dict):
        logger.warning("[FLAGS] %s=%r is not an object - keeping current sampling", key, raw)
        return dict(default)
    clean: dict[str, float | int] = {}
    for sk in _SAMPLING_KEYS:
        if sk not in raw:
            continue
        sv = raw[sk]
        if isinstance(sv, bool) or not isinstance(sv, (int, float)):
            continue
        if not math.isfinite(float(sv)):
            continue
        clean[sk] = int(sv) if sk in ("top_k", "seed") else float(sv)
    if not clean:
        logger.warning("[FLAGS] %s has no valid sampling fields - keeping current sampling", key)
        return dict(default)
    return clean


def _broadcast_attr(attr_name: str, value) -> int:
    """Set `attr_name` to `value` on every target backend/core module that defines it."""
    updated = 0
    for mod_name in _TARGET_MODULES:
        mod = sys.modules.get(mod_name)
        if mod is None:
            try:
                mod = importlib.import_module(mod_name)
            except ImportError:
                continue
        if hasattr(mod, attr_name):
            val = dict(value) if isinstance(value, dict) else value
            setattr(mod, attr_name, val)
            updated += 1
    return updated


def apply_runtime_flags(settings: dict) -> None:
    """Apply runtime settings into backend/core module globals."""
    if not isinstance(settings, dict):
        settings = {}

    from backend import llama_config as lc
    from backend import llama_server_manager as lsm
    from backend import manager_load as ml

    cache_reuse = max(0, _as_int(settings, "llama_cache_reuse", lc.CACHE_REUSE))
    cache_ram = max(0, _as_int(settings, "llama_cache_ram_mib", lc.CACHE_RAM_MIB))
    checkpoints = max(0, _as_int(settings, "llama_ctx_checkpoints", lc.CTX_CHECKPOINTS))
    mlock = bool(settings.get("llama_mlock", getattr(lc, "MLOCK_MODEL", True)))
    ubatch, _ = lc.resolve_ubatch_sizes(settings)

    # Set the five startup flags on all three core holders unconditionally:
    # tests pin direct presence on lc, lsm, and ml.
    for mod in (lc, lsm, ml):
        mod.CACHE_REUSE = cache_reuse
        mod.CACHE_RAM_MIB = cache_ram
        mod.CTX_CHECKPOINTS = checkpoints
        mod.MLOCK_MODEL = mlock
        mod.LLAMA_UBATCH = ubatch
    for attr_name, value in (
        ("CACHE_REUSE", cache_reuse),
        ("CACHE_RAM_MIB", cache_ram),
        ("CTX_CHECKPOINTS", checkpoints),
        ("MLOCK_MODEL", mlock),
        ("LLAMA_UBATCH", ubatch),
    ):
        _broadcast_attr(attr_name, value)

    if "vram_preflight_gate" in settings:
        _broadcast_attr("VRAM_PREFLIGHT_GATE", bool(settings.get("vram_preflight_gate", True)))

    if "llama_kv_cache_type" in settings:
        _broadcast_attr(
            "KV_CACHE_TYPE",
            _as_choice(settings, "llama_kv_cache_type", _KV_CACHE_CHOICES, getattr(lc, "KV_CACHE_TYPE", "q4_0")),
        )

    if "llama_max_slots" in settings:
        _broadcast_attr(
            "MAX_SLOTS",
            max(1, min(16, _as_int(settings, "llama_max_slots", getattr(lc, "MAX_SLOTS", 3)))),
        )

    if "llama_gpu_device" in settings:
        dev_cur = getattr(lc, "GPU_DEVICE", getattr(lc, "VULKAN_DEVICE", 0))
        dev = max(0, _as_int(settings, "llama_gpu_device", dev_cur))
        _broadcast_attr("GPU_DEVICE", dev)
        _broadcast_attr("VULKAN_DEVICE", dev)

    if "llama_extra_args" in settings:
        _broadcast_attr(
            "LLAMA_EXTRA_ARGS",
            _as_extra_args(settings, "llama_extra_args", getattr(lc, "LLAMA_EXTRA_ARGS", "")),
        )

    if "spec_decode_mode" in settings:
        _broadcast_attr(
            "SPEC_DECODE_MODE",
            _as_choice(settings, "spec_decode_mode", _SPEC_MODE_CHOICES, getattr(lc, "SPEC_DECODE_MODE", "auto")),
        )

    if "mtp_draft_n_max" in settings:
        _broadcast_attr(
            "MTP_DRAFT_N_MAX",
            max(1, min(4, _as_int(settings, "mtp_draft_n_max", getattr(lc, "MTP_DRAFT_N_MAX", 2)))),
        )

    if "mtp_draft_p_min" in settings:
        _broadcast_attr(
            "MTP_DRAFT_P_MIN",
            max(0.0, min(0.95, _as_float(settings, "mtp_draft_p_min", getattr(lc, "MTP_DRAFT_P_MIN", 0.0)))),
        )

    if "dflash_draft_p_min" in settings:
        _broadcast_attr(
            "DFLASH_DRAFT_P_MIN",
            max(0.0, min(0.95, _as_float(settings, "dflash_draft_p_min", getattr(lc, "DFLASH_DRAFT_P_MIN", 0.4)))),
        )

    if "total_vram_mib_override" in settings:
        from backend import llama_vram_table as lvt
        _broadcast_attr(
            "TOTAL_VRAM_MIB_OVERRIDE",
            max(0, _as_int(settings, "total_vram_mib_override", getattr(lvt, "TOTAL_VRAM_MIB_OVERRIDE", 0))),
        )

    if "vram_preflight_margin_mib" in settings:
        from backend import llama_manager_utils as lmu
        _broadcast_attr(
            "VRAM_PRE_FLIGHT_MARGIN_MIB",
            max(0, _as_int(settings, "vram_preflight_margin_mib", getattr(lmu, "VRAM_PRE_FLIGHT_MARGIN_MIB", 768))),
        )

    if "duo_compress_mode" in settings:
        from core import duo_compress_cycle as dcc
        _broadcast_attr(
            "DUO_COMPRESS_MODE",
            _as_choice(settings, "duo_compress_mode", _COMPRESS_MODE_CHOICES, getattr(dcc, "DUO_COMPRESS_MODE", "auto")),
        )

    if "default_unprofiled_sampling" in settings:
        from core import model_sampling as ms
        _broadcast_attr(
            "DEFAULT_UNPROFILED_SAMPLING",
            _as_sampling_dict(
                settings, "default_unprofiled_sampling", getattr(ms, "DEFAULT_UNPROFILED_SAMPLING", {}),
            ),
        )

    logger.info(
        "[FLAGS] cache_reuse=%d cache_ram=%d checkpoints=%d mlock=%s ubatch=%d",
        cache_reuse, cache_ram, checkpoints, mlock, ubatch,
    )
