"""Soul API-Router."""
import logging
from pathlib import Path

from fastapi import APIRouter, Request

from hive_functions.soul_engine import (
    load_soul, save_soul, reset_soul, get_soul_summary,
    maybe_evolve_soul, _skills_dir, MIN_RUNS_FOR_EVOLUTION,
    EVOLUTION_INTERVAL_RUNS,
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
    m = __import__("re").search(rf"\*\*{__import__('re').escape(field)}:\*\*\s*([^\s*][^\n*]*)", text)
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
    use_learned = settings.get("learning_preset_mode", False)
    text = soul.get("selbstverstaendnis", HIVEMIND_SOUL) if use_learned else HIVEMIND_SOUL
    return {"text": text, "source": "learned" if use_learned else "original"}


@router.get("/learned")
async def get_soul_learned_text():
    soul = load_soul(_BASE_FILE)
    return {
        "selbstverstaendnis": soul.get("selbstverstaendnis", ""),
        "version": soul.get("version", 1),
        "evolution_count": soul.get("evolution_count", 0),
        "last_evolved": soul.get("last_evolved", ""),
    }


@router.delete("/learned")
async def reset_soul_learned(req: Request):
    soul = load_soul(_BASE_FILE)
    current_text = soul.get("selbstverstaendnis", "")
    if current_text:
        log = soul.get("evolution_log", [])
        log.append({
            "timestamp": soul.get("last_evolved", "manual-reset"),
            "version": soul.get("version", 1),
            "reason": "Manually reset via UI",
            "text": current_text[:500],
        })
        soul["evolution_log"] = log[-20:]
        save_soul(_BASE_FILE, soul)
    reset_soul(_BASE_FILE)
    return {"ok": True, "soul": get_soul_summary(_BASE_FILE)}


@router.post("/evolve")
async def trigger_soul_evolution_ep(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    force = bool(body.get("force", False))
    settings = load_settings()
    run_count = _load_run_counter()

    _sea_manual = settings.get("soul_evolve_agent", "direct")
    if isinstance(_sea_manual, dict) and _sea_manual.get("enabled") and (_sea_manual.get("model") or "").strip():
        reflection_model = _sea_manual["model"].strip()
    else:
        reflection_model = _state.registry_get("direct")

    effective_runs = max(run_count, MIN_RUNS_FOR_EVOLUTION * 10) if force else max(run_count, MIN_RUNS_FOR_EVOLUTION)

    if force:
        soul = load_soul(_BASE_FILE)
        soul["run_count"] = max(effective_runs, MIN_RUNS_FOR_EVOLUTION + EVOLUTION_INTERVAL_RUNS)
        save_soul(_BASE_FILE, soul)

    new_soul = await maybe_evolve_soul(
        base_path=_BASE_FILE,
        ollama_client=_state.pipeline.ollama,
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
    return {
        "total_tokens": stats.get("total_tokens", 0),
        "total_runs": stats.get("total_runs", 0),
        "total_prompt_tokens": stats.get("total_prompt_tokens", 0),
        "total_cached_tokens": stats.get("total_cached_tokens", 0),
        "total_requests": stats.get("total_requests", 0),
        "daily": limited_daily,
    }


@router.delete("/token-stats")
async def reset_token_stats():
    if _ts._token_stats_lock is None:
        return {"ok": False, "error": "token stats not initialized"}
    with _ts._token_stats_lock:
        _save_token_stats({"total_tokens": 0, "total_runs": 0, "daily": {}})
    return {"ok": True}
