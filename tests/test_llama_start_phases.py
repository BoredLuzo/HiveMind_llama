# -*- coding: utf-8 -*-
"""Behavior test: backend/llama_start_phases._phase_flags (2026-11-07).

The 1,534-line split module had zero behavior coverage. This pins the flag
resolution: build gate, gpu-layer override precedence, CPU fallback, --fit
support and the thread defaults. The process seam is mocked.
"""
import asyncio
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import backend.llama_config as LC  # noqa: E402
import backend.llama_start_phases as P  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


class _Mgr:
    # far above every BINARY_MIN_BUILD entry -> no gate
    _binary_build_number = 99999


def _run(model, override, *, gpu_layers=33, backend="vulkan", fit=False,
         fit_target=0, help_fit=True, mgr=_Mgr):
    with mock.patch.object(P, "resolve_gpu_layers", return_value=gpu_layers), \
         mock.patch.object(P, "llama_gpu_layers_auto", return_value=False), \
         mock.patch.object(P, "GPU_BACKEND", backend), \
         mock.patch.object(LC, "LLAMA_FIT", fit), \
         mock.patch.object(LC, "LLAMA_FIT_TARGET_MIB", fit_target), \
         mock.patch("backend.manager_load._help_supports",
                    new=mock.AsyncMock(return_value=help_fit)):
        return asyncio.run(P._phase_flags(mgr(), model, override))


# ── 1. plain model: resolver value, model base, threads ────────────────────
ph = _run("testmodel:1b", None)
check("gpu layers come from the resolver", ph.gpu_layers == 33, f" ({ph.gpu_layers})")
check("model base strips alias/tag", ph.model_base == "testmodel", f" ({ph.model_base})")
check("fit off by default", ph.fit_supported is False and ph.fit_target == 0)
check("threads default (Windows 16/8 or POSIX cpu-count)",
      ph.def_threads >= 2 and ph.def_threads_batch >= 2,
      f" ({ph.def_threads}/{ph.def_threads_batch})")

# ── 2. binary too old -> RuntimeError before spawning ──────────────────────
class _OldMgr:
    _binary_build_number = 8300 - 1


try:
    _run("qwen3.5:4b", None, mgr=_OldMgr)
    _raised = False
except RuntimeError as exc:
    _raised = "Binary too old" in str(exc)
check("outdated binary is refused", _raised)

# ── 3. CPU backend forces zero GPU layers ──────────────────────────────────
ph_cpu = _run("testmodel:1b", None, gpu_layers=33, backend="cpu")
check("cpu backend -> gpu_layers 0", ph_cpu.gpu_layers == 0, f" ({ph_cpu.gpu_layers})")

# ── 4. explicit override wins / disables --fit ─────────────────────────────
ph_ov = _run("testmodel:1b", 12, gpu_layers=20, fit=True, fit_target=512)
check("explicit override wins", ph_ov.gpu_layers == 20, f" ({ph_ov.gpu_layers})")
check("override disables --fit", ph_ov.fit_supported is False)

# ── 5. --fit is only claimed when supported ────────────────────────────────
ph_fit = _run("testmodel:1b", None, fit=True, fit_target=512, help_fit=True)
check("--fit supported flag + target", ph_fit.fit_supported is True and ph_fit.fit_target == 512,
      f" ({ph_fit.fit_supported}/{ph_fit.fit_target})")
ph_nofit = _run("testmodel:1b", None, fit=True, fit_target=512, help_fit=False)
check("binary without --fit does not claim it", ph_nofit.fit_supported is False)

# ── 6. _phase_claim: slot preamble, file checks, eviction guards ───────────
class _Slot:
    def __init__(self, *, slot_id=1, port=8100, model="peer:1b",
                 num_ctx=4096, running=False, pinned=False):
        self.slot_id = slot_id
        self.port = port
        self.model = model
        self._num_ctx = num_ctx
        self.is_running = running
        self.pinned = pinned
        self._moe_cache_used = True
        self._orphan_port = 4242
        self.last_used = 0.0


class _MgrClaim:
    def __init__(self, slots=()):
        self._slots = list(slots)

    async def _port_alive(self, port):
        return False


