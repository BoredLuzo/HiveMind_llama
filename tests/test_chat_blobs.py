"""Chat blob store: hash-based content storage for the chat growth fix.

Sonnet's checklist, pinned as tests:
  - roundtrip: big panel/image payloads survive save -> load byte-exact
    while the ON-DISK json holds only {"_ref": hash}
  - migration: a legacy chat file (everything inline) loads unchanged
  - missing blob: restore yields a placeholder, never an exception
  - GC: deleting a chat removes its blob directory; a startup sweep
    removes blob dirs whose chat no longer exists

Run: python tests/test_chat_blobs.py
"""
import asyncio
import json
import shutil
import sys
import tempfile
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


def main():
    import core.state as _state
    from routers import chats as C

    ws = Path(tempfile.mkdtemp(prefix="hvm_chatblobs_"))
    _state._SESSIONS_DIR = ws
    C._cache_loaded = False
    import threading
    C._cache_lock = threading.Lock()  # normally created by server startup

    big_a = ("x" * 2000) + "-A"
    big_b = ("y" * 2000) + "-B"
    data_url = "data:image/png;base64," + ("Z" * 1200)
    chat_id = "b10ba5e1"

    def _mk_chat():
        return {
            "id": chat_id, "title": "blob test", "created_at": "t", "updated_at": "t",
            "messages": [
                {"role": "user", "content": "hi", "ts": 1,
                 "images": [{"preview": data_url}]},
                {"role": "assistant", "agent": "Coder", "content": "done", "ts": 2,
                 "cp": {
                     "src/a.js": {"content": big_a, "op": "write", "diffText": "",
                                  "diffs": [{"id": "d1", "name": "a", "text": big_b}],
                                  "view": "file"},
                     "src/small.txt": {"content": "tiny", "op": "edit",
                                       "diffText": "", "diffs": [], "view": "diff"},
                 }},
            ],
        }

    try:
        # 1. save: blobs on disk, refs in the json
        chat = _mk_chat()
        C._save_chat(chat_id, chat)
        raw = json.loads((ws / [f for f in ws.iterdir()
                                if f.name.endswith(f"{chat_id}.json")][0]
                          ).read_text(encoding="utf-8"))
        c0 = raw["messages"][1]["cp"]["src/a.js"]
        refs = (isinstance(c0.get("content"), dict) and "_ref" in c0["content"]
                and isinstance(c0["diffs"][0]["text"], dict))
        if refs:
            ok("on-disk json holds _ref instead of content")
        else:
            fail("on-disk json", json.dumps(c0)[:120])
        img0 = raw["messages"][0]["images"][0]["preview"]
        if isinstance(img0, dict) and "_ref" in img0:
            ok("on-disk json holds _ref instead of dataURL")
        else:
            fail("image ref", json.dumps(img0)[:120])
        if (c0["cp_small"] if False else raw["messages"][1]["cp"]["src/small.txt"]["content"] == "tiny"):
            ok("small content stays inline")
        else:
            fail("small inline")
        blob_files = list((ws / f"{chat_id}.blobs").iterdir())
        if len(blob_files) == 3:
            ok("3 distinct blobs written (dedup by hash)")
        else:
            fail("blob count", str([f.name for f in blob_files]))

        # 2. load via the API path: byte-exact restore
        full = C._load_chat_full(chat_id)
        C._restore_messages(chat_id, full["messages"])
        m1 = full["messages"][1]["cp"]["src/a.js"]
        if m1["content"] == big_a and m1["diffs"][0]["text"] == big_b:
            ok("roundtrip restores panel content byte-exact")
        else:
            fail("roundtrip content")
        if full["messages"][0]["images"][0]["preview"] == data_url:
            ok("roundtrip restores image dataURL")
        else:
            fail("roundtrip image")

        # 3. migration: a legacy fully-inline chat loads unchanged
        legacy = {
            "id": "1e3a2c4d", "title": "legacy", "created_at": "t", "updated_at": "t",
            "messages": [
                {"role": "user", "content": "old chat",
                 "images": [{"preview": data_url}]},
                {"role": "assistant", "agent": "A", "content": "r", "ts": 1,
                 "cp": {"f.py": {"content": big_a, "op": "write",
                                 "diffText": "", "diffs": [], "view": "file"}}},
            ],
        }
        (ws / "2026-01-01T00-00-00_legacy_1e3a2c4d.json").write_text(
            json.dumps(legacy, indent=2), encoding="utf-8")
        C._cache_loaded = False
        C._ensure_cache()
        loaded = C._load_chat_full("1e3a2c4d")
        C._restore_messages("1e3a2c4d", loaded["messages"])
        if (loaded["messages"][0]["images"][0]["preview"] == data_url
                and loaded["messages"][1]["cp"]["f.py"]["content"] == big_a):
            ok("legacy inline chat loads unchanged")
        else:
            fail("legacy migration")

        # 4. missing blob -> placeholder, no exception
        for f in (ws / f"{chat_id}.blobs").iterdir():
            f.unlink()
        full = C._load_chat_full(chat_id)
        C._restore_messages(chat_id, full["messages"])
        vals = [full["messages"][1]["cp"]["src/a.js"]["content"],
                full["messages"][1]["cp"]["src/a.js"]["diffs"][0]["text"],
                full["messages"][0]["images"][0]["preview"]]
        if any(v == C._BLOB_PLACEHOLDER for v in vals) and any(
                v.startswith("data:image/") for v in vals):
            ok("missing blob restores placeholder (text + image)")
        else:
            fail("missing blob", str([str(v)[:40] for v in vals]))

        # 5. GC: deleting the chat removes its blob dir
        asyncio.run(C.delete_chat(chat_id))
        if not (ws / f"{chat_id}.blobs").exists():
            ok("deleting a chat removes its blob dir")
        else:
            fail("delete GC")

        # 6. startup sweep removes orphan blob dirs - but only OLD ones:
        # a fresh dir (json write possibly still in flight) must survive
        import os as _os
        import time as _time
        young = ws / "y0ung00a.blobs"
        young.mkdir()
        (young / "deadbeef").write_text("x", encoding="utf-8")
        old_orphan = ws / "0ldorb00.blobs"
        old_orphan.mkdir()
        (old_orphan / "deadbeef").write_text("x", encoding="utf-8")
        _old = _time.time() - 7200
        _os.utime(old_orphan, (_old, _old))
        C._cache_loaded = False
        C._ensure_cache()
        if old_orphan.exists():
            fail("old orphan sweep")
        else:
            ok("startup sweep removes OLD blob dirs without a chat")
        if young.exists():
            ok("young blob dir survives the sweep (min age)")
        else:
            fail("young orphan removed too early")

    finally:
        shutil.rmtree(str(ws), ignore_errors=True)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
