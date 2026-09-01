"""Tool handlers: web tools (search/fetch) (part of tools/handlers, extracted from tools/handlers.py)."""

from __future__ import annotations

from pathlib import Path
from tools.errors import tool_error_response as _tool_error_response

from . import _shared


async def _inline_tool_web_search(args: dict, _workspace: Path, _workspace_lock: str | None) -> str:
    if not _shared._WEBSEARCH_AVAILABLE:
        return _tool_error_response(
            "WEBSEARCH_UNAVAILABLE",
            "web_search is unavailable because websearch.py is missing.",
            tool="web_search" )
    query = str(args.get("query", "")).strip()
    max_results = max(1, min(10, int(args.get("max_results") or 5)))
    if not query:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "web_search requires a non-empty query.",
            tool="web_search" )
    try:
        return await _shared._safe_web_search(query, max_results=max_results, phase="duo_tool")
    except Exception as e:
        return _tool_error_response(
            "WEBSEARCH_FAILED",
            f"web_search crashed: {type(e).__name__}: {str(e)[:200]}",
            tool="web_search" )


async def _inline_tool_web_fetch(args: dict, _workspace: Path, _workspace_lock: str | None) -> str:
    if not _shared._WEBSEARCH_AVAILABLE:
        return _tool_error_response(
            "WEBFETCH_UNAVAILABLE",
            "web_fetch is unavailable because websearch.py is missing.",
            tool="web_fetch" )
    url = str(args.get("url", "")).strip()
    if not url:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "web_fetch requires a non-empty url.",
            tool="web_fetch" )
    try:
        return await _shared._safe_web_fetch(url, phase="duo_tool")
    except Exception as e:
        return _tool_error_response(
            "WEBFETCH_FAILED",
            f"web_fetch crashed: {type(e).__name__}: {str(e)[:200]}",
            tool="web_fetch" )
