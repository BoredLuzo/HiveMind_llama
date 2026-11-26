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


def _strip_win_extended_prefix(path_str: str) -> str:
    """Strip Windows \\\\?\\ / \\\\.\\ device prefixes to normalize path checks."""
    raw = str(path_str or "")
    for pfx in ("\\\\?\\UNC\\", "//?/UNC/", "\\\\.\\UNC\\", "//./UNC/"):
        if raw.upper().startswith(pfx):
            return "\\\\" + raw[len(pfx):]
    for pfx in ("\\\\?\\", "//?/", "\\\\.\\", "//./"):
        if raw.startswith(pfx):
            return raw[len(pfx):]
    return raw


# HiveMind source root — two levels up from utils/file.py
_HIVEMIND_ROOT = Path(_strip_win_extended_prefix(str(Path(__file__).parent.parent.resolve()))).resolve()

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
_PROTECTED_DIRS = frozenset({
    "static", "sessions", "llama", ".git", ".venv", "venv",
    "deploy", "hivemind_gateway", "backend", "core", "tools", "infra",
    "routers", "model_configs", "stt", "voice_models", "utils", "context",
    "routing", "hive_functions", "learning", "scheduler", "sse", "explore",
    "vram", "scripts", "searxng-config", ".github", "logs",
})
_PROTECTED_ROOT_FILES = frozenset({
    "index.html", "gateway.toml", "gateway.toml.example", "build_info.txt",
    "update.bat", "install.bat", "start_hivemind.bat", "run.py", "server.py",
    "memory.json", "automations.json", "presets.json", "runtime_models.json",
    "vision_model.json", "pyproject.toml", "uv.lock", "requirements.txt",
    "agents.md", "token_stats.json", "run_counter.json",
})
_PROTECTED_SUFFIXES = (
    ".py", ".pyc", ".bat", ".sh", ".ps1", ".service",
    ".exe", ".dll", ".pyd", ".pth", ".so", ".dylib", ".cmd", ".vbs",
)

_SECRET_PATH_RE = re.compile(
    r"(^|/)("
    r"\.env(\..+)?|settings\.json|gateway\.toml|"
    r"\.git-credentials|\.netrc|\.npmrc|\.pypirc|\.kube/config|"
    r".*\.kdbx|.*\.pem|.*\.key|id_rsa.*|id_ed25519.*|id_ecdsa.*|"
    r"credentials.*|secrets?\..*"
    r")$",
    re.IGNORECASE,
)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SEARCH_ROW_RE = re.compile(r"^(.+?):(?:\d+:|$)")
_WIN_DRIVE_PREFIX_RE = re.compile(r"^/[A-Za-z]:[/\\]")


def _normalize_secret_part(part: str) -> str:
    """Strip NTFS alternate data stream suffixes and trailing dots/spaces."""
    s = str(part)
    # Keep Windows drive designator ("C:") intact while stripping ":stream" ADS.
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        head, tail = s[:2], s[2:]
        if ":" in tail:
            tail = tail.split(":", 1)[0]
        s = head + tail
    elif ":" in s:
        s = s.split(":", 1)[0]
    return s.rstrip(". ")


def _matches_secret_parts(path_str: str) -> bool:
    raw_posix = str(path_str or "").replace("\\", "/")
    norm_parts = [_normalize_secret_part(part) for part in Path(path_str).parts]
    rel_parts = [part.lower() for part in norm_parts if part]
    if any(part in {"sessions", "chats"} for part in rel_parts):
        return True
    norm_rel = "/".join(seg.strip("\\/") for seg in norm_parts if seg and seg not in ("/", "\\"))
    if _SECRET_PATH_RE.search(raw_posix) or _SECRET_PATH_RE.search(norm_rel):
        return True
    raw_name = Path(path_str).name
    norm_name = _normalize_secret_part(raw_name)
    if len(norm_name) >= 2 and norm_name[1] == ":" and norm_name[0].isalpha():
        norm_name = norm_name[2:]
    return bool(_SECRET_PATH_RE.search(raw_name) or _SECRET_PATH_RE.search(norm_name))


