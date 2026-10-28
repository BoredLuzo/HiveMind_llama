"""MCP client API: server status and reload."""
from __future__ import annotations

import logging

from fastapi import APIRouter

from infra import mcp_client
from infra.mcp_client import McpError, ServerSpec
from settings import load_settings

logger = logging.getLogger("hivemind.mcp_client")
router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/servers")
async def mcp_servers() -> dict:
    """Configured servers plus what the running client knows right now."""
    settings = load_settings() or {}
    configured = settings.get("mcp_servers")
    out: list = []
    if isinstance(configured, list):
        # REDACT (2026-10-28, mcp audit): the raw settings echo shipped every
        # env value and every literal header (an Authorization header is a
        # supported config!) to the browser. describe() carries only the
        # safe fields and flags auth as has_auth.
        for i, entry in enumerate(configured):
            if isinstance(entry, dict):
                try:
                    out.append(ServerSpec(entry, i).describe())
                except Exception:
                    out.append({"name": str(entry.get("name") or f"server{i}"),
                                "enabled": False, "kind": "unknown"})
    return {
        "enabled": mcp_client.enabled(settings),
        "configured": out,
        "timeout_s": mcp_client.timeout_s(settings),
        "running": mcp_client.status(),
        "tools": [t["function"]["name"] for t in mcp_client.tool_defs(settings)],
    }


@router.post("/reload")
async def mcp_reload() -> dict:
    """Start the enabled servers (or restart a dead one) and rebuild the tools."""
    settings = load_settings() or {}
    try:
        status = await mcp_client.refresh(settings)
    except McpError as exc:
        logger.info("[MCP] reload failed: %s", exc)
        return {"ok": False, "error": f"McpError: {exc}"}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError,
            AttributeError) as exc:
        logger.info("[MCP] reload failed: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, **status,
            "tools": [t["function"]["name"] for t in mcp_client.tool_defs(settings)]}
