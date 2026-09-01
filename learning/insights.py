"""Insight-Extractor (aus server.py extrahiert)."""
from __future__ import annotations

import asyncio, json, logging, re

logger = logging.getLogger("hivemind.insights")

_bg_insight_sem: asyncio.Semaphore | None = None
_registry_get: callable | None = None
_pipeline: object | None = None
_memory: object | None = None

def init_insights(insight_sem=None, registry_get_fn=None, pipeline_obj=None, memory_obj=None):
    global _bg_insight_sem, _registry_get, _pipeline, _memory
    if insight_sem is not None:
        _bg_insight_sem = insight_sem
    if registry_get_fn:
        _registry_get = registry_get_fn
    if pipeline_obj:
        _pipeline = pipeline_obj
    if memory_obj:
        _memory = memory_obj

async def _run_insight_extractor(
    task: str,
    written_files: list[str],
    critic_verdict: str,
    critic_issues: list[str],
    workspace: str,
):
    """Extract structured insights from a completed agentic loop and save them.

    Called as asyncio.create_task() after the Duo loop completes.
    Uses the smallest available model to minimize VRAM impact.
    """
    if not written_files or not task:
        return
    # Use own semaphore — independent from peer ratings
    if _bg_insight_sem.locked():
        return

    try:
        from hive_functions.prompts import INSIGHT_EXTRACTOR
    except ImportError:
        return

    # Build context for the insight extractor
    _files_str = "\n".join(f"  - {f}" for f in written_files[:20])
    _issues_str = "; ".join(critic_issues[:5]) if critic_issues else "none"
    _verdict_str = (critic_verdict or "completed")[:200]

    _user_msg = (
        f"Task: {task[:500]}\n\n"
        f"Files changed:\n{_files_str}\n\n"
        f"Critic verdict: {_verdict_str}\n"
        f"Issues: {_issues_str}\n"
    )

    # Use the smallest model available (judge or analyst) for low VRAM impact
    _extractor_model = _registry_get("judge") or _registry_get("analyst") or _registry_get("refiner")
    if not _extractor_model:
        return

    async with _bg_insight_sem:
        try:
            _messages = [
                {"role": "system", "content": INSIGHT_EXTRACTOR},
                {"role": "user", "content": _user_msg},
            ]
            _raw = await asyncio.wait_for(
                _pipeline.ollama.chat(
                    model=_extractor_model,
                    messages=_messages,
                    temperature=0.2,
                    max_tokens=800,
                ),
                timeout=30.0,
            )
            _raw = _raw.strip()
            if not _raw:
                return

            # Parse JSON array from response
            # Try direct parse first, then extract from markdown
            _insights_data = None
            try:
                _insights_data = json.loads(_raw)
            except json.JSONDecodeError:
                _jm = re.search(r'\[[\s\S]*?\]', _raw)
                if _jm:
                    try:
                        _insights_data = json.loads(_jm.group())
                    except json.JSONDecodeError:
                        pass

            if not isinstance(_insights_data, list) or not _insights_data:
                return

            # Save each valid insight
            _saved_count = 0
            for _ie in _insights_data:
                if not isinstance(_ie, dict):
                    continue
                _insight_text = str(_ie.get("insight", "")).strip()
                if not _insight_text or len(_insight_text) < 10:
                    continue
                _trigger_path = str(_ie.get("trigger_path", "")).strip()
                _source = f"insight_extractor:{_ie.get('type', 'unknown')}"
                _confidence = float(_ie.get("confidence", 0.5))

                # Only save insights with reasonable confidence.
                # Threshold 0.5 matches the INSIGHT_EXTRACTOR prompt spec (0.5-1.0).
                # Lower values would bypass the Grace Period (which requires score >= 0.5).
                if _confidence < 0.5:
                    continue

                # BUG-4 FIX: Seed relevance_score with extractor confidence
                # instead of always 1.0 — low-confidence insights decay faster.
                _saved = _memory.remember_repo_insight(
                    _insight_text[:400],
                    trigger_path=_trigger_path,
                    source=_source[:40],
                    relevance_score=_confidence,
                )
                if _saved:
                    _saved_count += 1

            if _saved_count:
                logger.info("[InsightExtractor] Saved %d structured insights from agentic loop (model=%s)",
                            _saved_count, _extractor_model)

        except asyncio.TimeoutError:
            logger.debug("[InsightExtractor] Timeout (30s) — skipping")
        except Exception as _ie_err:
            logger.warning("[InsightExtractor] Failed: %s", _ie_err)


