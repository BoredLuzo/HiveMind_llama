# -*- coding: utf-8 -*-
"""Workspace guards round 2026-10-25: chats POST handlers, skills router,
tool-use counter cap.

Pins:
  A. POST /chats + POST /chats/persist never STORE a dead workspace
     (logged + dropped; the autosave/beacon caller must not lose the
     messages over the workspace field — PUT keeps its strict 400).
  B. /skills + /skills/toggle accept an optional workspace and reject a
     dead one (they used to list the DEFAULT project's skills only).
  C. tools/runner._TOOL_USE_COUNTS evicts old run buckets (the per-run
     instrumentation leak).
"""
import asyncio
import json
import shutil
import sys
import tempfile
import threading
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


class _FakeReq:
    def __init__(self, payload=None):
        self._p = payload

    async def json(self):
        return self._p


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hm_wsguard_"))
    try:
        import core.state as _state
        from routers import chats as C

        print("\n=== chats POST workspace guard ===\n")
        ws_live = tmp / "live_ws"
        ws_live.mkdir()
        dead = str(tmp / "dead_ws")

        _state._SESSIONS_DIR = tmp / "sessions"
        _state._SESSIONS_DIR.mkdir()
        C._cache_loaded = False
        C._cache_lock = threading.Lock()

        r = asyncio.run(C.create_chat(_FakeReq(
            {"title": "t", "messages": [], "workspace": dead})))
        check("A1: create_chat with dead ws stores NO workspace key",
              isinstance(r, dict) and r.get("ok") is True
              and "workspace" not in (r.get("chat") or {}), str(r)[:90])

        r = asyncio.run(C.create_chat(_FakeReq(
            {"title": "t2", "messages": [], "workspace": str(ws_live)})))
        check("A2: create_chat with a live ws stores it",
              isinstance(r, dict) and (r.get("chat") or {}).get("workspace") == str(ws_live))

        cid = r["id"]
        r = asyncio.run(C.persist_chat(_FakeReq(
            {"chat_id": cid, "messages": [{"role": "user", "content": "hi"}],
             "workspace": dead})))
        check("A3: persist with dead ws keeps the previous workspace",
              isinstance(r, dict) and r.get("updated") is True)
        _files = list(_state._SESSIONS_DIR.glob(f"*{cid}.json"))
        assert _files, f"chat file for {cid} not found"
        saved = json.loads(_files[0].read_text(encoding="utf-8"))
        check("A4: on-disk chat kept the live ws, dead one dropped",
              saved.get("workspace") == str(ws_live), str(saved.get("workspace")))

        print("\n=== /skills workspace param ===\n")
        from routers import skills as S

        r = asyncio.run(S.list_skills(workspace=str(tmp / "nope")))
        check("B1: /skills 400 on dead workspace",
              getattr(r, "status_code", 0) == 400)
        r = asyncio.run(S.list_skills(workspace=str(ws_live)))
        check("B2: /skills accepts a live workspace",
              isinstance(r, dict) and r.get("workspace") == str(ws_live))
        r = asyncio.run(S.list_skills())
        check("B3: /skills without param keeps default resolution",
              isinstance(r, dict) and "workspace" in r)
        r = asyncio.run(S.toggle_skill(_FakeReq({"name": "nope", "enabled": True,
                                                 "workspace": str(tmp / "nope")})))
        check("B4: /skills/toggle 400 on dead workspace",
              getattr(r, "status_code", 0) == 400)

        print("\n=== _TOOL_USE_COUNTS cap ===\n")
        import tools.runner as R
        _orig = dict(R._TOOL_USE_COUNTS)
        try:
            R._TOOL_USE_COUNTS.clear()
            for i in range(100):
                R._note_tool_use(f"run-{i:04d}", "run_bash", "ok")
            check("C1: run buckets capped at 64",
                  len(R._TOOL_USE_COUNTS) <= 64 + 0, str(len(R._TOOL_USE_COUNTS)))
            check("C2: newest buckets survive", "run-0099" in R._TOOL_USE_COUNTS)
            check("C3: counting still works",
                  R._TOOL_USE_COUNTS["run-0099"].get("run_bash:ok") == 1)
        finally:
            R._TOOL_USE_COUNTS.clear()
            R._TOOL_USE_COUNTS.update(_orig)
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)

    print(f"\n  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
