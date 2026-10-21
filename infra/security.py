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


# ── EXTERNAL-BIND GUARD (2026-10-21, audit) ────────────────────────────────
# When the server binds a NON-loopback interface (HIVEMIND_HOST=0.0.0.0 or a
# LAN IP), the whole API is reachable from the network — and the API can
# write files and execute commands. Non-local clients must then present the
# internal token (HIVEMIND_INTERNAL_TOKEN, same header /internal/tool/exec
# already uses). Loopback clients (the local UI) are unaffected. Behind a
# reverse proxy the proxy must enforce this (the engine sees the proxy as
# loopback).
import hmac as _hmac
import os as _os

# Set by run.py once the resolved bind host is known.
EXTERNAL_BIND = {"on": False}

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_loopback_client(client_host: str) -> bool:
    host = (client_host or "").strip().lower()
    if host.startswith("::ffff:"):  # IPv4-mapped IPv6
        host = host[len("::ffff:"):]
    return host.split("%", 1)[0] in _LOOPBACK_HOSTS


def external_bind_decision(client_host: str, token_header: str,
                           token_configured: str, bind_external: bool) -> int:
    """Pure decision for tests: 0 = allow, 403 = bad token, 503 = no token
    configured. Loopback clients always pass; the guard only acts while an
    external bind is active."""
    if not bind_external:
        return 0
    if _is_loopback_client(client_host):
        return 0
    tok = (token_configured or "").strip()
    if not tok:
        return 503
    supplied = (token_header or "").strip()
    if not supplied or not _hmac.compare_digest(tok, supplied):
        return 403
    return 0


async def _external_bind_guard(request: Request, call_next):
    if not EXTERNAL_BIND.get("on"):
        return await call_next(request)
    client_host = request.client.host if request.client else ""
    if _is_loopback_client(client_host):
        return await call_next(request)
    tok = (_os.environ.get("HIVEMIND_INTERNAL_TOKEN") or "").strip()
    if not tok:
        return JSONResponse(
            {"ok": False,
             "error": ("Bound to a non-loopback interface without "
                       "HIVEMIND_INTERNAL_TOKEN - remote access is disabled. "
                       "Set the token and send it as the "
                       "x-hivemind-internal-token header.")},
            status_code=503,
        )
    supplied = request.headers.get("x-hivemind-internal-token", "")
    if not supplied or not _hmac.compare_digest(tok, supplied):
        return JSONResponse(
            {"ok": False, "error": "Invalid internal token."},
            status_code=403,
        )
    return await call_next(request)
