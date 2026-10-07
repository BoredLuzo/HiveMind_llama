"""R7 (owner, 1.3.2 finale): scheduled agent runs — cron for the phone.

Jobs live in the gateway state, fire as NORMAL phone runs (result
relays to the phone, transcript persists to the web UI), and the
prompt is free text — e.g. "lies die Datei X im Workspace und arbeite
an den Resten des letzten Laufs weiter" continues a previous agent's
work.

Spec formats (all local time):
  /cron every 10m <prompt>   recurring, every 10 minutes
  /cron every 2h <prompt>    recurring, every 2 hours
  /cron in 30m <prompt>      one-shot, in 30 minutes
  /cron at 09:00 <prompt>    recurring daily at 09:00

Pure helpers here are unit-testable; the fire itself calls
gw.bridge.start_text_run(prompt) as a normal phone run.
"""
from __future__ import annotations

import re
import time

MAX_JOBS = 20

_RE_EVERY = re.compile(r"every\s+(\d+)\s*([smhd])\b", re.I)
_RE_IN = re.compile(r"in\s+(\d+)\s*([smhd])\b", re.I)
_RE_AT = re.compile(r"at\s+(\d{1,2}):(\d{2})\b", re.I)
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_spec(arg: str) -> dict | None:
    """Parse a /cron add spec -> {'kind', 'interval_s'|'at_hm', 'prompt'}.
    None = unparseable. Prompt must be non-empty."""
    if not arg:
        return None
    arg = arg.strip()
    _me = _RE_EVERY.match(arg)
    if _me:
        n, u = int(_me.group(1)), _me.group(2).lower()
        interval = max(60, n * _UNIT_S[u])
        prompt = arg[_me.end():].strip()
        if not prompt:
            return None
        return {"kind": "every", "interval_s": interval, "prompt": prompt}
    _mi = _RE_IN.match(arg)
    if _mi:
        n, u = int(_mi.group(1)), _mi.group(2).lower()
        interval = max(60, n * _UNIT_S[u])
        prompt = arg[_mi.end():].strip()
        if not prompt:
            return None
        return {"kind": "in", "interval_s": interval, "prompt": prompt}
    _ma = _RE_AT.match(arg)
    if _ma:
        hh, mm = int(_ma.group(1)), int(_ma.group(2))
        if hh > 23 or mm > 59:
            return None
        prompt = arg[_ma.end():].strip()
        if not prompt:
            return None
        return {"kind": "at", "at_hm": f"{hh:02d}:{mm:02d}", "prompt": prompt}
    return None


def cron_help() -> str:
    return (
        "⏰ Cron — scheduled agent runs\n"
        "/cron add every 10m <prompt> — recurring\n"
        "/cron add in 30m <prompt> — one-shot\n"
        "/cron add at 09:00 <prompt> — daily at 09:00\n"
        "/cron list — show jobs\n"
        "/cron del <id> — delete a job\n"
        "/cron run <id> — fire a job now (test)")


def next_fire_ts(job: dict, now: float) -> float | None:
    """Next fire timestamp for a job (local-time epoch seconds).
    'at'-jobs: next occurrence of HH:MM after now. None = never."""
    kind = job.get("kind")
    if kind in ("every", "in", "once"):
        return float(now + int(job.get("interval_s") or 0))
    if kind == "at":
        import datetime as _dt
        hh, mm = str(job.get("at_hm") or "00:00").split(":")[:2]
        target = _dt.datetime.now().replace(hour=int(hh), minute=int(mm),
                                            second=0, microsecond=0)
        ts = target.timestamp()
        if ts <= now:
            ts += 86400
        return ts
    return None


class CronManager:
    """Owns the job list (persisted in gateway state) and fires due jobs
    as normal phone runs. The scheduler loop lives in main.py."""

    def __init__(self, state, bridge):
        self._state = state
        self._bridge = bridge
        jobs = state.data.get("cron_jobs")
        self._jobs: list[dict] = jobs if isinstance(jobs, list) else []

    # -- persistence -------------------------------------------------------
    def _save(self) -> None:
        self._state.data["cron_jobs"] = self._jobs
        self._state.save()

    # -- commands ----------------------------------------------------------
    def jobs(self) -> list[dict]:
        return list(self._jobs)

    def add(self, job: dict) -> tuple[bool, str]:
        if len(self._jobs) >= MAX_JOBS:
            return False, f"❌ Job limit reached ({MAX_JOBS}) — /cron del first."
        job = dict(job)
        job["id"] = f"j{int(time.time()) % 100000}_{len(self._jobs)}"
        job["fires"] = 0
        if job.get("kind") == "in":
            job["kind"] = "once"
        nxt = next_fire_ts(job, time.time())
        if nxt is None:
            return False, "❌ unparseable spec"
        job["next_ts"] = nxt
        self._jobs.append(job)
        self._save()
        nxt = job.get("next_ts")
        when = (time.strftime("%H:%M", time.localtime(nxt))
                if nxt else "?")
        return True, (f"⏰ Job {job['id']} added ({job['kind']}) — first "
                      f"fire ~{when}: {job['prompt'][:80]}")

    def delete(self, job_id: str) -> tuple[bool, str]:
        for i, job in enumerate(self._jobs):
            if job.get("id") == job_id or job.get("id", "").startswith(job_id):
                self._jobs.pop(i)
                self._save()
                return True, f"🗑 Job {job_id} deleted."
        return False, f"❌ No job {job_id}."

    def get(self, job_id: str) -> dict | None:
        for job in self._jobs:
            if job.get("id") == job_id or job.get("id", "").startswith(job_id):
                return job
        return None

    # -- firing ------------------------------------------------------------
    def due_jobs(self, now: float) -> list[dict]:
        return [j for j in self._jobs
                if j.get("next_ts") is not None and now >= j["next_ts"]]

    def mark_fired(self, job: dict, now: float) -> None:
        job["fires"] = int(job.get("fires") or 0) + 1
        job["last_fired"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if job.get("kind") == "once":
            self._jobs = [j for j in self._jobs if j is not job]
        elif job.get("kind") == "every":
            job["next_ts"] = now + int(job.get("interval_s") or 0)
        elif job.get("kind") == "at":
            # R5-fix (audit): the old pop left the daily job dead — nothing
            # ever re-computed next_ts. Schedule tomorrow's occurrence.
            job["next_ts"] = next_fire_ts(job, now + 60)
        self._save()
