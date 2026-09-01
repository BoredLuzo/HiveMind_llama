"""Chats API-Router."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
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

_RE_CHAT_ID_PATTERN = re.compile(r"_([a-f0-9\-]{4,36})\.json$")
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


def _save_chat(chat_id: str, chat: dict):
    with _cache_lock:
        _file = str(chat.get("_file") or "")
        if not _file and _state._SESSIONS_DIR:
            ts = chat.get("created_at", datetime.now().isoformat(timespec="seconds"))
            ts = ts.replace(":", "-").replace("T", "T")[:19]
            title = chat.get("title", "Chat")
            slg = title.split("\n")[0][:40]
            slg = _re_slug_special.sub("", slg)
            slg = _re_slug_space.sub("-", slg.strip()) or "Chat"
            _file = str(_state._SESSIONS_DIR / f"{ts}_{slg}_{chat_id}.json")
            chat["_file"] = _file
        _msgs = chat.get("messages", [])
        _last = _msgs[-1].get("content", "") if _msgs else ""
        _chats_cache[chat_id] = {
            "id": chat.get("id", chat_id),
            "title": chat.get("title", "Untitled"),
            "created_at": chat.get("created_at", ""),
            "updated_at": chat.get("updated_at", ""),
            "_msg_count": _user_msg_count(_msgs),
            "_tokens": _chat_tokens(_msgs),
            "_preview": _last[:80] if isinstance(_last, str) else "",
            "_file": _file,
        }
    _out = {k: v for k, v in chat.items() if not k.startswith("_")}
    _txt = json.dumps(_out, indent=2, ensure_ascii=False)
    _p = Path(_file)
    if not _p.name:
        return
    _tmp = _p.with_suffix(".tmp")
    _tmp.write_text(_txt, encoding="utf-8")
    os.replace(_tmp, _p)


@router.get("")
async def list_chats():
    _ensure_cache()
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
    _msgs = chat["messages"]
    _last = _msgs[-1].get("content", "") if _msgs else ""
    chat["_msg_count"] = _user_msg_count(_msgs)
    chat["_tokens"] = _chat_tokens(_msgs)
    chat["_preview"] = _last[:80] if isinstance(_last, str) else ""
    _save_chat(cid, chat)
    return {"ok": True, "id": cid, "chat": {k: v for k, v in chat.items() if k != "_file"}}


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
                    chat["updated_at"] = now
                    chat["_msg_count"] = _user_msg_count(msgs)
                    chat["_tokens"] = _chat_tokens(msgs)
                    _last = msgs[-1].get("content", "") if msgs else ""
                    chat["_preview"] = _last[:80] if isinstance(_last, str) else ""
            await asyncio.to_thread(_apply_update)
            await asyncio.to_thread(_save_chat, cid, chat)
            return {"ok": True, "id": cid, "created": False, "updated": True}

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
    _last = msgs[-1].get("content", "") if msgs else ""
    chat["_msg_count"] = _user_msg_count(msgs)
    chat["_tokens"] = _chat_tokens(msgs)
    chat["_preview"] = _last[:80] if isinstance(_last, str) else ""
    await asyncio.to_thread(_save_chat, cid, chat)
    return {"ok": True, "id": cid, "created": True, "updated": False}


@router.get("/{chat_id}")
async def get_chat(chat_id: str):
    _ensure_cache()
    chat = await asyncio.to_thread(_load_chat_full, chat_id)
    if chat is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    result = {k: v for k, v in chat.items() if k.startswith("_") is False}
    # INTERRUPTED (2026-08-21): last_run (stop_reason="disconnect") mitliefern,
    try:
        _ctx = _load_chat_context(chat_id)
        if isinstance(_ctx, dict):
            if _ctx.get("workspace"):
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
    chat = await asyncio.to_thread(_load_chat_full, chat_id)
    if chat is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    now = datetime.now().isoformat(timespec="seconds")
    def _apply_update():
        with _cache_lock:
            if "title" in data:
                chat["title"] = data["title"]
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
    await asyncio.to_thread(_apply_update)
    _save_chat(chat_id, chat)
    return {"ok": True}


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
        except Exception:
            pass
    return {"ok": True}
