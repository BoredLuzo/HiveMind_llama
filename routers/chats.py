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
_CHAT_JSON_LIMIT = 200 * 1024 * 1024  # 200 MB emergency brake per save
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


def _get_chat_save_lock(chat_id: str) -> threading.RLock:
    cid = str(chat_id or "")
    with _chat_save_locks_guard:
        if cid not in _chat_save_locks:
            # RLock: update_chat wraps load+mutate+save in ONE lock
            # section and calls _save_chat (same lock) inside it.
            _chat_save_locks[cid] = threading.RLock()
        return _chat_save_locks[cid]


def _replace_with_retry(src: Path, dst: Path) -> None:
    """os.replace can hit PermissionError on Windows while a reader (GET
    /chats/{id}, a checkpoint, an antivirus scan) still holds the target
    open - a real failure observed in the concurrency tests. 4 retries
    with backoff, then fail LOUDLY: a silently dropped write would lose
    the chat."""
    for _delay in (0.02, 0.05, 0.1, 0.2):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(_delay)
    os.replace(src, dst)


def _read_chat_file(chat_id: str) -> dict | None:
    """Cache-independent read (2026-10-03 review fix): CAS must compare
    against the DISK rev - the caller's dict may carry a stale or absent
    rev (dicts built inline start at 0)."""
    try:
        if not _state._SESSIONS_DIR:
            return None
        matches = sorted(_state._SESSIONS_DIR.glob(f"*_{chat_id}.json"))
        if not matches:
            return None
        return json.loads(matches[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def get_chat_rev(chat_id: str) -> int:
    return int((_read_chat_file(chat_id) or {}).get("rev") or 0)


def _save_chat(chat_id: str, chat: dict, base_rev: int | None = None) -> dict | None:
    """Atomic save on an internal COPY (2026-10-03, review fix): the
    caller's chat dict keeps its inline payloads and is never mutated
    with blob refs - POST /chats used to hand the blobified payload back.

    Returns {"id", "rev", "ts", "conflict"}.

    base_rev enables compare-and-set: None = legacy last-writer-wins
    (old cached JS, the pagehide beacon); a number must match the stored
    rev or NOTHING is written and conflict=True comes back - the server
    state wins, the caller adopts it. Chats without a rev count as 0."""
    with _get_chat_save_lock(chat_id):
        _cur_rev = get_chat_rev(chat_id)
        if base_rev is not None and base_rev != _cur_rev:
            return {"id": chat_id, "rev": _cur_rev, "conflict": True,
                    "ts": chat.get("updated_at", "")}
        stored = copy.deepcopy(chat)
        _new_rev = _cur_rev + 1
        stored["rev"] = _new_rev

        _file = str(stored.get("_file") or "")
        if not _file and _state._SESSIONS_DIR:
            # FORK GUARD (2026-10-04 review): a caller without the cache's
            # _file (cold cache, direct writer) used to GENERATE a name from
            # created_at + TITLE - a title change then created a second json
            # and readers split between the two. Adopt the EXISTING file for
            # this chat id first; only a genuinely new chat generates a name.
            _existing = sorted(_state._SESSIONS_DIR.glob(f"*_{chat_id}.json"))
            if _existing:
                # ADOPTION RULE (2026-10-04): duplicates resolve by HIGHEST
                # rev, never by name - adoption must not overwrite newer
                # state with an older fork leftover.
                _best, _best_rev = _existing[0], -1
                for _cand in _existing:
                    try:
                        _rv = int(json.loads(_cand.read_text(encoding="utf-8")).get("rev") or 0)
                    except (OSError, ValueError):
                        _rv = -1
                    if _rv > _best_rev:
                        _best, _best_rev = _cand, _rv
                _file = str(_best)
            else:
                _ts = str(stored.get("created_at") or
                          datetime.now().isoformat(timespec="seconds"))
                _ts = _ts.replace(":", "-")[:19]
                _title = stored.get("title", "Chat")
                _slg = _re_slug_special.sub("", _title.split("\n")[0][:40])
                _slg = _re_slug_space.sub("-", _slg.strip()) or "Chat"
                _file = str(_state._SESSIONS_DIR / f"{_ts}_{_slg}_{chat_id}.json")
            stored["_file"] = _file
        if not _file:
            return None

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
                "_file": _file,
            }

        # BLOB EXTRACTION (2026-10-03): after the meta block counted
        # tokens/previews on the INLINE payloads, shrink the stored form.
        try:
            _blobify_messages(chat_id, stored.get("messages", []))
        except OSError as _blob_err:
            logger.warning("[CHATS] blob extraction failed, saving inline: %s",
                           _blob_err)

        _out = {k: v for k, v in stored.items() if not k.startswith("_")}
        _txt = json.dumps(_out, indent=2, ensure_ascii=False)
        if len(_txt) > _CHAT_JSON_LIMIT:
            logger.error("[CHATS] save refused: chat %s serialized to %.1f MB "
                         "(limit %d MB) - a runaway run? Nothing was written.",
                         chat_id, len(_txt) / 1e6, _CHAT_JSON_LIMIT // (1024 * 1024))
            return {"id": chat_id, "rev": _cur_rev, "ts": "",
                    "conflict": False, "oversize": True}
        _p = Path(_file)
        _tmp = _p.with_suffix(".tmp")
        _tmp.write_text(_txt, encoding="utf-8")
        _replace_with_retry(_tmp, _p)
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


@router.get("")
async def list_chats():
    _ensure_cache()
    result = []
    def _get_items():
        with _cache_lock:
            return list(_chats_cache.items())
    items = await asyncio.to_thread(_get_items)
    def _disk_bytes(cid: str, meta_file: str) -> int:
        """CHAT SIZE (2026-10-05, user): main json + this chat's blob dir."""
        total = 0
        try:
            if meta_file and os.path.isfile(meta_file):
                total += os.path.getsize(meta_file)
            bdir = os.path.join(os.path.dirname(meta_file or ""), f"*_{cid}.blobs")
            import glob as _glob
            for bf in _glob.glob(bdir):
                for root, _dirs, files in os.walk(bf):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
        except OSError:
            pass
        return total
    for cid, c in items:
        result.append({
            "id": cid,
            "title": c.get("title", "Untitled"),
            "created_at": c.get("created_at", ""),
            "updated_at": c.get("updated_at", ""),
            "msg_count": c.get("_msg_count", 0),
            "tokens": c.get("_tokens", 0),
            "preview": c.get("_preview", ""),
            "bytes": _disk_bytes(cid, c.get("_file", "")),
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
    _ensure_cache()
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
    _saved = _save_chat(cid, chat)
    return {"ok": True, "id": cid, "rev": (_saved or {}).get("rev"),
            "chat": {k: v for k, v in chat.items() if k != "_file"}}


@router.post("/persist")
async def persist_chat(req: Request):
    """Create-or-update a chat in one call. Used by the auto-save feature
    (stream end, abort) and the browser-close beacon (POST-only)."""
    _ensure_cache()
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


@router.get("/search")
async def search_chats(q: str = "", limit: int = 20):
    """SEARCHBAR PHASE 1 (2026-10-04): case-insensitive substring search over
    chat titles AND message contents. Reads the session jsons in a thread
    (the meta cache has no message bodies); returns id/title/updated_at plus
    a snippet around the first message match. Registered BEFORE /{chat_id}
    so the path router does not swallow it."""
    _q = (q or "").strip().lower()
    if len(_q) < 2:
        return {"ok": True, "query": q, "results": []}
    _limit = max(1, min(int(limit or 20), 50))
    _ensure_cache()

    def _scan():
        hits = []
        if _state._SESSIONS_DIR is None:
            return hits
        for entry in sorted(_state._SESSIONS_DIR.iterdir(), reverse=True):
            if not entry.name.endswith(".json") or entry.name.endswith(".context.json"):
                continue
            m = _RE_CHAT_ID_PATTERN.search(entry.name)
            if not m:
                continue
            cid = m.group(1)
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            title = str(data.get("title") or "")
            if _q in title.lower():
                hits.append({"id": cid, "title": title,
                             "updated_at": data.get("updated_at", ""),
                             "match_in": "title", "snippet": title[:120]})
                if len(hits) >= _limit:
                    return hits
                continue
            for msg in data.get("messages") or []:
                c = msg.get("content")
                if isinstance(c, list):
                    c = " ".join(p.get("text", "") if isinstance(p, dict) else str(p)
                                 for p in c)
                c = str(c or "")
                pos = c.lower().find(_q)
                if pos >= 0:
                    start = max(0, pos - 40)
                    hits.append({"id": cid, "title": title or "Untitled",
                                 "updated_at": data.get("updated_at", ""),
                                 "match_in": "messages",
                                 "snippet": ("…" if start > 0 else "")
                                            + c[start:start + 120].replace("\n", " ")})
                    break
            if len(hits) >= _limit:
                return hits
        return hits

    results = await asyncio.to_thread(_scan)
    return {"ok": True, "query": q, "results": results[:_limit]}


@router.get("/{chat_id}")
async def get_chat(chat_id: str):
    _ensure_cache()
    chat = await asyncio.to_thread(_load_chat_full, chat_id)
    if chat is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    _restore_messages(chat_id, chat.get("messages", []))
    result = {k: v for k, v in chat.items() if k.startswith("_") is False}
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
    _ensure_cache()
    data = await req.json()
    now = datetime.now().isoformat(timespec="seconds")

    # ATOMIC UPDATE (2026-10-03 review fix): load, mutate and save under
    # the SAME per-chat lock (RLock, so _save_chat's lock nests). The old
    # shape loaded the chat outside the lock - a concurrent writer between
    # load and save made the PUT compare against a stale rev and clobber.
    def _locked_update():
        with _get_chat_save_lock(chat_id):
            chat = _load_chat_full(chat_id)
            if chat is None:
                return None
            with _cache_lock:
                if "title" in data:
                    chat["title"] = data["title"]
                if "workspace" in data:
                    chat["workspace"] = str(data["workspace"] or "")
                if "messages" in data:
                    chat["messages"] = data["messages"]
                    _msgs = data["messages"]
                    chat["_msg_count"] = _user_msg_count(_msgs)
                    chat["_tokens"] = _chat_tokens(_msgs)
                    _last = _msgs[-1].get("content", "") if _msgs else ""
                    chat["_preview"] = _last[:80] if isinstance(_last, str) else ""
                elif "messages" in chat:
                    _msgs = chat.get("messages", [])
                    chat["_msg_count"] = _user_msg_count(_msgs)
                    chat["_tokens"] = _chat_tokens(_msgs)
                    _last = _msgs[-1].get("content", "") if _msgs else ""
                    chat["_preview"] = _last[:80] if isinstance(_last, str) else ""
                chat["updated_at"] = now
            return _save_chat(chat_id, chat, data.get("base_rev"))

    _saved = await asyncio.to_thread(_locked_update)
    if _saved is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    if _saved and _saved.get("conflict"):
        _server = await asyncio.to_thread(_load_chat_full, chat_id)
        _server_msgs = copy.deepcopy((_server or {}).get("messages", []))
        await asyncio.to_thread(_restore_messages, chat_id, _server_msgs)
        return JSONResponse({"error": "stale_rev", "rev": _saved.get("rev"),
                             "messages": _server_msgs,
                             "hint": "server state wins - adopt these messages"},
                            status_code=409)
    return {"ok": True, "rev": (_saved or {}).get("rev")}


@router.delete("/{chat_id}")
async def delete_chat(chat_id: str):
    _ensure_cache()
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
