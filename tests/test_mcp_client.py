# -*- coding: utf-8 -*-
"""MCP client (beta): stdio handshake, tool defs, calls, failure modes.

Drives the real client against a real child process (a small stdlib MCP
server written to a temp dir), so protocol framing, timeouts and process
cleanup are actually exercised. No network, no model.

Every client runs inside ONE event loop: a subprocess transport belongs to
the loop that created it.
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
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


from infra import mcp_client as MC  # noqa: E402

FAKE = r'''
import json, sys, time

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"

def send(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        if MODE == "die":
            sys.exit(0)
        if MODE == "hang":
            time.sleep(30)
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0"}}})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": "echo", "description": "Echo the text back",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}},
                             "required": ["text"]}},
            {"name": "boom", "description": "Always fails",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "big", "description": "Huge output",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "reject", "description": "Server rejects the call",
             "inputSchema": {"type": "object", "properties": {}}},
        ]}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text", "text": "echo: " + str(args.get("text", ""))}]}})
        elif name == "boom":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "isError": True, "content": [{"type": "text", "text": "tool exploded"}]}})
        elif name == "reject":
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32602, "message": "bad arguments"}})
        elif name == "big":
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text", "text": "x" * 400000}]}})
        else:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "unknown tool"}})
'''


def spec(name: str, mode: str = "normal") -> dict:
    d = Path(tempfile.mkdtemp(prefix="hm_mcp_"))
    script = d / f"server_{mode}.py"
    script.write_text(FAKE, encoding="utf-8")
    return {"name": name, "command": sys.executable,
            "args": [str(script), mode], "enabled": True}


def settings_with(*servers: dict, enabled: bool = True, timeout_s: float = 5.0) -> dict:
    return {"mcp_client_enabled": enabled,
            "mcp_client_timeout_s": timeout_s,
            "mcp_servers": list(servers)}


# ── 1. Ausschalter ------------------------------------------------------
check("1a disabled by default", MC.McpClient.enabled({}) is False)
check("1b enabled flag is honoured", MC.McpClient.enabled({"mcp_client_enabled": True}) is True)
check("1c tool_defs is empty while disabled", MC.tool_defs({}) == [])


async def _disabled_flow():
    client = MC.McpClient()
    st = await client.refresh(settings_with(enabled=False))
    return st, client.tool_defs()


st, defs = asyncio.run(_disabled_flow())
check("1d refresh while disabled starts nothing", st.get("enabled") is False and defs == [])


# ── 2. Handshake, tools/list, defs, calls ------------------------------
async def _live_flow():
    client = MC.McpClient()
    conf = settings_with(spec("fake"))
    st = await client.refresh(conf)
    out = {}
    out["st"] = st
    out["defs"] = client.tool_defs()
    out["echo"] = await client.call("mcp_fake_echo", {"text": "hi"})
    out["boom"] = await client.call("mcp_fake_boom", {})
    out["big"] = await client.call("mcp_fake_big", {})
    out["reject"] = await client.call("mcp_fake_reject", {})
    out["missing"] = await client.call("mcp_fake_missing", {})
    out["unknown"] = await client.call("mcp_nope_x", {})
    conn = client._conns.get("fake")
    out["proc"] = conn._proc if conn else None
    await client.close_all()
    out["rc"] = getattr(out["proc"], "returncode", None)
    out["conns"] = dict(client._conns)
    return out


res = asyncio.run(_live_flow())
srv = res["st"]["servers"][0]
check("2a the server reports ready", srv["ok"] is True, f"({srv['error']})")
check("2b four tools listed", srv["tools"] == 4, f"({srv['tools']})")
names = [d["function"]["name"] for d in res["defs"]]
check("2c tool names are namespaced", "mcp_fake_echo" in names, f"({names})")
echo = next(d for d in res["defs"] if d["function"]["name"] == "mcp_fake_echo")
check("2d description is tagged", echo["function"]["description"].startswith("[mcp:fake]"))
check("2e input schema is passed through",
      echo["function"]["parameters"]["properties"]["text"]["type"] == "string")

check("3a call returns the text content", res["echo"] == "echo: hi", f"({res['echo']!r})")
check("3b isError becomes a tool error",
      res["boom"].startswith("[TOOL_ERROR: MCP_TOOL_ERROR]"), f"({res['boom'][:40]!r})")
check("3c the error text survives", "tool exploded" in res["boom"])
check("3d huge output is capped", len(res["big"]) <= MC.MAX_OUTPUT_CHARS + 40,
      f"(len={len(res['big'])})")
check("3e a name that no server lists is refused client-side",
      res["missing"].startswith("[TOOL_ERROR: MCP_UNKNOWN_TOOL]"), f"({res['missing'][:50]!r})")
check("3e2 a JSON-RPC error from the server becomes a tool error",
      res["reject"].startswith("[TOOL_ERROR: MCP_CALL_FAILED]") and "bad arguments" in res["reject"],
      f"({res['reject'][:60]!r})")
check("3f unknown name is explicit", res["unknown"].startswith("[TOOL_ERROR: MCP_UNKNOWN_TOOL]"))
check("6a the child was running", res["proc"] is not None)
check("6b close_all stops the child", res["rc"] is not None, f"(rc={res['rc']})")
check("6c the client forgets the connection", res["conns"] == {})


# ── 4. Server, der beim Start stirbt -----------------------------------
async def _die_flow():
    client = MC.McpClient()
    st = await client.refresh(settings_with(spec("dying", "die")))
    out = await client.call("mcp_dying_echo", {})
    await client.close_all()
    return st, out


st, out = asyncio.run(_die_flow())
check("4b a server that dies during init is reported, not raised",
      st["servers"][0]["ok"] is False, f"({st['servers'][0]['error'][:60]!r})")
check("4c calling it yields a tool error", out.startswith("[TOOL_ERROR:"), f"({out[:50]!r})")


# ── 5. Timeout ----------------------------------------------------------
async def _hang_flow():
    client = MC.McpClient()
    t0 = time.time()
    st = await client.refresh(settings_with(spec("hang", "hang"), timeout_s=1.0))
    dt = time.time() - t0
    await client.close_all()
    return st, dt


st, dt = asyncio.run(_hang_flow())
check("5a a hanging server times out instead of blocking",
      st["servers"][0]["ok"] is False and dt < 12, f"(dt={dt:.1f}s)")
check("5b the error names the timeout",
      "timed out" in st["servers"][0]["error"].lower(), f"({st['servers'][0]['error'][:60]!r})")


# ── 7. Namen und Specs -------------------------------------------------
check("7a names are sanitized", MC._tool_name("My Server/1", "Do It!") == "mcp_my_server_1_do_it")
_st = asyncio.run(MC.McpClient().refresh(settings_with({"name": "broken", "args": []})))
check("7b a server without a command fails cleanly", "no 'command'" in str(_st["servers"][0]["error"]))
_st = asyncio.run(MC.McpClient().refresh(
    settings_with({"name": "nope", "command": "definitely_not_a_binary_xyz"})))
check("7c a missing binary fails cleanly", "not found" in str(_st["servers"][0]["error"]))
check("7d more than MAX_SERVERS is capped",
      len(MC.McpClient._specs({"mcp_servers": [{"name": f"s{i}", "command": "x"}
                                               for i in range(20)]})) == MC.MAX_SERVERS)

# ── 8. Dispatch-Verdrahtung --------------------------------------------
from tools import runner as R  # noqa: E402

_orig_enabled = MC.McpClient.enabled
_orig_call = MC.call


async def _fake_call(name, args):
    return f"[stub] {name} {sorted((args or {}).keys())}"


MC.McpClient.enabled = staticmethod(lambda settings=None: False)  # type: ignore[assignment]
out = asyncio.run(R._run_inline_tool("mcp_fake_echo", {"text": "x"}))
check("8a dispatch refuses MCP tools while the client is off",
      "MCP_DISABLED" in out, f"({out[:70]!r})")

MC.McpClient.enabled = staticmethod(lambda settings=None: True)  # type: ignore[assignment]
MC.call = _fake_call  # type: ignore[assignment]
out = asyncio.run(R._run_inline_tool("mcp_fake_echo", {"text": "x"}))
check("8b dispatch routes mcp_ names to the client",
      out == "[stub] mcp_fake_echo ['text']", f"({out[:60]!r})")

MC.McpClient.enabled = _orig_enabled  # type: ignore[assignment]
MC.call = _orig_call  # type: ignore[assignment]

# ── 9. Router -----------------------------------------------------------
from routers import mcp as mcp_router_mod  # noqa: E402

check("9a the router is mounted under /mcp", mcp_router_mod.router.prefix == "/mcp")
paths = {r.path for r in mcp_router_mod.router.routes}
check("9b status endpoint exists", "/mcp/servers" in paths, f"({sorted(paths)})")
check("9c reload endpoint exists", "/mcp/reload" in paths)
status = asyncio.run(mcp_router_mod.mcp_servers())
check("9d status reports the disabled state", status.get("enabled") is False)
check("9e status lists no tools while disabled", status.get("tools") == [])

# ── 10. Settings-Defaults ----------------------------------------------
import settings as S  # noqa: E402

check("10a mcp_client_enabled defaults to off", S.DEFAULT_SETTINGS.get("mcp_client_enabled") is False)
check("10b timeout default is 30 s", S.DEFAULT_SETTINGS.get("mcp_client_timeout_s") == 30)
check("10c server list default is empty", S.DEFAULT_SETTINGS.get("mcp_servers") == [])

print()
print(f"SUMMARY mcp_client: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
