


from __future__ import annotations

import os as _os
import subprocess
from pathlib import Path

import logging

from settings import load_settings as _load_settings

logger = logging.getLogger("hivemind.git_tools")
settings = _load_settings()  # Runtime settings dict


async def _git_run(workspace: str, *cmd: str,
                   timeout: int | None = None) -> subprocess.CompletedProcess:
    """THE one git subprocess runner (2026-11-03 audit): every helper in this
    module used to define its own identical `_git` closure (10 copies) or its
    own `to_thread(subprocess.run, ...)` inline (8 more). Same behavior:
    non-blocking, captured output, utf-8 with replacement on decode errors."""
    import asyncio as _asyncio
    return await _asyncio.to_thread(
        subprocess.run, list(cmd),
        cwd=workspace, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout)


async def _paths_on_disk_or_head(workspace: str, files: list[str]) -> list[str]:
    """Scope filter shared by commit / checkpoint / squash (GIT-AUDIT P1
    logic, unified 2026-11-03): a path qualifies when it exists on disk OR in
    HEAD — a file the run DELETED is only at HEAD, and filtering it out
    silently restored the base version instead of committing the deletion.
    The caller has already contained the paths to the workspace; unknown
    paths (neither on disk nor in HEAD) are dropped."""
    out: list[str] = []
    for f in files:
        if (Path(workspace) / f).exists():
            out.append(f)
        elif (await _git_run(workspace, "git", "cat-file", "-e", f"HEAD:{f}")
                ).returncode == 0:
            out.append(f)   # deleted file: still part of the commit
    return out


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


def _panel_git_identity() -> tuple[str, str]:
    """Identity configured in the Git panel (settings git_username/git_email)."""
    try:
        _s = _load_settings() or {}
        return (str(_s.get("git_username", "") or "").strip(),
                str(_s.get("git_email", "") or "").strip())
    except Exception:
        return ("", "")


def _hivemind_install_dir() -> str:
    try:
        return str(Path(__file__).resolve().parents[1])
    except Exception:
        return ""


def auto_commit_block_reason(workspace: str) -> str:
    """Non-empty = Hivemind-initiated commits (checkpoints, auto-commit) must
    be skipped for this workspace, with this reason.

    The 2026-09-26 incident: with no git identity configured anywhere,
    checkpoint commits still ran — and with the workspace falling back to the
    HiveMind install directory they even landed in HiveMind's own repo.
    Auto-commits now require the panel identity and never touch the install
    directory itself."""
    _name, _email = _panel_git_identity()
    if not _name or not _email:
        return ("no git credentials configured in the Git panel "
                "(Username + Email below)")
    try:
        if workspace and _os.path.normcase(_os.path.abspath(workspace)) == \
                _os.path.normcase(_hivemind_install_dir()):
            return "workspace resolves to the HiveMind install directory"
    except Exception:
        pass
    return ""


async def ensure_commit_identity(workspace: str) -> None:
    """Set repo-local user.name/user.email from the panel config if the repo
    has no identity yet — commits are attributed to the panel user instead of
    whatever machine-global git config happens to exist."""
    _name, _email = _panel_git_identity()
    if not _name and not _email:
        return

    async def _get_local(key: str) -> str:
        _r = await _git_run(workspace, "git", "config", "--local", key)
        return (_r.stdout or "").strip()

    if _name and not await _get_local("user.name"):
        await _git_run(workspace, "git", "config", "--local", "user.name", _name)
    if _email and not await _get_local("user.email"):
        await _git_run(workspace, "git", "config", "--local", "user.email", _email)


def _git_auto_push_enabled() -> bool:
    try:
        return bool((_load_settings() or {}).get("git_auto_push", False))
    except Exception:
        return False


