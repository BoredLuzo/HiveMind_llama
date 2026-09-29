# -*- coding: utf-8 -*-
"""Tool utility functions (extracted from server.py)."""
from __future__ import annotations
import json
import re

_RE_UNESCAPED_BACKSLASH = re.compile(r'\\([^\\/"bfnrtu])')


def _repair_json_backslashes(raw: str) -> str:
    """Escapes unescaped backslashes in a JSON text (Windows paths)."""
    return _RE_UNESCAPED_BACKSLASH.sub(r'\\\\\1', raw)


def parse_tool_args(raw) -> dict:


    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            try:
                return json.loads(_repair_json_backslashes(raw))
            except Exception:
                return {}
    return raw if isinstance(raw, dict) else {}


# ── WRITE-LIMIT-BUDGET (2026-08-22) ────────────────────────────────────────
# at the output token limit (duo_coder.max_tokens=8000) mid JSON/XML argument
# truncated -> finish_reason=length -> DROPPED -> 3x retry -> loop stop.
# calibration: [WRITE-CALIBRATION] logs (agentic_tool_loop) provide real
# ── WRITE-SIZE HINT (2026-09-29) ──────────────────────────────────────────────
# No per-size tiers anymore. Core insight: a fully-arrived call (finish_reason
# == stop, valid JSON) can never exceed max_tokens — max_tokens IS the hard
# limit, so a complete write is always accepted, whatever its size. The only
# failure mode is TRUNCATION (finish_reason == length), and the salvage path
# recovers that at the last complete line. The number below is therefore just
# a soft hint (chars per write_file / write_file_append call) that keeps
# models clear of the truncation/salvage path:
#   usable_tokens = max_tokens - reasoning_tokens   [thinking models burn it]
#   write_hint    = usable_tokens * chars_per_token * 0.4
#   append_hint   = usable_tokens * chars_per_token * 0.25
# The 0.4 cold-start factor reserves room for reasoning/thinking output and
# the tool-call wrapper. No name matching — new models need no entry here.
_WRITE_HINT_WRITE_FACTOR = 0.4
_WRITE_HINT_APPEND_FACTOR = 0.25
_WRITE_HINT_MIN_CHARS = 500


def resolve_write_char_limits(model: str = "", token_budget: int | None = None,
                              chars_per_token: float | None = None,
                              reasoning_tokens: int = 0) -> tuple[int, int]:


    _cpt = float(chars_per_token or 2.5)
    if _cpt <= 0:
        _cpt = 2.5
    _tok = max(0, int(token_budget or 0) - max(0, int(reasoning_tokens or 0)))
    _write = max(_WRITE_HINT_MIN_CHARS, int(_tok * _cpt * _WRITE_HINT_WRITE_FACTOR))
    _append = max(_WRITE_HINT_MIN_CHARS, int(_tok * _cpt * _WRITE_HINT_APPEND_FACTOR))
    return _write, _append


# ── TRUNCATION-SALVAGE (2026-08-22) ────────────────────────────────────────
_RE_SURROGATE = re.compile(r"[\ud800-\udfff]")
# incomplete escape fragment at the very end of a truncated string
_RE_TRAILING_PARTIAL_ESCAPE = re.compile(r"\\(?:u[0-9a-fA-F]{0,3}|\\)?$")


