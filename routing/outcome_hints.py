# -*- coding: utf-8 -*-
"""Outcome hints from previous runs (D6, 2026-11-09).

The judge/automap routing is static: it sees the prompt, not how earlier runs
of the same chat actually ended. This module aggregates the per-chat
``run_audit`` ``run_end`` entries into a tiny success table and returns a hint
when the current route has a clearly worse track record than an alternative.

It never overrides a route. The caller emits it as a status line; the owner (or
a later policy layer) can act on it. No cross-chat learning: the audit is
chat-scoped by design.
"""
from __future__ import annotations

__all__ = ["summarize_outcomes", "suggest_route"]

_OK_STOPS = frozenset({"completed", "graceful_stop"})
_MIN_RUNS = 3
_MIN_GAP = 0.25


def summarize_outcomes(entries: list | None) -> dict:
    """Return {route: {runs, ok, rate}} from run_audit entries."""
    out: dict[str, dict] = {}
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("event") != "run_end":
            continue
        route = str(entry.get("route") or entry.get("mode") or "unknown")
        stop = str(entry.get("stop_reason") or "")
        if not stop:
            continue
        rec = out.setdefault(route, {"runs": 0, "ok": 0, "rate": 0.0})
        rec["runs"] += 1
        if stop in _OK_STOPS:
            rec["ok"] += 1
    for rec in out.values():
        rec["rate"] = round(rec["ok"] / rec["runs"], 3) if rec["runs"] else 0.0
    return out


def suggest_route(entries: list | None, current_route: str) -> dict | None:
    """Return a hint dict when another route is clearly better, else None.

    Requires at least `_MIN_RUNS` outcomes for the current route and for the
    candidate, and a success-rate gap of at least `_MIN_GAP`.
    """
    stats = summarize_outcomes(entries)
    current = stats.get(str(current_route or ""))
    if not current or current["runs"] < _MIN_RUNS:
        return None
    best_name = ""
    best = None
    for name, rec in stats.items():
        if name == current_route or rec["runs"] < _MIN_RUNS:
            continue
        if best is None or rec["rate"] > best["rate"]:
            best_name, best = name, rec
    if best is None or best["rate"] - current["rate"] < _MIN_GAP:
        return None
    return {
        "from": str(current_route),
        "to": best_name,
        "from_rate": current["rate"],
        "to_rate": best["rate"],
        "runs": {"from": current["runs"], "to": best["runs"]},
    }
