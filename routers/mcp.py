"""MCP client API: server status and reload."""
from __future__ import annotations

import logging

from fastapi import APIRouter

from infra import mcp_client
from settings import load_settings

logger = logging.getLogger("hivemind.mcp_client")
router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/servers")
async def mcp_servers() -> dict:
    """Configured servers plus what the running client knows right now."""
    settings = load_settings() or {}
    configured = settings.get("mcp_servers")
    return {
        "enabled": mcp_client.enabled(settings),
        "configured": configured if isinstance(configured, list) else [],
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
    except (OSError, ValueError, RuntimeError, KeyError, TypeError,
            AttributeError) as exc:
        logger.info("[MCP] reload failed: %s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, **status,
            "tools": [t["function"]["name"] for t in mcp_client.tool_defs(settings)]}
