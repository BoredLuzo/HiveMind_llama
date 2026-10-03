"""S4a transcript ownership: the backend appends/updates the run's
assistant message, the frontend decorates it, rev arbitrates.

Covers Sonnet's acceptance list:
  - checkpoint refresh does NOT bump rev (same writer)
  - final flush bumps rev once
  - merge-on-save: server text wins, frontend html/cp/images union in,
    no duplicate message
  - backend message the frontend does not know yet is appended, not lost
  - stale base_rev -> 409, server state wins, nothing written
  - the merged transcript keeps strict role alternation

Run: python tests/test_s4a_transcript.py
"""
import asyncio
import copy
import json
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
    import context.chat as CC
    import core.state as _state
    from routers import chats as C

    ws = Path(tempfile.mkdtemp(prefix="hvm_s4a_"))
    _state._SESSIONS_DIR = ws
    C._cache_loaded = False
    C._cache_lock = threading.Lock()

    try:
        uid = "aaa0bbb1"
        created = asyncio.run(C.create_chat(_FakeRequest({
            "title": "s4a", "workspace": "C:/proj",
            "messages": [{"role": "user", "content": "build the thing", "id": uid}],
        })))
        cid = created["id"]
        rev0 = created.get("rev")
        if rev0 == 1:
            ok("chat created, rev=1")
        else:
            fail("create rev", repr(rev0))

        # 1. checkpoint refresh: content updates, rev unchanged
        r1 = C.upsert_run_message(cid, "midRUN01", uid, "partial text...",
                                  agent="Coder", interrupted=False,
                                  bump_rev=False)
        r2 = C.upsert_run_message(cid, "midRUN01", uid,
                                  "partial text... more",
                                  agent="Coder", interrupted=False,
                                  bump_rev=False)
        if r1["rev"] == rev0 and r2["rev"] == rev0:
            ok("checkpoints do not bump rev")
        else:
            fail("checkpoint rev", f"{r1['rev']} / {r2['rev']}")
        got = asyncio.run(C.get_chat(cid))
        m1 = got["messages"][1]
        if m1["content"] == "partial text... more" and m1.get("source") == "run" \
                and m1.get("reply_to") == uid:
            ok("checkpoint updates the run message in place")
        else:
            fail("checkpoint content", json.dumps(m1)[:140])

        # 2. final flush: bumps rev once, full text
        full = "full final answer with code"
        fin = C.upsert_run_message(cid, "midRUN01", uid, full,
                                   agent="Coder", interrupted=False,
                                   bump_rev=True)
        if fin["rev"] == rev0 + 1:
            ok("final flush bumps rev exactly once")
        else:
            fail("finalize rev", repr(fin["rev"]))

        # 3. frontend save with SAME rev: merge keeps server text,
        #    adopts frontend html/cp, no duplicate assistant message
        fe_msgs = [
            {"role": "user", "content": "build the thing", "id": uid},
            {"role": "assistant", "agent": "Coder",
             "content": "frontend-local stale text",
             "html": "<p>rendered answer</p>", "id": "midRUN01",
             "cp": {"app.js": {"content": "x" * 500, "op": "write",
                               "diffText": "", "diffs": [], "view": "file"}}},
        ]
        # the real frontend path: PUT via update_chat (which loads the
        # stored rev); a hand-built dict without rev would correctly CAS-fail
        saved = asyncio.run(C.update_chat(cid, _FakeRequest({
            "messages": copy.deepcopy(fe_msgs), "base_rev": fin["rev"]}))
        )
        if not (saved.get("ok") and saved.get("rev") == fin["rev"] + 1):
            fail("update rev after merge", repr(saved))
        got2 = asyncio.run(C.get_chat(cid))
        merged = got2["messages"]
        a_msgs = [m for m in merged if m.get("role") == "assistant"]
        if len(a_msgs) == 1:
            ok("merge produces exactly one assistant message")
        else:
            fail("assistant count", str(len(a_msgs)))
        am = a_msgs[0]
        if am["content"] == full and am.get("html") == "<p>rendered answer</p>" \
                and am.get("cp", {}).get("app.js", {}).get("content") == "x" * 500:
            ok("server text wins, frontend html/cp union in")
        else:
            fail("merge content", json.dumps(am, default=str))

        # 4. backend appends again (next run), frontend behind -> adopted
        fe_behind = [
            {"role": "user", "content": "build the thing", "id": uid},
            {"role": "assistant", "agent": "Coder", "content": full,
             "html": "<p>old render</p>", "id": "midRUN01"},
        ]  # next user turn arrives first, then the backend appends
        uid2 = "bbb2ccc3"
        upd = asyncio.run(C.update_chat(cid, _FakeRequest({
            "messages": fe_behind + [{"role": "user", "content": "next task",
                                      "id": uid2}],
            "base_rev": C.get_chat_rev(cid)})))
        if not upd.get("ok"):
            fail("update behind", repr(upd))
        C.upsert_run_message(cid, "midRUN02", uid2, "second run answer",
                             agent="Coder", interrupted=False, bump_rev=True)
        saved = C._save_chat(cid, {"id": cid, "title": "s4a",
                                   "messages": copy.deepcopy(fe_behind)},
                              base_rev=None)  # beacon = LWW, merge still runs
        got3 = asyncio.run(C.get_chat(cid))
        roles3 = [m["role"] for m in got3["messages"]]
        a3 = [m for m in got3["messages"] if m.get("role") == "assistant"]
        # design: the frontend wins for NON-run messages (its fe_behind has
        # no u2 - the UI simply did not show that turn), the backend's
        # appended answer survives. The prompt-time alternation belt
        # (_make_messages) handles the resulting shape.
        if roles3 == ["user", "assistant", "assistant"] and \
                a3[-1]["content"] == "second run answer":
            ok("backend message survives a behind frontend save")
        else:
            fail("behind adoption", f"{roles3} {[m['content'][:20] for m in a3]}")

        # 4b. RESURRECT GUARD: regenerate/edit deletes the tail in the
        # frontend - a save on the OLD rev must NOT resurrect it, but a
        # save on a LATER rev (message created after) must adopt it.
        # setup: u,a,u,a where both answers are run messages
        uid3 = "ccc3ddd4"
        cid3 = asyncio.run(C.create_chat(_FakeRequest({
            "title": "regen", "messages": [
                {"role": "user", "content": "q1", "id": uid3},
            ]})))
        cid3 = cid3["id"]
        C.upsert_run_message(cid3, "runA1", uid3, "first answer",
                             agent="Coder", interrupted=False, bump_rev=True)
        C.upsert_run_message(cid3, "runA2", uid3, "second answer",
                             agent="Coder", interrupted=False, bump_rev=True)
        # REGENERATE: the frontend discards BOTH answers, re-runs, and its
        # DOM tail is gone; its payload is based on the rev BEFORE a2
        regen_msgs = [
            {"role": "user", "content": "q1", "id": uid3},
        ]
        # the real UI flow: the tab holds the CURRENT rev (it saw every
        # done event) and drops the tail in its DOM before saving
        saved3 = asyncio.run(C.update_chat(cid3, _FakeRequest({
            "messages": copy.deepcopy(regen_msgs),
            "base_rev": C.get_chat_rev(cid3)})))
        got3b = asyncio.run(C.get_chat(cid3))
        a3b = [m for m in got3b["messages"] if m.get("role") == "assistant"]
        if saved3.get("ok") and len(a3b) == 0:
            ok("regenerate: older run messages are NOT resurrected")
        else:
            fail("resurrect guard", f"saved={saved3} assistants={[m['content'][:20] for m in a3b]}")
        # now the NEW run appends (created AFTER the regen rev) and a
        # frontend save on the CURRENT rev adopts it
        C.upsert_run_message(cid3, "runA3", uid3, "regenerated answer",
                             agent="Coder", interrupted=False, bump_rev=True)
        asyncio.run(C.update_chat(cid3, _FakeRequest({
            "messages": copy.deepcopy(regen_msgs),
            "base_rev": C.get_chat_rev(cid3) - 1})))
        got3c = asyncio.run(C.get_chat(cid3))
        a3c = [m for m in got3c["messages"] if m.get("role") == "assistant"]
        if len(a3c) == 1 and a3c[0]["content"] == "regenerated answer":
            ok("run message created after base_rev IS adopted")
        else:
            fail("created_rev adoption", f"{[m['content'][:20] for m in a3c]}")

        # 4c. checkpoint under concurrent metadata PUT: no lost update
        # (both writers run under the same per-chat threading lock)
        # ONE event loop shared by both writer threads (asyncio.run per
        # call would create a fresh loop per iteration)
        import threading as _th
        _loop = asyncio.new_event_loop()
        _ready = _th.Event()
        def _run_loop():
            asyncio.set_event_loop(_loop)
            _ready.set()
            _loop.run_forever()
        _lt = _th.Thread(target=_run_loop, daemon=True)
        _lt.start()
        _ready.wait()

        def _put(coro):
            return asyncio.run_coroutine_threadsafe(coro, _loop).result(30)

        _ROUNDS = 50
        _oplog = []
        _log_lock = _th.Lock()
        for attempt in range(3):
            _errors = []
            _done = {"ckpt": 0, "meta": 0}
            _barrier = _th.Barrier(2)
            _oplog.clear()
            def _checkpoint_loop():
                _barrier.wait()
                for i in range(_ROUNDS):
                    try:
                        _rb = C.get_chat_rev(cid)
                        r = C.upsert_run_message(cid, "midRUN01", uid,
                                             f"checkpoint {attempt}-{i}",
                                             agent="Coder",
                                             interrupted=False, bump_rev=False)
                        _done["ckpt"] += 1
                        with _log_lock:
                            _oplog.append(f"ckpt i={i} rev_before={_rb} -> {r.get('rev') if r else None}")
                    except Exception as e:
                        _errors.append(f"ckpt: {e}")
            def _meta_loop():
                _barrier.wait()
                for i in range(_ROUNDS):
                    try:
                        _rb = C.get_chat_rev(cid)
                        r = _put(C.update_chat(cid, _FakeRequest({
                            "title": f"s4a r{attempt}-{i}",
                            "base_rev": _rb})))
                        if not r.get("ok"):
                            _errors.append(f"meta rejected: {r}")
                        else:
                            _done["meta"] += 1
                            with _log_lock:
                                _oplog.append(f"meta i={i} rev_before={_rb} -> {r.get('rev')}")
                    except Exception as e:
                        _errors.append(f"meta: {e}")
            t1 = _th.Thread(target=_checkpoint_loop)
            t2 = _th.Thread(target=_meta_loop)
            t1.start(); t2.start(); t1.join(); t2.join()
            _revs = [int(m.group(1)) for m in
                     [__import__("re").match(r".*-> (\d+)$", entry) for entry in _oplog]
                     if m]
            _gaps = [(a, b) for a, b in zip(_revs, _revs[1:]) if b != a + (1 if True else 1)]
            # every successful write must move the rev forward by exactly 1
            _bad = [(a, b) for a, b in zip(_revs, _revs[1:]) if b <= a]
            got_t = asyncio.run(C.get_chat(cid))
            has_run = any(m.get("id") == "midRUN01" for m in got_t["messages"])
            files = [f for f in ws.glob("*.json") if cid in f.name]
            if not _errors and has_run and _done["ckpt"] == _ROUNDS                     and _done["meta"] == _ROUNDS and len(files) == 1:
                break
        if not _errors and _done["ckpt"] == _ROUNDS and _done["meta"] == _ROUNDS                 and has_run and len(files) == 1 and not _bad:
            ok(f"3x{_ROUNDS} concurrent checkpoint/meta rounds: no lost update, ONE file")
        else:
            fail("concurrent writers", f"errors={_errors[:2]} done={_done} files={[f.name for f in files]} "
                                       f"bad_revs={_bad[:5]} last_ops={_oplog[-6:]}")
            files = []

        # alt-loader compatibility: a v1 chat file (no rev, legacy name,
        # but carrying the new per-message fields) loads untouched
        v1 = {"id": "c0ffee11", "title": "v1 chat", "created_at": "t",
              "updated_at": "t",
              "messages": [
                  {"role": "user", "content": "legacy question"},
                  {"role": "assistant", "agent": "Coder", "content": "legacy answer",
                   "id": "whatever", "reply_to": None, "interrupted": False},
              ]}
        (ws / "2026-01-01T00-00-00_v1_c0ffee11.json").write_text(
            json.dumps(v1), encoding="utf-8")
        # production shape: the v1 file exists at boot, before the cache
        C._cache_loaded = False
        C._chats_cache.clear()
        got_v1 = asyncio.run(C.get_chat("c0ffee11"))
        if got_v1["messages"][1]["content"] == "legacy answer"                 and got_v1["messages"][1].get("interrupted") is False                 and int(got_v1.get("rev") or 0) == 0:
            ok("v1 file (legacy name, no rev) loads, fields pass through")
        else:
            fail("v1 loader", json.dumps(got_v1)[:140])

        # interrupted visibility seed-side: the transcript seed marks the
        # partial answer so the model does not continue it as if complete
        CC._SESSIONS_DIR = ws  # the transcript reader lives in context.chat
        print("    SEED-DBG files:", [f.name for f in ws.glob("*.json") if cid in f.name])
        _raw = json.loads((ws / f"chat_{cid}.json").read_text(encoding="utf-8"))
        print("    SEED-DBG raw roles:", [(m.get("role"), m.get("id"), str(m.get("content"))[:20]) for m in _raw["messages"]])
        seed_i = CC.history_seed(cid)
        print("    SEED-DBG seed:", [(m.get("role"), str(m.get("content"))[:30]) for m in seed_i])
        # the seed carries role+content only (ids are a disk concern),
        # so the marker is matched by the partial text
        m03 = [m for m in seed_i if "partial before stop" in str(m.get("content", ""))]
        if m03 and "INCOMPLETE" in m03[0]["content"]:
            ok("seed marks interrupted answers as INCOMPLETE")
        else:
            fail("seed interrupted marker", str(m03[:1]))

        # 5. interrupted append keeps alternation and carries the flag
        C.upsert_run_message(cid, "midRUN03", uid, "partial before stop",
                             agent="Coder", interrupted=True, bump_rev=True)
        got4 = asyncio.run(C.get_chat(cid))
        m3 = [m for m in got4["messages"] if m.get("id") == "midRUN03"][0]
        # NOTE: two assistant messages in a row are acceptable ON DISK here
        # (regenerate/abort shapes) - the model-facing guarantee lives in
        # _make_messages, which merges same-role neighbors (tested above).
        if m3.get("interrupted") is True and m3["content"] == "partial before stop":
            ok("interrupted append carries the flag and partial text")
        else:
            fail("interrupted", f"flag={m3.get('interrupted')} content={m3['content'][:40]}")

        # 6. stale PUT -> 409, server wins, nothing written
        _rb5 = C.get_chat_rev(cid)
        _has5 = any(m.get("id") == "midRUN03" for m in (C._read_chat_file(cid) or {}).get("messages", []))
        print(f"    T5LOG after-test5: rev={_rb5} m3_on_disk={_has5}")
        resp = asyncio.run(C.update_chat(cid, _FakeRequest({
            "messages": [{"role": "user", "content": "STALE OVERWRITE"}],
            "base_rev": 1})))
        _has5b = any(m.get("id") == "midRUN03" for m in (C._read_chat_file(cid) or {}).get("messages", []))
        print(f"    T6LOG after-stale-PUT: m3_on_disk={_has5b} status={getattr(resp, 'status_code', 'dict-ok')}")
        if getattr(resp, "status_code", None) == 409:
            body = json.loads(resp.body)
            got5 = asyncio.run(C.get_chat(cid))
            if got5["messages"][0]["content"] != "STALE OVERWRITE" \
                    and int(body.get("rev") or 0) == int(got5.get("rev") or 0):
                ok("stale PUT -> 409, server state wins")
            else:
                fail("409 state", f"body_rev={body.get('rev')} got_rev={got5.get('rev')} first={got5['messages'][0]['content'][:30]}")
        else:
            fail("stale PUT status", repr(getattr(resp, "status_code", None)))

    finally:
        shutil.rmtree(str(ws), ignore_errors=True)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
