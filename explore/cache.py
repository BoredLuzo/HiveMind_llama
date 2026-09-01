"""Pre-Explore RAM-Cache (aus server.py extrahiert)."""
from __future__ import annotations
import asyncio, hashlib as _hashlib, json, logging, os, threading, time
from pathlib import Path
from hive_functions.tree_scout import parse_contract_summary
from utils.patterns import _RE_WIN_PATH, _RE_UNIX_PATH

logger = logging.getLogger("hivemind.pre_explore_cache")

_file_signature_matches = None
_build_file_signature = None
settings = None
_PRE_EXPLORE_CACHE_TTL = 600
_PRE_EXPLORE_CACHE_MAX = 32


_PRE_EXPLORE_CACHE_VERSION = 2


def init_explore_cache(file_signature_matches_fn=None, build_file_signature_fn=None,
                       settings_dict=None, cache_ttl=600, cache_max=32, cache_version=2):
    global _file_signature_matches, _build_file_signature, settings
    global _PRE_EXPLORE_CACHE_TTL, _PRE_EXPLORE_CACHE_MAX, _PRE_EXPLORE_CACHE_VERSION
    if file_signature_matches_fn:
        _file_signature_matches = file_signature_matches_fn
    if build_file_signature_fn:
        _build_file_signature = build_file_signature_fn
    if settings_dict is not None:
        settings = settings_dict
    _PRE_EXPLORE_CACHE_TTL = cache_ttl
    _PRE_EXPLORE_CACHE_MAX = cache_max
    _PRE_EXPLORE_CACHE_VERSION = cache_version

# ── Pre-Explore Cache ──────────────────────────────────────────────────────────
_pre_explore_cache: dict = {}
_pre_explore_cache_lock: asyncio.Lock | None = None


def _get_pre_explore_lock() -> asyncio.Lock:
    """Lazy-Init: asyncio.Lock erst beim ersten Aufruf erzeugen (Python 3.12 safe)."""
    global _pre_explore_cache_lock
    if _pre_explore_cache_lock is None:
        _pre_explore_cache_lock = asyncio.Lock()
    return _pre_explore_cache_lock

async def _pre_explore_cache_invalidate_workspace(workspace: str):  # BUG-6 FIX: async gemacht
    _ws_norm = str(Path(workspace).resolve()).lower()
    async with _get_pre_explore_lock():  # BUG-6 FIX: async with
        _keys = [k for k in _pre_explore_cache if str(k[0]).lower() == _ws_norm]
        for k in _keys:
            _pre_explore_cache.pop(k, None)

async def _pre_explore_cache_set(key: tuple, value: dict):  # BUG-6 FIX: async gemacht
    _results = value.get("results", [])
    _total_read = sum(r.get("n_files_read", 0) for r in _results) if _results else 0
    _ctx_len = len(str(value.get("explore_ctx", "") or "").strip())
    if _total_read == 0 and _ctx_len < 30:
        logger.debug("pre_explore_cache: skip caching empty result")
        return

    async with _get_pre_explore_lock():  # BUG-6 FIX: async with
        _pre_explore_cache[key] = value
        _now = time.time()
        expired = [k for k, v in _pre_explore_cache.items()
                   if _now - v.get("ts", 0) > _PRE_EXPLORE_CACHE_TTL]
        for k in expired:
            _pre_explore_cache.pop(k, None)
        while len(_pre_explore_cache) > _PRE_EXPLORE_CACHE_MAX:
            oldest = min(_pre_explore_cache, key=lambda k: _pre_explore_cache[k].get("ts", 0))
            _pre_explore_cache.pop(oldest, None)

def _explore_cache_settings_sig() -> str:
    """Stable settings signature for pre-explore cache keys."""
    try:
        _cfg = {
            "v": _PRE_EXPLORE_CACHE_VERSION,
            "duo_pre_explore_ctx": settings.get("duo_pre_explore_ctx"),
            "duo_pre_explore_tokens": settings.get("duo_pre_explore_tokens"),
            "duo_pre_explore_max_tools": settings.get("duo_pre_explore_max_tools"),
            "duo_pre_explore_ctx_char_ratio": settings.get("duo_pre_explore_ctx_char_ratio"),
            "duo_parallel_preexplore": settings.get("duo_parallel_preexplore"),
            "duo_worker_slots": settings.get("duo_worker_slots"),
            "duo_tree_scout_enabled": settings.get("duo_tree_scout_enabled"),
            "duo_tree_scout_max_depth": settings.get("duo_tree_scout_max_depth"),
            "duo_tree_scout_max_files": settings.get("duo_tree_scout_max_files"),
            "duo_websearch_enabled": settings.get("duo_websearch_enabled"),
        }
        _raw_agent = settings.get("exploration_agent") or {}
        if isinstance(_raw_agent, dict):
            _workers = []
            for _w in (_raw_agent.get("workers") or []):
                if not isinstance(_w, dict):
                    continue
                _workers.append({
                    "model": str(_w.get("model") or ""),
                    "ctx": int(_w.get("ctx") or 0) if _w.get("ctx") is not None else 0,
                    "parallel": int(_w.get("parallel") or 0) if _w.get("parallel") is not None else None,
                })
            _cfg["exploration_agent"] = {
                "enabled": bool(_raw_agent.get("enabled", False)),
                "model": str(_raw_agent.get("model") or ""),
                "workers": _workers,
            }
        _cfg_json = json.dumps(_cfg, sort_keys=True, separators=(",", ":"))
        return _hashlib.md5(_cfg_json.encode("utf-8", errors="ignore")).hexdigest()[:12]
    except Exception:
        return "0"

def _explore_cache_key(workspace: str, exec_mdl: str, task: str = "") -> tuple:
    # Voller Task-Hash als stabiler Cache-Key.
    task_hash = _hashlib.md5(task.encode("utf-8", errors="ignore")).hexdigest()[:12]
    cfg_sig = _explore_cache_settings_sig()
    return (workspace, exec_mdl, task_hash, cfg_sig)

def _explore_cache_valid(entry: dict) -> bool:
    # TTL-Check
    if not entry:
        return False
    if time.time() - entry.get("ts", 0) > _PRE_EXPLORE_CACHE_TTL:
        return False
    
    # File-Modification-Time validieren
    for path_str, cached_mtime in entry.get("files", {}).items():
        if not _file_signature_matches(path_str, cached_mtime):
            return False
    
    _ws_path = Path(entry.get("workspace", "."))
    _safety_files = [".gitignore", "package.json", "tsconfig.json", "pyproject.toml", "pom.xml", "Cargo.toml", "setup.py"]
    for safety_file in _safety_files:
        _sf_abs = str(_ws_path / safety_file)
        _cached_sig = entry.get("files", {}).get(_sf_abs) or entry.get("files", {}).get(safety_file, 0)
        if not _file_signature_matches(_sf_abs, _cached_sig):
            return False
    
    return True

def _explore_extract_files(explore_ctx: str, pre_explore_msgs: list) -> dict:
    file_mtimes: dict = {}
    _found: set = set()
    for m in _RE_WIN_PATH.finditer(explore_ctx):
        _found.add(m.group())
    for m in _RE_UNIX_PATH.finditer(explore_ctx):
        _found.add(m.group())
    for msg in pre_explore_msgs[:10]:
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) < 3000:
            for m in _RE_WIN_PATH.finditer(content):
                _found.add(m.group())
            for m in _RE_UNIX_PATH.finditer(content):
                _found.add(m.group())
    for p in _found:
        _sig = _build_file_signature(p)
        if _sig:
            file_mtimes[p] = _sig
    return file_mtimes

