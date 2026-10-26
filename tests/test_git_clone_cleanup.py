# -*- coding: utf-8 -*-
"""git /init: a FAILED clone must not leave a half-written .git behind.

Live 2026-10-26: a clone that failed on a network error left `.git` in the
workspace. The next attempt then hit the "already a repo" branch and every
further git action failed with a confusing error instead of "no repository".

The cleanup removes only what the clone created (this branch only runs when
the workspace had no .git to begin with)."""
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


import routers.git as git_router  # noqa: E402

_STUB = {"git_username": "t", "git_email": "t@x", "git_token": "",
         "git_repo_url": "", "git_default_branch": "main"}
git_router.load_settings = lambda: dict(_STUB)
git_router.save_settings = lambda s: None


class _FakeRequest:
    def __init__(self, payload):
        self._p = payload

    async def json(self):
        return self._p


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=30)


# ── 1. a clone URL that cannot work: local path that does not exist ─────
ws = Path(tempfile.mkdtemp(prefix="hm_gc_ws_"))
_bad = ws.parent / "does_not_exist_origin_xyz"
r = asyncio.run(git_router.git_init_ep(_FakeRequest(
    {"workspace": str(ws), "clone_url": str(_bad)})))

check("1a a failing clone reports ok=False", r.get("ok") is False, f" ({r.get('error', '')[:60]!r})")
check("1b the error names the clone failure",
      "clone" in str(r.get("error", "")).lower())
check("1c no half-written .git is left behind", not (ws / ".git").exists(),
      f" (.git exists={ (ws / '.git').exists() })")

# ── 2. after the failed clone the workspace is still usable ─────────────
r2 = asyncio.run(git_router.git_init_ep(_FakeRequest({"workspace": str(ws)})))
check("2a a plain init after the failed clone succeeds", r2.get("ok") is True,
      f" ({r2.get('error', '')[:60]!r})")
check("2b init created a real repository", (ws / ".git").exists())
check("2c workspace is not reported as already_existed by mistake",
      r2.get("already_existed") is not True)

# ── 3. a pre-existing .git is never removed by the cleanup path ─────────
ws2 = Path(tempfile.mkdtemp(prefix="hm_gc_ws2_"))
check("3a pre-flight: repository created", _git(ws2, "init", "-q", "-b", "main").returncode == 0)
r3 = asyncio.run(git_router.git_init_ep(_FakeRequest(
    {"workspace": str(ws2), "clone_url": str(_bad)})))
check("3b an already initialised workspace keeps its .git", (ws2 / ".git").exists())
check("3c and the endpoint does not claim a clone", r3.get("ok") is True
      or "already" in str(r3).lower(), f" ({str(r3)[:70]})")

print()
print(f"SUMMARY git_clone_cleanup: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
