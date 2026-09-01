"""VRAM API-Router."""
import asyncio
import hashlib as _hashlib
import logging
import os
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from settings import load_settings, save_settings
from vram.loader import _bk_load, _bk_evict
import vram.loader as _vldr
from core.duo_helpers import DEFAULT_VRAM_BUDGET_GB
import core.state as _state
from core.state import settings, registry_get, _get_num_ctx

try:
    from backend.llama_vram_table import VRAM_GB as _VRAM_LOOKUP_GB
    if not _VRAM_LOOKUP_GB:
        _VRAM_LOOKUP_GB = {}
except Exception:
    _VRAM_LOOKUP_GB: dict[str, float] = {}

try:
    from backend.llama_compat import force_kill_all as _force_kill_all
except ImportError:
    _force_kill_all = None

logger = logging.getLogger("hivemind.server")

router = APIRouter(prefix="/vram", tags=["VRAM"])

_vram_status_cache = {"ts": 0.0, "payload": None, "etag": ""}
_VRAM_STATUS_CACHE_TTL = 30.0


@router.get("/stats")
async def get_prefetch_stats():
    avgs = settings.get("prefetch_agent_avgs", {})
    lead = float(settings.get("prefetch_lead_seconds", 8.0))
    enabled = settings.get("smart_preload_enabled", True)
    agents_info = {}
    for agent_key in ["analyst", "refiner", "critic", "synthesizer"]:
        avg = avgs.get(agent_key, 0)
        fire_at = round(max(1.0, avg - lead), 1) if avg > 0 else None
        agents_info[agent_key] = {
            "model": registry_get(agent_key),
            "avg_seconds": avg,
            "prefetch_fires_at": fire_at,
        }
    return {
        "enabled": enabled,
        "prefetch_lead_seconds": lead,
        "agents": agents_info,
        "runs_logged": sum(1 for v in avgs.values() if v > 0),
    }


@router.get("/ps")
async def ollama_ps():
    try:
        models = await _vldr._bk_ps()
        result = []
        for m in models:
            vram = m.get("size_vram", 0)
            total = m.get("size", 0)
            result.append({
                "name": m.get("name", ""),
                "size_vram": vram,
                "size_total": total,
                "size_vram_gb": round(vram / 1024**3, 2),
                "size_total_gb": round(total / 1024**3, 2),
                "expires_at": m.get("expires_at", ""),
            })
        return {"models": result, "count": len(result)}
    except Exception as e:
        return JSONResponse({"models": [], "error": str(e)}, status_code=200)


@router.get("/status")
async def vram_status(req: Request):
    now = time.time()
    if now - _vram_status_cache["ts"] < _VRAM_STATUS_CACHE_TTL and _vram_status_cache["payload"] is not None:
        return JSONResponse(content=_vram_status_cache["payload"],
                           headers={"ETag": _vram_status_cache["etag"], "Cache-Control": "max-age=5"})
    try:
        raw_models = await _vldr._bk_ps()
    except Exception:
        raw_models = []

    _raw = settings.get("vram_budget_gb")
    budget_gb = float(_raw) if _raw is not None else DEFAULT_VRAM_BUDGET_GB
    models = []
    loaded_names = set()
    for m in raw_models:
        name = m.get("name", "")
        loaded_names.add(name)
        lookup = _VRAM_LOOKUP_GB.get(name)
        if lookup is None:
            lookup = next(
                (v for k, v in _VRAM_LOOKUP_GB.items()
                 if name.startswith(k.split(":")[0] + ":") and k.split(":")[-1] == name.split(":")[-1]),
                None
            )
        if lookup is None:
            name_base = name.split(":")[0]
            lookup = next(
                (v for k, v in _VRAM_LOOKUP_GB.items()
                 if name_base == k.split(":")[0]),
                None
            )
        vram_gb = lookup if lookup is not None else round(m.get("size_vram", 0) / 1024**3, 2)
        models.append({
            "name": name,
            "vram_gb": vram_gb,
            "total_gb": round(m.get("size", 0) / 1024**3, 2),
            "expires_at": m.get("expires_at", ""),
            "from_lookup": lookup is not None,
        })

    used_gb = sum(m["vram_gb"] for m in models)

    if _state.pipeline:
        agent_to_model = {k: registry_get(k) for k in _state.pipeline.agents}
    else:
        agent_to_model = {}
    loaded_agents = {}
    for agent_key, model_name in agent_to_model.items():
        name_base = model_name.split(":")[0]
        matched = next((ln for ln in loaded_names if ln.split(":")[0] == name_base), None)
        if matched:
            loaded_agents.setdefault(matched, []).append(agent_key)

    _judge_model = _state._registry.get("judge", "")
    judge_gb = round(_VRAM_LOOKUP_GB.get(_judge_model, 2.5), 1)
    budget_eff = budget_gb - 0.4
    solo_mode = False
    if models:
        _judge_base = _judge_model.split(":")[0] if _judge_model else ""
        non_judge = [m["vram_gb"] for m in models if m["name"].split(":")[0] != _judge_base]
        if non_judge:
            biggest = max(non_judge)
            solo_mode = (biggest + judge_gb > budget_eff)

    payload = {
        "models": models,
        "used_gb": round(used_gb, 2),
        "budget_gb": budget_gb,
        "pct": round(min(100, used_gb / budget_gb * 100), 1) if budget_gb else 0,
        "loaded_count": len(models),
        "loaded_agents": loaded_agents,
        "solo_mode": solo_mode,
        "judge_gb": judge_gb,
    }

    _etag_src = f"{[m['name'] for m in models]}:{round(used_gb, 1)}:{round(budget_gb, 2)}"
    etag = '"' + _hashlib.md5(_etag_src.encode()).hexdigest()[:12] + '"'
    _vram_status_cache["ts"] = time.time()
    _vram_status_cache["payload"] = payload
    _vram_status_cache["etag"] = etag
    if req.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "max-age=5"})
    return JSONResponse(content=payload, headers={"ETag": etag, "Cache-Control": "max-age=5"})