def _push_url() -> str:
    """Inline push URL for ONE git push invocation. Credentials are never
    persisted — not in the remote config, not on disk, only passed to the
    single subprocess. https only; ssh remotes carry their own auth."""
    _s = _load_settings() or {}
    _repo = str(_s.get("git_repo_url", "") or "").strip().rstrip("/")
    _user = str(_s.get("git_username", "") or "").strip()
    _token = str(_s.get("git_token", "") or "").strip()
    if not (_repo and _user and _token) or not _repo.startswith("https://"):
        return ""
    return f"https://{_user}:{_token}@{_repo[len('https://'):]}"

async def push_after_commit(workspace: str) -> str:
    """Auto-push HEAD after a successful hivemind commit. Fires ONLY when the
    Git panel Auto-Push toggle is on, the credential gate is clear, and
    repository URL + username + token are configured. Returns a status line
    ("" = push not enabled — silent by design, same as the commit toggles)."""
    if not _git_auto_push_enabled():
        return ""
    _block = auto_commit_block_reason(workspace)
    if _block:
        return f"ℹ️ auto-push skipped: {_block}"
    _url = _push_url()
    if not _url:
        return ("ℹ️ auto-push skipped: Repository URL / Username / Token "
                "missing in the Git panel")
    _r = await _git_run(workspace, "git", "push", _url, "HEAD", timeout=120)
    _s = _load_settings() or {}
    _token = str(_s.get("git_token", "") or "")
    if _r.returncode == 0:
        return "✅ auto-pushed HEAD to the configured repository"
    _err = (_r.stderr or _r.stdout or "").strip()
    if _token:
        import urllib.parse as _up
        _err = _err.replace(_token, "***")
        _err = _err.replace(_up.quote(_token), "***")  # H-audit: url-escaped form in git errors
        _err = _err.replace(_token.replace("@", "%40"), "***")
    return f"❌ auto-push failed: {_err[:200]}"


async def exec_git_commit(message: str, workspace: str,
                          files: list[str] | None = None) -> str:

    # CREDENTIAL GATE (2026-10-03): this is the single funnel for ALL
    # HiveMind-initiated commits - the per-chunk/final auto-commit AND the
    # git_commit TOOLCALL (tools/handlers/git_tools.py). The toolcall path
    # used to bypass auto_commit_block_reason: without panel credentials the
    # commit still ran, attributed to whatever machine-global git config
    # existed (the 2026-09-26 incident class, toolcall variant). The gate
    # lives here so both paths share it; the auto-commit path checks it
    # earlier as well, which is idempotent.
    _block = auto_commit_block_reason(workspace)
    if _block:
        return f"ℹ️ git_commit skipped: {_block}"

    check = await _git_run(workspace, "git", "rev-parse", "--git-dir")
    if check.returncode != 0:
        return f"⚠️ git_commit: {workspace} is not a git repository. No commit."

    await ensure_commit_identity(workspace)

    status = await _git_run(workspace, "git", "status", "--porcelain")
    if not status.stdout.strip():
            return "ℹ️ git_commit: No changes to commit."

    if not files:
        # NO `git add -A` (2026-10-14 policy, enforced 2026-11-03): the old
        # allow_add_all=True default kept a bare `git add -A` + commit branch
        # that no production caller used - exactly the index sweep the
        # gravSandbox incident banned. Scoping to the run's files is the only
        # staging strategy now; a call without a file list refuses.
        return "ℹ️ git_commit: no file list — refusing to commit the whole index."

    _existing = await _paths_on_disk_or_head(workspace, files)
    if not _existing:
        return "ℹ️ git_commit: none of the listed files exist — no commit."
    # RUN-SCOPED COMMIT (2026-10-14, security fix): `git commit -- paths`
    # commits ONLY those paths and ignores everything else in the index.
    # The previous `git add -- files` + bare `git commit` committed the
    # WHOLE index — foreign already-staged work (the owner's, in the
    # gravSandbox incident: 1186 files / 819k lines) was swept into the
    # run's chunk commit.
    # GIT-AUDIT P1-1 (2026-11-01): the scoped add must stay —
    # `git commit -- paths` matches TRACKED paths only, so a run's
    # brand-new files failed with "pathspec did not match" and nothing
    # was committed. Scoping the add to exactly the run's files keeps
    # the foreign-index protection AND commits new files.
    add_result = await _git_run(workspace, "git", "add", "--", *_existing)
    if add_result.returncode != 0:
        return f"❌ git add failed: {add_result.stderr.strip()}"
    commit_result = await _git_run(
        workspace, "git", "commit", "-m", message[:72], "--", *_existing)
    if commit_result.returncode != 0:
        _err = commit_result.stderr.strip()
        if "nothing to commit" in _err.lower() or "no changes added" in _err.lower():
            return "ℹ️ git_commit: no changes in the listed files — no commit."
        return f"❌ git commit failed: {_err}"
    hash_result = await _git_run(workspace, "git", "rev-parse", "--short", "HEAD")
    short_hash = hash_result.stdout.strip()
    return f"✅ Committed: [{short_hash}] {message[:72]}"


