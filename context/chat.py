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
        matches = sorted(
            m for m in _SESSIONS_DIR.glob(f"*_{chat_id}.json")
            # SIDECAR EXCLUSION (2026-10-05, transcript experiment): the
            # sidecar `<...>.context.json` ALSO matches the glob (it ends in
            # `_<id>.json`) and sorts LAST alphabetically - so matches[-1]
            # read the weak sidecar instead of the main chat json whenever
            # both existed (every UI-PUT chat). The transcript (history
            # seed) must come from the MAIN json only.
            if not m.name.endswith(".context.json")
        )
        if not matches:
            return None
        return json.loads(matches[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _normalize_seed(seed: list) -> list:
    """Drop ALL trailing user messages (the newest is the new prompt itself,
    an aborted run's unanswered attempt before it would break alternation)
    and merge consecutive same-role messages (strict gemma/mistral
    templates reject two user roles in a row)."""
    while seed and seed[-1].get("role") == "user":
        seed = seed[:-1]
    merged: list = []
    for m in seed:
        if merged and merged[-1].get("role") == m.get("role"):
            merged[-1] = dict(merged[-1])
            merged[-1]["content"] = (merged[-1].get("content", "") + "\n" + m.get("content", "")).strip()
        else:
            merged.append(m)
    return merged


def history_seed_provenance(chat_id: str, limit: int = 40) -> tuple:
    """(seed, source) with HONEST provenance (2026-10-05, label-lie fix).

    The previous history_seed() fell back to the sidecar INTERNALLY while
    chat_run labelled whatever it returned as source=json - the label lied
    whenever the main json transcript was empty (live TG-PROBE experiment:
    'source=json seeded 2' was sidecar history). Callers that need the
    source use this probe; history_seed() stays for compatibility.
    source is 'json' ONLY when the MAIN chat json carried the transcript,
    'sidecar' when the fallback fired, 'none' when neither had turns.
    The seed is NORMALIZED identically to history_seed (trailing-user drop
    + merge), so the logged N is the post-normalization count."""
    seed = load_chat_transcript(chat_id, limit=limit)  # MAIN json only
    if seed:
        return _normalize_seed(seed), "json"
    try:
        side = (_load_chat_context(chat_id) or {}).get("session") or []
    except (OSError, ValueError):
        side = []
    if side:
        return _normalize_seed(side[-limit:]), "sidecar"
    return [], "none"


def history_seed(chat_id: str, limit: int = 40) -> list:
    """Seed for a NEW run (2026-10-03): the saved conversation, minus a
    TRAILING user message - that message is the new prompt itself and
    arrives via the run, so seeding it would duplicate it. Falls back to
    the sidecar "session" for chats without a json.
    NOTE (2026-10-05): the fallback is INVISIBLE here - callers that report
    a source must use history_seed_provenance()."""
    seed = load_chat_transcript(chat_id, limit=limit)
    if not seed:
        try:
            seed = (_load_chat_context(chat_id) or {}).get("session") or []
        except (OSError, ValueError):
            seed = []
    return _normalize_seed(seed)


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
        # part=true marks UI-only parts (planner bubble, thinking block,
        # checklist) - they persist for the RELOAD but stay out of the
        # model history so raw thinking does not crowd the token budget.
        if m.get("part"):
            continue
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
