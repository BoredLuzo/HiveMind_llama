# -*- coding: utf-8 -*-
"""D7 checkpoint list/restore: only checkpoint commits, safety snapshot, content."""
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hive_functions.git_tools import list_checkpoints, exec_git_restore_checkpoint
from tests._harness import Harness

h = Harness(style="plain", pad=True)
check = h.check


def _git(ws, *args):
    return subprocess.run(["git", *args], cwd=ws, capture_output=True,
                          text=True, timeout=20)


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    _git(ws, "init", "-q")
    _git(ws, "config", "user.name", "Test")
    _git(ws, "config", "user.email", "test@example.invalid")
    (ws / "a.txt").write_text("base\n", encoding="utf-8")
    _git(ws, "add", "a.txt")
    _git(ws, "commit", "-q", "-m", "base")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "hivemind: checkpoint: unit test")
    (ws / "a.txt").write_text("changed\n", encoding="utf-8")

    rows = asyncio.run(list_checkpoints(str(ws), limit=10))
    check("checkpoint list finds the marker commit",
          len(rows) == 1 and rows[0]["label"] == "unit test", f"({rows})")
    check("checkpoint list returns a full hash and short form",
          len(rows[0]["hash"]) == 40 and len(rows[0]["short"]) == 10)

    good = asyncio.run(exec_git_restore_checkpoint(str(ws), rows[0]["hash"]))
    check("restore accepts a checkpoint and reports success",
          "restored to checkpoint" in good, f"({good})")
    check("restore returns the file content at the checkpoint",
          (ws / "a.txt").read_text(encoding="utf-8") == "base\n")

    bad = asyncio.run(exec_git_restore_checkpoint(str(ws), "deadbeef"))
    check("restore rejects a non-checkpoint hash",
          "not a checkpoint" in bad, f"({bad})")
    bad2 = asyncio.run(exec_git_restore_checkpoint(str(ws), "not-a-hash"))
    check("restore rejects a non-hash target", "7-40" in bad2, f"({bad2})")

sys.exit(h.finish("git_checkpoints"))
