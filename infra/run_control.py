"""Run control: abort/skip registries (extracted from server.py)."""
from __future__ import annotations
import asyncio, logging, threading, time

logger = logging.getLogger("hivemind.run_control")

_chats_cache = {}
_cache_lock = threading.Lock()

_chat_abort_registry: dict[str, asyncio.Event] = {}
_chat_abort_lock: asyncio.Lock | None = None

_run_abort_registry: dict[str, asyncio.Event] = {}
_run_abort_registry_ts: dict[str, float] = {}

_step_skip_registry: dict[str, asyncio.Event] = {}

def _get_abort_lock() -> asyncio.Lock:
    """Lazy-Init: asyncio.Lock erst beim ersten Aufruf erzeugen (Python 3.12 safe)."""
    global _chat_abort_lock
    if _chat_abort_lock is None:
        _chat_abort_lock = asyncio.Lock()
    return _chat_abort_lock

def init_run_control(chats_cache=None, cache_lock=None,
                     abort_registry=None, abort_lock=None,
                     run_abort_registry=None, run_abort_registry_ts=None,
                     step_skip_registry=None):
    global _chats_cache, _cache_lock, _chat_abort_registry, _chat_abort_lock
    global _run_abort_registry, _run_abort_registry_ts, _step_skip_registry
    if chats_cache is not None:
        _chats_cache = chats_cache
    if cache_lock is not None:
        _cache_lock = cache_lock
    if abort_registry is not None:
        _chat_abort_registry = abort_registry
    if abort_lock is not None:
        _chat_abort_lock = abort_lock
    if run_abort_registry is not None:
        _run_abort_registry = run_abort_registry
    if run_abort_registry_ts is not None:
        _run_abort_registry_ts = run_abort_registry_ts
    if step_skip_registry is not None:
        _step_skip_registry = step_skip_registry
 
async def _get_abort_event(chat_id: str) -> asyncio.Event:
    async with _get_abort_lock():
        if chat_id not in _chat_abort_registry:
            _chat_abort_registry[chat_id] = asyncio.Event()
        return _chat_abort_registry[chat_id]
 
async def _clear_abort_event(chat_id: str):
    async with _get_abort_lock():
        _ev = _chat_abort_registry.get(chat_id)
        if _ev is None:
            _chat_abort_registry[chat_id] = asyncio.Event()
        else:
            # FIX: do NOT replace the event object, clear it in place.
            # Previously an interim ev.set() (stop click) got lost,
            # because the old object was replaced by a fresh unset one.
            _ev.clear()
 
def _is_aborted(chat_id: str) -> bool:
    ev = _chat_abort_registry.get(chat_id)
    return ev is not None and ev.is_set()

async def _cleanup_abort_registry():
    try:
        def _get_known_ids():
            with _cache_lock:
                return set(_chats_cache.keys())
        known_ids = await asyncio.to_thread(_get_known_ids)
        async with _get_abort_lock():
            stale = [cid for cid in list(_chat_abort_registry)
                     if cid not in known_ids
                     and not _chat_abort_registry[cid].is_set()]
            for cid in stale:
                del _chat_abort_registry[cid]
        if stale:
            logger.debug("_abort_registry cleanup: %d orphaned events removed", len(stale))
    except Exception:
        pass

async def _abort_registry_cleanup_loop():
    """Periodic abort-registry cleanup (every 5 minutes)."""
    while True:
        await asyncio.sleep(300)
        await _cleanup_abort_registry()


# ═══════════════════════════════════════════════════════════════════════════════
#  Pause/Resume — Human-in-the-Loop
# ═══════════════════════════════════════════════════════════════════════════════

_pause_events: dict[str, asyncio.Event] = {}
_user_answers: dict[str, str] = {}
_user_questions: dict[str, str] = {}


async def initiate_pause(run_id: str, question: str):
    _user_questions[run_id] = question
    _pause_events[run_id] = asyncio.Event()


async def wait_for_resume(run_id: str, timeout_s: int = 600) -> str:
    event = _pause_events.get(run_id)
    if not event:
        return "[ask_user ERROR: no pause event]"
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        cleanup_pause(run_id)
        return f"[ask_user TIMEOUT: no response after {timeout_s}s]"
    return _user_answers.pop(run_id, "")


def set_user_answer(run_id: str, answer: str):
    event = _pause_events.get(run_id)
    if not event:
        _user_answers.pop(run_id, None)
        return
    _user_answers[run_id] = answer
    event.set()


def get_question(run_id: str) -> str | None:
    return _user_questions.get(run_id)


