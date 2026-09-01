# -*- coding: utf-8 -*-
"""LlamaEvictMixin — Methoden aus LlamaServerManager extrahiert (M3c)."""
from __future__ import annotations

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
from .llama_slots import ModelSlot
from typing import Optional
from .llama_models import resolve_model_path, list_available_models, resolve_mmproj_path, _strip_alias
from .llama_vram_table import vram_of as _vram_of, vram_of_with_ctx as _vram_of_ctx, vram_of_moe, VRAM_OVERFLOW_MODELS, _MOE_TABLE, get_live_gpu_free_mib, wait_for_vram_reclaim, TOTAL_VRAM_MIB
import asyncio
import time
import logging
logger = logging.getLogger("llama_manager")

class LlamaEvictMixin:

    async def evict(self, model: str):
        async with self._lock:
            slot = await self._find_loaded(model)
            if slot:
                logger.info(f"Evicting {model} from slot {slot.slot_id}")
                self._metric_inc("evictions_total")
                self._metric_inc("evictions_manual")
                await _kill_slot_async(slot)
            else:
                _loaded_names = [s.model for s in self._slots if s.model]
                logger.warning(f"Evict: model '{model}' not found. Loaded slots: {_loaded_names}")

    async def evict_all_unpinned(self):
        async with self._lock:
            for slot in self._slots:
                if slot.is_running and not slot.pinned:
                    logger.info(f"Evicting unpinned {slot.model}")
                    self._metric_inc("evictions_total")
                    self._metric_inc("evictions_manual")
                    await _kill_slot_async(slot)

    async def _trigger_pending_prefetch(self):
        """Nach einem Evict: pending_prefetch FIFO-Queue abarbeiten solange VRAM+Slots reichen."""
        if any(getattr(s, 'pinned', False) for s in self._slots if s.is_running or s._loading):
            return
        if getattr(self, '_planner_critical_phase', False):
            return
        while self._pending_prefetch:
            model, num_ctx = self._pending_prefetch[0]
            if self._has_active_prefetch(model, num_ctx):
                self._pending_prefetch.pop(0)
                continue
            current_vram   = sum(_vram_of(s.model) for s in self._slots
                                 if (s.is_running or s._loading) and s.model)
            if current_vram + _vram_of(model) > VRAM_BUDGET_GB:
                break
            slot = await self._find_free_slot()
            if slot is None:
                break
            self._pending_prefetch.pop(0)
            slot._loading = True
            slot.model    = model
            self._metric_inc("prefetch_dequeued")
            self._schedule_prefetch_task(slot, model, num_ctx)

    def can_fit(self, model_name: str, num_ctx: int, safety_margin_mib: int = 768,
                exclude_slot_id: Optional[int] = None) -> CanFitResult:


        needed_mib = round(vram_of_moe(_strip_alias(model_name), num_ctx) * 1024, 1)
        free_mib = get_live_gpu_free_mib()
        source = "live"
        if free_mib is None:
            _committed_gb = sum(
                vram_of_moe(_strip_alias(s.model), s._num_ctx if s._num_ctx > 0 else 4096)
                for s in self._slots
                if s.model and (exclude_slot_id is None or s.slot_id != exclude_slot_id)
            )
            free_mib = round((VRAM_BUDGET_GB - _committed_gb) * 1024, 1)
            source = "formula-fallback"
            logger.warning(
                "[can_fit] Live VRAM query not available — formula fallback "
                "(committed=%.1fGB, budget=%.1fGB → free=%.0fMiB)",
                _committed_gb, VRAM_BUDGET_GB, free_mib,
            )
        ok = (needed_mib + safety_margin_mib) <= free_mib
        return CanFitResult(ok, needed_mib, free_mib, safety_margin_mib, source)

    async def _pre_flight_grace_recheck(self, model_name: str, num_ctx: int,
                                        slot: ModelSlot, grace_s: float = _VRAM_PRE_FLIGHT_GRACE_S,
                                        poll_interval: float = 2.0) -> CanFitResult:


        _t0 = time.time()
        _fit = self.can_fit(model_name, num_ctx, exclude_slot_id=slot.slot_id)
        _deadline = _t0 + grace_s
        while not _fit.ok and time.time() < _deadline:
            await asyncio.sleep(poll_interval)
            _fit = self.can_fit(model_name, num_ctx, exclude_slot_id=slot.slot_id)
            if _fit.ok:
                logger.info(
                    "[PRE-FLIGHT-GRACE] %s @ctx=%d nach %.0fs frei (%.0fMiB) — weiter",
                    model_name, num_ctx, time.time() - _t0, _fit.free_mib,
                )
                return _fit
        if not _fit.ok:
            logger.warning(
                "[PRE-FLIGHT-GRACE] %s @ctx=%d bleibt nach %.0fs blockiert "
                "(frei=%.0fMiB) — Blockade endgueltig",
                model_name, num_ctx, grace_s, _fit.free_mib,
            )
        return _fit
