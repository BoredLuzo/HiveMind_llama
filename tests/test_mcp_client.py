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
    if MODE == "strid" and rid is not None:
        rid = str(rid)          # audit C9: the client must match "1" to 1
    if MODE == "wrongid" and msg.get("id") is not None:
        # audit C9: 60 answers for a foreign id - the client must cap its
        # skip loop instead of spinning until the call timeout
        for _i in range(60):
            send({"jsonrpc": "2.0", "id": msg["id"] + 1000, "result": {}})
        continue
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
        if MODE == "struct":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "struct", "description": "structured only",
                 "inputSchema": {"type": "object", "properties": {}}}]}})
            continue
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
        if name == "struct":
            # audit C12: a result that only carries structuredContent
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "structuredContent": {"answer": 42, "note": "structured only"}}})
        elif name == "echo":
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
# TAINT-MCP (2026-11-03): the forwarded result is untrusted and must carry
# the marker, otherwise a poisoned server can steer run_bash without a card.
check("8b dispatch routes mcp_ names to the client",
      "[stub] mcp_fake_echo ['text']" in out
      and "[UNTRUSTED CONTENT" in out, f"({out[:60]!r})")

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
check("16g the handshake version comes from core/version.py",
      "from core.version import HIVEMIND_VERSION" in _SRC)
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
# branches are exercised. Block 25 extends it with auth, protocol-version and
# retry modes; every request is recorded in srv.state.
import http.server as _http_server  # noqa: E402
import threading as _threading  # noqa: E402


