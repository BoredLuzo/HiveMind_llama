# -*- coding: utf-8 -*-
"""MCP protocol 2025-06-18 (2026-10-31): negotiation, sessions, GET-SSE stream.

Behavioural, in-process, no network beyond loopback and no live model:
  - initialize echoes 2025-06-18 / 2025-03-26, falls back to the 2024-11-05
    baseline for unknown (incl. newer) offers, never advertises newer;
  - a 2025+ initialize over HTTP issues a Mcp-Session-Id and every further
    POST of that generation requires it (missing/unknown -> 404);
  - GET /mcp opens the SSE stream: keep-alive comments, notification
    fan-out, Last-Event-ID replay; POSTs work concurrently;
  - DELETE /mcp ends a session (idempotent); idle-TTL sweep + cap eviction;
  - stdio needs no session; the 2024-11-05 byte-flow stays unchanged;
  - sampling/elicitation/completions stay unsupported (-32601).
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


import infra.mcp_server as mcp  # noqa: E402


def _init(ver):
    params = {} if ver is None else {"protocolVersion": ver}
    return asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}, "stdio"))


# ── 1. Protocol negotiation (transport-independent) ──────────────────────
check("1 2025-06-18 is echoed", _init("2025-06-18")["result"]["protocolVersion"] == "2025-06-18")
check("1 2025-03-26 is echoed verbatim",
      _init("2025-03-26")["result"]["protocolVersion"] == "2025-03-26")
check("1 2024-11-05 is still echoed",
      _init("2024-11-05")["result"]["protocolVersion"] == "2024-11-05")
check("1 unknown version falls back to the baseline",
      _init("1999-01-01")["result"]["protocolVersion"] == "2024-11-05")
check("1 missing version falls back to the baseline",
      _init(None)["result"]["protocolVersion"] == "2024-11-05")
check("1 a version NEWER than 2025-06-18 is never advertised",
      _init("2026-01-01")["result"]["protocolVersion"] == "2024-11-05")
_caps = _init("2025-06-18")["result"]["capabilities"]
check("1 capabilities stay tools+resources+prompts",
      set(_caps) == {"tools", "resources", "prompts"}, f"({sorted(_caps)})")

for _m in ("sampling/createMessage", "elicitation/create", "completion/complete"):
    _r = asyncio.run(mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": _m}, "stdio"))
    check(f"1 {_m} stays -32601", _r.get("error", {}).get("code") == -32601)

check("1 stdio 2025 initialize creates no session", len(mcp._SESSIONS) == 0)


# ── 2-7. HTTP transport: sessions, stream, DELETE, cap, TTL ──────────────
def _post(obj, extra_headers: str = "") -> bytes:
    body = json.dumps(obj).encode()
    head = (f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            f"{extra_headers}Content-Length: {len(body)}\r\n\r\n")
    return head.encode() + body


def _header(resp: bytes, name: str) -> str:
    head = resp.split(b"\r\n\r\n", 1)[0].decode("utf-8", "replace")
    for line in head.split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip().lower() == name.lower():
                return v.strip()
    return ""


def _status(resp: bytes) -> str:
    return resp.split(b"\r\n", 1)[0].decode("utf-8", "replace").strip()


async def _exchange(port, raw: bytes, timeout: float = 10.0) -> bytes:
    """One request -> full response (reads up to Content-Length)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        clen = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                clen = int(line.split(b":", 1)[1].strip() or 0)
        while len(rest) < clen:
            chunk = await asyncio.wait_for(reader.read(clen - len(rest)), timeout=timeout)
            if not chunk:
                break
            rest += chunk
        return head + b"\r\n\r\n" + rest
    finally:
        writer.close()


