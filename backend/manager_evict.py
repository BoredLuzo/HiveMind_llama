# -*- coding: utf-8 -*-
"""LlamaEvictMixin — Methoden aus LlamaServerManager extrahiert (M3c)."""
from __future__ import annotations

from .llama_manager_utils import (
    CanFitResult, VRAM_BUDGET_GB,
    _VRAM_PRE_FLIGHT_GRACE_S,
    VRAM_PRE_FLIGHT_MARGIN_MIB,
    _kill_slot_async,
)
from .llama_slots import ModelSlot
from .llama_config import DEFAULT_IDLE_TIMEOUT_SECONDS, ubatch_extra_mib
from typing import Optional
from .llama_models import _strip_alias
from .llama_vram_table import vram_of as _vram_of, vram_of_moe, get_live_gpu_free_mib
import asyncio
import time
import logging
logger = logging.getLogger("llama_manager")

class LlamaEvictMixin:

    async def evict(self, model: str):
        async with self._lock:
            slot = await self._find_loaded(model, lock_held=True)
            if slot:
                logger.info(f"Evicting {model} from slot {slot.slot_id}")
                _mem_label = f"evict:{model}"
                self._metric_inc("evictions_total")
                self._metric_inc("evictions_manual")
                await _kill_slot_async(slot)
                try:
                    from utils.mem_watch import log_snapshot as _mem_snap
                    _mem_snap(_mem_label)
                except Exception:
                    pass
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

    def clear_pin(self, model: str) -> int:
        """Clear the pinned flag on every slot running `model` (2026-10-01).

        Duo runs pin their planner/coder slots so the vision-preprocessing
        load cannot LRU-evict them mid-run; callers unpin when the run ends
        so normal eviction can reclaim VRAM again.

        A9b (audit 2026-11-03): named `unpin` before, which shadowed the async
        LlamaHealthMixin.unpin in the MRO — `await manager.unpin(...)` awaited
        an int and the slot stayed pinned.
        """
        _n = 0
        for slot in self._slots:
            if slot.model == model and slot.pinned:
                slot.pinned = False
                slot.pinned_at = 0.0
                _n += 1
        return _n

    def unpin_all(self) -> int:
        """Run-start reset (2026-10-02): clear EVERY pin — a stale pin from
        a crashed previous run must never block eviction for the new run."""
        _n = 0
        for slot in self._slots:
            if slot.pinned:
                slot.pinned = False
                slot.pinned_at = 0.0
                _n += 1
        return _n

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
            slot._idle_timeout = DEFAULT_IDLE_TIMEOUT_SECONDS  # H-audit: prefetched models must age
            self._metric_inc("prefetch_dequeued")
            self._schedule_prefetch_task(slot, model, num_ctx)

    def can_fit(self, model_name: str, num_ctx: int,
                safety_margin_mib: int = VRAM_PRE_FLIGHT_MARGIN_MIB,
                exclude_slot_id: Optional[int] = None,
                extra_needed_mib: float = 0.0) -> CanFitResult:


        try:
            from .llama_config import GPU_BACKEND as _ce_gb
        except Exception:
            _ce_gb = ""
        if _ce_gb == "cpu":
            # CPU-BACKEND (2026-09-08): VRAM pre-flight is meaningless — model
            # + KV live in system RAM. Accept the load; a GPU-based check here
            # would block every load on a CPU-only host.
            return CanFitResult(True, 0.0, 0.0, 0.0, "cpu-backend")
        # UBATCH-COMPUTE-BUFFER (2026-10-30): the table has no ubatch term;
        # the measured surcharge lives in llama_config.ubatch_extra_mib (one
        # definition, shared with the eviction heuristic). This is the gate
        # that can BLOCK a load, so the surcharge belongs here.
        try:
            from .llama_config import LLAMA_UBATCH as _ub_now
        except (ImportError, AttributeError):
            _ub_now = 256
        _ub_extra_mib = ubatch_extra_mib(int(_ub_now))
        needed_mib = round(vram_of_moe(_strip_alias(model_name), num_ctx) * 1024
                           + _ub_extra_mib + float(extra_needed_mib or 0.0), 1)
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
                                        poll_interval: float = 2.0,
                                        safety_margin_mib: int = VRAM_PRE_FLIGHT_MARGIN_MIB,
                                        progress_abort_s: float = 0.0,
                                        extra_needed_mib: float = 0.0) -> CanFitResult:
        # RECHECK-SURCHARGE (audit 2026-11-03): the grace recheck is the
        # gate that lets a blocked load proceed after eviction. Dropping the
        # MoE-cache surcharge here made it pass while the real command still
        # carried --moe-cache-mib -> VRAM overflow crash.

        _t0 = time.time()
        _fit = self.can_fit(model_name, num_ctx, exclude_slot_id=slot.slot_id,
                            safety_margin_mib=safety_margin_mib,
                            extra_needed_mib=extra_needed_mib)
        _deadline = _t0 + grace_s
        _first_free: float | None = _fit.free_mib
        _best_free: float | None = _first_free
        _aborted = False
        while not _fit.ok and time.time() < _deadline:
            await asyncio.sleep(poll_interval)
            _fit = self.can_fit(model_name, num_ctx, exclude_slot_id=slot.slot_id,
                                safety_margin_mib=safety_margin_mib,
                                extra_needed_mib=extra_needed_mib)
            if _fit.ok:
                logger.info(
                    "[PRE-FLIGHT-GRACE] %s @ctx=%d free after %.0fs (%.0fMiB, margin %dMiB) — continuing",
                    model_name, num_ctx, time.time() - _t0, _fit.free_mib,
                    safety_margin_mib,
                )
                return _fit
            if _fit.free_mib is not None:
                _best_free = _fit.free_mib if _best_free is None else max(_best_free, _fit.free_mib)
            # STALL-ABORT (2026-09-07): no release progress (>=64 MiB) ->
            # target structurally unreachable, abort early instead of waiting grace_s.
            if (progress_abort_s > 0.0 and _first_free is not None
                    and _best_free is not None
                    and time.time() - _t0 >= progress_abort_s
                    and _best_free < _first_free + 64):
                _aborted = True
                break
        if not _fit.ok:
            logger.warning(
                "[PRE-FLIGHT-GRACE] %s @ctx=%d still blocked after %.0fs "
                "(frei=%.0fMiB, Marge %dMiB)%s",
                model_name, num_ctx,
                time.time() - _t0 if _aborted else grace_s,
                _fit.free_mib, safety_margin_mib,
                " — abort (no release progress)" if _aborted else "",
            )
        return _fit
