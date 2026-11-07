# -*- coding: utf-8 -*-
"""Behavior tests for infra/log_noise.py (was at 0% coverage).

Covers the uvicorn polling-spam filter end to end: the status/path normalizers,
suppression decisions for the uvicorn access-log args tuple and the dict-args
fallback, the plain-message fallback (alternative logging formats), a real
logger+handler roundtrip, and the idempotent double-install of the filter on
the uvicorn loggers and their handlers. No server, no sockets.
"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from infra.log_noise import (  # noqa: E402
    _NoiseFilter,
    _install_uvicorn_noise_filter,
)
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _rec(args=None, msg=None):
    """LogRecord like the real uvicorn access logger: the msg carries the
    %-placeholders matching args (filter() calls getMessage() on the
    non-suppressed paths, so a mismatched msg would raise TypeError)."""
    if msg is None:
        if isinstance(args, tuple) and args:
            msg = " ".join(["%s"] * len(args))
        else:
            msg = "plain message"
    return logging.LogRecord(name="uvicorn.access", level=logging.INFO,
                             pathname="x.py", lineno=1, msg=msg,
                             args=args if args is not None else (),
                             exc_info=None)


f = _NoiseFilter()

# --- normalizers -------------------------------------------------------------
check("status int from str", f._status_int("200") == 200)
check("status int passthrough", f._status_int(304) == 304)
check("status int for garbage -> None", f._status_int("abc") is None)
check("status int for None -> None", f._status_int(None) is None)
check("path query stripped", f._norm_path("/vram/status?ts=1") == "/vram/status")
check("path None -> empty string", f._norm_path(None) == "")
check("path non-string coerced", f._norm_path(123) == "123")

# --- uvicorn access-logger args tuple ---------------------------------------
check("GET /vram/status 200 (with query) suppressed",
      f.filter(_rec(("1.2.3.4", "GET", "/vram/status?ts=1", "1.1", 200))) is False)
check("GET /models 304 suppressed",
      f.filter(_rec(("1.2.3.4", "GET", "/models", "1.1", 304))) is False)
check("GET /vram/table 200 suppressed",
      f.filter(_rec(("1.2.3.4", "GET", "/vram/table", "1.1", 200))) is False)
check("lowercase method still suppressed",
      f.filter(_rec(("1.2.3.4", "get", "/models", "1.1", 200))) is False)
check("POST /vram/status passes",
      f.filter(_rec(("1.2.3.4", "POST", "/vram/status", "1.1", 200))) is True)
check("GET /vram/status 500 passes",
      f.filter(_rec(("1.2.3.4", "GET", "/vram/status", "1.1", 500))) is True)
check("GET /chat 200 passes",
      f.filter(_rec(("1.2.3.4", "GET", "/chat", "1.1", 200))) is True)
check("status as string 200 suppressed",
      f.filter(_rec(("1.2.3.4", "GET", "/models", "1.1", "200"))) is False)
check("4-arg tuple (old format) passes",
      f.filter(_rec(("1.2.3.4", "GET", "/models", 200))) is True)
check("no args passes", f.filter(_rec(None)) is True)

# --- dict-args fallback ------------------------------------------------------
check("dict args GET/models/200 suppressed",
      f.filter(_rec({"method": "GET", "path": "/models?x=1",
                     "status_code": 200})) is False)
check("dict args full_path + status keys suppressed",
      f.filter(_rec({"method": "GET", "full_path": "/vram/status",
                     "status": 304})) is False)
check("dict args raw_path key recognized",
      f.filter(_rec({"method": "GET", "raw_path": "/vram/table",
                     "status_code": 200})) is False)
check("dict args POST passes",
      f.filter(_rec({"method": "POST", "path": "/models",
                     "status_code": 200})) is True)
check("dict args bad status passes",
      f.filter(_rec({"method": "GET", "path": "/models",
                     "status_code": "soon"})) is True)

# --- plain-message fallback (args neither tuple>=5 nor dict) ----------------
check("apache-style GET /models 200 line suppressed",
      f.filter(_rec(msg='1.2.3.4:0 - "GET /models HTTP/1.1" 200 OK')) is False)
check("apache-style GET /vram/status 304 line suppressed",
      f.filter(_rec(msg='"GET /vram/status HTTP/1.1" 304 -')) is False)
check("apache-style 500 line passes",
      f.filter(_rec(msg='"GET /models HTTP/1.1" 500 Internal')) is True)
check("message without quote-prefixed request passes",
      f.filter(_rec(msg="GET /models HTTP/1.1 200")) is True)
check("message for other paths passes",
      f.filter(_rec(msg='"GET /vram/other HTTP/1.1" 200 OK')) is True)

# --- end-to-end through a real logger/handler -------------------------------
seen = []


class _Capture(logging.Handler):
    def emit(self, record):
        seen.append(record)


lg = logging.getLogger("uvicorn.access.behavior_test")
lg.propagate = False
lg.handlers = []
lg.setLevel(logging.INFO)
cap = _Capture()
cap.addFilter(_NoiseFilter())
lg.addHandler(cap)
try:
    lg.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET", "/vram/status",
            "1.1", 200)
    lg.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET", "/chat", "1.1", 200)
    check("handler drops the poll record and keeps the real one",
          len(seen) == 1 and 'GET /chat' in seen[0].getMessage())
finally:
    lg.removeHandler(cap)
    lg.handlers = []
    lg.setLevel(logging.NOTSET)
    lg.propagate = True

# --- idempotent install on the real uvicorn loggers -------------------------
targets = ("uvicorn.access", "uvicorn")
saved = {}
try:
    for name in targets:
        lg_t = logging.getLogger(name)
        saved[name] = (list(lg_t.filters), list(lg_t.handlers))
        # exercise the handler branch deterministically on both loggers
        lg_t.addHandler(logging.NullHandler())

    _install_uvicorn_noise_filter()
    _install_uvicorn_noise_filter()
    for name in targets:
        _lg = logging.getLogger(name)
        n_log = sum(1 for x in _lg.filters if isinstance(x, _NoiseFilter))
        n_hdl = [sum(1 for x in hh.filters if isinstance(x, _NoiseFilter))
                 for hh in _lg.handlers]
        check(f"double install leaves exactly one filter on {name} logger",
              n_log == 1)
        check(f"double install leaves one filter per {name} handler",
              all(n == 1 for n in n_hdl) and len(n_hdl) >= 1)

    inst = logging.getLogger("uvicorn.access").filters[0]
    check("installed filter actually suppresses poll spam",
          inst.filter(_rec(("9.9.9.9", "GET", "/models", "1.1", 200))) is False)
finally:
    for name in targets:
        _lg = logging.getLogger(name)
        for flt in list(_lg.filters):
            if isinstance(flt, _NoiseFilter):
                _lg.removeFilter(flt)
        filters0, handlers0 = saved[name]
        for hdl in list(_lg.handlers):
            if hdl not in handlers0:
                _lg.removeHandler(hdl)

sys.exit(h.finish("log_noise_behavior"))
