# -*- coding: utf-8 -*-
"""LlamaLoadMixin — Methoden aus LlamaServerManager extrahiert (M3c)."""
from __future__ import annotations

from .llama_config import (
    LLAMA_BIN, BASE_PORT, MAX_SLOTS,
    KV_CACHE_TYPE, GPU_LAYERS, CONTEXT_SIZE_DEFAULT,
    DEFAULT_IDLE_TIMEOUT_SECONDS, VULKAN_DEVICE, GPU_BACKEND,
    BINARY_MIN_BUILD, MODELS_DIR,
    NO_MMAP_MIN_BUILD,
    MOE_CPU_EXPERTS, _MOE_EXPERT_COUNTS, _MOE_KV_CACHE_TYPES,
    MLOCK_MODEL, CACHE_REUSE,
    MTP_SPEC_TYPE, MTP_DRAFT_N_MAX, MTP_DRAFT_N_MIN,
    DSPARK_SPEC_TYPE, DSPARK_DRAFT_N_MAX, DSPARK_DRAFT_N_MIN, DSPARK_MIN_BUILD,
    _MTP_MODELS,
    _GPU_LAYERS_TABLE,
)
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
from pathlib import Path
from .llama_vram_table import vram_of as _vram_of, vram_of_with_ctx as _vram_of_ctx, vram_of_moe, VRAM_OVERFLOW_MODELS, _MOE_TABLE, get_live_gpu_free_mib, wait_for_vram_reclaim, TOTAL_VRAM_MIB
from .llama_models import resolve_model_path, list_available_models, resolve_mmproj_path, _strip_alias
import asyncio
import httpx
import logging
import subprocess
import time
import logging
logger = logging.getLogger("llama_manager")

