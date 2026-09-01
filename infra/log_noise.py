# -*- coding: utf-8 -*-
"""Uvicorn-Log-Noise-Filter (aus server.py extrahiert).

Suppresses polling spam from /vram/status, /models, /vram/table.
"""
from __future__ import annotations

import logging

_SUPPRESS_PATHS = {"/vram/status", "/models", "/vram/table"}
_SUPPRESS_STATUS = {200, 304}


class _NoiseFilter(logging.Filter):
    @staticmethod
    def _status_int(raw) -> int | None:
        try:
            return int(raw)
        except Exception:
            return None

    @staticmethod
    def _norm_path(raw) -> str:
        p = str(raw or "")
        return p.split("?", 1)[0]

    def _is_suppressed_access_record(self, record: logging.LogRecord) -> bool:
        args = getattr(record, "args", None)

        # Uvicorn Access-Logger: (client_addr, method, full_path, http_version, status_code)
        if isinstance(args, tuple) and len(args) >= 5:
            method = str(args[1]).upper()
            path = self._norm_path(args[2])
            status = self._status_int(args[4])
            return method == "GET" and path in _SUPPRESS_PATHS and status in _SUPPRESS_STATUS

        # fallback for alternative logging formats
        if isinstance(args, dict):
            method = str(args.get("method", "")).upper()
            path = self._norm_path(args.get("path") or args.get("full_path") or args.get("raw_path") or "")
            status = self._status_int(args.get("status_code") or args.get("status"))
            return method == "GET" and path in _SUPPRESS_PATHS and status in _SUPPRESS_STATUS

        return False

    def filter(self, record: logging.LogRecord) -> bool:
        if self._is_suppressed_access_record(record):
            return False

        msg = record.getMessage()
        for path in _SUPPRESS_PATHS:
            if f"\"GET {path} HTTP" in msg and ("\" 200 " in msg or "\" 304 " in msg):
                return False
        return True


def _install_uvicorn_noise_filter() -> None:
    """Install the polling filter idempotently on loggers and their handlers."""
    for _uvlog in ("uvicorn.access", "uvicorn"):
        _lg = logging.getLogger(_uvlog)
        if not any(isinstance(_f, _NoiseFilter) for _f in _lg.filters):
            _lg.addFilter(_NoiseFilter())
        for _h in _lg.handlers:
            if not any(isinstance(_f, _NoiseFilter) for _f in _h.filters):
                _h.addFilter(_NoiseFilter())
