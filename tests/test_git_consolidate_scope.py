# -*- coding: utf-8 -*-
"""B4: checkpoint consolidation must not sweep foreign staged work in.

The soft reset puts the checkpoint changes into the index; the old
consolidate branch then ran `git add -A` (no file list) and committed without
a pathspec, folding unrelated staged/untracked files into the checkpoint
commit. The scope is now the files the checkpoint commits actually touched.
"""
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hive_functions import git_tools as G       # noqa: E402

# The credential policy is not what this test is about.
G.auto_commit_block_reason = lambda workspace: ""

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


def make_repo():
    repo = Path(tempfile.mkdtemp())
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "init")
    marker = G._checkpoint_marker()
    for i in (2, 3):
        (repo / "a.txt").write_text(f"v{i}\n", encoding="utf-8")
        git(repo, "add", "a.txt")
        git(repo, "commit", "-q", "-m", f"{marker} cp{i}")
    (repo / "foreign.txt").write_text("not part of the run\n", encoding="utf-8")
    git(repo, "add", "foreign.txt")
    return repo


def head_files(repo):
    return git(repo, "show", "--name-only", "--pretty=format:", "HEAD").stdout


def staged_files(repo):
    return git(repo, "diff", "--cached", "--name-only").stdout


# 1. consolidate without a run file list -> checkpoint files only
repo = make_repo()
out = asyncio.run(G.exec_git_squash_checkpoints(
    "consolidate", str(repo), consolidate_only=True))
files = head_files(repo)
check("1a consolidation actually ran", "✅" in out, out[:80])
check("1b the checkpoint file is in the fold commit", "a.txt" in files,
      files.replace("\n", " ")[:90])
check("1c the foreign staged file is NOT committed", "foreign.txt" not in files,
      files.replace("\n", " ")[:90])
check("1d the foreign file is still staged",
      "foreign.txt" in staged_files(repo), staged_files(repo)[:90])

# 2. explicit file list still wins and stays scoped
repo2 = make_repo()
out = asyncio.run(G.exec_git_squash_checkpoints(
    "consolidate", str(repo2), consolidate_only=True, files=["a.txt"]))
files = head_files(repo2)
check("2a explicit list folds a.txt", "a.txt" in files)
check("2b explicit list leaves the foreign file out", "foreign.txt" not in files)
check("2c the foreign file stays staged", "foreign.txt" in staged_files(repo2))

# P1 (deep audit 2026-11-03): a checkpoint that DELETES a file must fold the
# deletion, not silently restore the base version.
repo3 = Path(tempfile.mkdtemp())
git(repo3, "init", "-q")
git(repo3, "config", "user.email", "test@example.invalid")
git(repo3, "config", "user.name", "Test")
(repo3 / "keep.txt").write_text("k\n", encoding="utf-8")
(repo3 / "gone.txt").write_text("g\n", encoding="utf-8")
git(repo3, "add", "keep.txt", "gone.txt")
git(repo3, "commit", "-q", "-m", "init")
(repo3 / "gone.txt").unlink()
git(repo3, "rm", "-q", "gone.txt")
_marker3 = G._checkpoint_marker()
git(repo3, "commit", "-q", "-m", f"{_marker3} cp-delete")
out3 = asyncio.run(G.exec_git_squash_checkpoints(
    "consolidate", str(repo3), consolidate_only=True))
tree = git(repo3, "ls-tree", "-r", "--name-only", "HEAD").stdout
check("3a consolidation ran", "✅" in out3, out3[:80])
check("3b the deletion is folded (file is gone from HEAD)",
      "gone.txt" not in tree, tree.replace("\n", " ")[:90])
check("3c the other file is still there", "keep.txt" in tree)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