def _claim(slot, *, resolved, slots=(), patches=()):
    stack = [mock.patch.object(P, "resolve_model_path", return_value=resolved)]
    stack.extend(patches)
    for p in stack:
        p.start()
    try:
        return asyncio.run(P._phase_claim(_MgrClaim(slots), slot, "m:1b", 4096))
    finally:
        for p in reversed(stack):
            p.stop()


def _missing(slot, *, resolved, models_dir):
    with mock.patch.object(P, "resolve_model_path", return_value=resolved), \
         mock.patch.object(LC, "MODELS_DIR", models_dir):
        try:
            asyncio.run(P._phase_claim(_MgrClaim(), slot, "m:1b", 4096))
        except FileNotFoundError as exc:
            return str(exc)
    return ""


with tempfile.TemporaryDirectory() as td:
    gguf = Path(td) / "m.gguf"
    gguf.write_bytes(b"x")

    slot = _Slot()
    ph = _claim(slot, resolved=str(gguf))
    check("claim: returns the resolved gguf", str(ph.gguf_path) == str(gguf),
          f" ({ph.gguf_path})")
    check("claim: no eviction without peers", ph.evicted is False)
    check("claim: stale moe cache flag is reset", slot._moe_cache_used is False)
    check("claim: recycled slot loses its orphan port", slot._orphan_port is None)
    check("claim: last_used is stamped for the idle monitor", slot.last_used > 0)

    slot_r = _Slot(running=True)
    with mock.patch.object(P, "_kill_slot_async", new=mock.AsyncMock()) as kill:
        ph_r = _claim(slot_r, resolved=str(gguf))
    check("claim: a running slot is killed", kill.await_count == 1)
    check("claim: the kill is reported as eviction", ph_r.evicted is True)

    msg_dir = _missing(_Slot(), resolved=None, models_dir=str(Path(td) / "nope"))
    check("claim: missing models dir names HIVEMIND_MODELS_DIR",
          "Models folder missing" in msg_dir and "HIVEMIND_MODELS_DIR" in msg_dir,
          f" ({msg_dir.splitlines()[0] if msg_dir else ''})")
    msg_ptr = _missing(_Slot(), resolved=None, models_dir=td)
    check("claim: unknown model points at scan_models",
          "Check models.json" in msg_ptr, f" ({msg_ptr.splitlines()[0] if msg_ptr else ''})")
    msg_gone = _missing(_Slot(), resolved=str(Path(td) / "gone.gguf"), models_dir=td)
    check("claim: missing gguf file is refused",
          "does not exist on disk" in msg_gone,
          f" ({msg_gone.splitlines()[0] if msg_gone else ''})")

    # over-budget peer: pinned => keep it, unpinned => kill it
    vram_patches = [
        mock.patch.object(P, "GPU_BACKEND", "vulkan"),
        mock.patch.object(P, "VRAM_BUDGET_GB", 7.5),
        mock.patch.object(P, "_VRAM_BASE_OVERHEAD_GB", 0.0),
        mock.patch.object(P, "vram_of_moe", return_value=10.0),
    ]
    peer_pinned = _Slot(slot_id=2, running=True, pinned=True)
    with mock.patch.object(P, "_kill_slot_async", new=mock.AsyncMock()) as kill:
        ph_pin = _claim(_Slot(slot_id=1), resolved=str(gguf),
                        slots=[peer_pinned], patches=vram_patches)
    check("claim: pinned peer is never evicted", kill.await_count == 0)
    check("claim: pinned peer leaves evicted false", ph_pin.evicted is False)

    peer_free = _Slot(slot_id=2, running=True, pinned=False)
    with mock.patch.object(P, "_kill_slot_async", new=mock.AsyncMock()) as kill, \
         mock.patch.object(P, "_slot_in_flight", return_value=False), \
         mock.patch.object(P, "wait_for_vram_reclaim",
                           new=mock.AsyncMock(return_value=True)) as reclaim, \
         mock.patch("backend.llama_start_phases.asyncio.sleep",
                    new=mock.AsyncMock()):
        ph_kill = _claim(_Slot(slot_id=1), resolved=str(gguf),
                         slots=[peer_free], patches=vram_patches)
    check("claim: unpinned peer is evicted", kill.await_count == 1)
    check("claim: eviction waits for VRAM reclaim", reclaim.await_count == 1)
    check("claim: peer eviction sets evicted", ph_kill.evicted is True)

sys.exit(h.finish("llama_start_phases"))
