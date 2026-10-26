"""Git API-Router."""
import asyncio
import logging
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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


async def _ws_reject_reason(request: Request) -> str | None:
    """SILENT-FALLBACK GUARD (2026-10-25 audit): _ws_from_body/_ws_from_query
    DROP a workspace candidate that is not a directory on THIS machine, and
    the endpoints then resolve the DEFAULT workspace — a reset/checkout/stash
    meant for project X would land on the default repo with no error at all
    (documented real case: a UI keeps sending a path that does not exist on
    this machine). Every /git endpoint that accepts a workspace therefore
    rejects an explicit-but-dead path with 400 instead of guessing. Returns
    the offending path, or None when no candidate was supplied (empty means
    "use the default" — unchanged behavior) or the path exists."""
    cand = ""
    try:
        _data = await request.json()
        if isinstance(_data, dict):
            cand = str(_data.get("workspace") or "").strip()
    except Exception:
        cand = ""
    if not cand:
        cand = str(request.query_params.get("workspace") or "").strip()
    if cand and not Path(cand).is_dir():
        return cand
    return None


@router.get("/status")
async def git_status_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE:
        return {"valid": False, "reason": "git_tools.py not loaded", "config": {}}
    _bad_ws = await _ws_reject_reason(request)
    if _bad_ws:
        return JSONResponse(
            {"error": f"workspace does not exist on this machine: {_bad_ws}"},
            status_code=400)
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
async def get_git_config_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE:
        return {"valid": False, "config": {}}
    # WS-PARAM (2026-10-25 audit F4): the config GET could not address a
    # workspace and always reported the default repo's branch/validity -
    # /git/status honors ?workspace=, this endpoint now does too.
    _bad_ws = await _ws_reject_reason(request)
    if _bad_ws:
        return JSONResponse(
            {"error": f"workspace does not exist on this machine: {_bad_ws}"},
            status_code=400)
    cfg = _get_git_config()
    valid, branch, branches = await asyncio.to_thread(_validate_git_repo, _ws_from_query(request))
    return {"valid": valid, "config": cfg, "branch": branch, "branches": branches}


@router.post("/config")
async def save_git_config_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE:
        return {"valid": False, "reason": "git_tools.py not loaded"}
    _bad_ws = await _ws_reject_reason(request)
    if _bad_ws:
        return JSONResponse(
            {"valid": False, "error": f"workspace does not exist on this machine: {_bad_ws}"},
            status_code=400)
    try:
        data = await request.json()
    except Exception:
        return {"valid": False, "reason": "Invalid JSON"}
    for k in _GIT_CONFIG_KEYS:
        if k in data:
            settings[k] = data[k]
    await asyncio.to_thread(save_settings, settings)
    valid, branch, branches = await asyncio.to_thread(_validate_git_repo, _resolve_ws(await _ws_from_body(request)))
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


def _safe_git_url(url: str) -> bool:
    """URL-VETTING (2026-10-25 audit): clone_url/remote reach `git clone` and
    `git remote add`. Two shapes must never reach git: a leading "-" (git
    parses it as OPTIONS - `--upload-pack=<cmd>` runs a command) and the
    "::" ext transports (`ext::<shell command>` executes locally). Legit
    https://, git@, ssh:// and plain local paths stay allowed."""
    u = str(url or "").strip()
    if not u or u.startswith("-") or "::" in u:
        return False
    return True


