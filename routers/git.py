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


def _validate_git_repo() -> tuple[bool, str, list[str]]:
    ws = str(Path(os.environ.get("HIVEMIND_WORKSPACE", ".")).resolve())
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


@router.get("/status")
async def git_status_ep():
    if not _state._GIT_TOOLS_AVAILABLE:
        return {"valid": False, "reason": "git_tools.py not loaded", "config": {}}
    valid, branch, branches = await asyncio.to_thread(_validate_git_repo)
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


@router.post("/validate")
async def validate_git_ep():
    if not _state._GIT_TOOLS_AVAILABLE:
        return {"valid": False, "reason": "git_tools.py not loaded"}
    valid, branch, branches = await asyncio.to_thread(_validate_git_repo)
    has_credentials = bool(settings.get("git_username") or settings.get("git_token"))
    fully_valid = valid and has_credentials
    return {
        "valid": fully_valid,
        "branch": branch,
        "branches": branches,
        "reason": "" if fully_valid else ("No git repo" if not valid else "Credentials missing"),
    }


@router.get("/branches")
async def git_branches_ep():
    if not _state._GIT_TOOLS_AVAILABLE:
        return {"branches": [], "current": "", "error": "git_tools.py not loaded"}
    valid, branch, branches = await asyncio.to_thread(_validate_git_repo)
    return {"branches": branches, "current": branch, "valid": valid}


@router.post("/init")
async def git_init_ep(request: Request):
    ws = str(Path(os.environ.get("HIVEMIND_WORKSPACE", ".")).resolve())
    if not ws:
        return {"ok": False, "error": "No workspace configured"}
    try:
        data = await request.json()
    except Exception:
        data = {}

    check = await asyncio.to_thread(
        subprocess.run, ["git", "rev-parse", "--git-dir"],
        cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
    )
    if check.returncode == 0:
        valid, branch, branches = await asyncio.to_thread(_validate_git_repo)
        return {"ok": True, "already_existed": True, "branch": branch, "branches": branches}

    clone_url = data.get("clone_url") or settings.get("git_repo_url", "")
    if clone_url and clone_url.strip():
        clone_url = clone_url.strip()
        parent = str(Path(ws).parent)
        repo_name = clone_url.rstrip("/").split("/")[-1].replace(".git", "")
        clone_result = await asyncio.to_thread(
            subprocess.run, ["git", "clone", clone_url, repo_name],
            cwd=parent, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
        )
        if clone_result.returncode == 0:
            valid, branch, branches = await asyncio.to_thread(_validate_git_repo)
            return {"ok": True, "cloned": True, "repo": repo_name, "branch": branch, "branches": branches}
        return {"ok": False, "error": f"Clone fehlgeschlagen: {clone_result.stderr.strip()[:200]}"}

    username = data.get("username") or settings.get("git_username", "")
    email = data.get("email") or settings.get("git_email", "")
    init_result = await asyncio.to_thread(
        subprocess.run, ["git", "init"],
        cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
    )
    if init_result.returncode != 0:
        return {"ok": False, "error": f"git init fehlgeschlagen: {init_result.stderr.strip()[:200]}"}

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

    default_branch = data.get("default_branch") or settings.get("git_default_branch", "main")
    await asyncio.to_thread(
        subprocess.run, ["git", "checkout", "-b", default_branch],
        cwd=ws, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
    )

    valid, branch, branches = await asyncio.to_thread(_validate_git_repo)
    return {"ok": True, "initialized": True, "branch": branch, "branches": branches}


@router.post("/reset")
async def git_reset_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE or _state.exec_git_reset is None:
        return {"ok": False, "error": "git_tools not loaded"}
    ws = str(Path(os.environ.get("HIVEMIND_WORKSPACE", ".")).resolve())
    try:
        data = await request.json()
    except Exception:
        data = {}
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
    ws = str(Path(os.environ.get("HIVEMIND_WORKSPACE", ".")).resolve())
    try:
        data = await request.json()
    except Exception:
        data = {}
    target = str(data.get("target", ""))[:120]
    if not target:
        return {"ok": False, "error": "target fehlt"}
    result = await _state.exec_git_checkout(ws, target=target)
    return {"ok": "\u2705" in result, "message": result}


@router.post("/stash")
async def git_stash_ep(request: Request):
    if not _state._GIT_TOOLS_AVAILABLE or _state.exec_git_stash is None:
        return {"ok": False, "error": "git_tools not loaded"}
    ws = str(Path(os.environ.get("HIVEMIND_WORKSPACE", ".")).resolve())
    try:
        data = await request.json()
    except Exception:
        data = {}
    action = str(data.get("action", "push"))
    message = str(data.get("message", ""))[:72]
    result = await _state.exec_git_stash(ws, action=action, message=message)
    return {"ok": "\u2705" in result or action == "list", "message": result}


@router.get("/detail")
async def git_detail_ep():
    if not _state._GIT_TOOLS_AVAILABLE or _state.exec_git_status_detailed is None:
        return {"valid": False, "error": "git_tools not loaded"}
    ws = str(Path(os.environ.get("HIVEMIND_WORKSPACE", ".")).resolve())
    return await _state.exec_git_status_detailed(ws)
