# -*- coding: utf-8 -*-
"""MCP client (beta): stdio handshake, tool defs, calls, failure modes.

Drives the real client against a real child process (a small stdlib MCP
server written to a temp dir), so protocol framing, timeouts and process
cleanup are actually exercised. No network, no model.

Every client runs inside ONE event loop: a subprocess transport belongs to
the loop that created it.
"""
import asyncio
import inspect
import json
import re
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
MARKER = sys.argv[2] if len(sys.argv) > 2 else "none"

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
        if MODE == "paged":
            cursor = (msg.get("params") or {}).get("cursor")
            if cursor == "p2":
                send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                    {"name": "second_page", "description": "page two",
                     "inputSchema": {"type": "object", "properties": {}}}]}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "tools": [{"name": "first_page", "description": "page one",
                               "inputSchema": {"type": "object", "properties": {}}}],
                    "nextCursor": "p2"}})
            continue
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
                {"type": "text", "text": "echo: " + MARKER + "|" + str(args.get("text", ""))}]}})
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


def spec(name: str, mode: str = "normal", marker: str = "none") -> dict:
    d = Path(tempfile.mkdtemp(prefix="hm_mcp_"))
    script = d / f"server_{mode}.py"
    script.write_text(FAKE, encoding="utf-8")
    return {"name": name, "command": sys.executable,
            "args": [str(script), mode, marker], "enabled": True}


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

check("3a call returns the text content", res["echo"] == "echo: none|hi", f"({res['echo']!r})")
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

_orig_enabled = MC.McpClient.__dict__["enabled"]   # staticmethod descriptor
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


# ── 11. Entfernter Server wird geschlossen, Kollision gemeldet ----------
async def _orphan_flow():
    client = MC.McpClient()
    first = spec("alpha")
    st1 = await client.refresh(settings_with(first))
    conn = client._conns.get("alpha")
    proc = conn._proc if conn else None
    # zweiter Refresh ohne diesen Server
    st2 = await client.refresh(settings_with(spec("beta")))
    rc = getattr(proc, "returncode", None)
    await client.close_all()
    return st1, st2, rc, dict(client._conns)


st1, st2, rc, conns = asyncio.run(_orphan_flow())
check("11a a server that left the config is closed", rc is not None, f"(rc={rc})")
check("11b the new config is active", st2["servers"][0]["name"] == "beta")

async def _collision_flow():
    # two names that sanitize to the same tool prefix: "my srv" and "my/srv"
    client = MC.McpClient()
    st = await client.refresh(settings_with(spec("my srv"), spec("my/srv")))
    names = [d["function"]["name"] for d in client.tool_defs()]
    await client.close_all()
    return st, names


st, names = asyncio.run(_collision_flow())
check("11c colliding tool names are not duplicated",
      len(names) == len(set(names)), f"({sorted(names)})")
_server_tools = [n for n in names if n != "mcp_reload"]
check("11d the prefix is the sanitized server name",
      all(n.startswith("mcp_my_srv_") for n in _server_tools), f"({sorted(names)})")
skipped = [s for e in st["servers"] for s in e.get("skipped", [])]
check("11e the skip is reported in the status", len(skipped) == 4, f"({skipped})")



# ── 12. B1/H6: Schema nur in Modes mit MCP-Recht ------------------------
import tools.definitions as D  # noqa: E402

_prev_enabled = MC.McpClient.__dict__["enabled"]
_prev_defs = MC.tool_defs
MC.McpClient.enabled = staticmethod(lambda settings=None: True)   # type: ignore[assignment]
MC.tool_defs = lambda settings=None: [{"type": "function", "function": {
    "name": "mcp_demo_echo", "description": "demo",
    "parameters": {"type": "object", "properties": {}}}}]


def _names_for(mode):
    return [t["function"]["name"] for t in D._get_inline_tools(mode=mode)]


check("12a a write/exec mode sees MCP tools", "mcp_demo_echo" in _names_for("duo_full"))
check("12b mcp_agent sees them too", "mcp_demo_echo" in _names_for("mcp_agent"))
check("12c a read-only mode does not", "mcp_demo_echo" not in _names_for("duo_readonly"))
check("12d pre_explore does not", "mcp_demo_echo" not in _names_for("pre_explore"))
check("12e the direct chat does not", "mcp_demo_echo" not in _names_for("direct"))