# ── 2.1 GIT-CHECKPOINTS (2026-08-24) ─────────────────────────────────────────
#
# History policy:
#                     Checkpoint zusammengelegt (begrenzt, Edge-Case c).


async def exec_git_checkpoint(label: str, workspace: str,
                              files: list[str] | None = None) -> str:

    _block = auto_commit_block_reason(workspace)
    if _block:
        return f"ℹ️ checkpoint skipped: {_block}"

    check = await _git_run(workspace, "git", "rev-parse", "--git-dir")
    if check.returncode != 0:
        return ""
    await ensure_commit_identity(workspace)
    status = await _git_run(workspace, "git", "status", "--porcelain")
    head = await _git_run(workspace, "git", "rev-parse", "--verify", "HEAD")
    has_head = head.returncode == 0
    clean = not status.stdout.strip()

    if clean and has_head:
        subj = await _git_run(workspace, "git", "log", "-1", "--pretty=%s")
        if _is_checkpoint_subject(subj.stdout.strip()):
            return ""
        c = await _git_run(workspace, "git", "commit", "--allow-empty",
                           "-m", f"{_checkpoint_marker()} {label}"[:72])
        if c.returncode != 0:
            return ""
        h = await _git_run(workspace, "git", "rev-parse", "--short", "HEAD")
        return f"[checkpoint {h.stdout.strip()}: {label[:50]}]"

    # DIRTY-REPO GUARD (2026-10-14, security fix): the checkpoint used to run
    # `git add -A` on a dirty repo - the gravSandbox checkpoint commit
    # (5a29baa) swept the owner's uncommitted work plus build artifacts into
    # a run commit. Without an explicit file list from the RUN, a dirty repo
    # is not ours to commit.
    if not files:
        logger.warning(
            "[git_checkpoint] dirty repo and no run file list - checkpoint "
            "skipped (would sweep foreign changes): %s", label[:60])
        return ""
    _existing = await _paths_on_disk_or_head(workspace, files)
    if not _existing:
        return ""
    add = await _git_run(workspace, "git", "add", "--", *_existing)
    if add.returncode != 0:
        return ""
    cmd = ["git", "commit", "-m", f"{_checkpoint_marker()} {label}"[:72],
           "--", *_existing]
    if not has_head:
        cmd.append("--allow-empty")
    c = await _git_run(workspace, *cmd)
    if c.returncode != 0:
        return ""
    h = await _git_run(workspace, "git", "rev-parse", "--short", "HEAD")
    return f"[checkpoint {h.stdout.strip()}: {label[:50]}]"