@router.get("/table")
async def vram_table():
    try:
        from backend.llama_vram_table import VRAM_GB
        return JSONResponse(content={"vram_gb": VRAM_GB})
    except ImportError:
        return JSONResponse(content={"vram_gb": _VRAM_LOOKUP_GB})


@router.get("/estimate")
async def vram_estimate(model: str = "", ctx: int = 4096):
    if not model:
        return JSONResponse({"error": "model required"}, status_code=400)
    try:
        from backend.llama_vram_table import vram_of_moe, vram_of_with_ctx, VRAM_GB, _MOE_TABLE
        _key = model.strip().lower()
        _moe = _MOE_TABLE.get(_key) or _MOE_TABLE.get(_key.replace("-ud", ""))
        if _moe:
            total = vram_of_moe(model, int(ctx))
            kv_gb = round(max(0, total - _moe["active_gpu_gb"]), 2)
            ram_gb = round(20.6, 1)
        else:
            total = vram_of_with_ctx(model, int(ctx))
            base = float(VRAM_GB.get(model, 4.0))
            kv_gb = round(max(0, total - base), 2)
            ram_gb = 0.0
        return {"model": model, "ctx": int(ctx), "vram_gb": round(total, 2), "vram_kv_gb": kv_gb, "ram_gb": ram_gb}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/log")
async def vram_log(model: str = ""):
    import glob as _glob
    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_files = sorted(_glob.glob(str(log_dir / "llama_server_*.log")), key=os.path.getmtime, reverse=True)
    if not log_files:
        return {"error": "No log files found"}
    errors = []
    for lf in log_files[:6]:
        try:
            content = Path(lf).read_text(encoding="utf-8", errors="replace")
            if model and model not in content:
                continue
            for line in content.splitlines():
                ll = line.lower()
                if any(kw in ll for kw in ("error", "failed", "cuda error", "out of memory", "oom",
                                              "invalid", "cannot", "unable", "exception", "traceback")):
                    errors.append(line.strip()[:200])
        except Exception:
            pass
    if errors:
        return {"error": errors[-1] if len(errors) == 1 else f"{len(errors)} errors: " + errors[-1]}
    return {"error": "No errors found in logs"}


@router.post("/budget")
async def set_vram_budget(req: Request):
    data = await req.json()
    gb = data.get("gb") or data.get("vram_budget_gb")
    logger.info("[VRAM-BUDGET] POST /vram/budget: raw gb=%s, data=%s", gb, data)
    if gb is None:
        return {"ok": False, "error": "No 'gb' value provided"}
    try:
        gb = float(gb)
    except (ValueError, TypeError):
        return {"ok": False, "error": f"Invalid value: {gb}"}
    if not (1.0 <= gb <= 96.0):
        return {"ok": False, "error": f"Value outside 1-96 GB: {gb}"}
    settings["vram_budget_gb"] = gb
    try:
        import backend.llama_server_manager as _lsm_vram
        _lsm_vram.VRAM_BUDGET_GB = gb
    except Exception:
        pass
    await asyncio.to_thread(save_settings, settings)
    _verify = load_settings()
    logger.info("[VRAM-BUDGET] After save: settings['vram_budget_gb']=%s, verify_reload=%s",
                 settings.get("vram_budget_gb"), _verify.get("vram_budget_gb"))
    return {"ok": True, "vram_budget_gb": gb}


