# -*- coding: utf-8 -*-
"""35B/performance levers (MAINTENANCE_TODO section G, 2026-11-12).

Behavior + unit coverage for the five safe 35B levers:

  1. LARGE-MOE MLOCK SKIP  - the global llama_mlock setting no longer forces
     --load-mode mmap+mlock for MoE GGUFs > 12 GB (measured up to 4.7x slower
     decode than "none": 2.92 vs 13.77 t/s tg128 on the 35B-A3B). The spec
     crash guard and an explicit profile load_mode still win; small models
     keep the mlock intent.
  3. MOE UBATCH RAISE      - the per-model profile "ubatch" key / the
     duo_ubatch_moe setting raise --ubatch-size above the duo_ubatch baseline
     for MoE models only; pure resolver here, VRAM gate pinned by source.
  6. TEXT-ONLY MMPROJ SKIP - projector families attach --mmproj only when the
     load actually requests vision; ~0.8-1.3 GB VRAM saved on text/code runs,
     one-time upgrade reload on the first image request.
  7. TOOL-THINKING CAP     - duo_tool_thinking_budget_cap (0 = off) bounds the
     per-round thinking budget in agentic tool rounds (opt-in, no silent
     quality change).
  8. SLOT REUSE            - slot matching canonicalizes the model label
     (case/:latest/#N) so a role switch to the SAME model reuses the resident
     slot instead of reloading it; ensure_loaded is driven end-to-end on a
     FakeManager (real mixins + real ModelSlot, no sockets/processes).

Run: python tests/test_35b_perf_levers.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

MOE_35B = "qwen3.6:35b-a3b-ud"          # the shipped 35B profile (ubatch 512)
MOE_35B_PLAIN = "qwen3.6:35b-a3b"       # _MOE_EXPERT_COUNTS entry
DENSE_4B = "qwen3.5:4b"


# ══════════════════════════════════════════════════════════════════════════
# Lever 1: large-MoE mlock skip
# ══════════════════════════════════════════════════════════════════════════
import backend.manager_load as ML  # noqa: E402


def test_is_large_moe_gguf():
    with mock.patch("backend.llama_preflight.gguf_size_gb", return_value=20.0):
        check("1a 20GB MoE gguf is large",
              ML.is_large_moe_gguf(MOE_35B_PLAIN) is True)
        check("1b 20GB dense gguf is NOT large",
              ML.is_large_moe_gguf(DENSE_4B) is False)
    with mock.patch("backend.llama_preflight.gguf_size_gb", return_value=5.0):
        check("1c 5GB MoE gguf is NOT large",
              ML.is_large_moe_gguf(MOE_35B_PLAIN) is False)
    # FAIL-CLOSED TO THE OLD BEHAVIOR: unknown size keeps the mlock intent
    with mock.patch("backend.llama_preflight.gguf_size_gb", return_value=0.0):
        check("1d unknown size -> not large (mlock intent unchanged)",
              ML.is_large_moe_gguf(MOE_35B_PLAIN) is False)
    with mock.patch("backend.llama_preflight.gguf_size_gb",
                    side_effect=OSError("boom")):
        check("1e size probe failure -> not large",
              ML.is_large_moe_gguf(MOE_35B_PLAIN) is False)


def test_resolve_load_mode_large_moe():
    kw = dict(spec_planned=False, mlock_now=True, profile_lm="")
    # the mlock intent is dropped for a large MoE -> profile/RAM-guard decide
    with mock.patch.object(ML, "_ram_guard_blocks", return_value=False):
        check("1f large MoE + mlock intent + free RAM -> none (not mmap+mlock)",
              ML._resolve_load_mode(MOE_35B_PLAIN, free_ram_gb=30.0,
                                    large_moe=True, **kw) == "none")
    check("1g large MoE + mlock intent + tight RAM -> plain mmap",
          ML._resolve_load_mode(MOE_35B_PLAIN, free_ram_gb=5.0,
                                large_moe=True, **kw) == "mmap")
    # small models keep the historical behavior
    check("1h small model + mlock intent -> mmap+mlock (unchanged)",
          ML._resolve_load_mode(MOE_35B_PLAIN, free_ram_gb=30.0,
                                large_moe=False, **kw) == "mmap+mlock")
    # an explicit profile load_mode still wins for large MoE
    check("1i large MoE + profile mmap+mlock -> profile wins",
          ML._resolve_load_mode(MOE_35B_PLAIN, spec_planned=False,
                                mlock_now=True, profile_lm="mmap+mlock",
                                free_ram_gb=30.0, large_moe=True)
          == "mmap+mlock")
    # the SPEC+NONE crash guard outranks the skip
    check("1j large MoE + spec planned -> mmap+mlock (crash guard)",
          ML._resolve_load_mode(MOE_35B_PLAIN, spec_planned=True,
                                mlock_now=True, profile_lm="none",
                                free_ram_gb=30.0, large_moe=True)
          == "mmap+mlock")
    # setting off -> the old behavior even for large MoE
    check("1k default kwargs (no large_moe) -> mmap+mlock",
          ML._resolve_load_mode(MOE_35B_PLAIN, spec_planned=False,
                                mlock_now=True, profile_lm="",
                                free_ram_gb=30.0) == "mmap+mlock")


def test_mlock_skip_setting_gate():
    # the setting is read live; False restores the forced mlock path
    with mock.patch("settings.load_settings",
                    return_value={"llama_mlock_skip_large_moe": False}):
        check("1l setting false -> skip disabled",
              ML._mlock_skip_large_moe_enabled() is False)
    with mock.patch("settings.load_settings", return_value={}):
        check("1m setting absent -> default True",
              ML._mlock_skip_large_moe_enabled() is True)
    # the warning is a one-time class flag wired in _start_process
    src = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
    check("1n one-time warning flag + [LARGE-MOE-MLOCK] marker present",
          "_large_moe_mlock_warned" in src and "[LARGE-MOE-MLOCK]" in src)


# ══════════════════════════════════════════════════════════════════════════
# Lever 3: MoE ubatch raise
# ══════════════════════════════════════════════════════════════════════════
from backend.llama_config import (  # noqa: E402
    resolve_model_ubatch, ubatch_extra_mib,
)
from model_configs import models_registry as REG  # noqa: E402


def test_profile_ubatch_wiring():
    check("3a shipped 35B profile declares ubatch 512",
          REG.get_ubatch(MOE_35B) == 512,
          f" ({REG.get_ubatch(MOE_35B)})")
    check("3b profile without ubatch key -> None",
          REG.get_ubatch(DENSE_4B) is None)
    # invalid/out-of-band profile values are ignored, never an abort
    with mock.patch.object(REG, "get_profile", return_value={"ubatch": 99999}):
        check("3c out-of-band profile value ignored",
              REG.get_ubatch(MOE_35B) is None)
    with mock.patch.object(REG, "get_profile", return_value={"ubatch": "abc"}):
        check("3d non-numeric profile value ignored",
              REG.get_ubatch(MOE_35B) is None)


def test_resolve_model_ubatch_pure():
    # non-MoE models ALWAYS keep the baseline (small cards unaffected)
    v, src = resolve_model_ubatch(DENSE_4B, 256, 0)
    check("3d dense model keeps baseline", (v, src) == (256, "base"))
    # MoE + profile raise
    v, src = resolve_model_ubatch(MOE_35B, 256, 0)
    check("3e MoE profile raise applies", (v, src) == (512, "profile"),
          f" ({v},{src})")
    # explicit setting wins over the profile, never below the baseline
    v, src = resolve_model_ubatch(MOE_35B, 256, 1024)
    check("3f duo_ubatch_moe force wins", (v, src) == (1024, "setting"))
    v, src = resolve_model_ubatch(MOE_35B, 1024, 512)
    check("3g setting below baseline -> baseline", (v, src) == (1024, "base"))
    # veto keeps today's behavior everywhere
    v, src = resolve_model_ubatch(MOE_35B, 256, -1)
    check("3h negative setting vetoes the raise", (v, src) == (256, "veto"))
    # user-raised global baseline stays authoritative over the profile
    v, src = resolve_model_ubatch(MOE_35B, 1024, 0)
    check("3i raised duo_ubatch beats profile 512", (v, src) == (1024, "base"))
    # clamped + 32-aligned
    v, src = resolve_model_ubatch(MOE_35B, 256, 9999)
    check("3j forced value clamps to 4096 band", v == 4096, f" ({v})")
    # baseline alignment unchanged
    v, src = resolve_model_ubatch(DENSE_4B, 300, 0)
    check("3k dense baseline aligned to 32 (profile does not raise)",
          v == 288, f" ({v})")
    # surcharge math stays shared with the pre-flight gate
    check("3l ubatch surcharge delta (512 vs 256)",
          abs(ubatch_extra_mib(512) - ubatch_extra_mib(256) - 71.68) < 0.01)


def test_moe_ubatch_loader_wiring():
    src = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
    check("3m loader resolves the per-model ubatch",
          "resolve_model_ubatch(model, _ub_base" in src)
    check("3n raise is VRAM-gated with can_fit fallback",
          "vram-fallback" in src and "_ub_extra_delta_mib" in src)
    check("3o pre-flight carries the surcharge delta",
          "_fit_extra_mib" in src)
    phases = (ROOT / "backend" / "llama_start_phases.py").read_text(encoding="utf-8")
    check("3p pre-flight phase documents the extra",
          "MoE-UBATCH-RAISE compute-buffer delta" in phases)


# ══════════════════════════════════════════════════════════════════════════
# Lever 6: text-only mmproj skip
# ══════════════════════════════════════════════════════════════════════════
from backend.llama_manager_utils import (  # noqa: E402
    _needs_mmproj, needs_vision_reload, _nm,
)


def test_text_only_mmproj_skip():
    # projector families: attach ONLY on a vision request now
    with mock.patch("settings.load_settings", return_value={}):
        check("6a llava text-only load: no projector",
              _needs_mmproj("llava:7b", vision=False) is False)
        check("6b llava vision load: projector",
              _needs_mmproj("llava:7b", vision=True) is True)
        check("6c qwen3.6 35B text-only: no projector",
              _needs_mmproj(MOE_35B, vision=False) is False)
        check("6d qwen3.6 35B vision load: projector",
              _needs_mmproj(MOE_35B, vision=True) is True)
        check("6e model without a registered projector: none on vision load",
              _needs_mmproj("customvl:2b", vision=True) is False)
    with mock.patch("settings.load_settings",
                    return_value={"vision_mmproj_always": True}):
        check("6f vision_mmproj_always restores always-attach",
              _needs_mmproj("llava:7b", vision=False) is True)
    # the upgrade path is what makes the skip safe
    check("6g upgrade-only reload rule intact",
          needs_vision_reload(slot_vision=False, want_vision=True) is True
          and needs_vision_reload(slot_vision=True, want_vision=False) is False)


# ══════════════════════════════════════════════════════════════════════════
# Lever 7: tool thinking budget cap
# ══════════════════════════════════════════════════════════════════════════
def test_tool_thinking_cap():
    from settings_defaults import DEFAULT_SETTINGS
    check("7a default 0 = off (no silent quality change)",
          DEFAULT_SETTINGS.get("duo_tool_thinking_budget_cap") == 0)
    src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
    check("7b cap applied to the tool-round thinking budget",
          "duo_tool_thinking_budget_cap" in src
          and "[TOOL-THINK-CAP]" in src)
    # the cap semantics: min(budget, cap) with cap>0, else unchanged
    def _cap(budget, cap):
        return min(budget, cap) if cap > 0 else budget
    check("7c cap bounds an 8000 budget to 512", _cap(8000, 512) == 512)
    check("7d cap below the budget only", _cap(300, 512) == 300)
    check("7e cap off keeps the budget", _cap(8000, 0) == 8000)


# ══════════════════════════════════════════════════════════════════════════
# Lever 8: slot reuse across role labels
# ══════════════════════════════════════════════════════════════════════════
def test_nm_canonicalization():
    check("8a case-folded matching",
          _nm("Qwen3.6:35B-A3B-UD") == _nm("qwen3.6:35b-a3b-ud"))
    check("8b :latest stripped (any casing)",
          _nm("Model:latest") == _nm("model")
          and _nm("Model:LATEST") == _nm("model"))
    check("8c #N alias decorator stripped",
          _nm("fakemoe:35b-a3b#2") == _nm("fakemoe:35b-a3b"))
    check("8d None/empty safe", _nm(None) == "" and _nm("") == "")
    check("8e genuinely different models stay different",
          _nm("fakemoe:35b-a3b") != _nm("othermoe:35b-a3b"))


class FakeProc:
    """Minimal live-process stand-in (poll()->None = running)."""

    def __init__(self, pid=42000):
        self.pid = pid

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


import backend.manager_process as MP  # noqa: E402
from backend.llama_slots import ModelSlot  # noqa: E402


class FakeManager(ML.LlamaLoadMixin, MP.LlamaProcessMixin):
    """Real load/process mixins; the server spawn and VRAM guards stubbed."""

    def __init__(self):
        self._slots = [ModelSlot(i) for i in range(3)]
        self._lock = asyncio.Lock()
        self._last_orphan_scan_ts = time.time() + 999.0   # skip adoption scans
        self._load_seq = 0
        self.metrics = {}
        self.start_calls = []
        self.port_alive_answer = True

    def _metric_inc(self, name, amount=1):
        self.metrics[name] = self.metrics.get(name, 0) + amount

    async def _port_alive(self, port):
        return self.port_alive_answer

    async def _enforce_vram_budget(self, slot):
        return None

    async def _find_free_slot(self):
        for slot in self._slots:
            if not slot.is_running and not slot._loading:
                return slot
        return None

    async def _evict_lru_async(self, exclude_slot=None):
        return None

    async def _adopt_orphan_slots(self):
        return 0

    async def _start_process(self, slot, model, num_ctx, vision=False,
                             n_parallel=1, gpu_layers_override=None,
                             ctx_graceful=True, moe_cache=True):
        self.start_calls.append((model, int(num_ctx), bool(vision)))
        slot.model = model
        slot.process = FakeProc()
        slot._num_ctx = int(num_ctx)
        slot._n_parallel = int(n_parallel)
        slot._vision = bool(vision)
        slot._jinja = True
        slot._loading = False
        slot._ready_event.set()
        return None


def _run(coro):
    return asyncio.run(coro)


async def _scenario_same_model_reuse():
    mgr = FakeManager()
    await mgr.ensure_loaded("fakemoe:35b-a3b", num_ctx=16384, pin=True)
    await mgr.ensure_loaded("fakemoe:35b-a3b", num_ctx=16384, pin=True)
    return mgr


def test_ensure_loaded_reuse():
    mgr = _run(_scenario_same_model_reuse())
    check("8f same model + same ctx reuses the resident slot",
          len(mgr.start_calls) == 1, f" (starts={mgr.start_calls})")

    async def _scenario_label_variants():
        mgr = FakeManager()
        await mgr.ensure_loaded("fakemoe:35b-a3b", num_ctx=16384)
        # next role hands over the same model with a different label
        await mgr.ensure_loaded("FakeMoe:35B-A3B", num_ctx=16384)
        await mgr.ensure_loaded("fakemoe:35b-a3b:latest", num_ctx=16384)
        await mgr.ensure_loaded("fakemoe:35b-a3b#2", num_ctx=16384)
        return mgr

    mgr = _run(_scenario_label_variants())
    check("8g role label casing/:latest/#N differences reuse the slot",
          len(mgr.start_calls) == 1, f" (starts={mgr.start_calls})")

    async def _scenario_smaller_ctx():
        mgr = FakeManager()
        await mgr.ensure_loaded("fakemoe:35b-a3b", num_ctx=16384)
        await mgr.ensure_loaded("fakemoe:35b-a3b", num_ctx=8192)   # critic role
        return mgr

    mgr = _run(_scenario_smaller_ctx())
    check("8h next role with a SMALLER ctx reuses the slot",
          len(mgr.start_calls) == 1, f" (starts={mgr.start_calls})")

    async def _scenario_larger_ctx():
        mgr = FakeManager()
        await mgr.ensure_loaded("fakemoe:35b-a3b", num_ctx=8192)
        await mgr.ensure_loaded("fakemoe:35b-a3b", num_ctx=16384)  # coder needs more
        return mgr

    mgr = _run(_scenario_larger_ctx())
    check("8i larger ctx still upgrades (reload) - pinned behavior",
          len(mgr.start_calls) == 2
          and mgr.metrics.get("evictions_ctx_reload") == 1,
          f" (starts={mgr.start_calls}, metrics={mgr.metrics})")

    async def _scenario_other_model():
        mgr = FakeManager()
        await mgr.ensure_loaded("fakemoe:35b-a3b", num_ctx=16384)
        await mgr.ensure_loaded("othermoe:35b-a3b", num_ctx=16384)
        return mgr

    mgr = _run(_scenario_other_model())
    check("8j a different model never matches the resident slot",
          len(mgr.start_calls) == 2
          and len([s for s in mgr._slots if s.is_running]) == 2,
          f" (starts={mgr.start_calls})")


def test_serial_slots_canonicalization():
    src = (ROOT / "core" / "duo_coder_preloop.py").read_text(encoding="utf-8")
    check("8k SERIAL-SLOTS compares via the manager canonicalization (_nm)",
          "from backend.llama_manager_utils import _nm as _canon_model" in src
          and "_canon_model(_planner_model) != _canon_model(exec_mdl)" in src)
    # decision table: label-only differences must NOT evict the planner
    same = [(_nm("fakemoe:35b-a3b"), _nm("fakemoe:35b-a3b")),
            (_nm("FakeMoe:35B-A3B"), _nm("fakemoe:35b-a3b")),
            (_nm("fakemoe:35b-a3b:latest"), _nm("fakemoe:35b-a3b")),
            (_nm("fakemoe:35b-a3b#2"), _nm("fakemoe:35b-a3b"))]
    check("8l same-model label variants compare equal",
          all(a == b for a, b in same))
    check("8m different models still evict",
          _nm("fakemoe:35b-a3b") != _nm("lightmoe:4b"))


# ══════════════════════════════════════════════════════════════════════════
# Settings surface
# ══════════════════════════════════════════════════════════════════════════
def test_settings_surface():
    from settings_defaults import DEFAULT_SETTINGS
    for key, want in (("llama_mlock_skip_large_moe", True),
                      ("duo_ubatch_moe", 0),
                      ("duo_tool_thinking_budget_cap", 0),
                      ("vision_mmproj_always", False)):
        check(f"s default {key}={want}", DEFAULT_SETTINGS.get(key) is want,
              f" ({DEFAULT_SETTINGS.get(key)!r})")
    # every new key is either in the UI or explicitly exempted
    from deploy.settings_ui_scan import in_ui, exempt_keys
    exposed = in_ui(DEFAULT_SETTINGS)
    exempt = exempt_keys()
    for key in ("llama_mlock_skip_large_moe", "duo_ubatch_moe",
                "duo_tool_thinking_budget_cap", "vision_mmproj_always"):
        check(f"s {key} documented as UI-exposed or exempt",
              key in exposed or key in exempt)


if __name__ == "__main__":
    test_is_large_moe_gguf()
    test_resolve_load_mode_large_moe()
    test_mlock_skip_setting_gate()
    test_profile_ubatch_wiring()
    test_resolve_model_ubatch_pure()
    test_moe_ubatch_loader_wiring()
    test_text_only_mmproj_skip()
    test_tool_thinking_cap()
    test_nm_canonicalization()
    test_ensure_loaded_reuse()
    test_serial_slots_canonicalization()
    test_settings_surface()
    sys.exit(h.finish("35b_perf_levers"))
