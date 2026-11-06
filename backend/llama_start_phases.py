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
import time
from pathlib import Path

from .llama_config import GPU_BACKEND, LLAMA_UBATCH, MODELS_DIR, ubatch_extra_mib
from .llama_manager_utils import (
    VRAM_BUDGET_GB, VRAM_PRE_FLIGHT_REDUCED_MARGIN_MIB,
    _VRAM_BASE_OVERHEAD_GB, _kill_slot_async,
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
