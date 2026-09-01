"""Tool handlers: miscellaneous helpers (datetime/task_complete/subagent) (part of tools/handlers, extracted from tools/handlers.py)."""

from __future__ import annotations

from pathlib import Path
from tools.errors import tool_error_response as _tool_error_response
import json

from . import _shared


async def _inline_tool_subagent_research(args: dict, workspace: Path, workspace_lock: str | None) -> str:


    task = str(args.get("task", "")).strip()
    if not task:
        return _tool_error_response(
            "MISSING_ARG", "task is required", tool="subagent_research")
    try:
        from core.subagent_lite import run_research
        return await run_research(task, workspace_lock=workspace_lock)
    except Exception as e:
        return _tool_error_response(
            "SUBAGENT_FAILED",
            f"subagent_research failed: {type(e).__name__}: {str(e)[:160]} "
            "— recherchiere inline.",
            tool="subagent_research")


async def _inline_tool_get_datetime(args: dict, _workspace: Path, _workspace_lock: str | None) -> str:
    """Return the current local date, time, weekday and timezone offset."""
    from datetime import datetime
    _now = datetime.now()
    _tz = _now.astimezone().strftime("%z")
    try:
        import zoneinfo as _zi
        _tzname = _now.astimezone().tzname() or ""
    except Exception:
        _tzname = ""
    return (
        f"[get_datetime]\n"
        f"Current local date/time: {_now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Weekday: {_now.strftime('%A')}\n"
        f"Timezone: UTC{_tz}{' (' + _tzname + ')' if _tzname else ''}"
    )


async def _inline_tool_task_complete(args: dict, _workspace: Path, _workspace_lock: str | None) -> str:
    """Handle task_complete — return structured status confirmation."""
    def _parse_task_complete_status(raw) -> dict:
        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        _s = str(raw).strip()
        try:
            _parsed = json.loads(_s)
            if isinstance(_parsed, dict):
                return _parsed
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            import ast as _ast  # noqa: E402
            _parsed = _ast.literal_eval(_s)
            if isinstance(_parsed, dict):
                return _parsed
        except Exception:
            pass
        if _s in ("completed", "blocked", "partial"):
            return {"build_status": _s}
        return {"raw": _s}

    status = _parse_task_complete_status(args.get("status", {}))
    completed = status.get("completed", []) if isinstance(status.get("completed"), list) else []
    blockers = status.get("blockers", []) if isinstance(status.get("blockers"), list) else []
    build_status = status.get("build_status", "untested")
    lines = [f"[task_complete] Build: {build_status}"]
    if completed:
        lines.append("Completed:\n  - " + "\n  - ".join(completed))
    else:
        lines.append("Completed: (none reported)")
    if blockers:
        lines.append("Blockers:\n  - " + "\n  - ".join(blockers))
    else:
        lines.append("Blockers: none")
    return "\n".join(lines)
