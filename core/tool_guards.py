# -*- coding: utf-8 -*-
"""Shared tool-loop guards (no-progress detection).

A model that repeats the SAME tool call with the SAME arguments is not making
progress; small local models do this often (re-read the same file, re-run the
same command). The guard nudges once, then lets the caller's round limit
handle a model that still refuses to move.
"""
from __future__ import annotations


def tool_signatures(msg: dict) -> list[str]:
    """Stable signatures for one assistant message's tool calls."""
    out: list[str] = []
    for _tc in (msg or {}).get("tool_calls") or []:
        _fn = (_tc or {}).get("function") or {}
        _name = str(_fn.get("name") or "")
        _args = _fn.get("arguments", "")
        if not isinstance(_args, str):
            import json as _json
            try:
                _args = _json.dumps(_args, sort_keys=True, ensure_ascii=False)
            except (TypeError, ValueError):
                _args = str(_args)
        if _name:
            out.append(f"{_name}:{_args}")
    return out


def repeat_guard():
    """on_after_round callback: nudge an identical repeated tool call once."""
    _counts: dict[str, int] = {}
    _nudged: set[str] = set()

    async def _after_round(msg, msgs, _round, _state):
        for _sig in tool_signatures(msg):
            _counts[_sig] = _counts.get(_sig, 0) + 1
            if _counts[_sig] >= 2 and _sig not in _nudged:
                _nudged.add(_sig)
                msgs.append({"role": "user", "content": (
                    "You already called this tool with the same arguments "
                    "earlier in this run. If its result is still in context, "
                    "use it instead of repeating; if it was compressed away "
                    "or you need fresh verification, re-running is fine.")})
        return True

    return _after_round
