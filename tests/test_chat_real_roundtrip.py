"""Blob-format roundtrip: restore(extract(messages)) must be lossless.

HERMETIC by default (2026-10-27): the fixtures are synthetic chats built in
a temp sessions dir through the production save/restore functions, covering
the whole blob surface -
  - content/html/cp/image-preview payloads >= _BLOB_MIN_CHARS (blobified on
    save), including a CRLF payload (the text-mode corruption regression)
  - a chat stored WITH refs on disk (pre-created via _store_blob) - the form
    real chats have after their first save
  - a missing blob -> exactly one placeholder (the loader's promise)
  - a legacy fully-inline chat -> stays inline (migration passthrough)
The old version sampled ../live/sessions unconditionally, so the user's
instance changes turned the suite red without a code change. The live
sampling is kept as an env opt-in (test_wedge_ui_toggle style):
  HIVEMIND_TEST_LIVE_SESSIONS=1  runs the same roundtrip against
  ../live/sessions (or the path given as argv[1] / HIVEMIND_LIVE_SESSIONS)
and skips with a notice when that folder does not exist.

Run: python tests/test_chat_real_roundtrip.py
"""
import copy
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


# ── fixture builders (exercise the production blobify thresholds) ────────────

def _big(s=600):
    return "x" * max(s, 401)          # >= _BLOB_MIN_CHARS (400)


def _crlf_big():
    # every \n here was a text-mode corruption candidate (2026-10-23 fix)
    return "line\r\n" * 80


def _msg_content(cid_hint, big=_big):
    return {"role": "assistant", "content": big()}


def build_fixtures(sessions: Path):
    """Write synthetic chats + blob payloads through the production code.
    Returns chat ids in a stable order."""
    import core.state as _state
    from routers import chats as C

    ids = []

    # 1. modern chat: big html/cp/image payloads (the blobified surfaces),
    #    CRLF payload inside cp.diffText (the text-mode corruption candidate),
    #    big plain content (stays inline - content is not blobified)
    cid = "0blobfeedface0001"
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": _big()},
        {"role": "assistant", "content": "crlf-payload",
         "cp": {"diffText": _crlf_big(), "files": ["a.py"]}},
        {"role": "assistant", "html": _big(),
         "cp": {"diffText": _big(), "text": _big()}},
        {"role": "assistant", "content": "pic",
         "images": [{"preview": _big()}]},
    ]
    _state._SESSIONS_DIR = sessions
    C._save_chat(cid, {"id": cid, "title": "blob fixture",
                       "messages": msgs, "created_at": "", "updated_at": ""})
    ids.append(cid)

    # 2. chat stored WITH refs on disk (the post-first-save disk form):
    #    pre-create the ref via _store_blob, ship the json with {"_ref": h}
    cid = "1prerefface000002"
    payload = _big(700)
    h = C._store_blob(cid, payload)
    disk = {"id": cid, "title": "pref ref fixture", "rev": 0,
            "created_at": "", "updated_at": "",
            "messages": [{"role": "assistant", "content": "restored below",
                          "html": {"_ref": h},
                          "cp": {"diffText": {"_ref": h}}}
                         ]}
    (sessions / f"chat_{cid}.json").write_text(
        json.dumps(disk), encoding="utf-8")
    ids.append(cid)

    # 3. missing blob: a cp ref whose payload file does not exist -> the
    #    loader must answer exactly one _BLOB_PLACEHOLDER (never a hang,
    #    never a crash)
    cid = "2missingface00003"
    disk = {"id": cid, "title": "missing blob fixture", "rev": 0,
            "created_at": "", "updated_at": "",
            "messages": [{"role": "assistant",
                          "cp": {"diffText": {"_ref": "deadbeefdeadbeef"}}}]}
    (sessions / f"chat_{cid}.json").write_text(
        json.dumps(disk), encoding="utf-8")
    ids.append(cid)

    # 4. legacy fully-inline chat: only short strings, nothing to blobify
    cid = "3legacyface000004"
    msgs = [{"role": "user", "content": "short"},
            {"role": "assistant", "content": "also short"}]
    _state._SESSIONS_DIR = sessions
    C._save_chat(cid, {"id": cid, "title": "legacy fixture",
                       "messages": msgs, "created_at": "", "updated_at": ""})
    ids.append(cid)
    return ids


