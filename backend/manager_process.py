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
import contextlib
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


def _probe_snapshot(slot) -> tuple:
    """F5-RACE (deep audit 2026-11-07): probe inputs for an orphan slot,
    captured while the manager lock is still held.

    The unlocked HTTP probe reads ONLY this snapshot (port + model + slot
    identity); the live slot is re-validated against it under the lock
    afterwards (`_probe_snapshot_changed`) before anything is mutated.
    """
    proc = slot.process
    dead = proc is not None and proc.poll() is not None
    return (slot.port, slot.model, proc, dead)


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

    def _rescan_active_slot(self, model: str) -> Optional[ModelSlot]:
        """Post-probe lookup: any slot that is active for `model` (running or
        claimed by an in-flight load), scanning the list from the start."""
        for s2 in self._slots:
            if _nm(s2.model) == _nm(model) and (s2.is_running or s2._loading):
                return s2
        return None

    def _probe_snapshot_changed(self, slot, model: str, snap: tuple) -> bool:
        """F5-RACE (deep audit 2026-11-07): re-validate a `_probe_snapshot`
        under the lock, AFTER the unlocked port probe of an orphan slot.

        While the lock was released, another coroutine (ensure_loaded under
        the lock) could claim or reset the very slot that was probed:
        `_loading = True`, `model` set/replaced, the process swapped or the
        slot pulled from the registry. Mutating such a slot clobbers the
        in-flight load (its ready wait then times out after 240 s), so every
        post-probe mutation is gated on this check: the slot is still the
        same registered object, same port, same model, still NOT `_loading`,
        and still the same process — still dead if it was dead at snapshot
        time. Any change means: do not mutate, continue the normal lookup.
        """
        snap_port, snap_model, snap_process, snap_dead = snap
        if not any(s is slot for s in self._slots):
            return True   # slot was removed/replaced in the registry meanwhile
        if slot.port != snap_port:
            return True
        if _nm(slot.model) != _nm(snap_model) or _nm(slot.model) != _nm(model):
            return True
        if slot._loading:
            return True   # claimed for an in-flight load during the probe
        if slot.process is not snap_process:
            return True   # a (re)load swapped the process during the probe
        if snap_process is not None and snap_dead and slot.process.poll() is None:
            return True   # snapshot-dead process no longer dead
        return False

    def _lock_ctx(self, *, lock_held: bool):
        """Return self._lock unless already held by caller or absent on test doubles."""
        if lock_held:
            return contextlib.nullcontext()
        lock = getattr(self, "_lock", None)
        return lock if lock is not None else contextlib.nullcontext()

    @contextlib.asynccontextmanager
    async def _unlocked_if_held(self, *, lock_held: bool):
        """Temporarily release self._lock when lock_held=True and re-acquire in finally."""
        lock = getattr(self, "_lock", None) if lock_held else None
        if lock is not None:
            lock.release()
        try:
            yield
        finally:
            if lock is not None:
                pending_cancel: asyncio.CancelledError | None = None
                while True:
                    try:
                        await lock.acquire()
                        break
                    except asyncio.CancelledError as exc:
                        pending_cancel = exc
                if pending_cancel is not None:
                    raise pending_cancel

    def _find_probe_candidate(
        self,
        model: str,
        checked_orphan: set[int],
        checked_rehab: set[int],
    ) -> tuple[str, object]:
        """Select the next matching slot or probe candidate under the manager lock."""
        for slot in self._slots:
            if _nm(slot.model) != _nm(model):
                continue
            if slot._loading:
                # LOAD-DIED-GUARD (2026-08-31): If the load process has already
                # died, _loading stays True and _ready_event is never
                # set -> ensure_loaded would wait 240s on a dead event.
                if slot.process is not None and slot.process.poll() is not None:
                    return (
                        "dead_loading",
                        (slot, slot.process, getattr(slot, "_lifecycle_gen", None)),
                    )
                return ("found", slot)
            if slot.is_running:
                if slot._orphan_port is None:
                    return ("found", slot)
                if id(slot) not in checked_orphan:
                    return ("orphan", (slot, _probe_snapshot(slot)))
        for slot in self._slots:
            if (
                _nm(slot.model) == _nm(model)
                and not slot.is_running
                and not slot._loading
                and id(slot) not in checked_rehab
            ):
                return ("rehab", (slot, _probe_snapshot(slot)))
        return ("none", None)

    def _apply_probe_result(
        self,
        kind: str,
        slot: ModelSlot,
        model: str,
        snap: tuple,
        api_ok: bool,
    ) -> tuple[bool, Optional[ModelSlot]]:
        """Validate snapshot under lock and apply orphan/rehab probe result."""
        if self._probe_snapshot_changed(slot, model, snap):
            # Slot claimed/changed during the probe window — leave it
            # untouched and continue the normal lookup path.
            return (True, self._rescan_active_slot(model))
        if kind == "orphan":
            if api_ok:
                if not slot._ready_event.is_set():
                    slot._ready_event.set()
                return (True, slot)
            logger.warning(
                f"Orphan slot stale: {model} on port {slot.port} no longer usable - discarding slot"
            )
            slot.model = None
            slot._orphan_port = None
            slot._loading = False
            slot._ready_event.clear()
            return (False, None)
        if api_ok:
            # ORPHAN-REGISTRY (deep audit 2026-11-08): mark the adopted
            # server so is_running/idle/LRU can manage it and the next
            # lookup does not probe again.
            slot._orphan_port = slot.port
            if not slot._ready_event.is_set():
                slot._ready_event.set()
            self._metric_inc("orphan_rehabilitations")
            logger.info(f"Orphan slot rehabilitated: {model} on port {slot.port}")
            return (True, slot)
        logger.warning(
            f"Port {slot.port} answers no llama API - "
            f"discarding stale slot for {model}"
        )
        slot.model = None
        slot._orphan_port = None
        slot._ready_event.clear()
        return (False, None)

    async def _find_loaded(self, model: str, *,
                           lock_held: bool = False) -> Optional[ModelSlot]:
        checked_orphan: set[int] = set()
        checked_rehab: set[int] = set()
        checked_dead: set[int] = set()
        while True:
            async with self._lock_ctx(lock_held=lock_held):
                action, payload = self._find_probe_candidate(
                    model, checked_orphan, checked_rehab
                )
                if action == "dead_loading":
                    slot, _dead_proc, _dead_gen = payload
                    if id(slot) in checked_dead:
                        return None
                    checked_dead.add(id(slot))
            if action == "found":
                return payload
            if action == "none":
                return None
            if action == "dead_loading":
                slot, dead_proc, dead_gen = payload
                logger.warning(
                    "ensure_loaded: load process for %s died (slot %s) - resetting slot.",
                    model, slot.slot_id,
                )
                if (
                    slot.process is dead_proc
                    and getattr(slot, "_lifecycle_gen", None) == dead_gen
                ):
                    async with self._unlocked_if_held(lock_held=lock_held):
                        await _kill_slot_async(slot)
                continue
            slot, snap = payload
            if action == "orphan":
                checked_orphan.add(id(slot))
            else:
                checked_rehab.add(id(slot))
            async with self._unlocked_if_held(lock_held=lock_held):
                api_ok = await self._verify_port_api(snap[0])
            async with self._lock_ctx(lock_held=lock_held):
                done, res = self._apply_probe_result(action, slot, model, snap, api_ok)
            if done:
                return res

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
                          and (s.is_running or getattr(s, "_orphan_port", None) is not None)
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
