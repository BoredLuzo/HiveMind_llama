"""Per-model harness quality counters - measurement only, never a cap.

Owner decision 2026-10-17 (after the tool-list review): there is NO
model-dependent tool cap and no "tier_cap" profile. The tier drop stays purely
window-based (schema tokens against the configured context); that is the only
hard limit. What we DO want is a number instead of "the small model handles the
tool list well":

  unknown_tool  - the model called a name that is not in its tool list
  invalid_args  - a tool call came back with INVALID_ARGUMENT (bad/missing args)
  repeat_call   - the same call with the same arguments as the previous one
  parse_error   - the response was not a usable tool call at all

Nothing in here changes behaviour: callers count, nobody branches on the
result. A first occurrence per (model, kind) is logged at INFO so it shows up in
the normal log, repeats only at DEBUG, and summary() gives one line per model for
run ends / telemetry rows.

The store is bounded (MAX_MODELS models, last few details each) - a long-lived
engine must not grow with every chat.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("hivemind.harness")

KINDS = ("unknown_tool", "invalid_args", "repeat_call", "parse_error")

MAX_MODELS = 32
MAX_DETAILS = 3

_LOCK = threading.Lock()
_COUNTS: dict[str, dict[str, int]] = {}
_DETAILS: dict[str, dict[str, list[str]]] = {}


def _model_key(model) -> str:
    return str(model or "").strip() or "(unset)"


def note(model, kind: str, detail: str = "") -> None:
    """Count one event for a model. Never raises, never blocks a tool call."""
    try:
        _kind = str(kind or "").strip()
        if _kind not in KINDS:
            return
        _m = _model_key(model)
        with _LOCK:
            if _m not in _COUNTS and len(_COUNTS) >= MAX_MODELS:
                # bounded: drop the model with the fewest events
                _drop = min(_COUNTS, key=lambda k: sum(_COUNTS[k].values()))
                _COUNTS.pop(_drop, None)
                _DETAILS.pop(_drop, None)
            _row = _COUNTS.setdefault(_m, {k: 0 for k in KINDS})
            _row[_kind] = int(_row.get(_kind, 0)) + 1
            _first = _row[_kind] == 1
            if detail:
                _det = _DETAILS.setdefault(_m, {}).setdefault(_kind, [])
                if len(_det) < MAX_DETAILS:
                    _det.append(str(detail)[:120])
            _total = _row[_kind]
        if _first:
            logger.info("[HARNESS] model=%s %s first occurrence%s",
                        _m, _kind, f" ({str(detail)[:80]})" if detail else "")
        else:
            logger.debug("[HARNESS] model=%s %s +1 (total %d)", _m, _kind, _total)
    except (AttributeError, TypeError, ValueError, KeyError, IndexError):
        # measurement must never break a run (bad model/detail objects are the
        # realistic failure here - narrowed on purpose, see the except lint)
        logger.debug("[HARNESS] note failed", exc_info=True)


def stats() -> dict[str, dict[str, int]]:
    """Snapshot: {model: {kind: count}} (only non-zero kinds)."""
    with _LOCK:
        return {m: {k: v for k, v in row.items() if v}
                for m, row in _COUNTS.items() if any(row.values())}


def details() -> dict[str, dict[str, list[str]]]:
    with _LOCK:
        return {m: {k: list(v) for k, v in kinds.items()}
                for m, kinds in _DETAILS.items()}


def summary(model=None) -> str:
    """One line for a model (or all models): what it tripped over, if anything."""
    _snap = stats()
    if model is not None:
        _m = _model_key(model)
        _row = _snap.get(_m)
        if not _row:
            return f"[HARNESS] model={_m} clean (no unknown tools, no invalid args, no repeats)"
        _parts = ", ".join(f"{k}={v}" for k, v in sorted(_row.items()))
        _ex = details().get(_m, {})
        _exs = "; ".join(f"{k}: {v[0]}" for k, v in sorted(_ex.items()) if v)
        return f"[HARNESS] model={_m} {_parts}" + (f" | {_exs}" if _exs else "")
    if not _snap:
        return "[HARNESS] no events recorded"
    return " ".join(summary(_m) for _m in sorted(_snap))


def reset() -> None:
    with _LOCK:
        _COUNTS.clear()
        _DETAILS.clear()
