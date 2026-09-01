


from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

from utils.file import write_json_atomic

logger = logging.getLogger("hivemind.workspace")

# Identische Patterns wie core/duo_helpers.py (_RE_WIN_PATH/_RE_UNIX_PATH) —
RE_WIN_PATH = re.compile(r'[A-Za-z]:\\[^\s\n\'"<>|?*]+')
RE_UNIX_PATH = re.compile(r'/(?:home|usr|var|opt|tmp|mnt)/[^\s\n\'"<>|?*]+')

_LAST_WS_FILE = Path(__file__).resolve().parent.parent / "context" / "last_workspace.json"


class WorkspaceForceInvalid(RuntimeError):

    def __init__(self, raw: str):
        self.raw = raw
        super().__init__(
            f"workspace_force_ui is active but the UI workspace is invalid: "
            f"'{raw}' does not exist. Fix the path in the UI or disable the toggle."
        )


def _valid_dir(p: str | None) -> bool:
    return bool(p) and Path(p).exists()


def load_last_workspace() -> str:
    try:
        if _LAST_WS_FILE.exists():
            data = json.loads(_LAST_WS_FILE.read_text(encoding="utf-8"))
            ws = str(data.get("workspace") or "")
            if ws and Path(ws).exists():
                return ws
    except Exception as _e:
        logger.debug("[WS-RESOLVE] last_workspace read failed: %s", _e)
    return ""


def save_last_workspace(ws: str) -> None:
    """Persists the last-used workspace atomically. Errors are logged, never thrown."""
    if not ws:
        return
    try:
        _LAST_WS_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(_LAST_WS_FILE, {"workspace": str(ws), "ts": time.time()})
    except Exception as _e:
        logger.warning("[WS-RESOLVE] last_workspace write failed: %s", _e)


def sync_env_workspace(ws: str) -> None:
    """Final genutzten Workspace ins Prozess-ENV spiegeln (Subprozess-Erbe)."""
    if not ws:
        return
    try:
        os.environ["HIVEMIND_WORKSPACE"] = str(ws)
    except Exception as _e:
        logger.debug("[WS-RESOLVE] env sync failed: %s", _e)


def extract_task_path(user_input: str) -> str:
    if not user_input:
        return ""
    m = RE_WIN_PATH.search(user_input) or RE_UNIX_PATH.search(user_input)
    if m:
        cand = m.group()
        try:
            if Path(cand).exists():
                return str(Path(cand).resolve())
        except OSError:
            pass
    return ""


def resolve_workspace(
    settings: dict,
    chat_ctx: dict | None,
    user_input: str = "",
    *,
    force: bool | None = None,
) -> tuple[str, str]:


    _mode = str((settings or {}).get("mode") or "").strip().lower()
    _force = bool(settings.get("workspace_force_ui")) if force is None else bool(force)
    if _mode == "simple":
        _force = False

    ui_ws = str((settings or {}).get("workspace") or "").strip()

    if _force:
        if ui_ws and _valid_dir(ui_ws):
            return str(Path(ui_ws).resolve()), "force_ui"
        raise WorkspaceForceInvalid(ui_ws)

    tp = extract_task_path(user_input)
    if tp:
        return tp, "task_path"

    cw = str(((chat_ctx or {}).get("workspace")) or "").strip()
    if cw and _valid_dir(cw):
        return str(Path(cw).resolve()), "chat_ctx"

    if ui_ws and _valid_dir(ui_ws):
        return str(Path(ui_ws).resolve()), "ui_setting"

    lw = load_last_workspace()
    if lw:
        return str(Path(lw).resolve()), "last_used"

    env_ws = str(os.environ.get("HIVEMIND_WORKSPACE") or "").strip()
    if env_ws and _valid_dir(env_ws):
        return str(Path(env_ws).resolve()), "env"

    return str(Path(".").resolve()), "cwd_fallback"
