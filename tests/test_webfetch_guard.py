# -*- coding: utf-8 -*-
"""web_fetch SSRF guard (audit G3): resolved-IP checks + port allowlist.

The pre-resolution URL guard judges IP literals only; G3 adds: resolve DNS
hostnames and judge EVERY resolved address with the same rules (a public
name answering with loopback/LAN IPs must not become a fetch target), plus
an 80/443 port allowlist so fetched URLs cannot probe loopback services by
hostname. Fully offline — getaddrinfo is monkeypatched.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import socket as _socket

from tools.websearch import _guard_fetch_target, _guard_fetch_url

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


_RESOLVE_TABLE = {
    "rebind.example": ["127.0.0.1"],
    "rebind6.example": ["::ffff:127.0.0.1"],
    "lan.example": ["192.168.1.10"],
    "multi.example": ["93.184.216.34", "10.0.0.5"],  # one bad among many
    "public.example": ["93.184.216.34"],
    "v6.example": ["2606:2800:220:1:248:1893:25c8:1946"],
}


def _fake_getaddrinfo(host, *args, **kwargs):
    host = str(host).lower()
    if host not in _RESOLVE_TABLE:
        raise _socket.gaierror(11001, "getaddrinfo failed")
    return [(2, 1, 6, "", (ip, 0)) for ip in _RESOLVE_TABLE[host]]


def main() -> int:
    orig = _socket.getaddrinfo
    _socket.getaddrinfo = _fake_getaddrinfo
    try:
        # the URL-literal guard still blocks what it always blocked
        check("literal loopback blocked",
              _guard_fetch_url("http://127.0.0.1/x") is not None)
        check("localhost blocked",
              _guard_fetch_url("http://localhost/x") is not None)
        check("v4-mapped literal blocked",
              _guard_fetch_url("http://[::ffff:127.0.0.1]/x") is not None)
        check("plain public literal passes",
              _guard_fetch_url("https://93.184.216.34/x") is None)

        # G3: hostnames are judged by their RESOLVED addresses
        out = asyncio.run(_guard_fetch_target("http://rebind.example/x"))
        check("rebinding name to 127.0.0.1 blocked",
              out is not None, f" [{out}]")
        out = asyncio.run(_guard_fetch_target("http://rebind6.example/x"))
        check("v4-mapped resolution blocked", out is not None, f" [{out}]")
        out = asyncio.run(_guard_fetch_target("http://lan.example/x"))
        check("LAN resolution blocked", out is not None, f" [{out}]")
        out = asyncio.run(_guard_fetch_target("http://multi.example/x"))
        check("one bad address among many blocks",
              out is not None, f" [{out}]")
        out = asyncio.run(_guard_fetch_target("http://public.example/x"))
        check("public resolution passes", out is None, f" [{out}]")
        out = asyncio.run(_guard_fetch_target("http://v6.example/x"))
        check("public IPv6 resolution passes", out is None, f" [{out}]")
        out = asyncio.run(
            _guard_fetch_target("http://unresolvable.invalid/x"))
        check("unresolvable passes the guard (request fails later)",
              out is None)

        # G3: port allowlist — no probing loopback services by hostname
        out = asyncio.run(
            _guard_fetch_target("http://public.example:8001/run/journal"))
        check("port 8001 blocked",
              out is not None and "port" in str(out), f" [{out}]")
        out = asyncio.run(
            _guard_fetch_target("http://public.example:8888/search"))
        check("port 8888 blocked", out is not None, f" [{out}]")
        out = asyncio.run(_guard_fetch_target("https://public.example/x"))
        check("default port passes", out is None, f" [{out}]")
    finally:
        _socket.getaddrinfo = orig

    print()
    print(f"passed={passed} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