def is_secret_path(p: Path | str, workspace_lock: Path | str | None = None) -> bool:
    """Return True if *p* matches the secret/credential denylist (.env, keys, credentials, etc.)."""
    raw = _strip_win_extended_prefix(str(p))
    try:
        path_obj = Path(raw)
        if workspace_lock:
            ws_clean = _strip_win_extended_prefix(str(workspace_lock))
            ws_res = Path(ws_clean).resolve()
            ws_res_norm = Path(_strip_win_extended_prefix(str(ws_res)))
            cand = path_obj if path_obj.is_absolute() else (ws_res_norm / path_obj)
            # Check lexical path inside workspace if contained (ignores ws parent dirs).
            try:
                lex_abs = Path(os.path.abspath(_strip_win_extended_prefix(str(cand))))
                lex_rel = lex_abs.relative_to(ws_res_norm).as_posix()
                if _matches_secret_parts(lex_rel):
                    return True
            except (OSError, ValueError):
                pass
            # Resolve symlinks/junctions and re-check against workspace or full target.
            try:
                resolved = Path(_strip_win_extended_prefix(str(cand.resolve())))
                try:
                    res_rel = resolved.relative_to(ws_res_norm).as_posix()
                    return _matches_secret_parts(res_rel)
                except ValueError:
                    return _matches_secret_parts(resolved.as_posix()) or _matches_secret_parts(raw.replace("\\", "/"))
            except (OSError, RuntimeError, ValueError):
                return _matches_secret_parts(raw.replace("\\", "/"))
        # No workspace_lock: check provided path parts and resolved filename/symlink target.
        if _matches_secret_parts(path_obj.as_posix().replace("\\", "/")):
            return True
        try:
            resolved = Path(_strip_win_extended_prefix(str(path_obj.resolve())))
            if path_obj.is_symlink():
                return _matches_secret_parts(resolved.as_posix())
            if _SECRET_PATH_RE.search(_normalize_secret_part(resolved.name)):
                return True
        except (OSError, RuntimeError, ValueError):
            pass
        return False
    except (OSError, RuntimeError, TypeError, ValueError):
        return _matches_secret_parts(raw.replace("\\", "/"))


def filter_secret_search_rows(output: str, workspace: str | Path | None = None) -> str:
    """Drop grep/rg/findstr result lines whose file path matches `is_secret_path`."""
    if not output:
        return output
    kept: list[str] = []
    for line in output.splitlines():
        clean = _ANSI_ESCAPE_RE.sub("", line)
        m = _SEARCH_ROW_RE.match(clean)
        if m and is_secret_path(m.group(1), workspace):
            continue
        kept.append(line)
    return "\n".join(kept)