def _run_git(ws: str, args: list, timeout: int = 5):
    """subprocess.run wrapper for git_init_ep (2026-10-25 audit): a missing
    git binary raised FileNotFoundError straight out of the endpoint (500),
    a hung git raised TimeoutExpired. Returns None on those and logs; callers
    turn it into {"ok": False, "error": ...}."""
    try:
        return subprocess.run(["git", *args], cwd=ws, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as _e:
        logger.warning("[GIT] git %s failed in %s: %s", args[0] if args else "?", ws, _e)
        return None


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
        # RATE-LIMIT (2026-10-25 audit): an unauthenticated check runs into
        # GitHub's 60 req/h per-IP budget; mapping that 403 onto "token
        # lacks access" sent users down the wrong remedy path.
        if _r.headers.get("x-ratelimit-remaining") == "0":
            return "unreachable", ("GitHub rate limit hit for this IP "
                                   "(60 unauthenticated requests/hour) - retry later or save a token")
        return "no_access", _GH_STATE_HINTS["no_access"]
    return "unreachable", "github returned HTTP " + str(_r.status_code)


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
            # 422-ALREADY-EXISTS (2026-10-25 audit): GitHub puts "name already
            # exists" into errors[], not the top-level message - check the
            # whole body or the precise hint never fires.
            _body = str(_cr.text or "")[:300]
            if _cr.status_code == 422 and "exist" in _body.lower():
                return False, ("repo already exists but this token cannot see it - "
                               "add it to the fine-grained token's repository list")
            _msg = str((_cr.json() or {}).get("message", ""))[:140] if _cr.text else ""
            return False, ("github repo NOT created (HTTP " + str(_cr.status_code)
                           + ": " + _msg + ") - check token permissions")
    except Exception as _e:
        return False, "github create failed: " + str(_e)[:120]


@router.post("/validate")
async def validate_git_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE:
        return {"valid": False, "reason": "git_tools.py not loaded"}
    _bad_ws = await _ws_reject_reason(request)
    if _bad_ws:
        return JSONResponse(
            {"error": f"workspace does not exist on this machine: {_bad_ws}"},
            status_code=400)
    _ws = await _ws_from_body(request)
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
    _bad_ws = await _ws_reject_reason(request)
    if _bad_ws:
        return JSONResponse(
            {"error": f"workspace does not exist on this machine: {_bad_ws}"},
            status_code=400)
    ws = _resolve_ws(_ws_from_query(request))
    valid, branch, branches = await asyncio.to_thread(_validate_git_repo, ws)
    return {"branches": branches, "current": branch, "valid": valid}


@router.post("/init")
async def git_init_ep(request: Request):
    _bad_ws = await _ws_reject_reason(request)
    if _bad_ws:
        return JSONResponse(
            {"ok": False, "error": f"workspace does not exist on this machine: {_bad_ws}"},
            status_code=400)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    # WORKSPACE-PARAM (2026-09-22): init the user-selected project, not the env
    _ws_cand = str((data or {}).get("workspace") or "").strip()
    if _ws_cand and Path(_ws_cand).is_dir():
        ws = str(Path(_ws_cand).resolve())
    else:
        # NO-WS GUARD (2026-10-25 audit): with neither a configured workspace
        # nor an env override, _ws_default() resolves to the HiveMind INSTALL
        # directory - `git init` there would put settings.json (plaintext
        # git_token) under version control. The old `if not ws:` guard could
        # never fire (Path(".").resolve() is always truthy).
        if not _ws_cand and not str(settings.get("workspace") or "").strip() \
                and not str(os.environ.get("HIVEMIND_WORKSPACE") or "").strip():
            return {"ok": False,
                    "error": "no workspace configured - pick a project folder first"}
        ws = str(Path(_ws_default()).resolve())
    if not ws:
        return {"ok": False, "error": "No workspace configured"}

    check = _run_git(ws, ["rev-parse", "--git-dir"])
    if check is None:
        return {"ok": False, "error": "git is not available on this machine (or timed out)"}
    if check.returncode == 0:
        # EXISTING-REPO REMOTE HEAL (live 2026-10-25): a bare local init used
        # to short-circuit here - "Create repo" answered "already exists"
        # while the GitHub repo was never created and no remote was attached
        # (the one-click dead end). Heal it: ensure the remote exists, then
        # attach origin if missing.
        _gh_created, _gh_msg = False, ""
        _eh_url = (data.get("clone_url") or settings.get("git_repo_url", "") or "").strip()
        _eh_token = str(settings.get("git_token", "") or "")
        if _eh_url and not _safe_git_url(_eh_url):
            return {"ok": False, "error": f"remote URL rejected: {_eh_url[:80]}"}
        if _eh_url and _eh_token:
            _gh_created, _gh_msg = await _github_ensure_repo(_eh_url, _eh_token)
        elif _eh_url and not _eh_token:
            # HEAL-TRANSPARENCY (2026-10-25 audit): URL but no token used to
            # skip silently and still answer "already existed".
            _gh_msg = "no token saved - the remote check/Create was skipped"
        _origin = _run_git(ws, ["remote", "get-url", "origin"])
        if _origin is not None and _origin.returncode != 0:
            _attached = _run_git(ws, ["remote", "add", "origin", _eh_url])
            if _attached is None or _attached.returncode != 0:
                _gh_msg = (_gh_msg + " | " if _gh_msg else "") + "remote add origin failed"
        valid, branch, branches = await asyncio.to_thread(_validate_git_repo, ws)
        return {"ok": True, "already_existed": True, "branch": branch,
                "branches": branches, "repo_valid": valid,
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
    _cloned = False
    _ws_files = [f for f in os.listdir(ws) if f != ".git"]
    if remote_url and not _safe_git_url(remote_url):
        return {"ok": False, "error": f"remote URL rejected: {remote_url[:80]}"}
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
            _init = _run_git(ws, ["init", "-b", default_branch], timeout=10)
            if _init is None or _init.returncode != 0:
                return {"ok": False,
                        "error": f"git init failed: {((_init and _init.stderr) or 'git failed').strip()[:200]}"}
        else:
            # empty workspace + remote -> clone INTO the workspace
            _clone = _run_git(ws, ["clone", remote_url, "."], timeout=120)
            if _clone is None or _clone.returncode != 0:
                # PARTIAL-CLONE CLEANUP (audit follow-up 2026-10-26): a failed
                # clone leaves a half-written .git behind; the next attempt
                # then hits the early "already_existed" branch and every
                # further action fails cryptically. Remove only what this
                # clone created (the workspace had no .git to begin with -
                # that is why this branch ran).
                _half_git = Path(ws) / ".git"
                if _half_git.exists():
                    import shutil as _shutil
                    try:
                        await asyncio.to_thread(_shutil.rmtree, _half_git, True)
                    except OSError:
                        pass
                _extra = f" | {_gh_msg}" if _gh_msg else ""
                return {"ok": False,
                        "error": f"Clone failed: {((_clone and _clone.stderr) or 'git failed').strip()[:200]}{_extra}"}
            _cloned = True
    else:
        _init = _run_git(ws, ["init", "-b", default_branch], timeout=10)
        if _init is None or _init.returncode != 0:
            return {"ok": False,
                    "error": f"git init failed: {((_init and _init.stderr) or 'git failed').strip()[:200]}"}
        # GITHUB CREATE (2026-10-07, owner; refactored 2026-10-25): the button
        # says "Create repo" - actually create it on GitHub when it does not
        # exist yet. Shared helper with the other init paths; private by
        # default (flip on GitHub if you want it public).
        if remote_url and str(settings.get("git_token", "") or ""):
            _gh_created, _gh_msg = await _github_ensure_repo(
                remote_url, str(settings.get("git_token", "") or ""))

    if username:
        _run_git(ws, ["config", "user.name", username])
    if email:
        _run_git(ws, ["config", "user.email", email])
    if remote_url:
        _run_git(ws, ["remote", "remove", "origin"])
        _attached = _run_git(ws, ["remote", "add", "origin", remote_url])
        if _attached is None or _attached.returncode != 0:
            _gh_msg = (_gh_msg + " | " if _gh_msg else "") + "remote add origin failed"

    valid, branch, branches = await asyncio.to_thread(
        _validate_git_repo, ws)
    return {"ok": True, "initialized": True, "cloned": _cloned,
            "branch": branch, "branches": branches, "remote": remote_url,
            "repo_valid": valid,
            "github_created": _gh_created, "github_msg": _gh_msg}


@router.post("/reset")
async def git_reset_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE or _state.exec_git_reset is None:
        return {"ok": False, "error": "git_tools not loaded"}
    _bad_ws = await _ws_reject_reason(request)
    if _bad_ws:
        return JSONResponse(
            {"ok": False, "error": f"workspace does not exist on this machine: {_bad_ws}"},
            status_code=400)
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
    _bad_ws = await _ws_reject_reason(request)
    if _bad_ws:
        return JSONResponse(
            {"ok": False, "error": f"workspace does not exist on this machine: {_bad_ws}"},
            status_code=400)
    try:
        data = await request.json()
    except Exception:
        data = {}
    ws = _resolve_ws(await _ws_from_body(request))
    target = str(data.get("target", ""))[:120]
    if not target:
        return {"ok": False, "error": "target missing"}
    result = await _state.exec_git_checkout(ws, target=target)
    return {"ok": "\u2705" in result, "message": result}


@router.post("/stash")
async def git_stash_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE or _state.exec_git_stash is None:
        return {"ok": False, "error": "git_tools not loaded"}
    _bad_ws = await _ws_reject_reason(request)
    if _bad_ws:
        return JSONResponse(
            {"ok": False, "error": f"workspace does not exist on this machine: {_bad_ws}"},
            status_code=400)
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
    _bad_ws = await _ws_reject_reason(request)
    if _bad_ws:
        return JSONResponse(
            {"error": f"workspace does not exist on this machine: {_bad_ws}"},
            status_code=400)
    ws = _resolve_ws(_ws_from_query(request))
    return await _state.exec_git_status_detailed(ws)
