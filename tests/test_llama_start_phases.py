# -*- coding: utf-8 -*-
"""Behavior test: backend/llama_start_phases._phase_flags (2026-11-07).

The 1,534-line split module had zero behavior coverage. This pins the flag
resolution: build gate, gpu-layer override precedence, CPU fallback, --fit
support and the thread defaults. The process seam is mocked.
"""
import asyncio
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import backend.llama_config as LC  # noqa: E402
import backend.llama_start_phases as P  # noqa: E402

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}{extra}")
    else:
        failed += 1
        print(f"  FAIL {name}{extra}")


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

print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
