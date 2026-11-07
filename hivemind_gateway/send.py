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
               chat_id: str | int | None, *, text: str | None = None,
               new_text: str | None = None, message_id: int | None = None,
               document_bytes: bytes | None = None,
               filename: str | None = None,
               voice_bytes: bytes | None = None,
               voice_duration: int = 0,
               reply_to_message_id: int | None = None,
               parse_mode: str | None = None,
               reply_markup: dict | None = None) -> dict:
    """All outbound traffic. Exactly one of:
      text          -> sendMessage
      new_text+id   -> editMessageText
      document+name -> sendDocument
      voice+ogg    -> sendVoice (Opus only, that is what Telegram accepts)
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
    # G3 (audit r2): filter_secrets ran ONLY in the own-run finish - the
    # mirror fazit, tool-call relay lines and approval previews reached
    # Telegram unfiltered. The transport boundary sees everything.
    from . import render as _render
    if text is not None:
        text = _render.filter_secrets(text)
    if new_text is not None:
        new_text = _render.filter_secrets(new_text)
    if document_bytes is not None:
        # BINARY-SAFE (2026-11-04): redaction is text-only. Decoding every
        # document with errors="replace" silently corrupted PNG/PDF/DOCX/ZIP
        # bytes into U+FFFD. Only known text types are decoded and filtered;
        # binary payloads go through untouched.
        _text_suffixes = {
            ".txt", ".md", ".markdown", ".html", ".htm", ".css", ".js",
            ".mjs", ".ts", ".tsx", ".jsx", ".json", ".jsonl", ".csv",
            ".log", ".xml", ".yml", ".yaml", ".toml", ".ini", ".cfg",
            ".py", ".sh", ".bat", ".ps1", ".sql", ".svg", ".r", ".go",
            ".rs", ".c", ".h", ".cpp", ".hpp", ".java", ".kt", ".php",
            ".rb", ".lua", ".tex", ".rtf",
        }
        _suffix = "." + str(filename or "").rsplit(".", 1)[-1].lower() if "." in str(filename or "") else ""
        _is_text = _suffix in _text_suffixes
        _looks_binary = b"\x00" in document_bytes[:4096]
        _text = None
        _encoding = "utf-8"
        if document_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
            # UTF-16 text (PowerShell Out-File default) must not bypass the
            # secret filter just because its bytes contain NULs.
            try:
                _text = document_bytes.decode("utf-16")
                _encoding = "utf-16"
            except UnicodeDecodeError:
                _text = None
        elif _is_text or not _looks_binary:
            try:
                _text = document_bytes.decode("utf-8")
            except UnicodeDecodeError:
                if _is_text:
                    # A text-suffixed file with legacy bytes: redact a
                    # lossy decode rather than sending the secret unread.
                    _text = document_bytes.decode("utf-8", "replace")
        if _text is not None:
            document_bytes = _render.filter_secrets(_text).encode(_encoding)
    if new_text is not None:
        if message_id is None:
            raise ValueError("edit requires message_id")
        return await api.edit_message_text(chat_id, message_id, new_text)
    if voice_bytes is not None:
        # a voice note is audio, nothing to redact - but the chat stays the
        # owner's (checked above) and the bytes must already be Ogg/Opus.
        return await api.send_voice(
            chat_id, voice_bytes, duration_s=voice_duration,
            reply_to_message_id=reply_to_message_id)
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