out = asyncio.run(R._run_inline_tool("mcp_demo_echo", {}, tool_mode="duo_readonly"))
check("12f a read-only run cannot CALL one either", "TOOL_NOT_ALLOWED" in out, f"({out[:60]!r})")

MC.McpClient.enabled = _prev_enabled          # type: ignore[assignment]
MC.tool_defs = _prev_defs                     # type: ignore[assignment]

# ── 13. H4: Reload mit geaenderten args startet neu --------------------
async def _reload_flow():
    client = MC.McpClient()
    await client.refresh(settings_with(spec("reload", "normal", "A")))
    first = await client.call("mcp_reload_echo", {"text": "x"})
    await client.refresh(settings_with(spec("reload", "normal", "B")))
    second = await client.call("mcp_reload_echo", {"text": "x"})
    await client.close_all()
    return first, second


first, second = asyncio.run(_reload_flow())
check("13a the first config answers", first == "echo: A|x", f"({first!r})")
check("13b a reload with new args restarts the server", second == "echo: B|x", f"({second!r})")

# ── 14. M2: ein belegter Server killt den zweiten Call nicht ------------
SLOW = r'''
import json, sys, time
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "serverInfo": {"name": "slow", "version": "0"}}}) + "\n")
        sys.stdout.flush()
    elif msg.get("method") == "tools/list":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "tools": [{"name": "wait", "description": "slow",
                       "inputSchema": {"type": "object", "properties": {}}}]}}) + "\n")
        sys.stdout.flush()
    elif msg.get("method") == "tools/call":
        time.sleep(3)
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": "done"}]}}) + "\n")
        sys.stdout.flush()
'''


async def _busy_flow():
    d = Path(tempfile.mkdtemp(prefix="hm_mcp_slow_"))
    script = d / "slow.py"
    script.write_text(SLOW, encoding="utf-8")
    client = MC.McpClient()
    conf = settings_with({"name": "slow", "command": sys.executable,
                          "args": [str(script)], "enabled": True}, timeout_s=5.0)
    await client.refresh(conf)
    first = asyncio.create_task(client.call("mcp_slow_wait", {}, timeout=5.0))
    await asyncio.sleep(0.2)
    # the second call gets a shorter budget than the first needs, so the LOCK
    # wait is what runs out - and that must not kill the server (M2)
    second = await client.call("mcp_slow_wait", {}, timeout=0.5)
    try:
        await first
    except (MC.McpError, asyncio.TimeoutError):
        pass
    await client.close_all()
    return second


second = asyncio.run(_busy_flow())
check("14a a second call reports busy instead of dying", "busy" in second.lower(), f"({second[:70]!r})")

# ── 15. M8/L4/L6/N3: Namen, Slots, Caches, Text ------------------------
check("15a tool names stay within 64 characters",
      len(MC._tool_name("s" * 40, "t" * 80)) <= 64,
      f"({len(MC._tool_name('s' * 40, 't' * 80))})")
check("15b disabled entries do not eat a slot",
      len(MC.McpClient._specs({"mcp_servers": [
          {"name": f"off{i}", "command": "x", "enabled": False} for i in range(8)
      ] + [{"name": "on", "command": "x", "enabled": True}]})) == 1)
check("15c duplicate server names are ignored",
      len(MC.McpClient._specs({"mcp_servers": [
          {"name": "dup", "command": "a"}, {"name": "dup", "command": "b"}]})) == 1)
check("15d enabled accepts the string false",
      MC.ServerSpec({"name": "x", "command": "y", "enabled": "false"}, 0).enabled is False)


async def _cache_flow():
    client = MC.McpClient()
    await client.refresh(settings_with(spec("cache")))
    had = len(client.tool_defs())
    await client.close_all()
    return had, client.tool_defs()


had, after = asyncio.run(_cache_flow())
check("15e close_all clears the cached tool definitions", had > 0 and after == [], f"({had} -> {len(after)})")

# ── 16. Struktur-Pins fuer Shutdown, Approval, Quoting -----------------
SRC_CLIENT = (ROOT / "infra" / "mcp_client.py").read_text(encoding="utf-8", errors="replace")
SRC_SERVER = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
SRC_RUNNER = (ROOT / "tools" / "runner.py").read_text(encoding="utf-8", errors="replace")
check("16a the shutdown hook closes MCP children",
      "_mc.close_all()" in SRC_SERVER and "async def _shutdown" in SRC_SERVER)
