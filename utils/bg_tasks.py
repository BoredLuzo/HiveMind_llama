"""Background tasks that cannot be garbage-collected.

`asyncio.create_task(coro)` without keeping the returned object is a documented
footgun: the event loop only holds a weak reference, so the task may vanish
mid-flight and its work is silently lost. `spawn()` keeps a strong reference
until the task finishes.

Usage:  from utils.bg_tasks import spawn
        spawn(coro, name="skill-distill")
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("hivemind.bg_tasks")

_TASKS: set[asyncio.Task] = set()


def spawn(coro, *, name: str | None = None) -> asyncio.Task:
    """Schedule `coro` and keep it alive until it finishes."""
    task = asyncio.create_task(coro, name=name) if name else asyncio.create_task(coro)
    _TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        _TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning("background task %s failed: %s: %s",
                           t.get_name(), type(exc).__name__, exc)

    task.add_done_callback(_done)
    return task


def pending() -> list[asyncio.Task]:
    """Live tasks, for tests and shutdown diagnostics."""
    return list(_TASKS)


async def drain(timeout: float = 5.0) -> None:
    """Wait for the currently scheduled tasks (tests, clean shutdown)."""
    tasks = list(_TASKS)
    if not tasks:
        return
    await asyncio.wait(tasks, timeout=timeout)
