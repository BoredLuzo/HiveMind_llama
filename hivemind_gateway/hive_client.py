"""HiveMind client — SSE + REST (WP2 wires the real flows; the shapes
here follow docs/gateway_contract.md).

Every transport failure surfaces as HiveUnreachable: the bridge turns
that into a readable phone message. Raw httpx exceptions must never
escape this module — realrun bug #4 (2026-10-05) was exactly that: a
ConnectError from create_chat killed the whole gateway instead of
answering the phone with "offline".
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

    async def _get(self, path: str) -> httpx.Response:
        try:
            return await self._client.get(path)
        except httpx.HTTPError as exc:
            raise HiveUnreachable(str(exc)) from None

    async def _post_json(self, path: str, body: dict) -> httpx.Response:
        try:
            return await self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise HiveUnreachable(str(exc)) from None

    async def _put_json(self, path: str, body: dict) -> httpx.Response:
        try:
            return await self._client.put(path, json=body)
        except httpx.HTTPError as exc:
            raise HiveUnreachable(str(exc)) from None

    async def health(self) -> dict:
        r = await self._get("/health")
        if r.status_code != 200:
            raise HiveUnreachable(f"/health HTTP {r.status_code}")
        return r.json()

    async def journal(self) -> dict:
        """GET /run/journal — {active, run_id, done, aborted, ts, frames}.
        Busiest heuristic the server offers until the WP3 409 guard."""
        r = await self._get("/run/journal")
        if r.status_code != 200:
            raise HiveUnreachable(f"/run/journal HTTP {r.status_code}")
        return r.json()

    async def settings(self) -> dict:
        """GET /settings — the gateway reads (never writes) it so the
        HiveMind UI can veto the whole integration
        (telegram_gateway_enabled=false => the gateway shuts down)."""
        r = await self._get("/settings")
        if r.status_code != 200:
            raise HiveUnreachable(f"/settings HTTP {r.status_code}")
        return r.json()

    async def create_chat(self, title: str, workspace: str | None = None) \
            -> dict:
        body: dict[str, Any] = {"title": title, "messages": []}
        if workspace:
            body["workspace"] = workspace
        r = await self._post_json("/chats", body)
        r.raise_for_status()
        return r.json()

    async def put_chat_messages(self, chat_id: str, messages: list,
                                base_rev: int) -> httpx.Response:
        """Transcript write; 409 means server-wins: adopt r.json()['messages']."""
        return await self._put_json(
            f"/chats/{chat_id}", {"messages": messages, "base_rev": base_rev})

    async def get_chat(self, chat_id: str) -> httpx.Response:
        return await self._get(f"/chats/{chat_id}")

    async def abort_chat(self, chat_id: str, silent: bool = False) -> dict:
        # /abort takes chat_id as QUERY param (contract divergence #1).
        try:
            r = await self._client.post(
                "/abort",
                params={"chat_id": chat_id, "silent": str(silent).lower()})
        except httpx.HTTPError as exc:
            raise HiveUnreachable(str(exc)) from None
        r.raise_for_status()
        return r.json()

    async def abort_run(self, run_id: str) -> httpx.Response:
        return await self._post_json(f"/abort/{run_id}", {})

    async def decide_approval(self, run_id: str, answer: str) -> httpx.Response:
        """answer "1"=once, "3"=deny. The gateway NEVER sends "2"."""
        return await self._post_json(
            f"/approval/decide/{run_id}", {"answer": answer})

    async def pending_approval(self, run_id: str) -> httpx.Response:
        return await self._get(f"/approval/pending/{run_id}")

    async def get_models(self) -> dict:
        """GET /models — {models: [names], profiles: [{name, thinking,
        vision, tool_call, ...}]}."""
        r = await self._get("/models")
        if r.status_code != 200:
            raise HiveUnreachable(f"/models HTTP {r.status_code}")
        return r.json()

    async def get_presets(self) -> dict:
        r = await self._get("/presets")
        if r.status_code != 200:
            raise HiveUnreachable(f"/presets HTTP {r.status_code}")
        return r.json()

    async def load_preset(self, name: str) -> httpx.Response:
        """POST /presets/{name}/load — GLOBAL state: sets the engine's
        active preset for UI and phone alike. The bridge warns about
        that in its confirmation."""
        return await self._post_json(f"/presets/{name}/load", {})

    async def stream(self, q: str, chat_id: str, images: list | None = None,
                     mode: str = "", overrides: dict | None = None):
        """Yield parsed SSE data payloads of a run. No token streaming is
        rendered — the caller decides what becomes a status update.
        `mode` travels in the REQUEST BODY only (never via POST /settings,
        which is taboo for the gateway): a Telegram-side mode therefore
        never touches what the browser UI runs. `overrides` are the
        model/ctx keys the engine merges into the run's settings
        snapshot (duo_planner_model, duo_coder_model, duo_planner_ctx_target,
        duo_coder_ctx_agentic, duo_coder_ctx_normal — verified
        chat_run.py:154). Transport breaks raise HiveUnreachable."""
        body: dict[str, Any] = {"q": q, "images": images or [],
                                "chat_id": chat_id}
        if mode:
            body["mode"] = mode
        if overrides:
            body.update(overrides)
        try:
            async with self._client.stream("POST", "/stream", json=body) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        yield json.loads(line[6:])
                    except ValueError:
                        continue  # keep-alive or malformed frame: skip
        except httpx.HTTPError as exc:
            raise HiveUnreachable(f"stream broke: {exc}") from None
