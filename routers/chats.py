"""Chats API-Router."""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import time as _time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.state import _chats_cache, _cache_lock, _cache_loaded
import core.state as _state
from context.chat import _load_chat_context
from utils.token import estimate_ctx_tokens

logger = logging.getLogger("hivemind.server")

router = APIRouter(prefix="/chats", tags=["Chats"])

# ── Content-addressed blob store (2026-10-03, chat growth fix) ───────────────
# Long assistant chats used to carry the FULL code-panel snapshot inside
# every message (quadratic growth), and image previews carried their whole
# dataURL per message. Both now live in a per-chat blob directory
# (sessions/<chat_id>.blobs/<sha256-16>) and the message holds only
# {"_ref": <hash>}. Backend-side on purpose: the frontend API is unchanged,
# old chat files (everything inline) load unchanged, and dedup within a
# chat collapses repeated snapshots for free.
#   Portability: a chat is no longer self-contained - copy the sessions/
#   folder as a whole (blob dirs travel with it), as backup_data.py does.
#   Missing blobs restore as visible placeholders, never as errors.
_BLOB_MIN_CHARS = 400          # shorter strings stay inline
_BLOB_PLACEHOLDER = "[missing: stored content not found]"
_IMG_PLACEHOLDER = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
                    "CAYAAAAfFcSJAAAADUlEQVR42mNsyK+BAAABHAEB/9S1AwAAAABJRU5"
                    "ErkJggg==")
_REF_KEYS = {"content", "diffText", "text", "preview"}


def _blob_dir(chat_id: str) -> Path:
    return _state._SESSIONS_DIR / f"{chat_id}.blobs"


def _blob_hash(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def _store_blob(chat_id: str, data: str) -> str:
    d = _blob_dir(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    h = _blob_hash(data)
    p = d / h
    if not p.exists():
        tmp = p.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, p)
    return h


def _extract_refs(chat_id: str, node) -> None:
    """Recursively replace big whitelisted strings with {"_ref": hash}."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, dict):
                if "_ref" not in v:
                    _extract_refs(chat_id, v)
            elif isinstance(v, list):
                for item in v:
                    _extract_refs(chat_id, item)
            elif (k in _REF_KEYS and isinstance(v, str)
                  and len(v) >= _BLOB_MIN_CHARS
                  and not v.startswith(_BLOB_PLACEHOLDER)):
                node[k] = {"_ref": _store_blob(chat_id, v)}


def _restore_refs(chat_id: str, node) -> None:
    """Inverse of _extract_refs; a missing blob becomes a placeholder."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, dict) and isinstance(v.get("_ref"), str):
                try:
                    node[k] = (_blob_dir(chat_id) / v["_ref"]).read_text(encoding="utf-8")
                except OSError:
                    node[k] = _IMG_PLACEHOLDER if k == "preview" else _BLOB_PLACEHOLDER
            elif isinstance(v, (dict, list)):
                _restore_refs(chat_id, v)
    elif isinstance(node, list):
        for item in node:
            _restore_refs(chat_id, item)


def _blobify_messages(chat_id: str, messages) -> None:
    """Save-side: extract big panel/image payloads into the blob store.
    Runs BEFORE the chat json is written, so a crash can only leave blobs
    without a json (cleaned by the startup sweep), never the reverse."""
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if isinstance(m.get("cp"), dict):
            _extract_refs(chat_id, m["cp"])
        imgs = m.get("images")
        if isinstance(imgs, list):
            for im in imgs:
                if (isinstance(im, dict) and isinstance(im.get("preview"), str)
                        and len(im["preview"]) >= _BLOB_MIN_CHARS
                        and "_ref" not in im):
                    im["preview"] = {"_ref": _store_blob(chat_id, im["preview"])}


def _restore_messages(chat_id: str, messages) -> None:
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if isinstance(m.get("cp"), dict):
            _restore_refs(chat_id, m["cp"])
        imgs = m.get("images")
        if isinstance(imgs, list):
            for im in imgs:
                if isinstance(im, dict) and isinstance(im.get("preview"), dict):
                    ref = im["preview"]
                    try:
                        im["preview"] = (_blob_dir(chat_id) / ref["_ref"]).read_text(encoding="utf-8")
                    except OSError:
                        im["preview"] = _IMG_PLACEHOLDER


def _sweep_orphan_blob_dirs() -> None:
    """GC (startup): blob dirs whose chat json no longer exists ON DISK.

    Filesystem check on purpose: deciding "orphan" from the in-memory
    cache would delete the blobs of a chat whose json merely FAILED to
    load (corrupt, half-written, locked) and is still recoverable. The
    1h minimum age keeps a just-created chat safe even if its json write
    is still in flight."""
    try:
        import time as _time
        _now = _time.time()
        for d in _state._SESSIONS_DIR.glob("*.blobs"):
            cid = d.name[:-len(".blobs")]
            if any(_state._SESSIONS_DIR.glob(f"*_{cid}.json")):
                continue
            if _now - d.stat().st_mtime < 3600:
                continue
            shutil.rmtree(d, ignore_errors=True)
            logger.info("[CHATS] removed orphan blob dir %s", d.name)
    except OSError as _sweep_err:
        logger.debug("[CHATS] blob dir sweep failed: %s", _sweep_err)

_RE_CHAT_ID_PATTERN = re.compile(r"_([a-f0-9\-]{4,36})\.json$")
_chat_save_locks: dict = {}
_chat_save_locks_guard = threading.Lock()
_re_slug_special = re.compile(r"[^\w\s-]")
_re_slug_space = re.compile(r"[\s_]+")


def _user_msg_count(msgs) -> int:
    """Count only the messages the USER sent (replies/intermediary agent
    bubbles do not count — the counter shows the user's own messages)."""
    if not msgs:
        return 0
    return sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "user")