def _roundtrip_one(sessions: Path, cid: str, disk_file: Path, expect_inline=False,
                   expect_placeholders=None):
    """The assertion block the live sampler used, on one prepared chat:
    restore(disk) has no placeholders, save -> load -> restore is cycle
    stable. expect_inline: saved file must stay ref-free (legacy chat)."""
    import core.state as _state
    from routers import chats as C

    data = json.loads(disk_file.read_text(encoding="utf-8"))
    disk_msgs = data.get("messages", [])

    r1 = copy.deepcopy(disk_msgs)
    C._restore_messages(cid, r1)
    placeholders = sum(
        1 for m in r1 if isinstance(m, dict)
        for v in [json.dumps(m)] if "[missing: stored content" in v)
    if expect_placeholders is not None:
        if placeholders == expect_placeholders:
            ok(f"{cid}: {placeholders} placeholder(s) as expected")
        else:
            fail(f"{cid}: expected {expect_placeholders} placeholder(s), got {placeholders}")
    elif placeholders:
        fail(f"{cid}: {placeholders} placeholder(s) in a healthy chat")
    else:
        ok(f"{cid}: restore clean ({len(r1)} msgs)")

    # cycle stability: save(R1) -> load -> restore == R1
    chat = {"id": cid, "title": data.get("title", ""),
            "messages": copy.deepcopy(r1),
            "created_at": data.get("created_at", ""),
            "updated_at": data.get("updated_at", "")}
    C._save_chat(cid, chat)
    disk2 = json.loads(next(
        p for p in sessions.glob(f"*_{cid}.json")
        if not p.name.endswith(".context.json")).read_text(encoding="utf-8"))
    r2 = copy.deepcopy(disk2["messages"])
    C._restore_messages(cid, r2)

    if r2 == r1:
        cp_n = sum(1 for m in r1 if isinstance(m, dict) and m.get("cp"))
        img_n = sum(1 for m in r1 if isinstance(m, dict) and m.get("images"))
        ok(f"{cid}: lossless roundtrip (cycle stable, {cp_n} snapshots, "
           f"{img_n} image msgs)")
    else:
        fail(f"{cid}: roundtrip differs")
        for i, (a, b) in enumerate(zip(r1, r2)):
            if a != b:
                print(f"    first diff in message {i}: "
                      f"{json.dumps(a)[:120]} vs {json.dumps(b)[:120]}")
                break

    if expect_inline:
        has_ref = "_ref" in json.dumps(disk2)
        if not has_ref:
            ok(f"{cid}: legacy chat stays inline (migration passthrough)")
        else:
            fail(f"{cid}: legacy chat grew refs")


def main():
    import core.state as _state
    from routers import chats as C
    import threading

    tmp = Path(tempfile.mkdtemp(prefix="hvm_chatreal_"))
    sessions = tmp / "sessions"
    sessions.mkdir()
    C._cache_lock = threading.Lock()  # normally created by server startup

    live_mode = False
    live_sessions = None
    if __import__("os").environ.get("HIVEMIND_TEST_LIVE_SESSIONS") == "1":
        live_sessions = Path(sys.argv[1]) if len(sys.argv) > 1 else \
            Path(__import__("os").environ.get("HIVEMIND_LIVE_SESSIONS")
                 or (Path(__file__).resolve().parents[2] / "live" / "sessions"))
        live_mode = live_sessions.is_dir()
        if not live_mode:
            print(f"  (skip) HIVEMIND_TEST_LIVE_SESSIONS=1 but no live "
                  f"sessions at {live_sessions}")

    try:
        # ── hermetic fixtures (the default, green without ../live) ──────────
        _state._SESSIONS_DIR = sessions
        C._cache_loaded = False
        C._chats_cache.clear()
        ids = build_fixtures(sessions)

        for cid in ids:
            C._cache_loaded = False
            C._chats_cache.clear()
            disk_file = next(p for p in sessions.glob(f"*_{cid}.json")
                             if not p.name.endswith(".context.json"))
            if cid.startswith("1preref"):
                _roundtrip_one(sessions, cid, disk_file)
            elif cid.startswith("2missing"):
                _roundtrip_one(sessions, cid, disk_file, expect_placeholders=1)
            elif cid.startswith("3legacy"):
                _roundtrip_one(sessions, cid, disk_file, expect_inline=True)
            else:
                _roundtrip_one(sessions, cid, disk_file)

        # ── live sampling (opt-in, the original assertion surface) ──────────
        if live_mode:
            chat_files = sorted(
                f for f in live_sessions.glob("*.json")
                if not f.name.endswith(".context.json"))
            if not chat_files:
                print("  (skip) no chat files found")
                return 0

            def _score(f: Path):
                try:
                    txt = f.read_text(encoding="utf-8")
                except (OSError, ValueError):
                    return (0, 0)
                return ('"cp"' in txt, '"images"' in txt), len(txt)

            chat_files.sort(key=lambda f: _score(f), reverse=True)
            sample = chat_files[:5]
            print(f"  sampling {len(sample)} of {len(chat_files)} live chats")

            for f in sample:
                data = json.loads(f.read_text(encoding="utf-8"))
                cid = data.get("id") or "realfake1"
                cid = cid if all(ch in "0123456789abcdef-" for ch in cid) else "realfake1"
                ws = tmp / ("live_" + cid)
                ws.mkdir()
                src_blobs = f.parent / f"{cid}.blobs"
                if src_blobs.is_dir():
                    shutil.copytree(src_blobs, ws / f"{cid}.blobs")
                _state._SESSIONS_DIR = ws
                C._cache_loaded = False
                C._chats_cache.clear()
                shutil.copy2(f, ws / f.name)
                disk_file = ws / f.name
                _roundtrip_one(ws, cid, disk_file)
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