class LlamaLoadMixin:

    async def _adopt_or_kill_port(self, slot) -> str:


        model_name = None
        _props_cache: dict | None = None
        c = self._get_shared_http_client()
        try:
            r = await c.get(f"http://127.0.0.1:{slot.port}/props")
            if r.status_code == 200:
                props = r.json()
                _props_cache = props
                model_path = props.get("model_path") or props.get("default_generation_settings", {}).get("model", "")
                if model_path:
                    model_name = _gguf_path_to_model_name(model_path)
                    if not model_name:
                        from pathlib import Path as _P
                        model_name = _P(model_path).stem
        except Exception:
            pass

        if model_name:
            slot.model     = model_name
            slot.loaded_at = time.time()
            slot.last_used = time.time()
            slot._idle_timeout  = DEFAULT_IDLE_TIMEOUT_SECONDS
            slot._ready_event.set()
            slot._orphan_port   = slot.port
            try:
                props_full = _props_cache if _props_cache is not None else {}
                _dgs = props_full.get("default_generation_settings", {})
                _ctx_val = _dgs.get("n_ctx") or props_full.get("n_ctx")
                if _ctx_val and int(_ctx_val) > 0:
                    slot._num_ctx = int(_ctx_val)
                _parallel_val = _dgs.get("n_parallel") or props_full.get("n_parallel")
                if _parallel_val and int(_parallel_val) > 0:
                    slot._n_parallel = int(_parallel_val)
                _attn = props_full.get("attention", {})
                _swa_window = _attn.get("sliding_window") or _attn.get("rope_sliding_window") or 0
                slot.swa_window = int(_swa_window) if _swa_window else 0
            except Exception:
                pass
            self._metric_inc("startup_orphan_rehabilitations")
            logger.info(f"[ADOPT] Slot {slot.slot_id} rehabilitated - {model_name} on port {slot.port}")
            return "rehabilitated"

        logger.warning(f"[ADOPT] Port {slot.port} is alive but no model identifiable - killing process")
        await self._kill_port(slot.port)
        return "killed"

    async def _adopt_orphan_slots(self) -> int:


        actions = 0
        for slot in self._slots:
            if slot.is_running or getattr(slot, "_loading", False):
                continue
            try:
                alive = await self._port_alive(slot.port)
            except Exception:
                alive = False
            if not alive:
                continue
            res = await self._adopt_or_kill_port(slot)
            if res in ("rehabilitated", "killed"):
                actions += 1
        return actions

    async def startup_cleanup(self):


        logger.info("startup_cleanup: checking ports for leftovers from the last run ...")
        rehabilitated = []
        killed_ports  = []

        c = self._get_shared_http_client()
        for slot in self._slots:
            alive = await self._port_alive(slot.port)
            if not alive:
                continue

            model_name = None
            _props_cache: dict | None = None
            try:
                r = await c.get(f"http://127.0.0.1:{slot.port}/props")
                if r.status_code == 200:
                    props = r.json()
                    _props_cache = props
                    # llama-server /props: {"model_path": "/path/to/model.gguf", ...}
                    model_path = props.get("model_path") or props.get("default_generation_settings", {}).get("model", "")
                    if model_path:
                        model_name = _gguf_path_to_model_name(model_path)
                        if not model_name:
                            from pathlib import Path as _P
                            model_name = _P(model_path).stem
            except Exception:
                pass

            if model_name:
                slot.model     = model_name
                slot.loaded_at = time.time()
                slot.last_used = time.time()
                slot._idle_timeout  = DEFAULT_IDLE_TIMEOUT_SECONDS
                slot._ready_event.set()
                slot._orphan_port   = slot.port
                # NUM_CTX FIX: Versuche ctx-Size aus /props zu lesen.
                try:
                    props_full = _props_cache if _props_cache is not None else {}
                    _dgs = props_full.get("default_generation_settings", {})
                    _ctx_val = _dgs.get("n_ctx") or props_full.get("n_ctx")
                    if _ctx_val and int(_ctx_val) > 0:
                        slot._num_ctx = int(_ctx_val)
                    _parallel_val = _dgs.get("n_parallel") or props_full.get("n_parallel")
                    if _parallel_val and int(_parallel_val) > 0:
                        slot._n_parallel = int(_parallel_val)
                    # SWA window size (Qwen3.6 and other sliding-window models)
                    _attn = props_full.get("attention", {})
                    _swa_window = _attn.get("sliding_window") or _attn.get("rope_sliding_window") or 0
                    slot.swa_window = int(_swa_window) if _swa_window else 0
                except Exception:
                    pass
                rehabilitated.append(f"{model_name}@{slot.port}")
                self._metric_inc("startup_orphan_rehabilitations")
                logger.info(f"startup_cleanup: Slot {slot.slot_id} rehabilitated - {model_name} on port {slot.port}")
            else:
                logger.warning(f"startup_cleanup: Port {slot.port} is alive but no model identifiable - killing process")
                await self._kill_port(slot.port)
                killed_ports.append(slot.port)

        if rehabilitated:
            logger.info(f"startup_cleanup: Rehabilitiert: {rehabilitated}")
        if killed_ports:
            logger.info(f"startup_cleanup: Zombie-Ports bereinigt: {killed_ports}")
        if not rehabilitated and not killed_ports:
            logger.info("startup_cleanup: No leftovers found - clean start")

    async def load(self, model: str, keep_alive_seconds: float = 600.0,
                   num_ctx: Optional[int] = None, pin: bool = False) -> ModelSlot:
        _canonical_l = _strip_alias(model)
        _base_l = _canonical_l.split(":")[0].lower()
        if _base_l in _OLLAMA_ONLY_BASES:
            raise RuntimeError(
                f"Modell '{model}' ist Ollama-only (Architektur nicht in b8278 unterstützt)."
            )
        if _canonical_l in VRAM_OVERFLOW_MODELS:
            raise RuntimeError(
                f"Modell '{model}' überschreitet VRAM-Budget ({_vram_of(_canonical_l):.1f}GB > {VRAM_BUDGET_GB}GB). "
                f"Auf dieser GPU (8GB) nicht ladbar."
            )
        _need_start = False
        slot = None
        async with self._lock:
            slot = await self._find_loaded(model)
            if slot:
                if not slot._loading:
                    slot.touch()
                    if pin:
                        slot.pinned = True
                    return slot

            if slot is None:
                slot = await self._find_free_slot() or self._evict_lru()
                if slot is None:
                    raise RuntimeError("No free slot available")
                slot._loading = True
                slot.model    = model
                slot._idle_timeout = None if pin else keep_alive_seconds
                slot.pinned        = pin
                _need_start = True

        if _need_start and not slot._ready_event.is_set():
            try:
                await self._start_process(slot, model, num_ctx or CONTEXT_SIZE_DEFAULT)
            except RuntimeError as _load_exc:
                _has_running_peers = any(
                    s.is_running for s in self._slots if s.slot_id != slot.slot_id
                )
                if "exit=-1" in str(_load_exc) and _has_running_peers:
                    logger.warning(
                        f"AMD-Vulkan-Kollision bei '{model}' in load() (exit=-1) — "
                        f"5s warten und einmal neu versuchen ..."
                    )
                    await _kill_slot_async(slot)
                    slot._loading = True
                    slot.model    = model
                    await asyncio.sleep(5.0)
                    try:
                        await self._start_process(slot, model, num_ctx or CONTEXT_SIZE_DEFAULT)
                    except Exception:
                        await _kill_slot_async(slot)
                        raise
                else:
                    await _kill_slot_async(slot)
                    raise
            except Exception:
                await _kill_slot_async(slot)
                raise

        try:
            await asyncio.wait_for(slot._ready_event.wait(), timeout=240.0)
        except asyncio.TimeoutError:
            raise RuntimeError(f"llama-server for '{model}' did not start in time (240s timeout)")

        slot.touch()
        return slot

    async def prefetch_next(self, model: str, num_ctx: Optional[int] = None):


        async with self._lock:
            _prefetch_ctx = int(num_ctx or CONTEXT_SIZE_DEFAULT)
            if self._has_active_prefetch(model, _prefetch_ctx):
                logger.debug("Prefetch skipped (already active): %s", model)
                return
            if await self._find_loaded(model):
                return

            current_vram = sum(_vram_of(s.model) for s in self._slots if s.model)
            next_vram    = _vram_of(model)

            if current_vram + next_vram <= VRAM_BUDGET_GB:
                slot = await self._find_free_slot()
                if slot is None:
                    entry = (model, _prefetch_ctx)
                    if not self._queue_contains_prefetch(*entry):
                        self._pending_prefetch.append(entry)
                    return
                slot._loading = True
                slot.model    = model
            else:
                logger.info(f"Prefetch pending (VRAM voll): {model} "
                            f"({current_vram:.1f}+{next_vram:.1f}>{VRAM_BUDGET_GB}GB)")
                entry = (model, _prefetch_ctx)
                if not self._queue_contains_prefetch(*entry):
                    self._pending_prefetch.append(entry)
                    self._metric_inc("prefetch_enqueued")
                return

            self._schedule_prefetch_task(slot, model, _prefetch_ctx)

    async def ensure_loaded(self, model: str, num_ctx: Optional[int] = None,
                            pin: bool = False, vision: bool = False,
                            n_parallel: int = 1) -> int:


        _canonical = _strip_alias(model)
        # OLLAMA-ONLY-GUARD
        _base = _canonical.split(":")[0].lower()
        if _base in _OLLAMA_ONLY_BASES:
            raise RuntimeError(
                f"Modell '{model}' ist Ollama-only (Architektur nicht in b8278 unterstützt)."
            )
        # OVERFLOW-GUARD (gegen kanonischen Namen)
        if _canonical in VRAM_OVERFLOW_MODELS:
            raise RuntimeError(
                f"Modell '{model}' überschreitet VRAM-Budget ({_vram_of(_canonical):.1f}GB > {VRAM_BUDGET_GB}GB). "
                f"Auf dieser GPU (8GB) nicht ladbar."
            )
        _need_start = False
        slot = None
        async with self._lock:
            # F1-HOOK-A (2026-08-24) ADOPTION-ON-DEMAND: Ungetrackte llama-server
            if time.time() - self._last_orphan_scan_ts >= 15.0:
                self._last_orphan_scan_ts = time.time()
                try:
                    await self._adopt_orphan_slots()
                except Exception as _adopt_exc:
                    logger.warning(
                        "ensure_loaded: orphan-adoption scan failed: %s", _adopt_exc
                    )
            slot = await self._find_loaded(model)
            if slot:
                if not slot._loading:
                    if vision != slot._vision:
                        self._metric_inc("evictions_total")
                        await _kill_slot_async(slot)
                        slot = None
                    # "Cannot read image.png" via GGUF-Chat-Template auf b8940+.
                    elif _base.startswith("qwen3") and not slot._jinja:
                        logger.info(
                            "ensure_loaded: %s Jinja-Mismatch (--jinja fehlt) — reload.", model
                        )
                        self._metric_inc("evictions_total")
                        await _kill_slot_async(slot)
                        slot = None
                    else:
                        # angefordert, klemmt llama-server max_tokens still auf
                        _req_ctx = num_ctx or CONTEXT_SIZE_DEFAULT
                        if slot._num_ctx > 0 and _req_ctx > slot._num_ctx:
                            logger.info(
                                f"ensure_loaded: {model} ctx-Mismatch "
                                f"(läuft mit {slot._num_ctx}, braucht {_req_ctx}) — reload."
                            )
                            self._metric_inc("evictions_total")
                            self._metric_inc("evictions_ctx_reload")
                            await _kill_slot_async(slot)
                            slot = None
                        # → Beim asyncio.gather feuern mehrere Requests gleichzeitig, llama queued sie.
                        elif (getattr(slot, "_n_parallel", 1) > 0
                              and n_parallel > getattr(slot, "_n_parallel", 1)):
                            logger.info(
                                f"ensure_loaded: {model} parallel-Mismatch "
                                f"(läuft mit --parallel {getattr(slot, '_n_parallel', 1)}, braucht {n_parallel}) — reload."
                            )
                            self._metric_inc("evictions_total")
                            self._metric_inc("evictions_parallel_reload")
                            await _kill_slot_async(slot)
                            slot = None
                        else:
                            # STALE-PORT-GUARD (2026-08-31): Phantom-Slot —
                            # Manager-Buchhaltung sagt "geladen", aber der Port
                            # bedient keine Requests mehr (Prozess gehangen oder
                            # extern gekillt → Live-Befund: Planner-POST bekam
                            # httpx.ConnectError auf einem "geladenen" Slot).
                            # TCP-Probe ist billig (~0.4s) und schlägt auch bei
                            # laufender Generation NICHT fehl (Kernel-Backlog).
                            # Port tot → Slot killen und sauber neu laden.
                            try:
                                _stale_alive = await self._port_alive(slot.port)
                            except Exception:
                                _stale_alive = False
                            if not _stale_alive:
                                logger.warning(
                                    "ensure_loaded: %s port %d dead despite loaded slot - reload.",
                                    model, slot.port,
                                )
                                self._metric_inc("evictions_total")
                                self._metric_inc("evictions_stale_port")
                                await _kill_slot_async(slot)
                                slot = None
                            else:
                                slot.touch()
                                if pin and not slot.pinned:
                                    slot.pinned        = True
                                    slot._idle_timeout = None
                                return slot.port
            if slot is None:
                slot = await self._find_free_slot() or self._evict_lru()
                if slot is None:
                    raise RuntimeError("No free slot available")
                slot._loading      = True
                slot.model         = model
                slot._idle_timeout = None if pin else DEFAULT_IDLE_TIMEOUT_SECONDS
                slot.pinned        = pin
                _need_start = True

        async with self._lock:
            _committed = sum(
                vram_of_moe(_strip_alias(s.model), s._num_ctx if s._num_ctx > 0 else 4096)
                if any(x in (s.model or "") for x in ("35b-a3b", "a3b-ud", "a3b-mtp"))
                else _vram_of(s.model)
                for s in self._slots if s.model
            )
            if _committed > VRAM_BUDGET_GB:
                _active_workers = [
                    s for s in self._slots
                    if s.model and s.is_running
                ]
                if _active_workers:
                    logging.getLogger("vram_guard").info(
                        "[VRAM-GUARD] Committed=%.1fGB > budget=%.1fGB — %d active worker(s) still running. Waiting 5s.",
                        _committed, VRAM_BUDGET_GB, len(_active_workers),
                    )
                    await asyncio.sleep(5.0)
                _extra = self._evict_lru(exclude_slot=slot)
                if _extra is not None:
                    await _kill_slot_async(_extra)
                # Recalculate after eviction (stale _committed used to log wrong value)
                _committed_after = sum(
                    vram_of_moe(_strip_alias(s.model), s._num_ctx if s._num_ctx > 0 else 4096)
                    if any(x in (s.model or "") for x in ("35b-a3b", "a3b-ud", "a3b-mtp"))
                    else _vram_of(s.model)
                    for s in self._slots if s.model
                )
                if _committed_after > VRAM_BUDGET_GB:
                    logging.getLogger("vram_guard").warning(
                        "[VRAM-GUARD] committed=%.1fGB > budget=%.1fGB after eviction — forced extra evict",
                        _committed_after, VRAM_BUDGET_GB,
                    )
                    _extra2 = self._evict_lru(exclude_slot=slot)
                    if _extra2 is not None:
                        await _kill_slot_async(_extra2)

        if _need_start and not slot._ready_event.is_set():
            try:
                await self._start_process(slot, model, num_ctx or CONTEXT_SIZE_DEFAULT,
                                          vision=vision, n_parallel=n_parallel)
            except RuntimeError as _start_exc:
                # AMD-VULKAN-KOLLISIONS-RETRY:
                _has_running_peers = any(
                    s.is_running for s in self._slots if s.slot_id != slot.slot_id
                )
                if "exit=-1" in str(_start_exc) and _has_running_peers:
                    logger.warning(
                        f"AMD-Vulkan-Kollision bei '{model}' (exit=-1) — "
                        f"5s warten und einmal neu versuchen ..."
                    )
                    await _kill_slot_async(slot)
                    slot._loading = True
                    slot.model    = model
                    await asyncio.sleep(5.0)
                    try:
                        await self._start_process(slot, model, num_ctx or CONTEXT_SIZE_DEFAULT,
                                                   vision=vision, n_parallel=n_parallel)
                    except Exception:
                        await _kill_slot_async(slot)
                        raise
                elif "exit=-1" in str(_start_exc) and GPU_LAYERS > 0:
                    _model_vram = _vram_of(_strip_alias(model))
                    if _model_vram > 5.0:
                        await _kill_slot_async(slot)
                        raise RuntimeError(
                            f"Modell '{model}' ({_model_vram:.1f}GB) konnte nicht auf GPU geladen werden (exit=-1).\n"
                            f"Wahrscheinliche Ursache: VRAM noch nicht freigegeben (AMD Vulkan-Timing).\n"
                            f"  → Server neu starten oder 10s warten und erneut versuchen.\n"
                            f"  → CPU-Fallback übersprungen: {_model_vram:.1f}GB auf CPU ist zu langsam."
                        ) from _start_exc
                    logger.warning(
                        f"Single-model start for '{model}' with exit=-1 failed - "
                        "one-time CPU fallback with --n-gpu-layers 0"
                    )
                    await _kill_slot_async(slot)
                    slot._loading = True
                    slot.model = model
                    try:
                        await self._start_process(
                            slot,
                            model,
                            num_ctx or CONTEXT_SIZE_DEFAULT,
                            vision=vision,
                            n_parallel=n_parallel,
                            gpu_layers_override=0,
                        )
                    except Exception:
                        await _kill_slot_async(slot)
                        raise
                else:
                    await _kill_slot_async(slot)
                    raise
            except Exception:
                await _kill_slot_async(slot)
                raise

        try:
            await asyncio.wait_for(slot._ready_event.wait(), timeout=240.0)
        except asyncio.TimeoutError:
            raise RuntimeError(f"llama-server for '{model}' did not start in time (240s timeout)")

        # A.2 FIX: Shared-slot guard — if the slot died during startup
        # (e.g., another caller's load failed → kill() set ready_event),
        # detect the dead slot here instead of silently continuing.
        if slot.model is None:
            raise RuntimeError(
                f"Shared slot for '{model}' died during startup — "
                f"the underlying llama-server process failed to load."
            )

        slot.touch()
        return slot.port

    async def _start_process(self, slot: ModelSlot, model: str, num_ctx: int,
                             vision: bool = False, n_parallel: int = 1,
                             gpu_layers_override: Optional[int] = None):
        """Startet llama-server. Setzt _ready_event wenn /health OK."""
        _evicted_here = False
        if slot.is_running:
            await _kill_slot_async(slot)
            _evicted_here = True
        slot._orphan_port = None

        gguf_path = resolve_model_path(model)
        if not gguf_path:
            from .llama_config import MODELS_DIR as _MDIR
            if not Path(_MDIR).exists():
                raise FileNotFoundError(
                    f"GGUF für '{model}' nicht gefunden.\n"
                    f"  → Models-Ordner fehlt: {_MDIR}\n"
                    f"  → Setze HIVEMIND_MODELS_DIR (Umgebungsvariable) auf deinen Models-Ordner\n"
                    f"    oder führe setup_models.bat aus, um die empfohlenen Modelle zu laden."
                )
            raise FileNotFoundError(
                f"GGUF für '{model}' nicht gefunden.\n"
                f"  → Prüfe models.json und führe hive_functions/scan_models.py aus.\n"
                f"  → Pfad muss auf eine .gguf-Datei zeigen."
            )
        if not Path(gguf_path).exists():
            raise FileNotFoundError(
                f"GGUF-Datei existiert nicht auf der Festplatte: {gguf_path}\n"
                f"  → Modell wurde verschoben oder gelöscht.\n"
                f"  → models.json aktualisieren oder hive_functions/scan_models.py erneut ausführen."
            )

        _resolved_str = str(gguf_path).replace("\\", "/")
        _is_ollama_blob = ".ollama/models/blobs/sha256-" in _resolved_str
        if _is_ollama_blob:
            _blob_model_base = _strip_alias(model).split(":")[0].lower()
            _broken_ollama_bases = {"qwen3.5", "qwen3-vl"}
            if _blob_model_base in _broken_ollama_bases:
                logger.warning(
                    f"OLLAMA-BLOB-WARNING: '{model}' is being loaded from an Ollama blob ({gguf_path}). "
                    f"Ollama blobs for {_blob_model_base} often have rope.dimension_sections[3] - "
                    f"llama.cpp expects [4] - possible crash. "
                    f"Fix: place a compatible GGUF in {MODELS_DIR} (will be found automatically)."
                )

        running_slots = [s for s in self._slots if s.is_running and s.slot_id != slot.slot_id]
        if running_slots:
            current_vram = sum(
                vram_of_moe(_strip_alias(s.model), s._num_ctx if s._num_ctx > 0 else 4096)
                for s in running_slots if s.model
            )
            new_vram = vram_of_moe(_strip_alias(model), num_ctx)
            if current_vram + new_vram + _VRAM_BASE_OVERHEAD_GB > VRAM_BUDGET_GB:
                #
                #   2. Worker-2 _start_process → VRAM-Check → _unpinned=[] →
                #      to_kill = [Worker-1-Slot] → Worker-1 gekillt
                #   4. Worker-1 reload → VRAM-Check → killt Worker-2 → Whack-a-Mole
                _unpinned = [s for s in running_slots if not s.pinned]
                if not _unpinned:
                    logger.warning(
                        f"_start_process [{model}]: Alle laufenden Peers "
                        f"({[s.model for s in running_slots]}) sind gepinnt — "
                        f"VRAM-Eviction übersprungen "
                        f"(current={current_vram:.1f}+new={new_vram:.1f}>{VRAM_BUDGET_GB}GB). "
                        f"Caller hat Budget validiert; AMD-Vulkan-Retry greift bei echtem OOM."
                    )
                else:
                    to_kill = _unpinned
                    if to_kill:
                        _evicted_here = True
                    for rs in to_kill:
                        logger.info(f"Vulkan-Serialisierung (VRAM {current_vram:.1f}+{new_vram:.1f}>{VRAM_BUDGET_GB}GB): evicte {rs.model} (pinned={rs.pinned})")
                        await _kill_slot_async(rs)
                    for rs in to_kill:
                        for _ in range(20):
                            alive = await self._port_alive(rs.port)
                            if not alive:
                                break
                            await asyncio.sleep(0.05)
                    await asyncio.sleep(0.5)
                    # VRAM-Reclaim-Wait: Port-Tod != VRAM frei (Vulkan asynchron)
                    _reclaimed = await wait_for_vram_reclaim(int(new_vram * 1024 + 768), timeout_sec=45)
                    if not _reclaimed:
                        _free_now = get_live_gpu_free_mib()
                        logger.warning(
                            "Vulkan: VRAM-Reclaim-Timeout nach Eviction — "
                            "Pre-Flight könnte trotzdem blocken (nur %.0fMiB frei)",
                            _free_now if _free_now is not None else -1,
                        )
            else:
                _live_free = get_live_gpu_free_mib()
                _need_mib = new_vram * 1024 + 768

                _should_evict = False
                if _live_free is None:
                    _formel_free_mib = (VRAM_BUDGET_GB - current_vram) * 1024
                    if _formel_free_mib < _need_mib + 500:
                        _should_evict = True
                        logger.warning(
                            "Vulkan: Live-Messung nicht verfügbar, "
                            "Formel knapp (%.0fMiB < %.0fMiB) — evicte konservativ",
                            _formel_free_mib, _need_mib + 500,
                        )
                elif _live_free < _need_mib:
                    _should_evict = True
                    logger.warning(
                        "Vulkan: Formel sagt passt (%.1f+%.1f<=%.1fGB), "
                        "aber Live-Messung zeigt nur %.0fMiB frei (<%.0fMiB nötig) — evicte",
                        current_vram, new_vram, VRAM_BUDGET_GB, _live_free, _need_mib,
                    )

                if _should_evict:
                    _to_kill = [s for s in running_slots if not s.pinned]
                    if _to_kill:
                        _evicted_here = True
                    for rs in _to_kill:
                        logger.info("Vulkan: live VRAM pressure - evicting %s", rs.model)
                        await _kill_slot_async(rs)
                    for rs in _to_kill:
                        for _ in range(20):
                            alive = await self._port_alive(rs.port)
                            if not alive:
                                break
                            await asyncio.sleep(0.05)
                    await asyncio.sleep(0.5)
                    _reclaimed = await wait_for_vram_reclaim(int(_need_mib), timeout_sec=45)
                    if not _reclaimed:
                        _free_now = get_live_gpu_free_mib()
                        logger.warning(
                            "Vulkan: VRAM-Reclaim-Timeout nach Eviction — "
                            "Pre-Flight könnte trotzdem blocken (nur %.0fMiB frei)",
                            _free_now if _free_now is not None else -1,
                        )
                else:
                    _free_str = f"{_live_free:.0f}MiB" if _live_free is not None else "N/A"
                    logger.info(
                        "Vulkan: %s passt neben laufenden "
                        "(Formel: %.1f+%.1f<=%.1fGB, Live: %s) — kein Kill nötig",
                        model, current_vram, new_vram, VRAM_BUDGET_GB, _free_str,
                    )

        if type(self)._binary_build_number is None:
            type(self)._binary_build_number = await asyncio.to_thread(
                _probe_binary_build, str(LLAMA_BIN)
            )
            logger.info(f"llama-server Binary Build: {type(self)._binary_build_number}")

        _build = type(self)._binary_build_number
        _model_base = _strip_alias(model).split(":")[0].lower()
        _min_build = BINARY_MIN_BUILD.get(_model_base, 0)
        if _build > 0 and _min_build > 0 and _build < _min_build:
            raise RuntimeError(
                f"Binary zu alt für '{model}':\n"
                f"  Installiert: Build {_build}\n"
                f"  Benötigt:    Build {_min_build}+\n\n"
                f"  → Neues Binary herunterladen:\n"
                f"    https://github.com/ggerganov/llama.cpp/releases\n"
                f"    Datei: llama-bXXXX-bin-win-vulkan-x64.zip\n"
                f"  → Danach llama_config.py → LLAMA_BIN aktualisieren"
            )
        elif _build == 0 and _min_build > 0:
            logger.warning(
                f"Build number of {LLAMA_BIN.name} could not be determined. "
                f"Build {_min_build}+ is recommended for '{model}'. "
                f"If the server crashes: update the binary to b{_min_build}+."
            )

        # ── Befehlszeile aufbauen ─────────────────────────────────────────────
        _gpu_layers = gpu_layers_override if gpu_layers_override is not None else _GPU_LAYERS_TABLE.get(model, GPU_LAYERS)
        try:
            from .llama_config import _try_registry_gpu_layers as _reg_gl
            _gl_reg = _reg_gl(_strip_alias(model))
            if _gl_reg is not None:
                _gpu_layers = _gl_reg
        except Exception:
            pass
        cmd = [
            str(LLAMA_BIN),
            "--model",        str(gguf_path),
            "--port",         str(slot.port),
            "--ctx-size",     str(num_ctx),
            "--n-gpu-layers", str(_gpu_layers),
            "--parallel",     str(max(1, n_parallel)),
            "--flash-attn",   "on",
            "--batch-size",   "1024",
            "--ubatch-size",  "256",
            "--threads",      "16",
            "--threads-batch","8",
            "--split-mode",   "none",
        ]

        if CACHE_REUSE and int(CACHE_REUSE) > 0:
            cmd += ["--cache-reuse", str(int(CACHE_REUSE))]
            logger.info("Prompt-Cache-Reuse aktiv: --cache-reuse %d", int(CACHE_REUSE))

        # ── --device Flag (Vulkan-Device-Auswahl) ────────────────────────────
        _all_flags_uncached = (
            type(self)._device_flag_supported is None
            or type(self)._kv_flag_supported is None
            or type(self)._load_mode_supported is None
        )
        if _all_flags_uncached:
            def _probe_all(llama_bin: str, kv_type: str) -> tuple[int, bool, bool, bool, bool, bool]:
                """Einmalige Probe: Build-Nummer + Flag-Tests aus einem --help-Aufruf."""
                import re as _re3
                build_num = 0
                device_ok = False
                kv_ok     = False
                moe_ok    = False
                load_mode_ok = False
                dspark_ok = False
                for flag in ["--version", "-v"]:
                    try:
                        r = subprocess.run(
                            [llama_bin, flag],
                            capture_output=True, text=True, timeout=10,
                            encoding="utf-8", errors="replace",
                            creationflags=_WIN_CNF,
                        )
                        text = (r.stdout or "") + (r.stderr or "")
                        for pat in [r'build[:\s]+(\d{4,})', r'\bb(\d{4,})\b']:
                            m = _re3.search(pat, text, _re3.IGNORECASE)
                            if m:
                                build_num = int(m.group(1))
                                break
                        if build_num > 0:
                            break
                    except Exception:
                        continue
                try:
                    r = subprocess.run(
                        [llama_bin, "--help"],
                        capture_output=True, text=True, timeout=10,
                        encoding="utf-8", errors="replace",
                        creationflags=_WIN_CNF,
                    )
                    help_text = (r.stdout or "") + (r.stderr or "")
                    if build_num == 0:
                        for pat in [r'build[:\s]+(\d{4,})', r'\bb(\d{4,})\b']:
                            m = _re3.search(pat, help_text, _re3.IGNORECASE)
                            if m:
                                build_num = int(m.group(1))
                                break
                    device_ok = "--device" in help_text.lower()
                    kv_ok     = ("cache-type-k" in help_text.lower()
                                 or "cache_type_k" in help_text.lower())
                    moe_ok    = "n-cpu-moe" in help_text.lower()
                    load_mode_ok = "load-mode" in help_text.lower()
                    dspark_ok = "draft-dspark" in help_text.lower()
                except Exception:
                    pass
                if build_num == 0:
                    import os as _os
                    _fname = _os.path.basename(llama_bin)
                    _fm = _re3.search(r'b(\d{4,})', _fname, _re3.IGNORECASE)
                    if _fm:
                        build_num = int(_fm.group(1))
                if build_num >= 8278:
                    device_ok = True
                    kv_ok     = True
                    moe_ok    = True
                if build_num >= DSPARK_MIN_BUILD:
                    dspark_ok = True
                return build_num, device_ok, kv_ok, moe_ok, load_mode_ok, dspark_ok

            _bn, _dev, _kv, _moe, _lm, _dspark = await asyncio.to_thread(_probe_all, str(LLAMA_BIN), KV_CACHE_TYPE)
            if _bn > 0 and type(self)._binary_build_number in (None, 0):
                type(self)._binary_build_number = _bn
                logger.info(f"llama-server Binary Build: {_bn} (from --help/filename)")
            if type(self)._device_flag_supported is None:
                type(self)._device_flag_supported = _dev
            if type(self)._kv_flag_supported is None:
                type(self)._kv_flag_supported = _kv
            if type(self)._moe_flag_supported is None:
                type(self)._moe_flag_supported = _moe
            if type(self)._load_mode_supported is None:
                type(self)._load_mode_supported = _lm
            if type(self)._dspark_flag_supported is None:
                type(self)._dspark_flag_supported = _dspark

        if type(self)._device_flag_supported:
            # CUDA → "CUDA<N>" (DEVICE-FORMAT-FIX 2026-08-27: llama.cpp
            if GPU_BACKEND == "cuda":
                cmd += ["--device", f"CUDA{VULKAN_DEVICE}"]
            else:
                cmd += ["--device", f"VULKAN{VULKAN_DEVICE}"]
        else:
            logger.warning(
                f"--device not supported by {LLAMA_BIN.name} - "
                "Vulkan device selection disabled. Update the binary to b8278+ recommended."
            )

        # ── Backend-DLL-Check (CUDA/Vulkan) ───────────────────────────────
        # kryptischem Start-Fail. Einmalig gecacht.
        if type(self)._backend_dlls_ok is None:
            _dll_ok = await asyncio.to_thread(
                _probe_backend_dlls, str(LLAMA_BIN), GPU_BACKEND
            )
            if _dll_ok is not None:
                type(self)._backend_dlls_ok = _dll_ok
                logger.info(
                    "Backend-DLL-Check (%s, %s): %s",
                    GPU_BACKEND.upper(), LLAMA_BIN.name,
                    "DLLs vollständig" if _dll_ok else "DLLs fehlen",
                )
        if type(self)._backend_dlls_ok is False:
            _be_upper = GPU_BACKEND.upper()
            raise RuntimeError(
                f"{_be_upper}-DLL-Check fehlgeschlagen: die Runtime-DLLs für "
                f"'{GPU_BACKEND}' fehlen neben {LLAMA_BIN.name}.\n\n"
                f"  Benötigt ({GPU_BACKEND}): "
                + ("ggml-cuda.dll, cudart64_*.dll, cublas64_*.dll, cublasLt64_*.dll"
                   if GPU_BACKEND == "cuda" else "ggml-vulkan.dll")
                + "\n"
                f"  Binary-Ordner: {LLAMA_BIN.parent}\n\n"
                f"  Lösung A: llama.cpp neu laden — die offiziellen ZIPs bündeln "
                f"die passenden DLLs:\n"
                f"    python deploy\\fetch_llamacpp.py --backend {GPU_BACKEND} --force\n"
                f"    (lädt die CUDA-Version passend zum Treiber via nvidia-smi)\n"
                f"  Lösung B: Ordner {LLAMA_BIN.parent} prüfen — ist es ein "
                f"{_be_upper}-Build (Name enthält '{GPU_BACKEND}')?\n"
                f"  Lösung C: Antivirus/Defender hat DLLs quarantänisiert — "
                f"Betroffene Dateien wiederherstellen."
            )

        # ── Backend-Device-Check (CUDA/Vulkan) ─────────────────────────────
        #      cuda-12.4-Build auf 13.x-Treiber → cudaGetDeviceCount=0 →
        if type(self)._backend_devices_ok is None:
            _dev_ok = await asyncio.to_thread(
                _probe_backend_devices, str(LLAMA_BIN), GPU_BACKEND
            )
            if _dev_ok is not None:
                type(self)._backend_devices_ok = _dev_ok
                logger.info(
                    "Backend-Device-Check (%s, %s): %s",
                    GPU_BACKEND.upper(), LLAMA_BIN.name,
                    "Gerät gefunden" if _dev_ok else "KEIN Gerät gefunden",
                )
        if type(self)._backend_devices_ok is False and type(self)._device_flag_supported:
            _be_upper = GPU_BACKEND.upper()
            raise RuntimeError(
                f"{_be_upper}-Device-Check fehlgeschlagen: {LLAMA_BIN.name} "
                f"listet KEINE {_be_upper}-Geräte (--list-devices leer).\n\n"
                f"  Binary: {LLAMA_BIN}\n"
                f"  Backend: {GPU_BACKEND} (settings.json → 'gpu_backend' / Env HIVEMIND_GPU_BACKEND)\n\n"
                f"  Häufigste Ursache: CUDA-Runtime des Builds passt nicht zum Treiber "
                f"(z.B. cuda-12.4-Build auf Treiber 13.x → GPU wird nicht erkannt).\n"
                f"  Lösung A: llama.cpp neu laden — lädt automatisch die CUDA-Version "
                f"passend zum Treiber (nvidia-smi):\n"
                f"    python deploy\\fetch_llamacpp.py --backend {GPU_BACKEND} --force\n"
                f"  Lösung B: Anderes Binary gewählt? prüfe llama\\-Ordner — es muss ein\n"
                f"    {_be_upper}-Build sein (Ordner enthält '{GPU_BACKEND}').\n"
                f"  Lösung C: Backend in settings.json auf das tatsächlich installierte "
                f"Binary stellen (\"gpu_backend\": \"vulkan\" bzw. \"cuda\")."
            )

        # (2026-08-17, Hermes-35B-MoE auf Windows-Vulkan):
        #   --load-mode mlock      → GGML_ASSERT(addr) in llama-mmap.cpp (crasht)
        if type(self)._load_mode_supported:
            _load_mode = "mmap+mlock" if MLOCK_MODEL else "none"
            cmd += ["--load-mode", _load_mode]
            logger.info("--load-mode %s (mlock=%s)", _load_mode, bool(MLOCK_MODEL))
        elif (type(self)._binary_build_number or 0) >= NO_MMAP_MIN_BUILD:
            cmd += ["--no-mmap"]
            logger.info("--no-mmap aktiv (Build %d >= %d, Windows-Vulkan mmap-Fix)",
                        type(self)._binary_build_number or 0, NO_MMAP_MIN_BUILD)

        _moe_model_key = _strip_alias(model).replace("-ud", "")
        # Per-Model-Override (dict {model_key: n_cpu_moe}) > Registry-Config > Tabellen-Default.
        _moe_count = 0
        try:
            from .llama_config import _try_registry_moe as _reg_moe
            _reg_moe_v = _reg_moe(_moe_model_key)
            if _reg_moe_v:
                _moe_count = _reg_moe_v
        except Exception:
            pass
        if _moe_count <= 0:
            try:
                _moe_map = MOE_CPU_EXPERTS
                if isinstance(_moe_map, dict):
                    _moe_count = int(_moe_map.get(_moe_model_key, 0) or 0)
                    if _moe_count <= 0:
                        for _ok, _ov in _moe_map.items():
                            if _moe_model_key.startswith(_ok) and int(_ov or 0) > 0:
                                _moe_count = int(_ov)
                                break
                else:
                    _moe_count = int(_moe_map or 0)
            except Exception:
                _moe_count = 0
        if _moe_count <= 0:
            _moe_count = _MOE_EXPERT_COUNTS.get(_moe_model_key, 0)
            if _moe_count == 0 and _moe_model_key:
                # Prefix-Match: GGUF auto-discovery suffixes
                for _ek in _MOE_EXPERT_COUNTS:
                    if _moe_model_key.startswith(_ek):
                        _moe_count = _MOE_EXPERT_COUNTS[_ek]
                        break
        # Modellnamen ableiten (Xb-aYb → X, ab 30B). Kleine MoE bleiben komplett
        if _moe_count <= 0 and _moe_model_key:
            try:
                from .llama_config import detect_moe_count as _moe_autodetect
                _moe_count = _moe_autodetect(_moe_model_key)
            except Exception:
                _moe_count = 0

        # ── VRAM-KALIBRIERUNGS-GUARD (soft) ──
        _cal_key = _strip_alias(model).strip().lower()
        _cal_cfg = _MOE_TABLE.get(_cal_key) or _MOE_TABLE.get(_cal_key.replace("-ud", ""))
        if _cal_cfg and "calibrated_n_cpu_moe" in _cal_cfg and _cal_cfg["calibrated_n_cpu_moe"] != _moe_count:
            logger.warning(
                f"[VRAM-KALIBRIERUNG] {model}: --n-cpu-moe={_moe_count} weicht vom kalibrierten Wert "
                f"{_cal_cfg['calibrated_n_cpu_moe']} ab — die VRAM-Schätzung (vram_of_moe) ist jetzt "
                f"möglicherweise ungenau. Neu kalibrieren via -lv 5 "
                f"(KV/compute buffer size aus dem llama-server-Log)."
            )

        # ── KV-Cache Quantisierung ────────────────────────────────────────────
        if KV_CACHE_TYPE and KV_CACHE_TYPE.lower() not in ("f16", "") and _moe_count <= 0:
            if type(self)._kv_flag_supported:
                cmd += ["--cache-type-k", KV_CACHE_TYPE, "--cache-type-v", KV_CACHE_TYPE]
                logger.info(f"KV-Cache Quantisierung aktiv: {KV_CACHE_TYPE}")
            else:
                logger.warning(
                    f"--cache-type-k {KV_CACHE_TYPE} nicht unterstuetzt von "
                    f"{LLAMA_BIN.name} — falle auf f16 zurueck. "
                    "Neueres Binary verwenden fuer q4_0-Support."
                )

        # ── MoE-Offloading ────────────────────────────────────────────────────
        if _moe_count > 0:
            if type(self)._moe_flag_supported:
                cmd += ["--n-cpu-moe", str(_moe_count)]
                logger.info(
                    f"MoE-Offloading aktiv: {_moe_count} Experts auf CPU "
                    f"({_moe_model_key})"
                )
                # MoE-spezifische Optimierungen
                cmd += ["--no-warmup"]
                if not type(self)._load_mode_supported and MLOCK_MODEL:
                    cmd += ["--mlock"]
                    logger.info("--mlock active (model locked in RAM)")
                if type(self)._kv_flag_supported:
                    _moe_kv = _MOE_KV_CACHE_TYPES.get(_moe_model_key, "q4_0")
                    cmd += ["--cache-type-k", _moe_kv, "--cache-type-v", _moe_kv]
                    logger.info("MoE KV-Cache: %s (ctx=%d, VRAM-optimiert)",
                                _moe_kv, num_ctx)
            else:
                logger.warning(
                    f"--n-cpu-moe {_moe_count} nicht unterstuetzt von "
                    f"{LLAMA_BIN.name} — MoE-Modell laeuft ohne Expert-Offloading. "
                    "Neueres Binary verwenden (b8278+)."
                )

        # ── MTP (Multi-Token Prediction / Speculative Decoding) ────────────────
        _mtp_model_key = _strip_alias(model)
        _mtp_from_registry = None
        try:
            from .llama_config import _try_registry_mtp as _reg_mtp
            _mtp_from_registry = _reg_mtp(_mtp_model_key)
        except Exception:
            pass
        _is_mtp = bool(_mtp_from_registry) if _mtp_from_registry is not None else (_mtp_model_key in _MTP_MODELS)
        if _is_mtp:
            cmd += [
                "--spec-type", MTP_SPEC_TYPE,
                "--spec-draft-n-max", str(MTP_DRAFT_N_MAX),
                "--spec-draft-n-min", str(MTP_DRAFT_N_MIN),
            ]
            logger.info(
                "MTP aktiv: %s (draft-max=%d, draft-min=%d)",
                MTP_SPEC_TYPE, MTP_DRAFT_N_MAX, MTP_DRAFT_N_MIN,
            )

        # ── DSpark external drafter (Speculative-Decoding Sidecar) ──────────
        _draft_fn = None
        try:
            from .llama_config import _try_registry_dspark_draft as _reg_dd2
            _draft_fn = _reg_dd2(_strip_alias(model))
        except Exception:
            _draft_fn = None
        if _draft_fn:
            _draft_path = None
            _dp = Path(_draft_fn)
            if _dp.is_absolute() and _dp.exists():
                _draft_path = _dp
            else:
                _cand = MODELS_DIR / _draft_fn
                if _cand.exists():
                    _draft_path = _cand
                else:
                    try:
                        _found = list(MODELS_DIR.rglob(_draft_fn))
                        if _found:
                            _draft_path = _found[0]
                    except Exception:
                        pass
            if _draft_path is None:
                logger.warning(
                    "DSpark-Drafter für '%s' konfiguriert (%s), aber nicht in %s gefunden "
                    "— läuft ohne Drafter.",
                    model, _draft_fn, MODELS_DIR,
                )
            else:
                _dspark_ok = type(self)._dspark_flag_supported
                if _dspark_ok is None:
                    _dspark_ok = (type(self)._binary_build_number or 0) >= DSPARK_MIN_BUILD
                if not _dspark_ok:
                    logger.warning(
                        f"DSpark-Speculative-Decoding für '{model}' nicht unterstützt von "
                        f"{LLAMA_BIN.name} (braucht --spec-type draft-dspark, Build "
                        f"{DSPARK_MIN_BUILD}+) — Drafter übersprungen."
                    )
                else:
                    cmd += [
                        "--spec-type", DSPARK_SPEC_TYPE,
                        "--model-draft", str(_draft_path),
                        "--spec-draft-n-max", str(DSPARK_DRAFT_N_MAX),
                        "--spec-draft-n-min", str(DSPARK_DRAFT_N_MIN),
                    ]
                    logger.info(
                        "DSpark aktiv: %s → Drafter %s (n-max=%d, n-min=%d)",
                        model, _draft_path.name, DSPARK_DRAFT_N_MAX, DSPARK_DRAFT_N_MIN,
                    )

        # ── Jinja-Chat-Template ───────────────────────────────────────────────
        _JINJA_BASES = {"qwen3.5", "qwen3.5", "qwen3-vl", "qwen3", "qwen3.6", "qwen3.8", "omnicoder", "hermes3.6", "lfm2.5"}

        # User-config (model_configs/models/*.json): jinja / reasoning / distilled / template.
        _reg_jinja = None
        _reg_reasoning = None
        _reg_distilled = None
        _reg_template = None
        try:
            from model_configs.models_registry import (
                is_jinja as _reg_is_jinja,
                get_reasoning as _reg_get_reasoning,
                is_distilled as _reg_is_distilled,
                get_chat_template as _reg_get_template,
            )
            _reg_jinja = _reg_is_jinja(model)
            _reg_reasoning = _reg_get_reasoning(model)
            _reg_distilled = _reg_is_distilled(model)
            _reg_template = _reg_get_template(model)
        except Exception:
            pass

        #
        # Per-Request-Kontrolle via /no_think Token im System-Prompt (server.py Tool-Calls).
        #
        _DISTILLED_MODELS = {"qwen3.5:2b-d", "qwen3.5:4b-d", "qwen3.5:9b-d", "qwen3.5:4b-ud-v3", "qwen3.5:9b-ud"}
        # Server <think>-Inhalte in reasoning_content → split_thinking + thinking_budget greifen.
        _REASONING_ON_BASES = {"qwen3.6", "hermes3.6", "hermes"}
        if type(self)._reasoning_override is not None:
            _ro = type(self)._reasoning_override
            type(self)._reasoning_override = None  # consume override
            cmd += ["--reasoning", "on" if _ro else "off"]
            logger.info(f"--reasoning {'on' if _ro else 'off'} for {model} (user override)")
        elif _reg_reasoning in ("on", "off"):
            cmd += ["--reasoning", _reg_reasoning]
            logger.info(f"--reasoning {_reg_reasoning} for {model} (registry config)")
        elif model in _DISTILLED_MODELS or _reg_distilled:
            cmd += ["--reasoning", "on"]
            logger.info(f"--reasoning on for distilled model: {model}")
        elif _model_base in _REASONING_ON_BASES:
            cmd += ["--reasoning", "on"]
            logger.info(f"--reasoning on for thinking base: {model}")
        elif _model_base in ("qwen3.5", "omnicoder"):
            cmd += ["--reasoning", "off"]

        # ── mmproj (Vision-Projektor) ─────────────────────────────────────────
        if _needs_mmproj(model, vision=vision):
            _mmproj_resolved = None
            try:
                _mmproj = resolve_mmproj_path(model)
                if _mmproj:
                    _p = Path(_mmproj)
                    if _p.exists():
                        _mmproj_resolved = _p
                    else:
                        logger.warning(f"mmproj path does not exist on disk: {_p}")
            except Exception as e:
                logger.warning(f"mmproj lookup failed for {model}: {e}")

            if _mmproj_resolved is None:
                _gguf_dir = Path(gguf_path).parent
                _size_tag = (model.split(":")[1].lower() if ":" in model else "").replace("b", "").strip()
                _all_mmproj = (list(_gguf_dir.glob("*mmproj*.gguf"))
                               + list(_gguf_dir.glob("*projector*.gguf")))
                if _all_mmproj:
                    # Versuche exakten Size-Tag-Match (z.B. "4b" in Dateiname)
                    _tag_matches = [p for p in _all_mmproj
                                    if _size_tag and _size_tag in p.stem.lower()]
                    _mmproj_resolved = _tag_matches[0] if _tag_matches else _all_mmproj[0]
                    logger.info(f"mmproj Fallback (GGUF-Verzeichnis): {_mmproj_resolved}")

            if _mmproj_resolved:
                cmd += ["--mmproj", str(_mmproj_resolved)]
                logger.info(f"mmproj: {_mmproj_resolved}")
            else:
                logger.warning(
                    f"WARNUNG: Kein mmproj für '{model}' gefunden.\n"
                    f"  Das Modell startet, aber Vision-Anfragen (Bilder) werden fehlschlagen.\n"
                    f"  Lösungen:\n"
                    f"    1. Modell in Ollama neu pullen: ollama pull {model}\n"
                    f"    2. mmproj.gguf manuell in {Path(gguf_path).parent} ablegen\n"
                    f"    3. models.json um einen 'mmproj'-Key ergänzen (falls llama_models.py das unterstützt)"
                )

        _use_jinja = _reg_jinja if _reg_jinja is not None else (_model_base in _JINJA_BASES)
        if _use_jinja:
            cmd += ["--jinja"]
            logger.info(f"--jinja enabled for {model}")
        # Registry-Template (expliziter Pfad oder Basisname) hat Vorrang.
        if _reg_template:
            _templates_dir = Path(__file__).resolve().parent.parent / "model_configs"
            _reg_template_path = Path(_reg_template)
            if not _reg_template_path.is_absolute():
                _reg_template_path = _templates_dir / _reg_template_path
            if _reg_template_path.exists():
                cmd += ["--chat-template-file", str(_reg_template_path)]
                logger.info(f"--chat-template-file (Registry): {_reg_template_path}")
            else:
                logger.warning(f"Registry chat template does not exist: {_reg_template_path}")
        elif _model_base in ("qwen3.5", "qwen3.6", "qwen3.8"):
            # Bases — reasoning_effort-Steuerung (<|think_*|>), preserve_reasoning,
            # \n\n → KV-Cache/MTP-Draft-Gewinne) und message.reasoning-Extraktion.
            # Fallback-Kette (Repo-relativ): v22.5 → Repo-Fallback qwen3.6_template.
            _templates_dir = Path(__file__).resolve().parent.parent / "model_configs"
            _template_v25 = _templates_dir / "chat_template22.5.jinja"
            if _template_v25.exists():
                _template_path = _template_v25
            else:
                _template_path = _templates_dir / "qwen3.6_chat_template.jinja"
            if _template_path.exists():
                cmd += ["--chat-template-file", str(_template_path)]
                logger.info(f"--chat-template-file: {_template_path}")
        if _model_base == "hermes3.6":
            _templates_dir = Path(__file__).resolve().parent.parent / "model_configs"
            _template_v25h = _templates_dir / "chat_template22.5.jinja"
            if _template_v25h.exists():
                _template_path = _template_v25h
            else:
                _template_path = _templates_dir / "qwen3.6_chat_template.jinja"
            if _template_path.exists():
                cmd += ["--chat-template-file", str(_template_path)]
                logger.info(f"--chat-template-file: {_template_path}")

        # ── HARTER PRE-FLIGHT-VRAM-GATE ───────────────────────────────────────
        _fit = self.can_fit(model, num_ctx, exclude_slot_id=slot.slot_id)
        logger.info(
            "[PRE-FLIGHT] %s @ctx=%d: needed=%.0fMiB + margin=%dMiB vs free=%.0fMiB (%s) → %s",
            model, num_ctx, _fit.needed_mib, _fit.margin_mib, _fit.free_mib, _fit.source,
            "OK" if _fit.ok else "BLOCK",
        )
        if not _fit.ok and _evicted_here:
            _fit = await self._pre_flight_grace_recheck(model, num_ctx, slot)
        #      gepinnte Slots bleiben unantastbar).
        if not _fit.ok:
            _recovered = False
            _scan_actions = 0
            try:
                self._last_orphan_scan_ts = time.time()
                _scan_actions = await self._adopt_orphan_slots()
            except Exception as _scan_exc:
                logger.warning(
                    "[PRE-FLIGHT] orphan-adoption scan failed: %s", _scan_exc
                )
            if _scan_actions:
                logger.info(
                    "[PRE-FLIGHT] %d ungetrackte Port-Aktion(en) (adopt/kill) nach Block",
                    _scan_actions,
                )
                _fit = self.can_fit(model, num_ctx, exclude_slot_id=slot.slot_id)
                if not _fit.ok:
                    _fit = await self._pre_flight_grace_recheck(model, num_ctx, slot)
                _recovered = _fit.ok
            if not _recovered and not _fit.ok:
                while not self.can_fit(model, num_ctx, exclude_slot_id=slot.slot_id).ok:
                    _cands = [
                        s for s in self._slots
                        if s is not slot and s.is_running
                        and not s.pinned and not getattr(s, "_loading", False)
                    ]
                    if not _cands:
                        break
                    _victim = min(_cands, key=lambda s: s.last_used)
                    self._metric_inc("evictions_total")
                    self._metric_inc("evictions_preflight")
                    logger.info(
                        "[PRE-FLIGHT-EVICT] %s (slot %d) geopfert für Load %s @ctx=%d",
                        _victim.model, _victim.slot_id, model, num_ctx,
                    )
                    _victim.kill()
                    _fit = await self._pre_flight_grace_recheck(model, num_ctx, slot)
                    if _fit.ok:
                        break
        if not _fit.ok:
            # AUDIT-FIX 2026-08-03: Diagnose-Attribute + Zeitreihen-Log.
            # external_usage_est = Total - frei - eigene geladene Slots (Fremdbelegung).
            _current_own = sum(
                vram_of_moe(_strip_alias(s.model), s._num_ctx if s._num_ctx > 0 else 4096)
                for s in self._slots if s.is_running and s.model
            )
            _ext_est = max(0, int(TOTAL_VRAM_MIB - _fit.free_mib - _current_own))
            _min_needed = vram_of_moe(_strip_alias(model), 4096) + _fit.margin_mib
            _fixed_dominant = _min_needed > _fit.free_mib
            # CTX-AUTO-DOWNGRADE (2026-09-01): before hard-blocking on a VRAM
            # shortfall, try progressively smaller context windows (e.g. a
            # leftover ctx_override of 32768 on an 8 GB GPU). If a smaller ctx
            # fits, load with that instead of failing the run.
            _downgraded = False
            if not _fixed_dominant:
                _ctx_cur = num_ctx
                _ctx_attempts = [16384, 8192, 4096]
                if _ctx_cur not in _ctx_attempts:
                    _ctx_attempts.insert(0, _ctx_cur)
                _ctx_seen: set[int] = set()
                for _cand in _ctx_attempts:
                    if _cand in _ctx_seen or _cand >= _ctx_cur:
                        continue
                    _ctx_seen.add(_cand)
                    _fit_c = self.can_fit(model, _cand, exclude_slot_id=slot.slot_id)
                    if _fit_c.ok:
                        logger.warning(
                            "[PRE-FLIGHT-CTX-DOWN] %s @ctx=%d passt nicht "
                            "(%dMiB frei < %dMiB nötig) — versuche ctx=%d",
                            model, _ctx_cur, int(_fit.free_mib), int(_fit.needed_mib), _cand,
                        )
                        num_ctx = _cand
                        slot._num_ctx = _cand
                        # Rewrite the --ctx-size flag that was baked into cmd
                        # before the pre-flight check.
                        for _ci, _ca in enumerate(cmd):
                            if _ca == "--ctx-size" and _ci + 1 < len(cmd):
                                cmd[_ci + 1] = str(num_ctx)
                                break
                        _fit = _fit_c
                        _downgraded = True
                        break
            if not _downgraded:
                # MODEL-SUGGEST (2026-09-01): when nothing fits at any ctx, name
                # fitting alternatives (verified via can_fit, availability-checked)
                # so the user knows what to pick on the Agent tab instead of a
                # generic "ctx senken" hint.
                _cands: list[str] = []
                try:
                    from .llama_vram_table import _VRAM_TABLE as _VRAM_TBL
                    _avail_models = set(list_available_models() or [])
                    for _sm, _gb in sorted(_VRAM_TBL.items(), key=lambda kv: kv[1]):
                        _base = _sm.split(":")[0].lower()
                        if _sm.lower() == _strip_alias(model).lower():
                            continue
                        if _base in _OLLAMA_ONLY_BASES:
                            continue
                        if _avail_models and _sm not in _avail_models:
                            continue
                        try:
                            if self.can_fit(_sm, 4096, exclude_slot_id=slot.slot_id).ok:
                                _cands.append(_sm)
                        except Exception:
                            pass
                except Exception:
                    pass
                # Spread the suggestions (smallest / mid / largest that fit)
                # instead of only the tiniest models.
                if len(_cands) <= 3:
                    _sugg = _cands
                else:
                    _sugg = [_cands[0], _cands[len(_cands) // 2], _cands[-1]]
                _sugg_txt = ", ".join(_sugg) if _sugg else "ein kleineres Modell im Agent-Tab wählen"
                logger.warning(
                    "[PRE-FLIGHT-BLOCK] %s @ctx=%d: external_usage_est=%d MiB "
                    "fixed_cost_dominant=%s needed=%dMiB free=%dMiB (Quelle: %s)",
                    model, num_ctx, _ext_est, _fixed_dominant,
                    int(_fit.needed_mib), int(_fit.free_mib), _fit.source,
                )
                raise VRAMPreFlightError(
                    model=model, num_ctx=num_ctx,
                    needed_mib=_fit.needed_mib, free_mib=_fit.free_mib, source=_fit.source,
                    external_usage_est_mib=_ext_est, fixed_cost_dominant=_fixed_dominant,
                    message=(
                        f"VRAM-Pre-Flight-Check fehlgeschlagen für '{model}' @ ctx={num_ctx}:\n"
                        f"  benötigt: {_fit.needed_mib:.0f} MiB + {_fit.margin_mib} MiB Sicherheitsmarge "
                        f"= {_fit.needed_mib + _fit.margin_mib:.0f} MiB\n"
                        f"  frei:     {_fit.free_mib:.0f} MiB (Quelle: {_fit.source})\n"
                        f"  extern:   ~{_ext_est} MiB Fremdbelegung (geschätzt)\n"
                        f"  → Vorschlag: Modell im Agent-Tab wechseln — z. B. {_sugg_txt}.\n"
                        f"  → Oder andere GPU-Nutzer schließen und erneut versuchen."
                    ),
                )

        # ── Log-Datei ─────────────────────────────────────────────────────────
        _log_path = Path(__file__).parent.parent / "logs" / f"llama_server_{slot.port}.log"
        _log_file = open(_log_path, "w", encoding="utf-8", errors="replace")
        _log_file_closed = False
        try:
            _log_file.write(
                f"=== llama-server START ===\n"
                f"Modell  : {model}\n"
                f"GGUF    : {gguf_path}\n"
                f"Port    : {slot.port}\n"
                f"ctx     : {num_ctx}\n"
                f"CMD     : {' '.join(cmd)}\n"
                f"========================\n\n"
            )
            _log_file.flush()

            await self._kill_port(slot.port)

            logger.info(f"Starting llama-server: model={model} port={slot.port} ctx={num_ctx}")
            logger.info(f"GGUF path: {gguf_path}")
            logger.info(f"CMD: {' '.join(cmd)}")
            try:
                import os as _os_diag
                _gguf_gb = (_os_diag.path.getsize(gguf_path) / (1024 ** 3)) if gguf_path and _os_diag.path.exists(gguf_path) else -1.0
            except Exception:
                _gguf_gb = -1.0
            logger.info(
                "[LOAD-DIAG] mlock=%s ctx=%d gguf=%.1fGB ram_free=%.1fGB",
                bool(MLOCK_MODEL), num_ctx, _gguf_gb, _available_ram_gb(),
            )

            slot._ready_event.clear()

            # ── VULKAN-INIT-SERIALISIERUNG ─────────────────────────────────────────
            logger.info(f"Waiting for GPU-init semaphore for {model} ...")
            async with self._vulkan_init_sem:
                logger.info(f"GPU-init semaphore acquired for {model}")
                slot.process   = subprocess.Popen(
                    cmd,
                    stdout=_log_file,
                    stderr=_log_file,
                    cwd=str(LLAMA_BIN.parent),  # WIN-FIX: DLLs (ggml-vulkan.dll etc.) relativ zur Binary finden
                    creationflags=_WIN_CNF,
                )
                _log_file.close()
                _log_file_closed = True
                slot.model      = model
                slot.loaded_at  = time.time()
                slot._vision    = vision
                slot._num_ctx   = num_ctx
                slot._n_parallel = n_parallel
                slot._jinja      = "--jinja" in cmd

                ready = await slot.wait_ready(
                    timeout=LLAMA_STARTUP_READY_TIMEOUT_SECONDS,
                    http_client=self._get_shared_http_client(),
                )

                # AMD-VULKAN-COMMIT-FIX:
                if ready:
                    _other_running = [
                        s for s in self._slots
                        if s.slot_id != slot.slot_id and (s.is_running or s._loading)
                    ]
                    if _other_running:
                        # AMD-VULKAN-ADAPTIVE-SLEEP:
                        # Formel: ctx=4096 → 2.0s | ctx=8192 → 4.0s | ctx=12800 → 6.0s (cap)
                        _vk_commit_s = round(min(6.0, max(2.0, 2.0 * (num_ctx / 4096))), 1)
                        logger.info(
                            f"AMD-Vulkan-Commit-Wait: {model} geladen, warte {_vk_commit_s}s "
                            f"auf VRAM-Sichtbarkeit fuer naechsten Slot "
                            f"(ctx={num_ctx}, {len(_other_running)} andere Slot(s) aktiv)"
                        )
                        await asyncio.sleep(_vk_commit_s)
            slot._loading = False
            if not ready:
                _exit = slot.process.poll() if slot.process else -1
                _log_content = _log_path.read_text(encoding="utf-8", errors="replace")[-8000:]
                slot.kill()
                if _exit is not None:
                    # ── Spezifische Fehlerdiagnose ────────────────────────────
                    if "key not found in model: qwen3vl.rope" in _log_content:
                        _extra = (
                            "\n\n→ qwen3-vl Ollama-GGUF inkompatibel mit aktuellem Binary:\n"
                            "  Der Key 'qwen3vl.rope.dimension_sections' fehlt im Ollama-Blob.\n"
                            "  Lösung A: Unsloth GGUF laden und in models.json eintragen:\n"
                            "    (GGUF in den konfigurierten Models-Ordner legen,\n"
                            "     z.B. Qwen3-VL-2B-Instruct-Q4_K_M.gguf)\n"
                            "  Lösung B: Neueres Binary (b8300+) von\n"
                            "  https://github.com/ggerganov/llama.cpp/releases"
                        )
                    elif "rope.dimension_sections has wrong array length" in _log_content:
                        _build_hint = (
                            f" (Binary: b{type(self)._binary_build_number})"
                            if type(self)._binary_build_number
                            else ""
                        )
                        _extra = (
                            f"\n\n→ GGUF-FORMAT INKOMPATIBEL: Ollama-Blob für dieses Modell\n"
                            f"  verwendet rope.dimension_sections mit 3 Elementen, aber llama.cpp{ _build_hint}\n"
                            f"  erwartet 4 Elemente (qwen3.5 VL-Blob vs. Text-only GGUF).\n"
                            f"  Ursache: models.json Override-Pfad existiert nicht → Fallback zu kaputtem Ollama-Blob.\n"
                            f"  Lösung A: Kompatiblen Unsloth UD-GGUF herunterladen (setup_models.bat)\n"
                            f"    oder: python -c \"from huggingface_hub import snapshot_download; "
                            f"snapshot_download('unsloth/Qwen3.5-2B-GGUF', "
                            f"local_dir=r'<models-dir>', "
                            f"allow_patterns=['*UD-Q4_K_XL*'])\"\n"
                            f"  Lösung B: models.json prüfen — der Pfad muss auf einen GGUF im\n"
                            f"    konfigurierten Models-Ordner zeigen,\n"
                            f"    NICHT auf .ollama/models/blobs/ (das ist der kaputte VL-Blob).\n"
                            f"  Lösung C: Anderes Modell verwenden (z.B. granite4:3b statt qwen3.5:2b)."
                        )
                    elif "cannot open model file" in _log_content or "failed to open" in _log_content.lower():
                        _extra = (
                            "\n\n→ GGUF-DATEI NICHT GEFUNDEN: Prüfe ob die Datei noch vorhanden ist.\n"
                            "  Lösung: hive_functions/scan_models.py erneut ausführen → models.json wird aktualisiert."
                        )
                    elif "exceed_context_size" in _log_content or "exceeds the available context size" in _log_content:
                        _extra = (
                            "\n\n→ CONTEXT ZU KLEIN: Das Modell wurde mit zu kleinem --ctx-size gestartet.\n"
                            "  Für Vision-Modelle die Bilder verarbeiten: ctx mindestens 8192.\n"
                            "  Fix: num_ctx_config.py → Wert für dieses Modell erhöhen."
                        )
                    elif "CUDA error" in _log_content or "Vulkan error" in _log_content:
                        _extra = (
                            "\n\n→ GPU-FEHLER: Treiber-Problem oder VRAM-Overflow.\n"
                            "  Prüfe ob ein anderer Prozess den VRAM belegt (GPU-Z oder Task-Manager)."
                        )
                    elif (
                        "load_tensors" in _log_content
                        and _exit == -1
                        and not any(e in _log_content for e in ("Vulkan error", "CUDA error", "rope.dimension"))
                        and _model_base in ("qwen3.5", "qwen3-vl", "qwen3")
                    ):
                        # SSM/Mamba-Architektur (qwen3.5) crasht auf Vulkan beim Tensor-Upload
                        _build_hint = (
                            f" (aktuell: build {type(self)._binary_build_number})"
                            if type(self)._binary_build_number
                            else ""
                        )
                        _extra = (
                            f"\n\n→ VULKAN SSM-CRASH: {model} verwendet Mamba-SSM-Layers die auf "
                            f"Vulkan erst ab llama.cpp b8300+ stabil sind{_build_hint}.\n"
                            f"  Lösung A (empfohlen): Neueres Binary laden:\n"
                            f"    https://github.com/ggerganov/llama.cpp/releases\n"
                            f"    Datei: llama-b8300+-bin-win-vulkan-x64.zip\n"
                            f"    → Dann llama_config.py → LLAMA_BIN aktualisieren\n"
                            f"  Lösung B: CPU-Fallback (langsamer, aber stabil):\n"
                            f"    llama_config.py → GPU_LAYERS = 0"
                        )
                    else:
                        _extra = ""
                    _msg = (
                        f"llama-server fuer '{model}' gecrasht (exit={_exit}).\n"
                        f"Log ({_log_path}):\n{_log_content}"
                        f"{_extra}\n\n"
                        f"Häufige Ursachen:\n"
                        f"  1. Binary-Version inkompatibel mit GGUF (z.B. neues qwen3.5 → b8300+ nötig)\n"
                        f"  2. GGUF-Datei beschädigt, verschoben oder falsches Format\n"
                        f"  3. Port {slot.port} bereits belegt (taskkill /F /IM llama-server.exe)\n"
                        f"  4. Unzureichend VRAM/RAM (Vision-Modelle + Bild: mind. 8192 ctx)"
                    )
                else:
                    _msg = (
                        f"llama-server fuer '{model}' Timeout ({int(LLAMA_STARTUP_READY_TIMEOUT_SECONDS)}s, Prozess laeuft noch).\n"
                        f"Log ({_log_path}):\n{_log_content}"
                    )
                raise RuntimeError(_msg)

            slot._ready_event.set()
            logger.info(f"Slot {slot.slot_id}: {model} ready on port {slot.port}")
            # SWA-FIX: populate swa_window from /props for fresh loads
            # (startup_cleanup only covers orphan slots)
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(connect=2.0, read=10.0, write=2.0, pool=2.0)) as _c:
                    _p = (await _c.get(f"http://127.0.0.1:{slot.port}/props")).json()
                    _attn = _p.get("attention", {})
                    slot.swa_window = int(
                        _attn.get("sliding_window_size") or
                        _attn.get("sliding_window") or
                        _attn.get("rope_sliding_window") or 0
                    )
            except Exception:
                slot.swa_window = 0
        finally:
            if not _log_file_closed:
                try:
                    _log_file.close()
                except Exception:
                    pass
