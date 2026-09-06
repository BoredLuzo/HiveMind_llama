# -*- coding: utf-8 -*-
"""Tool-call history sanitizer (2026-09-06).

llama-server validates the tool_calls arguments of assistant messages it receives
and answers HTTP 500 ("Failed to parse tool call arguments as JSON") when one is
malformed. A truncated generation near the max_tokens budget can leave such a
message in history (e.g. a write_file argument cut inside a JSON string), which
then poisons every following request.

This helper strips invalid assistant tool_calls (and the now-orphaned tool-result
messages that reference them) from the list BEFORE the POST, so a single truncated
call can never take the whole run down.
"""
from __future__ import annotations

import json
import re

_PATH_RE = re.compile(r'"path"\s*:\s*"([^"]+)"')


def sanitize_invalid_tool_call_history(messages: list) -> int:
    """Remove assistant tool_calls with invalid JSON arguments in place.

    Also removes tool messages whose tool_call_id references a removed call and
    appends a short "[REMOVED]" user notice so the coder does not forget that a
    write/edit attempt happened (it was truncated, not completed).
    Returns the number of removed tool-call entries (0 = nothing to do).
    """
    if not messages:
        return 0

    bad: dict = {}  # tool_call_id -> (function name, raw args)
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            raw = fn.get("arguments") if isinstance(fn, dict) else None
            if isinstance(raw, (dict, list)):
                continue  # already parsed object -> fine
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                json.loads(text)
            except Exception:
                cid = str(tc.get("id") or "").strip()
                if cid:
                    bad[cid] = (str(fn.get("name") or "tool"), text)

    if not bad:
        return 0

    removed = 0
    out: list = []
    removed_targets: list = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        if m.get("role") == "tool":
            if str(m.get("tool_call_id") or "") in bad:
                continue  # orphaned tool result - dropped, not counted as a call
            out.append(m)
            continue
        if m.get("role") == "assistant":
            tcs = m.get("tool_calls") or []
            if tcs:
                keep = []
                for tc in tcs:
                    if isinstance(tc, dict) and str(tc.get("id") or "") in bad:
                        _name, _raw = bad[str(tc.get("id") or "")]
                        _pm = _PATH_RE.search(_raw)
                        removed_targets.append(f"{_name} → {_pm.group(1)}" if _pm else _name)
                        removed += 1
                        continue
                    keep.append(tc)
                if len(keep) != len(tcs):
                    m = dict(m, tool_calls=keep or None)
        out.append(m)

    messages[:] = out
    if removed:
        if not (messages and str(messages[-1].get("content") or "").startswith("[REMOVED]")):
            messages.append({
                "role": "user",
                "content": (
                    "[REMOVED] "
                    f"{removed} truncated tool call(s) removed from history "
                    f"(malformed JSON, never executed): {', '.join(removed_targets)} "
                    "— retry if still needed."
                ),
            })
    return removed
