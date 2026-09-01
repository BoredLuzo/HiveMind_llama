"""Vision API-Router."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from vision.preprocess import _load_vision_model_cfg, _save_vision_model_cfg
from backend import api_tags as _bk_tags
from core.state import _vision_cfg, S_models_cache

router = APIRouter(prefix="/vision", tags=["Vision"])


@router.get("/config")
async def get_vision_config():
    cfg = _load_vision_model_cfg()
    _vision_cfg.update(cfg)
    return _vision_cfg


@router.post("/config")
async def set_vision_config(req: Request):
    data = await req.json()
    allowed = {"model", "enabled", "prompt"}
    patch = {k: v for k, v in data.items() if k in allowed}
    _vision_cfg.update(patch)
    _save_vision_model_cfg(_vision_cfg)
    return {"ok": True, "config": _vision_cfg}


@router.post("/test")
async def test_vision_model(req: Request):
    data = await req.json()
    model = data.get("model") or _vision_cfg.get("model")
    if not model:
        return JSONResponse({"ok": False, "reason": "No model specified"}, status_code=400)
    try:
        models = list(S_models_cache) if S_models_cache else await _bk_tags()
        available = model in models
        return {"ok": True, "model": model, "available": available,
                "vision_capable": any(v in model.lower() for v in ["vl", "llava", "vision", "moondream", "minicpm", "glm", "granite3.2"])}
    except Exception as e:
        return {"ok": False, "reason": str(e)}
