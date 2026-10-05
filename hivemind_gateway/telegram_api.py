"""Thin Telegram Bot API client — hand-written on httpx (no aiogram).

Exactly the methods the gateway needs:
  getUpdates, sendMessage, editMessageText, answerCallbackQuery,
  getFile, sendDocument.
No webhook support, no open port — outbound long-polling only.

The token arrives via constructor; it can still leak through httpx's own
error strings, so the caller must keep TokenRedactionFilter attached to
the log handlers (see redaction.py) — the URLs contain the token.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

TELEGRAM_BASE = "https://api.telegram.org"


class TelegramApiError(RuntimeError):
    def __init__(self, method: str, description: str,
                 retry_after: int | None = None):
        super().__init__(f"{method} failed: {description}")
        self.method = method
        self.description = description
        self.retry_after = retry_after


class TelegramApi:
    def __init__(self, token: str, base_url: str = TELEGRAM_BASE):
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._base = f"{self._base_url}/bot{token}"
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def _http(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=httpx.Timeout(40.0, connect=10.0))
        return self._client

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    async def _call(self, method: str, **params: Any) -> dict:
        c = await self._http()
        r = await c.post(f"{self._base}/{method}", json=params)
        try:
            data = r.json()
        except ValueError:
            raise TelegramApiError(
                method, f"non-JSON HTTP {r.status_code}") from None
        if not data.get("ok"):
            params_desc = str(data.get("description") or "unknown error")
            retry_after = None
            if data.get("parameters") \
                    and isinstance(data["parameters"], dict) \
                    and "retry_after" in data["parameters"]:
                retry_after = int(data["parameters"]["retry_after"])
            raise TelegramApiError(method, params_desc, retry_after)
        result = data.get("result")
        return result if isinstance(result, dict) else {"result": result}

    # -- the allowed methods ---------------------------------------------

    async def get_me(self) -> dict:
        """getMe — used by the setup-token CLI ONLY (validate a token
        before storing it). Never called by the poll loop."""
        return await self._call("getMe")

    async def get_updates(self, offset: int, timeout_s: int,
                          allowed_updates: list[str] | None = None) -> list:
        """offset=-1 drops the backlog (returns only the newest update)."""
        r = await self._call(
            "getUpdates",
            offset=offset,
            timeout=timeout_s,
            allowed_updates=allowed_updates
            or ["message", "edited_message", "callback_query"],
        )
        updates = r.get("result", [])
        return updates if isinstance(updates, list) else []

    async def send_message(self, chat_id: str | int, text: str,
                           reply_to_message_id: int | None = None,
                           disable_web_page_preview: bool = True,
                           parse_mode: str | None = None,
                           reply_markup: dict | None = None) -> dict:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": disable_web_page_preview},
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup:
            params["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            params["reply_parameters"] = {
                "message_id": reply_to_message_id, "allow_sending_without_reply": True}
        return await self._call("sendMessage", **params)

    async def edit_message_text(self, chat_id: str | int, message_id: int,
                                text: str) -> dict:
        return await self._call(
            "editMessageText",
            chat_id=chat_id, message_id=message_id, text=text,
            link_preview_options={"is_disabled": True},
        )

    async def answer_callback_query(self, callback_query_id: str,
                                    text: str | None = None) -> dict:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text
        return await self._call("answerCallbackQuery", **params)

    async def get_file(self, file_id: str) -> dict:
        return await self._call("getFile", file_id=file_id)

    async def send_document(self, chat_id: str | int, data: bytes,
                            filename: str,
                            caption: str | None = None,
                            reply_to_message_id: int | None = None) -> dict:
        c = await self._http()
        form: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            form["caption"] = caption
        if reply_to_message_id is not None:
            form["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True}
        files = {"document": (filename, data)}
        r = await c.post(f"{self._base}/sendDocument", data=form,
                         files=files)
        try:
            payload = r.json()
        except ValueError:
            raise TelegramApiError(
                "sendDocument", f"non-JSON HTTP {r.status_code}") from None
        if not payload.get("ok"):
            raise TelegramApiError(
                "sendDocument", str(payload.get("description") or "error"))
        result = payload.get("result")
        return result if isinstance(result, dict) else {"result": result}

    def file_url(self, file_path: str) -> str:
        """Download URL for a getFile result (photos, WP5). Contains the
        token — never log it."""
        return (f"{self._base_url}/file/bot{self._token}/{file_path.lstrip('/')}")