def _make_http_server(mode: str):
    state = {"requests": [], "initialize": 0, "tools_list": 0, "tools_call": 0,
             "session_n": 0}

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
            _reply_session = ""   # expire404: initialize hands out a session
            state["requests"].append({
                "method": method,
                "authorization": self.headers.get("Authorization"),
                "x-api-version": self.headers.get("X-Api-Version"),
                "offered_pv": (msg.get("params") or {}).get("protocolVersion")
                              if method == "initialize" else None,
            })
            if method == "initialize":
                state["initialize"] += 1
                offered = (msg.get("params") or {}).get("protocolVersion")
                if mode == "expire404":
                    # audit C4: every fresh initialize gets the next session
                    state["session_n"] += 1
                    _reply_session = f"sess-{state['session_n']}"
                if mode == "verreject" and offered == "2024-11-05":
                    body = {"jsonrpc": "2.0", "id": rid,
                            "error": {"code": -32602,
                                      "message": "requested protocolVersion "
                                                 "2024-11-05 is not supported, "
                                                 "server advertises 2025-03-26"}}
                else:
                    pv = ("2025-03-26" if mode == "ver0326"
                          else offered if mode == "verreject" else "2024-11-05")
                    body = {"jsonrpc": "2.0", "id": rid,
                            "result": {"protocolVersion": pv,
                                       "capabilities": {"tools": {}},
                                       "serverInfo": {"name": "httpfake",
                                                      "version": "0"}}}
            elif method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            elif method == "tools/list":
                state["tools_list"] += 1
                if mode == "t503once" and state["tools_list"] == 1:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if mode == "plain404":
                    # audit C4: without a sent session there is nothing to
                    # recover - this must fail the start
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                    {"name": "echo", "description": "echo",
                     "inputSchema": {"type": "object",
                                     "properties": {"text": {"type": "string"}}}}]}}
            elif method == "tools/call":
                state["tools_call"] += 1
                if mode == "auth401once" and state["tools_call"] == 1:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if mode == "slowcall":
                    time.sleep(3)          # longer than every test timeout
                args = (msg.get("params") or {}).get("arguments") or {}
                if mode == "expire404":
                    # audit C4: sess-1 is expired, 404 until the client
                    # re-initializes and comes back with sess-2
                    sid = self.headers.get("Mcp-Session-Id") or ""
                    if sid == "sess-1":
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    raw = json.dumps({"jsonrpc": "2.0", "id": rid,
                                      "result": {"content": [
                                          {"type": "text",
                                           "text": "http: " + str(args.get("text", ""))}]}}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if mode == "sseopen":
                    # audit C2: the answer event goes out, then the chunked
                    # stream is HELD OPEN (no terminal chunk) - the old
                    # whole-body buffer timed out here
                    raw = json.dumps({"jsonrpc": "2.0", "id": rid,
                                      "result": {"content": [
                                          {"type": "text",
                                           "text": "http: " + str(args.get("text", ""))}]}}).encode("utf-8")
                    event = b"event: message\ndata: " + raw + b"\n\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(f"{len(event):x}\r\n".encode("ascii")
                                     + event + b"\r\n")
                    self.wfile.flush()
                    time.sleep(3)
                    return
                if mode == "ssewrong":
                    # audit C1: a valid JSON-RPC message for ANOTHER id
                    raw = json.dumps({"jsonrpc": "2.0", "id": (rid or 0) + 5,
                                      "result": {"content": [
                                          {"type": "text",
                                           "text": "FOREIGN-PAYLOAD"}]}}).encode("utf-8")
                    event = b"event: message\ndata: " + raw + b"\n\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(event)))
                    self.end_headers()
                    self.wfile.write(event)
                    return
                if mode == "ssemulti":
                    # audit C10: the answer split over two data: lines
                    raw = json.dumps({"jsonrpc": "2.0", "id": rid,
                                      "result": {"content": [
                                          {"type": "text",
                                           "text": "http: " + str(args.get("text", ""))}]}}).encode("utf-8")
                    cut = raw.index(b'"result"')
                    event = (b"event: message\ndata: " + raw[:cut]
                             + b'"result"\ndata: ' + raw[cut + 8:] + b"\n\n")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(event)))
                    self.end_headers()
                    self.wfile.write(event)
                    return
                if mode == "jsonwrongid":
                    # audit C5: a plain-JSON answer for ANOTHER id
                    raw = json.dumps({"jsonrpc": "2.0", "id": (rid or 0) + 7,
                                      "result": {"content": [
                                          {"type": "text",
                                           "text": "FOREIGN-JSON-PAYLOAD"}]}}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                body = {"jsonrpc": "2.0", "id": rid, "result": {"content": [
                    {"type": "text", "text": "http: " + str(args.get("text", ""))}]}}
            else:
                body = {"jsonrpc": "2.0", "id": rid,
                        "error": {"message": "unknown method"}}
            raw = json.dumps(body).encode("utf-8")
            try:
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
                    if _reply_session:
                        self.send_header("Mcp-Session-Id", _reply_session)
                    self.end_headers()
                    self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                # a client that ran into its timeout is gone; the sleeping
                # handler must not spill a traceback into the test output
                pass

    srv = _http_server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.state = state
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



# ── 20. Regressionen aus dem langsamen Live-Run (2026-10-27) ──────────
# The log showed "RuntimeWarning: coroutine '_find_loaded' was never awaited"
# and a 20x decode drop after an hour. Both are pinned here.
_DUO = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
check("20a the in-use guard awaits _find_loaded",
      "await _lsm_inuse._find_loaded(exec_mdl)" in _DUO)
check("20b no bare _find_loaded call remains",
      "= _lsm_inuse._find_loaded(" not in _DUO)
_LOAD = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8", errors="replace")
check("20c the loader measures VRAM headroom after a load",
      "[VRAM-TIGHT]" in _LOAD and "get_live_gpu_free_mib" in _LOAD)
check("20d the tight-fit warning is recorded on the slot",
      "slot.last_warning =" in _LOAD)
_SLOT = (ROOT / "backend" / "llama_slots.py").read_text(encoding="utf-8", errors="replace")
check("20e ModelSlot carries the warning field", "self.last_warning" in _SLOT)
_VRAM = (ROOT / "routers" / "vram.py").read_text(encoding="utf-8", errors="replace")
check("20f /vram/ps reports the warning", "_slot_warning(m.get" in _VRAM)
check("20g the status helper never raises into the route",
      "except (ImportError, AttributeError, TypeError, RuntimeError)" in _VRAM)



# ── 21. Prefill-Kosten (langsamer Live-Run 2026-10-27) ────────────────
# Measured: decode 37-48 t/s is fine, prefill is ~100 t/s, so a cold 38k
# prefill costs 420 s. The read timeout has to cover that, and the run has to
# say what a cold prefill would cost.
import sys as _sys                                                    # noqa: E402
if str(ROOT) not in _sys.path:
    _sys.path.insert(0, str(ROOT))
from core.duo_helpers import _resolve_tool_read_timeout_seconds as _rt  # noqa: E402

_t_small = _rt({}, "balanced", False, num_ctx=8192)
_t_huge = _rt({}, "balanced", False, num_ctx=85000)
check("21a a small context keeps the plain timeout", _t_small < 600,
      f"({_t_small:.0f}s)")
check("21b a large context gets a prefill-aware timeout", _t_huge > 1800,
      f"({_t_huge:.0f}s)")
check("21c the timeout stays bounded", _rt({}, "balanced", False, num_ctx=500000) <= 3600)
_HELP = (ROOT / "core" / "duo_helpers.py").read_text(encoding="utf-8", errors="replace")
check("21d the reason is documented in the code",
      "PREFILL-AWARE" in _HELP and "prefill_tps_for" in _HELP)
_DUO_R = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
# split 2.5: the dtool payload budget lives in duo_dtool_setup.py now
_DUO_DT = (ROOT / "core" / "duo_dtool_setup.py").read_text(encoding="utf-8", errors="replace")
check("21e the run logs the cold-prefill cost", "[PREFILL-COST]" in _DUO_DT)
check("21f it uses the effective coder ctx, not a made-up attribute",
      "_cold_ctx = int(_dtool_ctx or 0)" in _DUO_DT)



# ── 22. Degradation über die Run-Dauer sichtbar machen ────────────────
_ATL = (ROOT / "core" / "agentic_tool_loop.py").read_text(encoding="utf-8", errors="replace")
check("22a the round log carries free VRAM", "vram_free=" in _ATL)
check("22b the VRAM query is guarded",
      "get_live_gpu_free_mib as _vfree" in _ATL)
from settings_source import settings_source as _settings_src
_SET = _settings_src()
check("22c checkpoints are a setting", '"llama_ctx_checkpoints":' in _SET)
check("22d the host prompt cache is a setting", '"llama_cache_ram_mib":' in _SET)
_SRV = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
check("22e both are synced into the backend at startup",
      "_lc_init.CTX_CHECKPOINTS = _cp_val" in _SRV
      and "_lc_init.CACHE_RAM_MIB = _cram_val" in _SRV)
_DOC = (ROOT / "docs" / "settings.md").read_text(encoding="utf-8", errors="replace")
check("22f settings docs list the knobs",
      "llama_ctx_checkpoints" in _DOC and "llama_cache_ram_mib" in _DOC)



# ── 23. Stats-Tooltips und until_finished-Default (2026-10-27) ────────
_HTML2 = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
_JS2 = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
_CSS2 = (ROOT / "static" / "style.css").read_text(encoding="utf-8", errors="replace")
_TIPS = ("pbb-ctx", "pbb-ctxreuse", "pbb-tok", "pbb-rate", "pbb-decode",
         "pbb-in", "pbb-cached", "pbb-new", "pbb-reqs", "pbb-tools")
_missing_tips = [i for i in _TIPS if f'id="{i}" data-tip-title=' not in _JS2]
check("23a every perf field carries a tooltip", not _missing_tips,
      f"(ohne: {_missing_tips})")
check("23b the bubble is styled", ".ctx-perf-item[data-tip]::after" in _CSS2)
check("23c the bubble is keyboard reachable", "focus-visible::after" in _CSS2)
check("23d the cache buster moved", "app.js?v=20261102-01" in _HTML2
      and "style.css?v=20261101-01" in _HTML2)
from settings import DEFAULT_SETTINGS as _D2                              # noqa: E402
check("23e until_finished is on by default", _D2.get("until_finished") is True)
_SRV2 = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
check("23f the request default follows",
      '_body_or_settings("until_finished",          default=True)' in _SRV2)
check("23g the UI checkbox follows the setting",
      "S.duoUntilFinished = s.until_finished !== false;" in _JS2)



# ── 24. Doku sagt die Wahrheit ueber den Client ───────────────────────
_DOC2 = (ROOT / "docs" / "mcp_client.md").read_text(encoding="utf-8", errors="replace")
check("24a docs no longer claim stdio-only", "stdio transport only" not in _DOC2)
check("24b docs no longer claim there is no card", "no MCP card" not in _DOC2)
check("24c docs name both transports", "Two transports" in _DOC2)
check("24d docs point at the card", "MCP client** card" in _DOC2)


# ── 25. Auth, protocol version, HTTP retry (2026-10-28) ───────────────
# Uses the fake HTTP server from block 19 with the new modes: auth401once
# (401 on the first tools/call), t503once (503 on the first tools/list),
# slowcall (tools/call sleeps past every timeout), ver0326 (answers
# protocolVersion 2025-03-26) and verreject (refuses the 2024-11-05 offer).
import os as _os                                                          # noqa: E402
import logging as _logging                                                # noqa: E402

_TOKEN = "supersecret123"
_TOKEN_ENV = "HIVEMIND_TEST_MCP_TOKEN"


async def _auth_flow():
    srv, url = _make_http_server("json")
    _os.environ[_TOKEN_ENV] = _TOKEN
    try:
        client = MC.McpClient()
        st = await client.refresh(settings_with(
            {"name": "httpfake", "url": url, "token_env": _TOKEN_ENV,
             "headers": {"X-Api-Version": "2022-11-28"}}))
        out = await client.call("mcp_httpfake_echo", {"text": "hi"})
        described = (st.get("servers") or [{}])[0]
        running = client.status()
        await client.close_all()
        return srv.state, out, described, running
    finally:
        _os.environ.pop(_TOKEN_ENV, None)
        srv.shutdown()


_state_a, _out_a, _desc_a, _status_a = asyncio.run(_auth_flow())
_auths = [r.get("authorization") for r in _state_a["requests"]]
check("25a token_env sends Authorization: Bearer <value>",
      bool(_auths) and all(a == f"Bearer {_TOKEN}" for a in _auths),
      f"({_auths})")
check("25a2 headers reach the server verbatim",
      all(r.get("x-api-version") == "2022-11-28" for r in _state_a["requests"]),
      f"({[r.get('x-api-version') for r in _state_a['requests']]})")
check("25d describe() reports has_auth, never the value",
      _desc_a.get("has_auth") is True
      and _TOKEN not in json.dumps(_desc_a)
      and _TOKEN not in json.dumps(_status_a),
      f"(has_auth={_desc_a.get('has_auth')})")


async def _no_token_flow(env_value):
    srv, url = _make_http_server("json")
    _os.environ.pop(_TOKEN_ENV, None)
    if env_value is not None:
        _os.environ[_TOKEN_ENV] = env_value
    try:
        client = MC.McpClient()
        st = await client.refresh(settings_with(
            {"name": "httpfake", "url": url, "token_env": _TOKEN_ENV}))
        return st, srv.state
    finally:
        _os.environ.pop(_TOKEN_ENV, None)
        srv.shutdown()


_st_b, _state_b = asyncio.run(_no_token_flow(None))
check("25b a token_env without its variable fails the start, no request sent",
      _st_b["servers"][0]["ok"] is False
      and _TOKEN_ENV in _st_b["servers"][0]["error"]
      and _state_b["requests"] == [],
      f"({_st_b['servers'][0]['error'][:70]!r})")
_st_b2, _state_b2 = asyncio.run(_no_token_flow(""))
check("25b2 an empty token_env variable fails the same way",
      _st_b2["servers"][0]["ok"] is False
      and _TOKEN_ENV in _st_b2["servers"][0]["error"]
      and _state_b2["requests"] == [],
      f"({_st_b2['servers'][0]['error'][:70]!r})")


async def _auth401_flow():
    srv, url = _make_http_server("auth401once")
    try:
        client = MC.McpClient()
        st = await client.refresh(settings_with({"name": "httpfake", "url": url}))
        first = await client.call("mcp_httpfake_echo", {"text": "hi"})
        conn = client._conns.get("httpfake")
        dead_after_401 = conn.dead if conn else None
        second = await client.call("mcp_httpfake_echo", {"text": "again"})
        await client.close_all()
        return st, first, dead_after_401, second
    finally:
        srv.shutdown()


_st_c, _first_c, _dead_c, _second_c = asyncio.run(_auth401_flow())
check("25c a 401 becomes MCP_AUTH_FAILED with the cause",
      _first_c.startswith("[TOOL_ERROR: MCP_AUTH_FAILED]")
      and ("missing" in _first_c and "expired" in _first_c),
      f"({_first_c[:80]!r})")
check("25c2 the connection is NOT marked dead after a 401",
      _st_c["servers"][0]["ok"] is True and _dead_c is False,
      f"(dead={_dead_c})")
check("25c3 a second call works once the server answers 200",
      _second_c == "http: again", f"({_second_c[:50]!r})")


async def _version_flow():
    srv, url = _make_http_server("ver0326")
    try:
        client = MC.McpClient()
        st = await client.refresh(settings_with({"name": "httpfake", "url": url}))
        conn = client._conns.get("httpfake")
        pv_first = conn.protocol_version if conn else None
        # a reload that changes the signature rebuilds the connection; it
        # must OFFER the stored version instead of the default
        st2 = await client.refresh(settings_with(
            {"name": "httpfake", "url": url, "headers": {"X-Test": "1"}}))
        offers = [r.get("offered_pv") for r in srv.state["requests"]
                  if r["method"] == "initialize"]
        conn2 = client._conns.get("httpfake")
        await client.close_all()
        return st, st2, pv_first, offers, conn2.protocol_version if conn2 else None
    finally:
        srv.shutdown()


_st_e, _st_e2, _pv_e, _offers_e, _pv_e2 = asyncio.run(_version_flow())
check("25e a newer protocolVersion is accepted and stored on the connection",
      _st_e["servers"][0]["ok"] is True and _pv_e == "2025-03-26",
      f"(pv={_pv_e})")
check("25e2 a rebuilt connection offers the stored version",
      _offers_e == ["2024-11-05", "2025-03-26"] and _pv_e2 == "2025-03-26"
      and _st_e2["servers"][0]["ok"] is True,
      f"(offers={_offers_e})")


class _LogGrab(_logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


async def _version_reject_flow():
    srv, url = _make_http_server("verreject")
    grab = _LogGrab()
    logger = _logging.getLogger("hivemind.mcp_client")
    old_level = logger.level
    logger.setLevel(_logging.INFO)
    logger.addHandler(grab)
    try:
        client = MC.McpClient()
        st = await client.refresh(settings_with({"name": "httpfake", "url": url}))
        out = await client.call("mcp_httpfake_echo", {"text": "hi"})
        conn = client._conns.get("httpfake")
        offers = [r.get("offered_pv") for r in srv.state["requests"]
                  if r["method"] == "initialize"]
        await client.close_all()
        return st, out, conn.protocol_version if conn else None, offers, grab.messages
    finally:
        logger.removeHandler(grab)
        logger.setLevel(old_level)
        srv.shutdown()


_st_f, _out_f, _pv_f, _offers_f, _logs_f = asyncio.run(_version_reject_flow())
check("25f a rejected version gets exactly one retry with the advertised one",
      _offers_f == ["2024-11-05", "2025-03-26"] and _pv_f == "2025-03-26",
      f"(offers={_offers_f}, pv={_pv_f})")
check("25f2 the retry is logged with its version",
      any("initialize retried with protocolVersion=2025-03-26" in m
          for m in _logs_f), f"({_logs_f[:2]})")
check("25f3 the connection is usable after the retry",
      _st_f["servers"][0]["ok"] is True and _out_f == "http: hi",
      f"({_out_f[:40]!r})")


async def _retry_503_flow():
    srv, url = _make_http_server("t503once")
    try:
        client = MC.McpClient()
        st = await client.refresh(settings_with({"name": "httpfake", "url": url},
                                                timeout_s=5.0))
        names = [d["function"]["name"] for d in client.tool_defs()]
        await client.close_all()
        return st, names, srv.state
    finally:
        srv.shutdown()


_st_g, _names_g, _state_g = asyncio.run(_retry_503_flow())
check("25g a 503 on tools/list is retried and succeeds on the second attempt",
      _state_g["tools_list"] == 2 and _st_g["servers"][0]["ok"] is True
      and "mcp_httpfake_echo" in _names_g,
      f"(tools_list={_state_g['tools_list']}, "
      f"err={_st_g['servers'][0]['error'][:50]!r})")


async def _slow_call_flow():
    srv, url = _make_http_server("slowcall")
    try:
        client = MC.McpClient()
        await client.refresh(settings_with({"name": "httpfake", "url": url},
                                           timeout_s=5.0))
        out = await client.call("mcp_httpfake_echo", {"text": "hi"}, timeout=0.6)
        conn = client._conns.get("httpfake")
        dead = conn.dead if conn else None
        await client.close_all()
        return out, dead, srv.state
    finally:
        srv.shutdown()


_out_h, _dead_h, _state_h = asyncio.run(_slow_call_flow())
check("25h a read timeout on tools/call is NOT retried",
      _state_h["tools_call"] == 1 and "timed out" in _out_h,
      f"(calls={_state_h['tools_call']}, out={_out_h[:60]!r})")
check("25h2 the final timeout marks the connection dead",
      _dead_h is True, f"(dead={_dead_h})")
check("25h3 the timed-out call is cancelled on the server",
      any(r["method"] == "notifications/cancelled"
          for r in _state_h["requests"]),
      f"({[r['method'] for r in _state_h['requests']]})")


_DOC3 = (ROOT / "docs" / "mcp_client.md").read_text(encoding="utf-8", errors="replace")
check("25i docs document headers and token_env",
      "token_env" in _DOC3 and '"headers"' in _DOC3 and "Bearer" in _DOC3,
      "(docs/mcp_client.md)")
check("25i2 docs describe the retry policy truthfully",
      "ConnectError" in _DOC3 and "502" in _DOC3
      and "tools/call" in _DOC3,
      "(retry section)")
check("25i3 docs name the auth flag and the version fallback",
      "has_auth" in _DOC3 and "2025-03-26" in _DOC3,
      "(auth + version section)")


# ── 26. HTTP streaming and id discipline (2026-10-31, C1/C2/C4/C5/C10) ─
# New fake-server modes: sseopen (answers as SSE, then holds the chunked
# stream open), ssewrong (SSE answer for a foreign id), ssemulti (answer
# split over two data: lines), jsonwrongid (plain JSON for a foreign id),
# expire404 (404 for the expired session, fresh session after re-init),
# plain404 (404 with no session at all).
async def _http_call_flow(mode: str, call_timeout: float = 4.0):
    srv, url = _make_http_server(mode)
    try:
        client = MC.McpClient()
        st = await client.refresh(settings_with({"name": "httpfake", "url": url},
                                                timeout_s=5.0))
        t0 = time.time()
        out = await client.call("mcp_httpfake_echo", {"text": "hi"},
                                timeout=call_timeout)
        dt = time.time() - t0
        conn = client._conns.get("httpfake")
        snap = {"dead": conn.dead if conn else None,
                "session": conn._session_id if conn else None,
                "inits": srv.state["initialize"]}
        await client.close_all()
        return st, out, dt, snap
    finally:
        srv.shutdown()


_st_26a, _out_26a, _dt_26a, _snap_26a = asyncio.run(
    _http_call_flow("sseopen", call_timeout=8.0))
check("26a the SSE answer arrives while the stream is still open",
      _out_26a == "http: hi" and _dt_26a < 2.0,
      f"(out={_out_26a[:40]!r}, dt={_dt_26a:.2f}s)")

_st_26b, _out_26b, _dt_26b, _snap_26b = asyncio.run(_http_call_flow("ssewrong"))
check("26b a wrong-id SSE message is not handed to the model",
      _out_26b.startswith("[TOOL_ERROR: MCP_CALL_FAILED]")
      and "FOREIGN-PAYLOAD" not in _out_26b and "mismatch" in _out_26b,
      f"({_out_26b[:80]!r})")

_st_26c, _names_26c, _out_26c = asyncio.run(_http_flow("ssemulti"))
check("26c multi-line data: fields are joined into one event",
      _out_26c == "http: hi", f"({_out_26c[:60]!r})")

_st_26d, _out_26d, _dt_26d, _snap_26d = asyncio.run(
    _http_call_flow("jsonwrongid"))
check("26d the plain-JSON path checks the response id too",
      _out_26d.startswith("[TOOL_ERROR: MCP_CALL_FAILED]")
      and "FOREIGN-JSON-PAYLOAD" not in _out_26d and "mismatch" in _out_26d,
      f"({_out_26d[:80]!r})")

_st_26e, _out_26e, _dt_26e, _snap_26e = asyncio.run(
    _http_call_flow("expire404", call_timeout=8.0))
check("26e an expired session (HTTP 404) re-initializes once and retries",
      _out_26e == "http: hi" and _snap_26e["inits"] == 2,
      f"(out={_out_26e[:50]!r}, inits={_snap_26e['inits']})")
check("26e2 the recovered connection is alive and carries the new session",
      _snap_26e["dead"] is False and _snap_26e["session"] == "sess-2",
      f"({_snap_26e})")

_srv_26f, _url_26f = _make_http_server("plain404")
try:
    _client_26f = MC.McpClient()
    _st_26f = asyncio.run(_client_26f.refresh(
        settings_with({"name": "httpfake", "url": _url_26f}, timeout_s=5.0)))
finally:
    _srv_26f.shutdown()
check("26f a 404 without a session still fails the start loudly",
      _st_26f["servers"][0]["ok"] is False
      and "404" in _st_26f["servers"][0]["error"]
      and _srv_26f.state["tools_list"] == 1,
      f"({_st_26f['servers'][0]['error'][:60]!r})")


# ── 27. stdio id discipline (2026-10-31, audit C9) ────────────────────
async def _strid_flow():
    client = MC.McpClient()
    st = await client.refresh(settings_with(spec("strid", "strid")))
    out = await client.call("mcp_strid_echo", {"text": "hi"})
    await client.close_all()
    return st, out


_st_27, _out_27 = asyncio.run(_strid_flow())
check("27a a server echoing string ids matches (\"1\" == 1)",
      _st_27["servers"][0]["ok"] is True and _out_27 == "echo: none|hi",
      f"(err={_st_27['servers'][0]['error'][:50]!r}, out={_out_27[:40]!r})")


async def _wrongid_flow():
    client = MC.McpClient()
    t0 = time.time()
    st = await client.refresh(settings_with(spec("wrong", "wrongid")))
    dt = time.time() - t0
    await client.close_all()
    return st, dt


_st_27b, _dt_27b = asyncio.run(_wrongid_flow())
check("27b a server answering only for other ids is capped, not spun on",
      _st_27b["servers"][0]["ok"] is False
      and "no answer for id" in _st_27b["servers"][0]["error"]
      and _dt_27b < 4.0,
      f"(err={_st_27b['servers'][0]['error'][:60]!r}, dt={_dt_27b:.2f}s)")


# ── 28. call vs refresh (2026-10-31, audit C11) ───────────────────────
# The per-conn guard: a refresh that rebuilds a conn must wait for the
# in-flight call instead of killing it mid-flight.
async def _guard_flow():
    d = Path(tempfile.mkdtemp(prefix="hm_mcp_guard_"))
    script = d / "slow.py"
    script.write_text(SLOW, encoding="utf-8")
    client = MC.McpClient()
    base = {"name": "gslow", "command": sys.executable,
            "args": [str(script)], "enabled": True}
    await client.refresh(settings_with(base, timeout_s=8.0))
    inflight = asyncio.create_task(client.call("mcp_gslow_wait", {}))
    await asyncio.sleep(0.3)
    # signature change (extra env) -> the refresh pops and closes the conn
    # the call is running on; without the guard the call dies with
    # "closed stdout"
    changed = dict(base, env={"HIVEMIND_TEST_GUARD": "1"})
    st2 = await client.refresh(settings_with(changed, timeout_s=8.0))
    done = await inflight
    second = await client.call("mcp_gslow_wait", {})
    await client.close_all()
    return done, st2, second


_done_28, _st_28, _second_28 = asyncio.run(_guard_flow())
check("28a an in-flight call survives a concurrent rebuild",
      _done_28 == "done", f"({_done_28[:70]!r})")
check("28b the rebuilt connection serves the next call",
      _st_28["servers"][0]["ok"] is True and _second_28 == "done",
      f"(err={_st_28['servers'][0]['error'][:50]!r})")
check("28c close waits on the per-conn guard",
      "async with self._guard" in SRC_CLIENT, "(infra/mcp_client.py)")


# ── 29. result rendering (2026-10-31, audit C12) ──────────────────────
async def _render_flow():
    client = MC.McpClient()
    await client.refresh(settings_with(spec("struct", "struct"),
                                       spec("big", "normal")))
    struct = await client.call("mcp_struct_struct", {})
    big = await client.call("mcp_big_big", {})
    await client.close_all()
    return struct, big


_struct_29, _big_29 = asyncio.run(_render_flow())
check("29a a structuredContent-only result is rendered, not '(no output)'",
      '"answer": 42' in _struct_29 and "(no output)" not in _struct_29,
      f"({_struct_29[:60]!r})")
check("29b a cut at MAX_OUTPUT_CHARS carries the [truncated] marker",
      _big_29.endswith("[truncated]")
      and len(_big_29) <= MC.MAX_OUTPUT_CHARS + len("\n[truncated]"),
      f"(len={len(_big_29)})")


# ── 30. process kill off the loop + cmd quoting (2026-10-31, C6/C13) ──
import inspect as _inspect                                               # noqa: E402
check("30a the tree kill no longer blocks the event loop",
      _inspect.iscoroutinefunction(MC._kill_tree)
      and "asyncio.to_thread" in SRC_CLIENT, "(C6)")


async def _kill_tree_flow():
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)",
        start_new_session=(sys.platform != "win32"))
    t0 = time.time()
    await MC._kill_tree(proc)
    return time.time() - t0


