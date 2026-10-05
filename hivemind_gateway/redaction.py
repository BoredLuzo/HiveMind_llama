"""Log redaction — the bot token sits INSIDE the Telegram URLs.

Every https://api.telegram.org/bot<TOKEN>/... request line that reaches a
log handler would leak the token into log files, consoles and therefore
backups. TokenRedactionFilter scrubs configured secrets and applies a
generic bot-token URL pattern as a second net. Attach it to every handler
the gateway creates (see attach_redaction()).
"""
from __future__ import annotations

import logging
import re

# <bot_id>:<35-ish chars> in a URL — matches even when the literal token
# string was not configured (e.g. a token that was rotated out of the
# filter list). Over-matching here is harmless: it only redacts logs.
_BOT_TOKEN_URL = re.compile(r"bot(\d+):([A-Za-z0-9_-]{20,})")


def scrub_text(text: str, secrets: list[str] | tuple[str, ...]) -> str:
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, "***REDACTED***")
    out = _BOT_TOKEN_URL.sub(r"bot\1:***REDACTED***", out)
    return out


class TokenRedactionFilter(logging.Filter):
    """Scrub secrets from every emitted record.

    The record is rewritten BEFORE any handler formats it: msg is replaced
    by the formatted-then-scrubbed text and args are cleared, so %-style
    lazy formatting cannot re-introduce the secret later.
    """

    def __init__(self, secrets: list[str] | tuple[str, ...] = ()):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def add_secret(self, secret: str) -> None:
        if secret and secret not in self._secrets:
            self._secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except (TypeError, ValueError, KeyError):
            text = repr(record.msg)
        record.msg = scrub_text(text, self._secrets)
        record.args = None
        return True


def attach_redaction(logger: logging.Logger,
                     secrets: list[str] | tuple[str, ...]) \
        -> TokenRedactionFilter:
    """Attach the filter to every handler already on `logger` (and its
    parents' handlers via propagation share the root handlers)."""
    f = TokenRedactionFilter(secrets)
    for h in logger.handlers:
        h.addFilter(f)
    return f


def attach_redaction_to_root(secrets: list[str] | tuple[str, ...]) \
        -> TokenRedactionFilter:
    """One filter instance on every root handler — the returned handle
    lets main() add the real token as a secret once it is resolved."""
    f = TokenRedactionFilter(secrets)
    for h in logging.getLogger().handlers:
        h.addFilter(f)
    return f