async def get_last_checkpoint(workspace: str) -> str | None:
    """Latest checkpoint commit (full hash) or None."""
    # The nested _run() here was dead code (never called) and would have run a
    # different command than the one below. Removed in the 2026-10-27 audit.
    r = await _git_run(workspace, "git", "log", "--pretty=%H %s", "-n", "80",
                       timeout=30)
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
    r = await _git_run(workspace, "git", "log", "--pretty=%H %s", "-n", "200")
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
                                      consolidate_only: bool = False,
                                      files: list[str] | None = None) -> str:

    check = await _git_run(workspace, "git", "rev-parse", "--git-dir")
    if check.returncode != 0:
        return ""

    if consolidate_only:
        _block = auto_commit_block_reason(workspace)
        if _block:
            return f"ℹ️ checkpoint consolidation skipped: {_block}"
    await ensure_commit_identity(workspace)

    cp_count = 0
    _cp_shas: list[str] = []
    log = await _git_run(workspace, "git", "log", "--pretty=%H%x09%s", "-n", "200")
    for _row in log.stdout.strip().splitlines():
        _sha, _, subj = _row.partition("\t")
        if subj and _is_checkpoint_subject(subj):
            cp_count += 1
            _cp_shas.append(_sha)
        else:
            break
    if cp_count == 0:
        return ""

    # B4 (audit 2026-11-03): the fold scope is what the checkpoint commits
    # actually touched. The old no-list branch ran `git add -A` + a
    # pathspec-less commit, sweeping foreign staged/untracked work in.
    _cp_files: set[str] = set()
    for _sha in _cp_shas:
        _show = await _git_run(workspace, "git", "show", "--pretty=format:",
                               "--name-only", _sha)
        _cp_files.update(x.strip() for x in _show.stdout.splitlines() if x.strip())

    base = await _find_fold_base(workspace)
    if base is None:
        root = await _git_run(workspace, "git", "rev-list", "--max-parents=0", "HEAD")
        roots = root.stdout.strip().splitlines()
        if not roots:
            return ""
        if cp_count <= 1:
            return ""
        reset = await _git_run(workspace, "git", "reset", "--soft", roots[-1].strip())
    else:
        reset = await _git_run(workspace, "git", "reset", "--soft", base)

    if reset.returncode != 0:
        return f"❌ squash: git reset failed: {reset.stderr.strip()}"

    # GIT-AUDIT P1-2 (2026-11-01): the soft-reset folds every change since
    # the base into the index — an unscoped `git add` then swept FOREIGN
    # uncommitted work into the folded commit (probed). The scope is now the
    # caller's file list, or for a checkpoint-only consolidation the files
    # the checkpoint commits themselves touched (B4, 2026-11-03). The
    # disk-or-HEAD filter (unified 2026-11-03, was two identical inline
    # loops) keeps run-DELETED paths in the scope — P1 (deep audit
    # 2026-11-03): a path deleted in a checkpoint is in
    # `git show --name-only` but not on disk; filtering by exists() dropped
    # it from the pathspec and the base version was silently restored.
    #
    # EOD-GIT-AUDIT (2026-11-01): three holes closed at once.
    #   A) the fold commit was BARE — a foreign already-STAGED file survived
    #      the scoped add but was still committed by `git commit -m`. The
    #      commit is now pathspec-scoped to the run files too.
    #   B) files=None kept `git add -A` for consolidate-only; the checkpoint
    #      file set above replaces it, so no branch sweeps the index.
    #   C) the empty-staged fallback was `git reset --hard HEAD` — it
    #      silently wiped foreign UNSTAGED modifications. Now a safe
    #      `git reset` (mixed: index back to HEAD, worktree untouched).
    _scope = await _paths_on_disk_or_head(workspace, list(files or []))

    if consolidate_only:
        _fold_scope = (await _paths_on_disk_or_head(workspace, list(files))
                       if files else
                       await _paths_on_disk_or_head(workspace, sorted(_cp_files)))
        if not _fold_scope:
            return ("ℹ️ squash-consolidate: none of the checkpoint/run files "
                    "exist — nothing to fold.")
        msg = f"{_checkpoint_marker()} session consolidated ({cp_count} checkpoints)"
        # Deletions are already staged by the soft reset; `git add` on a
        # missing path is a pathspec error, so only existing files are added.
        _add_list = [f for f in _fold_scope if (Path(workspace) / f).exists()]
        if _add_list:
            add = await _git_run(workspace, "git", "add", "-A", "--", *_add_list)
            if add.returncode != 0:
                return f"❌ git add failed: {add.stderr.strip()}"
        staged = await _git_run(workspace, "git", "diff", "--cached",
                                "--name-only", "--", *_fold_scope)
        cc = await _git_run(workspace, "git", "commit", "-m", msg[:72],
                            *(["--allow-empty"] if not staged.stdout.strip() else []),
                            "--", *_fold_scope)
        if cc.returncode != 0:
            return f"❌ squash-consolidate failed: {cc.stderr.strip()}"
        return f"✅ {cp_count} Checkpoints konsolidiert"

    if not files:
        return ("ℹ️ squash: no run file list — refusing to fold the whole "
                "index (2026-10-14 policy).")
    if not _scope:
        return "ℹ️ squash: none of the run files exist — nothing to fold."
    _add_list = [f for f in _scope if (Path(workspace) / f).exists()]
    if _add_list:
        add = await _git_run(workspace, "git", "add", "-A", "--", *_add_list)
        if add.returncode != 0:
            return f"❌ git add failed: {add.stderr.strip()}"
    staged = await _git_run(workspace, "git", "diff", "--cached",
                            "--name-only", "--", *_scope)
    if not staged.stdout.strip():
        # nothing of ours to fold — NEVER hard-reset here: the soft reset
        # above moved foreign work into the index and a --hard would wipe
        # foreign UNSTAGED edits with it (probed data loss).
        await _git_run(workspace, "git", "reset")
        return ""
    cc = await _git_run(workspace, "git", "commit", "-m", message[:72], "--", *_scope)
    if cc.returncode != 0:
        return f"❌ squash-commit failed: {cc.stderr.strip()}"
    h = await _git_run(workspace, "git", "rev-parse", "--short", "HEAD")
    return f"✅ Committed (Checkpoints gefaltet): [{h.stdout.strip()}] {message[:72]}"


