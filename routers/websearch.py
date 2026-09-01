"""Websearch API-Router."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from settings import save_settings
from core.state import (
    settings,
    _ws_configure,
)
import core.state as _state


def _ws_available() -> bool:
    return bool(_state._WEBSEARCH_AVAILABLE and _state._websearch is not None)


def _ws_module():
    return _state._websearch

router = APIRouter(prefix="/websearch", tags=["Websearch"])


@router.get("/status")
async def websearch_status():
    if not _ws_available():
        return JSONResponse({"ok": False, "reason": "websearch.py_missing"})
    return await _ws_module().check_status()


@router.post("/config")
async def websearch_config(req: Request):
    data = await req.json()
    allowed = {"searxng_host", "pipeline_websearch_enabled",
               "duo_websearch_enabled", "websearch_auto_trigger",
               "searxng_engines", "searxng_language",
               "duo_websearch_timeout_seconds", "duo_websearch_timeout_fast_seconds",
               "duo_websearch_timeout_critical_seconds"}
    for k in allowed:
        if k in data:
            settings[k] = data[k]
    save_settings(settings)
    _ws_configure(
        host=settings.get("searxng_host", "http://localhost:8888"),
        enabled=settings.get("pipeline_websearch_enabled", False)
        or settings.get("duo_websearch_enabled", False),
        engines=settings.get("searxng_engines"),
        language=settings.get("searxng_language"),
    )
    return {"ok": True, "settings": {k: settings.get(k) for k in allowed}}


@router.post("/test")
async def websearch_test(req: Request):
    if not _ws_available():
        return JSONResponse({"ok": False, "reason": "websearch.py_missing"})
    data = await req.json()
    query = data.get("query", "Python FastAPI docs")
    _safe = _state._safe_web_search
    if not callable(_safe):
        return JSONResponse({"ok": False, "reason": "safe_web_search_not_wired"})
    result = await _safe(query, max_results=2, phase="websearch_test")
    return {"ok": True, "result": result}
