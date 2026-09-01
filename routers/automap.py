"""Automap API-Router."""
import asyncio
from pathlib import Path

from fastapi import APIRouter, Request

from settings import save_settings
from core.duo_helpers import DEFAULT_VRAM_BUDGET_GB
from core.model_sampling import _model_profile
from routing.model_automap import (
    get_automap, get_model_display_map,
    get_routing_weights_summary, save_routing_weights,
)
import core.state as _state
from core.state import (
    settings, registry_get, registry_set,
    detect_vision_need,
)

router = APIRouter(prefix="/automap", tags=["Automap"])

_BASE_FILE = Path(__file__).parent.parent / "server.py"


@router.post("/p")
async def automap_endpoint(req: Request):
    data = await req.json()
    query = data.get("query", "")
    images = data.get("images", [])
    available = list(_state.S_models_cache) if _state.S_models_cache else list(
        set(a.model for a in _state.pipeline.agents.values())
    ) if _state.pipeline else []
    vision_type = detect_vision_need(images, query)
    _raw_b = settings.get("vram_budget_gb")
    result = get_automap(query, available, has_images=bool(images),
                          task_type_override=vision_type, base_path=_BASE_FILE,
                          vram_budget_gb=float(_raw_b) if _raw_b is not None else DEFAULT_VRAM_BUDGET_GB)
    result["model_profiles"] = get_model_display_map(available)
    return result


@router.post("/preview")
async def automap_preview(req: Request):
    return await automap_endpoint(req)


@router.post("/apply")
async def automap_apply(req: Request):
    data = await req.json()
    assignments = data.get("assignments", {})
    for agent_key, model in assignments.items():
        if _state.pipeline and agent_key in _state.pipeline.agents:
            registry_set(agent_key, model)
            if agent_key in settings.get("agents", {}):
                settings["agents"][agent_key]["model"] = model
    settings["automap_excluded"] = []
    await asyncio.to_thread(save_settings, settings)
    return {"ok": True, "applied": assignments}


@router.get("/profiles")
async def automap_profiles():
    available = list(_state.S_models_cache) if _state.S_models_cache else list(
        set(a.model for a in _state.pipeline.agents.values())
    ) if _state.pipeline else []
    return {
        "models": get_model_display_map(available),
        "task_types": ["code", "reasoning", "creative", "factual", "vision", "ocr", "math", "general"],
    }


@router.get("/current")
async def automap_current():
    assignments = {}
    if _state.pipeline:
        for agent_key, agent in _state.pipeline.agents.items():
            model = registry_get(agent_key)
            _prof = _model_profile(model)
            m_lower = model.lower()
            assignments[agent_key] = {
                "model": model,
                "display": model.replace(":latest", ""),
                "vision": _prof.get("vision", False) or any(v in m_lower for v in ["vl", "llava", "vision", "moondream"]),
                "thinking": _prof.get("thinking", False),
            }
    return {"assignments": assignments}


@router.get("/weights")
async def get_automap_weights():
    return get_routing_weights_summary(_BASE_FILE)


@router.delete("/weights")
async def reset_automap_weights():
    save_routing_weights(_BASE_FILE, {})
    return {"ok": True}
