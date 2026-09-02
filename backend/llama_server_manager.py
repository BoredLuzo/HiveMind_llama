


from __future__ import annotations

import asyncio
import platform
import random
import socket
import subprocess
import time
import httpx
import logging
from pathlib import Path
from typing import Optional
from collections import namedtuple


# ── Cross-Platform: CREATE_NO_WINDOW only exists on Windows ──
# On Linux/macOS, subprocess has no CREATE_NO_WINDOW attribute.
# Use 0 (no flags) as fallback so the same code runs everywhere.

from .llama_config import (
    LLAMA_BIN, BASE_PORT, MAX_SLOTS,
    KV_CACHE_TYPE, GPU_LAYERS, CONTEXT_SIZE_DEFAULT,
    DEFAULT_IDLE_TIMEOUT_SECONDS, VULKAN_DEVICE, GPU_BACKEND,
    BINARY_MIN_BUILD, MODELS_DIR,
    NO_MMAP_MIN_BUILD,
    MOE_CPU_EXPERTS, _MOE_EXPERT_COUNTS, _MOE_KV_CACHE_TYPES,
    MLOCK_MODEL, CACHE_REUSE,
    MTP_SPEC_TYPE, MTP_DRAFT_N_MAX, MTP_DRAFT_N_MIN,
    _MTP_MODELS,
    _GPU_LAYERS_TABLE,
)
from .llama_models import resolve_model_path, list_available_models, resolve_mmproj_path, _strip_alias
from .llama_vram_table import vram_of as _vram_of, vram_of_with_ctx as _vram_of_ctx, vram_of_moe, VRAM_OVERFLOW_MODELS, _MOE_TABLE, get_live_gpu_free_mib, wait_for_vram_reclaim, TOTAL_VRAM_MIB

logger = logging.getLogger("llama_manager")
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
from .manager_load import LlamaLoadMixin
from .manager_evict import LlamaEvictMixin
from .manager_process import LlamaProcessMixin
from .manager_prefetch import LlamaPrefetchMixin
from .manager_health import LlamaHealthMixin

from .llama_slots import ModelSlot

















# ── Slot ──────────────────────────────────────────────────────────────────────



# ── Manager ───────────────────────────────────────────────────────────────────



class LlamaServerManager(LlamaLoadMixin, LlamaEvictMixin, LlamaProcessMixin,
                          LlamaPrefetchMixin, LlamaHealthMixin):

    _kv_flag_supported: bool | None = None
    _moe_flag_supported: bool | None = None
    _dspark_flag_supported: bool | None = None
    _load_mode_supported: bool | None = None
    _device_flag_supported: bool | None = None
    _backend_devices_ok: bool | None = None
    _backend_dlls_ok: bool | None = None
    _reasoning_override: bool | None = None
    _binary_build_number: int | None = None
    def __init__(self):
        self._slots: list[ModelSlot]        = [ModelSlot(i) for i in range(MAX_SLOTS)]
        self._lock                          = asyncio.Lock()
        self._idle_task: Optional[asyncio.Task] = None
        self._pending_prefetch: list[tuple[str, int]] = []
        self._prefetch_tasks: set[asyncio.Task] = set()
        self._planner_critical_phase: bool = False
        self._vulkan_init_sem               = asyncio.Semaphore(1)
        self._shared_http_client: Optional[httpx.AsyncClient] = None
        self._last_orphan_scan_ts: float = 0.0
        self._telemetry: dict[str, int] = {
            "evictions_total": 0,
            "evictions_manual": 0,
            "evictions_lru": 0,
            "evictions_idle": 0,
            "evictions_ctx_reload": 0,
            "evictions_parallel_reload": 0,
            "evictions_preflight": 0,
            "orphan_rehabilitations": 0,
            "startup_orphan_rehabilitations": 0,
            "prefetch_enqueued": 0,
            "prefetch_dequeued": 0,
            "prefetch_failures": 0,
        }














    # ── Public API ────────────────────────────────────────────────────────────













    # ── Interne Helpers ───────────────────────────────────────────────────────



















# ── Singleton ─────────────────────────────────────────────────────────────────
manager = LlamaServerManager()