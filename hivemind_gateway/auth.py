"""Auth — owner whitelist, private-only, pairing. Pure, no network.

Rules (brief: SECURITY/Authentication):
  - Whitelist on from.id, only chat.type == private, for ALL update types
    the gateway handles: message, edited_message, callback_query.
  - Everything else is dropped (reason returned for rate-limited logging).
  - Pairing: one-time base32 code (>= 8 chars), 5 minutes valid, single
    use, constant-time comparison, max 5 failed attempts then locked
    until restart, disabled after success.
"""
from __future__ import annotations

import base64
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

PAIR_TTL_S = 300
MAX_FAILED_ATTEMPTS = 5
MIN_CODE_CHARS = 8

ALLOWED_UPDATE_TYPES = ("message", "edited_message", "callback_query")


def generate_pairing_code() -> str:
    """8 random bytes -> ~13 base32 chars (alphabet A-Z2-7), no padding."""
    return base64.b32encode(secrets.token_bytes(8)).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class ParsedUpdate:
    update_id: int
    kind: str                 # message | edited_message | callback_query
    from_id: int | None
    chat_id: str | None
    chat_type: str | None     # "private" | "group" | ... (None for malformed)
    text: str                 # message text or callback_data
    date: int | None          # message.date (unix, replay window)
    is_forwarded: bool
    message_id: int | None    # for edits/replies
    callback_id: str | None


def _from_of(payload: dict) -> dict | None:
    f = payload.get("from")
    return f if isinstance(f, dict) else None


def parse_update(raw: dict) -> ParsedUpdate | None:
    """Extract the identity/shape we need from ANY handled update type.
    None for updates we cannot interpret (dropped upstream)."""
    update_id = raw.get("update_id")
    if not isinstance(update_id, int):
        return None
    for kind in ALLOWED_UPDATE_TYPES:
        payload = raw.get(kind)
        if not isinstance(payload, dict):
            continue
        frm = _from_of(payload)
        chat = payload.get("chat") if kind != "callback_query" \
            else (payload.get("message") or {}).get("chat")
        chat = chat if isinstance(chat, dict) else {}
        text = payload.get("text") or payload.get("data") or ""
        if kind == "callback_query":
            date = (payload.get("message") or {}).get("date")
            message_id = (payload.get("message") or {}).get("message_id")
        else:
            date = payload.get("date")
            message_id = payload.get("message_id")
        forwarded = any(k.startswith("forward_") for k in payload)
        return ParsedUpdate(
            update_id=update_id,
            kind=kind,
            from_id=frm.get("id") if frm else None,
            chat_id=str(chat.get("id")) if chat.get("id") is not None else None,
            chat_type=str(chat.get("type")) if chat.get("type") else None,
            text=text if isinstance(text, str) else "",
            date=date if isinstance(date, int) else None,
            is_forwarded=forwarded,
            message_id=message_id if isinstance(message_id, int) else None,
            callback_id=payload.get("id") if kind == "callback_query" else None,
        )
    return None


def classify(p: ParsedUpdate, owner_id: int | None) -> str:
    """One of: owner | unknown_user | non_private | pair_window.

    No answer is ever owed to anything but "owner" (and /pair inside the
    pair window). Reasons exist only for rate-limited logging."""
    if p.from_id is None:
        return "unknown_user"
    if owner_id is None:
        return "pair_window"
    if p.from_id != owner_id:
        return "unknown_user"
    if p.chat_type != "private":
        return "non_private"
    return "owner"


class PairingError(Exception):
    """Base for verify() failures — callers answer the user generically."""


class PairingLocked(PairingError):
    """Too many failed attempts; locked until restart."""


class PairingDisabled(PairingError):
    """Pairing already succeeded; the gateway has an owner."""


class PairingDenied(PairingError):
    """Wrong or expired code (counts as a failed attempt)."""


class PairingManager:
    """One pairing window per process start (brief: 'locked until
    restart'; 'after successful pairing, pairing is disabled')."""

    def __init__(self, now: Callable[[], float] = time.time):
        self._now = now
        self._code: str | None = None
        self._expires: float = 0.0
        self._consumed = False
        self._failed = 0
        self._locked = False
        self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def failed_attempts(self) -> int:
        return self._failed

    def start_window(self) -> str:
        """Generate a fresh code; called at startup when unpaired."""
        self._code = generate_pairing_code()
        self._expires = self._now() + PAIR_TTL_S
        self._consumed = False
        return self._code

    def verify(self, guess: str) -> bool:
        """True only for the exact, unexpired, unused code. Every other
        outcome raises; 5 failures lock the window until restart."""
        if not self._enabled:
            raise PairingDisabled("pairing is disabled (owner already bound)")
        if self._locked:
            raise PairingLocked("pairing locked after too many failed "
                                "attempts — restart the gateway")
        if self._code is None or self._consumed:
            raise PairingDenied("no active pairing window")
        if self._now() > self._expires:
            raise PairingDenied("pairing code expired")
        ok = hmac.compare_digest(self._code, str(guess or "").strip())
        if not ok:
            self._failed += 1
            if self._failed >= MAX_FAILED_ATTEMPTS:
                self._locked = True
            raise PairingDenied("wrong pairing code")
        self._consumed = True
        self._enabled = False
        return True
