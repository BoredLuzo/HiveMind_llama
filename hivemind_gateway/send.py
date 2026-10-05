"""THE one send() — central invariant of the whole gateway.

No code path may hand a Telegram call its own chat_id; everything goes
through send(), which refuses any chat that is not the owner's. The
security table's "invariant: no code path sends to a chat_id other than
the owner's" is enforced HERE and tested in test_gateway_send.py.
"""
from __future__ import annotations


class PolicyViolation(RuntimeError):
    """Raised instead of sending when the target chat is not the owner."""


async def send(api, owner_chat_id: str | int | None,
               chat_id: str | int, *, text: str | None = None,
               new_text: str | None = None, message_id: int | None = None,
               document_bytes: bytes | None = None,
               filename: str | None = None,
               reply_to_message_id: int | None = None,
               parse_mode: str | None = None,
               reply_markup: dict | None = None) -> dict:
    """All outbound traffic. Exactly one of:
      text          -> sendMessage
      new_text+id   -> editMessageText
      document+name -> sendDocument
    Raises PolicyViolation BEFORE any network call when chat_id is not
    the owner's (or the gateway is unpaired: owner unknown => nothing is
    ever sent)."""
    if owner_chat_id is None:
        raise PolicyViolation("gateway is unpaired — refusing to send "
                              "to any chat")
    if str(chat_id) != str(owner_chat_id):
        raise PolicyViolation(
            f"refusing to send to chat {chat_id!r}: not the owner "
            f"({owner_chat_id!r})")
    if new_text is not None:
        if message_id is None:
            raise ValueError("edit requires message_id")
        return await api.edit_message_text(chat_id, message_id, new_text)
    if document_bytes is not None:
        return await api.send_document(
            chat_id, document_bytes, filename or "output.txt",
            reply_to_message_id=reply_to_message_id)
    if text is None:
        raise ValueError("send() requires one of text / new_text / "
                         "document_bytes")
    return await api.send_message(chat_id, text,
                                  reply_to_message_id=reply_to_message_id,
                                  parse_mode=parse_mode,
                                  reply_markup=reply_markup)
