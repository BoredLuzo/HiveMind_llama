# -*- coding: utf-8 -*-
"""LlamaHealthMixin — Methoden aus LlamaServerManager extrahiert (M3c)."""
from __future__ import annotations

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
from .llama_vram_table import vram_of as _vram_of, vram_of_with_ctx as _vram_of_ctx, vram_of_moe, VRAM_OVERFLOW_MODELS, _MOE_TABLE, get_live_gpu_free_mib, wait_for_vram_reclaim, TOTAL_VRAM_MIB
import asyncio
from .llama_models import resolve_model_path, list_available_models, resolve_mmproj_path, _strip_alias
import platform
import random
import subprocess
import time
import logging
logger = logging.getLogger("llama_manager")

class LlamaHealthMixin:

    def start_idle_monitor(self):
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_monitor())

    async def _idle_monitor(self):
        while True:
            try:
                await asyncio.sleep(30 + random.uniform(0.0, 5.0))
                now = time.time()
                async with self._lock:
                    for slot in self._slots:
                        try:
                            if (slot.is_running and not slot.pinned
                                    and slot._idle_timeout is not None
                                    and (now - slot.last_used) > slot._idle_timeout):
                                logger.info(f"Idle-Evicting {slot.model} (slot {slot.slot_id})")
                                self._metric_inc("evictions_total")
                                self._metric_inc("evictions_idle")
                                await _kill_slot_async(slot)
                        except Exception as e:
                            logger.warning("[idle_monitor] slot %s eviction failed: %s", slot.slot_id, e)
                            continue
                    await self._trigger_pending_prefetch()
            except asyncio.CancelledError:
                logger.info("[idle_monitor] cancelled, exiting")
                break
            except Exception as e:
                logger.error("[idle_monitor] iteration failed: %s — continuing in 60s", e)
                await asyncio.sleep(60.0)

    async def shutdown(self):
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._idle_task), timeout=2.0)
            except (Exception, asyncio.CancelledError):
                pass
        self._prune_prefetch_tasks()
        if self._prefetch_tasks:
            _tasks = tuple(self._prefetch_tasks)
            for t in _tasks:
                t.cancel()
            await asyncio.gather(*_tasks, return_exceptions=True)
            self._prefetch_tasks.clear()
        self._pending_prefetch.clear()
        for slot in self._slots:
            await _kill_slot_async(slot)
        if platform.system() == "Windows":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "llama-server.exe"],
                    capture_output=True, timeout=5,
                    creationflags=_WIN_CNF,
                )
            except Exception:
                pass
        if self._shared_http_client is not None and not self._shared_http_client.is_closed:
            try:
                await self._shared_http_client.aclose()
            except Exception:
                pass
            self._shared_http_client = None

    async def touch_if_loaded(self, model: str) -> bool:


        async with self._lock:
            for slot in self._slots:
                if _nm(slot.model) == _nm(model) and slot.is_running and not slot._loading:
                    slot.touch()
                    return True
        return False

    async def unpin(self, model: str, keep_alive_seconds: float = 600.0):
        async with self._lock:
            slot = await self._find_loaded(model)
            if slot:
                slot.pinned        = False
                slot._idle_timeout = keep_alive_seconds

    def get_port_for(self, model: str) -> Optional[int]:
        slot = self._find_loaded_sync(model)
        if slot and (slot.is_running or slot._loading):
            return slot.port
        return None

    async def list_loaded(self) -> list[dict]:


        async with self._lock:
            live = []
            orphan_candidates = []
            for s in self._slots:
                if not s.model:
                    continue
                if s.is_running or s._loading:
                    live.append({"name": s.model, "model": s.model, "port": s.port,
                                 "pinned": s.pinned, "loaded_at": s.loaded_at, "loading": s._loading})
                else:
                    orphan_candidates.append((s.model, s.port, s.pinned, s.loaded_at))

        if not orphan_candidates:
            return live

        checks = await asyncio.gather(
            *[self._port_alive(port) for _, port, _, _ in orphan_candidates],
            return_exceptions=False
        )
        for (model, port, pinned, loaded_at), alive in zip(orphan_candidates, checks):
            if alive:
                live.append({"name": model, "model": model, "port": port,
                             "pinned": pinned, "loaded_at": loaded_at, "loading": False})
        return live

    async def force_kill_all(self) -> dict:


        killed = []

        if platform.system() == "Windows":
            try:
                r = subprocess.run(
                    ["taskkill", "/F", "/IM", "llama-server.exe", "/T"],
                    capture_output=True, text=True, timeout=10,
                    encoding="utf-8", errors="replace",
                )
                if r.returncode == 0:
                    killed.append("taskkill:llama-server.exe")
                    logger.info("force_kill_all: taskkill OK")
                else:
                    logger.info(f"force_kill_all: taskkill returncode={r.returncode} (maybe nothing was running)")
            except Exception as e:
                logger.warning(f"force_kill_all: taskkill failed: {e}")
        else:
            try:
                subprocess.run(["pkill", "-9", "-f", "llama-server"], timeout=5)
                killed.append("pkill:llama-server")
            except Exception:
                pass

        c = self._get_shared_http_client()
        for s in self._slots:
            try:
                await c.post(f"http://127.0.0.1:{s.port}/shutdown")
                killed.append(f"http_shutdown:{s.port}")
            except Exception:
                pass

        self._prune_prefetch_tasks()
        if self._prefetch_tasks:
            _tasks = tuple(self._prefetch_tasks)
            for t in _tasks:
                t.cancel()
            await asyncio.gather(*_tasks, return_exceptions=True)

        async with self._lock:
            for s in self._slots:
                if s.process is not None:
                    try:
                        await _kill_slot_async(s.process)
                    except Exception:
                        pass
                s.process  = None
                s.model    = None
                s._loading = False
                s._ready_event.clear()
                s.pinned   = False
                s._orphan_port = None
            self._pending_prefetch = []
            self._prefetch_tasks.clear()
        logger.info(f"force_kill_all abgeschlossen. Actions: {killed}")
        return {"ok": True, "killed": killed}

    def list_all_models(self) -> list[dict]:
        return [{"name": m} for m in list_available_models()]

    def vram_status(self) -> dict:


        slots_info = []
        total_vram = 0.0
        for s in self._slots:
            if not s.model:
                continue
            vram = _vram_of_ctx(s.model, s._num_ctx if s._num_ctx > 0 else 4096)
            total_vram += vram
            slots_info.append({
                "slot_id":    s.slot_id,
                "port":       s.port,
                "model":      s.model,
                "vram_gb":    vram,
                "running":    s.is_running,
                "loading":    s._loading,
                "pinned":     s.pinned,
                "last_used":  s.last_used,
                "n_parallel": s._n_parallel,
            })
        return {
            "slots":      slots_info,
            "total_gb":   round(total_vram, 2),
            "budget_gb":  VRAM_BUDGET_GB,
            "free_gb":    round(max(0.0, VRAM_BUDGET_GB - total_vram), 2),
        }
