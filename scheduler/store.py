"""Persistent job/history store for Automations.

The file lives next to the other engine state files (settings.json, chats).
Writes are atomic; the schema is versioned so future fields can be migrated.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from utils.file import write_json_atomic

SCHEMA_VERSION = 1
MAX_HISTORY = 200


def _blank() -> dict:
    return {"version": SCHEMA_VERSION, "jobs": [], "history": []}


def _normalise_job(raw: dict, now: float) -> dict | None:
    if not isinstance(raw, dict):
        return None
    prompt = str(raw.get("prompt") or "").strip()
    if not prompt:
        return None
    schedule = raw.get("schedule")
    if not isinstance(schedule, dict):
        return None
    job = {
        "id": str(raw.get("id") or "").strip(),
        "name": str(raw.get("name") or "Automation").strip()[:80],
        "prompt": prompt[:8000],
        "schedule": dict(schedule),
        "timezone": str(raw.get("timezone") or ""),
        "mode": str(raw.get("mode") or "auto"),
        "tools_tier": str(raw.get("tools_tier") or "readonly"),
        "workspace": str(raw.get("workspace") or ""),
        "chat_id": str(raw.get("chat_id") or ""),
        "enabled": bool(raw.get("enabled", True)),
        "policy": str(raw.get("policy") or "skip"),
        "approvals": str(raw.get("approvals") or "deny"),
        "delivery": str(raw.get("delivery") or "ui"),
        "max_runtime_s": int(raw.get("max_runtime_s") or 1800),
        "next_ts": raw.get("next_ts"),
        "fires": int(raw.get("fires") or 0),
        "last_fired_ts": raw.get("last_fired_ts"),
        "last_status": str(raw.get("last_status") or ""),
        "created_at": float(raw.get("created_at") or now),
        "updated_at": float(raw.get("updated_at") or now),
        "pending_fire": raw.get("pending_fire"),
    }
    return job


class AutomationStore:
    """Thread-safe load/save of jobs and a bounded history ring."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data = _blank()
        self.load()

    def load(self) -> dict:
        with self._lock:
            if not self.path.exists():
                self.data = _blank()
                return self.data
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # Keep the broken file for forensics; start clean.
                try:
                    self.path.replace(self.path.with_suffix(".corrupt.json"))
                except OSError:
                    pass
                self.data = _blank()
                return self.data
            if not isinstance(raw, dict):
                self.data = _blank()
                return self.data
            now = time.time()
            jobs = []
            for item in raw.get("jobs") or []:
                job = _normalise_job(item, now)
                if job is not None:
                    jobs.append(job)
            history = [h for h in (raw.get("history") or [])
                       if isinstance(h, dict)][-MAX_HISTORY:]
            self.data = {
                "version": int(raw.get("version") or SCHEMA_VERSION),
                "jobs": jobs,
                "history": history,
            }
            return self.data

    def save(self) -> None:
        with self._lock:
            self.data["version"] = SCHEMA_VERSION
            self.data["history"] = (self.data.get("history") or [])[-MAX_HISTORY:]
            last_exc = None
            for _ in range(4):
                try:
                    write_json_atomic(self.path, self.data)
                    return
                except PermissionError as exc:  # Windows reader holding the file
                    last_exc = exc
                    time.sleep(0.05)
            if last_exc is not None:
                raise last_exc

    # -- jobs -------------------------------------------------------------
    def jobs(self) -> list[dict]:
        with self._lock:
            return list(self.data.get("jobs") or [])

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            for job in self.data.get("jobs") or []:
                if job.get("id") == job_id:
                    return job
        return None

    def add(self, job: dict) -> dict:
        with self._lock:
            now = time.time()
            item = dict(job)
            if not item.get("id"):
                item["id"] = f"a{int(now)}_{len(self.data['jobs']) + 1}"
            item.setdefault("created_at", now)
            item["updated_at"] = now
            item = _normalise_job(item, now) or item
            self.data["jobs"].append(item)
            self.save()
            return item

    def update(self, job_id: str, patch: dict) -> dict | None:
        with self._lock:
            for i, job in enumerate(self.data.get("jobs") or []):
                if job.get("id") != job_id:
                    continue
                merged = dict(job)
                merged.update(patch or {})
                merged["id"] = job_id
                merged["updated_at"] = time.time()
                item = _normalise_job(merged, merged["updated_at"])
                if item is None:
                    return None
                self.data["jobs"][i] = item
                self.save()
                return item
        return None

    def delete(self, job_id: str) -> bool:
        with self._lock:
            jobs = self.data.get("jobs") or []
            for i, job in enumerate(jobs):
                if job.get("id") == job_id:
                    jobs.pop(i)
                    self.save()
                    return True
        return False

    # -- history ----------------------------------------------------------
    def append_history(self, entry: dict) -> None:
        with self._lock:
            self.data.setdefault("history", []).append(dict(entry))
            self.data["history"] = self.data["history"][-MAX_HISTORY:]
            self.save()

    def history(self, job_id: str = "", limit: int = 50) -> list[dict]:
        with self._lock:
            items = list(self.data.get("history") or [])
        if job_id:
            items = [h for h in items if h.get("job_id") == job_id]
        return items[-max(1, limit):]
