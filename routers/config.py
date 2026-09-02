"""Config API-Router — Settings, Memory."""
import asyncio
import copy
import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from settings import (
    load_settings, save_settings,
    DEFAULT_AGENT_CFG, DEFAULT_SETTINGS,
)
import core.state as _state
from core.state import (
    settings, registry_all,
    registry_set,
    apply_settings_to_pipeline,
    _refresh_safe_profile_policy,
    _ws_configure,
)

logger = logging.getLogger("hivemind.server")

router = APIRouter(prefix="", tags=["Config"])


@router.get("/settings")
async def get_settings():
    s = dict(settings)
    # SECURITY: Secrets nicht im Klartext an den Client geben.
    if s.get("git_token"):
        s["git_token"] = "****"
    _sa = s.setdefault("agents", {})
    for _ak, _av in DEFAULT_AGENT_CFG.items():
        _sa.setdefault(_ak, _av)
        if isinstance(_sa[_ak], dict):
            _sa[_ak].setdefault("model", _av.get("model", ""))
            _sa[_ak].setdefault("temperature", _av.get("temperature", 0.3))
            _sa[_ak].setdefault("max_tokens", _av.get("max_tokens", 400))
            _sa[_ak].setdefault("thinking", _av.get("thinking", False))
            _sa[_ak].setdefault("thinking_budget", _av.get("thinking_budget", 0))
    s["_registry"] = registry_all()
    s["_safe_profile_state"] = dict(_state._safe_profile_state)
    try:
        from backend.llama_config import (
            _MOE_EXPERT_COUNTS as _moe_tbl,
            detect_moe_count as _moe_detect,
            is_moe_model as _moe_check,
        )
        # MOE-AUTODETECT (2026-08-27): moe_expert_defaults = kalibrierte Tabelle
        _defaults = dict(_moe_tbl)
        _autodetect: dict[str, bool] = {}
        _cands: set[str] = set()
        for _v in registry_all().values():
            if _v:
                _cands.add(str(_v))
        for _ag in (s.get("agents") or {}).values():
            if isinstance(_ag, dict) and _ag.get("model"):
                _cands.add(str(_ag["model"]))
        for _c in _cands:
            _key = str(_c).replace("-ud", "")
            if not _moe_check(_key):
                continue
            _n = _moe_detect(_key)
            if _n > 0 and _key not in _defaults:
                _defaults[_key] = _n
                _autodetect[_key] = True
        s["moe_expert_defaults"] = _defaults
        s["moe_autodetect"] = _autodetect
    except Exception:
        pass
    # CTX-DEFAULTS (2026-08-27): effective num_ctx default per agent role from
    # num_ctx_config.get_num_ctx — so agent cards with Context "Auto" show the
    # real model default and can cap max_tokens on it.
    try:
        from hive_functions.num_ctx_config import get_num_ctx as _gnc
        _ctx_defs: dict[str, int] = {}
        _role_of = {
            "analyst": "analyst", "refiner": None, "critic": None,
            "synthesizer": None, "direct": None, "judge": None,
            "duo_coder": "duo_coder", "duo_critic": None,
        }
        for _ak, _role in _role_of.items():
            _m = (s.get("agents") or {}).get(_ak, {}).get("model", "")
            if not _m:
                _m = registry_all().get(_ak, "")
            if not _m:
                continue
            _ctx = _gnc(str(_m), _role) if _role else _gnc(str(_m))
            if _ctx:
                _ctx_defs[_ak] = int(_ctx)
        _vm = (s.get("vision_agent_model") or "").strip()
        if _vm:
            _vctx = _gnc(_vm, "vision")
            if _vctx:
                _ctx_defs["vision"] = int(_vctx)
        s["ctx_defaults"] = _ctx_defs
    except Exception:
        pass
    return s


