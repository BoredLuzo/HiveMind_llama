"""Round-level telemetry for the agentic coder loop (2026-10-09).

One JSONL line per LLM call into live/logs/telemetry/YYYYMMDD-<model>.jsonl.
Append-only, day-rotated by filename, never read back by the engine.
Purpose: measure prefill (prompt_ms/prompt_tps), decode (decode_tps),
cache health (cached_tokens), waiting (ttfb_ms) and prefix divergence
(first_diff_idx) per call, so the two-parameter prefill model
dur = a*new + b*(p^2-c^2)/2000 can be re-fitted continuously.
Every entry point swallows its own failures - the loop must not break
because telemetry is broken.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "telemetry"
_COMPRESSION: dict = {"n": 0, "ts": ""}
_last_hashes: list | None = None


def bump_compression() -> None:
    """Call once per completed context compression."""
    _COMPRESSION["n"] += 1
    _COMPRESSION["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")


def compression_id() -> int:
    return _COMPRESSION["n"]


def message_hashes(msgs) -> tuple:
    """sha1[:10] per message + index of first divergence vs the previous request.

    first_diff_idx = -1 means "no previous request in this process".
    -2 means "prefix identical" (divergence only by appended messages is 0-based
    at the append point).
    """
    global _last_hashes
    hs = []
    for m in msgs or []:
        try:
            hs.append(hashlib.sha1(
                (str(m.get("role", "")) + "\x00" + str(m.get("content", "")))
                .encode("utf-8", "replace")).hexdigest()[:10])
        except Exception:
            hs.append("?")
    first_diff = -1
    if _last_hashes is not None:
        first_diff = -2
        for i in range(max(len(hs), len(_last_hashes))):
            if i >= len(hs) or i >= len(_last_hashes) or hs[i] != _last_hashes[i]:
                first_diff = i
                break
    _last_hashes = hs
    return hs, first_diff


def append_round(row: dict) -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        mdl = str(row.get("model") or "unknown").replace(":", "_").replace("/", "_")
        path = _LOG_DIR / f"{time.strftime('%Y%m%d')}-{mdl}.jsonl"
        out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **row}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── DEBUG-DUMP (request instrumentation) ─────────────────────────────────────
def should_dump(settings) -> bool:
    """True only when Setting debug_dump_request is set (None/absent = off)."""
    try:
        return bool((settings or {}).get("debug_dump_request"))
    except Exception:
        return False


def dump_request(role: str, chat_id, messages, tools=None,
                 extra: dict | None = None) -> str | None:
    """Write messages+tools EXACTLY as sent to
    dump_<role>_<chatid>_<ts>.json in the telemetry dir. Never raises."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if (c.isalnum() or c in "-_.") else "_"
                       for c in str(chat_id or "nochat"))[:60]
        path = _LOG_DIR / f"dump_{role}_{safe}_{time.strftime('%Y%m%d-%H%M%S')}.json"
        out = {"role": role, "chat_id": str(chat_id or ""),
               "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "messages": messages, "tools": tools}
        if extra:
            out.update(extra)
        path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return str(path)
    except Exception:
        return None
