"""Soul API-Router."""
import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from hive_functions.soul_engine import (
    load_soul, save_soul, reset_soul, get_soul_summary,
    maybe_evolve_soul, resolve_reflection_model, _skills_dir,
    MIN_RUNS_FOR_EVOLUTION,
)
from hive_functions.prompts import HIVEMIND_SOUL
from settings import load_settings
from infra.run_counter import _load_run_counter
from infra.token_stats import _load_token_stats, _save_token_stats
import infra.token_stats as _ts
from model_configs import read_learning_log
import core.state as _state

logger = logging.getLogger("hivemind.server")

router = APIRouter(prefix="/soul", tags=["Soul"])

_BASE_FILE = Path(__file__).parent.parent / "server.py"


def _parse_md_field(text: str, field: str, strip: str = "") -> str:
    m = re.search(rf"\*\*{re.escape(field)}:\*\*[ \t]*([^\s*\n][^\n*]*)", text)
    val = (m.group(1) if m else "").strip()
    if strip:
        val = val.rstrip(strip).strip()
    return val


@router.get("/skills")
async def get_soul_skills():
    skills_dir = _skills_dir(_BASE_FILE)
    if not skills_dir.exists():
        return []

    result = []
    for f in sorted(skills_dir.glob("skill_*.md")):
        try:
            text = f.read_text(encoding="utf-8")
            relevance = float(_parse_md_field(text, "Relevanz") or 0)
            merge_count = int(_parse_md_field(text, "Gesehen", strip="x") or 1)
            source = _parse_md_field(text, "Quelle") or "?"
            paths = _parse_md_field(text, "Pfade") or ""
            lines = text.split("\n")
            insight = next((l for l in lines if l and not l.startswith("#") and not l.startswith("**")), "")
            result.append({
                "filename": f.name,
                "insight": insight.strip(),
                "relevance_score": relevance,
                "merge_count": merge_count,
                "source": source.strip(),
                "trigger_paths": [p.strip() for p in paths.split(",") if p.strip()],
            })
        except Exception:
            continue

    return result


@router.get("")
async def get_soul_ep():
    return get_soul_summary(_BASE_FILE)


@router.get("/status")
async def get_soul_status():
    soul = load_soul(_BASE_FILE)
    settings = load_settings()
    use_learned = settings.get("learning_preset_mode", False)
    learned = {
        "version": soul.get("version", 1),
        "run_count": soul.get("run_count", 0),
        "evolutions": soul.get("evolution_count", 0),
        "last_reason": soul.get("last_reason", ""),
    }
    return {"use_learned": use_learned, "learned": learned}


@router.get("/current")
async def get_soul_current():
    soul = load_soul(_BASE_FILE)
    settings = load_settings()
    use_learned = bool(settings.get("learning_preset_mode", False))
    learned_text = (soul.get("selbstverstaendnis") or "").strip() if use_learned else ""
    if use_learned and learned_text:
        return {"text": soul["selbstverstaendnis"], "source": "learned"}
    return {"text": HIVEMIND_SOUL, "source": "original"}


@router.get("/learned")
async def get_soul_learned_text():
    soul = load_soul(_BASE_FILE)
    text = soul.get("selbstverstaendnis", "")
    return {
        "text": text,
        "selbstverstaendnis": text,
        "version": soul.get("version", 1),
        "evolution_count": soul.get("evolution_count", 0),
        "last_evolved": soul.get("last_evolved", ""),
        "last_reason": soul.get("last_reason", ""),
    }