def _chat_tokens(msgs) -> int:
    try:
        return int(estimate_ctx_tokens(msgs or []))
    except Exception:
        return 0


def _ensure_cache():
    global _cache_loaded
    if _state._SESSIONS_DIR is None:
        return
    with _cache_lock:
        if _cache_loaded:
            return
        _META = {"id", "title", "created_at", "updated_at"}
        result = {}
        for entry in sorted(_state._SESSIONS_DIR.iterdir()):
            if not entry.name.endswith(".json"):
                continue
            m = _RE_CHAT_ID_PATTERN.search(entry.name)
            if not m:
                continue
            cid = m.group(1)
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
                meta = {k: v for k, v in data.items() if k in _META}
                meta["_file"] = str(entry)
                _msgs = data.get("messages", [])
                meta["_msg_count"] = _user_msg_count(_msgs)
                meta["_tokens"] = _chat_tokens(_msgs)
                _last = _msgs[-1].get("content", "") if _msgs else ""
                meta["_preview"] = _last[:80] if isinstance(_last, str) else ""
                result[cid] = meta
            except Exception:
                pass
        _chats_cache.clear()
        _chats_cache.update(result)
        _cache_loaded = True
        _sweep_orphan_blob_dirs()


def _load_chat_full(chat_id: str) -> dict | None:
    with _cache_lock:
        meta = _chats_cache.get(chat_id)
    if not meta:
        return None
    _file = meta.get("_file", "")
    if not _file:
        return None
    try:
        data = json.loads(Path(_file).read_text(encoding="utf-8"))
        data["_file"] = _file
        return data
    except Exception:
        return None


def _get_chat_save_lock(chat_id: str) -> threading.Lock:
    cid = str(chat_id or "")
    with _chat_save_locks_guard:
        if cid not in _chat_save_locks:
            _chat_save_locks[cid] = threading.Lock()
        return _chat_save_locks[cid]


def _merge_run_messages(incoming: list, stored: list, base_rev: int | None) -> list:
    """Union the frontend's decorated messages with backend-appended ones
    (S4a), with resurrect protection (2026-10-03 review): a run message
    only survives when it was created AFTER the rev the caller's payload
    is based on (created_rev > base_rev). A payload on an OLDER rev that
    drops the message means a deliberate frontend deletion (regenerate,
    edit mid-chat, message delete) - the server must not resurrect it.
    For matching ids the SERVER text wins (the run streamed it), the
    frontend version donates html/cp/images. Incoming payloads without
    ids (old cached JS) pass through untouched."""
    _run_ids = {m["id"]: m for m in stored
                if isinstance(m, dict) and m.get("id") and m.get("source") == "run"}
    if not _run_ids:
        return incoming
    _has_ids = any(isinstance(m, dict) and m.get("id") for m in incoming)
    if not _has_ids:
        return incoming
    _out = []
    _seen = set()
    for m in incoming:
        if not isinstance(m, dict):
            _out.append(m)
            continue
        mid = m.get("id")
        prev = _run_ids.get(mid)
        if prev is not None:
            _seen.add(mid)
            merged = dict(m)
            merged["content"] = prev.get("content", m.get("content", ""))
            if "interrupted" in prev:
                merged["interrupted"] = prev.get("interrupted")
            _out.append(merged)
        else:
            _out.append(m)
    # RESURRECT GUARD: only run messages CREATED after the caller's base
    # rev are appended back. base_rev=None (beacon LWW, pre-rev callers)
    # keeps the old append-everything behavior - an old beacon cannot be
    # judged against a rev it never knew.
    for mid, prev in _run_ids.items():
        if mid in _seen:
            continue
        _created = int(prev.get("created_rev") or 0)
        if base_rev is None or _created > base_rev:
            _out.append(prev)
    return _out


