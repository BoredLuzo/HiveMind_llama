"""/cron proxy: the phone keeps its commands, the engine owns the schedule.

R8 (2026-11-05): the gateway no longer evaluates next_ts or fires runs
itself. Every /cron command proxies the engine's /automations API
(hive_client), and a one-time migration imports legacy gateway cron_jobs
into the engine store. Pure helpers stay testable here.
"""
from __future__ import annotations

import time

from hivemind_gateway.cron import parse_spec


def cron_help() -> str:
    return (
        "⏰ Automations — scheduled agent runs\n"
        "/cron add every 10m <prompt> — recurring\n"
        "/cron add in 30m <prompt> — one-shot\n"
        "/cron add at 09:00 <prompt> — daily at 09:00\n"
        "/cron list — show jobs\n"
        "/cron del <id> — delete a job\n"
        "/cron run <id> — fire a job now (test)")


def _kind_from_legacy(job: dict) -> dict | None:
    """Map a legacy gateway cron job to an engine schedule, or None."""
    kind = str(job.get("kind") or "")
    if kind == "every":
        return {"kind": "every", "interval_s": int(job.get("interval_s") or 600)}
    if kind in ("in", "once"):
        return {"kind": "once", "interval_s": int(job.get("interval_s") or 600)}
    if kind == "at":
        return {"kind": "daily", "at_hm": str(job.get("at_hm") or "09:00")}
    return None


def legacy_to_engine(job: dict) -> dict | None:
    """Legacy cron_jobs entry -> POST /automations body (or None to skip)."""
    prompt = str(job.get("prompt") or "").strip()
    schedule = _kind_from_legacy(job)
    if not prompt or schedule is None:
        return None
    return {
        "name": "Phone cron " + str(job.get("id") or "job"),
        "prompt": prompt[:8000],
        "schedule": schedule,
        "mode": "simple",
        "tools_tier": "readonly",
        "policy": "skip",
        "enabled": True,
    }


def job_line(job: dict) -> str:
    """One /cron list line in the familiar shape."""
    nxt = job.get("next_ts")
    when = time.strftime("%H:%M", time.localtime(float(nxt))) if nxt else "?"
    return (f"{job.get('id')} [{str((job.get('schedule') or {}).get('kind') or '?')}]"
            f" next {when} ({int(job.get('fires') or 0)} fires,"
            f" {str(job.get('last_status') or '-')}) : "
            f"{str(job.get('prompt'))[:60]}")


def spec_to_body(spec: dict) -> dict:
    """parse_spec output -> POST /automations body."""
    kind = str(spec.get("kind") or "")
    prompt = str(spec.get("prompt") or "").strip()
    if kind == "at":
        schedule = {"kind": "daily", "at_hm": str(spec.get("at_hm") or "09:00")}
    elif kind == "in":
        schedule = {"kind": "once", "interval_s": int(spec.get("interval_s") or 60)}
    else:
        schedule = {"kind": "every", "interval_s": int(spec.get("interval_s") or 600)}
    return {
        "name": "Phone cron",
        "prompt": prompt[:8000],
        "schedule": schedule,
        "mode": "simple",
        "tools_tier": "readonly",
        "policy": "skip",
        "enabled": True,
    }
