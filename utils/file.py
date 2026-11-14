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

# UI-POISON / ENGINE-TAMPER (audit 2026-11-03): when the workspace falls back
# to the install dir, an agent write to the served UI or transcripts becomes a
# persistent same-origin XSS / engine compromise. These stay read-only for
# file tools no matter the workspace.
_PROTECTED_DIRS = frozenset({"static", "sessions", "llama", ".git", ".venv",
                             "deploy", "hivemind_gateway", "backend", "core",
                             "tools", "infra", "routers", "model_configs"})
_PROTECTED_ROOT_FILES = frozenset({
    "index.html", "gateway.toml", "gateway.toml.example", "build_info.txt",
    "update.bat", "install.bat", "start_hivemind.bat", "run.py", "server.py",
})
_PROTECTED_SUFFIXES = (".py", ".pyc", ".bat", ".sh", ".ps1", ".service")

_SECRET_PATH_RE = re.compile(
    r"(^|/)("
    r"\.env(\..+)?|settings\.json|gateway\.toml|"
    r"\.git-credentials|\.netrc|\.npmrc|\.pypirc|\.kube/config|"
    r".*\.kdbx|.*\.pem|.*\.key|id_rsa.*|id_ed25519.*|id_ecdsa.*|"
    r"credentials.*|secrets?\..*"
    r")$",
    re.IGNORECASE,
)


def is_secret_path(p: Path | str, workspace_lock: Path | str | None = None) -> bool:
    """Return True if *p* matches the secret/credential denylist (.env, keys, credentials, etc.)."""
    try:
        path_obj = Path(p)
        rel = path_obj.as_posix().replace("\\", "/")
        if workspace_lock:
            try:
                rel = path_obj.resolve().relative_to(Path(workspace_lock).resolve()).as_posix()
            except (OSError, ValueError):
                rel = path_obj.as_posix().replace("\\", "/")
    except (TypeError, ValueError):
        rel = str(p).replace("\\", "/")
    _rel_parts = [part.lower() for part in Path(rel).parts]
    if any(part in {"sessions", "chats"} for part in _rel_parts):
        return True
    if _SECRET_PATH_RE.search(rel):
        return True
    return bool(_SECRET_PATH_RE.search(Path(str(p)).name))


def filter_secret_search_rows(output: str) -> str:
    """Drop grep/rg/findstr result lines whose file path matches `is_secret_path`."""
    if not output:
        return output
    kept: list[str] = []
    for line in output.splitlines():
        m = re.match(r"^([A-Za-z]:[^\r\n:]+|[^\r\n:]+?):(?:\d+:|$)", line)
        if m and is_secret_path(m.group(1)):
            continue
        kept.append(line)
    return "\n".join(kept)


def _is_protected_path(p: Path) -> bool:
    """Check if a path targets a HiveMind internal file that must not be modified."""
    try:
        _resolved = p.resolve()
    except Exception:
        _resolved = p
    if _resolved in _PROTECTED_PATHS:
        return True
    # H-audit: Win32 strips trailing dots/spaces on open and compares
    # case-insensitively - normalize before matching or 'settings.json.'
    # overwrites the real settings.json.
    if _resolved.name.rstrip(". ").lower() in {_pp.name.rstrip(". ").lower() for _pp in _PROTECTED_PATHS}:
        return True
    if _resolved.name.rstrip(". ").lower().endswith(".context.json"):
        return True
    try:
        _rel = _resolved.relative_to(_HIVEMIND_ROOT)
        if _rel.parts and _rel.parts[0].lower() in _PROTECTED_DIRS:
            return True
        if len(_rel.parts) == 1 and _rel.parts[0].lower() in _PROTECTED_ROOT_FILES:
            return True
        if _resolved.suffix.lower() in _PROTECTED_SUFFIXES:
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
    _raw = str(raw or "")
    # MSYS-STYLE DRIVE SLASH (2026-09-30): small models emit "/C:/users/..."
    # (leading slash before a drive letter). Path() keeps that as a
    # filesystem-root path and the workspace containment check rejects it —
    # live, two complete 6-7k writes were lost to PATH_OUTSIDE_WORKSPACE this
    # way. Strip the leading slash so the drive path works; containment still
    # runs afterwards, so this opens no hole.
    if re.match(r'^/[A-Za-z]:[/\\]', _raw):
        _raw = _raw[1:]
    if re.match(r'^[A-Za-z]:[/\\]', _raw):
        return Path(_raw)
    p = Path(_raw)
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
    if is_secret_path(p, workspace_lock):
        from tools.errors import tool_error_response as _tool_error_response
        return _tool_error_response(
            "SECRET_PATH_DENIED",
            f"Access to protected secret/credential file '{p.name}' is denied.",
            tool=tool_name,
            details={"path": str(p)},
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
            f"Path '{p}' is outside workspace '{workspace_lock}'. "
            f"Write files with a RELATIVE path (e.g. 'index.html' or "
            f"'src/main.py') — they are created inside the workspace root.",
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
    _out = str(p).replace("\\", "/")
    # Linux audit 2026-11-03: POSIX is case-sensitive; App.py and app.py are
    # different files and must not share a read-guard/claim key.
    return _out.lower() if os.name == "nt" else _out


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
