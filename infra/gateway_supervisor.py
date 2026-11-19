"""Gateway supervisor state (P8-lite, 2026-10-05).

The running gateway POSTs a heartbeat every mirror-loop tick (5 s); the
UI reads GET /gateway/status and can POST /gateway/start|stop. The
heartbeat store and freshness rule live here as pure functions so they
are unit-testable without a server; the subprocess wrappers live in
server.py (thin: spawn sys.executable -m hivemind_gateway.main, kill via
taskkill after a freshness + process-name check).

Trust model: loopback + Host allowlist only — a local process can forge
heartbeats exactly as it could call any other engine endpoint (same
class, no new surface).
"""
from __future__ import annotations

import time

MAX_AGE_S = 15.0  # ~3 missed heartbeats at the 5 s loop

_HB: dict = {"ts": 0.0, "pid": 0, "data": {}}
_RESERVED_STATUS_KEYS = frozenset({"running", "pid", "age_s"})


def parse_positive_int(val) -> int:
    """Return int(val) when *val* is a positive non-bool integer, else 0."""
    if val is None or isinstance(val, bool):
        return 0
    try:
        num = int(val)
    except (TypeError, ValueError):
        return 0
    return num if num > 0 else 0


def parse_birth(val) -> int | None:
    """Validate process birth timestamp (None/'' -> 0, invalid/negative -> None)."""
    if val is None or val == "":
        return 0
    if isinstance(val, bool):
        return None
    try:
        num = int(val)
    except (TypeError, ValueError):
        return None
    return num if num >= 0 else None


def is_loopback_request(req) -> bool:
    """Return True only when *req* originates from a loopback client host."""
    from infra.security import _is_loopback_client

    client = getattr(req, "client", None) if req is not None else None
    host = getattr(client, "host", "") if client is not None else ""
    return _is_loopback_client(str(host or ""))


def note_heartbeat(pid: int, data: dict | None = None,
                   now: float | None = None) -> None:
    _HB["ts"] = time.time() if now is None else now
    _HB["pid"] = parse_positive_int(pid)
    if isinstance(data, dict):
        _HB["data"] = {
            k: v for k, v in data.items()
            if k not in _RESERVED_STATUS_KEYS
        }
    else:
        _HB["data"] = {}


def clear() -> None:
    """Immediately mark the gateway gone (used by /gateway/stop so the
    status does not linger 'running' until the max age elapses)."""
    _HB["ts"] = 0.0
    _HB["pid"] = 0
    _HB["data"] = {}


def status(now: float | None = None, max_age_s: float = MAX_AGE_S) -> dict:
    t = time.time() if now is None else now
    age = (t - float(_HB["ts"])) if _HB["ts"] else None
    running = age is not None and 0 <= age <= max_age_s
    out: dict = {}
    if running and isinstance(_HB.get("data"), dict):
        for k, v in _HB["data"].items():
            if k not in _RESERVED_STATUS_KEYS:
                out[k] = v
    out["running"] = running
    out["pid"] = parse_positive_int(_HB["pid"]) if running else 0
    out["age_s"] = round(age, 1) if age is not None else None
    return out
