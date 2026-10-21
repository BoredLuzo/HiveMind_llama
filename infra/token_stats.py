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
            # METRICS-EXPAND (2026-10-19): per-phase duration + run mode so the
            # UI can show "how long" and "how strong" instead of only totals.
            "phases_ms": dict(run_record.get("phases_ms") or {}),
            "ui_mode": str(run_record.get("ui_mode") or ""),
            "mode": str(run_record.get("mode") or ""),
            # EDITOR-CONTEXT (2026-10-21): did an IDE feed this run, and how much
            "editor": (dict(run_record.get("editor")) if isinstance(run_record.get("editor"), dict) else None),
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

#: Roles that can own work in a run (whitelist of the phase names we record).
# pipeline stages own work too: analyst -> refiner -> critic -> synthesizer
_ROLES = ("coder", "planner", "critic", "pre_explore", "direct",
          "analyst", "refiner", "synthesizer", "vision")


def aggregate_by_model(daily: dict, day: str | None = None,
                       speeds: dict | None = None) -> list[dict]:
    """Group runs by (model, ROLE) - not by model alone.

    One row per model *and* role:
        model, role, runs, tokens, timed_tokens, timed_runs, seconds, tps,
        stop, completed_share, last, estimated

    Role tokens/times come from phases / phases_ms (planner, coder, critic,
    pre_explore). A direct/chat run has no phase split and is attributed to the
    "direct" role with the whole run.

    `day` restricts the aggregation to one YYYY-MM-DD entry (the UI's Daily view).
    `speeds` is the optional fallback table {tag: {"tps": float, ...}} built from
    the llama-server logs (deploy/backfill_speeds.py): records without phase
    timing then still show a measured decode rate, flagged speed_source="log".

    completed_share excludes USER-DRIVEN stops from the denominator (abort and
    graceful_stop): stopping a run yourself is not a failure of the model.

    tps = timed_tokens / seconds, i.e. only runs that carry a duration contribute.
    Dividing *all* tokens by partial seconds produced 400+ tok/s nonsense for
    records without phase timing; those rows report tps = 0 (UI shows a dash).

    Roles that were configured but never produced work (no tokens, no seconds)
    are skipped - they only add noise.
    """
    rows: dict[tuple[str, str], dict] = {}
    _days = [day] if day and day in daily else sorted(daily)
    for _day in _days:
        for run in (daily[_day].get("run_list") or []):
            models = run.get("models") or {}
            phases = run.get("phases") or {}
            ms = run.get("phases_ms") or {}
            _seen = set()
            for role in _ROLES:
                val = models.get(role)
                if isinstance(val, list):
                    val = val[0] if val else None
                if not val:
                    continue
                tag = str(val)
                if (role, tag) in _seen:
                    continue
                _seen.add((role, tag))
                row = rows.setdefault((role, tag), {
                    "model": tag, "role": role, "runs": 0, "tokens": 0,
                    "timed_tokens": 0, "timed_runs": 0, "seconds": 0.0,
                    "stop": {}, "aborted": 0, "graceful": 0, "excluded": 0,
                    "last": "", "estimated": False,
                })
                row["runs"] += 1
                _tok = int(phases.get(role) or 0)
                _sec = float(ms.get(role) or 0) / 1000.0
                if role == "direct":
                    _tok = int(run.get("tokens") or 0)
                    # elapsed includes the model load of a cold chat run, so a
                    # rate from it would be nonsense (0.2 tok/s for 19 tokens).
                    # Show tokens + duration, no tok/s for this role.
                    _sec = 0.0
                elif role == "coder" and not _tok:
                    # record without a phase split: run total for the SIZE only
                    _tok = int(run.get("tokens") or 0)
                    row["estimated"] = True
                if _tok <= 0 and _sec <= 0:
                    # Rolle war nur konfiguriert (z.B. critic in 70 duo-Runs ohne
                    # eine einzige Runde) - nicht als Run mitzaehlen
                    row["runs"] -= 1
                    if row["runs"] <= 0 and not row["tokens"] and not row["seconds"]:
                        rows.pop((role, tag), None)
                    continue
                row["tokens"] += _tok
                if _sec > 0:
                    row["seconds"] += _sec
                    row["timed_tokens"] += _tok
                    row["timed_runs"] += 1
                sr = str(run.get("stop_reason") or "completed")
                row["stop"][sr] = row["stop"].get(sr, 0) + 1
                if sr == "aborted":
                    row["aborted"] += 1
                elif sr == "graceful_stop":
                    row["graceful"] += 1
                row["last"] = max(row["last"], str(run.get("t") or ""))
    out = []
    for row in rows.values():
        if not row["tokens"] and not row["seconds"]:
            continue
        _sec = row["seconds"]
        row["tps"] = round(row["timed_tokens"] / _sec, 1) if _sec and row["timed_tokens"] else 0.0
        if not row["tps"] and speeds:
            _sp = speeds.get(row["model"]) or {}
            _log_tps = float(_sp.get("tps") or 0)
            if _log_tps > 0:
                row["tps"] = round(_log_tps, 1)
                row["speed_source"] = "log"
                row["speed_samples"] = int(_sp.get("samples") or 0)
        row["excluded"] = row["aborted"] + row["graceful"]   # you stopped it
        _judged = row["runs"] - row["excluded"]
        row["completed_share"] = round(
            row["stop"].get("completed", 0) / _judged, 2) if _judged > 0 else 0.0
        out.append(row)
    out.sort(key=lambda r: (-(r["tokens"] or 0), -(r["runs"] or 0)))
    return out


