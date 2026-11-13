"""Models API-Router."""
import asyncio
import logging

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
        # H-audit: mutate IN PLACE - a rebind starved every from-import
        # consumer (chat_run automap, /v1 agent pick) of the update.
        if isinstance(S_models_cache, list):
            S_models_cache[:] = models
        else:
            S_models_cache = models
        import core.state as _state
        if isinstance(_state.S_models_cache, list):
            _state.S_models_cache[:] = models
        else:
            _state.S_models_cache = models

    try:
        prof_dict = get_model_display_map(models)
        prof_list = [{"name": k, **v} for k, v in prof_dict.items()]
    except Exception:
        prof_list = [{"name": m, "tags": [], "vram_gb": 4.0, "tool_call": False, "vision": False} for m in models]

    for p in prof_list:
        _mname = p.get("name", "")
        _mp = _model_profile(_mname)
        if "thinking" not in p:
            p["thinking"] = _mp.get("thinking", False)
        if "vision" not in p:
            p["vision"] = _mp.get("vision", False)
        if "tool_call" not in p:
            p["tool_call"] = _mp.get("tool_call", False)
        try:
            from model_configs.models_registry import (
                get_profile as _gp,
                is_mtp as _im,
                get_load_mode as _glm,
                get_thinking_budget as _gtb,
            )
            _reg_prof = _gp(_mname) or {}
            p["mtp"] = bool(_im(_mname))
            p["load_mode"] = _glm(_mname) or _reg_prof.get("load_mode") or "auto"
            _tb = _gtb(_mname)
            p["thinking_budget"] = _tb if _tb is not None else _reg_prof.get("thinking_budget")
        except (ImportError, AttributeError, KeyError, TypeError, ValueError, OSError) as _prof_err:
            logging.getLogger(__name__).debug("model profile enrichment failed for %s: %s", _mname, _prof_err)
            p.setdefault("mtp", False)
            p.setdefault("load_mode", "auto")
            p.setdefault("thinking_budget", None)
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
