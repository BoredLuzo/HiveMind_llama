# -*- coding: utf-8 -*-
"""Passive [PERF] log line per llama request (2026-10-30).

Decision it supports: telling cache-loss (prompt_n high, cache_n ~0) apart
from depth cost and from side-call noise without re-running anything.
One line per request, INFO level, greppable:

    [PERF] kind=main model=... port=8101 p_tok=70956 cache_n=70782 \
           prompt_n=174 prompt_ms=10899.6 pred_n=52 tps=25.87

Fields come from the response `usage` (prompt_tokens,
prompt_tokens_details.cached_tokens, completion_tokens) and `timings`
(prompt_n, prompt_ms, predicted_n, predicted_per_second). Missing fields
print as "-", never as 0 (0 would fake a cache loss).
"""
from __future__ import annotations

_FMT = ("[PERF] kind=%s model=%s port=%s p_tok=%s cache_n=%s prompt_n=%s "
        "prompt_ms=%s pred_n=%s tps=%s")


def _num(value, nd=1):
    if value is None:
        return "-"
    try:
        return f"{float(value):.{nd}f}"
    except (TypeError, ValueError):
        return "-"


def _int(value):
    if value is None:
        return "-"
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "-"


def perf_line(*, kind: str, model: str = "", port="", usage: dict | None = None,
              timings: dict | None = None) -> str:
    """Build the one-line PERF summary (pure; unit-tested)."""
    u = usage or {}
    t = timings or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached is None:
        cached = u.get("cached_tokens")
    p_tok = u.get("prompt_tokens")
    # cache_n without p_tok is meaningless -> "-" keeps it honest
    cache_s = _int(cached) if p_tok is not None else "-"
    return _FMT % (
        kind or "-",
        model or "-",
        port if port not in (None, "") else "-",
        _int(p_tok),
        cache_s,
        _int(t.get("prompt_n")),
        _num(t.get("prompt_ms")),
        _int(t.get("predicted_n")),
        _num(t.get("predicted_per_second"), 2),
    )


def _env_on(name: str, default: bool = True) -> bool:
    """Env switch reader: '0'/'false'/'off'/'no' disables; unset keeps the
    default. Used for the passive diagnostics so they can be silenced
    without a UI change."""
    import os
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "off", "no")


def perf_enabled() -> bool:
    """[PERF] lines: HiveMind_perf_log=0 silences them (default on)."""
    return _env_on("HiveMind_perf_log", True)


def decode_warn_enabled() -> bool:
    """[DECODE-DROP] warnings: HiveMind_decode_drop_warn=0 silences them
    (default on). Warning only in either case - never a restart."""
    return _env_on("HiveMind_decode_drop_warn", True)
