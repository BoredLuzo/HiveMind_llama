# -*- coding: utf-8 -*-
"""CSRF/Origin-Guard (aus server.py extrahiert).

State-changing Requests nur Same-Origin; die Middleware-Registrierung
(``app.middleware("http")``) macht der Aufrufer in server.py.
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


async def _csrf_origin_guard(request: Request, call_next):
    try:
        if request.method not in _CSRF_SAFE_METHODS:
            origin = (request.headers.get("origin") or "").strip()
            if origin:
                _o = _csrf_origin_host(origin)
                _h = (request.headers.get("host") or "").lower()
                if not _o or not _h or _o != _h:
                    return JSONResponse(
                        {"ok": False, "error": "Cross-origin request blocked (CSRF guard)."},
                        status_code=403,
                    )
    except Exception:
        pass
    return await call_next(request)
