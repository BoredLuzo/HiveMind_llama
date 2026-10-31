"""Haertungstests fuer den MCP-Server (infra/mcp_server.py).

Deckt die Audit-Befunde ab:
  - Pfad-Containment: `..`/absolut/NUL -> PATH_OUTSIDE_WORKSPACE (kein stilles
    Umschreiben mehr)
  - Governance: ein Write-Tool ist auch ueber den Dispatch-Pfad gesperrt
  - JSON-RPC: kaputte params/arguments, unbekannte Methoden, Batch-Limit
  - HTTP: Header-Limit (431), Body-Limit (413), Health-Endpoint, Auth
  - stdio: zu lange Zeile wird beantwortet statt verschluckt
Ohne Netzwerk nach aussen - der HTTP-Test laeuft gegen einen echten lokalen
Server auf einem Ephemeral-Port.
"""
from __future__ import annotations

import asyncio
import json
import os
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

# ── 1. Pfad-Containment ──────────────────────────────────────────────────
WS = Path(mcp.WORKSPACE).resolve()
inside = mcp._resolve_path("sub/dir/file.py")
check("relative path resolves inside workspace", str(inside).startswith(str(WS)), f"({inside})")

for bad, label in (
    ("../../etc/passwd", "parent traversal"),
    ("..\\..\\windows\\system32\\x.dll", "windows traversal"),
    ("/etc/passwd", "posix absolute"),
    ("C:\\Windows\\System32\\evil.exe", "windows absolute"),
    ("bad\x00name.txt", "NUL byte"),
    ("", "empty"),
):
    try:
        mcp._resolve_path(bad)
        check(f"rejects {label}", False, "(no exception)")
    except mcp.ToolPathError as exc:
        check(f"rejects {label}", "outside the workspace" in str(exc) or "NUL" in str(exc)
              or "empty" in str(exc), f"({str(exc)[:60]})")

# Ein Pfad, der wie eine Flucht aussieht, aber innerhalb bleibt, muss gehen
ok_path = mcp._resolve_path("a/../b.txt")
check("normalises a path that stays inside", ok_path == (WS / "b.txt").resolve(), f"({ok_path})")

# ── 2. Governance im Dispatch-Pfad ───────────────────────────────────────
os.environ.pop("MCP_ALLOW_WRITE", None)
allowed, reason = mcp._tool_allowed("write_file", "http")
check("write_file denied without opt-in", allowed is False and "MCP_ALLOW_WRITE" in reason)
allowed, _ = mcp._tool_allowed("read_file", "http")
check("read_file allowed", allowed is True)
allowed, reason = mcp._tool_allowed("run_bash", "http")
check("exec never over http", allowed is False and "HTTP" in reason)

_dispatch_out = asyncio.run(mcp._dispatch_tool("write_file", {"path": "x", "content": "y"},
                                              local_handler=None, transport="http"))
check("dispatch path also enforces the tier gate",
      "TOOL_NOT_ALLOWED" in _dispatch_out, f"({_dispatch_out[:60]})")

# ── 3. JSON-RPC-Validierung ──────────────────────────────────────────────
async def _call(params):
    return await mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": params}, "stdio")


r = asyncio.run(_call("nope"))
check("non-object params -> -32602", r["error"]["code"] == -32602, f"({r.get('error')})")
r = asyncio.run(_call({"name": "read_file", "arguments": "nope"}))
check("non-object arguments -> -32602", r["error"]["code"] == -32602, f"({r.get('error')})")
r = asyncio.run(_call({"arguments": {}}))
check("missing tool name -> -32602", r["error"]["code"] == -32602, f"({r.get('error')})")
r = asyncio.run(mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "does/not/exist"}, "stdio"))
check("unknown method -> -32601", r["error"]["code"] == -32601)
r = asyncio.run(mcp.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "nope", "arguments": {}}}, "stdio"))
# S9 (2026-10-31): unknown tool is Invalid Params (-32602), not "method not found"
check("unknown tool -> -32602", r["error"]["code"] == -32602)

# tools/list darf nichts zeigen, was der Transport nicht darf
lst = asyncio.run(mcp.handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                                     "http"))
names = {t.get("name") for t in lst["result"]["tools"]}
check("http tools/list hides exec tools", not (names & {"run_bash", "shell", "run_python"}),
      f"({sorted(names)[:6]})")
check("http tools/list hides write tools without opt-in", "write_file" not in names)

