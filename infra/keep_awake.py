# -*- coding: utf-8 -*-


from __future__ import annotations

import ctypes
import logging
import sys
import threading

logger = logging.getLogger("hivemind.keep_awake")

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

_lock = threading.Lock()
_refs = 0
_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None


def enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        from settings import load_settings
        return bool(load_settings().get("keep_awake_during_run", True))
    except Exception:
        return True


def _es_set(flags: int) -> bool:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.SetThreadExecutionState.restype = ctypes.c_uint32
    k.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
    return bool(k.SetThreadExecutionState(flags))


def _hold_thread(stop: threading.Event):
    _es_set(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
    stop.wait()
    _es_set(_ES_CONTINUOUS)


def refs() -> int:
    with _lock:
        return _refs


def acquire() -> int:
    global _refs, _thread, _stop_event
    with _lock:
        _refs += 1
        if _thread is not None or not enabled():
            return _refs
        _stop_event = threading.Event()
        _thread = threading.Thread(
            target=_hold_thread, args=(_stop_event,),
            daemon=True, name="hivemind-keep-awake")
        _thread.start()
        logger.info("[KEEP-AWAKE] on (refs=%d)", _refs)
    return _refs


def release() -> int:
    global _refs, _thread, _stop_event
    with _lock:
        _refs = max(0, _refs - 1)
        if _refs > 0 or _thread is None:
            return _refs
        stop, thread = _stop_event, _thread
        _stop_event, _thread = None, None
    try:
        stop.set()
        thread.join(timeout=2)
    except Exception:
        pass
    logger.info("[KEEP-AWAKE] off")
    return _refs
