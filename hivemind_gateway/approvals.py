"""Approval nonce store (pure). The button's callback_data carries a short
random nonce — never a tool name, never a command. The server-side map is
nonce -> payload, single use, expiring, chat-bound.

WP4 wires this to real approvals; the store itself is final here because
it is pure and the security table (repeated tap, expired callback, wrong
chat) is testable without a Telegram connection.
"""
from __future__ import annotations

import secrets
import time
from collections.abc import Callable


class ApprovalStore:
    def __init__(self, ttl_s: int = 120, now: Callable[[], float] = time.time):
        self._ttl = ttl_s
        self._now = now
        self._entries: dict[str, dict] = {}

    def create(self, approval_id: str, chat_id: str, tool_call_hash: str,
               message_id: int | None = None) -> str:
        nonce = secrets.token_urlsafe(9)  # 12 chars, URL-safe
        self._entries[nonce] = {
            "approval_id": approval_id,
            "chat_id": str(chat_id),
            "tool_call_hash": tool_call_hash,
            "message_id": message_id,
            "expires": self._now() + self._ttl,
            "used": False,
        }
        return nonce

    def peek(self, nonce: str) -> dict | None:
        """Payload if the nonce is live (unused, unexpired) — for rendering
        the approval card. Does not consume."""
        e = self._entries.get(nonce)
        if e is None or e["used"] or self._now() > e["expires"]:
            return None
        return dict(e)

    def consume(self, nonce: str, chat_id: str) -> dict | None:
        """Single-use consume. None on unknown/expired/wrong-chat/replay —
        the caller treats None as deny-with-log."""
        e = self._entries.get(nonce)
        if e is None or e["used"] or self._now() > e["expires"]:
            return None
        if e["chat_id"] != str(chat_id):
            return None
        e["used"] = True
        return dict(e)

    def sweep(self) -> int:
        """Drop used/expired entries; returns the number removed."""
        now = self._now()
        dead = [n for n, e in self._entries.items()
                if e["used"] or now > e["expires"]]
        for n in dead:
            del self._entries[n]
        return len(dead)

    def all_live(self) -> dict[str, dict]:
        now = self._now()
        return {n: dict(e) for n, e in self._entries.items()
                if not e["used"] and now <= e["expires"]}
