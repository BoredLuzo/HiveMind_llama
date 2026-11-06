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
import time
from pathlib import Path

from .llama_config import (
    BINARY_MIN_BUILD, GPU_BACKEND, LLAMA_BIN, LLAMA_UBATCH, MODELS_DIR,
    VULKAN_DEVICE,
    llama_gpu_layers_auto, resolve_gpu_layers, ubatch_extra_mib, _GPU_LAYERS_TABLE,
)
from .llama_manager_utils import (
    VRAM_BUDGET_GB, VRAM_PRE_FLIGHT_REDUCED_MARGIN_MIB,
    _VRAM_BASE_OVERHEAD_GB, _kill_slot_async, _probe_backend_devices,
    _probe_backend_dlls, _probe_binary_build,
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
