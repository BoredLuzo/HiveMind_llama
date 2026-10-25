"""Git API-Router."""
import asyncio
import logging
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, Request

from settings import load_settings, save_settings
import core.state as _state
from core.state import (
    settings, _GIT_CONFIG_KEYS,
)

logger = logging.getLogger("hivemind.server")

router = APIRouter(prefix="/git", tags=["Git"])


def _get_git_config() -> dict:
    cfg = {}
    for k in _GIT_CONFIG_KEYS:
        cfg[k] = settings.get(k, "")
    if cfg.get("git_token"):
        cfg["git_token"] = "****"
    return cfg


def _ws_default() -> str:
    """Fallback workspace when no explicit override arrives (2026-10-16):
    the UI-configured project first (settings.workspace), the run-scoped
    env second - the same order as the workspace resolver and
    routers/core.py. The old env-first order could point the git panel at
    a stale/parallel run's directory while the UI showed another project."""
    ws_cfg = str(settings.get("workspace") or "").strip()
    if ws_cfg:
        return ws_cfg
    return str(os.environ.get("HIVEMIND_WORKSPACE", ".") or ".")


def _validate_git_repo(ws_override: str | None = None) -> tuple[bool, str, list[str]]:
    # WORKSPACE-PARAM (2026-09-22): the panel validates the PROJECT the user
    # selected, not the server env (HIVEMIND_WORKSPACE is run-scoped and
    # usually unset outside runs — the test button then checked the wrong
    # directory).
    if ws_override:
        ws = str(Path(ws_override).resolve())
    else:
        ws = str(Path(_ws_default()).resolve())
    if not ws:
        return False, "", []
    try:
        check = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
        if check.returncode != 0:
            return False, "", []
        branch_r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
        current_branch = branch_r.stdout.strip() if branch_r.returncode == 0 else ""
        branches_r = subprocess.run(
            ["git", "branch", "--list", "--no-color"],
            cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
        branches = []
        if branches_r.returncode == 0:
            for line in branches_r.stdout.strip().split("\n"):
                b = line.strip().lstrip("* ").strip()
                if b:
                    branches.append(b)
        return True, current_branch, branches
    except Exception:
        return False, "", []


async def _ws_from_body(request: Request) -> str | None:
    """Workspace override from a JSON body {workspace: ...} (POST routes)."""
    try:
        data = await request.json()
    except Exception:
        return None
    cand = str((data or {}).get("workspace") or "").strip()
    if cand and Path(cand).is_dir():
        return cand
    return None


def _ws_from_query(request: Request) -> str | None:
    """Workspace override from ?workspace=... (GET routes)."""
    cand = str(request.query_params.get("workspace") or "").strip()
    if cand and Path(cand).is_dir():
        return cand
    return None


def _resolve_ws(ws_override: str | None) -> str:
    if ws_override:
        return str(Path(ws_override).resolve())
    return str(Path(_ws_default()).resolve())


@router.get("/status")
async def git_status_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE:
        return {"valid": False, "reason": "git_tools.py not loaded", "config": {}}
    valid, branch, branches = await asyncio.to_thread(_validate_git_repo, _ws_from_query(request))
    cfg = _get_git_config()
    has_credentials = bool(settings.get("git_username") or settings.get("git_token"))
    return {
        "valid": valid and has_credentials,
        "repo_valid": valid,
        "branch": branch,
        "branches": branches,
        "config": cfg,
    }


@router.get("/config")
async def get_git_config_ep():
    if not _state._GIT_TOOLS_AVAILABLE:
        return {"valid": False, "config": {}}
    cfg = _get_git_config()
    valid, branch, branches = await asyncio.to_thread(_validate_git_repo)
    return {"valid": valid, "config": cfg, "branch": branch, "branches": branches}


@router.post("/config")
async def save_git_config_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE:
        return {"valid": False, "reason": "git_tools.py not loaded"}
    try:
        data = await request.json()
    except Exception:
        return {"valid": False, "reason": "Invalid JSON"}
    for k in _GIT_CONFIG_KEYS:
        if k in data:
            settings[k] = data[k]
    await asyncio.to_thread(save_settings, settings)
    valid, branch, branches = await asyncio.to_thread(_validate_git_repo)
    has_credentials = bool(settings.get("git_username") or settings.get("git_token"))
    fully_valid = valid and has_credentials
    return {
        "valid": fully_valid,
        "branch": branch,
        "branches": branches,
        "reason": "" if fully_valid else ("No git repo" if not valid else "Credentials missing"),
    }


def _gh_owner_name(repo_url: str):
    """Parse (owner, name) from a github.com https URL, else (None, None)."""
    import re as _re
    _m = _re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", str(repo_url or "").strip())
    if not _m:
        return None, None
    return _m.group(1), _m.group(2)


_GH_STATE_HINTS = {
    "ok": "remote repo reachable with this token",
    "missing_or_invisible": (
        "GitHub answers 404. Either the repo does not exist yet (Create repo "
        "below makes it) - or it exists but is PRIVATE and your fine-grained "
        "token does not list it (GitHub -> Settings -> Developer settings -> "
        "Fine-grained tokens -> Repository access: add this repo, then Test "
        "connection again)"),
    "token_invalid": "token rejected (401) - paste a fresh token below",
    "no_access": "token is valid but lacks access to this repo (403) - check its permissions",
    "unreachable": "github.com could not be reached - network or rate limit",
    "not_github": "the URL is not a github.com remote (local-only git works fine without one)",
}


async def _github_repo_state(repo_url: str, token: str) -> tuple[str, str]:
    """('ok'|'missing_or_invisible'|'token_invalid'|'no_access'|
    'unreachable'|'not_github', plain-language detail).

    GIT-TRANSPARENCY (2026-10-25, live): the old boolean flattened
    'repo missing' and 'token cannot see it' into one 404 message - the
    panel could not tell the user which of the two very different
    remedies applies. States map 1:1 to hints via _GH_STATE_HINTS."""
    import httpx as _hx
    _owner, _name = _gh_owner_name(repo_url)
    if not _owner:
        return "not_github", "remote is not a github.com URL"
    _headers = {"Accept": "application/vnd.github+json"}
    if token:
        _headers["Authorization"] = "Bearer " + token
    try:
        async with _hx.AsyncClient(timeout=15.0) as _c:
            _r = await _c.get("https://api.github.com/repos/" + _owner + "/" + _name,
                              headers=_headers)
    except Exception as _e:
        return "unreachable", "github unreachable: " + str(_e)[:120]
    if _r.status_code == 200:
        return "ok", "repo exists and the token can see it"
    if _r.status_code == 404:
        return "missing_or_invisible", _GH_STATE_HINTS["missing_or_invisible"]
    if _r.status_code == 401:
        return "token_invalid", _GH_STATE_HINTS["token_invalid"]
    if _r.status_code == 403:
        return "no_access", _GH_STATE_HINTS["no_access"]
    return "unreachable", "github returned HTTP " + str(_r.status_code)


async def _github_repo_exists(repo_url: str, token: str):
    """Backward-compatible bool view of _github_repo_state."""
    _state, _msg = await _github_repo_state(repo_url, token)
    return (_state == "ok"), _msg


async def _github_ensure_repo(remote_url: str, token: str) -> tuple[bool, str]:
    """Make the GitHub repo exist. Returns (created, msg).

    Extracted from git_init_ep so ALL paths can use it (GIT-TRANSPARENCY
    2026-10-25): an empty workspace used to clone-fail, an existing local
    repo used to silently skip - now both create the remote first."""
    _owner, _name = _gh_owner_name(remote_url)
    if not _owner:
        return False, ""
    if not token:
        return False, "no github token saved - Create repo cannot reach GitHub"
    _state, _msg = await _github_repo_state(remote_url, token)
    if _state == "ok":
        return False, "github repo already exists"
    if _state != "missing_or_invisible":
        return False, "github check failed: " + _msg
    import httpx as _hx
    _headers = {"Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + token}
    try:
        async with _hx.AsyncClient(timeout=25.0) as _c:
            _cr = await _c.post("https://api.github.com/user/repos",
                                headers=_headers,
                                json={"name": _name, "private": True,
                                      "auto_init": False})
            if _cr.status_code == 201:
                return True, "github repo created"
            _body = str((_cr.json() or {}).get("message", ""))[:140] if _cr.text else ""
            if _cr.status_code == 422 and "exist" in _body.lower():
                return False, ("repo already exists but this token cannot see it - "
                               "add it to the fine-grained token's repository list")
            return False, ("github repo NOT created (HTTP " + str(_cr.status_code)
                           + ": " + _body + ") - check token permissions")
    except Exception as _e:
        return False, "github create failed: " + str(_e)[:120]


@router.post("/validate")
async def validate_git_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE:
        return {"valid": False, "reason": "git_tools.py not loaded"}
    _ws = None
    try:
        _data = await request.json()
        _cand = str((_data or {}).get("workspace") or "").strip()
        if _cand and Path(_cand).is_dir():
            _ws = _cand
    except Exception:
        pass
    valid, branch, branches = await asyncio.to_thread(_validate_git_repo, _ws)
    has_credentials = bool(settings.get("git_username") or settings.get("git_token"))
    fully_valid = valid and has_credentials
    # GITHUB-PANEL FIX (2026-10-07): also answer what the panel button
    # actually promises - can this token SEE the configured remote repo?
    # GIT-TRANSPARENCY (2026-10-25): remote_state is machine-readable so
    # the UI can name the exact next step instead of one yellow blob.
    _remote_state, _remote_msg = "not_github", _GH_STATE_HINTS["not_github"]
    _repo_url = str(settings.get("git_repo_url", "") or "")
    if valid and _repo_url:
        _remote_state, _remote_msg = await _github_repo_state(
            _repo_url, str(settings.get("git_token", "") or ""))
    return {
        "valid": fully_valid,
        "branch": branch,
        "branches": branches,
        "remote_ok": _remote_state == "ok",
        "remote_msg": _remote_msg,
        "remote_state": _remote_state,
        "remote_hint": _GH_STATE_HINTS.get(_remote_state, _remote_msg),
        "reason": "" if fully_valid else ("No git repo" if not valid else "Credentials missing"),
    }


@router.get("/branches")
async def git_branches_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE:
        return {"branches": [], "current": "", "error": "git_tools.py not loaded"}
    # the panel calls this with ?workspace=<project> - honor it, or the
    # branch list shows the DEFAULT workspace's repo (live: pathtracer
    # panel listed Test5's branches)
    ws = _resolve_ws(_ws_from_query(request))
    valid, branch, branches = await asyncio.to_thread(_validate_git_repo, ws)
    return {"branches": branches, "current": branch, "valid": valid}


@router.post("/init")
async def git_init_ep(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    # WORKSPACE-PARAM (2026-09-22): init the user-selected project, not the env
    _ws_cand = str((data or {}).get("workspace") or "").strip()
    if _ws_cand and Path(_ws_cand).is_dir():
        ws = str(Path(_ws_cand).resolve())
    else:
        ws = str(Path(_ws_default()).resolve())
    if not ws:
        return {"ok": False, "error": "No workspace configured"}

    check = await asyncio.to_thread(
        subprocess.run, ["git", "rev-parse", "--git-dir"],
        cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
    )
    if check.returncode == 0:
        # EXISTING-REPO REMOTE HEAL (live 2026-10-25): a bare local init used
        # to short-circuit here - "Create repo" answered "already exists"
        # while the GitHub repo was never created and no remote was attached
        # (the one-click dead end). Heal it: ensure the remote exists, then
        # attach origin if missing.
        _gh_created, _gh_msg = False, ""
        _eh_url = (data.get("clone_url") or settings.get("git_repo_url", "") or "").strip()
        _eh_token = str(settings.get("git_token", "") or "")
        if _eh_url and _eh_token:
            _gh_created, _gh_msg = await _github_ensure_repo(_eh_url, _eh_token)
            _origin = await asyncio.to_thread(
                subprocess.run, ["git", "remote", "get-url", "origin"],
                cwd=ws, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=5)
            if _origin.returncode != 0:
                await asyncio.to_thread(
                    subprocess.run, ["git", "remote", "add", "origin", _eh_url],
                    cwd=ws, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=5)
        valid, branch, branches = await asyncio.to_thread(_validate_git_repo, ws)
        return {"ok": True, "already_existed": True, "branch": branch,
                "branches": branches,
                "github_created": _gh_created, "github_msg": _gh_msg}

    # INIT-IN-PLACE (2026-09-22): the old flow cloned the remote into the
    # workspace's PARENT directory (live: Desktop/showCase_... created while
    # FireWork itself stayed repo-less). Correct flow: init the workspace
    # itself, attach the remote; clone only fills an EMPTY workspace.
    username = data.get("username") or settings.get("git_username", "")
    email = data.get("email") or settings.get("git_email", "")
    remote_url = (data.get("clone_url") or settings.get("git_repo_url", "") or "").strip()
    default_branch = data.get("default_branch") or settings.get("git_default_branch", "main")

    # CLONE-PATH UNBOUND (live 2026-10-25): _gh_created/_gh_msg were only
    # initialized inside the init branch - a successful clone into an empty
    # workspace fell through to the return and crashed with UnboundLocalError.
    # Initialize before the branch; both paths reach the return.
    _gh_created, _gh_msg = False, ""
    _ws_files = [f for f in os.listdir(ws) if f != ".git"]
    if not _ws_files and remote_url:
        # CREATE-FIRST (live 2026-10-25): an empty workspace plus a remote
        # that does not exist yet used to fail the whole init in the clone
        # step (catch-22: the UI could never create the repo). Ensure the
        # remote exists first; a repo we just created is EMPTY - init
        # locally and attach instead of cloning.
        _token = str(settings.get("git_token", "") or "")
        if _token:
            _gh_created, _gh_msg = await _github_ensure_repo(remote_url, _token)
        if _gh_created:
            _init = await asyncio.to_thread(
                subprocess.run, ["git", "init", "-b", default_branch],
                cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
            )
            if _init.returncode != 0:
                return {"ok": False, "error": f"git init fehlgeschlagen: {_init.stderr.strip()[:200]}"}
        else:
            # empty workspace + remote -> clone INTO the workspace
            _clone = await asyncio.to_thread(
                subprocess.run, ["git", "clone", remote_url, "."],
                cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
            )
            if _clone.returncode != 0:
                _extra = f" | {_gh_msg}" if _gh_msg else ""
                return {"ok": False, "error": f"Clone fehlgeschlagen: {_clone.stderr.strip()[:200]}{_extra}"}
    else:
        _init = await asyncio.to_thread(
            subprocess.run, ["git", "init", "-b", default_branch],
            cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
        )
        if _init.returncode != 0:
            return {"ok": False, "error": f"git init fehlgeschlagen: {_init.stderr.strip()[:200]}"}
        # GITHUB CREATE (2026-10-07, owner; refactored 2026-10-25): the button
        # says "Create repo" - actually create it on GitHub when it does not
        # exist yet. Shared helper with the other init paths; private by
        # default (flip on GitHub if you want it public).
        if remote_url and str(settings.get("git_token", "") or ""):
            _gh_created, _gh_msg = await _github_ensure_repo(
                remote_url, str(settings.get("git_token", "") or ""))

    if username:
        await asyncio.to_thread(
            subprocess.run, ["git", "config", "user.name", username],
            cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
    if email:
        await asyncio.to_thread(
            subprocess.run, ["git", "config", "user.email", email],
            cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
    if remote_url:
        await asyncio.to_thread(
            subprocess.run, ["git", "remote", "remove", "origin"],
            cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
        await asyncio.to_thread(
            subprocess.run, ["git", "remote", "add", "origin", remote_url],
            cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )

    valid, branch, branches = await asyncio.to_thread(
        _validate_git_repo, ws)
    return {"ok": True, "initialized": True, "branch": branch,
            "branches": branches, "remote": remote_url,
            "github_created": _gh_created, "github_msg": _gh_msg}


@router.post("/reset")
async def git_reset_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE or _state.exec_git_reset is None:
        return {"ok": False, "error": "git_tools not loaded"}
    try:
        data = await request.json()
    except Exception:
        data = {}
    ws = _resolve_ws(await _ws_from_body(request))
    target = str(data.get("target", "HEAD"))[:72]
    hard = bool(data.get("hard", False))
    if hard:
        logger.warning("[GIT] Hard reset requested: target=%s", target)
    result = await _state.exec_git_reset(ws, target=target, hard=hard)
    return {"ok": "\u2705" in result, "message": result}


@router.post("/checkout")
async def git_checkout_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE or _state.exec_git_checkout is None:
        return {"ok": False, "error": "git_tools not loaded"}
    try:
        data = await request.json()
    except Exception:
        data = {}
    ws = _resolve_ws(await _ws_from_body(request))
    target = str(data.get("target", ""))[:120]
    if not target:
        return {"ok": False, "error": "target fehlt"}
    result = await _state.exec_git_checkout(ws, target=target)
    return {"ok": "\u2705" in result, "message": result}


@router.post("/stash")
async def git_stash_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE or _state.exec_git_stash is None:
        return {"ok": False, "error": "git_tools not loaded"}
    try:
        data = await request.json()
    except Exception:
        data = {}
    ws = _resolve_ws(await _ws_from_body(request))
    action = str(data.get("action", "push"))
    message = str(data.get("message", ""))[:72]
    result = await _state.exec_git_stash(ws, action=action, message=message)
    return {"ok": "\u2705" in result or action == "list", "message": result}


@router.get("/detail")
async def git_detail_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE or _state.exec_git_status_detailed is None:
        return {"valid": False, "error": "git_tools not loaded"}
    ws = _resolve_ws(_ws_from_query(request))
    return await _state.exec_git_status_detailed(ws)