def _save_chat(chat_id: str, chat: dict, base_rev: int | None = None,
               bump_rev: bool = True) -> dict | None:
    """Atomic save on an internal COPY (2026-10-03, review fix): the
    caller's chat dict keeps its inline payloads and is never mutated
    with blob refs - POST /chats used to hand the blobified payload back.

    Returns {"id", "rev", "ts", "conflict"}.

    base_rev enables compare-and-set: None = legacy last-writer-wins
    (old cached JS, the pagehide beacon); a number must match the stored
    rev or NOTHING is written and conflict=True comes back - the server
    state wins, the caller adopts it. Chats without a rev count as 0."""
    with _get_chat_save_lock(chat_id):
        # CAS against the DISK rev, never the caller's dict - the caller
        # may carry a stale or absent rev (dicts built inline start at 0)
        _cur_rev = get_chat_rev(chat_id)
        if base_rev is not None and base_rev != _cur_rev:
            return {"id": chat_id, "rev": _cur_rev, "conflict": True,
                    "ts": chat.get("updated_at", "")}
        # MERGE-ON-SAVE (S4a): union the caller's decorated messages with
        # backend-appended run messages before taking the copy. Runs on
        # EVERY save (not only CAS callers) - the beacon path must adopt
        # backend messages just the same. Legacy payloads without ids
        # pass through untouched.
        try:
            _stored_prev = _read_chat_file(chat_id)
            if _stored_prev and _stored_prev.get("messages"):
                chat["messages"] = _merge_run_messages(
                    chat.get("messages", []), _stored_prev["messages"],
                    base_rev)
        except (OSError, ValueError):
            pass
        stored = copy.deepcopy(chat)
        _new_rev = _cur_rev + (1 if bump_rev else 0)
        stored["rev"] = _new_rev

        if not _state._SESSIONS_DIR:
            return None
        stored["_file"] = str(_canonical_chat_file(chat_id))
        _write_stored(chat_id, stored)
        # adopt-and-remove: a legacy-named file is superseded the moment
        # the canonical one holds the newer state
        for _leg in _legacy_chat_files(chat_id):
            try:
                _dest = _state._SESSIONS_DIR / ".legacy" / _leg.name
                _dest.parent.mkdir(exist_ok=True)
                os.replace(_leg, _dest)
            except OSError:
                pass
        return {"id": chat_id, "rev": _new_rev, "ts": stored.get("updated_at", ""),
                "conflict": False}