# ── 4. HTTP-Transport (echter Server auf Port 0) ─────────────────────────
async def _http_probe():
    server = await asyncio.start_server(mcp._http_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def raw(request: bytes, read_bytes: int = 65536) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(read_bytes), timeout=15)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        return data

    results = {}
    health = await raw(b"GET /mcp/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    results["health"] = health
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    ok = await raw(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                   b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    results["ping"] = ok
    bad_host = await raw(b"GET /mcp/health HTTP/1.1\r\nHost: evil.example\r\n\r\n")
    results["bad_host"] = bad_host
    huge = await raw(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 999999999\r\n\r\n")
    results["huge"] = huge
    # Header-Block ueber dem Limit: 431
    big_hdr = (b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n" +
               b"X-Pad: " + b"a" * (mcp.MCP_MAX_HEADER_BYTES + 1024) + b"\r\n\r\n")
    results["big_header"] = await raw(big_hdr)
    server.close()
    try:
        await server.wait_closed()
    except (ConnectionError, OSError):
        pass
    return results


res = asyncio.run(_http_probe())
check("health endpoint answers 200", b"200 OK" in res["health"] and b"hivemind-mcp" in res["health"])
check("ping answers a jsonrpc result", b'"result"' in res["ping"])
check("non-loopback Host rejected (403)", b"403" in res["bad_host"])
check("oversized Content-Length rejected (413)", b"413" in res["huge"])
check("oversized header block rejected (431)", b"431" in res["big_header"], f"({res['big_header'][:40]})")

# ── 5. stdio-Zeilenlimit + Audit-Log ─────────────────────────────────────
src = (ROOT / "infra" / "mcp_server.py").read_text(encoding="utf-8", errors="replace")
check("stdio answers an oversized line instead of dropping it",
      "request line too long" in src and "-32600" in src)
check("audit log helper exists", "def _audit_tool_call(" in src)
check("audit log is emitted in tools/call", "[MCP-CALL]" in src)
check("stdio notifier serialises output", "with lock:" in src)

# ── 6. Limits sind konfigurierbar ────────────────────────────────────────
check("header/body/concurrency/batch limits defined",
      all(hasattr(mcp, n) for n in ("MCP_MAX_HEADER_BYTES", "MCP_MAX_BODY_BYTES",
                                    "MCP_MAX_CONCURRENCY", "MCP_MAX_BATCH",
                                    "MCP_REQUEST_TIMEOUT_S")),
      f"({mcp.MCP_MAX_HEADER_BYTES}, {mcp.MCP_MAX_CONCURRENCY}, {mcp.MCP_MAX_BATCH})")


# ── 7. Engine-Endpunkt /internal/tool/exec (der Weg, den MCP/IDE nimmt) ──
import routers.core as engine_core  # noqa: E402

os.environ.pop("HIVEMIND_INTERNAL_TOKEN", None)
ok_exec, why_exec = engine_core._internal_exec_allowed("run_bash")
check("exec over HTTP needs a token", ok_exec is False and "HIVEMIND_INTERNAL_TOKEN" in why_exec)
ok_read, _ = engine_core._internal_exec_allowed("read_file")
check("read stays available without a token", ok_read is True)
for name in ("shell", "run_bash", "run_python"):
    check(f"{name} gated", engine_core._internal_exec_allowed(name)[0] is False)
os.environ["HIVEMIND_INTERNAL_TOKEN"] = "probe-token"
check("exec allowed once a token is configured",
      engine_core._internal_exec_allowed("run_bash")[0] is True)
os.environ.pop("HIVEMIND_INTERNAL_TOKEN", None)
check("loopback Host accepted", engine_core._internal_host_allowed("127.0.0.1:8001") is True)
check("localhost accepted", engine_core._internal_host_allowed("localhost:8001") is True)
check("foreign Host rejected (DNS rebinding)", engine_core._internal_host_allowed("evil.example") is False)
check("empty Host tolerated (HTTP/1.0 client)", engine_core._internal_host_allowed("") is True)
_src_core = (ROOT / "routers" / "core.py").read_text(encoding="utf-8", errors="replace")
check("exec endpoint logs its calls", "[INTERNAL-EXEC] tool=" in _src_core)
check("exec endpoint rejects non-loopback hosts", "Host not allowed" in _src_core)

# ── 8. Audit round 2 (2026-10-31): JSON-RPC discipline, timeout abort, ────
#    connection limits, protocol negotiation
import io                                               # noqa: E402
import contextlib                                       # noqa: E402

# 8a S9: a message without "id" is a notification and gets no answer (the
# server used to reply with id:null).
_r = asyncio.run(mcp.handle_request({"jsonrpc": "2.0", "method": "ping"}, "stdio"))
check("8a notification (no id) gets no response", _r is None, f"({_r!r})")
_r = asyncio.run(mcp.handle_request({"jsonrpc": "2.0", "method": "tools/call",
                                     "params": {"name": "read_file", "arguments": {}}}, "stdio"))
check("8a2 tools/call without id is not executed/answered", _r is None, f"({_r!r})")

# 8b S9: non-dict requests (scalar batch items) must not raise AttributeError
# but get a -32600 answer.
_r = asyncio.run(mcp.handle_request(1))
check("8b non-dict request -> -32600, no exception",
      isinstance(_r, dict) and _r["error"]["code"] == -32600, f"({_r!r})")
for _bad in (1, "x", None, [1, 2]):
    _r = asyncio.run(mcp._handle_rpc_item(_bad))
    _ok = isinstance(_r, dict) and _r.get("error", {}).get("code") == -32600
    check(f"8b2 batch item {type(_bad).__name__} -> -32600 answer", _ok, f"({_r!r})")
_r = asyncio.run(mcp._handle_rpc_item({"jsonrpc": "2.0", "id": 7, "method": "ping"}))
check("8b3 dict batch item still answered", _r is not None and _r.get("result") == {})

# 8c S8: notifications/cancelled is registered; a request with that id is no
# longer executed/answered.
mcp._CANCELLED_REQUESTS.clear()
_r = asyncio.run(mcp.handle_request({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                     "params": {"requestId": "cancel-1"}}, "stdio"))
check("8c cancelled notification stays unanswered", _r is None)
check("8c2 the requestId is registered", "cancel-1" in mcp._CANCELLED_REQUESTS)
_r = asyncio.run(mcp.handle_request({"jsonrpc": "2.0", "id": "cancel-1",
                                     "method": "ping"}, "stdio"))
check("8c3 a request whose id was cancelled is dropped", _r is None, f"({_r!r})")
_r = asyncio.run(mcp.handle_request({"jsonrpc": "2.0", "id": "keep-me",
                                     "method": "ping"}, "stdio"))
check("8c4 an unrelated request still answers",
      _r is not None and _r.get("result") == {})
mcp._CANCELLED_REQUESTS.clear()

# 8d S7: the timeout of an HTTP request now writes the [MCP-CALL] line
# (previously skipped exactly when the call was long).
_orig_handle = mcp.handle_request
_orig_timeout = mcp.MCP_REQUEST_TIMEOUT_S


async def _slow_handle(req, transport="stdio"):
    await asyncio.sleep(1.0)


async def _timeout_probe():
    server = await asyncio.start_server(mcp._http_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "read_file", "arguments": {"path": "x"}}}).encode()
    writer.write(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                 b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=15)
    writer.close()
    server.close()
    return data


mcp.handle_request = _slow_handle
mcp.MCP_REQUEST_TIMEOUT_S = 0.2
_err_cap = io.StringIO()
try:
    with contextlib.redirect_stderr(_err_cap):
        _tout = asyncio.run(_timeout_probe())
finally:
    mcp.handle_request = _orig_handle
    mcp.MCP_REQUEST_TIMEOUT_S = _orig_timeout
check("8d timed-out request answers a jsonrpc error",
      b"-32000" in _tout and b"timed out" in _tout, f"({_tout[:60]!r})")
check("8d2 the timeout writes the [MCP-CALL] audit line",
      "[MCP-CALL]" in _err_cap.getvalue() and "request_timeout" in _err_cap.getvalue(),
      f"({_err_cap.getvalue().strip()[:80]!r})")

# 8e S10: the isError heuristic now only matches our own error envelope,
# not legitimate file contents that happen to start with "[".
check("8e plain bracketed content with an error word is not isError",
      mcp._looks_like_error_text("[TODO] fix the not found handling later") is False)
check("8e2 markdown link content is not isError",
      mcp._looks_like_error_text("[See the timeout docs](x.md)") is False)
check("8e3 inline TOOL_ERROR envelope is isError",
      mcp._looks_like_error_text("[TOOL_ERROR:READ_REQUIRED] fix first") is True)
check("8e4 lowercase envelope is isError",
      mcp._looks_like_error_text("[tool_error] {\"error\":{}}") is True)
check("8e5 empty/non-string is not isError", mcp._looks_like_error_text("") is False)

# 8f S12: stdio line cap == HTTP body cap ( big write_file payloads go
# through both transports ).
_src_mcp = (ROOT / "infra" / "mcp_server.py").read_text(encoding="utf-8", errors="replace")
check("8f stdio line cap shares the HTTP body cap",
      "_buf_max = MCP_MAX_BODY_BYTES" in _src_mcp and "1024 * 512" not in _src_mcp)

# 8g S13: body-read timeout (408) and connection cap (503).
check("8g limits configured",
      mcp.MCP_BODY_TIMEOUT_S >= 1.0 and mcp.MCP_MAX_CONNECTIONS >= 1,
      f"(body={mcp.MCP_BODY_TIMEOUT_S}s, conns={mcp.MCP_MAX_CONNECTIONS})")

async def _partial_body_probe():
    mcp.MCP_BODY_TIMEOUT_S = 0.5
    server = await asyncio.start_server(mcp._http_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    # Headers promise a 64-byte body, only 8 arrive - the read used to hang
    # forever (slowloris).
    writer.write(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 64\r\n\r\n"
                 b"{\"partial\"")
    await writer.drain()
    try:
        data = await asyncio.wait_for(reader.read(65536), timeout=10)
    except asyncio.TimeoutError:
        data = b"(no answer within 10s)"
    writer.close()
    server.close()
    return data


try:
    _t408 = asyncio.run(_partial_body_probe())
finally:
    mcp.MCP_BODY_TIMEOUT_S = max(1.0, float(os.environ.get("MCP_BODY_TIMEOUT_S", "30") or 30))
check("8g2 incomplete body answered with 408", b"408" in _t408, f"({_t408[:40]!r})")

async def _conn_cap_probe():
    _orig_sem = mcp._CONN_SEM
    mcp._CONN_SEM = asyncio.Semaphore(1)
    await mcp._CONN_SEM.acquire()          # cap artificially exhausted
    try:
        server = await asyncio.start_server(mcp._http_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /mcp/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(65536), timeout=10)
        writer.close()
        server.close()
        return data
    finally:
        mcp._CONN_SEM.release()
        mcp._CONN_SEM = _orig_sem


_t503 = asyncio.run(_conn_cap_probe())
check("8g3 a connection beyond the cap gets 503", b"503" in _t503, f"({_t503[:40]!r})")

# 8h S14: initialize negotiates the protocol version instead of hardcoding
# it.
_r = asyncio.run(mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2024-11-05"}}, "stdio"))
check("8h supported version is echoed",
      _r["result"]["protocolVersion"] == "2024-11-05", f"({_r['result']!r})")
_r = asyncio.run(mcp.handle_request({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                                     "params": {"protocolVersion": "1999-01-01"}}, "stdio"))
check("8h2 unknown version falls back to the baseline",
      _r["result"]["protocolVersion"] == "2024-11-05", f"({_r['result']!r})")
_r = asyncio.run(mcp.handle_request({"jsonrpc": "2.0", "id": 3, "method": "initialize",
                                     "params": {}}, "stdio"))
check("8h3 missing version falls back to the baseline",
      _r["result"]["protocolVersion"] == "2024-11-05")

# 8i S11: the tool name in the [INTERNAL-EXEC] log lines is single-line
# (no log forgery via newlines).
import routers.core as _rc8                             # noqa: E402
_safe = _rc8._log_safe_name("read_file\nfake line ok=True\r\nanother")
check("8i log name is flattened to one line",
      "\n" not in _safe and "\r" not in _safe and "another" in _safe, f"({_safe!r})")
check("8i2 the endpoint log calls use the sanitizer",
      "_log_safe_name(name)" in _src_core)

# 8j S6: doc drift fixed (docs/mcp.md now describes the real behaviour).
_doc = (ROOT / "docs" / "mcp.md").read_text(encoding="utf-8", errors="replace")
_doc_flat = " ".join(_doc.split())
check("8j query is documented as opt-in on BOTH transports",
      "both transports" in _doc and "MCP_ALLOW_QUERY" in _doc)
check("8j2 absolute paths inside the workspace are documented as allowed",
      "absolute paths that resolve inside the workspace are allowed" in _doc_flat
      and "are all rejected" not in _doc_flat)
check("8j3 MCP_SHARED_* envs are documented",
      all(k in _doc for k in ("MCP_SHARED_TOOL_ENABLED", "MCP_SHARED_TOOL_MODE",
                              "MCP_SHARED_INCLUDE_WEBSEARCH")))
check("8j4 engine exec is documented as HIVEMIND_INTERNAL_TOKEN-only",
      "HIVEMIND_INTERNAL_TOKEN" in _doc
      and "MCP_HTTP_TOKEN or HIVEMIND_INTERNAL_TOKEN" not in _doc)

print()
print(f"SUMMARY mcp_hardening: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
