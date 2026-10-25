# -*- coding: utf-8 -*-
"""git /init endpoint: the clone path must not crash (live 2026-10-25).

POST /git/init with an EMPTY workspace + clone_url took the clone branch;
a successful clone fell through to the return, where _gh_created/_gh_msg
were unbound (they were only initialized in the init branch) -> 500
UnboundLocalError. Reproduced here offline: local origin repo, real git
clone, both paths (clone + in-place init) must return ok."""
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

# keep the test away from real settings: stub load/save
_STUB = {"git_username": "t", "git_email": "t@x", "git_token": "",
         "git_repo_url": "", "git_default_branch": "main"}
_saved = []
_orig_load, _orig_save = git_router.load_settings, git_router.save_settings
git_router.load_settings = lambda: dict(_STUB)
git_router.save_settings = lambda s: _saved.append(s)


class _FakeRequest:
    def __init__(self, payload):
        self._p = payload

    async def json(self):
        return self._p


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=30)


# a local origin with one commit (offline clone target)
origin = Path(tempfile.mkdtemp(prefix="hm_gi_origin_"))
(origin / "README.md").write_text("x", encoding="utf-8")
assert _git(origin, "init", "-q", "-b", "main").returncode == 0
assert _git(origin, "add", ".").returncode == 0
assert _git(origin, "commit", "-q", "-m", "init").returncode == 0

# ── 1. the live crash: empty workspace + clone_url -> clone path ─────────
ws1 = Path(tempfile.mkdtemp(prefix="hm_gi_ws1_"))
r1 = asyncio.run(git_router.git_init_ep(_FakeRequest(
    {"workspace": str(ws1), "clone_url": str(origin)})))
check("1a clone path returns ok (was UnboundLocalError 500)",
      r1.get("ok") is True, f" (github_msg={r1.get('github_msg')!r})")
check("1b github_created present and false without a github remote",
      r1.get("github_created") is False)
check("1c clone actually brought the commit into the workspace",
      (ws1 / "README.md").exists() and (ws1 / ".git").exists())

# ── 2. in-place init: non-empty workspace, no remote ─────────────────────
ws2 = Path(tempfile.mkdtemp(prefix="hm_gi_ws2_"))
(ws2 / "code.py").write_text("print(1)", encoding="utf-8")
r2 = asyncio.run(git_router.git_init_ep(_FakeRequest(
    {"workspace": str(ws2)})))
check("2a init path still returns ok", r2.get("ok") is True)
check("2b init path reports github_created",
      "github_created" in r2 and r2.get("github_created") is False)

# ── 3. already-a-repo short-circuit stays intact ─────────────────────────
r3 = asyncio.run(git_router.git_init_ep(_FakeRequest(
    {"workspace": str(ws1)})))
check("3a existing repo short-circuits with already_existed",
      r3.get("ok") is True and r3.get("already_existed") is True)

git_router.load_settings, git_router.save_settings = _orig_load, _orig_save
print(f"\ngit_init_clone: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
