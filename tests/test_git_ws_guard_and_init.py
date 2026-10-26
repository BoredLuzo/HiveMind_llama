# -*- coding: utf-8 -*-
"""Git endpoint workspace guard + init hardening (audit round 2026-10-25).

Pins the behaviors fixed after the lens-4/lens-5 audits:
  A. SILENT-FALLBACK GUARD: an explicit workspace that is not a directory on
     this machine must 400 on every /git endpoint — the helpers used to drop
     it and the endpoints then acted on the DEFAULT repo (hard reset on the
     wrong repo risk; the documented case is a UI sending a path that does
     not exist here).
  B. /git/config honors ?workspace=/body workspace like /git/status.
  C. init hardening: non-dict body, no-workspace reject (never git-init the
     install dir), URL vetting (leading "-" and ext:: transports),
     _run_git None -> ok:False (missing git binary = error, not 500),
     clone path tagged "cloned", repo_valid surfaced.
"""
import asyncio
import json
import os
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


import routers.git as G  # noqa: E402
import core.state as _state  # noqa: E402


class _FakeReq:
    def __init__(self, payload=None, query=None):
        self._p = payload
        self.query_params = query or {}

    async def json(self):
        if self._p is None:
            raise ValueError("no body")
        return self._p


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x", *args],
                          cwd=str(cwd), capture_output=True, text=True, timeout=30)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hm_gitws_"))
    ws_repo = tmp / "proj"
    ws_repo.mkdir()
    assert _git(ws_repo, "init", "-q", "-b", "main").returncode == 0
    (ws_repo / "f.txt").write_text("x", encoding="utf-8")
    assert _git(ws_repo, "add", ".").returncode == 0
    assert _git(ws_repo, "commit", "-q", "-m", "i").returncode == 0
    dead = str(tmp / "does_not_exist")

    # stub the settings the endpoints read (never touch the real ones)
    _orig_ws = _state.settings.get("workspace")
    _state.settings["workspace"] = str(tmp / "default_ws")  # exists, no repo
    (tmp / "default_ws").mkdir(exist_ok=True)
    _orig_avail = _state._GIT_TOOLS_AVAILABLE
    _state._GIT_TOOLS_AVAILABLE = True

    try:
        print("\n=== /git workspace reject guard (silent-fallback fix) ===\n")

        # A: destructive trio must refuse a dead path WITHOUT acting
        _called = []
        _orig_reset = _state.exec_git_reset

        async def _reset_stub(ws, **kw):
            _called.append(ws)
            return "x"

        _state.exec_git_reset = _reset_stub
        try:
            r = asyncio.run(G.git_reset_ep(_FakeReq({"workspace": dead, "target": "HEAD", "hard": True})))
            check("A1: /git/reset 400 on dead workspace",
                  getattr(r, "status_code", 0) == 400, f"got {r!r:.80}")
            check("A2: reset executor never ran", not _called)
            r = asyncio.run(G.git_reset_ep(_FakeReq({"workspace": str(ws_repo), "target": "HEAD", "hard": False})))
            check("A3: /git/reset passes a live workspace to the executor",
                  _called and _called[0] == str(ws_repo.resolve()))
        finally:
            _state.exec_git_reset = _orig_reset

        r = asyncio.run(G.git_branches_ep(_FakeReq(query={"workspace": dead})))
        check("A4: /git/branches 400 on dead query workspace",
              getattr(r, "status_code", 0) == 400)
        _orig_det = _state.exec_git_status_detailed

        async def _det_stub(ws):
            return {"valid": True, "ws": ws}

        _state.exec_git_status_detailed = _det_stub
        try:
            r = asyncio.run(G.git_detail_ep(_FakeReq(query={"workspace": dead})))
            check("A5: /git/detail 400 on dead query workspace",
                  getattr(r, "status_code", 0) == 400)
            r = asyncio.run(G.git_detail_ep(_FakeReq(query={"workspace": str(ws_repo)})))
            check("A5b: /git/detail passes the resolved ws through",
                  isinstance(r, dict) and r.get("ws") == str(ws_repo.resolve()))
        finally:
            _state.exec_git_status_detailed = _orig_det
        r = asyncio.run(G.validate_git_ep(_FakeReq({"workspace": dead})))
        check("A6: /git/validate 400 on dead workspace",
              getattr(r, "status_code", 0) == 400)
        r = asyncio.run(G.git_init_ep(_FakeReq({"workspace": dead})))
        check("A7: /git/init 400 on dead workspace",
              getattr(r, "status_code", 0) == 400)

        # empty workspace still resolves the default (unchanged behavior)
        r = asyncio.run(G.git_branches_ep(_FakeReq(query={})))
        check("A8: empty workspace keeps default resolution (no 400)",
              not hasattr(r, "status_code") or r.status_code != 400)

        # B: /git/config honors the workspace param like /git/status
        r = asyncio.run(G.get_git_config_ep(_FakeReq(query={"workspace": str(ws_repo)})))
        check("B1: GET /git/config reports the requested repo's branch",
              isinstance(r, dict) and r.get("branch") == "main", str(r.get("branch")))
        r = asyncio.run(G.get_git_config_ep(_FakeReq(query={"workspace": dead})))
        check("B2: GET /git/config 400 on dead workspace",
              getattr(r, "status_code", 0) == 400)

        print("\n=== /git/init hardening ===\n")
        # C1: a non-dict body must not crash
        r = asyncio.run(G.git_init_ep(_FakeReq([1, 2])))
        check("C1: non-dict body handled (no 500)", isinstance(r, dict) or getattr(r, "status_code", None) == 400)
        # C2: no workspace configured -> never init the install dir
        _orig_sws = _state.settings.get("workspace")
        _state.settings["workspace"] = ""
        _orig_env = os.environ.pop("HIVEMIND_WORKSPACE", None)
        try:
            r = asyncio.run(G.git_init_ep(_FakeReq({})))
            ok_false = isinstance(r, dict) and r.get("ok") is False
            check("C2: init without any configured workspace is refused",
                  ok_false, str(r)[:80])
        finally:
            _state.settings["workspace"] = _orig_sws
            if _orig_env is not None:
                os.environ["HIVEMIND_WORKSPACE"] = _orig_env
        # C3: URL vetting
        for bad in ("-upload-pack=evil", "ext::sh -c evil"):
            r = asyncio.run(G.git_init_ep(_FakeReq(
                {"workspace": str(ws_repo), "clone_url": bad})))
            check(f"C3: clone_url {bad[:16]!r} rejected",
                  isinstance(r, dict) and r.get("ok") is False and "rejected" in str(r.get("error", "")))
        # C4: plain in-place init on a fresh dir works and tags the response
        ws_fresh = tmp / "fresh"
        ws_fresh.mkdir()
        r = asyncio.run(G.git_init_ep(_FakeReq({"workspace": str(ws_fresh)})))
        check("C4: in-place init ok with initialized=True",
              isinstance(r, dict) and r.get("ok") is True and r.get("initialized") is True,
              str(r)[:100])
        check("C5: repo_valid surfaced", isinstance(r, dict) and "repo_valid" in r)
        # C6: clone into an empty workspace -> response tagged cloned
        origin = tmp / "origin"
        origin.mkdir()
        assert _git(origin, "init", "-q", "-b", "main").returncode == 0
        (origin / "f.txt").write_text("x", encoding="utf-8")
        assert _git(origin, "add", ".").returncode == 0
        assert _git(origin, "commit", "-q", "-m", "i").returncode == 0
        ws_clone = tmp / "cloneme"
        ws_clone.mkdir()
        r = asyncio.run(G.git_init_ep(_FakeReq(
            {"workspace": str(ws_clone), "clone_url": str(origin)})))
        check("C6: clone path tagged cloned=True",
              isinstance(r, dict) and r.get("ok") is True and r.get("cloned") is True,
              str(r)[:100])
        # C7: git binary missing -> _run_git returns None and the endpoint
        # answers ok:False instead of raising
        _orig_run = G._run_git
        G._run_git = lambda *a, **k: None
        try:
            ws_x = tmp / "x2"
            ws_x.mkdir()
            r = asyncio.run(G.git_init_ep(_FakeReq({"workspace": str(ws_x)})))
            check("C7: _run_git None -> ok:False (no 500)",
                  isinstance(r, dict) and r.get("ok") is False)
        finally:
            G._run_git = _orig_run
    finally:
        if _orig_ws is None:
            _state.settings.pop("workspace", None)
        else:
            _state.settings["workspace"] = _orig_ws
        _state._GIT_TOOLS_AVAILABLE = _orig_avail

    print(f"\n  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
