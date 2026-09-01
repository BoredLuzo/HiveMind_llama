from __future__ import annotations
import json, logging, time
from pathlib import Path
from utils.file import write_json_atomic

logger = logging.getLogger("hivemind.pause_state")
_SESSIONS_DIR: Path | None = None


def init_pause_state(sessions_dir):
    global _SESSIONS_DIR
    _SESSIONS_DIR = Path(sessions_dir)


def _pause_path(chat_id: str) -> Path | None:
    if not chat_id or not _SESSIONS_DIR:
        return None
    return _SESSIONS_DIR / f".{chat_id}.pause_state.json"


def persist_pause_state(chat_id: str, run_id: str, chunks_done: int,
                        chunks_remaining: int, written_files: list[str]) -> None:
    p = _pause_path(chat_id)
    if not p:
        return
    write_json_atomic(p, {
        "run_id": run_id,
        "chat_id": chat_id,
        "chunks_done": chunks_done,
        "chunks_remaining": chunks_remaining,
        "written_files": written_files,
        "ts": time.time(),
    })
    logger.info("[pause_state] persisted for chat=%s run=%s", chat_id, run_id)


def load_pause_state(chat_id: str) -> dict | None:
    p = _pause_path(chat_id)
    if not p or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_pause_state(chat_id: str) -> None:
    p = _pause_path(chat_id)
    if p and p.exists():
        try:
            p.unlink()
        except Exception:
            pass
