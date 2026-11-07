# -*- coding: utf-8 -*-
"""Git commit scope + funnel coherence (audit round 2026-11-03).

Pins the consolidated behavior after the Herz-und-Niere git round:
  1. exec_git_commit stages ONLY the listed files - foreign staged work
     never lands in the run's commit (2026-10-14 policy).
  2. exec_git_commit WITHOUT a file list refuses - the `git add -A`
     escape hatch (allow_add_all=True default) is gone; scoping is the
     only staging strategy.
  3. A file the run DELETED (exists at HEAD, not on disk) is committed as
     a deletion - the same exists-or-HEAD scope the squash fold already
     had (P1, deep audit 2026-11-03); exec_git_commit and
     exec_git_checkpoint now share one filter (_paths_on_disk_or_head).
  4. The auto-commit funnel (preflight._auto_commit_chunk) keeps its
     gates (git toggle, workspace commit gate, credential block) and
     commits scoped with the configured prefix.
  5. routers/git._validate_git_repo resolves override/default exactly
     like _resolve_ws (the duplicated inline resolution is gone).

Run: python tests/test_git_commit_scope.py
"""
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="results", pad=True)
check = h.check


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def head_files(repo):
    """Files in the HEAD TREE (not the commit's diff) - deletions must be
    checked against the tree, `git show --name-only` still lists a deleted
    path as a changed file."""
    return set(git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.split())


def make_repo():
    repo = Path(tempfile.mkdtemp(prefix="hvm_gitscope_"))
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("v1\n", encoding="utf-8")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-q", "-m", "init")
    return repo


import hive_functions.git_tools as G            # noqa: E402

# The credential policy has its own suite (test_git_gate.py); give the
# funnel a panel identity so the scope checks actually reach git.
_FAKE = {"git_username": "Panel User", "git_email": "panel@user",
         "git_repo_url": "", "git_token": "",
         "git_commit_prefix": "hivemind:", "git_auto_push": False}
G._load_settings = lambda: dict(_FAKE)

print("\n=== exec_git_commit: scoped staging ===\n")
repo = make_repo()
(repo / "run_out.txt").write_text("run output\n", encoding="utf-8")
(repo / "foreign.txt").write_text("owner work\n", encoding="utf-8")
git(repo, "add", "foreign.txt")           # foreign work already staged

out = asyncio.run(G.exec_git_commit("hivemind: scoped run", str(repo),
                                    files=["run_out.txt"]))
check("1a commit succeeds", out.startswith("✅"), out[:80])
hf = head_files(repo)
check("1b the run file is committed", "run_out.txt" in hf, str(hf))
check("1c the foreign staged file is NOT committed", "foreign.txt" not in hf)
check("1d the foreign file stays staged",
      "foreign.txt" in git(repo, "diff", "--cached", "--name-only").stdout)

print("\n=== exec_git_commit: no file list refuses (no add -A) ===\n")
repo2 = make_repo()
(repo2 / "unlisted.txt").write_text("x\n", encoding="utf-8")
out = asyncio.run(G.exec_git_commit("hivemind: sweep attempt", str(repo2)))
check("2a refuses without a file list",
      out.startswith("ℹ️") and "no file list" in out, out[:90])
check("2b nothing was committed",
      len(git(repo2, "log", "--oneline").stdout.strip().splitlines()) == 1)

print("\n=== exec_git_commit / exec_git_checkpoint: deleted file folds ===\n")
repo3 = make_repo()
(repo3 / "gone.txt").write_text("bye\n", encoding="utf-8")
git(repo3, "add", "gone.txt")
git(repo3, "commit", "-q", "-m", "add gone")
(repo3 / "gone.txt").unlink()             # the run deleted it
out = asyncio.run(G.exec_git_commit("hivemind: rm gone", str(repo3),
                                    files=["gone.txt"]))
check("3a deletion commit succeeds", out.startswith("✅"), out[:80])
check("3b gone.txt is gone from HEAD", "gone.txt" not in head_files(repo3))

repo4 = make_repo()
(repo4 / "gone2.txt").write_text("bye\n", encoding="utf-8")
git(repo4, "add", "gone2.txt")
git(repo4, "commit", "-q", "-m", "add gone2")
(repo4 / "gone2.txt").unlink()
out = asyncio.run(G.exec_git_checkpoint("cp with deletion", str(repo4),
                                        files=["gone2.txt"]))