@router.get("/budget/debug")
async def vram_budget_debug():
    _result = {
        "in_memory": settings.get("vram_budget_gb"),
        "in_memory_type": type(settings.get("vram_budget_gb")).__name__,
    }
    try:
        _reloaded = load_settings()
        _result["reloaded"] = _reloaded.get("vram_budget_gb")
        _result["reloaded_type"] = type(_reloaded.get("vram_budget_gb")).__name__
    except Exception as e:
        _result["reloaded_error"] = str(e)
    try:
        import backend.llama_server_manager as _lsm_dbg
        _result["lsm_VRAM_BUDGET_GB"] = getattr(_lsm_dbg, "VRAM_BUDGET_GB", "NOT_SET")
    except Exception as e:
        _result["lsm_error"] = str(e)
    try:
        _BASE = Path(__file__).parent.parent
        _sf = _BASE / "settings.json"
        if _sf.exists():
            import json as _json_dbg
            _raw = _json_dbg.loads(_sf.read_text(encoding="utf-8"))
            _result["file_raw"] = _raw.get("vram_budget_gb")
            _result["file_keys_sample"] = list(_raw.keys())[:20]
        else:
            _result["file_error"] = "settings.json not found at " + str(_sf)
    except Exception as e:
        _result["file_error"] = str(e)
    return _result


@router.post("/preload_judge")
async def ollama_preload_judge():
    try:
        if not _state.pipeline:
            return JSONResponse({"ok": False, "error": "Pipeline not initialized"}, status_code=400)
        judge = _state.pipeline.agents.get("judge")
        if not judge or not judge.model:
            return JSONResponse({"ok": False, "error": "No judge configured"}, status_code=400)
        _pj_ctx = _get_num_ctx(judge.model)
        await _bk_load(judge.model, keep_alive="10m", num_ctx=_pj_ctx)
        return {"ok": True, "model": judge.model}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/load")
async def ollama_load(req: Request):
    try:
        data = await req.json()
        model = data.get("model", "")
        keep_alive = data.get("keep_alive", "-1")
        if not model:
            return JSONResponse({"error": "model fehlt"}, status_code=400)
        _load_ctx = _get_num_ctx(model)
        await _bk_load(model, keep_alive=keep_alive, num_ctx=_load_ctx)
        return {"ok": True, "model": model, "keep_alive": keep_alive}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/unload")
async def ollama_unload(req: Request):
    try:
        data = await req.json()
        model = data.get("model", "")
        if not model:
            return JSONResponse({"error": "model fehlt"}, status_code=400)
        await _bk_evict(model)
        return {"ok": True, "model": model}
    except Exception as e:
        import traceback
        logger.error("[VRAM-UNLOAD-500] %s\n%s", e, traceback.format_exc())
        return JSONResponse({"ok": False, "error": str(e), "tb": traceback.format_exc()[-500:]}, status_code=500)


@router.post("/preload_pipeline")
async def ollama_preload_pipeline():
    try:
        keep = settings.get("default_keep_alive", "30m")
        seen = set()
        models_to_load = []
        if _state.pipeline:
            for a in sorted(_state.pipeline.agents.values(), key=lambda x: _VRAM_LOOKUP_GB.get(x.model, 99)):
                if a.model not in seen:
                    seen.add(a.model)
                    models_to_load.append(a.model)
        results = {}
        for model in models_to_load:
            try:
                _pp_ctx = _get_num_ctx(model)
                await _bk_load(model, keep_alive=keep, num_ctx=_pp_ctx)
                results[model] = "loaded"
            except Exception as e:
                results[model] = f"error: {str(e)[:60]}"
        return {"ok": True, "results": results}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/kill_all")
async def llama_kill_all():
    if _force_kill_all is None:
        return JSONResponse({"ok": False, "error": "force_kill_all not available"}, status_code=500)
    try:
        result = await _force_kill_all()
        return {"ok": True, **result}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
