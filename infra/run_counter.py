"""Persistierter Run-Counter (aus server.py extrahiert)."""
from __future__ import annotations

import json, logging, threading
from pathlib import Path
from utils.file import write_json_atomic as _write_json_atomic

logger = logging.getLogger("hivemind.run_counter")
_run_counter_cache: int | None = None
_run_counter_lock: threading.RLock | None = None
_pipeline_run_counter_file: Path | None = None

def init_run_counter(counter_file=None, lock=None):
    global _pipeline_run_counter_file, _run_counter_lock
    if counter_file is not None:
        _pipeline_run_counter_file = Path(counter_file)
    if lock is not None:
        _run_counter_lock = lock

def _load_run_counter() -> int:
    """PHASE D: Lade Run-Counter aus run_counter.json mit Logging & Validation."""
    global _run_counter_cache
    with _run_counter_lock:
        if _run_counter_cache is not None:
            return _run_counter_cache
        if _pipeline_run_counter_file.exists():
            try:
                _data = json.loads(_pipeline_run_counter_file.read_text())
                _run_counter_cache = int(_data.get("count", 0))
                logger.debug(f"[RUN-COUNTER] loaded from disk: count={_run_counter_cache}")
                return _run_counter_cache
            except Exception as e:
                logger.warning(f"[RUN-COUNTER] corrupt or unreadable: {e} | reset to 0")
                _run_counter_cache = 0
                return 0
        logger.info("[RUN-COUNTER] first run | initializing counter")
        _run_counter_cache = 0
        return 0

def _increment_run_counter() -> int:
    """PHASE D: Inkrementiere Run-Counter mit atomic disk-write & Logging."""
    global _run_counter_cache
    with _run_counter_lock:
        count = _load_run_counter() + 1
        _run_counter_cache = count
        logger.info(f"[RUN-COUNTER] incremented: new_count={count}")

    def _persist_counter(v: int):
        try:
            _write_json_atomic(_pipeline_run_counter_file, {"count": int(v)})
            logger.debug(f"[RUN-COUNTER] persisted to disk: count={v}")
        except Exception as e:
            logger.error(f"[RUN-COUNTER] persist failed: {e}")

    threading.Thread(target=_persist_counter, args=(count,), daemon=True).start()
    return count

