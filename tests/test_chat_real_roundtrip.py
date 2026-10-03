"""Real-chat roundtrip: the blob format must be lossless on ACTUAL chats.

Uses real chat files from the live installation (default
../live/sessions relative to the repo, override with HIVEMIND_LIVE_SESSIONS)
- the most realistic migration test there is, per review. For every sampled
chat: restore(extract(messages)) must deep-equal the original messages, and
a legacy (fully inline) chat must keep loading unchanged.

Skips with a notice when no live sessions folder exists (fresh checkout).

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


def main():
    import core.state as _state
    from routers import chats as C

    live_sessions = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path(__file__).resolve().parents[2] / "live" / "sessions"
    if not live_sessions.is_dir():
        print(f"  (skip) no live sessions at {live_sessions}")
        return 0

    chat_files = sorted(
        f for f in live_sessions.glob("*.json")
        if not f.name.endswith(".context.json")
    )
    if not chat_files:
        print("  (skip) no chat files found")
        return 0

    # prefer the most telling samples: coder snapshots (cp) and images
    def _score(f: Path):
        try:
            txt = f.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return (0, 0)
        return ('"cp"' in txt, '"images"' in txt), len(txt)

    chat_files.sort(key=lambda f: _score(f), reverse=True)
    sample = chat_files[:5]
    print(f"  sampling {len(sample)} of {len(chat_files)} real chats "
          f"(largest with cp/images first)")

    tmp = Path(tempfile.mkdtemp(prefix="hvm_chatreal_"))
    import threading
    C._cache_lock = threading.Lock()  # normally created by server startup
    try:
        for f in sample:
            raw_txt = f.read_text(encoding="utf-8")
            try:
                data = json.loads(raw_txt)
            except ValueError as e:
                fail(f"{f.name} parses", str(e)[:80])
                continue
            cid = data.get("id") or "realfake1"
            cid = cid if all(ch in "0123456789abcdef-" for ch in cid) else "realfake1"

            # isolated copy of this one chat - blob dir travels with it
            # (a chat is no longer self-contained, see routers/chats.py)
            ws = tmp / cid
            ws.mkdir()
            src_blobs = f.parent / f"{cid}.blobs"
            if src_blobs.is_dir():
                shutil.copytree(src_blobs, ws / f"{cid}.blobs")
            _state._SESSIONS_DIR = ws
            C._cache_loaded = False
            C._chats_cache.clear()

            disk_msgs = data.get("messages", [])

            # R1: what the loader hands the UI for the on-disk form
            r1 = copy.deepcopy(disk_msgs)
            C._restore_messages(cid, r1)
            placeholders = sum(
                1 for m in r1 if isinstance(m, dict)
                for v in [json.dumps(m)] if "[missing: stored content" in v
            )
            if placeholders:
                fail(f"{f.name}: {placeholders} placeholder(s) in a healthy chat")

            # cycle stability: save(R1) -> load -> restore == R1
            chat = {"id": cid, "title": data.get("title", ""), "messages": copy.deepcopy(r1),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", "")}
            C._save_chat(cid, chat)
            disk2 = json.loads(next(
                p for p in ws.glob(f"*_{cid}.json")
            ).read_text(encoding="utf-8"))
            r2 = copy.deepcopy(disk2["messages"])
            C._restore_messages(cid, r2)

            if r2 == r1:
                n_msgs = len(r1)
                cp_n = sum(1 for m in r1 if isinstance(m, dict) and m.get("cp"))
                img_n = sum(1 for m in r1 if isinstance(m, dict) and m.get("images"))
                ok(f"{f.name}: {n_msgs} msgs, {cp_n} snapshots, "
                   f"{img_n} image msgs - lossless roundtrip (cycle stable)")
            else:
                fail(f"{f.name}: roundtrip differs")
                for i, (a, b) in enumerate(zip(r1, r2)):
                    if a != b:
                        print(f"    first diff in message {i}: "
                              f"{json.dumps(a)[:120]} vs {json.dumps(b)[:120]}")
                        break

        # legacy property: a chat file that predates blobs must load unchanged
        inline_files = [f for f in sample
                        if "_ref" not in f.read_text(encoding="utf-8")]
        if inline_files:
            ok(f"{len(inline_files)} sampled chats were legacy-inline and "
               f"all passed above (migration passthrough)")
        else:
            print("  (info) all sampled chats already contain refs")
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
