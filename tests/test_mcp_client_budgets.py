# -*- coding: utf-8 -*-
"""MCP client: per-server call budgets, annotations passthrough, config gen.

Covers the client-direction additions of 2026-10-31:
  - mcp_server_budgets: rolling 1 h window per server, standard tool-error
    shape on exhaustion, server NOT marked dead, next window works again,
    unlimited by default, counters survive a reload.
  - tool annotations from a server's tools/list pass through into the
    tool defs verbatim (missing -> key omitted).
  - tools/mcp_config_gen.py: every format parses as JSON, placeholder
    default, --include-secret embeds the env token, --check passes.

The budget/annotation flows drive the real client against a real child
process (stdlib MCP fake server), like tests/test_mcp_client.py does.
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from collections import deque
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
import json, sys

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
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0"}}})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": "echo", "description": "Echo text back",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}}},
             "annotations": {"readOnlyHint": True, "title": "Echo"}},
            {"name": "plain", "description": "No annotations here",
             "inputSchema": {"type": "object", "properties": {}}},
        ]}})
    elif method == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
            {"type": "text", "text": "echo: " + str(args.get("text", ""))}]}})
'''


def spec(name: str) -> dict:
    d = Path(tempfile.mkdtemp(prefix="hm_mcp_budget_"))
    script = d / "fake.py"
    script.write_text(FAKE, encoding="utf-8")
    return {"name": name, "command": sys.executable,
            "args": [str(script)], "enabled": True}


def settings_with(*servers: dict, budgets=None, timeout_s: float = 5.0) -> dict:
    s = {"mcp_client_enabled": True,
         "mcp_client_timeout_s": timeout_s,
         "mcp_servers": list(servers)}
    if budgets is not None:
        s["mcp_server_budgets"] = budgets
    return s


# ── 1. The rolling-window counter (injected clock) ─────────────────────
async def _budget_unit():
    b = MC._ServerCallBudget()
    r1 = await b.take(2, now=1000.0)
    r2 = await b.take(2, now=2500.0)
    r3 = await b.take(2, now=4300.0)   # both stamps inside the window
    r4 = await b.take(2, now=4601.0)   # stamp 1000 slid out -> allowed again
    r5 = await b.take(2, now=6100.0)   # stamp 2500 slid out -> allowed again
    r6 = await b.take(2, now=6102.0)   # 4601 and 6100 both inside -> denied
    return r1, r2, r3, r4, r5, r6


_r1, _r2, _r3, _r4, _r5, _r6 = asyncio.run(_budget_unit())
check("1a free slots are recorded", _r1 == (True, 0.0) and _r2 == (True, 0.0),
      f"({_r1}, {_r2})")
check("1b the limit is denied with the reset time of the OLDEST stamp",
      _r3 == (False, 300.0), f"({_r3})")
check("1c the window rolls: an expired stamp frees a slot",
      _r4 == (True, 0.0) and _r5 == (True, 0.0), f"({_r4}, {_r5})")
check("1d after refill the limit denies again", _r6 == (False, 2099.0),
      f"({_r6})")


# ── 2. _parse_budgets: tolerant, only positive integers count ──────────
check("2a a missing key means unlimited", MC._parse_budgets({}) == {})
check("2b a positive limit is kept",
      MC._parse_budgets({"mcp_server_budgets": {"a": 5}}) == {"a": 5})
check("2c a numeric string is accepted",
      MC._parse_budgets({"mcp_server_budgets": {"a": "7"}}) == {"a": 7})
check("2d 0 / negative / bool / garbage mean unlimited",
      MC._parse_budgets({"mcp_server_budgets": {
          "a": 0, "b": -3, "c": True, "d": "x", "e": 2}}) == {"e": 2})
check("2e an explicit null means unlimited",
      MC._parse_budgets({"mcp_server_budgets": None}) == {})


class _LogGrab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


_grab_2f = _LogGrab()
_lg_2f = logging.getLogger("hivemind.mcp_client")
_lg_2f.addHandler(_grab_2f)
try:
    check("2f a non-dict budgets value is decayed loudly",
          MC._parse_budgets({"mcp_server_budgets": "oops"}) == {}
          and any("mcp_server_budgets" in m for m in _grab_2f.messages),
          f"({_grab_2f.messages[-1:]})")
finally:
    _lg_2f.removeHandler(_grab_2f)


# ── 3. Budget enforcement against a real server ────────────────────────
async def _budget_flow():
    client = MC.McpClient()
    st = await client.refresh(settings_with(spec("bfake"),
                                            budgets={"bfake": 2}))
    r1 = await client.call("mcp_bfake_echo", {"text": "one"})
    r2 = await client.call("mcp_bfake_echo", {"text": "two"})
    r3 = await client.call("mcp_bfake_echo", {"text": "three"})
    conn = client._conns.get("bfake")
    alive = conn is not None and not conn.dead and st["servers"][0]["ok"]
    status_ok = any(e["name"] == "bfake" and e["ok"] for e in client.status())
    # a reload must NOT reset the budget (editing config = loophole)
    await client.refresh(settings_with(spec("bfake"), budgets={"bfake": 2}))
    r4 = await client.call("mcp_bfake_echo", {"text": "four"})
    # simulate the window passing: backdate the recorded stamps
    budget = client._budgets.get("bfake")
    shift = MC.MCP_BUDGET_WINDOW_S + 5.0
    budget._stamps = deque(t - shift for t in budget._stamps)
    r5 = await client.call("mcp_bfake_echo", {"text": "five"})
    await client.close_all()
    return r1, r2, r3, alive, status_ok, r4, r5


_r1, _r2, _r3, _alive, _status_ok, _r4, _r5 = asyncio.run(_budget_flow())
check("3a calls inside the budget succeed",
      _r1 == "echo: one" and _r2 == "echo: two", f"({_r1!r}, {_r2!r})")
check("3b the exhausted call fails with the standard tool-error shape",
      _r3.startswith("[TOOL_ERROR: MCP_BUDGET_EXCEEDED]"),
      f"({_r3[:60]!r})")
check("3c the error names the server and the limit",
      "bfake" in _r3 and "2 tool call(s)" in _r3, f"({_r3!r})")
check("3d the error says when the window resets",
      "resets in" in _r3, f"({_r3!r})")
check("3e exhaustion does NOT mark the server dead",
      _alive and _status_ok, f"(alive={_alive}, status_ok={_status_ok})")
check("3f a reload does not reset the budget either",
      _r4.startswith("[TOOL_ERROR: MCP_BUDGET_EXCEEDED]"),
      f"({_r4[:50]!r})")
check("3g the next window works again", _r5 == "echo: five", f"({_r5!r})")


async def _unlimited_flow():
    client = MC.McpClient()
    await client.refresh(settings_with(spec("ufree")))
    outs = [await client.call("mcp_ufree_echo", {"text": str(i)})
            for i in range(4)]
    limits = client._budget_limits
    await client.close_all()
    return outs, limits


_outs_u, _limits_u = asyncio.run(_unlimited_flow())
check("3h no budgets key means unlimited (default stays unlimited)",
      all(o == f"echo: {i}" for i, o in enumerate(_outs_u))
      and _limits_u == {}, f"({_limits_u})")


async def _per_server_flow():
    client = MC.McpClient()
    await client.refresh(settings_with(spec("bsolo"), spec("bfree"),
                                       budgets={"bsolo": 1}))
    solo1 = await client.call("mcp_bsolo_echo", {"text": "a"})
    solo2 = await client.call("mcp_bsolo_echo", {"text": "b"})
    free = [await client.call("mcp_bfree_echo", {"text": str(i)})
            for i in range(3)]
    await client.close_all()
    return solo1, solo2, free


_solo1, _solo2, _free = asyncio.run(_per_server_flow())
check("3i the budget is per server: bsolo is capped after 1 call",
      _solo1 == "echo: a" and _solo2.startswith("[TOOL_ERROR: MCP_BUDGET_EXCEEDED]"),
      f"({_solo2[:50]!r})")
check("3j a server without a limit stays unlimited",
      all(f == f"echo: {i}" for i, f in enumerate(_free)))


# ── 4. Annotations passthrough ──────────────────────────────────────────
async def _annotations_flow():
    client = MC.McpClient()
    await client.refresh(settings_with(spec("ann")))
    defs = {d["function"]["name"]: d["function"] for d in client.tool_defs()}
    await client.close_all()
    return defs


_defs_ann = asyncio.run(_annotations_flow())
echo_fn = _defs_ann.get("mcp_ann_echo", {})
plain_fn = _defs_ann.get("mcp_ann_plain", {})
check("4a a remote tool's annotations pass through verbatim",
      echo_fn.get("annotations") == {"readOnlyHint": True, "title": "Echo"},
      f"({echo_fn.get('annotations')})")
check("4b a tool without annotations has NO annotations key",
      "annotations" not in plain_fn, f"({sorted(plain_fn)})")
check("4c annotations land inside the function object (next to parameters)",
      "parameters" in echo_fn and "annotations" in echo_fn)

_d1 = MC._tool_def("mcp_x_t", "x", {"name": "t", "annotations":
                                    {"readOnlyHint": True}})
_d2 = MC._tool_def("mcp_x_t", "x", {"name": "t"})
_d3 = MC._tool_def("mcp_x_t", "x", {"name": "t", "annotations": {}})
check("4d _tool_def passes annotations through",
      _d1["function"]["annotations"] == {"readOnlyHint": True})
check("4e missing annotations omit the key entirely",
      "annotations" not in _d2["function"])
check("4f an empty annotations dict omits the key too (no empty dict)",
      "annotations" not in _d3["function"])


# ── 5. tools/mcp_config_gen.py ──────────────────────────────────────────
from tools import mcp_config_gen as CG  # noqa: E402

_TOKEN_TEST = "hivemind-test-token-12345"
_TOP_KEYS = {"claude_desktop": "mcpServers", "cline": "mcpServers",
             "zed": "context_servers", "vscode": "servers"}

for _fmt, _top in _TOP_KEYS.items():
    try:
        _doc = json.loads(CG.build(_fmt))
        _entry = _doc[_top][CG.SERVER_NAME]
        _ok = (_top in _doc
               and _entry.get("args", [""])[0] == str(ROOT / "infra" / "mcp_server.py")
               and (_entry.get("command") == "python"
                    or Path(_entry.get("command", "")).is_absolute())
               and _entry.get("env", {}).get(CG.TOKEN_ENV) == CG.TOKEN_PLACEHOLDER)
        check(f"5a {_fmt} builds valid JSON with command/args/env", _ok,
              f"(keys={sorted(_doc)}, entry={sorted(_entry)})")
    except (ValueError, KeyError, TypeError) as _exc:
        check(f"5a {_fmt} builds valid JSON with command/args/env", False,
              f"({_exc})")

check("5b paths resolve from THIS checkout's root",
      CG.REPO_ROOT == ROOT and CG.SERVER_SCRIPT == ROOT / "infra" / "mcp_server.py")

_saved_token = os.environ.get(CG.TOKEN_ENV)
os.environ[CG.TOKEN_ENV] = _TOKEN_TEST
try:
    _plain = CG.build("vscode")
    check("5c the placeholder is the default even when the env token is set",
          CG.TOKEN_PLACEHOLDER in _plain and _TOKEN_TEST not in _plain)
    _secret = CG.build("vscode", include_secret=True)
    check("5d --include-secret embeds the real env value",
          _TOKEN_TEST in _secret and CG.TOKEN_PLACEHOLDER not in _secret)
    _secret_missing = None
    del os.environ[CG.TOKEN_ENV]
    _secret_missing = CG.build("vscode", include_secret=True)
    check("5e --include-secret without the variable keeps the placeholder",
          CG.TOKEN_PLACEHOLDER in _secret_missing)
finally:
    if _saved_token is not None:
        os.environ[CG.TOKEN_ENV] = _saved_token
    else:
        os.environ.pop(CG.TOKEN_ENV, None)

check("5f settings.json secrets are never read into the output "
      "(only server_port is looked at)",
      "server_port" in
      (ROOT / "tools" / "mcp_config_gen.py").read_text(encoding="utf-8",
                                                       errors="replace")
      and "api_key" not in
      (ROOT / "tools" / "mcp_config_gen.py").read_text(encoding="utf-8",
                                                       errors="replace"))

_cli = ROOT / "tools" / "mcp_config_gen.py"


def _run_cli(*cli_args: str, token: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if token is None:
        env.pop(CG.TOKEN_ENV, None)
    else:
        env[CG.TOKEN_ENV] = token
    return subprocess.run([sys.executable, str(_cli), *cli_args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=str(ROOT),
                          timeout=30)


_r = _run_cli("--check")
check("5g --check validates all formats", _r.returncode == 0
      and all(f"OK {f}" in _r.stdout for f in _TOP_KEYS),
      f"(rc={_r.returncode}, out={_r.stdout.strip()[-60:]!r})")
_r = _run_cli("--format", "vscode", "--check")
check("5h --check works for a single format", _r.returncode == 0
      and "OK vscode" in _r.stdout, f"(rc={_r.returncode})")

_outfile = Path(tempfile.mkdtemp(prefix="hm_mcp_cfg_")) / "mcp.json"
_r = _run_cli("--format", "claude_desktop", "--out", str(_outfile))
_file_ok = False
try:
    _file_doc = json.loads(_outfile.read_text(encoding="utf-8"))
    _file_ok = ("mcpServers" in _file_doc
                and CG.TOKEN_PLACEHOLDER in _outfile.read_text(encoding="utf-8"))
except (OSError, ValueError):
    pass
check("5i --out writes a parseable file with the placeholder",
      _r.returncode == 0 and _file_ok and _TOKEN_TEST not in (_r.stdout or "")
      and _TOKEN_TEST not in _outfile.read_text(encoding="utf-8"),
      f"(rc={_r.returncode}, exists={_outfile.exists()})")

_r = _run_cli("--format", "zed", "--include-secret", token=_TOKEN_TEST)
check("5j --include-secret via CLI embeds the env value",
      _r.returncode == 0 and _TOKEN_TEST in _r.stdout
      and CG.TOKEN_PLACEHOLDER not in _r.stdout, f"(rc={_r.returncode})")

_r = _run_cli("--format", "zed", token=_TOKEN_TEST)
check("5k without --include-secret the CLI never prints a real token",
      _r.returncode == 0 and _TOKEN_TEST not in _r.stdout
      and CG.TOKEN_PLACEHOLDER in _r.stdout, f"(rc={_r.returncode})")

_r = _run_cli()
check("5l the default run prints all four formats", _r.returncode == 0
      and all(f in _r.stdout for f in _TOP_KEYS), f"(rc={_r.returncode})")


print()
print(f"SUMMARY mcp_client_budgets: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