def _is_protected_path(p: Path) -> bool:
    """Check if a path targets a HiveMind internal file that must not be modified."""
    raw_p = Path(_strip_win_extended_prefix(str(p)))
    try:
        _resolved = Path(_strip_win_extended_prefix(str(raw_p.resolve())))
    except (OSError, RuntimeError, ValueError):
        _resolved = raw_p
    if _resolved in _PROTECTED_PATHS:
        return True
    # H-audit: Win32 strips trailing dots/spaces on open and compares
    # case-insensitively - normalize before matching or 'settings.json.'
    # overwrites the real settings.json. Also strip NTFS ':stream' ADS.
    _norm_leaf = _normalize_secret_part(_resolved.name).lower()
    if _norm_leaf in {_pp.name.rstrip(". ").lower() for _pp in _PROTECTED_PATHS}:
        return True
    if _norm_leaf.endswith(".context.json"):
        return True
    try:
        _rel = _resolved.relative_to(_HIVEMIND_ROOT)
        _rel_norm = [_normalize_secret_part(part).lower() for part in _rel.parts]
        if _rel_norm and _rel_norm[0] in _PROTECTED_DIRS:
            return True
        if len(_rel_norm) == 1:
            _root_name = _rel_norm[0]
            if _root_name in _PROTECTED_ROOT_FILES or (
                _root_name.startswith("requirements") and _root_name.endswith(".txt")
            ):
                return True
        _in_default_ws = False
        if _rel_norm and _rel_norm[0] == "default_workspace":
            _default_ws = (_HIVEMIND_ROOT / "default_workspace").resolve()
            try:
                _dw_rel = _resolved.relative_to(_default_ws)
                _in_default_ws = bool(_dw_rel.parts)
            except ValueError:
                _in_default_ws = False
        if not _in_default_ws and any(_norm_leaf.endswith(sfx) for sfx in _PROTECTED_SUFFIXES):
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
    _raw_str = _strip_win_extended_prefix(str(p))
    if str(p).startswith(("\\\\?\\", "//?/", "\\\\.\\", "//./")):
        from tools.errors import tool_error_response as _tool_error_response
        return _tool_error_response(
            "PATH_OUTSIDE_WORKSPACE",
            f"Refusing device/extended path '{p}'.",
            tool=tool_name,
            details={"path": str(p)},
        )
    for _idx, _part in enumerate(Path(_raw_str).parts):
        if _idx == 0 and (len(_part) >= 2 and _part[1] == ":" and _part[0].isalpha() or _part in ("\\", "/")):
            continue
        if ":" in _part or (_part not in (".", "..") and _part != _part.rstrip(". ")):
            from tools.errors import tool_error_response as _tool_error_response
            return _tool_error_response(
                "PATH_OUTSIDE_WORKSPACE",
                f"Refusing ambiguous path component '{_part}' in '{p}'.",
                tool=tool_name,
                details={"path": str(p), "component": _part},
            )
    if _is_protected_path(p):
        from tools.errors import tool_error_response as _tool_error_response
        try:
            _prot_path = str(p.resolve())
        except (OSError, RuntimeError):
            _prot_path = str(p)
        return _tool_error_response(
            "PATH_PROTECTED",
            f"'{p.name}' is a HiveMind internal file and cannot be "
            f"modified by the coder.",
            tool=tool_name,
            details={"path": _prot_path},
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
    except (ValueError, OSError, RuntimeError):
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
    file_path = str(file_path)
    if os.name == "nt" and _WIN_DRIVE_PREFIX_RE.match(file_path):
        file_path = file_path[1:]
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
    req_clean = _strip_win_extended_prefix(str(requested_path or ""))
    ws_clean = _strip_win_extended_prefix(str(workspace or ""))
    if not req_clean or not ws_clean:
        return None
    ws = Path(ws_clean).resolve()
    p = Path(req_clean)
    cand = p if p.is_absolute() else (ws / p)
    try:
        cand_res = Path(_strip_win_extended_prefix(str(cand.resolve())))
        cand_res.relative_to(ws)
    except (OSError, ValueError):
        return None

    if cand.exists():
        if _is_protected_path(cand_res) or is_secret_path(cand_res, ws):
            return None
        return requested_path if not p.is_absolute() else str(cand_res)

    search_name = p.name
    search_dir = (ws / p.parent) if not p.is_absolute() else ws
    try:
        search_dir_res = Path(_strip_win_extended_prefix(str(search_dir.resolve())))
        search_dir_res.relative_to(ws)
        if not search_dir_res.is_dir():
            search_dir_res = ws
    except (OSError, ValueError):
        search_dir_res = ws

    try:
        all_files = [
            f.name for f in search_dir_res.iterdir()
            if f.is_file() and not is_secret_path(f, ws) and not _is_protected_path(f)
        ]
        matches = get_close_matches(search_name, all_files, n=1, cutoff=0.6)
        if matches:
            corrected = (search_dir_res / matches[0]).resolve()
            corrected.relative_to(ws)
            if not is_secret_path(corrected, ws) and not _is_protected_path(corrected):
                return str(corrected)
    except (OSError, ValueError):
        pass

    return None
