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
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),         # github PATs/OAuth
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),       # fine-grained PATs
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),           # gitlab PATs
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),       # slack tokens
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token)"
               r"\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{8,}"),  # inline key=value
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
    """For the parse_mode="HTML" path (see md_to_telegram_html).
    Plain-text sends must not use it — raw text is already safe."""
    return html.escape(text, quote=False)


def md_to_telegram_html(text: str) -> str:
    """Markdown-ish run output -> Telegram HTML (2026-10-05 UX round).

    Escape FIRST, then translate only COMPLETE constructs inside the
    chunk: ``` fences become <pre> blocks, `inline` becomes <code>,
    **bold** becomes <b>. Incomplete constructs survive as escaped plain
    text, so a chunk boundary can never produce invalid HTML — and every
    dynamic substring (<, >, &, user content) is escaped before any tag
    is emitted."""
    out = html.escape(text, quote=False)
    # fenced blocks first (```lang\n...\n``` -> <pre>...</pre>)
    out = re.sub(r"```[A-Za-z0-9_+#-]*\n(.*?)```",
                 lambda m: "<pre>" + m.group(1) + "</pre>", out, flags=re.S)
    # H-audit: stash <pre> spans - the rewrites below turned **text**
    # inside fenced blocks into <b> nested in <pre>, which Telegram
    # rejects (400) or misrenders.
    _pre_spans: list = []
    def _stash_pre(m):
        _pre_spans.append(m.group(0))
        return "\x00PRE" + str(len(_pre_spans) - 1) + "\x00"
    out = re.sub(r"<pre>.*?</pre>", _stash_pre, out, flags=re.S)
    # inline code (single backticks; may also hit text inside <pre> —
    # harmless: Telegram renders <code> inside <pre> fine)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    # bold
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", out)
    for _i, _span in enumerate(_pre_spans):
        out = out.replace("\x00PRE" + str(_i) + "\x00", _span)
    return out


def _tg_len(text: str) -> int:
    """Telegram counts UTF-16 code units, not Python characters (P1, audit
    2026-11-03): 2100 emoji fit len() but are 4200 units and get rejected."""
    return len((text or "").encode("utf-16-le")) // 2


def _hard_split(text: str, limit: int) -> list[str]:
    """Split by UTF-16 units so an emoji-heavy line cannot exceed the limit."""
    out: list[str] = []
    buf: list[str] = []
    n = 0
    for ch in text:
        w = _tg_len(ch)
        if buf and n + w > limit:
            out.append("".join(buf))
            buf, n = [], 0
        buf.append(ch)
        n += w
    if buf:
        out.append("".join(buf))
    return out


def split_message(text: str, limit: int = TELEGRAM_HARD_LIMIT) -> list[str]:
    """Split at LINE boundaries under `limit`, keeping code fences
    balanced: a chunk that cuts inside a fence gets the fence closed and
    the next chunk reopens it with the same language tag. A single line
    longer than `limit` is hard-split (must not be lost)."""
    if _tg_len(text) <= limit:
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
            cur_len = _tg_len(current[0])
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
        elif _tg_len(line) + 1 > limit and not m:
            # oversized single line: flush, then hard-split the line
            close_chunk(open_fence is not None)
            chunks.extend(_hard_split(line, limit))
            # H-audit: close_chunk() reopened the fence into current and
            # the reset below discarded it - the rest of the block lost
            # its opening fence. Re-open it here.
            if open_fence is not None:
                current = [open_fence + open_lang] if open_lang \
                    else [open_fence]
                cur_len = _tg_len(current[0]) + 1
            else:
                current, cur_len = [], 0
            continue
        if cur_len + _tg_len(line) + 1 > limit and current:
            close_chunk(open_fence is not None)
        current.append(line)
        cur_len += _tg_len(line) + 1
    close_chunk(False)
    return [c for c in chunks if c.strip("` \n") or "```" in c] or [""]
