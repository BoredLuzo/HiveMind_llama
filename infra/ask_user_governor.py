"""Ask-User Governor — Timeout + Frequency Throttle for ask_user tool.

Timeout: Until-Finished runs auto-answer after N seconds if user doesn't respond.
Throttle: Hard-pause when agent fires > N ask_user events within 10 minutes.
"""
from __future__ import annotations
import asyncio
import logging
import time
from collections import deque

logger = logging.getLogger("hivemind.ask_user_governor")

_RUN_CONFIG: dict[str, dict] = {}
_ASK_USER_HISTORY: dict[str, deque] = {}
_TIMEOUT_TASKS: dict[str, asyncio.Task] = {}
_TIMEOUT_ANSWER_SENT: dict[str, bool] = {}
_THROTTLE_TRIGGERED: dict[str, bool] = {}


def configure_run(run_id: str, until_finished: bool, settings: dict) -> None:
    _RUN_CONFIG[run_id] = {
        "until_finished": until_finished,
        "timeout_s": int(settings.get("ask_user_timeout_until_finished_seconds", 300) or 300),
        "max_per_10min": int(settings.get("ask_user_max_per_10min", 5) or 5),
        "auto_answer": str(
            settings.get("ask_user_auto_answer",
                         "Use best judgment, document decision in commit message.") or
            "Use best judgment, document decision in commit message."
        ),
        "throttle_message": str(
            settings.get("ask_user_throttle_pause_message",
                         "Agent is asking too many questions \u2014 manual help required. "
                         "Please check the agent status and resume with clarification.") or
            "Agent is asking too many questions."
        ),
    }


def get_run_config(run_id: str) -> dict | None:
    return _RUN_CONFIG.get(run_id)


def record_ask_user(run_id: str) -> None:
    if run_id not in _ASK_USER_HISTORY:
        _ASK_USER_HISTORY[run_id] = deque(maxlen=20)
    _ASK_USER_HISTORY[run_id].append(time.time())


def check_throttle(run_id: str, max_per_10min: int) -> tuple[bool, int]:
    if max_per_10min <= 0:
        return False, 0
    now = time.time()
    history = _ASK_USER_HISTORY.get(run_id)
    if not history:
        return False, 0
    valid = [t for t in history if t > now - 600]
    _ASK_USER_HISTORY[run_id] = deque(valid, maxlen=20)
    return len(valid) > max_per_10min, len(valid)


def is_throttle_triggered(run_id: str) -> bool:
    return _THROTTLE_TRIGGERED.get(run_id, False)


def set_throttle_triggered(run_id: str) -> None:
    _THROTTLE_TRIGGERED[run_id] = True


def clear_throttle_triggered(run_id: str) -> None:
    _THROTTLE_TRIGGERED.pop(run_id, None)


def clear_throttle_state(run_id: str) -> None:


    _THROTTLE_TRIGGERED.pop(run_id, None)
    _ASK_USER_HISTORY.pop(run_id, None)


async def start_timeout(run_id: str, timeout_s: int, auto_answer: str) -> None:
    if timeout_s <= 0:
        return
    cancel_timeout(run_id)
    _TIMEOUT_ANSWER_SENT[run_id] = False

    async def _task():
        try:
            await asyncio.sleep(timeout_s)
            if not _TIMEOUT_ANSWER_SENT.get(run_id, False):
                _TIMEOUT_ANSWER_SENT[run_id] = True
                from infra.run_control import set_user_answer
                set_user_answer(run_id, auto_answer)
                logger.info("[ASK-USER-GOVERNOR] Timeout after %ds for %s — auto-answer sent",
                            timeout_s, run_id)
        except asyncio.CancelledError:
            pass

    _TIMEOUT_TASKS[run_id] = asyncio.create_task(_task())


def cancel_timeout(run_id: str) -> None:
    task = _TIMEOUT_TASKS.pop(run_id, None)
    if task and not task.done():
        task.cancel()


def is_timeout_answer_sent(run_id: str) -> bool:
    return _TIMEOUT_ANSWER_SENT.get(run_id, False)


def cleanup_governor(run_id: str) -> None:
    _RUN_CONFIG.pop(run_id, None)
    _ASK_USER_HISTORY.pop(run_id, None)
    cancel_timeout(run_id)
    _TIMEOUT_ANSWER_SENT.pop(run_id, None)
    _THROTTLE_TRIGGERED.pop(run_id, None)
