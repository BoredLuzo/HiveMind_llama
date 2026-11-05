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
_ALLOWED_POLICY = {"skip", "queue"}

# UPDATE-WHITELIST (2026-11-05 audit): PUT must not be able to rewrite
# bookkeeping (fires/next_ts/chat_id/pending_fire) - that was both a
# correctness hole and a way to wedge the scheduler with junk values.
_PATCHABLE = {"name", "prompt", "schedule", "timezone", "mode",
              "tools_tier", "workspace", "policy", "enabled",
              "max_runtime_s"}


def _mgr():
    return get_manager()


def _err(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)


def _schedule_from(body: dict) -> dict | None:
    sched = body.get("schedule")
    if isinstance(sched, dict):
        kind = str(sched.get("kind") or "")
        if kind == "daily":
            hm = str(sched.get("at_hm") or "")
            if spec.parse_schedule(f"daily {hm}") is None:
                return None
            out = {"kind": "daily", "at_hm": hm}
            days = sched.get("days")
            if isinstance(days, list) and days:
                try:
                    clean = sorted({int(d) for d in days
                                    if isinstance(d, (int, float))
                                    and 0 <= int(d) <= 6})
                except (TypeError, ValueError):
                    return None
                if not clean:
                    return None
                out["days"] = clean
            return out
        if kind in ("every", "once"):
            try:
                interval = int(sched.get("interval_s") or 0)
            except (TypeError, ValueError):
                return None
            if interval <= 0:
                return None
            return {"kind": kind, "interval_s": interval}
        return None
    text = str(body.get("schedule_text") or "").strip()
    return spec.parse_schedule(text) if text else None


def _validate(body: dict) -> tuple[dict | None, str]:
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return None, "prompt is required"
    name = str(body.get("name") or "Automation").strip()[:80] or "Automation"
    schedule = _schedule_from(body)
    if not schedule:
        return None, "schedule is required (every/once in minutes, daily HH:MM)"
    mode = str(body.get("mode") or "simple")
    if mode not in _ALLOWED_MODES:
        return None, f"unknown mode: {mode}"
    tier = str(body.get("tools_tier") or "readonly")
    if tier not in _ALLOWED_TIERS:
        return None, f"unknown tools_tier: {tier}"
    policy = str(body.get("policy") or "skip")
    if policy not in _ALLOWED_POLICY:
        return None, f"unknown policy: {policy}"
    try:
        max_runtime_s = int(body.get("max_runtime_s") or 1800)
    except (TypeError, ValueError):
        return None, "max_runtime_s must be a number"
    max_runtime_s = max(60, min(7200, max_runtime_s))
    job = {
        "name": name,
        "prompt": prompt[:8000],
        "schedule": schedule,
        "timezone": str(body.get("timezone") or ""),
        "mode": mode,
        "tools_tier": tier,
        "workspace": str(body.get("workspace") or ""),
        "policy": policy,
        "enabled": bool(body.get("enabled", True)),
        "max_runtime_s": max_runtime_s,
    }
    job["next_ts"] = spec.next_fire_ts(schedule, time.time(),
                                       job["timezone"])
    if job["next_ts"] is None:
        return None, "schedule has no next fire time"
    return job, ""


@router.get("")
async def list_automations():
    mgr = _mgr()
    if mgr is None:
        return _err("scheduler not running", 503)
    return {"automations": mgr.store.jobs()}


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
    try:
        created = mgr.store.add(job)
    except ValueError as exc:
        return _err(str(exc))
    return JSONResponse(created, status_code=201)


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
    current = mgr.store.get(job_id)
    if current is None:
        return _err("Not found", 404)
    # Same validation as POST, on the MERGED job - a partial patch can
    # never smuggle in an unknown mode/tier/policy or a raw schedule dict
    # that would wedge next_fire_ts.
    merged = {**current, **{k: v for k, v in body.items()
                            if k in _PATCHABLE}}
    job, why = _validate(merged)
    if job is None:
        return _err(why)
    job["id"] = job_id
    job["chat_id"] = current.get("chat_id") or ""
    job["created_at"] = current.get("created_at")
    job["fires"] = current.get("fires") or 0
    job["last_fired_ts"] = current.get("last_fired_ts")
    updated = mgr.store.update(job_id, job)
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
    ok, note = mgr.run_now(job_id)
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
    # A queued fire on a now-disabled job must not run later.
    updated = mgr.store.update(job_id, {
        "enabled": not job.get("enabled", True),
        "pending_fire": None})
    return updated


@router.post("/preview")
async def preview_schedule(req: Request):
    """Next 3 fire times for a schedule draft - the editor's live preview."""
    try:
        body = await req.json()
    except ValueError:
        return _err("JSON body expected")
    if not isinstance(body, dict):
        return _err("JSON body must be an object")
    schedule = _schedule_from(body)
    if schedule is None:
        return _err("unparseable schedule")
    tz_name = str(body.get("timezone") or "")
    now = time.time()
    out: list[float] = []
    cursor = float(now)
    slot = schedule
    for _ in range(3):
        nxt = spec.next_fire_ts(slot, cursor, tz_name)
        if nxt is None:
            break
        out.append(nxt)
        if str(schedule.get("kind")) == "every":
            cursor = nxt + max(60, int(schedule.get("interval_s") or 60))
        elif str(schedule.get("kind")) == "daily":
            cursor = nxt + 1
        else:
            break
    return {"next": out[:3]}


@router.get("/{job_id}/runs")
async def automation_runs(job_id: str, limit: int = 20):
    mgr = _mgr()
    if mgr is None:
        return _err("scheduler not running", 503)
    return {"runs": mgr.store.history(job_id, max(1, min(100, limit)))}
