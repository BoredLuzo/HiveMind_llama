"""send_file: deliver a run-written workspace file to the owner's Telegram.

The actual upload happens in the gateway when it receives the `send_file`
SSE event; this handler is the engine-side guard and acknowledgement. It
never puts file bytes into the model context.
"""
from __future__ import annotations

from pathlib import Path

from tools.errors import tool_error_response as _tool_error_response
from core.send_files import SendFileError, is_written_file, resolve_workspace_file


async def _inline_tool_send_file(args: dict, workspace: Path,
                                 workspace_lock: str | None) -> str:
    raw = str((args or {}).get("path") or "").strip()
    try:
        target = resolve_workspace_file(workspace, raw)
    except SendFileError as exc:
        return _tool_error_response(
            "SEND_FILE_BLOCKED", str(exc), tool="send_file",
            details={"path": raw[:300]})

    # Only files this run wrote itself. This is what keeps the tool from
    # becoming an arbitrary-file exfiltration channel for a poisoned model.
    try:
        from tools import runner as _runner
        written = _runner._files_written_in_run.get(None)
        started_at = _runner._run_file_epoch.get(None)
    except (ImportError, AttributeError, TypeError, OSError):
        written = None
        started_at = None
    if not is_written_file(str(target), workspace, written, started_at):
        return _tool_error_response(
            "SEND_FILE_UNTRACKED",
            "Only files written by THIS run can be sent. Create the file "
            "first with write_file/edit_file/run_python, then call send_file "
            "with its path.",
            tool="send_file",
            details={"path": raw[:300]})

    try:
        size = target.stat().st_size
    except OSError:
        return _tool_error_response(
            "SEND_FILE_UNREADABLE", "The file cannot be read.",
            tool="send_file", details={"path": raw[:300]})
    return (f"[FILE_QUEUED] {target.name} ({size} bytes) is queued for "
            "delivery after the final verification.")
