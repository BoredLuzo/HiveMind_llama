# -*- coding: utf-8 -*-
"""CSRF guard Host allowlist (audit G4): DNS-rebinding defense.

A rebound page's Origin and Host are BOTH the attacker domain — the old
Origin==Host check alone passed for such requests, and the page was
same-origin (reads included). The engine now answers only to an allowlist
of hostnames (loopback by default, HIVEMIND_TRUSTED_HOSTS to extend).
Unit-level: the middleware only reads .method/.headers, so a stub request
is enough. Offline.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.responses import JSONResponse

from infra.security import _csrf_origin_guard, _host_hostname

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


def _req(method="POST", host="127.0.0.1:8001", origin=None):
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    return SimpleNamespace(method=method, headers=headers)


async def _call_next(req):
    return "OK"


def _is_pass(result):
    return not isinstance(result, JSONResponse)


def main() -> int:
    import os

    # hostname extraction (IPv6 brackets, ports)
    check("host split ipv4", _host_hostname("127.0.0.1:8001") == "127.0.0.1")
    check("host split ipv6", _host_hostname("[::1]:8001") == "::1")
    check("host bare", _host_hostname("localhost") == "localhost")

    # loopback hosts pass on every method
    for method in ("GET", "POST", "PUT"):
        r = asyncio.run(_csrf_origin_guard(
            _req(method, "127.0.0.1:8001"), _call_next))
        check(f"loopback host passes ({method})", _is_pass(r))
    r = asyncio.run(_csrf_origin_guard(_req("POST", "localhost:8000"),
                                       _call_next))
    check("localhost passes", _is_pass(r))
    r = asyncio.run(_csrf_origin_guard(_req("POST", "[::1]:8001"),
                                       _call_next))
    check("ipv6 loopback passes", _is_pass(r))

    # G4 core: a rebinding domain as Host is blocked — even GET reads
    for method in ("GET", "POST"):
        r = asyncio.run(_csrf_origin_guard(
            _req(method, "attacker.example:8001"), _call_next))
        check(f"rebinding host blocked ({method})",
              isinstance(r, JSONResponse) and r.status_code == 403)

    # the original Origin check still works on top
    r = asyncio.run(_csrf_origin_guard(
        _req("POST", "127.0.0.1:8001", "http://attacker.example:8001"),
        _call_next))
    check("cross-origin POST still blocked",
          isinstance(r, JSONResponse) and r.status_code == 403)
    r = asyncio.run(_csrf_origin_guard(
        _req("POST", "127.0.0.1:8001", "http://127.0.0.1:8001"),
        _call_next))
    check("same-origin POST passes", _is_pass(r))
    r = asyncio.run(_csrf_origin_guard(_req("POST", "127.0.0.1:8001"),
                                       _call_next))
    check("no-Origin POST passes (CLI/curl)", _is_pass(r))

    # env extension for deliberately non-local binds
    os.environ["HIVEMIND_TRUSTED_HOSTS"] = "hive.lan, 192.168.7.9"
    try:
        r = asyncio.run(_csrf_origin_guard(_req("POST", "hive.lan:8001"),
                                           _call_next))
        check("trusted host env accepted", _is_pass(r))
        r = asyncio.run(_csrf_origin_guard(_req("POST", "other.example"),
                                           _call_next))
        check("non-listed host still blocked", isinstance(r, JSONResponse))
    finally:
        os.environ.pop("HIVEMIND_TRUSTED_HOSTS", None)

    print()
    print(f"passed={passed} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
