# -*- coding: utf-8 -*-
"""MCP 2026-07-28 minimal support (server/discover, _meta version, stateless).

Run: python tests/test_mcp_2026.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="summary")

check = h.check


import infra.mcp_server as mcp  # noqa: E402

_META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}


def _hr(req):
    return asyncio.run(mcp.handle_request(req, "http"))


# ── 1. server/discover ───────────────────────────────────────────────────
_r = _hr({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
check("1a server/discover answers", "result" in _r)
check("1b it carries the supported versions",
      _r["result"]["protocolVersions"] == list(mcp._SUPPORTED_PROTOCOL_VERSIONS),
      f"({_r['result']['protocolVersions']})")
check("1c it carries identity + capabilities",
      _r["result"]["serverInfo"]["name"] == "hivemind-mcp"
      and {"tools", "resources", "prompts"} <= set(_r["result"]["capabilities"]))
_r = _hr({"jsonrpc": "2.0", "id": 2, "method": "server/discover"}, )
check("1d discover works on stdio too", "result" in _r)

# ── 2. per-request _meta version ─────────────────────────────────────────
_r = _hr({"jsonrpc": "2.0", "id": 3, "method": "tools/list",
          "params": {"_meta": dict(_META)}})
check("2a a 2026 _meta request proceeds stateless",
      "result" in _r and len(_r["result"]["tools"]) >= 10)
_r = _hr({"jsonrpc": "2.0", "id": 4, "method": "tools/list",
          "params": {"_meta": {"io.modelcontextprotocol/protocolVersion":
                               "2024-11-05"}}})
check("2b an older _meta version is accepted",
      "result" in _r)
_r = _hr({"jsonrpc": "2.0", "id": 5, "method": "tools/list",
          "params": {"_meta": {"io.modelcontextprotocol/protocolVersion":
                               "2027-01-01"}}})
check("2c an unsupported _meta version -> -32022",
      _r.get("error", {}).get("code") == mcp._ERR_UNSUPPORTED_PROTOCOL_VERSION,
      f"({_r.get('error')})")
_r = _hr({"jsonrpc": "2.0", "id": 6, "method": "tools/list"})
check("2d no _meta -> legacy path untouched", "result" in _r)

# ── 3. initialize with 2026: echo verbatim, stateless ────────────────────
_n_before = len(mcp._SESSIONS)
_r = _hr({"jsonrpc": "2.0", "id": 7, "method": "initialize",
          "params": {"protocolVersion": "2026-07-28", "capabilities": {},
                     "clientInfo": {"name": "t"}}})
check("3a a 2026 initialize echoes 2026-07-28",
      _r["result"]["protocolVersion"] == "2026-07-28")
check("3b it creates NO session (2026 is stateless)",
      len(mcp._SESSIONS) == _n_before)
_r = _hr({"jsonrpc": "2.0", "id": 8, "method": "initialize",
          "params": {"protocolVersion": "2026-06-01", "capabilities": {},
                     "clientInfo": {"name": "t"}}})
check("3c an unknown version falls back to the baseline",
      _r["result"]["protocolVersion"] == "2024-11-05")

# ── 4. batched initialize is baseline-answered for every version ─────────
for _ver in ("2025-06-18", "2026-07-28"):
    _item = mcp._baseline_batch_item(
        {"jsonrpc": "2.0", "id": 9, "method": "initialize",
         "params": {"protocolVersion": _ver, "capabilities": {},
                    "clientInfo": {"name": "t"}}})
    _r = asyncio.run(mcp.handle_request(_item, "http"))
    check(f"4a a batched {_ver} initialize is answered on the 2024 baseline",
          _r["result"]["protocolVersion"] == "2024-11-05")
_item = mcp._baseline_batch_item({"jsonrpc": "2.0", "id": 10,
                                  "method": "initialize",
                                  "params": {"protocolVersion": "2024-11-05",
                                             "capabilities": {}}})
check("4b a batched 2024 initialize passes through untouched",
      _item["params"]["protocolVersion"] == "2024-11-05")
check("4c a non-initialize batch item passes through untouched",
      mcp._baseline_batch_item({"jsonrpc": "2.0", "id": 11,
                                "method": "ping"})["method"] == "ping")

sys.exit(h.finish("mcp_2026"))