_dt_30b = asyncio.run(_kill_tree_flow())
check("30b the tree kill runs detached and fast",
      _dt_30b < 10.0, f"(dt={_dt_30b:.2f}s)")

_sp_30 = MC.ServerSpec({"name": "cmdq", "command": "C:/tools/s.cmd",
                        "args": ['say "hi" & echo %PWNED%']}, 0)
if sys.platform == "win32":
    _argv_30 = _sp_30.argv()
    check("30c cmd-style quoting doubles the quote sign",
          _argv_30[:4] == ["cmd", "/d", "/c", "C:/tools/s.cmd"]
          and _argv_30[4] == '"say ""hi"" & echo %PWNED%"',
          f"({_argv_30})")
else:
    check("30c cmd-style quoting doubles the quote sign", True, "(posix run)")


# ── 31. redaction in describe() (2026-10-31, audit C7) ────────────────
_sp_31 = MC.ServerSpec({"name": "sec", "command": "python",
                        "args": ["-m", "server", "--api-key", "sk-live-123",
                                 "--token=abc123", "--verbose",
                                 "--db", "C:/x.db"]}, 0)
_d_31 = _sp_31.describe()
_masked_31 = sum(1 for a in _d_31["args"]
                 if a == "***" or a.endswith("=***"))
check("31a secret arg values are masked, options stay readable",
      "sk-live-123" not in json.dumps(_d_31)
      and "abc123" not in json.dumps(_d_31)
      and _masked_31 == 2
      and "--api-key" in _d_31["args"] and "--token=***" in _d_31["args"]
      and "C:/x.db" in _d_31["args"],
      f"({_d_31['args']})")
