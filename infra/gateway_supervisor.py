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


def note_heartbeat(pid: int, data: dict | None = None,
                   now: float | None = None) -> None:
    _HB["ts"] = time.time() if now is None else now
    _HB["pid"] = int(pid or 0)
    _HB["data"] = dict(data or {})


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
    out: dict = {"running": running,
                 "pid": _HB["pid"] if running else 0,
                 "age_s": round(age, 1) if age is not None else None}
    out.update(_HB["data"] if running else {})
    return out
