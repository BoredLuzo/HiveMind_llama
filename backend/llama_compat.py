


import re
import logging
from typing import Optional

from .llama_server_manager import manager
from .llama_config import CONTEXT_SIZE_DEFAULT, DEFAULT_IDLE_TIMEOUT_SECONDS
from .llama_models import list_available_models, resolve_model_path

logger = logging.getLogger("llama_compat")


# ── /api/tags ────────────────────────────────────────────────────────────────

async def tags_list() -> dict:
    models = list_available_models()
    return {"models": [{"name": m, "model": m} for m in models]}


# ── /api/ps ──────────────────────────────────────────────────────────────────

async def ps_list() -> dict:
    loaded = await manager.list_loaded()
    if not loaded:
        return {"models": []}
    status = manager.vram_status()
    slot_by_model = {s.get("model", ""): s for s in status.get("slots", []) if s.get("model")}
    models = []
    for m in loaded:
        name = m.get("name") or ""
        slot = slot_by_model.get(name, {})
        vram_gb = slot.get("vram_gb", 0) or 0
        vram_bytes = int(vram_gb * 1024 ** 3)
        models.append({
            "name": name,
            "model": name,
            "expires_at": "n/a",
            "size_vram": vram_bytes,
            "size": vram_bytes,
            "port": m.get("port"),
            "pinned": m.get("pinned", False),
        })
    return {"models": models}


# ── /api/generate (keep_alive) ───────────────────────────────────────────────

async def model_load(model: str, keep_alive: str = "10m",
                     num_ctx: Optional[int] = None):
    seconds = _parse_keep_alive(keep_alive)
    if seconds == 0:
        await manager.evict(model)
        return
    if seconds < 0:
        await model_pin(model, num_ctx=num_ctx)
        return
    try:
        await manager.load(model, keep_alive_seconds=seconds,
                           num_ctx=num_ctx or CONTEXT_SIZE_DEFAULT, pin=False)
    except Exception as e:
        logger.warning(f"model_load({model}) failed: {e}")
        raise


async def model_evict(model: str):
    await manager.evict(model)


async def model_pin(model: str, num_ctx: Optional[int] = None):
    try:
        await manager.load(model, keep_alive_seconds=DEFAULT_IDLE_TIMEOUT_SECONDS,
                           num_ctx=num_ctx or CONTEXT_SIZE_DEFAULT, pin=True)
    except Exception as e:
        logger.warning(f"model_pin({model}) failed: {e}")
        raise


async def force_kill_all() -> dict:
    return await manager.force_kill_all()


async def model_unpin(model: str, keep_alive: str = "10m"):
    seconds = _parse_keep_alive(keep_alive)
    await manager.unpin(model, keep_alive_seconds=max(seconds, 60))


async def evict_all_unpinned():
    await manager.evict_all_unpinned()


# ── Prefetch ──────────────────────────────────────────────────────────────────

async def model_prefetch(model: str, num_ctx: Optional[int] = None):


    await manager.prefetch_next(model, num_ctx=num_ctx)


def model_available(model: str) -> bool:
    return resolve_model_path(model) is not None


# ── Helper ────────────────────────────────────────────────────────────────────

def _parse_keep_alive(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    if s in ("-1", "-1s"):
        return -1.0
    if s in ("0", "0s"):
        return 0.0
    m = re.match(r'^(\d+(?:\.\d+)?)\s*([smhd]?)$', s)
    if m:
        n    = float(m.group(1))
        unit = m.group(2)
        return n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 1}.get(unit, 1)
    try:
        return float(s)
    except ValueError:
        return DEFAULT_IDLE_TIMEOUT_SECONDS