@router.post("/learned")
async def save_soul_learned(req: Request):
    try:
        body = await req.json()
    except (ValueError, RuntimeError):
        return JSONResponse({"ok": False, "error": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "Expected JSON object."}, status_code=400)
    raw_text = body.get("text")
    if raw_text is None:
        raw_text = body.get("selbstverstaendnis")
    text = str(raw_text or "").strip()
    if len(text) < 50:
        return JSONResponse(
            {"ok": False, "error": "Soul text too short (min. 50 characters)."},
            status_code=400,
        )
    reason = str(body.get("reason") or "").strip() or "Manually edited"

    soul = load_soul(_BASE_FILE)
    prev_text = soul.get("selbstverstaendnis", "")
    if prev_text:
        log = list(soul.get("evolution_log") or [])
        log.append({
            "timestamp": soul.get("last_evolved") or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "version": soul.get("version", 1),
            "reason": f"Replaced ({reason})",
            "text": prev_text[:500],
        })
        soul["evolution_log"] = log[-20:]

    soul["selbstverstaendnis"] = text
    soul["version"] = int(soul.get("version", 1) or 1) + 1
    soul["evolution_count"] = int(soul.get("evolution_count", 0) or 0) + 1
    soul["last_evolved"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    soul["last_reason"] = reason
    save_soul(_BASE_FILE, soul)
    return {"ok": True, "status": get_soul_summary(_BASE_FILE)}


@router.delete("/learned")
async def reset_soul_learned(req: Request = None):
    soul = load_soul(_BASE_FILE)
    current_text = soul.get("selbstverstaendnis", "")
    log = list(soul.get("evolution_log") or [])
    if current_text:
        log.append({
            "timestamp": soul.get("last_evolved") or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "version": soul.get("version", 1),
            "reason": "Manually reset via UI",
            "text": current_text[:500],
        })
        log = log[-20:]
    reset_soul(_BASE_FILE)
    if log:
        fresh = load_soul(_BASE_FILE)
        fresh["evolution_log"] = log
        save_soul(_BASE_FILE, fresh)
    return {"ok": True, "soul": get_soul_summary(_BASE_FILE)}


@router.post("/evolve")
async def trigger_soul_evolution_ep(req: Request):
    try:
        body = await req.json()
    except (ValueError, RuntimeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    force = bool(body.get("force", False))
    settings = load_settings()
    run_count = _load_run_counter()

    reflection_model = resolve_reflection_model(
        settings, _state.registry_get("direct"), require_enabled=False
    )
    if not reflection_model:
        return JSONResponse(
            {"ok": False, "error": "No direct or evolve reflection model configured."},
            status_code=400,
        )

    ollama_client = getattr(_state.pipeline, "ollama", None) if _state.pipeline else None
    if ollama_client is None:
        return JSONResponse(
            {"ok": False, "error": "Pipeline/Ollama client not initialized."},
            status_code=503,
        )

    if force:
        soul = load_soul(_BASE_FILE)
        last_run = int(soul.get("run_count", 0) or 0)
        # Ensure effective_runs strictly exceeds soul.run_count so maybe_evolve_soul
        # does not skip evolution via its `last_run >= total_runs` guard.
        effective_runs = max(run_count, last_run + 1, MIN_RUNS_FOR_EVOLUTION)
    else:
        effective_runs = max(run_count, MIN_RUNS_FOR_EVOLUTION)

    new_soul = await maybe_evolve_soul(
        base_path=_BASE_FILE,
        ollama_client=ollama_client,
        model=reflection_model,
        learning_log_reader=read_learning_log,
        registry_all_fn=_state.registry_all,
        total_runs=effective_runs,
    )

    if new_soul:
        return {"ok": True, "forced": force, "soul": get_soul_summary(_BASE_FILE)}
    return {"ok": False, "forced": force, "reason": "Evolution not possible (too little learning data for a meaningful evolution)"}


@router.get("/history")
async def get_soul_history(limit: int = 8):
    soul = load_soul(_BASE_FILE)
    _history = []
    for _e in soul.get("evolution_log", [])[-limit:]:
        _e = dict(_e)
        _e.setdefault("soul_preview", str(_e.get("text", "") or ""))
        _history.append(_e)
    return {"history": _history}


@router.get("/insights")
async def get_soul_insights():
    if not _state.pipeline or not _state.pipeline.memory:
        return {"insights": [], "total": 0}
    with _state.pipeline.memory._insight_lock:
        snapshot = list(_state.pipeline.memory._insights)
    snapshot.sort(key=lambda x: float(x.get("relevance_score", 0)), reverse=True)
    return {
        "total": len(snapshot),
        "insights": [
            {
                "insight": ins.get("insight", "")[:120],
                "relevance_score": round(float(ins.get("relevance_score", 0)), 3),
                "source": ins.get("source", "?"),
                "trigger_path": ins.get("trigger_path", ""),
                "merge_count": ins.get("merge_count", 1),
                "saved_at": ins.get("saved_at", ""),
            }
            for ins in snapshot[:50]
        ],
    }


@router.delete("/insights")
async def reset_soul_insights():
    if not _state.pipeline or not _state.pipeline.memory:
        return {"ok": False, "reason": "no_memory"}
    with _state.pipeline.memory._insight_lock:
        _state.pipeline.memory._insights.clear()
        _state.pipeline.memory._persist_insights()
    logger.info("[INSIGHTS] All insights reset via UI")
    return {"ok": True, "remaining": 0}


@router.get("/token-stats")
async def get_token_stats(days: int = 30):
    stats = _load_token_stats()
    daily = stats.get("daily", {})
    sorted_days = sorted(daily.keys(), reverse=True)[:days]
    limited_daily = {d: daily[d] for d in sorted_days}
    # Measured decode rates from the llama-server logs (deploy/backfill_speeds.py):
    # they let the model list show a speed even for records without phase timing.
    _speeds = (stats.get("speeds") or {})
    return {
        "total_tokens": stats.get("total_tokens", 0),
        "total_runs": stats.get("total_runs", 0),
        "total_prompt_tokens": stats.get("total_prompt_tokens", 0),
        "total_cached_tokens": stats.get("total_cached_tokens", 0),
        "total_requests": stats.get("total_requests", 0),
        "daily": limited_daily,
        # METRICS-EXPAND (2026-10-19): comparison per model+role, total and per day
        # ("Daily overview" rows can switch the table between total and one day)
        "by_model": _ts.aggregate_by_model(limited_daily, speeds=_speeds),
        "by_model_by_day": {d: _ts.aggregate_by_model(limited_daily, day=d, speeds=_speeds)
                            for d in sorted_days},
        # which routes actually ran (code_duo / direct / pipeline)
        "by_mode": _ts.aggregate_by_mode(limited_daily),
    }


@router.delete("/token-stats")
async def reset_token_stats():
    if _ts._token_stats_lock is None:
        return {"ok": False, "error": "token stats not initialized"}
    with _ts._token_stats_lock:
        _save_token_stats({"total_tokens": 0, "total_runs": 0, "daily": {}})
    return {"ok": True}
