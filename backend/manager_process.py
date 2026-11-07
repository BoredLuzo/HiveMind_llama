# -*- coding: utf-8 -*-
"""LlamaProcessMixin — Methoden aus LlamaServerManager extrahiert (M3c)."""
from __future__ import annotations

from .llama_slots import ModelSlot
from typing import Optional
from .llama_manager_utils import (
    VRAM_BUDGET_GB,  # noqa: F401 -- runtime_sync live-syncs this attr (hasattr-gated)
    _kill_slot_async,
    _tcp_alive, _kill_port_sync, _nm,
)
import asyncio
import httpx
import time
import logging
logger = logging.getLogger("llama_manager")


def _slot_in_flight(slot) -> bool:
    """A9c: age-aware in-flight check (raw counter for test doubles).

    The producer has no finally; a mark older than ModelSlot.in_flight_active's
    max age is treated as leaked and no longer blocks eviction.
    """
    fn = getattr(slot, "in_flight_active", None)
    if callable(fn):
        return bool(fn())
    return int(getattr(slot, "in_flight", 0) or 0) > 0


class LlamaProcessMixin:

    async def _port_alive(self, port: int) -> bool:
        """Async-safe TCP-Check: laeuft im Threadpool, blockiert Event-Loop nicht."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _tcp_alive, port)

    async def _verify_port_api(self, port: int) -> bool:
        """TCP + HTTP /v1/models check in einem Call."""
        if not await self._port_alive(port):
            return False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=0.8, read=1.5, write=0.8, pool=0.8)) as c:
                r = await c.get(f"http://127.0.0.1:{port}/v1/models")
                return r.status_code == 200
        except Exception:
            return False

    async def _kill_port(self, port: int):
        if await self._port_alive(port):
            logger.info(f"Port {port} is in use - trying cleanup ...")
            await asyncio.to_thread(_kill_port_sync, port)
            await asyncio.sleep(0.5)

    def _find_loaded_sync(self, model: str) -> Optional[ModelSlot]:
        for slot in self._slots:
            if _nm(slot.model) == _nm(model) and (slot.is_running or slot._loading):
                return slot
        for slot in self._slots:
            if _nm(slot.model) == _nm(model):
                return slot
        return None

    async def _find_loaded(self, model: str) -> Optional[ModelSlot]:


        for slot in self._slots:
            if _nm(slot.model) != _nm(model):
                continue
            if slot._loading:
                # LOAD-DIED-GUARD (2026-08-31): If the load process has already
                # died, _loading stays True and _ready_event is never
                # set → ensure_loaded would wait 240s on a dead event.
                # Reset the slot and keep searching (caller reloads).
                if slot.process is not None and slot.process.poll() is not None:
                    logger.warning(
                        "ensure_loaded: load process for %s died (slot %s) - resetting slot.",
                        model, slot.slot_id,
                    )
                    await _kill_slot_async(slot)
                    continue
                return slot
            if slot.is_running:
                if slot._orphan_port is None:
                    return slot
                # Orphan-Slot: re-verify via TCP+API. LOCK-GUARD (audit
                # 2026-11-03): unlocked callers crashed on release() of an
                # unheld lock when the matched slot carried an orphan marker.
                _held = self._lock.locked()
                if _held:
                    self._lock.release()
                try:
                    api_ok = await self._verify_port_api(slot.port)
                finally:
                    if _held:
                        await self._lock.acquire()
                if _nm(slot.model) != _nm(model):
                    for s2 in self._slots:
                        if _nm(s2.model) == _nm(model) and (s2.is_running or s2._loading):
                            return s2
                    return None
                if api_ok:
                    if not slot._ready_event.is_set():
                        slot._ready_event.set()
                    return slot
                logger.warning(
                    f"Orphan slot stale: {model} on port {slot.port} no longer usable - discarding slot"
                )
                slot.model = None
                slot._orphan_port = None
                slot._loading = False
                slot._ready_event.clear()
        for slot in self._slots:
            if _nm(slot.model) == _nm(model) and not slot.is_running and not slot._loading:
                # LOCK-GUARD (audit 2026-11-03): see the orphan branch above.
                _held = self._lock.locked()
                if _held:
                    self._lock.release()
                try:
                    api_ok = await self._verify_port_api(slot.port)
                finally:
                    if _held:
                        await self._lock.acquire()
                if _nm(slot.model) != _nm(model):
                    for s2 in self._slots:
                        if _nm(s2.model) == _nm(model) and (s2.is_running or s2._loading):
                            return s2
                    return None
                if api_ok:
                    if not slot._ready_event.is_set():
                        slot._ready_event.set()
                    self._metric_inc("orphan_rehabilitations")
                    logger.info(f"Orphan slot rehabilitated: {model} on port {slot.port}")
                    return slot
                else:
                    logger.warning(
                        f"Port {slot.port} answers no llama API - "
                        f"discarding stale slot for {model}"
                    )
                    slot.model = None
                    slot._orphan_port = None
                    slot._ready_event.clear()
        return None

    async def _find_free_slot(self) -> Optional[ModelSlot]:
        for slot in self._slots:
            if not slot.is_running and not slot._loading:
                try:
                    if await self._port_alive(slot.port):
                        continue
                except Exception:
                    pass
                return slot
        return None

    def _evict_lru(self, exclude_slot: Optional[ModelSlot] = None) -> Optional[ModelSlot]:
        """Select the LRU victim. Does NOT kill it.

        KILL-OFF-LOOP (deep audit 2026-11-07): `ModelSlot.kill()` runs
        taskkill/terminate/wait (up to ~10 s) and used to be called from
        here - inside `async with self._lock`, freezing the whole event loop.
        Async callers must use `_evict_lru_async`; the sync selector stays
        for tests and lock-free callers.
        """
        # PIN EXPIRY (2026-10-02): a pin protects a slot for 30 minutes from
        # its pin time — hung pins (crashed runner that never unpinned) can
        # no longer block eviction forever. MONOTONIC (audit A3): pairs with
        # set_pinned's stamp.
        _now = time.monotonic()
        def _protected(s: "ModelSlot") -> bool:
            if not s.pinned:
                return False
            return (getattr(s, "pinned_at", 0.0) or 0.0) > _now - 1800
        candidates = [s for s in self._slots if s is not exclude_slot and s.is_running
                      and not _slot_in_flight(s)   # A9c: never kill a slot with a round in flight
                      and not getattr(s, "_loading", False) and not _protected(s)]  # H-audit: never kill a mid-load slot
        if not candidates:
            candidates = [s for s in self._slots if s is not exclude_slot and not s.pinned
                          and not _slot_in_flight(s)
                          and not s._loading]
        if not candidates:
            return None
        lru = min(candidates, key=lambda s: s.last_used)
        logger.info(f"LRU-Evicting {lru.model} (slot {lru.slot_id})")
        self._metric_inc("evictions_total")
        self._metric_inc("evictions_lru")
        return lru

    async def _evict_lru_async(
        self, exclude_slot: Optional[ModelSlot] = None
    ) -> Optional[ModelSlot]:
        """Select + kill the LRU victim without blocking the event loop."""
        victim = self._evict_lru(exclude_slot)
        if victim is not None:
            await _kill_slot_async(victim)
        return victim