def _slugify(text: str) -> str:
    import re as _re
    _slug = _re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return _slug[:40]


def _parse_skill_json(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _jm = re.search(r"\{[\s\S]*?\}", raw)
        if _jm:
            try:
                data = json.loads(_jm.group())
            except json.JSONDecodeError:
                return None
        else:
            return None
    if not isinstance(data, dict):
        return None
    return data


async def _run_skill_distillation(workspace: str):


    if not _memory or not _pipeline or not _registry_get:
        return
    from hive_functions.skills import select_skill_candidates, skill_file_exists, write_skill_md
    try:
        from hive_functions.prompts import SKILL_DISTILLER
    except ImportError:
        return

    try:
        _lock = getattr(_memory, "_insight_lock", None)
        if _lock:
            _lock.acquire()
        try:
            _insights = list(getattr(_memory, "_insights", []) or [])
        finally:
            if _lock:
                _lock.release()
    except Exception:
        _insights = list(getattr(_memory, "_insights", []) or [])

    _candidates = select_skill_candidates(_insights, max_candidates=3)
    if not _candidates:
        return

    _distiller_model = _registry_get("judge") or _registry_get("analyst") or _registry_get("refiner")
    if not _distiller_model:
        return

    for _cand in _candidates:
        _slug = _slugify(str(_cand.get("insight", "")) or "")
        if not _slug:
            continue
        if skill_file_exists(workspace, _slug):
            _mark_insight_distilled(_cand, _slug)
            continue

        _paths = [str(p) for p in (_cand.get("trigger_paths") or [_cand.get("trigger_path", "")]) if str(p).strip()]
        _user_msg = (
            f"Insight: {_cand.get('insight', '')}\n"
            f"Trigger paths: {', '.join(_paths) if _paths else '(none)'}\n"
        )
        try:
            _raw = await asyncio.wait_for(
                _pipeline.ollama.chat(
                    model=_distiller_model,
                    messages=[
                        {"role": "system", "content": SKILL_DISTILLER},
                        {"role": "user", "content": _user_msg},
                    ],
                    temperature=0.2,
                    max_tokens=600,
                ),
                timeout=30.0,
            )
        except Exception as _sd_err:
            logger.debug("[SkillDistillation] LLM failed: %s", _sd_err)
            continue

        _skill = _parse_skill_json(_raw)
        if _skill is None:
            continue
        if not str(_skill.get("name", "")).strip():
            _skill["name"] = _slug
        _skill["source"] = "distilled"
        _skill["enabled"] = False
        _skill["version"] = "1.0.0"

        _ok = write_skill_md(workspace, _skill)
        if _ok:
            _name = str(_skill["name"]).strip()
            _mark_insight_distilled(_cand, _name)
            logger.info("[SkillDistillation] Skill '%s' aus Insight destilliert (enabled=false)", _name)
            return  # max 1 Skill pro Run


def _mark_insight_distilled(insight: dict, skill_name: str) -> None:
    """Markiert das Insight-Objekt als destilliert (verhindert Re-Destillation)."""
    try:
        if not _memory:
            return
        insight["distilled_skill"] = skill_name
        _persist = getattr(_memory, "_persist_insights", None)
        if _persist:
            _persist()
    except Exception:
        pass

# ── Runtime VRAM Planner Override ─────────────────────────────


