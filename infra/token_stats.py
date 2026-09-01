"""Token-Usage-Tracking (aus server.py extrahiert)."""
from __future__ import annotations

import json, logging, threading, time
from pathlib import Path
from utils.file import write_json_atomic as _write_json_atomic

logger = logging.getLogger("hivemind.token_stats")
_TOKEN_STATS_FILE: Path | None = None
_token_stats_lock: threading.Lock | None = None

def init_token_stats(stats_file=None, lock=None):
    global _TOKEN_STATS_FILE, _token_stats_lock
    if stats_file is not None:
        _TOKEN_STATS_FILE = Path(stats_file)
    if lock is not None:
        _token_stats_lock = lock

def _load_token_stats() -> dict:
    """Load token stats from disk. Returns {total_tokens, total_runs, daily: {...}}.

    1.1.0: zusaetzlich total_prompt_tokens / total_cached_tokens sowie
    prompt/cached-Felder je Day-Entry und Run (backward-compat via .get)."""
    if _TOKEN_STATS_FILE is None or not _TOKEN_STATS_FILE.exists():
        return {"total_tokens": 0, "total_runs": 0, "daily": {}}
    try:
        return json.loads(_TOKEN_STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"total_tokens": 0, "total_runs": 0, "daily": {}}


def _save_token_stats(stats: dict):
    """Atomic write of token stats."""
    if _TOKEN_STATS_FILE is None:
        return
    _write_json_atomic(_TOKEN_STATS_FILE, stats)


def _record_run(run_record: dict) -> None:


    if _token_stats_lock is None:
        return
    tokens = int(run_record.get("tokens") or 0)
    prompt = int(run_record.get("prompt_tokens") or 0)
    cached = int(run_record.get("cached_tokens") or 0)
    requests = int(run_record.get("requests") or 0)
    if tokens <= 0 and prompt <= 0 and cached <= 0 and requests <= 0:
        return
    with _token_stats_lock:
        stats = _load_token_stats()
        stats["total_tokens"] = stats.get("total_tokens", 0) + max(tokens, 0)
        stats["total_runs"] = stats.get("total_runs", 0) + 1
        stats["total_prompt_tokens"] = stats.get("total_prompt_tokens", 0) + prompt
        stats["total_cached_tokens"] = stats.get("total_cached_tokens", 0) + cached
        stats["total_requests"] = stats.get("total_requests", 0) + max(requests, 0)
        daily = stats.setdefault("daily", {})
        _now = time.localtime()
        day = time.strftime("%Y-%m-%d", _now)
        day_entry = daily.setdefault(day, {"tokens": 0, "runs": 0, "elapsed_s": 0, "run_list": []})
        day_entry["tokens"] = day_entry.get("tokens", 0) + max(tokens, 0)
        day_entry["runs"] = day_entry.get("runs", 0) + 1
        day_entry["elapsed_s"] = day_entry.get("elapsed_s", 0.0) + float(run_record.get("elapsed_s") or 0)
        day_entry["prompt_tokens"] = day_entry.get("prompt_tokens", 0) + prompt
        day_entry["cached_tokens"] = day_entry.get("cached_tokens", 0) + cached
        day_entry["requests"] = day_entry.get("requests", 0) + max(requests, 0)
        _run = {
            "run_id": str(run_record.get("run_id") or ""),
            "t": time.strftime("%H:%M:%S", _now),
            "tokens": tokens,
            "elapsed_s": float(run_record.get("elapsed_s") or 0),
            "stop_reason": str(run_record.get("stop_reason") or "completed"),
            "phases": dict(run_record.get("phases") or {}),
            "models": dict(run_record.get("models") or {}),
            "prompt_tokens": prompt,
            "cached_tokens": cached,
            "requests": requests,
            "phases_prompt": dict(run_record.get("phases_prompt") or {}),
            "phases_cached": dict(run_record.get("phases_cached") or {}),
        }
        day_entry.setdefault("run_list", []).append(_run)
        if len(day_entry["run_list"]) > 500:
            day_entry["run_list"] = day_entry["run_list"][-500:]
        # Prune daily entries older than 90 days
        try:
            _sorted_days = sorted(daily.keys(), reverse=True)
            for _old_day in _sorted_days[90:]:
                del daily[_old_day]
        except Exception:
            pass
        _save_token_stats(stats)


def _estimate_tokens_from_content(content: str) -> int:
    """Estimate token count from streamed content. Uses chars/3 heuristic
    (matches frontend estimation). More accurate than no tracking at all."""
    if not content:
        return 0
    # CJK heuristic: if >30% of chars are CJK, use chars/2
    _cjk = sum(1 for c in content if '\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u309f' or '\u30a0' <= c <= '\u30ff')
    if _cjk > len(content) * 0.3:
        return max(1, len(content) // 2)
    return max(1, len(content) // 3)