async def _files_new_since_cp(workspace: str, cp_hash: str) -> list[str]:
    d = await _git_run(workspace, "git", "diff", "--name-only", cp_hash)
    changed = {l for l in d.stdout.strip().splitlines() if l}
    u = await _git_run(workspace, "git", "ls-files", "--others",
                       "--exclude-standard")
    untracked = {l for l in u.stdout.strip().splitlines() if l}

    new_files = []
    for rel in sorted(changed | untracked):
        exists_at_cp = await _git_run(workspace, "git", "cat-file", "-e",
                                      f"{cp_hash}:{rel}")
        if exists_at_cp.returncode != 0:
            new_files.append(rel)
    return new_files


async def file_changed_since(workspace: str, cp_hash: str, rel_path: str) -> bool:
    r = await _git_run(workspace, "git", "diff", "--name-only", cp_hash,
                       "--", rel_path)
    return r.returncode == 0 and bool(r.stdout.strip())


def _undo_backup_root(workspace: str) -> Path:
    """Where undo_full moves untracked files (P1, deep audit 2026-11-03).

    A linked worktree has `.git` as a FILE, so a backup under .git is not
    possible there; fall back to the temp dir instead of deleting the file.
    """
    import tempfile as _tempfile
    import time as _time
    _stamp = _time.strftime("%Y%m%d-%H%M%S") + f"-{_os.getpid()}"
    _git_dir = Path(workspace) / ".git"
    if _git_dir.is_dir():
        return _git_dir / "hive_undo_backup" / _stamp
    return Path(_tempfile.gettempdir()) / "hivemind_undo_backup" / _stamp


