"""Pure output rendering: splitting, escaping, secret filtering.

Model and tool output is UNTRUSTED (brief: SECURITY/Output):
  - default is plain text (parse_mode off) — escape_html exists only for
    the explicitly tested parse_mode path,
  - > 4096 chars goes out as a .txt document; split_message covers the
    in-between case (multiple messages, line boundaries, balanced fences),
  - filter_secrets runs BEFORE anything is sent.
"""
from __future__ import annotations

import html
import re

TELEGRAM_HARD_LIMIT = 4096

# Things that must never travel to Telegram even inside "harmless" output.
_DEFAULT_SECRET_PATTERNS = (
    re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}"),          # bot token in URLs
    re.compile(r"sk-[A-Za-z0-9]{16,}"),                # openai-style keys
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),               # github PATs
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_ENV_SECRET_LINE = re.compile(
    r"^\s*([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY)[A-Z0-9_]*)\s*=\s*(\S+)\s*$",
    re.IGNORECASE)


def filter_secrets(text: str, extra_patterns: tuple = ()) -> str:
    out = text
    for pat in _DEFAULT_SECRET_PATTERNS:
        out = pat.sub("***", out)
    for pat in extra_patterns:
        out = pat.sub("***", out)
    out = "\n".join(
        _ENV_SECRET_LINE.sub(r"\1=***", line) for line in out.split("\n"))
    return out


def escape_html(text: str) -> str:
    """For the parse_mode="HTML" path ONLY (WP2 decides, with tests).
    Plain-text sends must not use it — raw text is already safe."""
    return html.escape(text, quote=False)


def split_message(text: str, limit: int = TELEGRAM_HARD_LIMIT) -> list[str]:
    """Split at LINE boundaries under `limit`, keeping code fences
    balanced: a chunk that cuts inside a fence gets the fence closed and
    the next chunk reopens it with the same language tag. A single line
    longer than `limit` is hard-split (must not be lost)."""
    if len(text) <= limit:
        return [text] if text else []
    fence_re = re.compile(r"^\s*(`{3,})([A-Za-z0-9_+-]*)\s*$")
    chunks: list[str] = []
    current: list[str] = []
    cur_len = 0
    open_fence: str | None = None
    open_lang = ""

    def close_chunk(force_fence_close: bool) -> None:
        nonlocal current, cur_len, open_fence
        if not current:
            return
        body = "\n".join(current)
        if force_fence_close and open_fence is not None:
            body += "\n" + open_fence
        chunks.append(body)
        if force_fence_close and open_fence is not None:
            current = [open_fence + open_lang] if open_lang \
                else [open_fence]
            cur_len = len(current[0])
        else:
            current = []
            cur_len = 0

    for line in text.split("\n"):
        m = fence_re.match(line)
        if m:
            if open_fence is None:
                open_fence, open_lang = m.group(1), m.group(2)
            else:
                open_fence, open_lang = None, ""
        elif len(line) + 1 > limit and not m:
            # oversized single line: flush, then hard-split the line
            close_chunk(open_fence is not None)
            for i in range(0, len(line), limit):
                pieces = line[i:i + limit]
                chunks.append(pieces)
            current, cur_len = [], 0
            continue
        if cur_len + len(line) + 1 > limit and current:
            close_chunk(open_fence is not None)
        current.append(line)
        cur_len += len(line) + 1
    close_chunk(False)
    return [c for c in chunks if c.strip("` \n") or "```" in c] or [""]
