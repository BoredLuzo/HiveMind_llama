# -*- coding: utf-8 -*-
"""B5: undo_full must not destroy pre-existing untracked files.

git stash create snapshots TRACKED changes only. The untracked cleanup then
unlinked every file that was not in the checkpoint — including the user's own
untracked files that existed before the run. Content must survive undo.
"""
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hive_functions import git_tools as G       # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


repo = Path(tempfile.mkdtemp())
git(repo, "init", "-q")
git(repo, "config", "user.email", "test@example.invalid")
git(repo, "config", "user.name", "Test")
(repo / "a.txt").write_text("v1\n", encoding="utf-8")
git(repo, "add", "a.txt")
git(repo, "commit", "-q", "-m", "init")

# The user's own untracked file, present BEFORE the checkpoint commit.
(repo / "notes.txt").write_text("mine, do not delete\n", encoding="utf-8")

marker = G._checkpoint_marker()
(repo / "a.txt").write_text("v2\n", encoding="utf-8")
git(repo, "add", "a.txt")
git(repo, "commit", "-q", "-m", f"{marker} cp")

# A file the run created after the checkpoint.
(repo / "run_output.txt").write_text("generated\n", encoding="utf-8")

out = asyncio.run(G.exec_git_undo_full(str(repo)))

check("1 the tracked file is back at the checkpoint",
      (repo / "a.txt").read_text(encoding="utf-8") == "v2\n")
check("2 the run-created file is gone from the worktree",
      not (repo / "run_output.txt").exists())
check("3 the pre-existing untracked content survives somewhere",
      "mine, do not delete" in "".join(
          p.read_text(encoding="utf-8", errors="replace")
          for p in repo.rglob("*.txt") if p.is_file()))
backups = list((repo / ".git" / "hive_undo_backup").rglob("notes.txt")) \
    if (repo / ".git" / "hive_undo_backup").exists() else []
check("4 it is in the undo backup", bool(backups),
      str(backups[0]) if backups else "(no backup dir)")
check("5 the result names the backup", "backup" in out.lower() or "gesichert" in out.lower(),
      out[:100])

# P1 (deep audit 2026-11-03): a linked worktree has .git as a FILE; the backup
# must go to the temp dir instead of falling back to unlink.
wt = Path(tempfile.mkdtemp())
(wt / ".git").write_text("gitdir: C:/elsewhere/.git/worktrees/wt\n", encoding="utf-8")
root = G._undo_backup_root(str(wt))
check("6 a worktree backup goes outside the workspace",
      not str(root).startswith(str(wt)) and "hivemind_undo_backup" in str(root),
      str(root))

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
