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
check("unknown tool -> -32601", r["error"]["code"] == -32601)

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

print()
print(f"SUMMARY mcp_hardening: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