check("16b a startup refresh exists", "async def _mcp_startup_refresh" in SRC_SERVER)
check("16c the tree kill is wired", "_kill_tree(proc)" in SRC_CLIENT and "taskkill" in SRC_CLIENT)
check("16d cmd gets /d and quoted arguments", '"cmd", "/d", "/c"' in SRC_CLIENT)
check("16e MCP calls pass the approval gate", "gated=True" in SRC_RUNNER)
check("16f no path normalisation on the MCP payload", "_normalize_meta_paths(_mcp_result)" not in SRC_RUNNER)
check("16g schemas are capped", "MAX_SCHEMA_CHARS" in SRC_CLIENT)
check("16h the router reports running servers", '"running"' in
      (ROOT / "routers" / "mcp.py").read_text(encoding="utf-8", errors="replace"))



# ── Audit-Cleanup (2026-10-27) ─────────────────────────────────────────
# 1. tool_defs() must not accept a settings argument it ignores, and the
#    client has to expose its running state instead of the router reading
#    _conns.
from infra.mcp_client import McpClient as _MC  # noqa: E402
check("16a tool_defs takes no settings argument",
      len(inspect.signature(_MC.tool_defs).parameters) == 1)
check("16b status() is the public view of the running servers",
      hasattr(_MC, "status") and len(inspect.signature(_MC.status).parameters) == 1)
_ROUTER = (ROOT / "routers" / "mcp.py").read_text(encoding="utf-8", errors="replace")
check("16c the router does not reach into _conns", "_conns" not in _ROUTER)
check("16d the router asks for status()", "mcp_client.status()" in _ROUTER)

# 2. No dead members.
_SRC = (ROOT / "infra" / "mcp_client.py").read_text(encoding="utf-8", errors="replace")
check("16e known() is gone", "def known(" not in _SRC)
check("16f _warned is gone", "_warned" not in _SRC)

# 3. The handshake version follows the engine, it is not pinned.
check("16g the handshake version comes from server.py",
      "from server import HIVEMIND_VERSION" in _SRC)
check("16h no hardcoded engine version in the client",
      not re.search(r'"version": "1\.4\.', _SRC))

# 4. MCP tools are added exactly once, after the mode filter.
_DEFS = (ROOT / "tools" / "definitions.py").read_text(encoding="utf-8", errors="replace")
check("16i MCP tools are extended in one place",
      _DEFS.count("tools.extend(_mcp_tool_defs())") == 1)
_inline = _DEFS.split("def _get_inline_tools(", 1)[1].split("\ndef ", 1)[0] \
    if "def _get_inline_tools(" in _DEFS else ""
check("16j _get_inline_tools no longer extends the raw list itself",
      "_mcp.tool_defs()" not in _inline)



# ── 17. Pagination und das Reload-Tool (Punkt B) ───────────────────────
async def _paged_flow():
    client = MC.McpClient()
    st = await client.refresh(settings_with(spec("paged", mode="paged")))
    names = [d["function"]["name"] for d in client.tool_defs()]
    reload_out = await client.call("mcp_reload", {})
    names_after = [d["function"]["name"] for d in client.tool_defs()]
    await client.close_all()
    return st, names, reload_out, names_after


_paged_st, _paged_names, _reload_out, _paged_after = asyncio.run(_paged_flow())
check("17a every tools/list page is collected",
      "mcp_paged_first_page" in _paged_names and "mcp_paged_second_page" in _paged_names,
      f"({_paged_names})")
check("17b both pages count as tools of the server",
      (_paged_st.get("servers") or [{}])[0].get("tools") == 2,
      f"({(_paged_st.get('servers') or [{}])[0].get('tools')})")
check("17c mcp_reload is offered as a tool", "mcp_reload" in _paged_names)
check("17d calling it restarts and reports",
      "[MCP] reloaded" in _reload_out and "ok, 2 tool" in _reload_out,
      f"({_reload_out[:60]!r})")
check("17e the tool list survives the reload", _paged_after == _paged_names,
      f"({len(_paged_after)} vs {len(_paged_names)})")