def _estimate_tokens_from_content(content: str) -> int:
    """Estimate token count from streamed content (chars/3 heuristic)."""
    if not content:
        return 0
    _cjk = sum(1 for c in content if '\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u309f' or '\u30a0' <= c <= '\u30ff')
    if _cjk > len(content) * 0.3:
        return max(1, len(content) // 2)
    return max(1, len(content) // 3)

def aggregate_by_mode(daily: dict) -> list[dict]:
    """Runs je Route (code_duo / direct / pipeline), mit Tokens und Erfolgsquote.

    `mode` ist seit 2026-10-20 die tatsaechlich gelaufene Route; aeltere Records
    tragen nur den UI-Modus oder gar nichts -> "unrecorded".
    """
    rows: dict[str, dict] = {}
    for _day in sorted(daily):
        for run in (daily[_day].get("run_list") or []):
            key = str(run.get("mode") or "unrecorded")
            row = rows.setdefault(key, {
                "mode": key, "runs": 0, "tokens": 0, "timed_tokens": 0,
                "seconds": 0.0, "elapsed_s": 0.0, "stop": {}, "excluded": 0,
            })
            row["runs"] += 1
            _tok = int(run.get("tokens") or 0)
            row["tokens"] += _tok
            row["elapsed_s"] += float(run.get("elapsed_s") or 0)
            _ph = run.get("phases") or {}
            _pm = run.get("phases_ms") or {}
            _sec, _timed = 0.0, 0
            for _role, _v in _ph.items():
                if _role in _ROLES and float(_pm.get(_role) or 0) > 0:
                    _sec += float(_pm.get(_role) or 0) / 1000.0
                    _timed += int(_v or 0)
            if _sec > 0:
                row["seconds"] += _sec
                row["timed_tokens"] += _timed
            sr = str(run.get("stop_reason") or "completed")
            row["stop"][sr] = row["stop"].get(sr, 0) + 1
            if sr in ("aborted", "graceful_stop"):
                row["excluded"] += 1
    out = []
    for row in rows.values():
        _judged = row["runs"] - row["excluded"]
        row["completed_share"] = round(
            row["stop"].get("completed", 0) / _judged, 2) if _judged > 0 else 0.0
        row["tps"] = round(row["timed_tokens"] / row["seconds"], 1) if row["seconds"] and row["timed_tokens"] else 0.0
        row["avg_elapsed_s"] = round(row["elapsed_s"] / row["runs"], 1) if row["runs"] else 0.0
        out.append(row)
    out.sort(key=lambda r: -(r["runs"] or 0))
    return out
