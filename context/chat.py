"""Chat-Context-Persistenz (aus server.py extrahiert)."""
from __future__ import annotations
import json, logging, threading, time
from pathlib import Path
from utils.file import write_json_atomic as _write_json_atomic

logger = logging.getLogger("hivemind.chat_context")
_chat_ctx_locks: dict[str, threading.Lock] = {}
_chat_ctx_locks_guard = threading.Lock()
_cache_lock: threading.Lock = threading.Lock()
_chats_cache: dict = {}
_SESSIONS_DIR: Path | None = None
_file_signature_matches = None
_build_file_signature = None

def init_chat_context(sessions_dir, chats_cache=None, cache_lock=None,
                      file_signature_matches=None, build_file_signature=None):
    global _SESSIONS_DIR, _chats_cache, _cache_lock
    global _file_signature_matches, _build_file_signature
    _SESSIONS_DIR = Path(sessions_dir)
    if chats_cache is not None: _chats_cache = chats_cache
    if cache_lock is not None: _cache_lock = cache_lock
    if file_signature_matches: _file_signature_matches = file_signature_matches
    if build_file_signature: _build_file_signature = build_file_signature


def _read_chat_json(chat_id: str) -> dict | None:
    try:
        if not _SESSIONS_DIR:
            return None
        matches = sorted(_SESSIONS_DIR.glob(f"*_{chat_id}.json"))
        if not matches:
            return None
        return json.loads(matches[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def history_seed(chat_id: str, limit: int = 40) -> list:
    """Seed for a NEW run (2026-10-03): the saved conversation, minus a
    TRAILING user message - that message is the new prompt itself and
    arrives via the run, so seeding it would duplicate it. Falls back to
    the sidecar "session" for chats without a json."""
    seed = load_chat_transcript(chat_id, limit=limit)
    if not seed:
        try:
            seed = (_load_chat_context(chat_id) or {}).get("session") or []
        except (OSError, ValueError):
            seed = []
    # Drop ALL trailing user messages: the newest one is the new prompt
    # itself, and an aborted run's unanswered attempt before it would
    # leave two user roles in a row in the final prompt (strict
    # alternation templates reject that). The user's latest intent is
    # carried by the new prompt, not by the dead attempt.
    while seed and seed[-1].get("role") == "user":
        seed = seed[:-1]
    # INTERRUPTED MARKER (2026-10-03 review): an aborted run's partial
    # answer must not be continued as if it were complete - tell the model
    # explicitly what happened.
    for _m in seed:
        if _m.get("role") == "assistant" and _m.get("interrupted"):
            _i = seed.index(_m)
            _c = dict(_m)
            _c["content"] = (str(_c.get("content", ""))
                             + "\n[NOTE: this reply was cut off by the user "
                               "or an error and may be INCOMPLETE]")
            seed[_i] = _c
    # ALTERNATION (2026-10-03): an aborted run leaves its unanswered user
    # message in the transcript - with the new prompt that makes two user
    # messages in a row, which strict templates (gemma/mistral) reject.
    # Merge consecutive same-role messages instead of emitting them raw.
    merged: list = []
    for m in seed:
        if merged and merged[-1].get("role") == m.get("role"):
            merged[-1] = dict(merged[-1])
            merged[-1]["content"] = (merged[-1].get("content", "") + "\n" + m.get("content", "")).strip()
        else:
            merged.append(m)
    return merged


def get_chat_workspace(chat_id: str) -> str:
    """Top-level workspace stored in the chat json ("" when absent)."""
    data = _read_chat_json(chat_id)
    return str((data or {}).get("workspace") or "")


def load_chat_transcript(chat_id: str, limit: int = 40) -> list:
    """USER-VISIBLE transcript as THE model history source (2026-10-03).

    Reads the newest `limit` user/assistant text messages straight from the
    chat json in sessions/. Message-level content is never blobified, so no
    ref restoration is needed here. The sidecar "session" key demotes to a
    fallback for chats without a json (or run-state-only sidecars)."""
    data = _read_chat_json(chat_id)
    if not data:
        return []
    out = []
    for m in data.get("messages", []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content})
    return out[-limit:]


def _get_chat_ctx_lock(chat_id: str) -> threading.Lock:
    """Per-chat lock to serialize context file access."""
    _cid = str(chat_id or "")
    with _chat_ctx_locks_guard:
        _lk = _chat_ctx_locks.get(_cid)
        if _lk is None:
            _lk = threading.Lock()
            _chat_ctx_locks[_cid] = _lk
        return _lk


def _load_chat_context_locked(chat_id: str) -> dict:
    """Internal context loader. Caller must hold per-chat lock."""
    p = _ctx_path_for_chat(chat_id)
    if not p or not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_chat_context_locked(chat_id: str, ctx: dict):
    """Internal context saver. Caller must hold per-chat lock."""
    p = _ctx_path_for_chat(chat_id)
    if not p:
        return
    try:
        _write_json_atomic(p, ctx)
    except Exception as _e:
        logger.warning("Context save failed for %s: %s", p, _e)


def _mutate_chat_context(chat_id: str, mutator):
    """Atomically load-mutate-save context for one chat_id."""
    if not chat_id:
        return
    _lk = _get_chat_ctx_lock(chat_id)
    with _lk:
        _ctx = _load_chat_context_locked(chat_id)
        mutator(_ctx)
        _save_chat_context_locked(chat_id, _ctx)


def _ctx_path_for_chat(chat_id: str) -> Path | None:
    if not chat_id:
        return None
    try:
        with _cache_lock:
            chat = _chats_cache.get(chat_id)
        if chat and chat.get("_file"):
            p = Path(chat["_file"])
            return p.with_suffix(".context.json")
        for f in _SESSIONS_DIR.glob(f"*_{chat_id}.json"):
            return f.with_suffix(".context.json")
    except Exception as _e:
        logger.warning("_ctx_path_for_chat(%s) failed: %s", chat_id, _e)
    return None


def _load_chat_context(chat_id: str) -> dict:
    if not chat_id:
        return {}
    _lk = _get_chat_ctx_lock(chat_id)
    with _lk:
        return _load_chat_context_locked(chat_id)


def _save_chat_context(chat_id: str, ctx: dict):
    if not chat_id:
        return
    _lk = _get_chat_ctx_lock(chat_id)
    with _lk:
        _save_chat_context_locked(chat_id, ctx)


def _chat_context_valid(ctx: dict, explore_ttl: int = 3600) -> bool:
    if not ctx or not ctx.get("workspace") or not ctx.get("explore_ctx"):
        return False
    if not Path(ctx["workspace"]).is_dir():
        return False
    if time.time() - ctx.get("ts", 0) > explore_ttl:
        return False
    for path_str, cached_mtime in ctx.get("files", {}).items():
        if not _file_signature_matches(path_str, cached_mtime):
            return False
    return True


def update_test_history(chat_id: str, results: dict[str, str]) -> None:
    """Persist per-test outcomes for flaky detection.

    results = {test_id: "pass"|"fail"}
    Keeps last 8 outcomes per test_id.
    """
    if not chat_id:
        return
    _lk = _get_chat_ctx_lock(chat_id)
    with _lk:
        ctx = _load_chat_context_locked(chat_id) or {}
        hist = ctx.get("test_history", {})
        for tid, outcome in results.items():
            entry = hist.setdefault(tid, {"outcomes": []})
            entry["outcomes"] = (entry["outcomes"] + [outcome])[-8:]
        ctx["test_history"] = hist
        _save_chat_context_locked(chat_id, ctx)