@router.post("/settings")
async def post_settings(req: Request):
    data = await req.json()
    if "ctx_overrides" in data and not isinstance(data.get("ctx_overrides"), dict):
        return JSONResponse({"error": "ctx_overrides must be an object"}, status_code=400)
    # the whole workspace chain was crippled (follow-up ran on repo root).
    if "workspace" in data:
        _ws_raw = str(data.get("workspace") or "").strip()
        if _ws_raw and not Path(_ws_raw).exists():
            logger.warning(
                "[SETTINGS-GUARD] workspace '%s' does not exist - rejected "
                "(previous value stays active)", _ws_raw,
            )
            return JSONResponse({
                "error": f"Workspace path does not exist: {_ws_raw}",
            }, status_code=400)
        data["workspace"] = _ws_raw  # trim durchreichen (leer = Env/Default)
    if "agents" in data:
        _incoming_agents = data.pop("agents")
        _sa = settings.setdefault("agents", {})
        if isinstance(_incoming_agents, dict):
            for _ak, _av in _incoming_agents.items():
                if _ak == "duo_coder" and isinstance(_av, dict):
                    try:
                        _in_mt = int(_av.get("max_tokens") or 0)
                    except Exception:
                        _in_mt = 0
                    try:
                        _cur_mt = int((settings.get("agents", {}).get("duo_coder") or {}).get("max_tokens") or 0)
                    except Exception:
                        _cur_mt = 0
                    # 20.08.-bug (stale tab POSTs a cached snapshot, 8000→2800)
                    #   external clients.)
                    if 0 < _in_mt < _cur_mt and not bool(data.get("agents_force")):
                        logger.warning(
                            "[SETTINGS-GUARD] duo_coder.max_tokens decrease %d→%d ignored "
                            "(stale protection; explicitly allow with \"agents_force\": true)",
                            _cur_mt, _in_mt,
                        )
                        _av = dict(_av)
                        _av.pop("max_tokens", None)
                if isinstance(_av, dict) and isinstance(_sa.get(_ak), dict):
                    _sa[_ak].update(_av)
                else:
                    _sa[_ak] = _av
        for _ak, _av in DEFAULT_AGENT_CFG.items():
            _sa.setdefault(_ak, _av)
            if isinstance(_sa[_ak], dict):
                _sa[_ak].setdefault("model", _av.get("model", ""))
                _sa[_ak].setdefault("temperature", _av.get("temperature", 0.3))
                _sa[_ak].setdefault("max_tokens", _av.get("max_tokens", 400))
                _sa[_ak].setdefault("thinking", _av.get("thinking", False))
                _sa[_ak].setdefault("thinking_budget", _av.get("thinking_budget", 0))
    _DEEP_MERGE_KEYS = {"soul_evolve_agent", "intent_agent", "exploration_agent"}
    for _dmk in _DEEP_MERGE_KEYS:
        if _dmk in data:
            _incoming = data.pop(_dmk)
            _existing = settings.setdefault(_dmk, {})
            if isinstance(_incoming, dict) and isinstance(_existing, dict):
                for _dk, _dv in _incoming.items():
                    if _dk == "model" and _dv == "" and _existing.get("model"):
                        _inc_enabled = _incoming.get("enabled")
                        if _inc_enabled is not False:
                            continue
                    _existing[_dk] = _dv
            else:
                settings[_dmk] = _incoming
    settings.update(data)
    _xa = settings.get("exploration_agent")
    if isinstance(_xa, dict) and _xa.get("enabled") and not (_xa.get("model") or "").strip():
        _xa["model"] = DEFAULT_SETTINGS["exploration_agent"]["model"]
    _refresh_safe_profile_policy()
    apply_settings_to_pipeline(settings)
    if _state._WEBSEARCH_AVAILABLE and any(k in data for k in (
            "duo_websearch_enabled", "pipeline_websearch_enabled",
            "searxng_host", "searxng_engines", "searxng_language")):
        _ws_configure(
            host=settings.get("searxng_host", "http://localhost:8888"),
            enabled=settings.get("pipeline_websearch_enabled", False)
            or settings.get("duo_websearch_enabled", False),
            engines=settings.get("searxng_engines"),
            language=settings.get("searxng_language"),
        )
    try:
        await asyncio.to_thread(save_settings, settings)
    except Exception as e:
        logger.warning("[settings] save_settings failed: %s", e, exc_info=True)
        return JSONResponse({"error": f"Save failed: {str(e)[:120]}"}, status_code=500)
    return {"ok": True}


