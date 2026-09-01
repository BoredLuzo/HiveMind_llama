# -*- coding: utf-8 -*-
"""Datei-Utilities (aus server.py extrahiert)."""
from __future__ import annotations
import hashlib as _hashlib
import json
import os
import tempfile
from difflib import get_close_matches
from pathlib import Path
import re
import sys as _sys


# HiveMind source root — two levels up from utils/file.py
_HIVEMIND_ROOT = Path(__file__).parent.parent.resolve()

# Files the coder must never overwrite regardless of workspace setting
_PROTECTED_PATHS: frozenset = frozenset({
    _HIVEMIND_ROOT / "settings.json",
    _HIVEMIND_ROOT / "soul.json",
    _HIVEMIND_ROOT / "memories_db.json",
})


def _is_protected_path(p: Path) -> bool:
    """Check if a path targets a HiveMind internal file that must not be modified."""
    try:
        _resolved = p.resolve()
    except Exception:
        _resolved = p
    if _resolved in _PROTECTED_PATHS:
        return True
    if _resolved.name.endswith(".context.json"):
        return True
    try:
        _resolved.relative_to(_HIVEMIND_ROOT)
        if _resolved.suffix in (".py", ".pyc"):
            return True
    except ValueError:
        pass
    return False


def write_json_atomic(path: Path, payload: dict):
    """Atomically persist JSON payload to avoid partial/corrupt files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as _tf:
            json.dump(payload, _tf, indent=2, ensure_ascii=False)
            tmp_name = _tf.name
        Path(tmp_name).replace(path)
        tmp_name = None
    finally:
        if tmp_name:
            try:
                _tp = Path(tmp_name)
                if _tp.exists():
                    _tp.unlink()
            except Exception:
                pass


def quick_file_sha1(path: Path, sample_bytes: int = 8192) -> str:
    """Fast content signature: head (+tail for larger files)."""
    h = _hashlib.sha1()
    st = path.stat()
    with path.open("rb") as f:
        head = f.read(sample_bytes)
        h.update(head)
        if st.st_size > sample_bytes:
            if st.st_size > (sample_bytes * 2):
                f.seek(max(0, st.st_size - sample_bytes))
                h.update(f.read(sample_bytes))
            else:
                h.update(f.read())
    return h.hexdigest()[:16]


def _inline_resolve_path(workspace: Path, raw: str) -> Path:
    """Cross-platform path resolution for inline tools."""
    if re.match(r'^[A-Za-z]:[/\\]', raw or ""):
        return Path(raw)
    p = Path(raw)
    if p.is_absolute():
        return p
    return workspace / p


def _is_junction(p: Path) -> bool:
    """
    Returns True if p is a Windows directory junction (reparse point).
    Safe on Python 3.10+. Always returns False on non-Windows platforms.
    """
    if _sys.platform != "win32":
        return False
    if hasattr(os.path, "isjunction"):
        return os.path.isjunction(p)
    try:
        import stat as _stat
        _st = os.lstat(p)
        _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        return bool(
            getattr(_st, "st_file_attributes", 0)
            & _FILE_ATTRIBUTE_REPARSE_POINT
        )
    except (OSError, AttributeError):
        return False


def _inline_check_workspace(p: Path, workspace_lock, tool_name=""):
    if _is_protected_path(p):
        from tools.errors import tool_error_response as _tool_error_response
        return _tool_error_response(
            "PATH_PROTECTED",
            f"'{p.name}' is a HiveMind internal file and cannot be "
            f"modified by the coder.",
            tool=tool_name,
            details={"path": str(p.resolve())},
        )
    # Junction check — prevent sandbox escape via Windows directory junctions
    _check_p = p
    while True:
        if _is_junction(_check_p):
            from tools.errors import tool_error_response as _tool_error_response
            return _tool_error_response(
                "PATH_JUNCTION_DENIED",
                f"Path '{p}' contains a directory junction at "
                f"'{_check_p}' and cannot be accessed for security reasons.",
                tool=tool_name,
                details={"path": str(p), "junction": str(_check_p)},
            )
        _parent = _check_p.parent
        if _parent == _check_p:  # filesystem root reached
            break
        _check_p = _parent
    if workspace_lock is None:
        return None
    try:
        p.resolve().relative_to(Path(workspace_lock).resolve())
        return None
    except ValueError:
        from tools.errors import tool_error_response as _tool_error_response
        return _tool_error_response(
            "PATH_OUTSIDE_WORKSPACE",
            f"Path '{p}' is outside workspace '{workspace_lock}'.",
            tool=tool_name,
            details={"path": str(p), "workspace": str(workspace_lock)},
        )


def normalize_tool_path(file_path: str, workspace: str | Path | None = None) -> str:
    """Normalize a file path for consistent read-tracking comparison.

    Produces: absolute, forward-slash, lowercase string.
    Used by runner.py read-guard and duo_runner.py pre-explore bridge.
    """
    if not file_path:
        return ""
    try:
        p = Path(file_path)
        if not p.is_absolute() and workspace:
            p = Path(workspace) / p
        p = p.resolve()
    except Exception:
        p = Path(file_path)
    return str(p).replace("\\", "/").lower()


def fuzzy_resolve_path(requested_path: str, workspace: str) -> str | None:
    p = Path(requested_path)
    if p.exists():
        return requested_path

    ws = Path(workspace)
    search_name = p.name
    search_dir = ws / p.parent if not p.is_absolute() else ws

    if not search_dir.exists():
        search_dir = ws

    try:
        all_files = [f.name for f in search_dir.iterdir() if f.is_file()]
        matches = get_close_matches(search_name, all_files, n=1, cutoff=0.6)
        if matches:
            corrected = search_dir / matches[0]
            return str(corrected)
    except OSError:
        pass

    return None
