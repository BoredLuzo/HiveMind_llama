"""_start_process phase helpers (2026-11-06 split 4.1).

The slot-claim preamble of _start_process as a module-level phase:
stale-cache flag reset, kill of a running slot, ollama-blob warning,
GGUF resolution checks and the VRAM eviction sweep (formula- and
live-measurement driven, pin/flight aware, with reclaim waits).
Verbatim code motion — no logic changes; `self` is passed explicitly.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import platform
import subprocess
import time
from pathlib import Path

from .llama_config import (
    BINARY_MIN_BUILD, DSPARK_DRAFT_N_MAX, DSPARK_DRAFT_N_MIN,
    DSPARK_MIN_BUILD, DSPARK_SPEC_TYPE, GPU_BACKEND, KV_CACHE_TYPE,
    LLAMA_BIN, LLAMA_UBATCH, MODELS_DIR, MTP_DRAFT_N_MAX, MTP_DRAFT_N_MIN,
    MTP_DRAFT_P_MIN, MTP_SPEC_TYPE, VULKAN_DEVICE, _GPU_LAYERS_TABLE,
    _MTP_MODELS, _MOE_EXPERT_COUNTS,
    dense_kv_cache_type, llama_gpu_layers_auto, moe_kv_cache_types,
    resolve_gpu_layers, ubatch_extra_mib,
)
from .llama_manager_utils import (
    VRAM_BUDGET_GB, VRAM_PRE_FLIGHT_REDUCED_MARGIN_MIB,
    _VRAM_BASE_OVERHEAD_GB, _kill_slot_async, _needs_mmproj,
    _probe_backend_devices, _probe_backend_dlls,
    _probe_binary_build, pick_mmproj_fallback,
)
from .llama_models import _strip_alias, resolve_model_path
from .llama_vram_table import (
    get_live_gpu_free_mib, vram_of_moe, wait_for_vram_reclaim,
)
from .manager_process import _slot_in_flight

logger = logging.getLogger("llama_manager")


@dataclasses.dataclass
class _ClaimPhase:
    """Phase-1 outputs (split 4.1): what the rest of _start_process
    consumes from the slot-claim preamble."""
    gguf_path: object = None
    evicted: bool = False


async def _phase_claim(self, slot, model: str, num_ctx: int) -> _ClaimPhase:
    _evicted_here = False
    # STALE-CACHE-FLAG (audit 2026-11-03): a start that fails before the
    # MoE section must not leave a previous load's True behind - the
    # retry would drop a flag the failed command never carried.
    slot._moe_cache_used = False
    if slot.is_running:
        await _kill_slot_async(slot)
        _evicted_here = True
    slot._orphan_port = None
    # AUDIT M2 (2026-10-24): stamp freshness at claim time — a recycled
    # slot's stale last_used let the idle monitor kill a SLOW LOAD
    # mid-start once the kill moved off the manager lock (A5).
    slot.last_used = time.monotonic()

    gguf_path = resolve_model_path(model)
    if not gguf_path:
        from .llama_config import MODELS_DIR as _MDIR
        if not Path(_MDIR).exists():
            raise FileNotFoundError(
                f"GGUF for '{model}' not found.\n"
                f"  → Models folder missing: {_MDIR}\n"
                f"  → Set HIVEMIND_MODELS_DIR (environment variable) to your models folder\n"
                f"    or run setup_models.bat to download the recommended models."
            )
        raise FileNotFoundError(
            f"GGUF for '{model}' not found.\n"
            f"  → Check models.json and run hive_functions/scan_models.py.\n"
            f"  → The path must point to a .gguf file."
        )
    if not Path(gguf_path).exists():
        raise FileNotFoundError(
            f"GGUF file does not exist on disk: {gguf_path}\n"
            f"  → The model was moved or deleted.\n"
            f"  → Update models.json or run hive_functions/scan_models.py again."
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
    # CPU-BACKEND (audit 2026-11-03): the eviction heuristic below is
    # pure VRAM math; on a CPU host it killed peers for a budget that
    # does not apply. Keep the peers.
    if running_slots and GPU_BACKEND != "cpu":
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
            # IN-FLIGHT-GUARD (audit 2026-11-03): this heuristic fired
            # even after the committed-VRAM guard passed, killing a peer
            # with an active round. Same rule as _evict_lru: never kill
            # a slot whose round is running (age-aware).
            _unpinned = [s for s in running_slots
                         if not s.pinned and not _slot_in_flight(s)]
            if not _unpinned:
                logger.warning(
                    f"_start_process [{model}]: all running peers "
                    f"({[s.model for s in running_slots]}) are pinned — "
                    f"VRAM eviction skipped "
                    f"(current={current_vram:.1f}+new={new_vram:.1f}>{VRAM_BUDGET_GB}GB). "
                    f"Caller validated the budget; AMD Vulkan retry covers real OOM."
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
                # VRAM reclaim wait: port dead != VRAM free (Vulkan is async)
                _reclaimed = await wait_for_vram_reclaim(int(new_vram * 1024 + VRAM_PRE_FLIGHT_REDUCED_MARGIN_MIB), timeout_sec=45, stall_abort_s=12.0)
                if not _reclaimed:
                    _free_now = get_live_gpu_free_mib()
                    logger.warning(
                        "Vulkan: VRAM reclaim timeout after eviction — "
                        "pre-flight could still block (only %.0fMiB free)",
                        _free_now if _free_now is not None else -1,
                    )
        else:
            _live_free = get_live_gpu_free_mib()
            # STALL-FIX (2026-09-07): reclaim target with REDUCED margin —
            # the 768 value was structurally unreachable under external GPU load
            # (guaranteed 45s wait, e.g. target 5775 with only ~5.06 GB free).
            # The 256 target is what the resolution accepts.
            # UBATCH-COMPUTE-BUFFER (2026-10-30): the table's vram_of()
            # has no ubatch term — the measured surcharge comes from
            # llama_config.ubatch_extra_mib (shared with can_fit, so the
            # gate and the eviction heuristic cannot drift).
            _ub_extra_mib = ubatch_extra_mib(int(LLAMA_UBATCH))
            _need_mib = (new_vram * 1024 + VRAM_PRE_FLIGHT_REDUCED_MARGIN_MIB
                         + _ub_extra_mib)

            _should_evict = False
            if _live_free is None:
                _formel_free_mib = (VRAM_BUDGET_GB - current_vram) * 1024
                if _formel_free_mib < _need_mib + 500:
                    _should_evict = True
                    logger.warning(
                        "Vulkan: live measurement unavailable, "
                        "formula is tight (%.0fMiB < %.0fMiB) — evicting conservatively",
                        _formel_free_mib, _need_mib + 500,
                    )
            elif _live_free < _need_mib:
                _should_evict = True
                logger.warning(
                    "Vulkan: formula says it fits (%.1f+%.1f<=%.1fGB), "
                    "but live measurement shows only %.0fMiB free (<%.0fMiB needed) — evicting",
                    current_vram, new_vram, VRAM_BUDGET_GB, _live_free, _need_mib,
                )

            if _should_evict:
                _to_kill = [s for s in running_slots
                            if not s.pinned and not _slot_in_flight(s)]
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
                _reclaimed = await wait_for_vram_reclaim(int(_need_mib), timeout_sec=45, stall_abort_s=12.0)
                if not _reclaimed:
                    _free_now = get_live_gpu_free_mib()
                    logger.warning(
                        "Vulkan: VRAM reclaim timeout after eviction — "
                        "pre-flight could still block (only %.0fMiB free)",
                        _free_now if _free_now is not None else -1,
                    )
            else:
                _free_str = f"{_live_free:.0f}MiB" if _live_free is not None else "N/A"
                logger.info(
                    "Vulkan: %s fits next to running "
                    "(formula: %.1f+%.1f<=%.1fGB, live: %s) — no kill needed",
                    model, current_vram, new_vram, VRAM_BUDGET_GB, _free_str,
                )

    return _ClaimPhase(gguf_path=gguf_path, evicted=_evicted_here)



@dataclasses.dataclass
class _FlagsPhase:
    """Phase-2 outputs (split 4.2): the flag/threads config the cmd
    builder consumes."""
    gpu_layers: object = None
    fit_supported: bool = False
    fit_target: int = 0
    def_threads: int = 16
    def_threads_batch: int = 8
    model_base: str = ""


async def _phase_flags(self, model: str, gpu_layers_override) -> _FlagsPhase:
    from backend.manager_load import _help_supports  # lazy: module cycle
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
            f"Binary too old for '{model}':\n"
            f"  Installed: build {_build}\n"
            f"  Required:  build {_min_build}+\n\n"
            f"  → Download a new binary:\n"
            f"    https://github.com/ggerganov/llama.cpp/releases\n"
            f"    File: llama-bXXXX-bin-win-vulkan-x64.zip\n"
            f"  → Then update llama_config.py → LLAMA_BIN"
        )
    elif _build == 0 and _min_build > 0:
        logger.warning(
            f"Build number of {LLAMA_BIN.name} could not be determined. "
            f"Build {_min_build}+ is recommended for '{model}'. "
            f"If the server crashes: update the binary to b{_min_build}+."
        )

    # ── Befehlszeile aufbauen ─────────────────────────────────────────────
    # B3 (audit): resolve_gpu_layers returns None in auto mode = omit the
    # flag so llama.cpp fits the layers to free VRAM itself.
    _fit_on = False
    _fit_target = 0
    try:
        from . import llama_config as _lc_fit
        _fit_on = bool(getattr(_lc_fit, "LLAMA_FIT", False))
        _fit_target = int(getattr(_lc_fit, "LLAMA_FIT_TARGET_MIB", 0) or 0)
    except (ImportError, AttributeError, TypeError, ValueError):
        _fit_on = False
        _fit_target = 0
    _fit_supported = bool(_fit_on and gpu_layers_override is None
                          and GPU_BACKEND != "cpu"
                          and await _help_supports("--fit"))
    if _fit_on and not _fit_supported:
        logger.info("--fit not applied (pinned layers/cpu backend or "
                    "binary lacks the flag)")
    _gpu_layers = resolve_gpu_layers(model, gpu_layers_override,
                                     _GPU_LAYERS_TABLE, llama_gpu_layers_auto())
    # OVERRIDE-PRECEDENCE (audit 2026-11-03): an explicit override wins
    # over the registry. The CPU fallback passes 0 — the registry block
    # used to replace it with the profile value, re-offloading the layers
    # that just crashed and defeating the fallback.
    if _gpu_layers is not None and gpu_layers_override is None:
        try:
            from .llama_config import _try_registry_gpu_layers as _reg_gl
            _gl_reg = _reg_gl(_strip_alias(model))
            if _gl_reg is not None:
                _gpu_layers = _gl_reg
        except (OSError, RuntimeError, ValueError, TypeError, KeyError,
                AttributeError, IndexError) as _reg_err:
            logger.debug("[GPU-LAYERS] registry lookup skipped: %s", _reg_err)
    # CPU-BACKEND (2026-09-08): every layer runs on the CPU — a GPU-layer
    # count from registry/table would request an offload to a GPU that
    # does not exist.
    if GPU_BACKEND == "cpu":
        _gpu_layers = 0
    import os as _os_th
    # THREADS-DEFAULT (2026-09-08): Windows keeps the proven 16/8 pair;
    # POSIX hosts get a CPU-count-aware default — a fixed 16 oversubscribes
    # small Linux boxes.
    if platform.system() == "Windows":
        _def_threads, _def_threads_batch = 16, 8
    else:
        _n_cpu = max(2, min(16, (_os_th.cpu_count() or 8)))
        _def_threads, _def_threads_batch = _n_cpu, _n_cpu

    return _FlagsPhase(
        gpu_layers=_gpu_layers, fit_supported=_fit_supported,
        fit_target=_fit_target, def_threads=_def_threads,
        def_threads_batch=_def_threads_batch, model_base=_model_base,
    )



@dataclasses.dataclass
class _CapsPhase:
    """Phase-3 outputs (split 4.3): the capability-augmented cmd and the
    resolved _load_mode (the [LOAD-DIAG] log reads it unconditionally
    after Popen)."""
    cmd: list = dataclasses.field(default_factory=list)
    load_mode: str = "none"


async def _phase_capabilities(self, cmd, _all_flags_uncached: bool) -> _CapsPhase:
    from backend.manager_load import _probe_all  # lazy: module cycle
    from . import llama_config as _llama_cfg
    if _all_flags_uncached:
        _bn, _dev, _kv, _moe, _lm, _dspark = await asyncio.to_thread(_probe_all, str(LLAMA_BIN))
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

    if GPU_BACKEND != "cpu" and type(self)._device_flag_supported:
        # CUDA → "CUDA<N>" (DEVICE-FORMAT-FIX 2026-08-27: llama.cpp
        if GPU_BACKEND == "cuda":
            cmd += ["--device", f"CUDA{VULKAN_DEVICE}"]
        elif GPU_BACKEND == "rocm":
            # HIP/ROCm names devices differently (ROCm0/HIP0 by build);
            # let llama.cpp pick the single visible device.
            pass
        else:
            cmd += ["--device", f"VULKAN{VULKAN_DEVICE}"]
    else:
        logger.warning(
            f"--device not supported by {LLAMA_BIN.name} - "
            "Vulkan device selection disabled. Update the binary to b8278+ recommended."
        )

    # ── Backend DLL check (CUDA/Vulkan) ───────────────────────────────
    # cryptic start failure. Cached once.
    if type(self)._backend_dlls_ok is None:
        # OS-GUARD (2026-09-19): DLL probing is Windows-only — on Linux
        # the .so layout differs and the probe misfires. llama-server
        # itself fails loudly at startup when its backend is broken,
        # and the manager's health check catches that (live: Docker/
        # CUDA host).
        if os.name == "nt":
            _dll_ok = await asyncio.to_thread(
                _probe_backend_dlls, str(LLAMA_BIN), GPU_BACKEND
            )
            if _dll_ok is not None:
                type(self)._backend_dlls_ok = _dll_ok
                logger.info(
                    "Backend DLL check (%s, %s): %s",
                    GPU_BACKEND.upper(), LLAMA_BIN.name,
                    "DLLs complete" if _dll_ok else "DLLs missing",
                )
        else:
            type(self)._backend_dlls_ok = True
            logger.info("Backend DLL check: skipped (non-Windows host)")
    if type(self)._backend_dlls_ok is False:
        _be_upper = GPU_BACKEND.upper()
        _posix_dll = platform.system() != "Windows"
        raise RuntimeError(
            f"{_be_upper} DLL check failed: the runtime DLLs for "
            f"'{GPU_BACKEND}' are missing under {LLAMA_BIN.parent}.\n\n"
            f"  Required ({GPU_BACKEND}): "
            + ("*ggml-cuda.{dll,so} (+ Windows: cudart64_*.dll, cublas64_*.dll, cublasLt64_*.dll)"
               if GPU_BACKEND == "cuda" else
               "*ggml-rocm.{dll,so} or *ggml-hip.{dll,so}"
               if GPU_BACKEND == "rocm" else
               "*ggml-vulkan.dll"
               if not _posix_dll else "lib/libggml-vulkan.so (or flat ggml-vulkan.so)")
            + "\n"
            f"  Binary folder: {LLAMA_BIN.parent}\n\n"
            f"  Fix A: re-download llama.cpp — the official ZIPs bundle "
            f"the matching DLLs:\n"
            f"    python deploy\\fetch_llamacpp.py --backend {GPU_BACKEND} --force\n"
            f"    (fetches the CUDA version matching the driver via nvidia-smi)\n"
            f"  Fix B: check folder {LLAMA_BIN.parent} — is it a "
            f"{_be_upper} build (name contains '{GPU_BACKEND}')?\n"
            f"  Fix C: antivirus/Defender quarantined DLLs — "
            f"restore the affected files."
        )

    # ── Backend device check (CUDA/Vulkan) ─────────────────────────────
    #      cuda-12.4 build on 13.x driver → cudaGetDeviceCount=0 →
    if type(self)._backend_devices_ok is None:
        # OS-GUARD (2026-09-19): same as the DLL check — --list-devices
        # misfires on some Linux/CUDA combos (cuda-12.4 build on 13.x
        # driver -> cudaGetDeviceCount=0), so the probe runs on Windows
        # only. A genuinely broken backend surfaces at llama-server
        # startup (health check / crash recovery).
        if os.name == "nt":
            _dev_ok = await asyncio.to_thread(
                _probe_backend_devices, str(LLAMA_BIN), GPU_BACKEND
            )
            if _dev_ok is not None:
                type(self)._backend_devices_ok = _dev_ok
                logger.info(
                    "Backend device check (%s, %s): %s",
                    GPU_BACKEND.upper(), LLAMA_BIN.name,
                    "device found" if _dev_ok else "NO device found",
                )
        else:
            type(self)._backend_devices_ok = True
            logger.info("Backend device check: skipped (non-Windows host)")
    if GPU_BACKEND != "cpu" and type(self)._backend_devices_ok is False and type(self)._device_flag_supported:
        _be_upper = GPU_BACKEND.upper()
        raise RuntimeError(
            f"{_be_upper} device check failed: {LLAMA_BIN.name} "
            f"lists NO {_be_upper} devices (--list-devices empty).\n\n"
            f"  Binary: {LLAMA_BIN}\n"
            f"  Backend: {GPU_BACKEND} (settings.json → 'gpu_backend' / env HIVEMIND_GPU_BACKEND)\n\n"
            f"  Most common cause: the build's CUDA runtime doesn't match the driver "
            f"(e.g. cuda-12.4 build on 13.x driver → GPU not detected).\n"
            f"  Fix A: re-download llama.cpp — it fetches the CUDA version "
            f"matching the driver automatically (nvidia-smi):\n"
            f"    python deploy\\fetch_llamacpp.py --backend {GPU_BACKEND} --force\n"
            f"  Fix B: chose a different binary? check the llama\\ folder — it must be a\n"
            f"    {_be_upper} build (folder contains '{GPU_BACKEND}').\n"
            f"  Fix C: point the backend in settings.json at the actually installed "
            f"binary (\"gpu_backend\": \"vulkan\" or \"cuda\")."
        )

    # A9a (audit 2026-11-03): the capability branch below is optional, but
    # the [LOAD-DIAG] log after Popen reads _load_mode unconditionally.
    # Binaries without --load-mode died with UnboundLocalError AFTER a
    # successful spawn.
    _load_mode = "none"
    # (2026-08-17, Hermes-35B-MoE auf Windows-Vulkan):
    #   --load-mode mlock      → GGML_ASSERT(addr) in llama-mmap.cpp (crasht)

    return _CapsPhase(cmd=cmd, load_mode=_load_mode)



@dataclasses.dataclass
class _MoePhase:
    """Phase-4 outputs (split 4.4): the MoE-augmented cmd; the cache
    flag rides on the slot (as before)."""
    cmd: list = dataclasses.field(default_factory=list)
    moe_cache_used: bool = False


async def _phase_moe(self, slot, cmd, model: str, num_ctx: int,
                     moe_cache: bool, _def_threads) -> _MoePhase:
    from backend.manager_load import _help_supports  # lazy: module cycle
    from . import llama_config as _llama_cfg
    _moe_model_key = _strip_alias(model).replace("-ud", "")
    # Per-Model-Override (dict {model_key: n_cpu_moe}) > Registry-Config > Tabellen-Default.
    _moe_count = 0
    # MOE-PIN (2026-10-14): profile "moe": false forces dense — no expert
    # offload even when another source (table/autodetect) suggests MoE.
    _moe_pinned_off = False
    try:
        from model_configs.models_registry import get_moe_pin as _reg_moe_pin
        if _reg_moe_pin(_moe_model_key) is False:
            _moe_pinned_off = True
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        _moe_pinned_off = False
    if _moe_pinned_off:
        logger.info("MoE pin: %s is dense per model config — expert offload skipped", _moe_model_key)
    # ORDER (2026-11-02): the per-model OVERRIDE comes first. It used to be
    # the other way round - the registry profile won, so a model with a
    # shipped moe_cpu_experts (groot: 32) ignored the UI value entirely.
    # The override is also read LIVE: a module-level `from ... import
    # MOE_CPU_EXPERTS` binds the dict from boot, and server.py replaces
    # the attribute on a settings change - the loader then kept looking at
    # an empty dict and the toggle did nothing.
    _moe_src = "table"
    try:
        from . import llama_config as _lc_live
        _moe_map = getattr(_lc_live, "MOE_CPU_EXPERTS", None)
        if not _moe_map:
            from . import llama_server_manager as _lsm_live
            _moe_map = getattr(_lsm_live, "MOE_CPU_EXPERTS", {})
        if isinstance(_moe_map, dict):
            _moe_count = int(_moe_map.get(_moe_model_key, 0) or 0)
            if _moe_count <= 0:
                for _ok, _ov in _moe_map.items():
                    if _moe_model_key.startswith(_ok) and int(_ov or 0) > 0:
                        _moe_count = int(_ov)
                        break
        else:
            _moe_count = int(_moe_map or 0)
        if _moe_count > 0:
            _moe_src = "override"
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        _moe_count = 0
    if _moe_count <= 0:
        try:
            from .llama_config import _try_registry_moe as _reg_moe
            _reg_moe_v = _reg_moe(_moe_model_key)
            if _reg_moe_v:
                _moe_count = _reg_moe_v
                _moe_src = "profile"
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as _reg_err:
            logger.debug("[MOE] profile MoE-count probe skipped: %s", _reg_err)
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
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError):
            _moe_count = 0
    if _moe_pinned_off:
        _moe_count = 0
        _moe_src = "pinned-dense"
    if _moe_count > 0:
        logger.info("MoE experts: %s -> %d (-n-cpu-moe, source=%s)",
                    _moe_model_key, _moe_count, _moe_src)

    # ── VRAM calibration guard (soft) ──
    # Prefix-aware (2026-10-21): the table keys carry no tag suffixes, so an
    # exact lookup silently skipped every "-a2b"/"-ud"/finetune tag. Use the
    # same resolution vram_of_moe uses, or the estimate and the loader drift
    # apart without a warning.
    from .llama_vram_table import moe_cfg_for as _moe_cfg_for
    _cal_cfg = _moe_cfg_for(_strip_alias(model).strip().lower())
    if _cal_cfg and "calibrated_n_cpu_moe" in _cal_cfg and _cal_cfg["calibrated_n_cpu_moe"] != _moe_count:
        logger.warning(
            f"[VRAM-CALIBRATION] {model}: --n-cpu-moe={_moe_count} deviates from the calibrated value "
            f"{_cal_cfg['calibrated_n_cpu_moe']} — the VRAM estimate (vram_of_moe) may now "
            f"be inaccurate. Recalibrate via -lv 5 "
            f"(KV/compute buffer size from the llama-server log)."
        )

    # ── KV cache quantization ────────────────────────────────────────────
    # DENSE-PROFILE-KV (2026-10-27): dense_kv_cache_type consults the
    # model profile first — the MoE path always did via
    # moe_kv_cache_types, dense only read the static family table, so a
    # profile q8_0 (spark-x2.6:4b) silently ran the global q4_0. No
    # profile value -> previous chain (family table -> global; an
    # f16/empty global still means "no quantization flags" for
    # profile-less models).
    if _moe_count <= 0:
        _kv_global_active = bool(KV_CACHE_TYPE) and KV_CACHE_TYPE.lower() not in ("f16", "")
        _dense_kv = dense_kv_cache_type(
            _moe_model_key,
            global_default=KV_CACHE_TYPE if _kv_global_active else "",
        )
        if _dense_kv:
            if type(self)._kv_flag_supported:
                cmd += ["--cache-type-k", _dense_kv, "--cache-type-v", _dense_kv]
                logger.info(f"KV cache quantization active: {_dense_kv}")
            else:
                logger.warning(
                    f"--cache-type-k {_dense_kv} not supported by "
                    f"{LLAMA_BIN.name} — falling back to f16. "
                    "Use a newer binary for q4_0 support."
                )

    # ── MoE-Offloading ────────────────────────────────────────────────────
    _moe_cache_mib = 0
    try:
        from . import llama_config as _lc_cache
        _moe_cache_mib = int(getattr(_lc_cache, "MOE_CACHE_MIB", 0) or 0)
    except (ImportError, AttributeError, TypeError, ValueError):
        _moe_cache_mib = 0
    _moe_cache_used = 0
    try:
        slot._moe_cache_used = False
    except AttributeError:
        pass
    if _moe_count > 0 and GPU_BACKEND != "cpu":
        if type(self)._moe_flag_supported:
            cmd += ["--n-cpu-moe", str(_moe_count)]
            logger.info(
                f"MoE offloading active: {_moe_count} experts on CPU "
                f"({_moe_model_key})"
            )
            # THREADS-MOE (2026-10-19, revised 2026-11-03): experts in RAM
            # make decode a memory-bound CPU gather. Use HALF of the
            # logical CPUs (the SMT sibling adds contention, not
            # throughput; measured 12.6 vs 16.5 tok/s at 16 vs 8 threads
            # on an 8c/16t Ryzen).
            try:
                import os as _os_thr
                try:
                    # respect cgroup/affinity limits where available
                    _logical = len(_os_thr.sched_getaffinity(0))
                except (AttributeError, OSError):
                    _logical = int(_os_thr.cpu_count() or 0)
            except (TypeError, ValueError):
                _logical = 0
            _half = max(1, _logical // 2) if _logical else 0
            _target = min(_def_threads, _half) if _half else _def_threads
            if _target < _def_threads:
                for _tflag in ("--threads", "--threads-batch"):
                    if _tflag in cmd:
                        _ti = cmd.index(_tflag)
                        cmd[_ti + 1] = str(_target)
                logger.info(
                    "MoE threads: %d -> %d (half of %d logical CPUs, "
                    "experts on CPU)", _def_threads, _target, _logical,
                )
            # EXPERT-CACHE (2026-11-03): opt-in --moe-cache-mib.
            if moe_cache and _moe_cache_mib > 0:
                if await _help_supports("--moe-cache-mib"):
                    if GPU_BACKEND == "vulkan":
                        logger.warning(
                            "MoE expert cache on Vulkan: llama.cpp #30697 "
                            "reports load crashes - a failed load is "
                            "retried once without the flag")
                    cmd += ["--moe-cache-mib", str(_moe_cache_mib)]
                    _moe_cache_used = int(_moe_cache_mib)
                    try:
                        slot._moe_cache_used = True
                    except AttributeError:
                        pass
                    logger.info(
                        "MoE expert cache: %d MiB (--moe-cache-mib)",
                        _moe_cache_mib)
                else:
                    logger.warning(
                        "--moe-cache-mib is not supported by %s - ignored",
                        LLAMA_BIN.name)
            # MoE-spezifische Optimierungen
            cmd += ["--no-warmup"]
            if not type(self)._load_mode_supported and _llama_cfg.MLOCK_MODEL:
                cmd += ["--mlock"]
                logger.info("--mlock active (model locked in RAM)")
            if type(self)._kv_flag_supported:
                _moe_kv = moe_kv_cache_types(_moe_model_key)
                cmd += ["--cache-type-k", _moe_kv, "--cache-type-v", _moe_kv]
                logger.info("MoE KV cache: %s (ctx=%d, VRAM optimised)",
                            _moe_kv, num_ctx)
        else:
            logger.warning(
                f"--n-cpu-moe {_moe_count} is not supported by "
                f"{LLAMA_BIN.name} - the MoE model runs without expert "
                "offloading. Use a newer binary (b8278+)."
            )

    return _MoePhase(cmd=cmd, moe_cache_used=bool(slot._moe_cache_used))



@dataclasses.dataclass
class _ModelFlagsPhase:
    """Phase-5 outputs (split 4.5): the model-config-augmented cmd and
    the resolved mmproj path (consumed by the spawn tail)."""
    cmd: list = dataclasses.field(default_factory=list)
    mmproj_resolved: object = None


async def _phase_model_flags(self, cmd, model: str, _model_base: str,
                             gguf_path, vision: bool) -> _ModelFlagsPhase:
    from backend.manager_load import (  # lazy: module cycle
        _append_chat_template, _help_supports, _JINJA_BASES,
        _omit_mmproj_for, _REASONING_ON_BASES,
    )
    from .llama_models import resolve_mmproj_path
    # ── MTP (Multi-Token Prediction / Speculative Decoding) ────────────────
    _mtp_model_key = _strip_alias(model)
    _mtp_from_registry = None
    try:
        from .llama_config import _try_registry_mtp as _reg_mtp
        _mtp_from_registry = _reg_mtp(_mtp_model_key)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as _e:
        logger.debug("[MTP] registry MTP probe skipped: %s", _e)
    _is_mtp = bool(_mtp_from_registry) if _mtp_from_registry is not None else (_mtp_model_key in _MTP_MODELS)
    # SPEC OFF-SWITCH (2026-09-17): HIVEMIND_MTP_SPEC=0 disables the
    # draft-mtp flags entirely (any model, MTP layers or not) —
    # draft-mtp disables llama-server's prompt cache-reuse and has
    # heavy prefill regressions on some backends. A/B via env without
    # touching model configs.
    # OFF-SWITCH INVERTED (1.3.9): the condition used to be the
    # enable-list itself, so =0 appended --spec-type draft-mtp to
    # EVERY model instead of switching it off.
    if os.environ.get("HIVEMIND_MTP_SPEC", "1") not in ("0", "false", "off") and _is_mtp:
        # profile overrides (mtp_draft_n_max / mtp_draft_p_min) win over
        # the module defaults; the values are clamped to sane ranges so a
        # typo cannot produce a 0-token draft or an 8-token greedy draft
        # (the card's sweep shows n_max 8 / p_min 0 collapses to 0.57x).
        _draft_nmax, _draft_pmin = MTP_DRAFT_N_MAX, MTP_DRAFT_P_MIN
        try:
            from model_configs.models_registry import get_profile as _gp
            _prof = _gp(_mtp_model_key) or {}
            if _prof.get("mtp_draft_n_max") is not None:
                _draft_nmax = int(_prof["mtp_draft_n_max"])
            if _prof.get("mtp_draft_p_min") is not None:
                _draft_pmin = float(_prof["mtp_draft_p_min"])
        except (ImportError, ValueError, TypeError) as _prof_err:
            logger.debug("MTP profile override skipped: %s", _prof_err)
        _draft_nmax = max(1, min(4, int(_draft_nmax)))
        _draft_pmin = max(0.0, min(0.95, float(_draft_pmin)))
        cmd += [
            "--spec-type", MTP_SPEC_TYPE,
            "--spec-draft-n-max", str(_draft_nmax),
            "--spec-draft-n-min", str(MTP_DRAFT_N_MIN),
        ]
        # --spec-draft-p-min exists from b12001 on; on older binaries the
        # flag would abort the load, so gate it on the help text we read.
        if await _help_supports("--spec-draft-p-min"):
            cmd += ["--spec-draft-p-min", f"{_draft_pmin:g}"]
        logger.info(
            "MTP active: %s (draft-max=%d, draft-min=%d, draft-p-min=%s%s)",
            MTP_SPEC_TYPE, _draft_nmax, MTP_DRAFT_N_MIN, f"{_draft_pmin:g}",
            "" if await _help_supports("--spec-draft-p-min") else " [flag unsupported - omitted]",
        )

    # ── DSpark external drafter (Speculative-Decoding Sidecar) ──────────
    # OFF-SWITCH (2026-10-19): HIVEMIND_DSPARK_SPEC=0 disables the drafter
    # for every model without touching any profile. Measured with the app's
    # own llama flags (Sharp-MiniCPM5-2B Q4 + this drafter, AMD Radeon iGPU,
    # 16 threads): target alone 14.1 tok/s, DSpark n-max 2
    # 10.6 (49% acceptance), n-max 4 7.7 (34%). Slower at every setting,
    # hence switchable.
    _draft_fn = None
    _draft_nmax = None
    try:
        from .llama_config import _try_registry_dspark_draft as _reg_dd2
        _draft_fn = _reg_dd2(_strip_alias(model))
        # Per-model draft budget from the profile (dspark_draft_n_max); the
        # field used to be ignored and everything ran with the global default.
        from model_configs.models_registry import (
            get_dspark_draft_n_max as _reg_nmax,
        )
        _draft_nmax = _reg_nmax(_strip_alias(model))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        _draft_fn = None
        _draft_nmax = None
    if os.environ.get("HIVEMIND_DSPARK_SPEC", "1") in ("0", "false", "off"):
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
                except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as _e:
                    logger.debug("[MTP] draft-path probe skipped: %s", _e)
        if _draft_path is None:
            logger.warning(
                "DSpark drafter configured for '%s' (%s) but not found in %s "
                "— running without the drafter.",
                model, _draft_fn, MODELS_DIR,
            )
        else:
            _dspark_ok = type(self)._dspark_flag_supported
            if _dspark_ok is None:
                _dspark_ok = (type(self)._binary_build_number or 0) >= DSPARK_MIN_BUILD
            if not _dspark_ok:
                logger.warning(
                    f"DSpark speculative decoding not supported for '{model}' by "
                    f"{LLAMA_BIN.name} (needs --spec-type draft-dspark, build "
                    f"{DSPARK_MIN_BUILD}+) — skipping the drafter."
                )
            else:
                if not _draft_nmax:
                    _draft_nmax = DSPARK_DRAFT_N_MAX
                cmd += [
                    "--spec-type", DSPARK_SPEC_TYPE,
                    "--model-draft", str(_draft_path),
                    "--spec-draft-n-max", str(_draft_nmax),
                    "--spec-draft-n-min", str(DSPARK_DRAFT_N_MIN),
                ]
                logger.info(
                    "DSpark active: %s → drafter %s (n-max=%d, n-min=%d)",
                    model, _draft_path.name, _draft_nmax, DSPARK_DRAFT_N_MIN,
                )

    # ── Jinja-Chat-Template ───────────────────────────────────────────────
    # (Bases: module-level _JINJA_BASES — registry "jinja" wins.)

    # User-config (model_configs/models/*.json): jinja / reasoning / distilled / template.
    _reg_jinja = None
    _reg_reasoning = None
    _reg_distilled = None
    _reg_template = None
    _reg_is_mtp = None   # audit: pre-bind — the MTP block below calls it outside this import's try
    try:
        from model_configs.models_registry import (
            is_jinja as _reg_is_jinja,
            get_reasoning as _reg_get_reasoning,
            is_distilled as _reg_is_distilled,
            get_chat_template as _reg_get_template,
            is_mtp as _reg_is_mtp,
        )
        _reg_jinja = _reg_is_jinja(model)
        _reg_reasoning = _reg_get_reasoning(model)
        _reg_distilled = _reg_is_distilled(model)
        _reg_template = _reg_get_template(model)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as _e:
        logger.debug("[MODEL-CONFIG] registry probe skipped: %s", _e)

    #
    # Per-Request-Kontrolle via /no_think Token im System-Prompt (server.py Tool-Calls).
    #
    _DISTILLED_MODELS = {"qwen3.5:2b-d", "qwen3.5:4b-d", "qwen3.5:9b-d", "qwen3.5:4b-ud-v3", "qwen3.5:9b-ud"}
    # Server <think>-Inhalte in reasoning_content → split_thinking + thinking_budget greifen.
    # (Bases: module-level _REASONING_ON_BASES — registry "reasoning" wins.)
    # Per-model override (duo tool-thinking toggle): the flag is bound to a
    # specific server instance, so it must only fire for the model it was
    # set for — a plain consume-once flag landed on whichever model loaded
    # first (e.g. the planner) and left the coder server at template default.
    _ro_for_model = (type(self)._reasoning_override or {}).pop(model, None)
    if type(self)._reasoning_override is not None and not type(self)._reasoning_override:
        type(self)._reasoning_override = None
    if _ro_for_model is not None:
        cmd += ["--reasoning", "on" if _ro_for_model else "off"]
        logger.info(f"--reasoning {'on' if _ro_for_model else 'off'} for {model} (user override)")
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

    # MTP SELF-SPECULATIVE DECODING (2026-10-08, user feedback: use mtp decoding with draft count 2):
    # registry mtp:true models carry predictive layers - draft-mtp uses
    # them as their own draft model. Verified on VULKAN: the server
    # accepts the flags and generates.
    try:
        if (_reg_is_mtp is not None and _reg_is_mtp(model)
                and "--spec-type" not in cmd
                # ENV-BYPASS FIX (audit 2026-10-24): the registry block
                # re-enabled draft-mtp even with HIVEMIND_MTP_SPEC=0 -
                # the off-switch only covered the name-list gate.
                and os.environ.get("HIVEMIND_MTP_SPEC", "1") not in ("0", "false", "off")):
            # FLAG-NAME FIX (2026-10-10, exit=1 evidence): this build wants
            # --spec-draft-n-max; the shipped --spec-draft-max is an
            # invalid argument and CRASHED EVERY mtp-model server start
            # (coder-load 3/3 failed -> runtime_load_error, run dead).
            # Guarded like the KV flags below: the flag must exist in the
            # binary's --help, otherwise the server starts undrafted.
            _help_txt = ""
            try:
                # HELP-OFF-LOOP (audit 2026-11-03): this probe blocked
                # the event loop up to 20 s on every MTP load.
                _help_txt = (await asyncio.to_thread(
                    subprocess.run, [cmd[0], "--help"],
                    capture_output=True, text=True, timeout=20,
                    encoding="utf-8", errors="replace")).stdout
            except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError):
                _help_txt = ""
            if "--spec-draft-n-max" in _help_txt:
                cmd += ["--spec-type", "draft-mtp", "--spec-draft-n-max", "2"]
                logger.info(f"--spec-type draft-mtp (n-max 2) enabled for {model} (registry mtp)")
            else:
                logger.warning("spec-draft-n-max not supported by this binary - %s starts undrafted", model)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as _e:
        logger.debug("[MTP] spec flags not applicable: %s", _e)

    # ── mmproj (Vision-Projektor) ─────────────────────────────────────────
    # TEST SWITCH (2026-10-04): the negative test for the
    # /props vision gate needs a server that RUNS without the projector
    # while the plan, the profile and the file on disk all say vision.
    # Renaming the file fires the OLDER attach-time file check instead -
    # this env var (default off) omits --mmproj at start so ONLY the
    # post-load /props gate can catch the drop. Value = model substring
    # (e.g. "9b-ud" scopes it to one role; "*" = all models).
    _mmproj_resolved = None  # H-audit: default for unlisted vision bases
    if _omit_mmproj_for(model, vision=vision):
        _mmproj_resolved = None
    elif _needs_mmproj(model, vision=vision):
        _mmproj_resolved = None
        try:
            _mmproj = resolve_mmproj_path(model)
            if _mmproj:
                _p = Path(_mmproj)
                if _p.exists():
                    _mmproj_resolved = _p
                else:
                    logger.warning(f"mmproj path does not exist on disk: {_p}")
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as e:
            logger.warning(f"mmproj lookup failed for {model}: {e}")

        if _mmproj_resolved is None:
            # MMFALLBACK-STRICT (2026-10-01): only a size-tag MATCHING file
            # is used — the old "first mmproj in directory" put a 2B
            # projector on a 4B model (broken vision, wrong dimensions)
            _gguf_dir = Path(gguf_path).parent
            _mmproj_resolved = pick_mmproj_fallback(_gguf_dir, model)
            if _mmproj_resolved:
                logger.info(f"mmproj fallback (GGUF directory): {_mmproj_resolved}")
            else:
                logger.warning(
                    f"mmproj fallback: no size-tag matching projector for '{model}' "
                    f"in {_gguf_dir} — server starts WITHOUT mmproj; image requests "
                    f"will return 0 tokens. Place a matching mmproj-*.gguf or set "
                    f"mmproj_filename in the model registry.")

        if _mmproj_resolved:
            cmd += ["--mmproj", str(_mmproj_resolved)]
            logger.info(f"mmproj: {_mmproj_resolved}")
        else:
            logger.warning(
                f"WARNING: no mmproj found for '{model}'.\n"
                f"  The model starts, but vision requests (images) will fail.\n"
                f"  Fixes:\n"
                f"    1. Re-pull the model in Ollama: ollama pull {model}\n"
                f"    2. Place mmproj.gguf manually in {Path(gguf_path).parent}\n"
                f"    3. Add an 'mmproj' key to models.json (if llama_models.py supports it)"
            )

    _use_jinja = _reg_jinja if _reg_jinja is not None else (_model_base in _JINJA_BASES)
    if _use_jinja:
        cmd += ["--jinja"]
        logger.info(f"--jinja enabled for {model}")
    # CHAT-TEMPLATE-ONE (2026-10-30, audit S2): one helper, one flag —
    # registry template wins, fallback chain only for qwen*/hermes bases.
    _append_chat_template(cmd, _model_base, _reg_template)

    return _ModelFlagsPhase(cmd=cmd, mmproj_resolved=_mmproj_resolved)
