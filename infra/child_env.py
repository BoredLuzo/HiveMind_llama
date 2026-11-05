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
})
_SECRET_SUFFIXES = (
    "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD", "_API_KEY",
    "_ACCESS_KEY", "_PRIVATE_KEY", "_CLIENT_SECRET",
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


def tool_env(extra: dict | None = None) -> dict:
    """Scrubbed env for model-chosen tools, with the engine Python on PATH.

    uv-managed installs often have no `python` on the system PATH (only
    python3.14.exe). Prepending the running interpreter's directory makes
    `python script.py` / `python -m pytest` work without touching the user's
    PATH.
    """
    out = scrubbed_env(extra)
    try:
        import sys
        from pathlib import Path
        py = Path(sys.executable)
        if py.is_file():
            out["PATH"] = str(py.parent) + os.pathsep + out.get("PATH", "")
    except (OSError, TypeError, ValueError):
        pass
    out["PYTHONIOENCODING"] = "utf-8"
    out["PYTHONUTF8"] = "1"
    return out
