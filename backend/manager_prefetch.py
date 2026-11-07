# -*- coding: utf-8 -*-
"""LlamaPrefetchMixin — Methoden aus LlamaServerManager extrahiert (M3c)."""
from __future__ import annotations

from .llama_slots import ModelSlot
from .llama_manager_utils import (
    VRAM_BUDGET_GB,  # noqa: F401 -- runtime_sync live-syncs this attr (hasattr-gated)
    _kill_slot_async,
    _prefetch_key,
)
import asyncio
import httpx
import logging
from typing import TYPE_CHECKING
logger = logging.getLogger("llama_manager")

class LlamaPrefetchMixin:

    if TYPE_CHECKING:
        # Cross-mixin contract: these attributes/methods live on sibling
        # mixins and are bound in LlamaServerManager.__init__. Declared here
        # (types only, no runtime effect) so this mixin can be type-checked
        # in isolation.
        _telemetry: dict[str, int]
        _pending_prefetch: list[tuple[str, int]]
        _prefetch_tasks: set[asyncio.Task]
        _slots: list[ModelSlot]
        _shared_http_client: httpx.AsyncClient | None

        async def _start_process(self, slot: ModelSlot, model: str, num_ctx: int,
                                 vision: bool = False, n_parallel: int = 1,
                                 gpu_layers_override: int | None = None,
                                 ctx_graceful: bool = True,
                                 moe_cache: bool = True) -> None: ...

    def _metric_inc(self, key: str, amount: int = 1):
        self._telemetry[key] = int(self._telemetry.get(key, 0)) + int(amount)

    def telemetry_snapshot(self) -> dict:
        self._prune_prefetch_tasks()
        snap = dict(self._telemetry)
        snap["prefetch_pending"] = len(self._pending_prefetch)
        snap["prefetch_active"] = len([t for t in self._prefetch_tasks if not t.done()])
        snap["slots_running"] = len([s for s in self._slots if s.is_running])
        snap["slots_loading"] = len([s for s in self._slots if s._loading])
        return snap

    def _prune_prefetch_tasks(self):
        done = [t for t in self._prefetch_tasks if t.done()]
        for t in done:
            self._prefetch_tasks.discard(t)

    def _has_active_prefetch(self, model: str, num_ctx: int) -> bool:
        key = _prefetch_key(model, num_ctx)
        self._prune_prefetch_tasks()
        for t in self._prefetch_tasks:
            if t.done():
                continue
            if getattr(t, "_prefetch_key", None) == key:
                return True
        return False

    def _queue_contains_prefetch(self, model: str, num_ctx: int) -> bool:
        key = _prefetch_key(model, num_ctx)
        return any(_prefetch_key(m, c) == key for m, c in self._pending_prefetch)

    def _schedule_prefetch_task(self, slot: ModelSlot, model: str, num_ctx: int):
        """Track prefetch tasks so exceptions are visible and shutdown can cancel cleanly."""
        task = asyncio.create_task(self._prefetch_task(slot, model, num_ctx))
        setattr(task, "_prefetch_key", _prefetch_key(model, num_ctx))

        def _done(t: asyncio.Task):
            self._prefetch_tasks.discard(t)
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                logger.info("Prefetch task cancelled: %s", model)
                return
            except Exception:
                return
            if exc is not None:
                logger.warning("Prefetch task crashed for %s: %s", model, exc)

        self._prefetch_tasks.add(task)
        task.add_done_callback(_done)

    def _get_shared_http_client(self) -> httpx.AsyncClient:
        """Shared client for health/probe polling to avoid per-call client setup overhead."""
        if self._shared_http_client is None or self._shared_http_client.is_closed:
            self._shared_http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._shared_http_client

    async def _prefetch_task(self, slot: ModelSlot, model: str, num_ctx: int):
        """Background task: starts llama-server and sets _ready_event.

        PRE-EXPLORE-GUARD (Fix A):
        """
        try:
            await self._start_process(slot, model, num_ctx)
            logger.info(f"Prefetch finished: {model} on port {slot.port}")
        except asyncio.CancelledError:
            logger.info("Prefetch aborted: %s", model)
            raise
        except Exception as e:
            logger.warning(f"Prefetch failed for {model}: {e}")
            self._metric_inc("prefetch_failures")
            await _kill_slot_async(slot)
