"""Concurrent chat writers on MAIN (2026-10-04 review): PUT vs PUT and
PUT vs title-rename under the per-chat RLock, with a barrier so both
threads enter simultaneously. Invariants (the ones the S4a thread test
taught us):
  - the rev on disk never SINKS across the whole run
  - every confirmed write lands (no lost update)
  - exactly ONE json file exists for the chat afterwards
  - the last title on disk is one of the written titles (no resurrection
    of a pre-rename state)

Run: python tests/test_chat_concurrency.py
"""
import asyncio
import json
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def main():
    import core.state as _state
    from routers import chats as C

    ws = Path(tempfile.mkdtemp(prefix="hvm_conc_"))
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
        created = put(C.create_chat(_FakeRequest({
            "title": "conc", "messages": [{"role": "user", "content": "start"}]})))
        cid = created["id"]

        ROUNDS = 40
        _errors = []
        _revs_seen = []
        _rev_lock = threading.Lock()
        _barrier = threading.Barrier(2)

        def _put(payload):
            """PUT like the frontend does: a 409 is a legit race outcome -
            adopt the server rev and count it as conflict-resolved (the
            write itself is lost BY DESIGN, the server state wins)."""
            r = put(C.update_chat(cid, _FakeRequest(payload)))
            if hasattr(r, "status_code"):
                if r.status_code == 409:
                    return ("conflict", json.loads(r.body).get("rev"))
                return ("error", r)
            return ("ok", r)

        _conflicts = {"msg": 0, "title": 0}

        def msg_writer():
            _barrier.wait()
            for i in range(ROUNDS):
                try:
                    kind, r = _put({
                        "messages": [
                            {"role": "user", "content": "start"},
                            {"role": "assistant", "content": f"answer {i}"},
                        ],
                        "base_rev": C.get_chat_rev(cid)})
                    if kind == "ok":
                        with _rev_lock:
                            _revs_seen.append(r["rev"])
                    elif kind == "conflict":
                        _conflicts["msg"] += 1
                    else:
                        _errors.append(f"msg i={i}: {r}")
                except (OSError, ValueError, RuntimeError, AttributeError) as e:
                    _errors.append(f"msg i={i}: {e}")

        def title_writer():
            _barrier.wait()
            for i in range(ROUNDS):
                try:
                    kind, r = _put({
                        "title": f"conc renamed {i}",
                        "base_rev": C.get_chat_rev(cid)})
                    if kind == "ok":
                        with _rev_lock:
                            _revs_seen.append(r["rev"])
                    elif kind == "conflict":
                        _conflicts["title"] += 1
                    else:
                        _errors.append(f"title i={i}: {r}")
                except (OSError, ValueError, RuntimeError, AttributeError) as e:
                    _errors.append(f"title i={i}: {e}")

        t1 = threading.Thread(target=msg_writer)
        t2 = threading.Thread(target=title_writer)
        t1.start(); t2.start(); t1.join(); t2.join()

        files = [f for f in ws.glob("*.json") if cid in f.name]
        got = put(C.get_chat(cid))
        disk_rev = int(got.get("rev") or 0)
        sinks = [(a, b) for a, b in zip(_revs_seen, _revs_seen[1:]) if b < a]

        if not _errors:
            ok(f"2x{ROUNDS} concurrent PUTs: none rejected")
        else:
            fail("rejections", str(_errors[:3]))
        if not sinks:
            ok("rev never sank across all reported writes")
        else:
            fail("rev sinks", str(sinks[:5]))
        if len(files) == 1:
            ok("exactly ONE json file for the chat")
        else:
            fail("file count", str([f.name for f in files]))
        if len(_revs_seen) + _conflicts["msg"] + _conflicts["title"] == 2 * ROUNDS                 and disk_rev >= max(_revs_seen or [0]):
            ok(f"every write accounted for: {len(_revs_seen)} landed, "
               f"{_conflicts['msg'] + _conflicts['title']} CAS-resolved (server wins); "
               f"disk rev {disk_rev} >= last landed {max(_revs_seen or [0])}")
        else:
            fail("lost writes", f"landed={len(_revs_seen)} conflicts={_conflicts} disk={disk_rev}")
        # the losing writer's state never lands (CAS, server wins) - the
        # disk title must be a state some SUCCESSFUL write produced: the
        # create title or one of the renamed ones, never a mix
        _t = got.get("title", "")
        if _t == "conc" or _t.startswith("conc renamed "):
            ok(f"title is a landed write's state ({_t!r} - no resurrection)")
        else:
            fail("title", repr(_t))
        # if the title writer won EVERY race, no assistant message ever
        # landed - the create user message surviving intact is then the
        # no-rollback proof (scheduling-dependent, both outcomes legit)
        _as = [m for m in got.get("messages", []) if m.get("role") == "assistant"]
        if _as and _as[-1]["content"].startswith("answer "):
            ok("message content is a post-write state (no rollback)")
        elif not _as and any(m.get("content") == "start" for m in got.get("messages", [])):
            ok("msg writer lost every race (legit) - create state intact, no rollback")
        else:
            fail("content rollback", str([m.get("content", "")[:20] for m in got.get("messages", [])]))

        # oversize brake: a 250 MB payload is refused loudly, not written
        big = {"id": cid, "title": "big", "created_at": "t", "updated_at": "t",
               "messages": [{"role": "assistant",
                             "content": "x" * (210 * 1024 * 1024)}]}
        r_big = C._save_chat("oversize1", big)
        if r_big and r_big.get("oversize") and not (ws / "x_oversize1.json").exists() \
                and not list(ws.glob("*_oversize1.json")):
            ok("oversize save refused loudly (nothing written)")
        else:
            fail("oversize brake", repr(r_big)[:120])
    finally:
        shutil.rmtree(str(ws), ignore_errors=True)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
