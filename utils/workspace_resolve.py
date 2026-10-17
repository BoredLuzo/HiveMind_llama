


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
    return bool(p) and Path(p).is_dir()  # H-audit: a FILE is not a workspace


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
            _cand_p = Path(cand)
            # H-audit: 'C:\\.' resolves to the DRIVE ROOT and a file path
            # is not a workspace - both refuse.
            if _cand_p.exists() and _cand_p.is_dir() and _cand_p.resolve() != Path(_cand_p.anchor):  # H-audit: Path-vs-str always !=
                return str(_cand_p.resolve())
        except OSError:
            pass
    return ""


def resolve_workspace(
    settings: dict,
    chat_ctx: dict | None,
    user_input: str = "",
    *,
    force: bool | None = None,
    request_workspace: str = "",
) -> tuple[str, str]:

    # REQUEST-WINS (2026-10-14, security fix): an explicit /stream workspace
    # parameter beats every other source. Before this, the body parameter was
    # ignored entirely and runs silently landed in "last used" (the
    # gravSandbox contamination: a stray API run committed 1186 files there).
    _req = str(request_workspace or "").strip()
    # UI-DEFAULT-IS-NOT-A-REQUEST (2026-10-17, ThinkPad regression on 1.3.9:
    # "the model forgets the workspace, before that it worked"): the web UI sends
    # the *global* Workspace field with EVERY run, so treating it as an explicit
    # request made that field override a chat's own workspace and bypass the
    # workspace_force_ui check - and a stale value (a folder that only exists on
    # another machine) left the run with no workspace at all. A value identical to
    # the UI setting carries no new information: fall through to the normal chain
    # (task path > chat workspace > UI setting > last used). Only a value that
    # DIFFERS from the setting is an explicit request (API callers, "run this in
    # <dir>"), and only that keeps the strict behaviour below.
    _ui_raw = str((settings or {}).get("workspace") or "").strip()
    _explicit_req = bool(_req) and _req != _ui_raw
    if _explicit_req and _valid_dir(_req):
        return str(Path(_req).resolve()), "request"
    if _explicit_req:
        logger.warning(
            "[WS-RESOLVE] request workspace '%s' does not exist or is not a "
            "directory - ignoring it (run will NOT auto-commit anywhere)",
            _req[:120],
        )
        return "", "request_invalid"
    if _req:
        logger.info(
            "[WS-RESOLVE] /stream workspace equals the UI setting - treated as "
            "the UI default, the normal chain decides (%s)",
            _req[:120],
        )

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


def bootstrap_workspace(
    request_workspace: str,
    chat_run_workspace: str,
    resume_workspace: str,
    fallback_fn=None,
) -> tuple[str, str]:
    """WS-PROVENANCE (2026-10-15, gravSandbox incident): priority for the
    pre-explore/agentic bootstrap and every run-internal consumer (tree scout,
    repo map, planner context). The incident: _pre_explore.py:179 let a stale
    resume block or the process-global ENV override the workspace chat_run had
    resolved from the request (RUN-TRACE calc_app, tree scout gravSandbox).

    Priority: request > resume block (only reaches this function on a real
    resume of the same chat_id - _try_resume loads exactly that chat's block)
    > the chat_run resolution. ENV is NEVER a primary source here; when
    nothing is known, the caller's full resolver chain decides (env is its
    documented second-to-last resort, above cwd only)."""
    req = str(request_workspace or "").strip()
    if req:
        cw = str(chat_run_workspace or "").strip()
        if cw:
            return str(Path(cw).resolve()), "request"
        # Request present but no chat_run resolution in scope (direct/test
        # call): re-resolve, never fall through to the stale resume block.
        if fallback_fn is not None:
            return fallback_fn()
        return str(Path(".").resolve()), "cwd_fallback"
    rw = str(resume_workspace or "").strip()
    if rw:
        return str(Path(rw).resolve()), "resume"
    cw = str(chat_run_workspace or "").strip()
    if cw:
        return str(Path(cw).resolve()), "chat_run"
    if fallback_fn is not None:
        return fallback_fn()
    return str(Path(".").resolve()), "cwd_fallback"


def effective_run_workspace(ctx_workspace: str = "", state_workspace: str = "") -> str:
    """WS-PROVENANCE (2026-10-15): authoritative workspace for run-internal
    side effects (auto-test suites, chunk checks). The chat_run-resolved value
    wins; ENV is only a last resort because a parallel run re-points the
    process-global (sync_env_workspace) at any time."""
    ws = str(ctx_workspace or "").strip() or str(state_workspace or "").strip()
    if ws:
        return str(Path(ws).resolve())
    return str(Path(os.environ.get("HIVEMIND_WORKSPACE") or ".").resolve())


def commit_allowed_for(request_workspace: str, chat_ctx: dict | None) -> bool:
    """COMMITS (2026-10-14, security fix): an API run may only auto-commit
    when it EXPLICITLY addressed a workspace (valid request parameter) or
    continues an existing chat session (UI flow persists the workspace in
    the chat context). A run with neither - the gravSandbox incident shape -
    may read the fallback workspace but must never commit anywhere."""
    _req = str(request_workspace or "").strip()
    if _req and _valid_dir(_req):
        return True
    cw = str(((chat_ctx or {}).get("workspace")) or "").strip()
    return bool(cw and _valid_dir(cw))

def fallback_for_invalid_request(settings, chat_ctx, user_input: str = "") -> tuple[str, str]:
    """Interactive fallback when the /stream workspace parameter is invalid.

    REQUEST-INVALID-FALLBACK (2026-10-17, live ThinkPad regression): 1.3.9 lets
    the request parameter beat every other source (gravSandbox fix). When the UI
    keeps sending a path that does not exist on this machine - another box's
    folder, a moved project - the run used to lose its workspace completely
    (no root for write_file, the model looked like it forgot the project), while
    1.3.8 ignored the parameter and therefore worked. This re-runs the normal
    chain WITHOUT the request so an interactive session keeps a usable workspace.

    The caller must keep auto-commit off (see chat_run): the safety property of
    the gravSandbox fix is about committing, not about reading/writing.
    Returns ("", "") when nothing usable is left.
    """
    try:
        ws, src = resolve_workspace(settings, chat_ctx, user_input)
    except WorkspaceForceInvalid:
        return "", ""
    if not ws or src == "request_invalid":
        return "", ""
    return ws, src
