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
        self._last_sent_files: list[dict[str, str]] = []
        self._fire_lock = asyncio.Lock()
        self._in_flight_jobs: set[str] = set()

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
                trust_env=False,
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
        if pending and not self.lane_busy() and not self._in_flight_jobs and not self._fire_lock.locked():
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
                _job_snap = {**job, "pending_fire": dict(p)}
                self.store.update(job.get("id"), {"pending_fire": None})
                self._spawn_fire(_job_snap, now, reason="queued")
            return

        for job in jobs:
            if not job.get("enabled", True):
                continue
            if job.get("pending_fire"):
                continue  # already waiting for the lane; not skipped_busy
            _jid = str(job.get("id") or "")
            if _jid and _jid in self._in_flight_jobs:
                continue
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
                continue
            self._spawn_fire(job, now, reason="due")
            return

    # -- run-now ----------------------------------------------------------
    def run_now(self, job_id: str) -> tuple[bool, str]:
        job = self.store.get(job_id)
        if job is None:
            return False, "not found"
        if self.lane_busy() or self._in_flight_jobs or self._fire_lock.locked():
            return False, "busy: another run is active"
        self._in_flight_jobs.add(str(job_id))
        # SPAWN-HELPER (2026-11-05 audit): raw create_task loses the only
        # strong reference when this method returns - utils.bg_tasks.spawn
        # exists for exactly that. Fire in the background: a run can take
        # minutes and the HTTP caller (UI button) must not block on it.
        from utils.bg_tasks import spawn
        _spawned = False
        try:
            t = spawn(self._fire(job, _now(), reason="manual"),
                      name=f"automation-{job_id}")
            _spawned = True
        finally:
            if not _spawned:
                self._in_flight_jobs.discard(str(job_id))
        self._fire_tasks.add(t)
        t.add_done_callback(self._fire_tasks.discard)
        return True, "started"

    # -- firing -----------------------------------------------------------
    def _spawn_fire(self, job: dict, now: float, reason: str) -> None:
        jid = str(job.get("id") or "")
        if jid:
            self._in_flight_jobs.add(jid)
        from utils.bg_tasks import spawn
        _spawned = False
        try:
            t = spawn(self._fire(job, now, reason=reason),
                      name=f"automation-{jid}")
            _spawned = True
        finally:
            if not _spawned and jid:
                self._in_flight_jobs.discard(jid)
        self._fire_tasks.add(t)
        t.add_done_callback(self._fire_tasks.discard)

    async def _fire(self, job: dict, now: float, reason: str) -> None:
        job_id = str(job.get("id") or "")
        try:
            async with self._fire_lock:
                await self._fire_locked(job, now, reason)
        finally:
            if job_id:
                self._in_flight_jobs.discard(job_id)

    async def _fire_locked(self, job: dict, now: float, reason: str) -> None:
            if self.lane_busy():
                # Lost the race (run_now vs tick): re-queue if it came from pending_fire,
                # otherwise record skipped_busy instead of silently dropping.
                if reason == "queued" and job.get("pending_fire"):
                    self.store.update(job.get("id"), {
                        "pending_fire": job.get("pending_fire"),
                        "last_status": "queued"})
                else:
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
            self._last_ask_preview = ""
            self._last_sent_files = []
            raw_mode = str(job.get("mode") or "simple").strip().lower()
            tier = str(job.get("tools_tier") or "readonly").strip().lower()
            notify_tg = bool(job.get("notify_telegram", False))

            if tier in ("mcp", "full") or raw_mode == "auto":
                try:
                    from infra import mcp_client as _mc
                    if _mc.enabled() and getattr(_mc.get_client(), "_defs", None) is None:
                        await asyncio.wait_for(_mc.refresh(), timeout=15.0)
                except (ImportError, AttributeError, RuntimeError, OSError, asyncio.TimeoutError) as _mcp_err:
                    logger.debug("[AUTOMATION] lazy MCP warmup skipped: %s", _mcp_err)

            if raw_mode in ("agentic", "code_duo"):
                stream_mode = "code_duo"
                duo_agentic: bool | None = True
                agentic_force = True
            elif raw_mode == "code_critic":
                stream_mode = "code_duo"
                duo_agentic = False
                agentic_force = False
            elif raw_mode in ("direct", "simple"):
                stream_mode = "simple"
                duo_agentic = None
                agentic_force = False
            else:
                stream_mode = raw_mode
                duo_agentic = True if raw_mode == "auto" else None
                agentic_force = False

            direct_tier = ("full" if tier == "full" else (
                tier if tier in ("off", "readonly", "research", "mcp", "python") else "readonly"))
            duo_coder_tier = (
                "full" if tier == "full" else
                "mcp" if tier == "mcp" else
                "readonly" if tier in ("off", "readonly", "research") else
                "core"
            )

            body: dict[str, Any] = {
                "q": prompt,
                "chat_id": chat_id,
                "mode": stream_mode,
                "direct_tools_enabled": tier != "off",
                "direct_tools_tier": direct_tier,
                "duo_coder_tool_tier": duo_coder_tier,
                "duo_websearch_enabled": tier != "off",
            }
            if tier == "off":
                body["duo_tool_rounds"] = 0
                body["duo_pre_explore"] = False
            if duo_agentic is not None:
                body["duo_agentic_mode"] = duo_agentic
            if agentic_force:
                body["agentic_force"] = True
                if tier != "off":
                    body["duo_tool_rounds"] = 1
                body["iters"] = 1
            elif raw_mode == "code_critic" and tier != "off":
                body["duo_tool_rounds"] = 1
            if notify_tg:
                body["source"] = "telegram"
            ws = str(job.get("workspace") or "")
            if ws:
                body["workspace"] = ws
            answer, stop_reason, run_id = await self._run_stream(
                body, int(float(job.get("max_runtime_s") or DEFAULT_RUNTIME_S)))
            sent_files = list(self._last_sent_files)
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
            if notify_tg:
                await self._deliver_telegram(
                    job, answer, status, stop_reason, sent_files)
            duration_s = round(max(0.0, _now() - started), 1)
            # RE-FETCH (2026-11-05 audit): `job` may be a stale dict if the
            # user edited/deleted the job during a long run. Mutate through
            # the store, and decide once-deletion from the STORED schedule.
            current = self.store.get(job_id)
            if current is None:
                self.store.append_history({
                    "job_id": job_id, "run_id": run_id, "chat_id": chat_id,
                    "status": status, "stop_reason": stop_reason,
                    "duration_s": duration_s,
                    "started": started, "finished": _now(),
                    "reason": reason})
                return
            self.store.update(job_id, {
                "last_status": status, "last_duration_s": duration_s})
            self.store.append_history({
                "job_id": job_id, "run_id": run_id, "chat_id": chat_id,
                "status": status, "stop_reason": stop_reason,
                "duration_s": duration_s,
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
                    elif etype == "send_file" and ev.get("path"):
                        self._last_sent_files.append({
                            "path": str(ev.get("path") or ""),
                            "caption": str(ev.get("caption") or ""),
                            "workspace": str(ev.get("workspace") or body.get("workspace") or ""),
                        })
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

    async def _deliver_telegram(
        self,
        job: dict[str, Any],
        answer: str,
        status: str,
        stop_reason: str,
        sent_files: list[dict[str, str]] | None = None,
    ) -> None:
        """Push automation output (+ any send_file attachments) to paired owner."""
        try:
            from hivemind_gateway.main import resolve_token, StartupError
            from hivemind_gateway.state import GatewayState
            from hivemind_gateway.telegram_api import TelegramApi, TelegramApiError
            from hivemind_gateway import render, send as gw_send
            from core.send_files import SendFileError, read_sendable
        except ImportError as exc:
            logger.warning("[AUTOMATION] Telegram delivery unavailable for %s: %s", job.get("id"), exc)
            return

        def _load_tg_creds() -> tuple[str, int | str | None]:
            try:
                tok = resolve_token()
            except StartupError:
                tok = ""
            return tok, GatewayState().owner_telegram_id

        try:
            token, owner_id = await asyncio.to_thread(_load_tg_creds)
            if not token or not owner_id:
                logger.info(
                    "[AUTOMATION] %s notify_telegram=True skipped: Telegram token or owner not configured",
                    job.get("id"),
                )
                return
            api = TelegramApi(token)
            try:
                icon = "✅" if status == "ok" else ("❓" if status == "needs_input" else "⚠️")
                header = f"⏰ **{job.get('name') or 'Automation'}** ({icon} {status})\n\n"
                if answer:
                    body_md = answer.strip()
                elif status == "needs_input" and self._last_ask_preview:
                    body_md = f"(Automation paused — {self._last_ask_preview})"
                else:
                    body_md = f"(No text output — {stop_reason or status})"
                for chunk in render.split_message(header + body_md):
                    html_chunk = render.md_to_telegram_html(chunk)
                    try:
                        await gw_send.send(
                            api, owner_id, owner_id,
                            text=html_chunk, parse_mode="HTML",
                        )
                    except TelegramApiError as exc:
                        ra = getattr(exc, "retry_after", None)
                        if ra:
                            await asyncio.sleep(int(ra) + 1)
                        await gw_send.send(api, owner_id, owner_id, text=chunk)
                job_ws = str(job.get("workspace") or "")
                for sf in (sent_files or []):
                    raw_path = str(sf.get("path") or "")
                    ws = str(sf.get("workspace") or job_ws)
                    if not raw_path:
                        continue
                    try:
                        data, fname = await asyncio.to_thread(read_sendable, ws, raw_path)
                    except (SendFileError, OSError) as sf_exc:
                        logger.warning("[AUTOMATION] send_file skipped for %s: %s", raw_path, sf_exc)
                        continue
                    try:
                        await gw_send.send(
                            api, owner_id, owner_id,
                            document_bytes=data,
                            filename=fname,
                        )
                    except (TelegramApiError, OSError, ValueError, RuntimeError, httpx.HTTPError) as sf_send_exc:
                        logger.warning("[AUTOMATION] send_file delivery failed for %s: %s", raw_path, sf_send_exc)
            finally:
                await api.close()
        except (OSError, ValueError, RuntimeError, httpx.HTTPError, TelegramApiError) as exc:
            logger.warning("[AUTOMATION] Telegram delivery failed for %s: %s", job.get("id"), exc)

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
