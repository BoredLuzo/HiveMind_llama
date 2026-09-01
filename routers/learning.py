"""Learning / Model-Configs API-Router."""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from model_configs import (
    list_base_configs, get_base_config,
    list_learned_models, list_learned_configs,
    get_learned_config, save_learned_config, delete_learned_config,
    reset_learned_configs, get_effective_config,
    read_learning_log, append_learning_log, clear_learning_log,
)

router = APIRouter(prefix="/model_configs", tags=["Learning"])

_BASE_FILE = Path(__file__).parent.parent / "server.py"


@router.get("/base")
async def get_base_configs():
    return list_base_configs(_BASE_FILE)


@router.get("/base/{agent}")
async def get_base_config_ep(agent: str):
    return get_base_config(_BASE_FILE, agent)


@router.get("/learned")
async def get_learned_models_ep():
    models = list_learned_models(_BASE_FILE)
    result = {}
    for m in models:
        result[m] = list_learned_configs(_BASE_FILE, m)
    return {"models": result}


@router.get("/learned/{model_name}")
async def get_learned_model_configs(model_name: str):
    return {"model": model_name, "configs": list_learned_configs(_BASE_FILE, model_name)}


@router.get("/learned/{model_name}/{agent}")
async def get_learned_agent_config(model_name: str, agent: str):
    learned = get_learned_config(_BASE_FILE, model_name, agent)
    base = get_base_config(_BASE_FILE, agent)
    return {
        "model": model_name,
        "agent": agent,
        "config": learned or base,
        "is_learned": learned is not None,
        "base": base,
    }


@router.post("/learned/{model_name}/{agent}")
async def save_learned_agent_config(model_name: str, agent: str, req: Request):
    data = await req.json()
    allowed = {"temperature", "max_tokens", "system_prompt_override", "notes"}
    config = {k: v for k, v in data.items() if k in allowed}
    if not config:
        return JSONResponse({"error": "No valid fields"}, status_code=400)
    save_learned_config(_BASE_FILE, model_name, agent, config)
    append_learning_log(_BASE_FILE, model_name, {
        "event": "manual_config_save",
        "agent": agent,
        "config": config,
    })
    return {"ok": True}


@router.delete("/learned/{model_name}/{agent}")
async def delete_learned_agent_config(model_name: str, agent: str):
    deleted = delete_learned_config(_BASE_FILE, model_name, agent)
    return {"ok": True, "deleted": deleted}


@router.delete("/learned/{model_name}")
async def reset_learned_model_ep(model_name: str):
    reset_learned_configs(_BASE_FILE, model_name)
    return {"ok": True}


@router.get("/effective/{model_name}/{agent}")
async def get_effective_agent_config(model_name: str, agent: str, use_learned: bool = True):
    config = get_effective_config(_BASE_FILE, model_name, agent, use_learned)
    return {"model": model_name, "agent": agent, "config": config, "use_learned": use_learned}


@router.get("/log/{model_name}")
async def get_learning_log(model_name: str, limit: int = 50):
    entries = read_learning_log(_BASE_FILE, model_name, limit)
    return {"model": model_name, "entries": entries, "count": len(entries)}


@router.get("/ratings/{model_name}")
async def get_ratings_summary(model_name: str):
    log = read_learning_log(_BASE_FILE, model_name, limit=200)
    ratings = [e for e in log if e.get("event") == "peer_rating"]
    if not ratings:
        return {"model": model_name, "count": 0, "avg_score": None, "by_agent": {}}
    by_agent = {}
    for r in ratings:
        agent = r.get("rated_agent", "?")
        if agent not in by_agent:
            by_agent[agent] = []
        by_agent[agent].append(r.get("score", 0.5))
    return {
        "model": model_name,
        "count": len(ratings),
        "avg_score": round(sum(r.get("score", 0.5) for r in ratings) / len(ratings), 3),
        "by_agent": {
            a: {"count": len(scores), "avg": round(sum(scores) / len(scores), 3), "last": scores[-1]}
            for a, scores in by_agent.items()
        }
    }


@router.post("/log/{model_name}")
async def add_learning_log_entry(model_name: str, req: Request):
    data = await req.json()
    append_learning_log(_BASE_FILE, model_name, data)
    return {"ok": True}


@router.delete("/log/{model_name}")
async def delete_learning_log_ep(model_name: str):
    clear_learning_log(_BASE_FILE, model_name)
    return {"ok": True}
