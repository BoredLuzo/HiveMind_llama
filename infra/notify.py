


import logging
import os
import sys
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


_TOAST_PS = (
    "$ErrorActionPreference = 'Stop'\n"
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null\n"
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null\n"
    "$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)\n"
    "$nodes = $tpl.GetElementsByTagName('text')\n"
    "$nodes.Item(0).AppendChild($tpl.CreateTextNode($env:HM_TOAST_TITLE)) > $null\n"
    "$nodes.Item(1).AppendChild($tpl.CreateTextNode($env:HM_TOAST_MSG)) > $null\n"
    "$toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)\n"
    "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:HM_TOAST_APPID)\n"
    "$notifier.Show($toast)\n"
)
_TOAST_PS_B64 = __import__("base64").b64encode(_TOAST_PS.encode("utf-16-le")).decode("ascii")


def _sanitize_toast_text(text: object, max_len: int = 240) -> str:
    """Normalize toast text by stripping control characters and bounding length."""
    cleaned = "".join(
        " " if ch in ("\r", "\n", "\t") else ("" if (ord(ch) < 32 or ord(ch) == 127) else ch)
        for ch in str(text or "")
    )
    collapsed = " ".join(cleaned.split())
    if max_len > 0 and len(collapsed) > max_len:
        return collapsed[:max_len]
    return collapsed


def _do_notify(title: str, message: str):
    """Display a desktop toast via a static -EncodedCommand PowerShell script and env vars."""
    import subprocess as _sp_mod
    import sys as _sys_mod
    from infra.child_env import scrubbed_env

    safe_title = _sanitize_toast_text(title, max_len=160)
    safe_msg = _sanitize_toast_text(message, max_len=400)
    if _sys_mod.platform != "win32":
        logger.warning("[NOTIFY-fallback] %s — %s", safe_title, safe_msg)
        return
    try:
        env = scrubbed_env(extra={
            "HM_TOAST_APPID": _sanitize_toast_text(APP_ID, max_len=128) or "HiveMind",
            "HM_TOAST_TITLE": safe_title,
            "HM_TOAST_MSG": safe_msg,
        })
        proc = _sp_mod.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                _TOAST_PS_B64,
            ],
            stdin=_sp_mod.DEVNULL,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=getattr(_sp_mod, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if proc.returncode != 0:
            err_tail = _sanitize_toast_text(proc.stderr or proc.stdout or f"rc={proc.returncode}", max_len=160)
            logger.warning("[NOTIFY-fallback] %s — %s (%s)", safe_title, safe_msg, err_tail)
    except (OSError, _sp_mod.SubprocessError) as _e:
        logger.warning("[NOTIFY-fallback] %s — %s (%s)", safe_title, safe_msg, _e)


def notify(title: str, message: str, dedup_sig: str = "") -> bool:
    """Feuert eine Notification ab. Rate-limited + Dedup. Non-blocking."""
    # OS-GUARD (2026-09-19): winotify ist Windows-only — auf POSIX stumm
    # aussteigen, statt pro Event eine ModuleNotFound-warning zu spammen.
    if sys.platform != "win32":
        return False
    if os.environ.get("HIVEMIND_NO_NOTIFY", "").strip().lower() in ("1", "true", "yes"):
        return False
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
    """Desktop toast for a waiting approval/question — but NOT for
    Telegram-started runs (2026-10-05): the phone surface (tappable
    card / gateway auto-deny) answers those within moments, and a
    Windows toast for an auto-answered approval is pure noise."""
    try:
        from tools.runner import _phone_source_run
    except ImportError:
        _phone_source_run = None
    if _phone_source_run is not None and _phone_source_run.get():
        return
    notify("HiveMind — question for you", f"[{run_id}] {question[:160]}", dedup_sig=f"ask:{run_id}")


def notify_run_stopped(stop_reason: str, summary: str) -> None:
    notify(f"HiveMind — run finished ({stop_reason})", summary[:200], dedup_sig=f"stop:{stop_reason}")


def notify_run_completed(summary: str) -> None:
    notify("HiveMind — run completed", summary[:200], dedup_sig="stop:completed")
