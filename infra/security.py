# -*- coding: utf-8 -*-
"""CSRF/Origin-Guard (aus server.py extrahiert).

State-changing Requests nur Same-Origin; die Middleware-Registrierung
(``app.middleware("http")``) macht der Aufrufer in server.py.

G4 (full audit 2026-10-05): the Origin==Host check alone is NOT a DNS-
rebinding defense — a rebound page's Origin and Host are BOTH the attacker
domain, so the check passes and the page is same-origin (reads included).
The engine therefore answers only to an allowlist of hostnames (loopback by
default, extendable via HIVEMIND_TRUSTED_HOSTS for non-default binds).
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _csrf_origin_host(origin: str) -> str:
    try:
        from urllib.parse import urlsplit
        return (urlsplit(origin).netloc or "").lower()
    except Exception:
        return ""


def _trusted_hostnames() -> set:
    """Hostnames the engine accepts in the Host header. Loopback by
    default (matches run.py's 127.0.0.1 bind); extra names only via
    HIVEMIND_TRUSTED_HOSTS (comma-separated) for deliberately non-local
    binds."""
    import os
    hosts = {"127.0.0.1", "localhost", "::1"}
    for _h in os.environ.get("HIVEMIND_TRUSTED_HOSTS", "").split(","):
        _h = _h.strip().lower()
        if _h:
            hosts.add(_h)
    return hosts


def _host_hostname(host: str) -> str:
    """'127.0.0.1:8001' -> '127.0.0.1', '[::1]:8001' -> '::1'."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0].lstrip("[")
    return host.rsplit(":", 1)[0] if ":" in host else host


async def _csrf_origin_guard(request: Request, call_next):
    _method = request.method.upper()
    # G4: Host allowlist — applies to EVERY method (rebinding pages can
    # also read GET surfaces once same-origin). No Host header at all
    # (raw HTTP/1.0 clients) cannot be judged and passes as before.
    _hname = _host_hostname(request.headers.get("host") or "")
    if _hname and _hname not in _trusted_hostnames():
        return JSONResponse(
            {"ok": False,
             "error": "Untrusted Host header (DNS-rebinding guard)."},
            status_code=403,
        )
    if _method not in _CSRF_SAFE_METHODS:
        origin = (request.headers.get("origin") or "").strip()
        if origin:
            _o = _csrf_origin_host(origin)
            _h = (request.headers.get("host") or "").lower()
            if not _o or not _h or _o != _h:
                return JSONResponse(
                    {"ok": False, "error": "Cross-origin request blocked (CSRF guard)."},
                    status_code=403,
                )
    return await call_next(request)
