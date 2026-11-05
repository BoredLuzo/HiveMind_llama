"""REST surface for engine-owned Automations (scheduled agent runs)."""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scheduler import spec
from scheduler.manager import get_manager

router = APIRouter(prefix="/automations", tags=["automations"])

_ALLOWED_MODES = {"auto", "simple", "pipeline", "code_duo"}
_ALLOWED_TIERS = {"off", "readonly", "python", "full"}
_ALLOWED_POLICY = {"skip", "queue", "replace"}


def _mgr():
    return get_manager()


def _err(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)


def _schedule_from(body: dict) -> dict | None:
    sched = body.get("schedule")
    if isinstance(sched, dict):
        return sched
    text = str(body.get("schedule_text") or "").strip()
    return spec.parse_schedule(text) if text else None


def _validate(body: dict) -> tuple[dict | None, str]:
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return None, "prompt is required"
    name = str(body.get("name") or "Automation").strip()[:80]
    schedule = _schedule_from(body)
    if not schedule:
        return None, "schedule is required (every/in/daily)"
    mode = str(body.get("mode") or "simple")
    if mode not in _ALLOWED_MODES:
        return None, f"unknown mode: {mode}"
    tier = str(body.get("tools_tier") or "readonly")
    if tier not in _ALLOWED_TIERS:
        return None, f"unknown tools_tier: {tier}"
    policy = str(body.get("policy") or "skip")
    if policy not in _ALLOWED_POLICY:
        return None, f"unknown policy: {policy}"
    job = {
        "name": name,
        "prompt": prompt[:8000],
        "schedule": schedule,
        "timezone": str(body.get("timezone") or ""),
        "mode": mode,
        "tools_tier": tier,
        "workspace": str(body.get("workspace") or ""),
        "policy": policy,
        "approvals": str(body.get("approvals") or "deny"),
        "delivery": str(body.get("delivery") or "ui"),
        "enabled": bool(body.get("enabled", True)),
        "max_runtime_s": int(body.get("max_runtime_s") or 1800),
    }
    job["next_ts"] = spec.next_fire_ts(
        schedule, time.time(), job["timezone"])
    if job["next_ts"] is None:
        return None, "schedule has no next fire time"
    return job, ""


@router.get("")
async def list_automations():
    mgr = _mgr()
    if mgr is None:
        return _err("scheduler not running", 503)
    jobs = mgr.store.jobs()
    return {"automations": jobs}


@router.post("")
async def create_automation(req: Request):
    mgr = _mgr()
    if mgr is None:
        return _err("scheduler not running", 503)
    try:
        body = await req.json()
    except ValueError:
        return _err("JSON body expected")
    if not isinstance(body, dict):
        return _err("JSON body must be an object")
    job, why = _validate(body)
    if job is None:
        return _err(why)
    created = mgr.store.add(job)
    return JSONResponse({"automation": created}, status_code=201)


@router.get("/{job_id}")
async def get_automation(job_id: str):
    mgr = _mgr()
    if mgr is None:
        return _err("scheduler not running", 503)
    job = mgr.store.get(job_id)
    return job if job is not None else _err("Not found", 404)


@router.put("/{job_id}")
async def update_automation(job_id: str, req: Request):
    mgr = _mgr()
    if mgr is None:
        return _err("scheduler not running", 503)
    try:
        body = await req.json()
    except ValueError:
        return _err("JSON body expected")
    if not isinstance(body, dict):
        return _err("JSON body must be an object")
    patch = dict(body)
    schedule = _schedule_from(body)
    if schedule is not None:
        patch["schedule"] = schedule
        patch["next_ts"] = spec.next_fire_ts(
            schedule, time.time(),
            str(body.get("timezone") or ""))
        patch["pending_fire"] = None
    updated = mgr.store.update(job_id, patch)
    return updated if updated is not None else _err("Not found", 404)


@router.delete("/{job_id}")
async def delete_automation(job_id: str):
    mgr = _mgr()
    if mgr is None:
        return _err("scheduler not running", 503)
    return ({"ok": True} if mgr.store.delete(job_id)
            else _err("Not found", 404))


@router.post("/{job_id}/run")
async def run_automation(job_id: str):
    mgr = _mgr()
    if mgr is None:
        return _err("scheduler not running", 503)
    ok, note = await mgr.run_now(job_id)
    return JSONResponse({"ok": ok, "note": note},
                        status_code=200 if ok else 409)


@router.post("/{job_id}/toggle")
async def toggle_automation(job_id: str):
    mgr = _mgr()
    if mgr is None:
        return _err("scheduler not running", 503)
    job = mgr.store.get(job_id)
    if job is None:
        return _err("Not found", 404)
    updated = mgr.store.update(job_id, {"enabled": not job.get("enabled", True)})
    return {"automation": updated}


@router.get("/{job_id}/runs")
async def automation_runs(job_id: str, limit: int = 20):
    mgr = _mgr()
    if mgr is None:
        return _err("scheduler not running", 503)
    return {"runs": mgr.store.history(job_id, max(1, min(100, limit)))}
