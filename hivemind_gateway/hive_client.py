"""HiveMind client — SSE + REST (WP2 wires the real flows; the shapes
here follow docs/gateway_contract.md).

Implemented now because it is pure HTTP with no side effects at import
time; the gateway does NOT call any of it before WP2 (brief: WP1 builds
the skeleton with NO HiveMind call).
"""
from __future__ import annotations

import json
from typing import Any

import httpx

DONE_STOP_REASONS = (
    "completed", "error", "model_load_failed", "timeout", "hard_stop",
    "graceful_stop", "loop_detected", "wedge_escalated", "halted",
    "aborted", "max_tool_rounds", "verification_required_after_write",
)


class HiveUnreachable(RuntimeError):
    pass


class HiveClient:
    def __init__(self, base_url: str):
        self._base = base_url.rstrip("/")
        # trust_env=False: loopback traffic must NEVER detour through a
        # proxy configured in the environment (HTTP_PROXY etc.) — the
        # engine hop is local by contract.
        self._client = httpx.AsyncClient(
            base_url=self._base, trust_env=False,
            timeout=httpx.Timeout(60.0, connect=5.0))

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict:
        try:
            r = await self._client.get("/health")
        except httpx.HTTPError as exc:
            raise HiveUnreachable(str(exc)) from None
        if r.status_code != 200:
            raise HiveUnreachable(f"/health HTTP {r.status_code}")
        return r.json()

    async def journal(self) -> dict:
        """GET /run/journal — {active, run_id, done, aborted, ts, frames}.
        Busiest heuristic the server offers until the WP3 409 guard."""
        try:
            r = await self._client.get("/run/journal")
        except httpx.HTTPError as exc:
            raise HiveUnreachable(str(exc)) from None
        if r.status_code != 200:
            raise HiveUnreachable(f"/run/journal HTTP {r.status_code}")
        return r.json()

    async def settings(self) -> dict:
        """GET /settings — the gateway reads (never writes) it so the
        HiveMind UI can veto the whole integration
        (telegram_gateway_enabled=false => the gateway shuts down)."""
        try:
            r = await self._client.get("/settings")
        except httpx.HTTPError as exc:
            raise HiveUnreachable(str(exc)) from None
        if r.status_code != 200:
            raise HiveUnreachable(f"/settings HTTP {r.status_code}")
        return r.json()

    async def create_chat(self, title: str, workspace: str | None = None) \
            -> dict:
        body: dict[str, Any] = {"title": title, "messages": []}
        if workspace:
            body["workspace"] = workspace
        r = await self._client.post("/chats", json=body)
        r.raise_for_status()
        return r.json()

    async def put_chat_messages(self, chat_id: str, messages: list,
                                base_rev: int) -> httpx.Response:
        """Transcript write; 409 means server-wins: adopt r.json()['messages']."""
        return await self._client.put(
            f"/chats/{chat_id}",
            json={"messages": messages, "base_rev": base_rev})

    async def get_chat(self, chat_id: str) -> httpx.Response:
        return await self._client.get(f"/chats/{chat_id}")

    async def abort_chat(self, chat_id: str, silent: bool = False) -> dict:
        # /abort takes chat_id as QUERY param (contract divergence #1).
        r = await self._client.post(
            "/abort", params={"chat_id": chat_id, "silent": str(silent).lower()})
        r.raise_for_status()
        return r.json()

    async def abort_run(self, run_id: str) -> httpx.Response:
        return await self._client.post(f"/abort/{run_id}")

    async def decide_approval(self, run_id: str, answer: str) -> httpx.Response:
        """answer "1"=once, "3"=deny. The gateway NEVER sends "2"."""
        return await self._client.post(
            f"/approval/decide/{run_id}", json={"answer": answer})

    async def pending_approval(self, run_id: str) -> httpx.Response:
        return await self._client.get(f"/approval/pending/{run_id}")

    async def stream(self, q: str, chat_id: str, images: list | None = None):
        """Yield parsed SSE data payloads of a run. No token streaming is
        rendered — the caller decides what becomes a status update."""
        body = {"q": q, "images": images or [], "chat_id": chat_id}
        async with self._client.stream("POST", "/stream", json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    yield json.loads(line[6:])
                except ValueError:
                    continue  # keep-alive or malformed frame: skip, don't crash
