"""WORKSPACE-GUARD on PUT /chats/{id} (audit N9, 2026-10-15).

PUT /chats/{id} applied any workspace string unvalidated while
POST /settings rejects dead paths. A silent cross-project switch here
poisons the whole chat (GET /chats/{id} feeds the json value into the
run resolver). Contract: dead path -> 400, previous value stays;
existing path -> applied; empty string -> clears."""
import asyncio
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def main():
    import core.state as _state
    from routers import chats as C

    ws = Path(tempfile.mkdtemp(prefix="hvm_wsguard_"))
    _state._SESSIONS_DIR = ws
    C._cache_loaded = False
    C._cache_lock = threading.Lock()
    _loop = asyncio.new_event_loop()
    _ready = threading.Event()

    def _run_loop():
        asyncio.set_event_loop(_loop)
        _ready.set()
        _loop.run_forever()

    threading.Thread(target=_run_loop, daemon=True).start()
    _ready.wait()

    def put(coro):
        return asyncio.run_coroutine_threadsafe(coro, _loop).result(30)

    try:
        created = put(C.create_chat(_FakeRequest({"title": "wsguard"})))
        cid = created["id"]

        dead = str(ws / "does_not_exist_proj")
        r = put(C.update_chat(cid, _FakeRequest({"workspace": dead})))
        check("dead workspace -> 400",
              hasattr(r, "status_code") and r.status_code == 400,
              f" got {getattr(r, 'status_code', r)}")
        check("dead workspace error names the path",
              hasattr(r, "body") and dead in json.loads(r.body).get("error", ""))
        got = put(C.get_chat(cid))
        check("previous value stays active after reject", not got.get("workspace"))

        good = ws / "real_proj"
        good.mkdir()
        r = put(C.update_chat(cid, _FakeRequest({"workspace": str(good)})))
        check("existing workspace applied", not hasattr(r, "status_code"))
        got = put(C.get_chat(cid))
        check("existing workspace persisted", got.get("workspace") == str(good))

        r = put(C.update_chat(cid, _FakeRequest({"workspace": ""})))
        check("empty string clears (no 400)", not hasattr(r, "status_code"))
        got = put(C.get_chat(cid))
        check("cleared workspace empty", not got.get("workspace"))

        r = put(C.update_chat(cid, _FakeRequest({"title": "no ws key"})))
        check("PUT without workspace key unaffected", not hasattr(r, "status_code"))
    finally:
        _loop.call_soon_threadsafe(_loop.stop)

    print()
    print(f"SUMMARY chats_ws_guard: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


main()
