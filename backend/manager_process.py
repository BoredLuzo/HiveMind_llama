# -*- coding: utf-8 -*-
"""LlamaProcessMixin — Methoden aus LlamaServerManager extrahiert (M3c)."""
from __future__ import annotations

from .llama_slots import ModelSlot
from typing import Optional
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
                # LOAD-DIED-GUARD (2026-08-31): Wenn der Ladeprozess bereits
                # gestorben ist, klemmt _loading auf True und _ready_event wird
                # nie gesetzt → ensure_loaded würde 240s auf ein totes Event
                # warten. Slot zurücksetzen und weitersuchen (Caller lädt neu).
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
                # Orphan-Slot: re-verify via TCP+API
                is_orphan = True
                self._lock.release()
                try:
                    api_ok = await self._verify_port_api(slot.port)
                finally:
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
                self._lock.release()
                try:
                    api_ok = await self._verify_port_api(slot.port)
                finally:
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
                        f"Port {slot.port} hat keine llama-API — "
                        f"verwerfe stale Slot fuer {model}"
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
        candidates = [s for s in self._slots if s is not exclude_slot and s.is_running and not s.pinned]
        if not candidates:
            candidates = [s for s in self._slots if s is not exclude_slot and not s.pinned and not s._loading]
        if not candidates:
            return None
        lru = min(candidates, key=lambda s: s.last_used)
        logger.info(f"LRU-Evicting {lru.model} (slot {lru.slot_id})")
        self._metric_inc("evictions_total")
        self._metric_inc("evictions_lru")
        lru.kill()
        return lru
