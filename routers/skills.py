"""Skills API-Router — Liste + Toggle (C-6, Phase 1)."""
import logging

from fastapi import APIRouter, Request

from hive_functions.skills import load_skills, set_skill_enabled

logger = logging.getLogger("hivemind.server")

router = APIRouter(prefix="/skills", tags=["Skills"])


def _workspace() -> str:
    try:
        from core.state import settings
        _ws = settings.get("workspace", "") or ""
    except Exception:
        _ws = ""
    if not _ws:
        import os
        _ws = os.environ.get("HIVEMIND_WORKSPACE", ".") or "."
    return _ws


@router.get("")
async def list_skills():
    ws = _workspace()
    skills = load_skills(ws, include_disabled=True)
    return {"workspace": ws, "skills": [
        {
            "name": s["name"],
            "description": s["description"],
            "trigger_keywords": s["trigger_keywords"],
            "trigger_paths": s["trigger_paths"],
            "priority": s["priority"],
            "enabled": s["enabled"],
            "source": s["source"],
            "version": s["version"],
            "path": s.get("_path", ""),
        }
        for s in skills
    ]}


@router.post("/toggle")
async def toggle_skill(req: Request):
    body = await req.json()
    name = str(body.get("name", "")).strip()
    enabled = bool(body.get("enabled", True))
    if not name:
        return {"ok": False, "reason": "name required"}
    ws = _workspace()
    skills = load_skills(ws, include_disabled=True)
    target = next((s for s in skills if s["name"] == name), None)
    if target is None:
        return {"ok": False, "reason": f"skill '{name}' not found"}
    path = target.get("_path", "")
    ok = set_skill_enabled(path, enabled) if path else False
    return {"ok": ok, "name": name, "enabled": enabled}