check("3c checkpoint records the deletion", out.startswith("[checkpoint"),
      out[:80])
check("3d gone2.txt is gone from HEAD", "gone2.txt" not in head_files(repo4))

print("\n=== _paths_on_disk_or_head (the shared scope filter) ===\n")
repo5 = make_repo()
(repo5 / "on_disk.txt").write_text("d\n", encoding="utf-8")
git(repo5, "add", "on_disk.txt")
git(repo5, "commit", "-q", "-m", "second")
(repo5 / "on_disk.txt").unlink()          # now only at HEAD
s = asyncio.run(G._paths_on_disk_or_head(str(repo5),
                                         ["on_disk.txt", "nope.txt"]))
check("4a HEAD-only path kept", s == ["on_disk.txt"], str(s))

print("\n=== auto-commit funnel (preflight._auto_commit_chunk) ===\n")
import core.duo.preflight as P                  # noqa: E402

out = asyncio.run(P._auto_commit_chunk("u", "s", str(repo5), False,
                                       files=["x"], commit_allowed=True))
check("5a git toggle off -> silent", out == "", repr(out[:60]))

out = asyncio.run(P._auto_commit_chunk("u", "s", str(repo5), True,
                                       files=["x"], commit_allowed=False))
check("5b no explicit workspace -> skip line", "auto-commit skipped" in out,
      out[:90])

_ws_blocked = make_repo()
G2 = G
_real_ident = G2._panel_git_identity
G2._panel_git_identity = lambda: ("", "")   # no panel credentials
out = asyncio.run(P._auto_commit_chunk("u", "s", str(_ws_blocked), True,
                                       files=["base.txt"], commit_allowed=True))
G2._panel_git_identity = _real_ident
check("5c no credentials -> block reason surfaces", "credentials" in out,
      out[:90])

_ws_ok = make_repo()
(_ws_ok / "fun.txt").write_text("fun\n", encoding="utf-8")
out = asyncio.run(P._auto_commit_chunk("do it", "sub task", str(_ws_ok), True,
                                       files=["fun.txt"], commit_allowed=True))
check("5d funnel commits scoped", out.startswith("✅"), out[:90])
hf = head_files(_ws_ok)
check("5e only the run file landed", "fun.txt" in hf and "base.txt" in hf
      and len(hf) == 2, str(hf))
subj = git(_ws_ok, "log", "-1", "--pretty=%s").stdout.strip()
check("5f prefix + label in the message", subj == "hivemind: sub task", subj)

print("\n=== routers/git._validate_git_repo resolution ===\n")
import routers.git as RG                        # noqa: E402

_ws_def = Path(tempfile.mkdtemp(prefix="hvm_gitdef_"))
git(_ws_def, "init", "-q", "-b", "main")
(_ws_def / "seed.txt").write_text("s\n", encoding="utf-8")
git(_ws_def, "add", "seed.txt")
git(_ws_def, "commit", "-q", "-m", "seed")   # unborn HEAD reports no branch
_orig_ws = RG.settings.get("workspace")
RG.settings["workspace"] = str(_ws_def)
try:
    check("6a default resolution matches _resolve_ws",
          RG._validate_git_repo(None)[1] == "main")
    ws_other = make_repo()
    git(ws_other, "checkout", "-q", "-b", "topic")
    check("6b override resolution matches _resolve_ws",
          RG._validate_git_repo(str(ws_other))[1] == "topic")
finally:
    if _orig_ws is None:
        RG.settings.pop("workspace", None)
    else:
        RG.settings["workspace"] = _orig_ws

print("\n=== duo_runner push helper (single implementation) ===\n")
_dsrc = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8",
                                                    errors="replace")
check("7a exactly one push_after_commit import (inside the helper)",
      _dsrc.count("push_after_commit as _pap") == 1)
check("7b the helper exists", "async def _auto_push_after_commit" in _dsrc)
check("7c no dead consolidate prefix read left", "_pfx" not in _dsrc)

print("\n=== empty file list is a silent skip ===\n")
_empty_out = asyncio.run(P._auto_commit_chunk("u", "s", str(ROOT), True, files=[]))
check("8a a run with no written files does not emit a commit note",
      _empty_out == "", _empty_out[:60])

sys.exit(h.finish())
