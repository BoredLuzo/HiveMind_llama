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


_BLOB_MAGIC = b"HIVEBLOB1\n"
_BLOB_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _valid_blob_id(h: object) -> str | None:
    """Return *h* only when it is a strict 16-hex blob identifier."""
    if not isinstance(h, str) or not _BLOB_ID_RE.fullmatch(h):
        return None
    return h


def _blob_dir(chat_id: str) -> Path:
    return _state._SESSIONS_DIR / f"{chat_id}.blobs"


def _safe_blob_path(chat_id: str, h: object) -> Path | None:
    """Resolve blob path and verify strict containment inside _blob_dir(chat_id)."""
    bid = _valid_blob_id(h)
    if bid is None or _state._SESSIONS_DIR is None:
        return None
    try:
        sessions_root = _state._SESSIONS_DIR.resolve()
        bdir = _blob_dir(chat_id).resolve()
        bdir_rel = bdir.relative_to(sessions_root)
        if len(bdir_rel.parts) != 1:
            return None
        target = (bdir / bid).resolve()
        rel = target.relative_to(bdir)
        if not rel.parts or target == bdir:
            return None
        return target
    except (OSError, ValueError, RuntimeError):
        return None


def _blob_hash(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def _store_blob(chat_id: str, data: str) -> str:
    d = _blob_dir(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    h = _blob_hash(data)
    p = d / h
    if not p.exists():
        tmp = p.with_suffix(".tmp")
        # BINARY-SAFE (2026-10-23): text-mode write translated \n to \r\n on
        # Windows, so payloads containing literal \r\n came back as \n\n —
        # the roundtrip was lossy for every CRLF payload since 2026-10-03.
        # New blobs carry a magic prefix; legacy files are repaired on read
        # (the text-mode expansion \r*m+\n maps back to \r*(m-1)+\n).
        tmp.write_bytes(_BLOB_MAGIC + data.encode("utf-8"))
        os.replace(tmp, p)
        _blob_size_cache.pop(str(d), None)
    return h


def _read_blob(chat_id: str, h: str) -> str | None:
    p = _safe_blob_path(chat_id, h)
    if p is None:
        return None
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if raw.startswith(_BLOB_MAGIC):
        return raw[len(_BLOB_MAGIC):].decode("utf-8", errors="replace")
    # Legacy repair (text-mode writes) is WINDOWS-ONLY (audit 2026-10-24):
    # the \r\n expansion the repair reverses happens in Windows text-mode
    # writes. A POSIX-written legacy blob holds genuine \r\n — repairing
    # there would strip real CRs (silent corruption on the Linux install).
    if os.name != "nt":
        return raw.decode("utf-8", errors="replace")
    # every original \n was expanded to \r\n, so a run of m CRs before an
    # LF means m-1 original CRs
    return raw.replace(b"\r\n", b"\n").decode("utf-8", errors="replace")


def _sanitize_invalid_refs(node) -> None:
    """Neutralize client-injected {"_ref": ...} dicts with invalid blob IDs."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, dict) and "_ref" in v:
                if _valid_blob_id(v.get("_ref")) is None:
                    if k == "html":
                        node[k] = ""
                    elif k == "preview":
                        node[k] = _IMG_PLACEHOLDER
                    else:
                        node[k] = _BLOB_PLACEHOLDER
            elif isinstance(v, (dict, list)):
                _sanitize_invalid_refs(v)
    elif isinstance(node, list):
        for i, item in enumerate(list(node)):
            if isinstance(item, dict) and "_ref" in item:
                if _valid_blob_id(item.get("_ref")) is None:
                    node[i] = _BLOB_PLACEHOLDER
            elif isinstance(item, (dict, list)):
                _sanitize_invalid_refs(item)


def _extract_refs(chat_id: str, node) -> None:
    """Recursively replace big whitelisted strings with {"_ref": hash}."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, dict):
                if "_ref" in v:
                    if _valid_blob_id(v.get("_ref")) is None:
                        node[k] = _IMG_PLACEHOLDER if k == "preview" else _BLOB_PLACEHOLDER
                else:
                    _extract_refs(chat_id, v)
            elif isinstance(v, list):
                for item in v:
                    _extract_refs(chat_id, item)
            elif (k in _REF_KEYS and isinstance(v, str)
                  and len(v) >= _BLOB_MIN_CHARS
                  and not v.startswith(_BLOB_PLACEHOLDER)):
                node[k] = {"_ref": _store_blob(chat_id, v)}
    elif isinstance(node, list):
        for i, item in enumerate(list(node)):
            if isinstance(item, dict) and "_ref" in item:
                if _valid_blob_id(item.get("_ref")) is None:
                    node[i] = _BLOB_PLACEHOLDER
            elif isinstance(item, (dict, list)):
                _extract_refs(chat_id, item)


def _restore_refs(chat_id: str, node) -> None:
    """Inverse of _extract_refs; a missing or invalid blob becomes a placeholder."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, dict) and "_ref" in v:
                _bid = _valid_blob_id(v.get("_ref"))
                _txt = _read_blob(chat_id, _bid) if _bid is not None else None
                node[k] = _txt if _txt is not None else (
                    _IMG_PLACEHOLDER if k == "preview" else _BLOB_PLACEHOLDER)
            elif isinstance(v, (dict, list)):
                _restore_refs(chat_id, v)
    elif isinstance(node, list):
        for i, item in enumerate(list(node)):
            if isinstance(item, dict) and "_ref" in item:
                _bid = _valid_blob_id(item.get("_ref"))
                _txt = _read_blob(chat_id, _bid) if _bid is not None else None
                node[i] = _txt if _txt is not None else _BLOB_PLACEHOLDER
            elif isinstance(item, (dict, list)):
                _restore_refs(chat_id, item)


def _blobify_messages(chat_id: str, messages) -> None:
    """Save-side: extract big panel/image payloads into the blob store.
    Runs BEFORE the chat json is written, so a crash can only leave blobs
    without a json (cleaned by the startup sweep), never the reverse."""
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        _sanitize_invalid_refs(m)
        # HTML-BLOB (audit A6, 2026-10-23): the rendered answer html is the
        # biggest per-message payload (measured 1.1-2.7 MB per chat, 15-50x
        # the cp snapshots) and used to stay inline in the json.
        if (isinstance(m.get("html"), str)
                and len(m["html"]) >= _BLOB_MIN_CHARS
                and not m["html"].startswith(_BLOB_PLACEHOLDER)):
            m["html"] = {"_ref": _store_blob(chat_id, m["html"])}
        if isinstance(m.get("cp"), dict):
            _extract_refs(chat_id, m["cp"])
        imgs = m.get("images")
        if isinstance(imgs, list):
            for im in imgs:
                if not isinstance(im, dict):
                    continue
                if (isinstance(im.get("preview"), str)
                        and len(im["preview"]) >= _BLOB_MIN_CHARS
                        and "_ref" not in im):
                    im["preview"] = {"_ref": _store_blob(chat_id, im["preview"])}
                elif isinstance(im.get("preview"), dict) and "_ref" in im["preview"]:
                    if _valid_blob_id(im["preview"].get("_ref")) is None:
                        im["preview"] = _IMG_PLACEHOLDER


def _restore_messages(chat_id: str, messages) -> None:
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        # HTML-BLOB: a missing blob restores as "" — the frontend renders the
        # plain-text content field when html is empty (loadChat fallback).
        if isinstance(m.get("html"), dict) and "_ref" in m["html"]:
            _bid = _valid_blob_id(m["html"].get("_ref"))
            _txt = _read_blob(chat_id, _bid) if _bid is not None else None
            m["html"] = _txt if _txt is not None else ""
        if isinstance(m.get("cp"), dict):
            _restore_refs(chat_id, m["cp"])
        imgs = m.get("images")
        if isinstance(imgs, list):
            for im in imgs:
                if isinstance(im, dict) and isinstance(im.get("preview"), dict) and "_ref" in im["preview"]:
                    ref = im["preview"]
                    _bid = _valid_blob_id(ref.get("_ref"))
                    _txt = _read_blob(chat_id, _bid) if _bid is not None else None
                    im["preview"] = _txt if _txt is not None else _IMG_PLACEHOLDER


def _sweep_orphan_blob_dirs(existing_cids: set[str] | None = None) -> None:
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
            if existing_cids is not None:
                if cid in existing_cids:
                    continue
            elif any(_state._SESSIONS_DIR.glob(f"*_{cid}.json")):
                continue
            if _now - d.stat().st_mtime < 3600:
                continue
            shutil.rmtree(d, ignore_errors=True)
            _blob_size_cache.pop(str(d), None)
            logger.info("[CHATS] removed orphan blob dir %s", d.name)
    except OSError as _sweep_err:
        logger.debug("[CHATS] blob dir sweep failed: %s", _sweep_err)

_RE_CHAT_ID_PATTERN = re.compile(r"_([a-f0-9\-]{4,64})\.json$")
_CHAT_JSON_LIMIT = 200 * 1024 * 1024  # 200 MB emergency brake per save
_CHATS_INDEX_NAME = ".chats_index.cache"  # NOT .json: keeps session *.json globs clean
_chat_save_locks: dict = {}
_chat_save_locks_guard = threading.Lock()
_blob_size_cache: dict[str, tuple[int, int]] = {}
_ctx_interrupted_cache: dict[str, tuple[int, int, bool]] = {}
_re_slug_special = re.compile(r"[^\w\s-]")
_re_slug_space = re.compile(r"[\s_]+")


def _load_chats_index(sessions_dir: Path) -> dict[str, dict]:
    """Load persisted chat metadata index keyed by filename (empty dict on miss/corrupt)."""
    idx_path = sessions_dir / _CHATS_INDEX_NAME
    try:
        if not idx_path.is_file():
            return {}
        raw = json.loads(idx_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("version") == 1 and isinstance(raw.get("entries"), dict):
            return {
                str(k): v
                for k, v in raw["entries"].items()
                if isinstance(v, dict)
            }
    except (OSError, ValueError, TypeError) as exc:
        logger.debug("[CHATS] metadata index read skipped: %s", exc)
    return {}


def _save_chats_index(sessions_dir: Path, entries: dict[str, dict]) -> None:
    """Persist chat metadata index atomically outside *.json globs."""
    idx_path = sessions_dir / _CHATS_INDEX_NAME
    tmp = sessions_dir / ".chats_index.tmp"
    try:
        payload = json.dumps({"version": 1, "entries": entries}, ensure_ascii=False)
        tmp.write_text(payload, encoding="utf-8")
        _replace_with_retry(tmp, idx_path)
    except (OSError, ValueError, TypeError) as exc:
        logger.debug("[CHATS] metadata index write skipped: %s", exc)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


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
        disk_cids: set[str] = set()
        old_entries = _load_chats_index(_state._SESSIONS_DIR)
        new_entries: dict[str, dict] = {}
        index_dirty = False
        try:
            with os.scandir(_state._SESSIONS_DIR) as _it:
                _entries = sorted(
                    (
                        e for e in _it
                        if e.name.endswith(".json") and not e.name.endswith(".context.json")
                    ),
                    key=lambda e: e.name,
                )
        except OSError:
            _entries = []
        for entry in _entries:
            m = _RE_CHAT_ID_PATTERN.search(entry.name)
            if not m:
                continue
            cid = m.group(1)
            disk_cids.add(cid)
            try:
                st = entry.stat()
                cached = old_entries.get(entry.name)
                if (
                    isinstance(cached, dict)
                    and cached.get("cid") == cid
                    and cached.get("mtime_ns") == st.st_mtime_ns
                    and cached.get("size") == st.st_size
                    and isinstance(cached.get("meta"), dict)
                ):
                    meta = dict(cached["meta"])
                    meta["_file"] = entry.path
                    result[cid] = meta
                    new_entries[entry.name] = cached
                    continue
                data = json.loads(Path(entry.path).read_text(encoding="utf-8"))
                meta = {k: v for k, v in data.items() if k in _META}
                _msgs = data.get("messages", [])
                meta["_msg_count"] = _user_msg_count(_msgs)
                meta["_tokens"] = _chat_tokens(_msgs)
                _last = _msgs[-1].get("content", "") if _msgs else ""
                meta["_preview"] = _last[:80] if isinstance(_last, str) else ""
                new_entries[entry.name] = {
                    "cid": cid,
                    "mtime_ns": st.st_mtime_ns,
                    "size": st.st_size,
                    "meta": dict(meta),
                }
                meta["_file"] = entry.path
                result[cid] = meta
                index_dirty = True
            except Exception:
                pass
        if index_dirty or (old_entries and set(new_entries.keys()) != set(old_entries.keys())):
            _save_chats_index(_state._SESSIONS_DIR, new_entries)
        _chats_cache.clear()
        _chats_cache.update(result)
        _cache_loaded = True
        _sweep_orphan_blob_dirs(disk_cids)


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
        _last = _msgs[-1].get("content", "") if (_msgs and isinstance(_msgs[-1], dict)) else ""
        _cache_entry = {
            "id": stored.get("id", chat_id),
            "title": stored.get("title", "Untitled"),
            "created_at": stored.get("created_at", ""),
            "updated_at": stored.get("updated_at", ""),
            "_msg_count": _user_msg_count(_msgs),
            "_tokens": _chat_tokens(_msgs),
            "_preview": _last[:80] if isinstance(_last, str) else "",
            "_file": _file,
        }

        # Sanitize any client-injected invalid _ref payloads before blobification
        # or disk serialization so even an OSError in _store_blob cannot leave
        # traversal refs in the saved JSON.
        _sanitize_invalid_refs(stored.get("messages", []))

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
        # Update cache ONLY after disk write succeeds so oversize/IO errors
        # never poison _chats_cache with unwritten metadata.
        with _cache_lock:
            _chats_cache[chat_id] = _cache_entry
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
    await asyncio.to_thread(_ensure_cache)
    # ABORT-REGISTRY SWEEP (deep audit 2026-11-08): the helper existed but was
    # never called; listing chats is the natural refresh point.
    try:
        from infra import run_control as _rc
        await _rc._cleanup_abort_registry()
    except (ImportError, AttributeError, OSError, RuntimeError):
        pass
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
            _bdirs = []
            _direct_bdir = str(_blob_dir(cid))
            if os.path.isdir(_direct_bdir):
                _bdirs.append(_direct_bdir)
            import glob as _glob
            _legacy_pat = os.path.join(os.path.dirname(meta_file or ""), f"*_{cid}.blobs")
            for _lb in _glob.glob(_legacy_pat):
                if _lb != _direct_bdir:
                    _bdirs.append(_lb)
            for bf in _bdirs:
                for root, _dirs, files in os.walk(bf):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
        except OSError:
            pass
        return total
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

    def _chat_disk_meta_all(pairs):
        file_sizes: dict[str, int] = {}
        blobs_by_name: dict[str, tuple[str, int]] = {}
        legacy_blobs: list[tuple[str, str, int]] = []
        ctx_stats: dict[str, tuple[str, int, int]] | None = None
        if _state._SESSIONS_DIR is not None:
            try:
                with os.scandir(_state._SESSIONS_DIR) as _it:
                    ctx_stats = {}
                    for _e in _it:
                        _nm = _e.name
                        if _nm.endswith(".context.json"):
                            _st = _e.stat()
                            ctx_stats[_nm] = (_e.path, _st.st_mtime_ns, _st.st_size)
                        elif _nm.endswith(".json"):
                            file_sizes[_nm] = _e.stat().st_size
                        elif _nm.endswith(".blobs") and _e.is_dir():
                            _binfo = (_e.path, _e.stat().st_mtime_ns)
                            blobs_by_name[_nm] = _binfo
                            if "_" in _nm:
                                legacy_blobs.append((_nm, _e.path, _binfo[1]))
            except OSError:
                ctx_stats = None

        def _dir_bytes_cached(dir_path: str, mtime_ns: int) -> int:
            cached = _blob_size_cache.get(dir_path)
            if cached is not None and cached[0] == mtime_ns:
                return cached[1]
            sub = 0
            try:
                for root, _dirs, files in os.walk(dir_path):
                    for f in files:
                        try:
                            sub += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
            except OSError:
                pass
            _blob_size_cache[dir_path] = (mtime_ns, sub)
            return sub

        def _fast_bytes(cid: str, meta_file: str) -> int:
            if ctx_stats is None:
                return _disk_bytes(cid, meta_file)
            total = 0
            if meta_file:
                _mname = os.path.basename(meta_file)
                if _mname in file_sizes:
                    total += file_sizes[_mname]
                else:
                    try:
                        if os.path.isfile(meta_file):
                            total += os.path.getsize(meta_file)
                    except OSError:
                        pass
            _direct_name = f"{cid}.blobs"
            _direct_info = blobs_by_name.get(_direct_name)
            _direct_path = ""
            if _direct_info is not None:
                _direct_path = _direct_info[0]
                total += _dir_bytes_cached(_direct_path, _direct_info[1])
            if legacy_blobs:
                _lsuf = f"_{cid}.blobs"
                for _lnm, _lpath, _lmtime in legacy_blobs:
                    if _lnm.endswith(_lsuf) and _lpath != _direct_path:
                        total += _dir_bytes_cached(_lpath, _lmtime)
            return total

        def _fast_interrupted(cid: str, meta_file: str) -> bool:
            if ctx_stats is None:
                return _is_interrupted(cid)
            _cands: list[str] = []
            if meta_file:
                _cands.append(Path(meta_file).with_suffix(".context.json").name)
            _sa = f"standalone_{cid}.context.json"
            if _sa not in _cands:
                _cands.append(_sa)
            for _cname in _cands:
                _cinfo = ctx_stats.get(_cname)
                if _cinfo is None:
                    continue
                _cpath, _cmtime, _csize = _cinfo
                _hit = _ctx_interrupted_cache.get(_cpath)
                if _hit is not None and _hit[0] == _cmtime and _hit[1] == _csize:
                    return _hit[2]
                _flag = False
                try:
                    _ctx = json.loads(Path(_cpath).read_text(encoding="utf-8"))
                    _flag = bool(
                        isinstance(_ctx, dict)
                        and isinstance(_ctx.get("last_run"), dict)
                        and _ctx["last_run"].get("stop_reason") == "disconnect"
                    )
                except (OSError, ValueError, TypeError):
                    _flag = False
                _ctx_interrupted_cache[_cpath] = (_cmtime, _csize, _flag)
                return _flag
            return False

        return (
            {cid: _fast_bytes(cid, f) for cid, f in pairs},
            {cid: _fast_interrupted(cid, f) for cid, f in pairs},
        )

    _bytes_map, _int_map = await asyncio.to_thread(
        _chat_disk_meta_all, [(cid, c.get("_file", "")) for cid, c in items])
    for cid, c in items:
        result.append({
            "id": cid,
            "title": c.get("title", "Untitled"),
            "created_at": c.get("created_at", ""),
            "updated_at": c.get("updated_at", ""),
            "msg_count": c.get("_msg_count", 0),
            "tokens": c.get("_tokens", 0),
            "preview": c.get("_preview", ""),
            "bytes": _bytes_map.get(cid, 0),
            "interrupted": _int_map.get(cid, False),
        })
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
    # WS-GUARD (2026-10-25 audit): a dead path is not stored anymore (it was
    # silently skipped by the run resolver). Dropped with a log instead of a
    # 400 — the autosave caller must not lose the MESSAGES over the
    # workspace field (PUT /{chat_id} keeps its strict 400 contract).
    if data.get("workspace"):
        _ws_raw = str(data["workspace"]).strip()
        if _ws_raw and not Path(_ws_raw).exists():
            logger.warning(
                "[CHATS-GUARD] workspace '%s' does not exist - dropped "
                "(chat falls back to the global workspace)", _ws_raw[:120],
            )
        else:
            chat["workspace"] = _ws_raw
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
    import asyncio as _aio
    await _aio.to_thread(_ensure_cache)  # H-audit: first-call scan off the loop
    data = await req.json()
    msgs = data.get("messages", []) or []
    if not isinstance(msgs, list) or not all(isinstance(m, dict) for m in msgs):
        return {"ok": False, "error": "messages must be a list of objects"}
    # H-audit: a garbage chat_id fed the adoption glob (*_*.json) and could
    # overwrite a foreign chat - pin the shape the cache regex already means.
    _cid = str(data.get("chat_id") or "")
    if _cid and not re.fullmatch(r"[a-f0-9-]{4,64}", _cid):
        return {"ok": False, "error": "invalid chat_id"}
    if not msgs:
        return {"ok": True, "id": data.get("chat_id"), "updated": False}
    now = datetime.now().isoformat(timespec="seconds")
    cid = data.get("chat_id") or None

    def _refresh_meta(chat: dict, msgs_list: list) -> None:
        chat["_msg_count"] = _user_msg_count(msgs_list)
        chat["_tokens"] = _chat_tokens(msgs_list)
        _last = msgs_list[-1].get("content", "") if msgs_list else ""
        chat["_preview"] = _last[:80] if isinstance(_last, str) else ""

    if cid:
        def _locked_persist_update():
            with _get_chat_save_lock(cid):
                chat = _load_chat_full(cid)
                if chat is None:
                    return False, None
                with _cache_lock:
                    chat["messages"] = msgs
                    if data.get("title"):
                        chat["title"] = data["title"]
                    # WS-GUARD (2026-10-25 audit): dead paths are dropped (logged), never stored.
                    if data.get("workspace"):
                        _ws_raw = str(data["workspace"]).strip()
                        if _ws_raw and not Path(_ws_raw).exists():
                            logger.warning(
                                "[CHATS-GUARD] persist: workspace '%s' does not exist - dropped",
                                _ws_raw[:120],
                            )
                        else:
                            chat["workspace"] = _ws_raw
                    chat["updated_at"] = now
                    _refresh_meta(chat, msgs)
                return True, _save_chat(cid, chat, data.get("base_rev"))

        _found, _saved = await asyncio.to_thread(_locked_persist_update)
        if _found:
            _saved = _saved or {}
            if _saved.get("conflict"):
                return JSONResponse({"error": "stale_rev",
                                     "rev": _saved.get("rev")},
                                    status_code=409)
            if _saved.get("oversize"):
                return JSONResponse({"ok": False, "error": "chat_too_large"},
                                    status_code=413)
            return {"ok": True, "id": cid, "rev": _saved.get("rev"),
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
        _ws_raw = str(data["workspace"]).strip()
        if _ws_raw and not Path(_ws_raw).exists():
            logger.warning(
                "[CHATS-GUARD] persist: workspace '%s' does not exist - dropped",
                _ws_raw[:120])
        else:
            chat["workspace"] = _ws_raw
    _refresh_meta(chat, msgs)
    _saved = await asyncio.to_thread(_save_chat, cid, chat)
    if _saved and _saved.get("oversize"):
        return JSONResponse({"ok": False, "error": "chat_too_large"},
                            status_code=413)
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
    await asyncio.to_thread(_ensure_cache)

    def _scan():
        hits = []
        if _state._SESSIONS_DIR is None or not _state._SESSIONS_DIR.exists():
            return hits
        for entry in _state._SESSIONS_DIR.iterdir():
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
        hits.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return hits[:_limit]

    results = await asyncio.to_thread(_scan)
    return {"ok": True, "query": q, "results": results[:_limit]}


@router.get("/{chat_id}")
async def get_chat(chat_id: str):
    await asyncio.to_thread(_ensure_cache)
    chat = await asyncio.to_thread(_load_chat_full, chat_id)
    if chat is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    def _hydrate() -> dict | None:
        _restore_messages(chat_id, chat.get("messages", []))
        try:
            return _load_chat_context(chat_id)
        except (OSError, ValueError, TypeError):
            return None

    _ctx = await asyncio.to_thread(_hydrate)
    result = {k: v for k, v in chat.items() if k.startswith("_") is False}
    # INTERRUPTED (2026-08-21): last_run (stop_reason="disconnect") mitliefern,
    if isinstance(_ctx, dict):
        # json workspace wins; the sidecar key only fills older chats
        # unless the main json explicitly set an empty workspace string.
        if "workspace" not in result and _ctx.get("workspace"):
            result["workspace"] = _ctx["workspace"]
        if isinstance(_ctx.get("last_run"), dict):
            result["last_run"] = _ctx["last_run"]
    return result


@router.put("/{chat_id}")
async def update_chat(chat_id: str, req: Request):
    await asyncio.to_thread(_ensure_cache)
    data = await req.json()
    # WORKSPACE-GUARD (2026-10-15, audit N9): PUT /chats/{id} applied any
    # string as workspace unvalidated - in contrast to POST /settings
    # (routers/config.py), which rejects dead paths. A silent cross-project
    # switch here poisons the whole chat (GET /chats/{id} serves the json
    # value into the run resolver). Same contract: empty string clears.
    if "workspace" in data:
        _ws_raw = str(data.get("workspace") or "").strip()
        if _ws_raw and not Path(_ws_raw).exists():
            logger.warning(
                "[CHATS-GUARD] workspace '%s' does not exist - rejected "
                "(previous value stays active)", _ws_raw[:120],
            )
            return JSONResponse({
                "error": f"Workspace path does not exist: {_ws_raw}",
            }, status_code=400)
        data["workspace"] = _ws_raw
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
                if "messages" in data or "messages" in chat:
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
    if _saved and _saved.get("oversize"):
        return JSONResponse({"ok": False, "error": "chat_too_large"},
                            status_code=413)
    if _saved and _saved.get("conflict"):
        _server = await asyncio.to_thread(_load_chat_full, chat_id)
        _server_msgs = copy.deepcopy((_server or {}).get("messages", []))
        await asyncio.to_thread(_restore_messages, chat_id, _server_msgs)
        return JSONResponse({"error": "stale_rev", "rev": _saved.get("rev"),
                             "messages": _server_msgs,
                             "hint": "server state wins - adopt these messages"},
                            status_code=409)
    if "workspace" in data:
        def _sync_ctx_ws():
            from context.chat import _mutate_chat_context
            _new_ws = str(data.get("workspace") or "").strip()
            def _mut(c):
                if _new_ws:
                    c["workspace"] = _new_ws
                else:
                    c.pop("workspace", None)
            _mutate_chat_context(chat_id, _mut)
        await asyncio.to_thread(_sync_ctx_ws)
    return {"ok": True, "rev": (_saved or {}).get("rev")}


@router.delete("/{chat_id}")
async def delete_chat(chat_id: str):
    # SHAPE-GUARD (audit 2026-11-03): every other chat endpoint validates the
    # id; delete fed it straight into the blob dir + rmtree.
    if not re.fullmatch(r"[a-f0-9-]{4,64}", chat_id or ""):
        return JSONResponse({"ok": False, "error": "invalid chat_id"},
                            status_code=400)
    await asyncio.to_thread(_ensure_cache)
    def _pop_chat():
        with _cache_lock:
            return _chats_cache.pop(chat_id, None)
    chat = await asyncio.to_thread(_pop_chat)
    if chat:
        def _delete_files():
            try:
                p = Path(chat.get("_file", "")) if chat.get("_file") else None
                if p and p.exists():
                    p.unlink()
                if p:
                    ctx_p = p.with_suffix(".context.json")
                    if ctx_p.exists():
                        ctx_p.unlink()
                    _ctx_interrupted_cache.pop(str(ctx_p), None)
                _bdir = _blob_dir(chat_id)
                shutil.rmtree(_bdir, ignore_errors=True)
                _blob_size_cache.pop(str(_bdir), None)
                try:
                    from context.chat import clear_chat_images as _clear_imgs
                    _clear_imgs(chat_id)
                except (ImportError, OSError):
                    pass
            except OSError:
                pass

        await asyncio.to_thread(_delete_files)
    return {"ok": True}
