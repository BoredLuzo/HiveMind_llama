


from __future__ import annotations

import subprocess
from pathlib import Path

from settings import load_settings as _load_settings
settings = _load_settings()  # Runtime settings dict


def _git_prefix() -> str:
    """Commit-Prefix aus Runtime-Settings (live gelesen, nicht Import-Snapshot)."""
    try:
        return (str(_load_settings().get("git_commit_prefix", "") or "").strip()
                or "hivemind:")
    except Exception:
        return "hivemind:"


def _checkpoint_marker() -> str:
    return f"{_git_prefix()} checkpoint:"


def _is_checkpoint_subject(subject: str) -> bool:
    return subject.strip().lower().startswith(_checkpoint_marker().lower())


async def exec_git_commit(message: str, workspace: str, files: list[str] | None = None) -> str:


    import asyncio as _asyncio

    async def _git(*cmd: str) -> subprocess.CompletedProcess:
        """Non-blocking subprocess via Thread-Pool."""
        return await _asyncio.to_thread(
            subprocess.run, list(cmd),
            cwd=workspace, capture_output=True,
            text=True, encoding="utf-8", errors="replace"
        )

    check = await _git("git", "rev-parse", "--git-dir")
    if check.returncode != 0:
        return f"⚠️ git_commit: {workspace} is not a git repository. No commit."

    status = await _git("git", "status", "--porcelain")
    if not status.stdout.strip():
            return "ℹ️ git_commit: No changes to commit."

    if files:
        _existing = [f for f in files if (Path(workspace) / f).exists()]
        if not _existing:
            return "ℹ️ git_commit: none of the listed files exist — no commit."
        add_result = await _git("git", "add", "--", *_existing)
        if add_result.returncode != 0:
            return f"❌ git add fehlgeschlagen: {add_result.stderr.strip()}"
        staged = await _git("git", "diff", "--cached", "--name-only")
        if not staged.stdout.strip():
            return "ℹ️ git_commit: No changes to commit."
    else:
        add_result = await _git("git", "add", "-A")
        if add_result.returncode != 0:
            return f"❌ git add fehlgeschlagen: {add_result.stderr.strip()}"

    commit_result = await _git("git", "commit", "-m", message[:72])
    if commit_result.returncode != 0:
        return f"❌ git commit fehlgeschlagen: {commit_result.stderr.strip()}"

    hash_result = await _git("git", "rev-parse", "--short", "HEAD")
    short_hash = hash_result.stdout.strip()
    return f"✅ Committed: [{short_hash}] {message[:72]}"


# ── 2.1 GIT-CHECKPOINTS (2026-08-24) ─────────────────────────────────────────
#
# History policy:
#                     Checkpoint zusammengelegt (begrenzt, Edge-Case c).

import os as _os


async def exec_git_checkpoint(label: str, workspace: str) -> str:


    import asyncio as _asyncio

    async def _git(*cmd: str) -> subprocess.CompletedProcess:
        return await _asyncio.to_thread(
            subprocess.run, list(cmd),
            cwd=workspace, capture_output=True,
            text=True, encoding="utf-8", errors="replace"
        )

    check = await _git("git", "rev-parse", "--git-dir")
    if check.returncode != 0:
        return ""
    status = await _git("git", "status", "--porcelain")
    head = await _git("git", "rev-parse", "--verify", "HEAD")
    has_head = head.returncode == 0
    clean = not status.stdout.strip()

    if clean and has_head:
        subj = await _git("git", "log", "-1", "--pretty=%s")
        if _is_checkpoint_subject(subj.stdout.strip()):
            return ""
        cmd = ["git", "commit", "--allow-empty",
               "-m", f"{_checkpoint_marker()} {label}"[:72]]
        c = await _git(*cmd)
        if c.returncode != 0:
            return ""
        h = await _git("git", "rev-parse", "--short", "HEAD")
        return f"[checkpoint {h.stdout.strip()}: {label[:50]}]"

    add = await _git("git", "add", "-A")
    if add.returncode != 0:
        return ""
    cmd = ["git", "commit", "-m", f"{_checkpoint_marker()} {label}"[:72]]
    if not has_head:
        cmd.append("--allow-empty")
    c = await _git(*cmd)
    if c.returncode != 0:
        return ""
    h = await _git("git", "rev-parse", "--short", "HEAD")
    return f"[checkpoint {h.stdout.strip()}: {label[:50]}]"