check("31b the signature stays raw so a reload still rebuilds correctly",
      "sk-live-123" in str(_sp_31.signature()), "(C7)")
_d_31u = MC.ServerSpec({"name": "secu",
                        "url": "http://user:pass@h:8931/mcp"}, 0).describe()
check("31c url userinfo is masked",
      _d_31u["url"] == "http://user:***@h:8931/mcp"
      and "pass" not in _d_31u["url"], f"({_d_31u['url']})")
_d_31u2 = MC.ServerSpec({"name": "secu2",
                         "url": "https://token123@h/mcp"}, 0).describe()
check("31d a bare userinfo (token-in-username style) is masked completely",
      _d_31u2["url"] == "https://***@h/mcp", f"({_d_31u2['url']})")
check("31e a url without userinfo is untouched",
      MC._redact_url("http://127.0.0.1:8931/mcp") == "http://127.0.0.1:8931/mcp")
check("31f non-secret args pass through",
      MC._redact_args(["-m", "server", "--verbose"]) == ["-m", "server", "--verbose"])


# ── 32. config decay is logged (2026-10-31, audit C8) ─────────────────
_grab_32 = _LogGrab()
_lg_32 = _logging.getLogger("hivemind.mcp_client")
_lg_32.addHandler(_grab_32)
try:
    check("32a a non-list mcp_servers is decayed loudly",
          MC.McpClient._specs({"mcp_servers": "oops"}) == []
          and any("mcp_servers" in m and "list" in m
                  for m in _grab_32.messages),
          f"({_grab_32.messages[-1:]})")
    _grab_32.messages.clear()

    import settings as _S_32                                             # noqa: E402
    _orig_load_32 = _S_32.load_settings

    def _broken_load():
        raise ValueError("broken settings file")

    _S_32.load_settings = _broken_load
    MC._SETTINGS_LOAD_WARNED = False
    _loaded_32 = MC._settings(None)
    _S_32.load_settings = _orig_load_32
    check("32b a broken settings load decays to {} loudly",
          _loaded_32 == {}
          and any("settings" in m.lower() for m in _grab_32.messages),
          f"({_grab_32.messages[-1:]})")
    _grab_32.messages.clear()

    MC.ServerSpec({"name": "both", "url": "http://x/mcp", "command": "py"}, 0)
    check("32c url + command together is decayed loudly",
          any("'url' and 'command'" in m for m in _grab_32.messages),
          f"({_grab_32.messages[-1:]})")
finally:
    _lg_32.removeHandler(_grab_32)


print()
print(f"SUMMARY mcp_client: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
