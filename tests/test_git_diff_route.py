# -*- coding: utf-8 -*-
"""D7 git/diff endpoint: read-only, capped workspace diff for the UI."""
import asyncio
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from routers import git as _git
from tests._harness import Harness

h = Harness(style="plain", pad=True)
check = h.check


class _Req:
    def __init__(self, ws):
        self.query_params = {"workspace": str(ws)}

    async def json(self):
        return {}


def _fake_run(cmd, **kw):
    if "--stat" in cmd:
        return mock.Mock(returncode=0, stdout=" mod.py | 2 +-\n", stderr="")
    return mock.Mock(returncode=0, stdout="+new line\n-old line\n", stderr="")


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    with mock.patch.object(_git.subprocess, "run", side_effect=_fake_run):
        out = asyncio.run(_git.git_diff_ep(_Req(td)))
    check("diff endpoint returns stat and body",
          out.get("ok") is True and "mod.py" in out.get("stat", "")
          and "+new line" in out.get("diff", ""), f"({out})")
    check("diff endpoint reports a byte count and no truncation",
          out.get("bytes") == 20 and out.get("truncated") is False,
          f"({out.get('bytes')}/{out.get('truncated')})")

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    _bad = mock.Mock(returncode=1, stdout="", stderr="fatal: not a git repository")
    with mock.patch.object(_git.subprocess, "run", return_value=_bad):
        out_bad = asyncio.run(_git.git_diff_ep(_Req(td)))
    check("non-git workspace returns ok=False with the git error",
          out_bad.get("ok") is False and "not a git repository" in out_bad.get("error", ""))

sys.exit(h.finish("git_diff_route"))
