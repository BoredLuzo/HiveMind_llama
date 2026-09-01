"""Debug API-Router."""
import asyncio
import json
import traceback
from pathlib import Path

from fastapi import APIRouter

from core.duo_helpers import _get_thinking_profile, _calculate_thinking_tokens
from core.state import settings, S_models_cache, registry_all
from backend import api_tags as _bk_tags

router = APIRouter(prefix="", tags=["Debug"])


@router.get("/debug/tasks")
async def debug_tasks():


    out = []
    for task in asyncio.all_tasks():
        name = task.get_name()
        done = task.done()
        cancelled = task.cancelled()
        frames = task.get_stack() if not done else []
        stack_lines = []
        for fr in frames:
            stack_lines.append(
                f"{fr.f_code.co_filename}:{fr.f_lineno} in {fr.f_code.co_name}"
            )
        coro = task.get_coro()
        coro_desc = ""
        if coro is not None:
            try:
                cr_frame = coro.cr_frame
                if cr_frame is not None:
                    coro_desc = (
                        f"{cr_frame.f_code.co_filename}:{cr_frame.f_lineno} "
                        f"in {cr_frame.f_code.co_name}"
                    )
            except Exception:
                pass
        out.append({
            "name": name,
            "done": done,
            "cancelled": cancelled,
            "coro_at": coro_desc,
            "stack": stack_lines[-12:],
        })
    return {"tasks": out}


@router.get("/prefetch/stats")
async def prefetch_stats():
    avgs = settings.get("prefetch_agent_avgs", {}) or {}
    return {
        "prefetch_lead_seconds": float(settings.get("prefetch_lead_seconds", 8.0) or 8.0),
        "agent_avgs": [
            {"agent": agent, "avg_elapsed_s": float(v.get("elapsed", 0) if isinstance(v, dict) else v), "prefetch_fires_at": v.get("fires_at", 0) if isinstance(v, dict) else 0}
            for agent, v in avgs.items()
        ],
    }


@router.get("/debug/models")
async def debug_models():
    _BASE = Path(__file__).parent.parent
    result = {}
    mj = _BASE / "models.json"
    result["models_json_exists"] = mj.exists()
    if mj.exists():
        try:
            raw = json.loads(mj.read_text(encoding="utf-8"))
            result["models_json_keys"] = [k for k in raw if not k.startswith("_")][:10]
            result["models_json_count"] = len([k for k in raw if not k.startswith("_")])
        except Exception as e:
            result["models_json_error"] = str(e)
    result["S_models_cache"] = list(S_models_cache)[:10]
    result["S_models_cache_count"] = len(S_models_cache)
    try:
        reg = registry_all()
        result["registry"] = reg
    except Exception as e:
        result["registry_error"] = str(e)
    try:
        result["settings_agents"] = {k: v.get("model", "") for k, v in settings.get("agents", {}).items()}
    except Exception as e:
        result["settings_error"] = str(e)
    try:
        tags = await _bk_tags()
        result["bk_tags"] = tags[:10]
        result["bk_tags_count"] = len(tags)
    except Exception as e:
        result["bk_tags_error"] = str(e)
    return result


@router.get("/thinking/debug")
async def thinking_budget_debug(model: str = "", agent: str = "", ctx: int = 10240, input_tokens: int = 0):
    if not model:
        model = settings.get("agents", {}).get("duo_planner", {}).get("model", "")
        if not model:
            model = settings.get("agents", {}).get("duo_coder", {}).get("model", "qwen3.5:9b-ud")
    _profile = _get_thinking_profile(model, settings)
    _budget = _calculate_thinking_tokens(model, settings, input_tokens=input_tokens, available_ctx=ctx, agent_name=agent)
    return {
        "model": model,
        "agent": agent or "(none)",
        "ctx": ctx,
        "input_tokens": input_tokens,
        "thinking_profile": _profile,
        "calculated_budget": _budget,
        "max_output_available": ctx - input_tokens - _budget,
        "total_max_tokens": ctx,
    }
