"""Tool handlers: git tools (part of tools/handlers, extracted from tools/handlers.py)."""

from __future__ import annotations

from pathlib import Path
from utils.file import _inline_check_workspace
from tools.errors import tool_error_response as _tool_error_response
import asyncio

from . import _shared


async def _inline_tool_git_status(args: dict, workspace: Path, _workspace_lock: str | None) -> str:
    # 3.7-FIX (2026-08-24): Gate auf _shared._GIT_TOOLS_AVAILABLE wie bei git_commit —
    if not _shared._GIT_TOOLS_AVAILABLE:
        return _tool_error_response(
            "GIT_UNAVAILABLE",
            "git tools not loaded - git features disabled",
            tool="git_status")
    cmd = args.get("cmd", "status")
    if cmd not in ("status", "diff", "log", "show"):
        cmd = "status"
    try:
        r = await asyncio.create_subprocess_exec(
            "git", cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace))
        out, err = await asyncio.wait_for(r.communicate(), 15)
        stdout_str = out.decode(errors="replace").strip()
        stderr_str = err.decode(errors="replace").strip()
        if r.returncode != 0:
            return _tool_error_response(
                "GIT_STATUS_FAILED",
                f"git {cmd} exited with code {r.returncode}: {stderr_str[:500]}",
                tool="git_status",
                details={"cmd": cmd, "exit_code": r.returncode})
        return (stdout_str or stderr_str)[:4000]
    except Exception as e:
        return _tool_error_response(
            "GIT_STATUS_FAILED",
            f"git {cmd} failed: {e}",
            tool="git_status" )


async def _inline_tool_git_commit(args: dict, workspace: Path, _workspace_lock: str | None) -> str:
    """Git commit tool — delegates to git_tools._shared.exec_git_commit."""
    if not _shared._GIT_TOOLS_AVAILABLE or _shared.exec_git_commit is None:
        return _tool_error_response("GIT_UNAVAILABLE", "git_tools.py not loaded — git features disabled", tool="git_commit")
    message = str(args.get("message", "")).strip()
    if not message:
        return _tool_error_response("MISSING_ARG", "message is required", tool="git_commit")
    ws = str(args.get("workspace", "")).strip() or str(workspace)
    if err := _inline_check_workspace(Path(ws), _workspace_lock, "git_commit"):
        return err
    # GIT-AUDIT P1-3 (2026-11-01): the model-callable tool did
    # `git add -A` + a bare commit (which swept the whole index — exactly
    # what the 2026-10-14 policy bans). Paths are required and the commit is
    # scoped to them; the add -A branch is gone from exec_git_commit entirely
    # (2026-11-03).
    _paths = args.get("paths")
    if not isinstance(_paths, list) or not [str(x) for x in _paths if str(x).strip()]:
        return _tool_error_response(
            "MISSING_ARG",
            "paths is required: list the files you changed in this run "
            "(workspace-relative). The whole index is never swept.",
            tool="git_commit")
    _paths = [str(x).strip() for x in _paths if str(x).strip()]
    # EOD-GIT-AUDIT: "." / ".." sweep the whole workspace through a scoped
    # pathspec — reject them explicitly alongside pathspec magic (":"),
    # symlinks/junctions escaping the workspace, root aliases ("./", "sub/.."),
    # and secret paths.
    import os as _os
    from utils.file import is_secret_path as _is_secret_path, _is_junction as _is_junction

    ws_path = Path(ws)
    try:
        ws_res = ws_path.resolve()
    except OSError as _ws_exc:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            f"git_commit invalid workspace {ws}: {_ws_exc}",
            tool="git_commit",
        )

    _clean_paths: list[str] = []
    _bad: list[str] = []
    for _raw in _paths:
        if (
            _raw in (".", "..")
            or _raw.startswith("../")
            or _raw.startswith("..\\")
            or _raw.startswith(":")
        ):
            _bad.append(_raw)
            continue
        _cand_raw = Path(_raw)
        _cand = _cand_raw if _cand_raw.is_absolute() else (ws_path / _cand_raw)
        _escaped = False
        try:
            _rel_parts = Path(_os.path.normpath(str(_cand))).relative_to(
                Path(_os.path.normpath(str(ws_path)))
            ).parts
            _cur = ws_path
            for _part in _rel_parts:
                _cur = _cur / _part
                if _cur.is_symlink() or _is_junction(_cur):
                    try:
                        _cur_res = _cur.resolve()
                    except OSError:
                        _escaped = True
                        break
                    if not _cur_res.is_relative_to(ws_res) or (
                        _is_junction(_cur) and _cur_res == _cur
                    ):
                        _escaped = True
                        break
        except (ValueError, OSError):
            pass
        if _escaped:
            _bad.append(_raw)
            continue
        try:
            _cand_res = _cand.resolve()
        except OSError:
            _bad.append(_raw)
            continue
        if (
            not _cand_res.is_relative_to(ws_res)
            or _cand_res == ws_res
            or _is_secret_path(_cand_res, ws_res)
            or _is_secret_path(_raw, ws_res)
        ):
            _bad.append(_raw)
            continue
        _clean_paths.append(_cand_res.relative_to(ws_res).as_posix())

    if _bad:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            f"git_commit paths must stay inside the workspace and must not "
            f"target root, secrets, or pathspec magic — got: {', '.join(_bad[:3])}",
            tool="git_commit")
    _paths = list(dict.fromkeys(_clean_paths))
    if not _paths:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "git_commit requires at least one valid workspace-relative path",
            tool="git_commit",
        )
    try:
        # exec_git_commit is the single funnel; scoping to `_paths` is its
        # only staging strategy (the add -A escape hatch was removed with the
        # 2026-11-03 audit - a commit without a file list refuses).
        result = await _shared.exec_git_commit(message=message, workspace=ws,
                                               files=_paths)
        return result
    except Exception as e:
        return _tool_error_response(
            "GIT_COMMIT_FAILED",
            f"git_commit failed: {e}",
            tool="git_commit" )