async def get_last_checkpoint(workspace: str) -> str | None:
    """Latest checkpoint commit (full hash) or None."""
    import asyncio as _asyncio

    def _run():
        return subprocess.run(
            ["git", "rev-list", "-n", "1", "--grep=", "-i", "HEAD"],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace")

    r = await _asyncio.to_thread(
        subprocess.run,
        ["git", "log", "--pretty=%H %s", "-n", "80"],
        cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return None
    marker = _checkpoint_marker().lower()
    for line in r.stdout.strip().splitlines():
        if not line:
            continue
        h, _, subj = line.partition(" ")
        if subj.strip().lower().startswith(marker):
            return h.strip()
    return None


async def _find_fold_base(workspace: str) -> str | None:
    import asyncio as _asyncio
    r = await _asyncio.to_thread(
        subprocess.run,
        ["git", "log", "--pretty=%H %s", "-n", "200"],
        cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return None
    for line in r.stdout.strip().splitlines():
        if not line:
            continue
        h, _, subj = line.partition(" ")
        if not _is_checkpoint_subject(subj):
            return h.strip()
    return None


async def exec_git_squash_checkpoints(message: str, workspace: str,
                                      consolidate_only: bool = False) -> str:


    import asyncio as _asyncio

    async def _git(*cmd: str) -> subprocess.CompletedProcess:
        return await _asyncio.to_thread(
            subprocess.run, list(cmd),
            cwd=workspace, capture_output=True,
            text=True, encoding="utf-8", errors="replace"
        )

    check = await _git("git", "rev-parse", "--git-dir")
    if check.returncode != 0:
        return ""

    cp_count = 0
    log = await _git("git", "log", "--pretty=%s", "-n", "200")
    for subj in log.stdout.strip().splitlines():
        if subj and _is_checkpoint_subject(subj):
            cp_count += 1
        else:
            break
    if cp_count == 0:
        return ""

    base = await _find_fold_base(workspace)
    if base is None:
        root = await _git("git", "rev-list", "--max-parents=0", "HEAD")
        roots = root.stdout.strip().splitlines()
        if not roots:
            return ""
        if cp_count <= 1:
            return ""
        reset = await _git("git", "reset", "--soft", roots[-1].strip())
    else:
        reset = await _git("git", "reset", "--soft", base)

    if reset.returncode != 0:
        return f"❌ squash: git reset fehlgeschlagen: {reset.stderr.strip()}"

    if consolidate_only:
        msg = f"{_checkpoint_marker()} session consolidated ({cp_count} checkpoints)"
        add = await _git("git", "add", "-A")
        staged = await _git("git", "diff", "--cached", "--name-only")
        cc = await _git("git", "commit", "-m", msg[:72],
                        *(["--allow-empty"] if not staged.stdout.strip() else []))
        if cc.returncode != 0:
            return f"❌ squash-consolidate fehlgeschlagen: {cc.stderr.strip()}"
        return f"✅ {cp_count} Checkpoints konsolidiert"

    add = await _git("git", "add", "-A")
    if add.returncode != 0:
        return f"❌ git add fehlgeschlagen: {add.stderr.strip()}"
    staged = await _git("git", "diff", "--cached", "--name-only")
    if not staged.stdout.strip():
        cc = await _git("git", "reset", "--hard", "HEAD")
        return ""
    cc = await _git("git", "commit", "-m", message[:72])
    if cc.returncode != 0:
        return f"❌ squash-commit fehlgeschlagen: {cc.stderr.strip()}"
    h = await _git("git", "rev-parse", "--short", "HEAD")
    return f"✅ Committed (Checkpoints gefaltet): [{h.stdout.strip()}] {message[:72]}"


async def _files_new_since_cp(workspace: str, cp_hash: str) -> list[str]:

    import asyncio as _asyncio

    async def _git(*cmd: str) -> subprocess.CompletedProcess:
        return await _asyncio.to_thread(
            subprocess.run, list(cmd),
            cwd=workspace, capture_output=True,
            text=True, encoding="utf-8", errors="replace"
        )

    d = await _git("git", "diff", "--name-only", cp_hash)
    changed = {l for l in d.stdout.strip().splitlines() if l}
    u = await _git("git", "ls-files", "--others", "--exclude-standard")
    untracked = {l for l in u.stdout.strip().splitlines() if l}

    new_files = []
    for rel in sorted(changed | untracked):
        exists_at_cp = await _git("git", "cat-file", "-e", f"{cp_hash}:{rel}")
        if exists_at_cp.returncode != 0:
            new_files.append(rel)
    return new_files


async def list_files_added_since(workspace: str, cp_hash: str) -> list[str]:
    return await _files_new_since_cp(workspace, cp_hash)


async def file_changed_since(workspace: str, cp_hash: str, rel_path: str) -> bool:

    import asyncio as _asyncio
    r = await _asyncio.to_thread(
        subprocess.run,
        ["git", "diff", "--name-only", cp_hash, "--", rel_path],
        cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0 and bool(r.stdout.strip())


async def exec_git_undo_full(workspace: str) -> str:

    import asyncio as _asyncio

    async def _git(*cmd: str) -> subprocess.CompletedProcess:
        return await _asyncio.to_thread(
            subprocess.run, list(cmd),
            cwd=workspace, capture_output=True,
            text=True, encoding="utf-8", errors="replace"
        )

    cp = await get_last_checkpoint(workspace)
    if not cp:
        return ""
    added = await _files_new_since_cp(workspace, cp)
    reset = await _git("git", "reset", "--hard", cp)
    if reset.returncode != 0:
        return f"❌ undo: reset failed: {reset.stderr.strip()}"
    removed = []
    for rel in added:
        p = Path(workspace) / rel
        try:
            tracked = await _asyncio.to_thread(
                subprocess.run, ["git", "ls-files", "--error-unmatch", rel],
                cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if p.is_file() and tracked.returncode != 0:
                p.unlink(missing_ok=True)
                removed.append(rel)
        except Exception:
            pass
    h = await _git("git", "rev-parse", "--short", "HEAD")
    msg = f"restored to checkpoint [{h.stdout.strip()}]"
    if removed:
        msg += f"; neue Dateien entfernt: {', '.join(removed[:8])}"
    return msg


async def exec_git_undo_file(workspace: str, abs_path: str) -> str:

    import asyncio as _asyncio

    async def _git(*cmd: str) -> subprocess.CompletedProcess:
        return await _asyncio.to_thread(
            subprocess.run, list(cmd),
            cwd=workspace, capture_output=True,
            text=True, encoding="utf-8", errors="replace"
        )

    cp = await get_last_checkpoint(workspace)
    if not cp:
        return ""
    rel = _os.path.relpath(abs_path, workspace).replace("\\", "/")
    tracked_changed = await file_changed_since(workspace, cp, rel)
    untracked = await _git("git", "ls-files", "--others", "--exclude-standard",
                           "--", rel)
    is_untracked = bool(untracked.stdout.strip())
    if not tracked_changed and not is_untracked:
        return ""
    exists_at_cp = await _git("git", "cat-file", "-e", f"{cp}:{rel}")
    if exists_at_cp.returncode == 0:
        co = await _git("git", "checkout", cp, "--", rel)
        if co.returncode != 0:
            return f"❌ undo-file: checkout failed: {co.stderr.strip()}"
        return f"restored '{rel}' from checkpoint"
    p = Path(abs_path)
    if p.exists():
        p.unlink(missing_ok=True)
        return f"removed '{rel}' (erstellt nach Checkpoint)"
    return ""


def get_git_diff_ctx(workspace: str, max_chars: int = 2000) -> str:


    try:
        check = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=workspace, capture_output=True,
            text=True, encoding="utf-8", errors="replace"
        )
        if check.returncode != 0:
            return ""

        diff_result = subprocess.run(
            ["git", "diff", "HEAD", "--stat", "--no-color"],
            cwd=workspace, capture_output=True,
            text=True, encoding="utf-8", errors="replace"
        )
        stat = diff_result.stdout.strip()
        if not stat:
            return ""

        return f"\n\nCurrent git diff (--stat):\n{stat[:max_chars]}"

    except Exception:
        return ""


async def get_git_diff_ctx_async(workspace: str, max_chars: int = 2000) -> str:


    import asyncio as _asyncio
    return await _asyncio.to_thread(get_git_diff_ctx, workspace, max_chars)


def get_git_log_oneline(workspace: str, n: int = 5) -> str:


    try:
        result = subprocess.run(
            ["git", "log", f"-{n}", "--oneline", "--no-color"],
            cwd=workspace, capture_output=True,
            text=True, encoding="utf-8", errors="replace"
        )
        return result.stdout.strip()
    except Exception:
        return ""


async def get_git_log_oneline_async(workspace: str, n: int = 5) -> str:
    """
    BUG-15 FIX: Async-Variante — delegiert an Thread-Pool.
    """
    import asyncio as _asyncio
    return await _asyncio.to_thread(get_git_log_oneline, workspace, n)


# ── v0.96.5: Additional Git Operations ──────────────────────────────────────

async def exec_git_reset(workspace: str, target: str = "HEAD", hard: bool = False) -> str:
    """Reset workspace to target commit/branch. If hard=True, discards all uncommitted changes.
    Use with caution — hard reset cannot be undone."""
    import asyncio as _asyncio

    async def _git(*cmd: str) -> subprocess.CompletedProcess:
        return await _asyncio.to_thread(
            subprocess.run, list(cmd),
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        )

    check = await _git("git", "rev-parse", "--git-dir")
    if check.returncode != 0:
        return f"⚠️ git_reset: {workspace} is not a git repository."

    mode = "--hard" if hard else "--soft"
    result = await _git("git", "reset", mode, target)
    if result.returncode != 0:
        return f"❌ git reset {mode} {target} fehlgeschlagen: {result.stderr.strip()}"
    return f"✅ Reset {mode} → {target}"


async def exec_git_checkout(workspace: str, target: str) -> str:
    """Checkout a branch or restore files. Supports branch switching and file restoration."""
    import asyncio as _asyncio

    async def _git(*cmd: str) -> subprocess.CompletedProcess:
        return await _asyncio.to_thread(
            subprocess.run, list(cmd),
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        )

    check = await _git("git", "rev-parse", "--git-dir")
    if check.returncode != 0:
        return f"⚠️ git_checkout: {workspace} is not a git repository."

    result = await _git("git", "checkout", target)
    if result.returncode != 0:
        return f"❌ git checkout {target} fehlgeschlagen: {result.stderr.strip()}"
    return f"✅ Checkout → {target}"


async def exec_git_stash(workspace: str, action: str = "push", message: str = "") -> str:
    """Git stash operations: push, pop, list, drop.
    action: 'push' (default), 'pop', 'list', 'drop'"""
    import asyncio as _asyncio

    async def _git(*cmd: str) -> subprocess.CompletedProcess:
        return await _asyncio.to_thread(
            subprocess.run, list(cmd),
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        )

    check = await _git("git", "rev-parse", "--git-dir")
    if check.returncode != 0:
        return f"⚠️ git_stash: {workspace} is not a git repository."

    if action == "push":
        cmd = ["git", "stash", "push"]
        if message:
            cmd.extend(["-m", message])
        result = await _git(*cmd)
        if result.returncode != 0:
            return f"❌ git stash push failed: {result.stderr.strip()}"
        return f"✅ Stash created" + (f": {message}" if message else "")
    elif action == "pop":
        result = await _git("git", "stash", "pop")
        if result.returncode != 0:
            return f"❌ git stash pop failed: {result.stderr.strip()}"
        return f"✅ Stash applied (popped)"
    elif action == "list":
        result = await _git("git", "stash", "list")
        return result.stdout.strip() or "No stashes available."
    elif action == "drop":
        result = await _git("git", "stash", "drop")
        if result.returncode != 0:
            return f"❌ git stash drop failed: {result.stderr.strip()}"
        return f"✅ Latest stash deleted"
    else:
        return f"⚠️ Unknown stash action: {action} (push/pop/list/drop)"


async def exec_git_status_detailed(workspace: str) -> dict:
    """Returns detailed git status as dict: branch, staged, unstaged, untracked, stash_count."""
    import asyncio as _asyncio

    async def _git(*cmd: str) -> subprocess.CompletedProcess:
        return await _asyncio.to_thread(
            subprocess.run, list(cmd),
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15
        )

    check = await _git("git", "rev-parse", "--git-dir")
    if check.returncode != 0:
        return {"valid": False}

    branch_r = await _git("git", "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_r.stdout.strip() if branch_r.returncode == 0 else "?"

    status_r = await _git("git", "status", "--porcelain")
    _raw_status = status_r.stdout or ""
    lines = (_raw_status.rstrip("\r\n").replace("\r\n", "\n").split("\n")
             if _raw_status.strip() else [])
    staged = [l[3:].strip() for l in lines
              if len(l) >= 4 and l[0] not in (" ", "?")]
    unstaged = [l[3:].strip() for l in lines
                if len(l) >= 4 and not l.startswith("??") and l[1] not in (" ", "?")]
    untracked = [l[3:].strip() for l in lines if l.startswith("??")]

    stash_r = await _git("git", "stash", "list")
    stash_count = len(stash_r.stdout.strip().split("\n")) if stash_r.stdout.strip() else 0

    return {
        "valid": True,
        "branch": branch,
        "staged": staged[:20],
        "unstaged": unstaged[:20],
        "untracked": untracked[:20],
        "staged_count": len(staged),
        "unstaged_count": len(unstaged),
        "untracked_count": len(untracked),
        "stash_count": stash_count,
    }
