


import logging
import threading
import time

logger = logging.getLogger("hivemind.notify")

APP_ID = "HiveMind"
_MIN_INTERVAL_S = 30.0
_DEDUP_WINDOW_S = 300.0

_last: dict = {"ts": 0.0, "sig": ""}
_lock = threading.Lock()


def _notifications_enabled() -> bool:
    """settings['desktop_notifications'] == false schaltet Toasts ab (still)."""
    try:
        from settings import load_settings
        return bool(load_settings().get("desktop_notifications", True))
    except Exception:
        return True


def _do_notify(title: str, message: str):
    try:
        from winotify import Notification
        toast = Notification(app_id=APP_ID, title=title, msg=message, duration="short")
        toast.show()
    except Exception as _e:
        logger.warning("[NOTIFY-fallback] %s — %s (%s)", title, message, _e)


def notify(title: str, message: str, dedup_sig: str = "") -> bool:
    """Feuert eine Notification ab. Rate-limited + Dedup. Non-blocking."""
    if not _notifications_enabled():
        return False
    now = time.time()
    with _lock:
        if now - _last["ts"] < _MIN_INTERVAL_S:
            return False
        if dedup_sig and dedup_sig == _last["sig"] and now - _last["ts"] < _DEDUP_WINDOW_S:
            return False
        _last["ts"] = now
        _last["sig"] = dedup_sig
    try:
        threading.Thread(target=_do_notify, args=(title, message), daemon=True).start()
        return True
    except Exception as _e:
        logger.warning("[NOTIFY] Start failed: %s", _e)
        return False


def notify_agent_needs_input(run_id: str, question: str) -> None:
    notify("HiveMind — question for you", f"[{run_id}] {question[:160]}", dedup_sig=f"ask:{run_id}")


def notify_run_stopped(stop_reason: str, summary: str) -> None:
    notify(f"HiveMind — run finished ({stop_reason})", summary[:200], dedup_sig=f"stop:{stop_reason}")


def notify_run_completed(summary: str) -> None:
    notify("HiveMind — run completed", summary[:200], dedup_sig="stop:completed")