@router.post("/settings/agent")
async def set_agent(req: Request):
    d = await req.json()
    key = d.get("agent")
    if not _state.pipeline or key not in _state.pipeline.agents:
        return JSONResponse({"error": f"Unknown: {key}"}, status_code=400)
    _model = d.get("model") or ""
    try:
        if _model:
            registry_set(key, _model)
            settings.setdefault("agents", {}).setdefault(key, {})["model"] = _model
            _excluded = settings.setdefault("automap_excluded", [])
            if key not in _excluded:
                _excluded.append(key)
        if "temperature" in d:
            if d["temperature"] is None:
                raise ValueError("temperature missing")
            v = float(d["temperature"])
            _state.pipeline.agents[key].temperature = v
            settings.setdefault("agents", {}).setdefault(key, {})["temperature"] = v
        if "max_tokens" in d:
            if d["max_tokens"] is None:
                raise ValueError("max_tokens missing")
            v = int(d["max_tokens"])
            _state.pipeline.agents[key].max_tokens = v
            settings.setdefault("agents", {}).setdefault(key, {})["max_tokens"] = v
        if "thinking" in d:
            _t = bool(d["thinking"])
            _state.pipeline.agents[key].thinking = _t
            settings.setdefault("agents", {}).setdefault(key, {})["thinking"] = _t
        if "thinking_budget" in d:
            if d["thinking_budget"] is None:
                raise ValueError("thinking_budget missing")
            _tb = int(d["thinking_budget"])
            _state.pipeline.agents[key].thinking_budget = _tb
            settings.setdefault("agents", {}).setdefault(key, {})["thinking_budget"] = _tb
        if "ctx" in d:
            if d["ctx"] is None:
                raise ValueError("ctx missing")
            _ctx = int(d["ctx"])
            _co = settings.setdefault("ctx_overrides", {})
            _co.setdefault("roles", {})[key] = _ctx if _ctx > 0 else None
            if _ctx <= 0:
                _co["roles"].pop(key, None)
    except Exception as e:
        logger.warning("[settings/agent] configuration failed (%s): %s", key, e, exc_info=True)
        return JSONResponse({"error": f"Configuration failed: {str(e)[:120]}"}, status_code=500)
    # NO model load on save (user decision 2026-08-19): only change the
    # settings, do not load. The model is loaded lazily on first real use
    # (pipeline/duo runners call ensure_loaded).
    try:
        await asyncio.to_thread(save_settings, settings)
    except Exception as e:
        logger.warning("[settings/agent] save_settings failed: %s", e, exc_info=True)
        return JSONResponse({"error": f"Save failed: {str(e)[:120]}"}, status_code=500)
    return {"ok": True}


@router.post("/settings/all_model")
async def set_all_model(req: Request):
    d = await req.json()
    m = d.get("model", "")
    if not m:
        return JSONResponse({"error": "model missing"}, status_code=400)
    if _state.pipeline:
        for key in list(_state.pipeline.agents.keys()):
            registry_set(key, m)
    for key in settings["agents"]:
        settings["agents"][key]["model"] = m
    try:
        await asyncio.to_thread(save_settings, settings)
    except Exception as e:
        logger.warning("[settings/all_model] save_settings failed: %s", e, exc_info=True)
        return JSONResponse({"error": f"Save failed: {str(e)[:120]}"}, status_code=500)
    return {"ok": True}


@router.get("/registry")
async def get_registry():
    return registry_all()


@router.get("/memory")
async def get_memory():
    if not _state.memory:
        return {"memories": []}
    return {
        "memories": [
            {"key": k, "value": v, "saved_at": d}
            for k, v, d in _state.memory.list_memories()
        ]
    }


@router.delete("/memory/{key}")
async def del_memory(key: str):
    if not _state.memory:
        return {"ok": False}
    return {"ok": _state.memory.forget(key)}


@router.post("/memory/clear_session")
async def clear_session(chat_id: str = Query(None, description="Optional: chat_id for ProjectState deletion")):
    if _state.memory:
        _state.memory.clear_session()
    if chat_id:
        try:
            from context.project_state import ProjectStateManager
            ProjectStateManager().delete(chat_id)
        except Exception:
            pass
    return {"ok": True}


@router.post("/memory/{key}")
async def set_memory(key: str, req: Request):
    data = await req.json()
    value = data.get("value", "").strip()
    if not key or not value:
        return JSONResponse({"error": "key and value required"}, status_code=400)
    if not _state.memory:
        return JSONResponse({"error": "Memory not available"}, status_code=500)
    try:
        if hasattr(_state.memory, "remember"):
            _state.memory.remember(key, value)
        elif hasattr(_state.memory, "store"):
            _state.memory.store(key, value)
        elif hasattr(_state.memory, "set"):
            _state.memory.set(key, value)
        elif hasattr(_state.memory, "add"):
            _state.memory.add(key, value)
        elif _state.pipeline:
            await _state.pipeline._handle_memory_request(f"Remember: {key} is {value}")
    except Exception as e:
        return JSONResponse({"error": f"Memory error: {str(e)[:80]}"}, status_code=500)
    return {"ok": True, "key": key, "value": value}
