"""Models API-Router."""
import asyncio

from fastapi import APIRouter

from routing.model_automap import get_model_display_map
from routing.model_automap import _VISION_PREPROCESSING_ALLOWLIST
from core.model_sampling import _model_profile
from core.state import settings, S_models_cache, registry_all
from backend import api_tags as _bk_tags

router = APIRouter(prefix="", tags=["Models"])


@router.get("/models")
async def get_models():
    global S_models_cache
    try:
        from backend.llama_models import list_available_models as _lam
        models = await asyncio.to_thread(_lam)
    except Exception:
        models = []

    if not models:
        try:
            models = await _bk_tags()
        except Exception:
            pass

    if not models and S_models_cache:
        models = list(S_models_cache)

    if not models:
        models = sorted(set(v for v in registry_all().values() if v))

    if models:
        S_models_cache = models
        import core.state as _state
        _state.S_models_cache = models

    try:
        prof_dict = get_model_display_map(models)
        prof_list = [{"name": k, **v} for k, v in prof_dict.items()]
    except Exception:
        prof_list = [{"name": m, "tags": [], "vram_gb": 4.0, "tool_call": False, "vision": False} for m in models]

    for p in prof_list:
        _mp = _model_profile(p.get("name", ""))
        if "thinking" not in p:
            p["thinking"] = _mp.get("thinking", False)
        if "vision" not in p:
            p["vision"] = _mp.get("vision", False)
        if "tool_call" not in p:
            p["tool_call"] = _mp.get("tool_call", False)
    return {
        "models": models,
        "profiles": prof_list,
        "vision_preprocessing_allowlist": list(_VISION_PREPROCESSING_ALLOWLIST),
    }


@router.get("/v1/models")
async def v1_models():
    if S_models_cache:
        models = list(S_models_cache)
    else:
        try:
            models = await _bk_tags()
        except Exception:
            models = list({a["model"] for a in settings.get("agents", {}).values() if a.get("model")})
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": 0, "owned_by": "ollama"}
            for m in models
        ]
    }
