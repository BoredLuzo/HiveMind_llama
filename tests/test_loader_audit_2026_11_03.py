# -*- coding: utf-8 -*-
"""Backend/Loader deep audit (2026-11-03): regressions as behavior, not grep.

The audit found the grace recheck dropped the MoE-cache surcharge, the
registry overrode the explicit CPU fallback, and ROCm was hard-blocked by
vulkan/cuda-only probes. The previous tests only grepped for substrings that
already existed elsewhere in the file, so none of these were caught.
"""
import asyncio
import platform
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import llama_manager_utils as U          # noqa: E402
from backend import llama_vram_table as VT            # noqa: E402
from backend import manager_evict as ME               # noqa: E402
from backend import manager_load as ML                # noqa: E402

# split 4.1: the _start_process slot-claim phase lives in llama_start_phases.py
ML_SRC = ((ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
          + (ROOT / "backend" / "llama_start_phases.py").read_text(encoding="utf-8"))
MH_SRC = (ROOT / "backend" / "manager_health.py").read_text(encoding="utf-8")
SRV = (ROOT / "server.py").read_text(encoding="utf-8")
UPDATE = (ROOT / "update.bat").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── 1. Grace recheck forwards the expert-cache surcharge ─────────────────────
class _Slot:
    slot_id = 7
    _ready_event = threading.Event()


class _FitRecorder:
    def __init__(self):
        self.calls = []

    def can_fit(self, model_name, num_ctx, **kw):
        self.calls.append(kw)
        return U.CanFitResult(True, 0.0, 0.0, 0.0, "test")


_rec = _FitRecorder()
_res = asyncio.run(ME.LlamaEvictMixin._pre_flight_grace_recheck(
    _rec, "m", 4096, _Slot(), extra_needed_mib=512))
check("1 grace recheck passes extra_needed_mib to can_fit",
      _rec.calls and _rec.calls[0].get("extra_needed_mib") == 512,
      str(_rec.calls[:1]))
check("1b grace recheck still honors the reduced margin",
      _rec.calls and "safety_margin_mib" in _rec.calls[0])

# Every pre-flight/refit site in the loader must carry the surcharge
# (initial gate + 4 grace/reclaim rechecks + post-reclaim refit).
_n_surcharge = ML_SRC.count("extra_needed_mib=_moe_cache_used")
check("2 all loader rechecks carry the surcharge (>= 6)",
      _n_surcharge >= 6, f"count={_n_surcharge}")

# ── 2. --fit must not defeat the CPU backend or the CPU fallback ─────────────
check("3 fit is only auto-applied without an explicit layer override",
      "gpu_layers_override is None" in ML_SRC
      and 'GPU_BACKEND != "cpu"' in ML_SRC.split("_fit_supported = bool(", 1)[1].split("await _help_supports", 1)[0])
check("4 the CPU fallback override wins over the registry",
      "if _gpu_layers is not None and gpu_layers_override is None:" in ML_SRC)
check("5 CPU backend skips VRAM eviction entirely",
      "if running_slots and GPU_BACKEND != \"cpu\":" in ML_SRC
      and 'if GPU_BACKEND == "cpu":\n            return' in ML_SRC)
check("6 Vram guard short-circuits before taking the lock",
      'if GPU_BACKEND == "cpu":\n            return\n        async with self._lock:'
      in ML_SRC)

# CPU path: the method returns before touching the lock. A fake lock that
# raises proves it did not enter the GPU branch.
class _BoomLock:
    async def __aenter__(self):
        raise AssertionError("lock entered on cpu backend")

    async def __aexit__(self, *exc):
        return False


class _FakeMgr:
    _lock = _BoomLock()
    _slots = []


_old_backend = ML.GPU_BACKEND
try:
    ML.GPU_BACKEND = "cpu"
    try:
        asyncio.run(ML.LlamaLoadMixin._enforce_vram_budget(_FakeMgr(), object()))
        _cpu_ok = True
    except AssertionError:
        _cpu_ok = False
finally:
    ML.GPU_BACKEND = _old_backend
check("7 cpu backend does not evict peers", _cpu_ok)

# ── 3. Retry-without-cache keys off the flag that was actually used ──────────
check("8 cache retry no longer re-derives MoE-ness (UI override safe)",
      "if _cache_live > 0 and _cache_wanted:" in ML_SRC
      and "_is_moe" not in ML_SRC)

# ── 4. force_kill_all passes the slot and wakes waiters ──────────────────────
check("9 force_kill_all passes the slot, not the Popen",
      "_kill_slot_async(s.process)" not in MH_SRC
      and "_kill_slot_async(s)" in MH_SRC)
check("10 force_kill_all wakes in-flight ensure_loaded waiters",
      "s._ready_event.set()" in MH_SRC
      and "s._ready_event.clear()" not in MH_SRC)

# ── 5. ROCm probes ───────────────────────────────────────────────────────────
class _R:
    def __init__(self, out):
        self.stdout = out
        self.stderr = ""


with mock.patch.object(U.subprocess, "run",
                       lambda *a, **k: _R("ROCm0: AMD Radeon RX 6600\n")):
    check("11 rocm device probe accepts ROCm0",
          U._probe_backend_devices("llama-server", "rocm") is True)
with mock.patch.object(U.subprocess, "run",
                       lambda *a, **k: _R("Vulkan0: AMD Radeon RX 6600\n")):
    check("12 vulkan probe still works",
          U._probe_backend_devices("llama-server", "vulkan") is True)
    check("13 a vulkan listing is not accepted as rocm",
          U._probe_backend_devices("llama-server", "rocm") is False)

_dll = Path(tempfile.mkdtemp())
_exe = _dll / ("llama-server.exe" if platform.system() == "Windows" else "llama-server")
_exe.write_text("", encoding="utf-8")
_suffix = ".dll" if platform.system() == "Windows" else ".so"
_rocm_lib = _dll / f"ggml-rocm{_suffix}"
_rocm_lib.write_text("", encoding="utf-8")
check("14 a libggml-rocm build passes the dll probe",
      U._probe_backend_dlls(str(_exe), "rocm") is True)
_rocm_lib.unlink()
check("15 a missing rocm library is reported",
      U._probe_backend_dlls(str(_exe), "rocm") is False)

# CUDA must not fall through to the ROCm/vulkan branch (the elif edit).
_cuda_lib = _dll / f"ggml-cuda{_suffix}"
_cuda_lib.write_text("", encoding="utf-8")
if platform.system() == "Windows":
    for _base in ("cudart64_12", "cublas64_12", "cublasLt64_12"):
        (_dll / f"{_base}.dll").write_text("", encoding="utf-8")
check("15b a libggml-cuda build still passes the dll probe",
      U._probe_backend_dlls(str(_exe), "cuda") is True)
check("15c a cuda build is not accepted as vulkan",
      U._probe_backend_dlls(str(_exe), "vulkan") is False)
_cuda_lib.unlink()
for _f in _dll.glob("*64_12.dll"):
    _f.unlink()

# ── 6. -ud stripping is suffix-only ──────────────────────────────────────────
_old_table = VT._MOE_TABLE
try:
    VT._MOE_TABLE = {"x-y": {"experts": 8}}
    check("16 a trailing -ud is stripped", VT.moe_cfg_for("x-y-ud") is not None)
    check("17 a mid-name -ud is not rewritten", VT.moe_cfg_for("x-ud-y") is None)
finally:
    VT._MOE_TABLE = _old_table

# ── 7. Updates never touch the user's GGUF directory ─────────────────────────
_apply = [ln for ln in UPDATE.splitlines() if ln.startswith('robocopy "%EXROOT%"')]
check("18 the update apply step excludes models/",
      bool(_apply) and "%EXROOT%\\models" in _apply[0],
      _apply[0][:110] if _apply else "no line")

# ── 8. cache retry keeps the shared post-start path; stale flag is reset ────
_retry = ML_SRC.split("moe_cache=False", 1)[-1].split("except Exception", 1)[0]
check("19 a successful cache retry does not return early",
      "_cache_retried_ok = True" in ML_SRC and "return slot.port" not in _retry)
check("20 the AMD/CPU fallbacks are gated on the retry flag",
      'if not _cache_retried_ok and "exit=-1"' in ML_SRC
      and "elif not _cache_retried_ok:" in ML_SRC)
# split 4.1: the claim preamble is _phase_claim in llama_start_phases.py now
_start = (ROOT / "backend" / "llama_start_phases.py").read_text(encoding="utf-8").split("async def _phase_claim", 1)[-1][:1000]
check("21 every load clears the per-slot cache flag first",
      "slot._moe_cache_used = False" in _start)
check("22 the startup sync accepts the rocm backend",
      'if _gb in ("vulkan", "cuda", "rocm", "cpu"):' in SRV)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
