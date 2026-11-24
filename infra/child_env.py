# -*- coding: utf-8 -*-
"""Environment for child processes that run model-chosen commands.

TOOL-ENV (audit 2026-11-03): run_bash/run_python/background/MCP children used
to inherit the FULL engine environment, including HIVEMIND_INTERNAL_TOKEN and
Telegram/API credentials. One `env` call in a tainted run could print them
into the model context, the logs and the phone mirror. LSP children already
had a scrubbed environment; this is the shared helper for the rest.

The scrub is a denylist of well-known secret names plus secret-shaped
suffixes. PATH, HOME, locale and toolchain variables survive.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

_SECRET_EXACT = frozenset({
    "HIVEMIND_INTERNAL_TOKEN",
    "HIVEMIND_TG_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITLAB_TOKEN",
    "GIT_TOKEN",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "HUGGINGFACEHUB_API_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "MCP_HTTP_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "SECRET_KEY",
    "API_KEY",
    "PRIVATE_KEY",
    "ACCESS_KEY",
    "AUTH_TOKEN",
    "REFRESH_TOKEN",
    "CLIENT_SECRET",
    "ENCRYPTION_KEY",
    "SIGNING_KEY",
    "JWT_SECRET",
})
_SECRET_SUFFIXES = (
    "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD", "_API_KEY",
    "_ACCESS_KEY", "_PRIVATE_KEY", "_CLIENT_SECRET",
    "_SECRET_KEY", "_SIGNING_KEY", "_ENCRYPTION_KEY", "_MASTER_KEY",
    "_AUTH_KEY", "_PASSPHRASE", "_CREDENTIAL", "_CREDENTIALS",
)


def _is_secret_name(name: str) -> bool:
    upper = str(name or "").upper()
    if upper in _SECRET_EXACT:
        return True
    # All HIVEMIND_* vars are engine-internal (tokens, paths, test flags);
    # they are never needed by a model-chosen shell command.
    if upper.startswith("HIVEMIND_"):
        return True
    return any(upper.endswith(sfx) for sfx in _SECRET_SUFFIXES)


def scrubbed_env(extra: dict | None = None) -> dict:
    """os.environ minus engine/secret variables, plus explicit extras."""
    out = {k: v for k, v in os.environ.items() if not _is_secret_name(k)}
    if extra:
        out.update({str(k): str(v) for k, v in extra.items()})
    return out


def _is_windows_store_python(path: Path) -> bool:
    if sys.platform != "win32":
        return False
    return "microsoft\\windowsapps" in str(path).lower().replace("/", "\\")


def workspace_venv_python(workspace: str | Path | None) -> Path | None:
    """Return the Python executable inside workspace/.venv or workspace/venv, if present."""
    if not workspace:
        return None
    ws = Path(workspace)
    rels = (
        (Path(".venv") / "Scripts" / "python.exe", Path("venv") / "Scripts" / "python.exe")
        if sys.platform == "win32"
        else (Path(".venv") / "bin" / "python", Path("venv") / "bin" / "python")
    )
    for rel in rels:
        cand = ws / rel
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _find_system_python(path_env: str, *, exclude_dir: Path | None = None) -> Path | None:
    """Locate a real system Python on PATH, skipping WindowsApps stubs and engine venv."""
    ex_norm = ""
    if exclude_dir is not None:
        try:
            ex_norm = str(exclude_dir.resolve()).lower()
        except OSError:
            ex_norm = str(exclude_dir).lower()
    names = ("python.exe", "python3.exe") if sys.platform == "win32" else ("python3", "python")
    for raw_dir in (path_env or "").split(os.pathsep):
        d_str = raw_dir.strip().strip('"')
        if not d_str:
            continue
        d = Path(d_str)
        try:
            d_norm = str(d.resolve()).lower()
        except OSError:
            d_norm = d_str.lower()
        if ex_norm and d_norm == ex_norm:
            continue
        for name in names:
            cand = d / name
            try:
                if cand.is_file() and not _is_windows_store_python(cand):
                    return cand
            except OSError:
                continue
    return None


def _engine_venv_dir() -> Path | None:
    """Return engine interpreter's bin/Scripts dir only when running inside a venv."""
    if not sys.executable:
        return None
    try:
        py = Path(sys.executable)
        if not py.is_file():
            return None
        in_venv = (
            getattr(sys, "base_prefix", sys.prefix) != sys.prefix
            or (py.parent.parent / "pyvenv.cfg").is_file()
            or "venv" in py.parent.parent.name.lower()
        )
        return py.parent if in_venv else None
    except (OSError, TypeError, ValueError):
        return None


def resolve_workspace_python(
    workspace: str | Path | None = None,
    *,
    exclude_engine: bool = False,
) -> Path | None:
    """3-tier Python lookup: workspace .venv -> real system Python -> engine venv fallback."""
    ws_py = workspace_venv_python(workspace)
    if ws_py is not None:
        return ws_py
    eng_dir = _engine_venv_dir()
    sys_py = _find_system_python(os.environ.get("PATH", ""), exclude_dir=eng_dir)
    if sys_py is not None:
        return sys_py
    if not exclude_engine and sys.executable:
        eng_py = Path(sys.executable)
        try:
            if eng_py.is_file():
                return eng_py
        except OSError:
            pass
    return None


def tool_env(extra: dict | None = None, *, workspace: str | Path | None = None) -> dict:
    """Scrubbed env for model-chosen tools with safe Python PATH resolution.

    Priority:
      1. Workspace .venv / venv (prepended + VIRTUAL_ENV set)
      2. Real system Python on PATH (engine venv appended as fallback only so
         HiveMind's own server venv does not shadow or leak into user projects)
      3. Engine venv prepended ONLY when no real system Python exists (e.g.
         uv-only Windows installs with only the WindowsApps stub).
    """
    out = scrubbed_env(extra)
    cur_path = out.get("PATH", "")
    eng_dir: Path | None = None
    try:
        py = Path(sys.executable)
        if py.is_file():
            eng_dir = py.parent
    except (OSError, TypeError, ValueError):
        eng_dir = None

    # Never leak HiveMind's own engine VIRTUAL_ENV into child workspaces
    if eng_dir is not None and out.get("VIRTUAL_ENV"):
        try:
            if Path(out["VIRTUAL_ENV"]).resolve() == eng_dir.parent.resolve():
                out.pop("VIRTUAL_ENV", None)
        except OSError:
            pass

    ws_py = workspace_venv_python(workspace)
    has_sys_py = _find_system_python(cur_path, exclude_dir=eng_dir) is not None

    parts: list[str] = []
    if ws_py is not None:
        parts.append(str(ws_py.parent))
        out["VIRTUAL_ENV"] = str(ws_py.parent.parent)
    if eng_dir is not None and not has_sys_py:
        parts.append(str(eng_dir))
    if cur_path:
        parts.append(cur_path)
    if eng_dir is not None and has_sys_py:
        parts.append(str(eng_dir))
    if parts:
        out["PATH"] = os.pathsep.join(parts)

    out["PYTHONIOENCODING"] = "utf-8"
    out["PYTHONUTF8"] = "1"
    return out