def import_dom_messages(chat_id: str, messages: list,
                        workspace: str | None = None) -> dict | None:
    """Backend-side truth import (2026-10-03): when the frontend flush
    failed, /stream carries the DOM messages; the server overwrites the
    chat with them (rev bumped, blobs extracted) so the run seeds fresh
    state instead of the stale pre-edit json."""
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    if not msgs:
        return None
    chat = _load_chat_full(chat_id) or {
        "id": chat_id, "title": "Chat",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": "", "messages": [],
    }
    chat["messages"] = msgs
    if workspace:
        chat["workspace"] = str(workspace)
    chat["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return _save_chat(chat_id, chat)


def _write_stored(chat_id: str, stored: dict) -> None:
    """Cache meta + blob extraction + atomic json write for a prepared
    stored dict (shared by _save_chat and upsert_run_message)."""
    # TRIPWIRE (2026-10-03 review): the single write funnel reports any
    # rev sink or message disappearance WITH the culprit stacktrace,
    # instead of the bug being reconstructed from the aftermath.
    import traceback as _tb
    _p_check = Path(str(stored.get("_file") or ""))
    try:
        if _p_check.exists():
            _prev = json.loads(_p_check.read_text(encoding="utf-8"))
            _prev_rev = int(_prev.get("rev") or 0)
            _new_rev = int(stored.get("rev") or 0)
            if _new_rev < _prev_rev:
                logger.error("[CHATS-TRIPWIRE] rev SINK %d -> %d (chat=%s)\n%s",
                             _prev_rev, _new_rev, chat_id, "".join(_tb.format_stack()))
            _prev_ids = {m.get("id") for m in (_prev.get("messages") or [])
                         if isinstance(m, dict) and m.get("id")}
            _new_ids = {m.get("id") for m in (stored.get("messages") or [])
                        if isinstance(m, dict) and m.get("id")}
            _vanished = _prev_ids - _new_ids
            if _vanished:
                logger.error("[CHATS-TRIPWIRE] message ids vanished %s (chat=%s)\n%s",
                             sorted(_vanished), chat_id, "".join(_tb.format_stack()))
    except (OSError, ValueError):
        pass
    _msgs = stored.get("messages", [])
    _last = _msgs[-1].get("content", "") if _msgs else ""
    with _cache_lock:
        _chats_cache[chat_id] = {
            "id": stored.get("id", chat_id),
            "title": stored.get("title", "Untitled"),
            "created_at": stored.get("created_at", ""),
            "updated_at": stored.get("updated_at", ""),
            "_msg_count": _user_msg_count(_msgs),
            "_tokens": _chat_tokens(_msgs),
            "_preview": _last[:80] if isinstance(_last, str) else "",
            "_file": str(stored.get("_file") or ""),
        }
    try:
        _blobify_messages(chat_id, stored.get("messages", []))
    except OSError as _blob_err:
        logger.warning("[CHATS] blob extraction failed, saving inline: %s",
                       _blob_err)
    _out = {k: v for k, v in stored.items() if not k.startswith("_")}
    _p = Path(str(stored.get("_file") or ""))
    _tmp = _p.with_suffix(".tmp")
    _tmp.write_text(json.dumps(_out, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    _replace_with_retry(_tmp, _p)


def upsert_run_message(chat_id: str, msg_id: str, user_msg_id: str,
                       content: str, agent: str = "Assistant",
                       interrupted: bool | None = None,
                       bump_rev: bool = False) -> dict | None:
    """S4a: the backend owns one assistant message per run. Checkpoints
    refresh it IN PLACE without bumping rev (same writer - no concurrency
    claim); the final flush bumps rev so the frontend's next save adopts
    via base_rev. Inserts right after the user message it answers."""
    with _get_chat_save_lock(chat_id):
        chat = _read_chat_file(chat_id)
        if chat is None:
            return None
        stored = copy.deepcopy(chat)
        _msgs = stored.get("messages", [])
        existing = next((m for m in _msgs if m.get("id") == msg_id), None)
        _new_rev = int(stored.get("rev") or 0) + (1 if bump_rev else 0)
        stored["rev"] = _new_rev
        if existing is not None:
            existing["content"] = content
            if interrupted is not None:
                existing["interrupted"] = interrupted
            if bump_rev:
                # the finalize rev is the message's creation stamp: merges
                # compare created_rev against the frontend's base_rev
                existing["created_rev"] = _new_rev
        else:
            new_msg = {"id": msg_id, "role": "assistant", "agent": agent,
                       "content": content, "source": "run",
                       "reply_to": user_msg_id or None,
                       "created_rev": _new_rev}
            if interrupted is not None:
                new_msg["interrupted"] = interrupted
            pos = next((i + 1 for i, m in enumerate(_msgs)
                        if m.get("id") == user_msg_id), len(_msgs))
            _msgs.insert(pos, new_msg)
        stored["updated_at"] = datetime.now().isoformat(timespec="seconds")
        stored["_file"] = str(_canonical_chat_file(chat_id))
        _write_stored(chat_id, stored)
        for _leg in _legacy_chat_files(chat_id):
            try:
                _dest = _state._SESSIONS_DIR / ".legacy" / _leg.name
                _dest.parent.mkdir(exist_ok=True)
                os.replace(_leg, _dest)
            except OSError:
                pass
        return {"id": chat_id, "rev": _new_rev, "msg_id": msg_id,
                "conflict": False}


class RunTranscriptWriter:
    """S4a: accumulate a run's assistant text and write it into the chat
    transcript under a stable message id. Checkpoints refresh the message
    IN PLACE without bumping rev (same writer, no concurrency claim); the
    final flush bumps rev once so the frontend's next save adopts via
    base_rev. Not attached when the chat has no json (chat-less runs)."""
    CHECKPOINT_S = 10.0

    def __init__(self, chat_id: str, user_msg_id: str, agent: str = "Assistant"):
        self.chat_id = str(chat_id)
        self.user_msg_id = str(user_msg_id or "")
        self.agent = agent or "Assistant"
        self.msg_id = uuid.uuid4().hex[:12]
        self._parts: list = []
        self._last_ckpt = 0.0
        self.finalized = False
        self.ok = _read_chat_file(self.chat_id) is not None

    def add_token(self, text: str) -> None:
        if not self.ok or self.finalized or not text:
            return
        self._parts.append(text)
        _now = _time.time()
        if _now - self._last_ckpt >= self.CHECKPOINT_S:
            self.checkpoint()

    def checkpoint(self) -> None:
        if not self.ok or self.finalized:
            return
        self._last_ckpt = _time.time()
        try:
            upsert_run_message(self.chat_id, self.msg_id, self.user_msg_id,
                               "".join(self._parts), self.agent,
                               interrupted=False, bump_rev=False)
        except (OSError, ValueError) as _ck_err:
            logger.warning("[CHATS] run checkpoint failed (chat=%s): %s",
                           self.chat_id, _ck_err)

    def finalize(self, interrupted: bool | None = None) -> dict | None:
        if not self.ok or self.finalized:
            return None
        self.finalized = True
        try:
            return upsert_run_message(self.chat_id, self.msg_id,
                                      self.user_msg_id, "".join(self._parts),
                                      self.agent, interrupted=interrupted,
                                      bump_rev=True)
        except (OSError, ValueError) as _fin_err:
            logger.warning("[CHATS] run finalize failed (chat=%s): %s",
                           self.chat_id, _fin_err)
            return None


def _canonical_chat_file(chat_id: str) -> Path:
    """ONE file per chat, derived from the id alone (2026-10-03 review
    fix). The old generated names (timestamp + title + id) forked the
    chat whenever a writer lost the _file path: title-changing PUTs each
    created a new file and sorted[-1] readers and the cache's _file
    disagreed about which one was current - two truths, again. The name
    still matches _RE_CHAT_ID_PATTERN, so list_chats keeps working."""
    return _state._SESSIONS_DIR / f"chat_{chat_id}.json"


def _legacy_chat_files(chat_id: str) -> list:
    return [p for p in _state._SESSIONS_DIR.glob(f"*_{chat_id}.json")
            if p.name != f"chat_{chat_id}.json"]


def _resolve_chat_file(chat_id: str) -> Path | None:
    c = _canonical_chat_file(chat_id)
    if c.exists():
        return c
    legs = _legacy_chat_files(chat_id)
    if not legs:
        return None
    return max(legs, key=lambda p: p.stat().st_mtime)


def _replace_with_retry(src: Path, dst: Path) -> None:
    """os.replace can hit PermissionError on Windows while a reader (GET
    /chats/{id}, a checkpoint, an antivirus scan) still holds the target
    open. 4 retries with backoff, then fail LOUDLY - a silently dropped
    write would lose the user's chat."""
    _last: Exception | None = None
    for delay in (0.02, 0.05, 0.1, 0.2):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            _last = e
            time.sleep(delay)
    raise _last if _last else OSError(f"replace failed: {src} -> {dst}")


def _read_chat_file(chat_id: str) -> dict | None:
    """Cache-independent read of the chat json (the run path may start
    before any /chats endpoint warmed the meta cache)."""
    try:
        if not _state._SESSIONS_DIR:
            return None
        f = _resolve_chat_file(chat_id)
        if f is None:
            return None
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def get_chat_rev(chat_id: str) -> int:
    return int((_read_chat_file(chat_id) or {}).get("rev") or 0)


@router.get("")
async def list_chats():
    await asyncio.to_thread(_ensure_cache)
    result = []
    def _get_items():
        with _cache_lock:
            return list(_chats_cache.items())
    items = await asyncio.to_thread(_get_items)
    for cid, c in items:
        result.append({
            "id": cid,
            "title": c.get("title", "Untitled"),
            "created_at": c.get("created_at", ""),
            "updated_at": c.get("updated_at", ""),
            "msg_count": c.get("_msg_count", 0),
            "tokens": c.get("_tokens", 0),
            "preview": c.get("_preview", ""),
        })
    # INTERRUPTED-FLAG (2026-08-21): letzter Run via Browser-Close parkiert?
    def _is_interrupted(cid: str) -> bool:
        try:
            _ctx = _load_chat_context(cid)
            return bool(
                isinstance(_ctx, dict)
                and isinstance(_ctx.get("last_run"), dict)
                and _ctx["last_run"].get("stop_reason") == "disconnect"
            )
        except Exception:
            return False
    for _r in result:
        _r["interrupted"] = await asyncio.to_thread(_is_interrupted, _r["id"])
    result.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"chats": result}


@router.post("")
async def create_chat(req: Request):
    await asyncio.to_thread(_ensure_cache)
    data = await req.json()
    cid = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat(timespec="seconds")
    chat = {
        "id": cid,
        "title": data.get("title", "New Chat"),
        "created_at": now,
        "updated_at": now,
        "messages": data.get("messages", []),
    }
    # WORKSPACE PER CHAT (2026-10-03): stored with the chat so a reload
    # cannot silently fall back to the global workspace (audit finding).
    if data.get("workspace"):
        chat["workspace"] = str(data["workspace"])
    _msgs = chat["messages"]
    _last = _msgs[-1].get("content", "") if _msgs else ""
    chat["_msg_count"] = _user_msg_count(_msgs)
    chat["_tokens"] = _chat_tokens(_msgs)
    chat["_preview"] = _last[:80] if isinstance(_last, str) else ""
    _saved = await asyncio.to_thread(_save_chat, cid, chat)
    return {"ok": True, "id": cid, "rev": (_saved or {}).get("rev"),
            "chat": {k: v for k, v in chat.items() if k != "_file"}}


@router.post("/persist")
async def persist_chat(req: Request):
    """Create-or-update a chat in one call. Used by the auto-save feature
    (stream end, abort) and the browser-close beacon (POST-only)."""
    await asyncio.to_thread(_ensure_cache)
    data = await req.json()
    msgs = data.get("messages", []) or []
    if not msgs:
        return {"ok": True, "id": data.get("chat_id"), "updated": False}
    now = datetime.now().isoformat(timespec="seconds")
    cid = data.get("chat_id") or None

    def _try_load(_cid):
        return _load_chat_full(_cid)

    if cid:
        chat = await asyncio.to_thread(_try_load, cid)
        if chat is not None:
            def _apply_update():
                with _cache_lock:
                    chat["messages"] = msgs
                    if data.get("title"):
                        chat["title"] = data["title"]
                    if data.get("workspace"):
                        chat["workspace"] = str(data["workspace"])
                    chat["updated_at"] = now
                    chat["_msg_count"] = _user_msg_count(msgs)
                    chat["_tokens"] = _chat_tokens(msgs)
                    _last = msgs[-1].get("content", "") if msgs else ""
                    chat["_preview"] = _last[:80] if isinstance(_last, str) else ""
            await asyncio.to_thread(_apply_update)
            _saved = await asyncio.to_thread(_save_chat, cid, chat,
                                             data.get("base_rev"))
            if _saved and _saved.get("conflict"):
                return JSONResponse({"error": "stale_rev",
                                     "rev": _saved.get("rev")},
                                    status_code=409)
            return {"ok": True, "id": cid, "rev": (_saved or {}).get("rev"),
                    "created": False, "updated": True}

    # Create (reuse given chat_id so beacon retries keep a stable file).
    if not cid:
        cid = str(uuid.uuid4())[:8]
    title = data.get("title") or "New Chat"
    chat = {
        "id": cid,
        "title": title,
        "created_at": now,
        "updated_at": now,
        "messages": msgs,
    }
    if data.get("workspace"):
        chat["workspace"] = str(data["workspace"])
    _last = msgs[-1].get("content", "") if msgs else ""
    chat["_msg_count"] = _user_msg_count(msgs)
    chat["_tokens"] = _chat_tokens(msgs)
    chat["_preview"] = _last[:80] if isinstance(_last, str) else ""
    _saved = await asyncio.to_thread(_save_chat, cid, chat)
    return {"ok": True, "id": cid, "rev": (_saved or {}).get("rev"),
            "created": True, "updated": False}


@router.get("/{chat_id}")
async def get_chat(chat_id: str):
    await asyncio.to_thread(_ensure_cache)
    chat = await asyncio.to_thread(_load_chat_full, chat_id)
    if chat is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    _restore_messages(chat_id, chat.get("messages", []))
    result = {k: v for k, v in chat.items() if k.startswith("_") is False}
    # S4a: the frontend tracks base_rev from this value (done-event + here)
    result["rev"] = int((chat.get("rev") or 0))
    # INTERRUPTED (2026-08-21): last_run (stop_reason="disconnect") mitliefern,
    try:
        _ctx = _load_chat_context(chat_id)
        if isinstance(_ctx, dict):
            # json workspace wins; the sidecar key only fills older chats
            if not result.get("workspace") and _ctx.get("workspace"):
                result["workspace"] = _ctx["workspace"]
            if isinstance(_ctx.get("last_run"), dict):
                result["last_run"] = _ctx["last_run"]
    except Exception:
        pass
    return result


@router.put("/{chat_id}")
async def update_chat(chat_id: str, req: Request):
    await asyncio.to_thread(_ensure_cache)
    data = await req.json()
    now = datetime.now().isoformat(timespec="seconds")

    # ATOMIC UPDATE (2026-10-03 review fix): load, mutate and save under
    # the SAME per-chat lock the run checkpoints use. The old shape
    # (load outside, mutate under _cache_lock, save later) let a
    # checkpoint slip between load and save - the PUT then compared
    # against a stale rev and could overwrite a fresh checkpoint.
    def _atomic_update():
        with _get_chat_save_lock(chat_id):
            chat = _read_chat_file(chat_id)
            if chat is None:
                return None
            stored = copy.deepcopy(chat)
            _stored_rev = int(stored.get("rev") or 0)
            _base = data.get("base_rev")
            if _base is not None and _base != _stored_rev:
                return {"conflict": True, "rev": _stored_rev}
            # MERGE-ON-SAVE (S4a): same rule as _save_chat - the incoming
            # payload is the caller's decorated view, the disk copy holds
            # backend run messages; result REPLACES the payload.
            _inc = data.get("messages")
            if _inc is not None:
                try:
                    _inc = _merge_run_messages(
                        _inc, stored.get("messages", []), data.get("base_rev"))
                except (OSError, ValueError):
                    pass
                stored["messages"] = _inc
            if "title" in data:
                stored["title"] = data["title"]
            if "workspace" in data:
                stored["workspace"] = str(data["workspace"] or "")
            _msgs = stored.get("messages", [])
            stored["_msg_count"] = _user_msg_count(_msgs)
            stored["_tokens"] = _chat_tokens(_msgs)
            _last = _msgs[-1].get("content", "") if _msgs else ""
            stored["_preview"] = _last[:80] if isinstance(_last, str) else ""
            stored["updated_at"] = now
            stored["_file"] = str(_canonical_chat_file(chat_id))
            _new_rev = _stored_rev + 1
            stored["rev"] = _new_rev
            _write_stored(chat_id, stored)
            for _leg in _legacy_chat_files(chat_id):
                try:
                    _leg.unlink()
                except OSError:
                    pass
            return {"conflict": False, "rev": _new_rev}

    _saved = await asyncio.to_thread(_atomic_update)
    if _saved is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    if _saved.get("conflict"):
        _server = await asyncio.to_thread(_read_chat_file, chat_id)
        _server_msgs = copy.deepcopy((_server or {}).get("messages", []))
        await asyncio.to_thread(_restore_messages, chat_id, _server_msgs)
        return JSONResponse({"error": "stale_rev", "rev": _saved.get("rev"),
                             "messages": _server_msgs,
                             "hint": "server state wins - adopt these messages"},
                            status_code=409)
    return {"ok": True, "rev": _saved.get("rev")}


@router.delete("/{chat_id}")
async def delete_chat(chat_id: str):
    await asyncio.to_thread(_ensure_cache)
    def _pop_chat():
        with _cache_lock:
            return _chats_cache.pop(chat_id, None)
    chat = await asyncio.to_thread(_pop_chat)
    if chat:
        try:
            p = Path(chat.get("_file", "")) if chat.get("_file") else None
            if p and p.exists():
                p.unlink()
            if p:
                ctx_p = p.with_suffix(".context.json")
                if ctx_p.exists():
                    ctx_p.unlink()
            shutil.rmtree(_blob_dir(chat_id), ignore_errors=True)
        except Exception:
            pass
    return {"ok": True}