# ── 18. Die Karte in der UI (2026-10-27) ───────────────────────────────
# The beta had no UI at all: mcp_client_enabled could only be set by editing
# settings.json. These pins keep the card wired.
_HTML = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
_APPJS = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
check("18a the card exists", 'id="mcp-client-toggle"' in _HTML)
check("18b the toggle posts mcp_client_enabled",
      "onFlagToggle('mcp_client_enabled'" in _HTML)
check("18c the server list is editable", 'id="mcp-servers-json"' in _HTML)
check("18d the card offers reload and save",
      "reloadMcpServers()" in _HTML and "saveMcpServers()" in _HTML)
check("18e app.js syncs the toggle",
      "_syncFlagCheck('mcp-client-toggle', S.mcpClientEnabled)" in _APPJS)
check("18f app.js has the three functions",
      all(f"function {n}(" in _APPJS for n in
          ("mcpLoadStatus", "saveMcpServers", "reloadMcpServers")))
check("18g the JSON is validated before saving",
      "expected a JSON list of servers" in _APPJS)
check("18h the card hits the real endpoints",
      "'/mcp/servers'" in _APPJS and "'/mcp/reload'" in _APPJS)
_m = re.search(r"app\.js\?v=(\d{8}-\d{2})", _HTML)
check("18i the cache buster follows the change", bool(_m) and _m.group(1) >= "20261027-01",
      f"({_m.group(1) if _m else 'fehlt'})")



# ── 19. HTTP-Transport (2026-10-27) ────────────────────────────────────
# A server entry with `url` speaks Streamable HTTP instead of starting a
# process. The fake server below answers JSON and one SSE response, so both
# branches are exercised.
import http.server as _http_server  # noqa: E402
import threading as _threading  # noqa: E402


def _make_http_server(mode: str):
    class Handler(_http_server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):        # keep the test output clean
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                msg = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                msg = {}
            rid = msg.get("id")
            method = msg.get("method")
            if method == "initialize":
                body = {"jsonrpc": "2.0", "id": rid,
                        "result": {"protocolVersion": "2024-11-05",
                                   "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "httpfake", "version": "0"}}}
            elif method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            elif method == "tools/list":
                body = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                    {"name": "echo", "description": "echo",
                     "inputSchema": {"type": "object",
                                     "properties": {"text": {"type": "string"}}}}]}}
            elif method == "tools/call":
                args = (msg.get("params") or {}).get("arguments") or {}
                body = {"jsonrpc": "2.0", "id": rid, "result": {"content": [
                    {"type": "text", "text": "http: " + str(args.get("text", ""))}]}}
            else:
                body = {"jsonrpc": "2.0", "id": rid,
                        "error": {"message": "unknown method"}}
            raw = json.dumps(body).encode("utf-8")
            if mode == "sse":
                payload = b"event: message\ndata: " + raw + b"\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Mcp-Session-Id", "sess-1")
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

    srv = _http_server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    _threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/mcp"


async def _http_flow(mode: str):
    srv, url = _make_http_server(mode)
    try:
        client = MC.McpClient()
        st = await client.refresh(settings_with({"name": "httpfake", "url": url}))
        names = [d["function"]["name"] for d in client.tool_defs()]
        out = await client.call("mcp_httpfake_echo", {"text": "hi"})
        await client.close_all()
        return st, names, out
    finally:
        srv.shutdown()


for _mode in ("json", "sse"):
    _st, _names, _out = asyncio.run(_http_flow(_mode))
    _srv = (_st.get("servers") or [{}])[0]
    check(f"19 [{_mode}] a url server connects", _srv.get("ok") is True,
          f"({_srv.get('error')})")
    check(f"19 [{_mode}] its tools appear", "mcp_httpfake_echo" in _names,
          f"({_names})")
    check(f"19 [{_mode}] a tool call returns the payload", _out == "http: hi",
          f"({_out!r})")

_SRC = (ROOT / "infra" / "mcp_client.py").read_text(encoding="utf-8", errors="replace")
check("19e both transports are selected by kind",
      'spec.kind == "http"' in _SRC and "class HttpConn:" in _SRC)
check("19f no process is started for a url server",
      "server uses 'url', not a process" in _SRC)
check("19g an SSE body is parsed for the matching id",
      "def _sse_response(" in _SRC)
check("19h the session id is carried",
      "Mcp-Session-Id" in _SRC)
check("19i a bad url fails cleanly",
      "url must start with http" in _SRC)

print()
print(f"SUMMARY mcp_client: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
