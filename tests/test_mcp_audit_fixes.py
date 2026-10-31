# -*- coding: utf-8 -*-
"""MCP client audit fixes (2026-10-28), behaviour-tested in-process.

Covers the findings of the MCP audit that the main suite did not:
  - a failed start() left a dead=False connection in _conns, so
    GET /mcp/servers showed "ok: true, tools: 0" for a server that never
    started, and a second refresh reused the zombie instead of retrying;
  - a 500/404 on the initialized notification was waved through and the
    session started anyway (only 401/403 raised);
  - a fixed token_env never applied, because the reused connection kept
    its stale bearer;
  - close_all kept _server_errors alive (the unknown-tool hint kept
    naming servers that are gone);
  - GET /mcp/servers echoed the raw settings - every env value and every
    literal Authorization header went to the browser.
"""
import asyncio
import http.server
import json
import sys
import threading
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


import routers.mcp as rm                    # noqa: E402
from infra import mcp_client as MC          # noqa: E402


def _make_http_server(mode: str):
    """Minimal fake: normal MCP answers unless the mode says otherwise.
    Modes: n401 / n500 -> that status on notifications/initialized;
    n502always -> 502 on everything; authtok -> 401 unless the bearer
    matches the MARKER (set as argv[2])."""
    state = {"requests": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                msg = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                msg = {}
            rid = msg.get("id")
            method = msg.get("method")
            state["requests"].append(method)

            def send(status, body_obj):
                raw = json.dumps(body_obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            if mode in ("n401", "n500") and method == "notifications/initialized":
                self.send_response(401 if mode == "n401" else 500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if mode == "n502always":
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if mode == "authtok":
                marker = (self.path or "").split("marker=")[-1]
                if (self.headers.get("Authorization") or "") != "Bearer " + marker:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            if method == "initialize":
                send(200, {"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": "2024-11-05", "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "0"}}})
            elif method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif method == "tools/list":
                send(200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                    {"name": "echo", "description": "e",
                     "inputSchema": {"type": "object", "properties": {}}}]}})
            elif method == "tools/call":
                send(200, {"jsonrpc": "2.0", "id": rid, "result": {"content": [
                    {"type": "text", "text": "ok"}]}})
            else:
                send(200, {"jsonrpc": "2.0", "id": rid, "result": {}})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.state = state
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/mcp"


# ── 1. failed start: no zombie, refresh retries ─────────────────────────
async def _start_fail_status():
    client = MC.McpClient()
    _cfg = {"mcp_client_enabled": True, "mcp_client_timeout_s": 5.0,
            "mcp_servers": [{"name": "nocmd", "args": []}]}
    st = await client.refresh(_cfg)
    running = client.status()
    st2 = await client.refresh(_cfg)
    running2 = client.status()
    await client.close_all()
    return st, running, st2, running2


def settings_with(server):
    return {"mcp_client_enabled": True, "mcp_client_timeout_s": 5.0,
            "mcp_servers": [server]}


_st, _run, _st2, _run2 = asyncio.run(_start_fail_status())
check("1a failed start reports ok:false with the reason",
      _st["servers"][0]["ok"] is False
      and "no 'command'" in _st["servers"][0]["error"],
      f"({_st['servers'][0]['error'][:40]!r})")
check("1b the status endpoint no longer shows a zombie",
      _run == [] or _run[0]["ok"] is False, f"({_run})")
check("1c a second refresh retries instead of reusing the zombie",
      _st2["servers"][0]["ok"] is False and _run2 == [],
      f"({len(_run2)} zombies)")

# ── 2. notification status codes ─────────────────────────────────────────
async def _notify_flow(mode):
    srv, url = _make_http_server(mode)
    try:
        client = MC.McpClient()
        st = await client.refresh({"mcp_client_enabled": True,
                                   "mcp_client_timeout_s": 60,
                                   "mcp_servers": [{"name": "httpfake", "url": url}]})
        running = client.status()
        await client.close_all()
        return st, running, srv.state
    finally:
        srv.shutdown()


_st401, _run401, _ = asyncio.run(_notify_flow("n401"))
check("2a 401 on the initialized notification fails the start loudly",
      _st401["servers"][0]["ok"] is False
      and "HTTP 401" in _st401["servers"][0]["error"],
      f"({_st401['servers'][0]['error'][:50]!r})")
check("2a2 no zombie after the 401",
      _run401 == [] or _run401[0]["ok"] is False, f"({_run401})")

_st500, _, _ = asyncio.run(_notify_flow("n500"))
check("2b 500 on the initialized notification fails the start (was waved through)",
      _st500["servers"][0]["ok"] is False
      and "HTTP 500" in _st500["servers"][0]["error"],
      f"({_st500['servers'][0]['error'][:50]!r})")

_st502, _, state502 = asyncio.run(_notify_flow("n502always"))
_inits = [r for r in state502["requests"] if r == "initialize"]
check("2c 502 on the notification retries to exhaustion then fails",
      _st502["servers"][0]["ok"] is False
      and "HTTP 502 after 4 attempts" in _st502["servers"][0]["error"],
      f"({_st502['servers'][0]['error'][:60]!r})")
check("2c2 exactly 4 attempts (1 + 3 retries)", len(_inits) == 4,
      f"({len(_inits)})")

# ── 3. a changed token_env applies after the reload ──────────────────────
import os as _os  # noqa: E402

_TOK = "HIVEMIND_MCP_AUDIT_TOKEN"


async def _stale_bearer_flow():
    srv, url = _make_http_server("authtok")
    _os.environ[_TOK] = "token-one"
    try:
        client = MC.McpClient()
        st1 = await client.refresh(settings_with(
            {"name": "httpfake", "url": url + "/?marker=token-one",
             "token_env": _TOK}))
        ok1 = st1["servers"][0]["ok"]
        _os.environ[_TOK] = "token-two"
        st2 = await client.refresh(settings_with(
            {"name": "httpfake", "url": url + "/?marker=token-two",
             "token_env": _TOK}))
        out = await client.call("mcp_httpfake_echo", {"text": "hi"})
        await client.close_all()
        return ok1, st2["servers"][0]["ok"], out
    finally:
        _os.environ.pop(_TOK, None)
        srv.shutdown()


_ok1, _ok2, _out = asyncio.run(_stale_bearer_flow())
check("3a the first token works", _ok1 is True)
check("3b a changed token_env applies after the reload (stale bearer fixed)",
      _ok2 is True and _out == "ok", f"({_out[:40]!r})")

# ── 4. close_all forgets the server errors ───────────────────────────────
async def _close_errors():
    client = MC.McpClient()
    await client.refresh(settings_with({"name": "nocmd", "args": []}))
    had = bool(client._server_errors)
    await client.close_all()
    return had, dict(client._server_errors)


_had, _after = asyncio.run(_close_errors())
check("4 close_all forgets the server errors",
      _had is True and _after == {}, f"({_after})")

# ── 5. /mcp/servers redacts configured entries ───────────────────────────
_load_orig = rm.load_settings
try:
    rm.load_settings = lambda: {"mcp_client_enabled": False, "mcp_servers": [
        {"name": "leaky", "command": "x", "env": {"SECRET_ENV": "v"},
         "headers": {"Authorization": "Bearer literal"}},
        {"name": "authy", "url": "http://127.0.0.1:9/mcp",
         "token_env": "SOME_TOKEN_VAR"},
    ]}
    payload = asyncio.run(rm.mcp_servers())
finally:
    rm.load_settings = _load_orig
blob = json.dumps(payload)
entries = payload.get("configured") or []
check("5a /mcp/servers redacts env and header values",
      "SECRET_ENV" not in blob and "Bearer literal" not in blob,
      f"({blob[:100]})")
_http_entry = next((e for e in entries if e.get("kind") == "http"), {})
check("5a2 the http entry reports has_auth (flag, not value)",
      _http_entry.get("has_auth") is True and "SOME_TOKEN_VAR" not in blob,
      f"(http entry: {json.dumps(_http_entry)[:80]})")

# ── 6. Integration round 2 (2026-10-31): budgets on the IDE path, mode ──
#    decision, destructive gate for MCP args, direct_full comment
from tools import runner as TR               # noqa: E402
from tools.definitions import mcp_allowed_in_mode   # noqa: E402

# 6a S3: the contextless /internal/tool/exec path seeds SHARED budget
# windows instead of getting a fresh (unlimited) counter per call.
async def _seed_shape():
    TR._external_budget_windows.clear()
    TR._seed_external_budgets()
    ws = TR._web_search_count.get()
    fetch = TR._web_fetch_count.get()
    inst = TR._install_count.get()
    mcp = TR._mcp_call_count.get()
    shared = dict(TR._external_budget_windows)
    TR._seed_external_budgets()   # second seed inside the window: same lists
    return (ws, fetch, inst, mcp, shared,
            TR._web_search_count.get(), TR._web_fetch_count.get(),
            TR._install_count.get(), TR._mcp_call_count.get())


_w1, _f1, _i1, _m1, _shared, _w2, _f2, _i2, _m2 = asyncio.run(_seed_shape())
check("6a all four budgets are wired to the shared windows",
      _w1 is _shared.get("web_search") and _f1 is _shared.get("web_fetch")
      and _i1 is _shared.get("install") and _m1 is _shared.get("mcp"),
      f"({sorted(_shared)})")
check("6a2 re-seeding within the window keeps the same counters",
      _w2 is _w1 and _f2 is _f1 and _i2 is _i1 and _m2 is _m1)

# 6b S3: an expired window resets the shared counters.
async def _window_reset():
    TR._external_budget_windows.clear()
    TR._seed_external_budgets()
    old = TR._external_budget_windows["web_search"]
    old[0] = 7
    old[1] -= TR._EXTERNAL_BUDGET_WINDOW_S + 1.0
    TR._seed_external_budgets()
    return old, TR._external_budget_windows["web_search"]


_old, _new = asyncio.run(_window_reset())
check("6b an expired window starts a fresh counter",
      _new is not _old and _new[0] == 0, f"({ _old} -> {_new})")
TR._external_budget_windows.clear()

# 6c S3/NO-CONTEXT: without external dispatch the governors keep the
# deliberate fresh-per-call behaviour - they never touch the shared windows.
async def _no_context_consume():
    TR._external_budget_windows.clear()
    before = dict(TR._external_budget_windows)
    TR._consume_web_search_budget()
    TR._consume_install_budget()
    TR._consume_mcp_call_budget()
    return before, dict(TR._external_budget_windows)


_before, _after = asyncio.run(_no_context_consume())
check("6c contextless direct calls do not touch the shared windows",
      _before == {} and _after == {}, f"({_after})")
TR._external_budget_windows.clear()

# 6d S3: the real external call seeds the windows (mcp client is disabled in
# this environment, so the call ends in MCP_DISABLED - after seeding).
async def _external_call_seeds():
    tok = TR._external_dispatch.set(True)
    try:
        TR._external_budget_windows.clear()
        out = await TR._run_inline_tool("mcp_auditfix_probe", {})
        return out, dict(TR._external_budget_windows)
    finally:
        TR._external_dispatch.reset(tok)
        TR._external_budget_windows.clear()


_out, _win = asyncio.run(_external_call_seeds())
check("6d an /internal/tool/exec-style call seeds the budget windows",
      "MCP_DISABLED" in _out and {"web_search", "web_fetch", "install", "mcp"} <= set(_win),
      f"({sorted(_win)}, out={_out[:40]!r})")

# 6e I3: the mcp_* budget trips like the other governors (fallback limit 40,
# no settings key yet).
async def _mcp_budget_trip():
    TR._external_budget_windows.clear()
    results = []
    for _ in range(64):
        results.append(TR._consume_mcp_call_budget())
        if results[-1]:
            break
    return results


_res = asyncio.run(_mcp_budget_trip())
check("6e the mcp call budget trips (first True at limit+1)",
      _res[-1] is True and all(r is False for r in _res[:-1])
      and len(_res) == TR._MCP_CALL_BUDGET_DEFAULT + 1,
      f"(first True after {len(_res)} calls)")
TR._external_budget_windows.clear()

# 6f I5: the destructive gate also inspects MCP tool args now.
check("6f precheck: the probe pattern is destructive",
      TR._is_destructive_bash("format C:") is not None)
_gate_block = asyncio.run(TR._destructive_gate("mcp_probe_tool", {"cmd": "format C:"}))
check("6f2 destructive MCP args hit the gate (no run_id -> denied)",
      _gate_block is not None and "DESTRUCTIVE GATE" in _gate_block,
      f"({_gate_block[:60]!r})")
_gate_clean = asyncio.run(TR._destructive_gate("mcp_probe_tool", {"path": "notes.txt"}))
check("6f3 benign MCP args pass the gate", _gate_clean is None)

# 6g I4: the mode-less decision is explicit - documented in the code and
# never used by the HTTP surface.
check("6g mode-less calls are a documented MCP allowance",
      mcp_allowed_in_mode(None) is True)
check("6g2 the endpoint default mode keeps MCP access",
      mcp_allowed_in_mode("mcp_agent") is True)
check("6g3 read-only modes stay excluded",
      mcp_allowed_in_mode("duo_readonly") is False)
_src_core6 = (ROOT / "routers" / "core.py").read_text(encoding="utf-8", errors="replace")
_src_defs = (ROOT / "tools" / "definitions.py").read_text(encoding="utf-8", errors="replace")
check("6g4 the HTTP surface defaults to mcp_agent (never mode-less)",
      '"tool_mode", "mcp_agent"' in _src_core6)
check("6g5 the None-escape is documented in mcp_allowed_in_mode",
      "documented decision" in _src_defs)

# 6h I15: the MCP_TOOL_MODES comment no longer claims direct_full is
# excluded.
_block = _src_defs.split("MCP_TOOL_MODES = frozenset")[0][-700:]
check("6h the comment states direct_full stays included",
      'direct_full' in _block and '(mode "direct")' in _block, f"({_block[-120:]!r})")

print()
print(f"SUMMARY mcp_audit_fixes: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
