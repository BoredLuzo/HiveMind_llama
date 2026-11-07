"""Engine-owned Automations scheduler.

Jobs fire as normal engine runs through the loopback `/stream` endpoint so
the run journal, chat transcript and UI behave exactly like an interactive
run. One scheduler lane: a fire never starts while another run is live.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from scheduler import spec
from scheduler.store import AutomationStore

logger = logging.getLogger("hivemind.scheduler")

TICK_S = 5.0
MISSED_GRACE_S = 600.0
MAX_PENDING_S = 1800.0
DEFAULT_RUNTIME_S = 1800.0


def _now() -> float:
    return time.time()


class SchedulerManager:
    def __init__(self, store_path: str | Path, base_url: str):
        self.store = AutomationStore(store_path)
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._fire_tasks: set[asyncio.Task] = set()
        self._active_run_id = ""
        self._last_ask_preview = ""
        self._fire_lock = asyncio.Lock()

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_loop(),
                                             name="automations")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # SHUTDOWN-ABORT (2026-11-05 audit): an in-flight automation run
        # would survive as a detached engine run with nobody watching it.
        if self._active_run_id:
            try:
                await self._http().post(f"/abort/{self._active_run_id}",
                                        json={})
                logger.warning("[AUTOMATION] aborted in-flight run %s on "
                               "shutdown", self._active_run_id)
            except (httpx.HTTPError, OSError):
                pass
        for task in list(self._fire_tasks):
            task.cancel()
        if self._fire_tasks:
            await asyncio.gather(*self._fire_tasks, return_exceptions=True)
        self._fire_tasks.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(600.0, connect=10.0),
            )
        return self._client

    async def run_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except (RuntimeError, ValueError, OSError, AttributeError,
                    TypeError, KeyError) as exc:
                logger.warning("[AUTOMATION] tick failed: %s: %s",
                               type(exc).__name__, exc)
            await asyncio.sleep(TICK_S)

    # -- lane -------------------------------------------------------------
    @staticmethod
    def _live_run_ids() -> list[str]:
        from infra.run_control import live_run_ids
        return live_run_ids()

    def lane_busy(self) -> bool:
        return bool(self._live_run_ids())

    # -- tick -------------------------------------------------------------
    async def tick(self, now: float | None = None) -> None:
        now = _now() if now is None else float(now)
        jobs = self.store.jobs()
        # A queued fire waits for the lane; one slot per job.
        pending = [j for j in jobs
                   if j.get("pending_fire") and j.get("enabled", True)]
        if pending and not self.lane_busy():
            job = min(pending, key=lambda j: float(
                (j.get("pending_fire") or {}).get("due_ts") or 0))
            p = job.get("pending_fire") or {}
            if now - float(p.get("queued_ts") or now) > MAX_PENDING_S:
                self.store.update(job.get("id"), {
                    "pending_fire": None,
                    "next_ts": spec.advance_fire_ts(
                        job.get("schedule") or {},
                        float(job.get("next_ts") or now),
                        now, job.get("timezone") or ""),
                    "last_status": "skipped_stale"})
                self.store.append_history({
                    "job_id": job.get("id"), "status": "skipped_stale",
                    "queued_ts": p.get("queued_ts"), "ts": now})
            else:
                self.store.update(job.get("id"), {"pending_fire": None})
                self._spawn_fire(job, now, reason="queued")
            return

        for job in jobs:
            if not job.get("enabled", True):
                continue
            if job.get("pending_fire"):
                continue  # already waiting for the lane; not skipped_busy
            schedule = job.get("schedule") or {}
            # ZOMBIE-ONCE (2026-11-05 audit): a once job whose slot was
            # missed while the engine was down has next_ts=None forever.
            if (str(schedule.get("kind") or "") == "once"
                    and job.get("next_ts") is None):
                self.store.delete(job.get("id"))
                self.store.append_history({
                    "job_id": job.get("id"), "status": "expired", "ts": now})
                continue
            due = spec.classify_due(job.get("next_ts"), now, MISSED_GRACE_S)
            if due == "not_due":
                continue
            if due == "missed":
                self.store.update(job.get("id"), {
                    "next_ts": spec.advance_fire_ts(
                        schedule, float(job.get("next_ts") or now), now,
                        job.get("timezone") or ""),
                    "last_status": "missed"})
                self.store.append_history({
                    "job_id": job.get("id"), "status": "missed", "ts": now})
                continue
            if self.lane_busy():
                policy = str(job.get("policy") or "skip")
                if policy == "queue" and not job.get("pending_fire"):
                    self.store.update(job.get("id"), {
                        "pending_fire": {"due_ts": job.get("next_ts"),
                                         "queued_ts": now},
                        "last_status": "queued"})
                else:
                    self.store.update(job.get("id"), {
                        "next_ts": spec.advance_fire_ts(
                            schedule, float(job.get("next_ts") or now), now,
                            job.get("timezone") or ""),
                        "last_status": "skipped_busy"})
                    self.store.append_history({
                        "job_id": job.get("id"), "status": "skipped_busy",
                        "ts": now})
                return
            self._spawn_fire(job, now, reason="due")
            return

    # -- run-now ----------------------------------------------------------
    def run_now(self, job_id: str) -> tuple[bool, str]:
        job = self.store.get(job_id)
        if job is None:
            return False, "not found"
        if self.lane_busy():
            return False, "busy: another run is active"
        # SPAWN-HELPER (2026-11-05 audit): raw create_task loses the only
        # strong reference when this method returns - utils.bg_tasks.spawn
        # exists for exactly that. Fire in the background: a run can take
        # minutes and the HTTP caller (UI button) must not block on it.
        from utils.bg_tasks import spawn
        spawn(self._fire(job, _now(), reason="manual"),
              name=f"automation-{job_id}")
        return True, "started"

    # -- firing -----------------------------------------------------------
    def _spawn_fire(self, job: dict, now: float, reason: str) -> None:
        from utils.bg_tasks import spawn
        t = spawn(self._fire(job, now, reason=reason),
                  name=f"automation-{job.get('id')}")
        self._fire_tasks.add(t)
        t.add_done_callback(self._fire_tasks.discard)

    async def _fire(self, job: dict, now: float, reason: str) -> None:
        async with self._fire_lock:
            if self.lane_busy():
                # Lost the race (run_now vs tick): say so instead of a
                # silent drop.
                self.store.update(job.get("id"), {
                    "last_status": "skipped_busy"})
                self.store.append_history({
                    "job_id": job.get("id"), "status": "skipped_busy",
                    "ts": now, "reason": reason})
                return
            job_id = str(job.get("id") or "")
            schedule = job.get("schedule") or {}
            started = _now()
            chat_id = await self._ensure_chat(job)
            if not chat_id:
                self.store.update(job_id, {"last_status": "failed_chat"})
                self.store.append_history({
                    "job_id": job_id, "status": "failed_chat",
                    "ts": started})
                return
            prompt = str(job.get("prompt") or "")
            await self._append_turn(chat_id, "user", prompt, job)
            # Persist the slot before the run: a crash must not refire it.
            nxt = spec.advance_fire_ts(schedule, float(job.get("next_ts") or now),
                                       now, job.get("timezone") or "")
            self.store.update(job_id, {
                "fires": int(job.get("fires") or 0) + 1,
                "last_fired_ts": started,
                "next_ts": nxt})
            self._active_run_id = ""
            body: dict[str, Any] = {
                "q": prompt,
                "chat_id": chat_id,
                "mode": str(job.get("mode") or "simple"),
                "direct_tools_enabled": str(job.get("tools_tier") or "readonly") != "off",
                "direct_tools_tier": str(job.get("tools_tier") or "readonly"),
            }
            ws = str(job.get("workspace") or "")
            if ws:
                body["workspace"] = ws
            answer, stop_reason, run_id = await self._run_stream(
                body, int(float(job.get("max_runtime_s") or DEFAULT_RUNTIME_S)))
            self._active_run_id = ""
            if answer:
                await self._append_turn(chat_id, "assistant", answer, job)
            else:
                await self._append_turn(
                    chat_id, "assistant",
                    f"✅ Automation finished ({stop_reason}).", job)
            status = "ok" if stop_reason == "completed" else (
                "partial" if stop_reason in ("loop_detected", "max_tool_rounds")
                else "failed")
            if self._last_ask_preview and status != "ok":
                # The run paused on a question/card at least once - the
                # honest outcome is needs_input, not a bare failed.
                status = "needs_input"
            # RE-FETCH (2026-11-05 audit): `job` may be a stale dict if the
            # user edited/deleted the job during a long run. Mutate through
            # the store, and decide once-deletion from the STORED schedule.
            current = self.store.get(job_id)
            if current is None:
                self.store.append_history({
                    "job_id": job_id, "run_id": run_id, "chat_id": chat_id,
                    "status": status, "stop_reason": stop_reason,
                    "started": started, "finished": _now(),
                    "reason": reason})
                return
            self.store.update(job_id, {"last_status": status})
            self.store.append_history({
                "job_id": job_id, "run_id": run_id, "chat_id": chat_id,
                "status": status, "stop_reason": stop_reason,
                "started": started, "finished": _now(),
                "reason": reason})
            if (str((current.get("schedule") or {}).get("kind") or "")
                    == "once"):
                self.store.delete(job_id)

    async def _run_stream(self, body: dict, max_runtime_s: int
                          ) -> tuple[str, str, str]:
        answer_parts: list[str] = []
        run_id = ""
        stop_reason = ""
        client = self._http()

        async def _consume() -> None:
            nonlocal run_id, stop_reason
            async with client.stream("POST", "/stream", json=body) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except ValueError:
                        continue
                    etype = ev.get("type")
                    if etype == "run_id" and ev.get("run_id"):
                        run_id = str(ev["run_id"])
                        self._active_run_id = run_id
                    elif etype == "token":
                        answer_parts.append(str(ev.get("content") or ""))
                    elif etype in ("agent_asking", "approval_request"):
                        # NEEDS-INPUT (2026-11-05): an unattended run paused
                        # on a question/card - surface it instead of letting
                        # the run silently burn its runtime.
                        _q = str(ev.get("question") or ev.get("preview")
                                 or "")[:200]
                        if _q and _q != self._last_ask_preview:
                            self._last_ask_preview = _q
                            logger.warning(
                                "[AUTOMATION] %s needs input: %s",
                                run_id or "?", _q)
                            try:
                                from infra.notify import (
                                    notify_agent_needs_input as _nai)
                                _nai(run_id or "automation",
                                     "Automation paused: " + _q)
                            except (ImportError, OSError, RuntimeError):
                                pass
                    elif etype == "done":
                        stop_reason = str(ev.get("stop_reason") or "completed")
                        return

        try:
            async with asyncio.timeout(max(30, int(max_runtime_s))):
                await _consume()
        except asyncio.TimeoutError:
            stop_reason = "timeout"
            if run_id:
                try:
                    await client.post(f"/abort/{run_id}", json={})
                except (httpx.HTTPError, OSError):
                    pass
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("[AUTOMATION] stream failed: %s", exc)
            stop_reason = "engine_error"
        return "".join(answer_parts).strip(), stop_reason or "engine_error", run_id

    # -- chat persistence -------------------------------------------------
    async def _ensure_chat(self, job: dict) -> str:
        existing = str(job.get("chat_id") or "")
        if existing:
            return existing
        name = str(job.get("name") or "Automation")
        ws = str(job.get("workspace") or "")
        try:
            r = await self._http().post("/chats", json={
                "title": f"⏰ {name}", "messages": [], "workspace": ws})
            r.raise_for_status()
            data = r.json()
            cid = str(data.get("id") or (data.get("chat") or {}).get("id") or "")
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning("[AUTOMATION] chat create failed: %s", exc)
            return ""
        if cid:
            self.store.update(job.get("id"), {"chat_id": cid})
        return cid

    async def _get_chat(self, chat_id: str) -> tuple[list, int]:
        r = await self._http().get(f"/chats/{chat_id}")
        r.raise_for_status()
        data = r.json()
        msgs = data.get("messages") or []
        rev = int(data.get("rev") or 0)
        return msgs, rev

    async def _append_turn(self, chat_id: str, role: str, content: str,
                           job: dict) -> None:
        if not content:
            return
        for _ in range(2):
            try:
                msgs, rev = await self._get_chat(chat_id)
                msgs = list(msgs) + [{"role": role, "content": content,
                                      "ts": int(time.time() * 1000)}]
                ws = str(job.get("workspace") or "")
                r = await self._http().put(f"/chats/{chat_id}", json={
                    "messages": msgs, "workspace": ws, "base_rev": rev})
                if r.status_code == 409:
                    continue
                r.raise_for_status()
                return
            except (httpx.HTTPError, OSError, ValueError) as exc:
                logger.warning("[AUTOMATION] chat append failed (%s): %s",
                               chat_id, exc)
                return


_manager: SchedulerManager | None = None


def init_manager(store_path: str | Path, base_url: str) -> SchedulerManager:
    global _manager
    _manager = SchedulerManager(store_path, base_url)
    return _manager


def get_manager() -> SchedulerManager | None:
    return _manager