async def exec_git_undo_full(workspace: str) -> str:

    cp = await get_last_checkpoint(workspace)
    if not cp:
        return ""
    added = await _files_new_since_cp(workspace, cp)
    # GIT-AUDIT P1-4 (2026-11-01): reset --hard destroyed ALL uncommitted
    # work since the checkpoint, and undo_last is deliberately un-gated —
    # a model could silently wipe the owner's uncommitted changes. Capture
    # a stash snapshot first (dangling commit, working tree untouched);
    # the result names it so the destruction is always recoverable.
    _safety = await _git_run(workspace, "git", "stash", "create",
                             "undo_last safety snapshot")
    _safety_sha = _safety.stdout.strip() if _safety.returncode == 0 else ""
    reset = await _git_run(workspace, "git", "reset", "--hard", cp)
    if reset.returncode != 0:
        return f"❌ undo: reset failed: {reset.stderr.strip()}"
    # B5 (audit 2026-11-03): the untracked cleanup could not tell a file the
    # run created from one the user had before it — stash create covers
    # tracked changes only, so unlinking destroyed user data. Move them into
    # .git/hive_undo_backup instead (outside the worktree, ignored by git).
    _backup_root = _undo_backup_root(workspace)
    _backed_up = 0
    removed = []
    for rel in added:
        p = Path(workspace) / rel
        try:
            tracked = await _git_run(workspace, "git", "ls-files",
                                     "--error-unmatch", rel)
            if p.is_file() and tracked.returncode != 0:
                try:
                    _dst = _backup_root / rel
                    _dst.parent.mkdir(parents=True, exist_ok=True)
                    p.replace(_dst)
                    _backed_up += 1
                except OSError:
                    # Never delete: keep the content next to the original.
                    try:
                        p.rename(p.with_name(p.name + ".undo-bak"))
                    except OSError:
                        pass
                removed.append(rel)
        except Exception:
            pass
    h = await _git_run(workspace, "git", "rev-parse", "--short", "HEAD")
    msg = f"restored to checkpoint [{h.stdout.strip()}]"
    if _backed_up:
        msg += (f"; {_backed_up} untracked file(s) backed up to "
                f"{_backup_root}")
    if removed:
        msg += f"; removed new files: {', '.join(removed[:8])}"
    if _safety_sha:
        msg += (f"; safety snapshot {_safety_sha[:10]} "
                f"(recover with: git stash apply {_safety_sha[:10]})")
    return msg


async def exec_git_undo_file(workspace: str, abs_path: str) -> str:

    cp = await get_last_checkpoint(workspace)
    if not cp:
        return ""
    rel = _os.path.relpath(abs_path, workspace).replace("\\", "/")
    tracked_changed = await file_changed_since(workspace, cp, rel)
    untracked = await _git_run(workspace, "git", "ls-files", "--others",
                               "--exclude-standard", "--", rel)
    is_untracked = bool(untracked.stdout.strip())
    if not tracked_changed and not is_untracked:
        return ""
    exists_at_cp = await _git_run(workspace, "git", "cat-file", "-e", f"{cp}:{rel}")
    if exists_at_cp.returncode == 0:
        co = await _git_run(workspace, "git", "checkout", cp, "--", rel)
        if co.returncode != 0:
            return f"❌ undo-file: checkout failed: {co.stderr.strip()}"
        return f"restored '{rel}' from checkpoint"
    p = Path(abs_path)
    if p.exists():
        p.unlink(missing_ok=True)
        return f"removed '{rel}' (created after checkpoint)"
    return ""


# DEAD CODE REMOVED (2026-11-03 audit): get_git_diff_ctx(_async) and
# get_git_log_oneline(_async) had no caller anywhere (only a dead import in
# server.py, also removed) - each was additionally a sync+async double of the
# other. git status/log for the model goes through the git_status tool
# (tools/handlers/git_tools.py), for the panel through /git/detail.


# ── v0.96.5: Additional Git Operations ──────────────────────────────────────