def cleanup_pause(run_id: str):
    # PAUSE-WAKE (2026-09-02): signal any pending wait_for_resume BEFORE removing
    # the event. If a run stops/aborts while ask_user is waiting (e.g. loop_detect
    # hard-stop or an external cancel), the waiter would otherwise hang or the
    # task would be orphaned while the UI showed "run cancelled" after a question.
    _ev = _pause_events.get(run_id)
    if _ev is not None:
        try:
            _ev.set()
        except Exception:
            pass
    _pause_events.pop(run_id, None)
    _user_answers.pop(run_id, None)
    _user_questions.pop(run_id, None)


# -- Run-Abort Registry (per-run_id, not chat_id) --

def _register_step_skip(run_id: str) -> asyncio.Event:
    ev = asyncio.Event()
    _step_skip_registry[run_id] = ev
    return ev


def _step_skip_event(run_id: str) -> asyncio.Event | None:
    return _step_skip_registry.get(run_id)


def _clear_step_skip(run_id: str):
    ev = _step_skip_registry.get(run_id)
    if ev:
        ev.clear()


def _unregister_step_skip(run_id: str):
    _step_skip_registry.pop(run_id, None)


def _register_abort(run_id: str) -> asyncio.Event:
    now = time.time()
    stale = [rid for rid, ts in _run_abort_registry_ts.items() if now - ts > 7200]
    for rid in stale:
        _run_abort_registry.pop(rid, None)
        _run_abort_registry_ts.pop(rid, None)
    ev = asyncio.Event()
    _run_abort_registry[run_id] = ev
    _run_abort_registry_ts[run_id] = now
    return ev


def _unregister_abort(run_id: str):
    _run_abort_registry.pop(run_id, None)
    _run_abort_registry_ts.pop(run_id, None)


def _abort_event(run_id: str) -> asyncio.Event | None:
    return _run_abort_registry.get(run_id)


# -- Graceful Stop Registry (per-run_id) --
_graceful_stop_flags: dict[str, bool] = {}


async def request_graceful_stop(run_id: str) -> None:
    _graceful_stop_flags[run_id] = True
    logger.info("[graceful_stop] requested for run_id=%s", run_id)


def is_graceful_stop_requested(run_id: str) -> bool:
    return _graceful_stop_flags.get(run_id, False)


def clear_graceful_stop(run_id: str) -> None:
    _graceful_stop_flags.pop(run_id, None)


def is_pause_pending(run_id: str) -> bool:
    return is_pause_requested(run_id) or run_id in _RESUME_SIGNALS


# -- Manual Pause Registry (per-run_id) --
_PAUSE_AFTER_CHUNK_FLAGS: dict[str, bool] = {}
_RESUME_SIGNALS: dict[str, asyncio.Event] = {}
_ABORT_DURING_PAUSE_SIGNALS: dict[str, asyncio.Event] = {}


async def request_pause_after_chunk(run_id: str) -> None:
    _PAUSE_AFTER_CHUNK_FLAGS[run_id] = True
    logger.info("[pause] requested for run_id=%s", run_id)


def is_pause_requested(run_id: str) -> bool:
    return _PAUSE_AFTER_CHUNK_FLAGS.get(run_id, False)


def clear_pause_request(run_id: str) -> None:
    _PAUSE_AFTER_CHUNK_FLAGS.pop(run_id, None)


def get_resume_signal(run_id: str) -> asyncio.Event:
    if run_id not in _RESUME_SIGNALS:
        _RESUME_SIGNALS[run_id] = asyncio.Event()
    return _RESUME_SIGNALS[run_id]


def signal_resume(run_id: str) -> bool:
    sig = _RESUME_SIGNALS.get(run_id)
    if sig is None:
        return False
    sig.set()
    return True


def get_abort_during_pause_signal(run_id: str) -> asyncio.Event:
    if run_id not in _ABORT_DURING_PAUSE_SIGNALS:
        _ABORT_DURING_PAUSE_SIGNALS[run_id] = asyncio.Event()
    return _ABORT_DURING_PAUSE_SIGNALS[run_id]


def signal_abort_during_pause(run_id: str) -> bool:
    sig = _ABORT_DURING_PAUSE_SIGNALS.get(run_id)
    if sig is None:
        return False
    sig.set()
    return True


def cleanup_pause_state(run_id: str) -> None:
    _PAUSE_AFTER_CHUNK_FLAGS.pop(run_id, None)
    _RESUME_SIGNALS.pop(run_id, None)
    _ABORT_DURING_PAUSE_SIGNALS.pop(run_id, None)