def _decode_salvaged_string(body: str) -> str:


    out = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            break  # dangling backslash: truncation -> discard the tail
        nxt = body[i + 1]
        if nxt == "u":
            _hex = body[i + 2:i + 6]
            if len(_hex) < 4 or not all(c in "0123456789abcdefABCDEF" for c in _hex):
                break  # incomplete \\uXXXX: truncation -> discard the tail
            out.append(chr(int(_hex, 16)))
            i += 6
            continue
        _esc_map = {"\\": "\\", '"': '"', "/": "/", "b": "\b",
                    "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
        if nxt in _esc_map:
            out.append(_esc_map[nxt])
            i += 2
            continue
        out.append("\\")
        out.append(nxt)
        i += 2
    return "".join(out)


def _scan_json_string(s: str, q: int) -> tuple[str, bool]:


    i, n = q + 1, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return s[q + 1:i], True
        i += 1
    return s[q + 1:n], False


def _extract_write_key(raw: str, key: str, start: int) -> tuple[str, bool] | None:


    _m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"', raw[start:])
    if not _m:
        return None
    _q = start + _m.end() - 1
    return _scan_json_string(raw, _q)


def _salvaged_line_count(content: str) -> int:


    return content.count("\n") if content.endswith("\n") else content.count("\n") + 1


def salvage_truncated_write_args(raw: str, tool_name: str = "write_file") -> dict | None:


    if not isinstance(raw, str) or not raw:
        return None
    if tool_name not in ("write_file", "write_file_append"):
        return None
    _start = raw.find("{")
    if _start == -1:
        return None
    _cand = raw[_start:]

    try:
        _parsed = json.loads(_cand + "}")
        if isinstance(_parsed, dict) and _parsed.get("path") and _parsed.get("content") is not None:
            _c = str(_parsed["content"])
            if _c:
                return {"args": {"path": str(_parsed["path"]), "content": _c},
                        "_salvage": True,
                        "_salvaged_chars": len(_c),
                        "_salvaged_lines": _salvaged_line_count(_c)}
    except (json.JSONDecodeError, TypeError):
        pass

    _path_body, _path_closed = _extract_write_key(_cand, "path", 0) or (None, False)
    if not _path_body or not _path_closed:
        return None
    _path = _decode_salvaged_string(_path_body)
    if not _path:
        return None

    _body, _closed = _extract_write_key(_cand, "content", 0) or (None, False)
    if _body is None:
        return None
    _content = _decode_salvaged_string(_body)
    _content = _RE_SURROGATE.sub("\ufffd", _content)
    if not _content:
        return None
    # TRUNCATION-EDGE (2026-09-29): a cut inside an escape sequence leaves a
    # literal fragment at the end of the decoded content ("\u00e", a lone
    # backslash). Only strip it when the JSON string was NOT closed — a real
    # trailing backslash in a closed string is legitimate content.
    if not _closed:
        _content = _RE_TRAILING_PARTIAL_ESCAPE.sub("", _content)
    if not _content:
        return None
    if not _closed and "\n" in _content:
        _head, _, _tail = _content.rpartition("\n")
        if _tail:
            _content = _head + "\n"
    _tail_lines = _content.splitlines()[-3:]
    return {"args": {"path": _path, "content": _content},
            "_salvage": True,
            "_salvaged_chars": len(_content),
            "_salvaged_lines": _salvaged_line_count(_content),
            "_salvaged_tail": "\n".join(_tail_lines)}


def run_bash_failed(result: str) -> bool:
    from tools.errors import parse_tool_error as _parse_tool_error
    txt = str(result or "")
    _terr = _parse_tool_error(txt)
    if _terr and str(_terr.get("tool", "") or "") == "run_bash":
        return True
    low = txt.lower()
    if txt.startswith("[run_bash error:"):
        return True
    if "[run_bash: timeout" in low:
        return True
    # TEST-RESULT-FAIL (2026-09-13): run_tests results carry no [exit code:]
    # marker — a failing suite (server-generated ❌) must count as a failure,
    # otherwise the verify gates treat mutations as verified despite red tests.
    if "[test-result] ❌" in low:
        return True
    if "[exit code:" in txt:
        # LAST match wins: the real marker is appended at the very end of the
        # output; an earlier literal "[exit code: 0]" (echoed by the model)
        # must not mask a genuine non-zero exit.
        matches = __import__("re").findall(r"\[exit code:\s*(\d+)", txt)
        if matches and int(matches[-1]) != 0:
            return True
    return False