async def exec_git_reset(workspace: str, target: str = "HEAD", hard: bool = False) -> str:
    """Reset workspace to target commit/branch. If hard=True, discards all uncommitted changes.
    Use with caution — hard reset cannot be undone."""
    check = await _git_run(workspace, "git", "rev-parse", "--git-dir", timeout=30)
    if check.returncode != 0:
        return f"⚠️ git_reset: {workspace} is not a git repository."

    mode = "--hard" if hard else "--soft"
    result = await _git_run(workspace, "git", "reset", mode, target, timeout=30)
    if result.returncode != 0:
        return f"❌ git reset {mode} {target} failed: {result.stderr.strip()}"
    return f"✅ Reset {mode} → {target}"


async def exec_git_checkout(workspace: str, target: str) -> str:
    """Checkout a branch or restore files. Supports branch switching and file restoration."""
    check = await _git_run(workspace, "git", "rev-parse", "--git-dir", timeout=30)
    if check.returncode != 0:
        return f"⚠️ git_checkout: {workspace} is not a git repository."

    result = await _git_run(workspace, "git", "checkout", target, timeout=30)
    if result.returncode != 0:
        return f"❌ git checkout {target} failed: {result.stderr.strip()}"
    return f"✅ Checkout → {target}"


async def exec_git_stash(workspace: str, action: str = "push", message: str = "") -> str:
    """Git stash operations: push, pop, list, drop.
    action: 'push' (default), 'pop', 'list', 'drop'"""
    check = await _git_run(workspace, "git", "rev-parse", "--git-dir", timeout=30)
    if check.returncode != 0:
        return f"⚠️ git_stash: {workspace} is not a git repository."

    if action == "push":
        cmd = ["git", "stash", "push"]
        if message:
            cmd.extend(["-m", message])
        result = await _git_run(workspace, *cmd, timeout=30)
        if result.returncode != 0:
            return f"❌ git stash push failed: {result.stderr.strip()}"
        return f"✅ Stash created" + (f": {message}" if message else "")
    elif action == "pop":
        result = await _git_run(workspace, "git", "stash", "pop", timeout=30)
        if result.returncode != 0:
            return f"❌ git stash pop failed: {result.stderr.strip()}"
        return f"✅ Stash applied (popped)"
    elif action == "list":
        result = await _git_run(workspace, "git", "stash", "list", timeout=30)
        return result.stdout.strip() or "No stashes available."
    elif action == "drop":
        result = await _git_run(workspace, "git", "stash", "drop", timeout=30)
        if result.returncode != 0:
            return f"❌ git stash drop failed: {result.stderr.strip()}"
        return f"✅ Latest stash deleted"
    else:
        return f"⚠️ Unknown stash action: {action} (push/pop/list/drop)"


async def exec_git_status_detailed(workspace: str) -> dict:
    """Returns detailed git status as dict: branch, staged, unstaged, untracked, stash_count."""
    check = await _git_run(workspace, "git", "rev-parse", "--git-dir", timeout=15)
    if check.returncode != 0:
        return {"valid": False}

    branch_r = await _git_run(workspace, "git", "rev-parse", "--abbrev-ref",
                              "HEAD", timeout=15)
    branch = branch_r.stdout.strip() if branch_r.returncode == 0 else "?"

    status_r = await _git_run(workspace, "git", "status", "--porcelain", timeout=15)
    _raw_status = status_r.stdout or ""
    lines = (_raw_status.rstrip("\r\n").replace("\r\n", "\n").split("\n")
             if _raw_status.strip() else [])
    staged = [l[3:].strip() for l in lines
              if len(l) >= 4 and l[0] not in (" ", "?")]
    unstaged = [l[3:].strip() for l in lines
                if len(l) >= 4 and not l.startswith("??") and l[1] not in (" ", "?")]
    untracked = [l[3:].strip() for l in lines if l.startswith("??")]

    stash_r = await _git_run(workspace, "git", "stash", "list", timeout=15)
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
