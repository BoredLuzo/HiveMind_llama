# -*- coding: utf-8 -*-
"""LlamaPrefetchMixin — Methoden aus LlamaServerManager extrahiert (M3c)."""
from __future__ import annotations

from .llama_slots import ModelSlot
from .llama_manager_utils import (
    CanFitResult, _WIN_CNF, VRAM_BUDGET_GB,
    LLAMA_STARTUP_READY_TIMEOUT_SECONDS,
    _OLLAMA_ONLY_BASES, _MMPROJ_REQUIRED_BASES, _VISION_CAPABLE_BASES,
    _VRAM_BASE_OVERHEAD_GB, _VRAM_PRE_FLIGHT_GRACE_S,
    VRAMPreFlightError, _available_ram_gb, _kill_slot_async,
    _needs_mmproj, _gguf_path_to_model_name,
    _probe_binary_build, _probe_kv_flag, _probe_moe_flag,
    _probe_device_flag, _probe_backend_devices, _probe_backend_dlls,
    _prefetch_key, _tcp_alive, _kill_port_sync, _nm,
)
import asyncio
import httpx
import logging
logger = logging.getLogger("llama_manager")

class LlamaPrefetchMixin:

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
        """Background-Task: startet llama-server und setzt _ready_event.

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