async def _read_until(reader, buf: bytearray, marker: bytes, timeout: float = 5.0) -> bool:
    """Accumulate into buf until marker appears; False on EOF/deadline."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while marker not in buf:
        if loop.time() >= deadline:
            return False
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        except (ConnectionError, OSError):
            return False
        if not chunk:
            return False
        buf.extend(chunk)
    return True


async def _http_suite():
    server = await asyncio.start_server(mcp._http_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        # ── 2. session issued + required ──────────────────────────────
        resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                            "params": {"protocolVersion": "2025-06-18"}}))
        check("2 2025 initialize answers 200", resp.startswith(b"HTTP/1.1 200"))
        sid = _header(resp, "Mcp-Session-Id")
        check("2 2025 initialize issues Mcp-Session-Id", len(sid) >= 32, f"(len={len(sid)})")
        check("2 session id is a urlsafe token",
              sid and all(c.isalnum() or c in "-_" for c in sid))
        check("2 initialize echoes 2025-06-18", b'"protocolVersion": "2025-06-18"' in resp)

        resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 2, "method": "ping"},
                                           f"Mcp-Session-Id: {sid}\r\n"))
        check("2 POST with the valid session works", b'"result"' in resp)

        resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 3, "method": "ping"},
                                           "MCP-Protocol-Version: 2025-06-18\r\n"))
        check("2 2025 POST without session id -> 404", _status(resp).startswith("HTTP/1.1 404"))
        check("2 the 404 carries a jsonrpc -32001 body",
              b"-32001" in resp and b"session" in resp)

        resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 4, "method": "ping"},
                                           "Mcp-Session-Id: bogus-session-id\r\n"))
        check("2 POST with an unknown session -> 404", _status(resp).startswith("HTTP/1.1 404"))

        resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 5, "method": "ping"}))
        check("2 headerless legacy POST unchanged", b'"result"' in resp)

        resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 6, "method": "initialize",
                                            "params": {"protocolVersion": "2024-11-05"}}))
        check("2 2024 initialize: echoed, no session header",
              b'"protocolVersion": "2024-11-05"' in resp and _header(resp, "Mcp-Session-Id") == "")

        resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 7, "method": "initialize",
                                            "params": {"protocolVersion": "2026-01-01"}}))
        check("2 newer-than-2025 initialize: baseline, no session header",
              b'"protocolVersion": "2024-11-05"' in resp and _header(resp, "Mcp-Session-Id") == "")

        resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 8, "method": "initialize",
                                            "params": {"protocolVersion": "2025-03-26"}}))
        check("2 2025-03-26 initialize: echoed verbatim + session",
              b'"protocolVersion": "2025-03-26"' in resp and _header(resp, "Mcp-Session-Id") != "")

        # ── 3. GET stream: connect, concurrent POST, keep-alive, fan-out ─
        old_keep = mcp.MCP_SSE_KEEPALIVE_S
        mcp.MCP_SSE_KEEPALIVE_S = 0.2
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         f"Mcp-Session-Id: {sid}\r\n\r\n".encode())
            await writer.drain()
            buf = bytearray()
            check("3 GET stream answers 200 + text/event-stream",
                  await _read_until(reader, buf, b"\r\n\r\n")
                  and bytes(buf).startswith(b"HTTP/1.1 200")
                  and b"text/event-stream" in bytes(buf))

            resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 10, "method": "ping"},
                                               f"Mcp-Session-Id: {sid}\r\n"))
            check("3 POST works while the stream is open", b'"result"' in resp)

            check("3 keep-alive comment received",
                  await _read_until(reader, buf, b": keep-alive"))

            sess = mcp._session_get_alive(sid)
            mcp._session_append_event(sess, "notifications/progress",
                                      {"progress": 7, "status": "step"})
            check("3 notification fan-out reaches the stream",
                  await _read_until(reader, buf, b"notifications/progress"))
            check("3 fan-out is a proper id+message frame",
                  b"id: " in buf and b"event: message" in buf and b'"progress": 7' in buf)
            writer.close()
        finally:
            mcp.MCP_SSE_KEEPALIVE_S = old_keep

        # ── 4. Last-Event-ID replay ───────────────────────────────────
        sess = mcp._session_get_alive(sid)
        s1 = mcp._session_append_event(sess, "notifications/message", {"v": "e1"})
        mcp._session_append_event(sess, "notifications/message", {"v": "e2"})
        s3 = mcp._session_append_event(sess, "notifications/message", {"v": "e3"})
        reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
        writer2.write(f"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nMcp-Session-Id: {sid}\r\n"
                      f"Last-Event-ID: {s1}\r\n\r\n".encode())
        await writer2.drain()
        buf2 = bytearray()
        await _read_until(reader2, buf2, b'"v": "e3"')
        check("4 replay returns only the missed events",
              b'"v": "e2"' in buf2 and b'"v": "e3"' in buf2
              and b'"v": "e1"' not in buf2,
              f"(ids {s1}..{s3})")
        mcp._session_append_event(sess, "notifications/message", {"v": "e4"})
        check("4 live events follow the replay",
              await _read_until(reader2, buf2, b'"v": "e4"'))
        writer2.close()

        # GET without / unknown session falls back to the legacy 404 shape
        resp = await _exchange(port, b"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        check("4 GET /mcp without session -> legacy 404",
              _status(resp).startswith("HTTP/1.1 404") and b"event-stream" not in resp)
        resp = await _exchange(port, b"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                     b"Mcp-Session-Id: bogus\r\n\r\n")
        check("4 GET /mcp with an unknown session -> 404",
              _status(resp).startswith("HTTP/1.1 404"))

        # ── 5. DELETE ends the session (idempotent) ───────────────────
        resp = await _exchange(port, f"DELETE /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                     f"Mcp-Session-Id: {sid}\r\n\r\n".encode())
        check("5 DELETE with a valid session -> 204", _status(resp).startswith("HTTP/1.1 204"))
        resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 11, "method": "ping"},
                                           f"Mcp-Session-Id: {sid}\r\n"))
        check("5 POST after DELETE -> 404", _status(resp).startswith("HTTP/1.1 404"))
        resp = await _exchange(port, f"DELETE /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                     f"Mcp-Session-Id: {sid}\r\n\r\n".encode())
        check("5 second DELETE stays idempotent (204)",
              _status(resp).startswith("HTTP/1.1 204"))
        resp = await _exchange(port, b"DELETE /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                     b"Mcp-Session-Id: never-existed\r\n\r\n")
        check("5 unknown-session DELETE is idempotent (204)",
              _status(resp).startswith("HTTP/1.1 204"))

        # ── 6. session cap evicts the oldest ──────────────────────────
        old_max = mcp.MCP_SESSION_MAX
        try:
            mcp.MCP_SESSION_MAX = 3
            ids = []
            for i in range(4):
                resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 20 + i,
                                                    "method": "initialize",
                                                    "params": {"protocolVersion": "2025-06-18"}}))
                ids.append(_header(resp, "Mcp-Session-Id"))
            check("6 four initializes under cap=3 issue distinct ids",
                  len(set(ids)) == 4 and all(ids))
            resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 30, "method": "ping"},
                                               f"Mcp-Session-Id: {ids[0]}\r\n"))
            check("6 the oldest session is evicted -> clean 404",
                  _status(resp).startswith("HTTP/1.1 404"))
            resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 31, "method": "ping"},
                                               f"Mcp-Session-Id: {ids[3]}\r\n"))
            check("6 the newest session stays alive", b'"result"' in resp)
        finally:
            mcp.MCP_SESSION_MAX = old_max

        # ── 7. idle-TTL sweep (injectable clock) ──────────────────────
        resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 40, "method": "initialize",
                                            "params": {"protocolVersion": "2025-06-18"}}))
        sid7 = _header(resp, "Mcp-Session-Id")
        sess7 = mcp._session_get_alive(sid7)
        check("7 session is registered", sess7 is not None)
        sess7.last_seen = mcp._session_clock() - (mcp.MCP_SESSION_TTL_S + 10)
        n = mcp._sessions_sweep()
        check("7 the sweep drops the idle session",
              sid7 not in mcp._SESSIONS and n >= 1, f"(swept={n})")
        resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 41, "method": "ping"},
                                           f"Mcp-Session-Id: {sid7}\r\n"))
        check("7 an expired session -> 404 on access", _status(resp).startswith("HTTP/1.1 404"))

        resp = await _exchange(port, _post({"jsonrpc": "2.0", "id": 42, "method": "initialize",
                                            "params": {"protocolVersion": "2025-06-18"}}))
        sid7b = _header(resp, "Mcp-Session-Id")
        orig_clock = mcp._session_clock
        try:
            mcp._session_clock = lambda: orig_clock() + 999999.0
            mcp._sessions_sweep()
            check("7 an injectable clock expires the session", sid7b not in mcp._SESSIONS)
        finally:
            mcp._session_clock = orig_clock

        # ── 8. stdio needs no session; legacy byte-flow unchanged ─────
        r = await mcp.handle_request({"jsonrpc": "2.0", "id": 50, "method": "initialize",
                                      "params": {"protocolVersion": "2025-06-18"}}, "stdio")
        check("8 stdio 2025 initialize works without a session",
              r["result"]["protocolVersion"] == "2025-06-18")
        check("8 stdio creates no session", len(mcp._SESSIONS) == 0)
        r = await mcp.handle_request({"jsonrpc": "2.0", "id": 51, "method": "ping"}, "stdio")
        check("8 stdio ping unchanged", r.get("result") == {})
        r = await mcp.handle_request({"jsonrpc": "2.0", "id": 52, "method": "initialize",
                                      "params": {"protocolVersion": "2024-11-05"}}, "stdio")
        check("8 stdio 2024 initialize unchanged",
              r["result"]["protocolVersion"] == "2024-11-05"
              and set(r["result"]["capabilities"]) == {"tools", "resources", "prompts"})
    finally:
        server.close()
        mcp._SESSIONS.clear()


asyncio.run(_http_suite())

print()
print(f"SUMMARY mcp_2025: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
