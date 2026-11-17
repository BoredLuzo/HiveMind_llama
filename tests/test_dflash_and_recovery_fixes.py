# -*- coding: utf-8 -*-
"""Regression checks for DFlash --spec-draft-p-min, --fit/Pre-Flight drafter handling,
and single-slot GPU reclaim+retry before CPU fallback.

Run: python tests/test_dflash_and_recovery_fixes.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check

import backend.llama_config as lc  # noqa: E402
import backend.llama_start_phases as lsp  # noqa: E402
import backend.manager_load as ml  # noqa: E402
from backend.llama_manager_utils import CanFitResult  # noqa: E402
from model_configs import models_registry as mr  # noqa: E402


# ── 1) DFlash p_min config & registry clamping ─────────────────────────────────
check("DFLASH_DRAFT_P_MIN defaults to 0.4", abs(lc.DFLASH_DRAFT_P_MIN - 0.4) < 1e-6,
      f"(got {lc.DFLASH_DRAFT_P_MIN})")
check("Nemotron Bolt profile exposes dflash_draft_p_min=0.4",
      mr.get_dflash_draft_p_min("nemotron-3.5-bolt:e4b") == 0.4,
      f"(got {mr.get_dflash_draft_p_min('nemotron-3.5-bolt:e4b')})")
check("Unknown model returns None for dflash_draft_p_min",
      mr.get_dflash_draft_p_min("nonexistent-model-xyz:1b") is None)

with mock.patch.object(mr, "get_profile", side_effect=[{"dflash_draft_p_min": 1.8},
                                                        {"dflash_draft_p_min": -0.5}]):
    check("dflash_draft_p_min clamps upper bound to 0.95",
          mr.get_dflash_draft_p_min("any:1b") == 0.95)
    check("dflash_draft_p_min clamps lower bound to 0.0",
          mr.get_dflash_draft_p_min("any:1b") == 0.0)


# ── 2) _phase_flags & _phase_model_flags for external drafters ─────────────────
async def _test_phases():
    with tempfile.TemporaryDirectory(prefix="hm_df_") as td:
        mdir = Path(td)
        gguf = mdir / "Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf"
        draft = mdir / "Nemotron-3.5-Bolt-E4B-DFlash-BF16.gguf"
        gguf.write_bytes(b"GGUF")
        draft.write_bytes(b"GGUF")

        class _PhaseMgr:
            _binary_build_number = 12001
            _dspark_flag_supported = None
            _reasoning_override = {}

        mgr = _PhaseMgr()

        async def _fake_help_all(flag: str) -> bool:
            return flag in ("--fit", "draft-dflash", "--spec-draft-p-min", "--spec-draft-ngl")

        with (
            mock.patch.object(lsp, "MODELS_DIR", mdir),
            mock.patch.object(lc, "LLAMA_FIT", True),
            mock.patch.object(lsp, "llama_gpu_layers_auto", return_value=True),
            mock.patch.object(ml, "_help_supports", side_effect=_fake_help_all),
        ):
            flags_df = await lsp._phase_flags(mgr, "nemotron-3.5-bolt:e4b", None)
            check("DFlash model disables --fit even when LLAMA_FIT=True",
                  flags_df.fit_supported is False, f"({flags_df})")
            check("DFlash model pins gpu_layers even when auto=True",
                  flags_df.gpu_layers == lc.GPU_LAYERS, f"({flags_df})")

            flags_ctl = await lsp._phase_flags(mgr, "gemma-4:e4b", None)
            check("Non-drafter model still enables --fit when LLAMA_FIT=True",
                  flags_ctl.fit_supported is True, f"({flags_ctl})")
            check("Non-drafter model omits gpu_layers in auto mode",
                  flags_ctl.gpu_layers is None, f"({flags_ctl})")

            res_mf = await lsp._phase_model_flags(
                mgr, ["llama-server"], "nemotron-3.5-bolt:e4b", "nemotron-3.5-bolt", gguf, False
            )
            cmd = res_mf.cmd
            check("DFlash emits --spec-draft-p-min 0.4 when supported",
                  "--spec-draft-p-min" in cmd
                  and cmd[cmd.index("--spec-draft-p-min") + 1] == "0.4",
                  f"({cmd})")
            check("DFlash keeps --spec-draft-ngl 999",
                  "--spec-draft-ngl" in cmd
                  and cmd[cmd.index("--spec-draft-ngl") + 1] == "999")

        async def _fake_help_no_pmin(flag: str) -> bool:
            return flag in ("--fit", "draft-dflash", "--spec-draft-ngl")

        with (
            mock.patch.object(lsp, "MODELS_DIR", mdir),
            mock.patch.object(ml, "_help_supports", side_effect=_fake_help_no_pmin),
        ):
            res_old = await lsp._phase_model_flags(
                mgr, ["llama-server"], "nemotron-3.5-bolt:e4b", "nemotron-3.5-bolt", gguf, False
            )
            check("DFlash omits --spec-draft-p-min if binary lacks flag",
                  "--spec-draft-p-min" not in res_old.cmd
                  and "--spec-type" in res_old.cmd,
                  f"({res_old.cmd})")


asyncio.run(_test_phases())


# ── 3) External drafter Pre-Flight VRAM accounting & auto-drop ─────────────────
def _fake_can_fit_factory(free_mib: float, calibrated_base_mib: float):
    def _can_fit(_model: str, _ctx: int, safety_margin_mib: int = 768,
                 exclude_slot_id=None, extra_needed_mib: float = 0.0) -> CanFitResult:
        needed = round(calibrated_base_mib + float(extra_needed_mib or 0.0), 1)
        ok = (needed + safety_margin_mib) <= free_mib
        return CanFitResult(ok, needed, free_mib, safety_margin_mib, "live")
    return _can_fit


_slot_pf = SimpleNamespace(slot_id=0)
_base_cmd = [
    "llama-server", "--ctx-size", "16384", "--n-gpu-layers", "99",
    "--spec-type", "draft-dflash", "--model-draft", "Nemotron-3.5-Bolt-E4B-DFlash-BF16.gguf",
    "--spec-draft-n-max", "7", "--spec-draft-n-min", "0",
    "--spec-draft-p-min", "0.4", "--spec-draft-ngl", "999", "--jinja",
]

with mock.patch.object(lsp, "_external_draft_footprint_mib", return_value=1250.0):
    # Case A: Free = 4250 MiB, calibrated Nemotron+DFlash = 4505 MiB -> fits ONLY without drafter!
    mgr_tight = SimpleNamespace(can_fit=_fake_can_fit_factory(4250.0, 4505.0))
    cmd_a = list(_base_cmd)
    fit_in_a = mgr_tight.can_fit("nemotron-3.5-bolt:e4b", 16384)
    fit_out_a, extra_a, dropped_a = lsp._try_drop_external_drafter(
        mgr_tight, _slot_pf, cmd_a, "nemotron-3.5-bolt:e4b", 16384, fit_in_a, 0.0, 1250.0
    )
    check("Tight VRAM auto-drops DFlash so target alone fits cleanly",
          dropped_a is True and fit_out_a.ok is True and extra_a == -1250.0,
          f"(dropped={dropped_a}, fit={fit_out_a}, extra={extra_a})")
    check("Stripped command removes all external drafter flags and keeps target flags",
          "--model-draft" not in cmd_a and "--spec-type" not in cmd_a
          and "--spec-draft-p-min" not in cmd_a and "--spec-draft-ngl" not in cmd_a
          and "--ctx-size" in cmd_a and "--n-gpu-layers" in cmd_a and "--jinja" in cmd_a,
          f"({cmd_a})")

    # Case B: Free = 4900 MiB -> full ctx+DFlash fits with reduced margin (256), keep drafter!
    mgr_ok_red = SimpleNamespace(can_fit=_fake_can_fit_factory(4900.0, 4505.0))
    cmd_b = list(_base_cmd)
    fit_in_b = mgr_ok_red.can_fit("nemotron-3.5-bolt:e4b", 16384)
    fit_out_b, _extra_b, dropped_b = lsp._try_drop_external_drafter(
        mgr_ok_red, _slot_pf, cmd_b, "nemotron-3.5-bolt:e4b", 16384, fit_in_b, 0.0, 1250.0
    )
    check("Reduced-margin VRAM headroom keeps DFlash drafter active",
          dropped_b is False and "--model-draft" in cmd_b,
          f"(dropped={dropped_b}, fit={fit_out_b})")

_mtp_cmd = ["llama-server", "--spec-type", "draft-mtp", "--spec-draft-n-max", "2", "--jinja"]
check("Built-in MTP flags are untouched by _strip_external_drafter_flags",
      lsp._strip_external_drafter_flags(_mtp_cmd) == _mtp_cmd)


async def _test_uncalibrated_preflight_surcharge():
    calls: list[float] = []

    def _rec_can_fit(model: str, ctx: int, safety_margin_mib: int = 768,
                     exclude_slot_id=None, extra_needed_mib: float = 0.0) -> CanFitResult:
        calls.append(float(extra_needed_mib))
        return CanFitResult(True, 3000.0 + extra_needed_mib, 6000.0, safety_margin_mib, "live")

    mgr_u = SimpleNamespace(
        can_fit=_rec_can_fit,
        _slots=[],
    )
    slot_u = SimpleNamespace(slot_id=0, port=8199, model="custom:4b", _ready_event=asyncio.Event())

    with (
        mock.patch.object(mr, "get_vram_gb_override", return_value=None),
        mock.patch.object(lsp, "_external_draft_footprint_mib", return_value=1100.0),
        mock.patch.object(lsp.time, "strftime", side_effect=RuntimeError("stop-before-log")),
    ):
        try:
            await lsp._phase_preflight_and_spawn(
                mgr_u, slot_u, list(_base_cmd), "custom:4b", Path("m.gguf"),
                16384, False, 1, True, "custom", "mmap", None, 0.0, False,
            )
        except RuntimeError:
            pass
    check("Uncalibrated profile adds external drafter surcharge to initial pre-flight",
          bool(calls) and abs(calls[0] - 1100.0) < 1e-3,
          f"(calls={calls})")

    calls.clear()
    with (
        mock.patch.object(mr, "get_vram_gb_override", return_value=4.4),
        mock.patch.object(lsp, "_external_draft_footprint_mib", return_value=1250.0),
        mock.patch.object(lsp.time, "strftime", side_effect=RuntimeError("stop-before-log")),
    ):
        try:
            await lsp._phase_preflight_and_spawn(
                mgr_u, slot_u, list(_base_cmd), "nemotron-3.5-bolt:e4b", Path("m.gguf"),
                16384, False, 1, True, "nemotron-3.5-bolt", "mmap", None, 0.0, False,
            )
        except RuntimeError:
            pass
    check("Calibrated vram_gb_override avoids double-counting drafter surcharge",
          bool(calls) and calls[0] == 0.0,
          f"(calls={calls})")


asyncio.run(_test_uncalibrated_preflight_surcharge())


# ── 4) Single-slot exit=-1 runs reclaim wait + GPU retry before CPU fallback ───
class _FakeSlot:
    def __init__(self):
        self.slot_id = 0
        self.port = 8199
        self.model = None
        self.is_running = False
        self.pinned = False
        self._loading = False
        self._lifecycle_gen = 0
        self._idle_timeout = None
        self._ready_event = asyncio.Event()
        self.in_flight = 0
        self._entry_load_seq = 0
        self._moe_cache_used = False

    def set_pinned(self, v: bool) -> None:
        self.pinned = bool(v)

    def touch(self) -> None:
        pass


class _FakeLoadMgr(ml.LlamaLoadMixin):
    def __init__(self, script):
        self._slots = [_FakeSlot()]
        self._lock = asyncio.Lock()
        self._last_orphan_scan_ts = 1e12
        self._script = list(script)
        self.start_calls = []

    def _metric_inc(self, name: str, delta: int = 1) -> None:
        pass

    async def _find_loaded(self, model: str, lock_held: bool = False):
        return None

    async def _find_free_slot(self):
        return self._slots[0]

    async def _enforce_vram_budget(self, slot) -> None:
        pass

    async def _start_process(self, slot, model, num_ctx, vision=False,
                             n_parallel=1, gpu_layers_override=None,
                             ctx_graceful=True):
        self.start_calls.append(gpu_layers_override)
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        slot._loading = False
        slot.is_running = True
        slot._ready_event.set()


async def _test_single_slot_retry():
    reclaim_calls: list[int] = []

    async def _fake_reclaim(need_mib: int, timeout_sec: int = 15, stall_abort_s: float = 5.0):
        reclaim_calls.append(need_mib)
        return True

    async def _fake_kill(s) -> None:
        s.model = None
        s._loading = False
        s.is_running = False

    async def _fast_sleep(_sec: float) -> None:
        return None

    with (
        mock.patch.object(ml, "wait_for_vram_reclaim", side_effect=_fake_reclaim),
        mock.patch.object(ml, "_kill_slot_async", side_effect=_fake_kill),
        mock.patch.object(ml, "_vram_of", return_value=2.5),
        mock.patch.object(ml, "GPU_LAYERS", 99),
        mock.patch.object(ml.asyncio, "sleep", side_effect=_fast_sleep),
    ):
        # 4a) Transient exit=-1 on <= 3.0 GB single slot: GPU retry succeeds BEFORE CPU fallback
        reclaim_calls.clear()
        mgr1 = _FakeLoadMgr([RuntimeError("died exit=-1"), "ok"])
        port1 = await mgr1.ensure_loaded("qwen3.5:4b-mtp")
        check("Single-slot small model retries on GPU after reclaim before CPU fallback",
              port1 == 8199 and mgr1.start_calls == [None, None] and len(reclaim_calls) == 1,
              f"(calls={mgr1.start_calls}, reclaim={reclaim_calls})")

        # 4b) Persistent exit=-1 on <= 3.0 GB single slot: GPU retry, THEN CPU fallback (0)
        reclaim_calls.clear()
        mgr2 = _FakeLoadMgr([RuntimeError("died exit=-1"), RuntimeError("died exit=-1"), "ok"])
        port2 = await mgr2.ensure_loaded("qwen3.5:4b-mtp")
        check("Single-slot small model still reaches CPU fallback after failed GPU retry",
              port2 == 8199 and mgr2.start_calls == [None, None, 0] and len(reclaim_calls) == 1,
              f"(calls={mgr2.start_calls}, reclaim={reclaim_calls})")

    with (
        mock.patch.object(ml, "wait_for_vram_reclaim", side_effect=_fake_reclaim),
        mock.patch.object(ml, "_kill_slot_async", side_effect=_fake_kill),
        mock.patch.object(ml, "_vram_of", return_value=4.8),
        mock.patch.object(ml, "GPU_LAYERS", 99),
        mock.patch.object(ml.asyncio, "sleep", side_effect=_fast_sleep),
    ):
        # 4c) Large model (> 3.0 GB): retries ONCE on GPU, never falls back to CPU (0)
        reclaim_calls.clear()
        mgr3 = _FakeLoadMgr([RuntimeError("died exit=-1"), RuntimeError("died exit=-1")])
        err_msg = ""
        try:
            await mgr3.ensure_loaded("hermes:7b")
        except RuntimeError as exc:
            err_msg = str(exc)
        check("Single-slot >3.0 GB model retries once on GPU and refuses CPU fallback",
              mgr3.start_calls == [None, None] and "CPU fallback skipped" in err_msg,
              f"(calls={mgr3.start_calls}, err={err_msg!r})")


asyncio.run(_test_single_slot_retry())


# ── 5) Quick wins: --fit strip on MoE, mmproj pre-flight, ctx-ladder guard,
#       drafter-before-autostep + load-mode refresh, heavy test classification ──
async def _test_moe_and_vision_preflight_fixes():
    import backend.llama_vram_table as lvt
    from backend.llama_manager_utils import VRAMPreFlightError
    from backend.manager_evict import LlamaEvictMixin
    import tests.run_regressions as rr

    # 5a) --fit stripped and -ngl pinned when --n-cpu-moe is appended
    class _MoePhaseMgr:
        _moe_flag_supported = True
        _load_mode_supported = True
        _kv_flag_supported = True

    moe_slot = SimpleNamespace(_moe_cache_used=False)
    in_cmd = [
        "llama-server", "--ctx-size", "16384",
        "--threads", "16", "--threads-batch", "8",
        "--fit", "on", "--fit-target", "1024",
    ]
    with (
        mock.patch.object(lsp, "_strip_alias", return_value="groot:19b-a3b"),
        mock.patch.object(lsp, "_MOE_EXPERT_COUNTS", {"groot:19b-a3b": 32}),
        mock.patch.object(lc, "MOE_CPU_EXPERTS", {}),
        mock.patch.object(lc, "_try_registry_moe", return_value=0),
        mock.patch.object(lsp, "GPU_BACKEND", "vulkan"),
        mock.patch.object(lsp, "moe_kv_cache_types", return_value="q4_0"),
        mock.patch("backend.llama_vram_table.moe_cfg_for", return_value={}),
    ):
        mph = await lsp._phase_moe(
            _MoePhaseMgr(), moe_slot, list(in_cmd), "groot:19b-a3b",
            16384, False, 16, 8, gpu_layers=99,
        )
    check("MoE --n-cpu-moe strips --fit and --fit-target and pins -ngl 99",
          "--fit" not in mph.cmd and "--fit-target" not in mph.cmd
          and "--n-cpu-moe" in mph.cmd
          and "--n-gpu-layers" in mph.cmd
          and mph.cmd[mph.cmd.index("--n-gpu-layers") + 1] == "99",
          f"({mph.cmd})")

    # 5b) mmproj_surcharge_mib + pre-flight + formula-fallback peer accounting
    with tempfile.TemporaryDirectory(prefix="hm_mm_") as td:
        mm_path = Path(td) / "mmproj-test-F16.gguf"
        mm_path.write_bytes(b"M" * (2 * 1024 * 1024))
        with mock.patch("os.path.getsize", return_value=858 * 1024 * 1024):
            surcharge = lvt.mmproj_surcharge_mib(mm_path)
        check("mmproj_surcharge_mib adds 15% CLIP headroom over F16 projector size",
              abs(surcharge - round(858 * 1.15, 1)) < 0.2, f"(got {surcharge})")

        pf_extras: list[float] = []

        def _mm_can_fit(_m: str, _c: int, safety_margin_mib: int = 768,
                        exclude_slot_id=None, extra_needed_mib: float = 0.0) -> CanFitResult:
            pf_extras.append(float(extra_needed_mib))
            return CanFitResult(True, 3000.0 + extra_needed_mib, 6000.0, safety_margin_mib, "live")

        mm_mgr = SimpleNamespace(can_fit=_mm_can_fit, _slots=[])
        mm_slot = SimpleNamespace(slot_id=0, port=8199, model="m:4b", _ready_event=asyncio.Event())
        with (
            mock.patch.object(lsp, "mmproj_surcharge_mib", return_value=986.7),
            mock.patch.object(lsp.time, "strftime", side_effect=RuntimeError("stop-before-log")),
        ):
            try:
                await lsp._phase_preflight_and_spawn(
                    mm_mgr, mm_slot, ["llama", "--ctx-size", "4096"], "m:4b",
                    Path("m.gguf"), 4096, True, 1, True, "m", "none", mm_path, 0.0, False,
                )
            except RuntimeError:
                pass
        check("_phase_preflight_and_spawn includes mmproj surcharge in can_fit",
              bool(pf_extras) and abs(pf_extras[0] - 986.7) < 0.2,
              f"(extras={pf_extras})")

    class _FormulaMgr(LlamaEvictMixin):
        def __init__(self, peers):
            self._slots = peers

    peer_vis = SimpleNamespace(slot_id=1, model="qwen3.5:4b", _num_ctx=4096, _vision=True)
    with (
        mock.patch("backend.manager_evict.get_live_gpu_free_mib", return_value=None),
        mock.patch("backend.manager_evict.VRAM_BUDGET_GB", 7.5),
        mock.patch("backend.manager_evict.vram_of_moe", return_value=3.0),
        mock.patch("backend.manager_evict.slot_mmproj_vram_mib", return_value=1024.0),
    ):
        fb_res = _FormulaMgr([peer_vis]).can_fit("other:2b", 4096)
    # Free = (7.5 - (3.0 + 1.0)) * 1024 = 3584 MiB (without peer mmproj it was 4608 MiB).
    check("formula-fallback counts active vision peer mmproj against committed VRAM",
          fb_res.source == "formula-fallback" and abs(fb_res.free_mib - 3584.0) < 1.0,
          f"(fb={fb_res})")

    # 5c) Ctx-ladder accounts for extras AND validates downgraded _fit.ok before continuing
    def _ladder_can_fit(_m: str, ctx: int, safety_margin_mib: int = 768,
                        exclude_slot_id=None, extra_needed_mib: float = 0.0) -> CanFitResult:
        # Base table stays 3000 MiB (fits 4000 free at reduced 256 margin WITHOUT extras),
        # but real load carries 900 MiB extra -> 3900 + 256 = 4156 > 4000 (MUST block!).
        needed = round(3000.0 + float(extra_needed_mib), 1)
        ok = (needed + safety_margin_mib) <= 4000.0
        return CanFitResult(ok, needed, 4000.0, safety_margin_mib, "live")

    lad_mgr = SimpleNamespace(
        can_fit=_ladder_can_fit,
        _slots=[],
        _metric_inc=lambda *_a: None,
        _adopt_orphan_slots=mock.AsyncMock(return_value=0),
        _pre_flight_grace_recheck=mock.AsyncMock(
            side_effect=lambda m, c, s, **kw: _ladder_can_fit(m, c, **kw)
        ),
    )
    lad_slot = SimpleNamespace(slot_id=0, port=8199, model="hermes:35b", _num_ctx=32768)
    blocked = False
    with (
        mock.patch.object(lc, "VRAM_PREFLIGHT_GATE", True),
        mock.patch.object(lc, "LLAMA_UBATCH", 256),
        mock.patch.object(lsp, "get_live_gpu_free_mib", return_value=4000.0),
        mock.patch.object(lsp, "vram_of_moe", return_value=3000.0 / 1024.0),
        mock.patch.object(lsp, "wait_for_vram_reclaim", new=mock.AsyncMock(return_value=False)),
        mock.patch("backend.manager_load._pre_flight_evict_candidates", return_value=[]),
    ):
        try:
            await lsp._phase_preflight_and_spawn(
                lad_mgr, lad_slot, ["llama", "--ctx-size", "32768"], "hermes:35b",
                Path("m.gguf"), 32768, False, 1, True, "hermes", "none", None, 900.0, False,
            )
        except VRAMPreFlightError:
            blocked = True
    check("Ctx-ladder refuses silent spill when base fits but extras break VRAM budget",
          blocked is True)

    # 5d) External drafter drops BEFORE MoE auto-step AND refreshes --load-mode
    def _dspark_moe_can_fit(_m: str, _c: int, safety_margin_mib: int = 768,
                            exclude_slot_id=None, extra_needed_mib: float = 0.0) -> CanFitResult:
        # Target alone @35 fits 5200 free (4750+256=5006 <= 5200); with +870 drafter fails.
        needed = round(4750.0 + float(extra_needed_mib), 1)
        ok = (needed + safety_margin_mib) <= 5200.0
        return CanFitResult(ok, needed, 5200.0, safety_margin_mib, "live")

    dm_mgr = SimpleNamespace(
        can_fit=_dspark_moe_can_fit,
        _slots=[],
        _metric_inc=lambda *_a: None,
        _adopt_orphan_slots=mock.AsyncMock(return_value=0),
        _pre_flight_grace_recheck=mock.AsyncMock(
            side_effect=lambda m, c, s, **kw: _dspark_moe_can_fit(m, c, **kw)
        ),
    )
    dm_slot = SimpleNamespace(slot_id=0, port=8199, model="minicpm-v-4.7:35b-a3b-mix", _num_ctx=16384)
    dm_cmd = [
        "llama-server", "--ctx-size", "16384", "--n-cpu-moe", "35",
        "--load-mode", "mmap+mlock",
        "--spec-type", "draft-dspark", "--model-draft", "dspark.gguf",
        "--spec-draft-n-max", "2", "--spec-draft-n-min", "0", "--spec-draft-ngl", "999",
    ]
    lm_cfg = {
        "load_mode_supported": True,
        "profile_lm": "none",
        "mlock_now": False,
        "large_moe": True,
        "mtp_planned": False,
        "lazy_mode": "",
    }
    with (
        mock.patch.object(lc, "VRAM_PREFLIGHT_GATE", True),
        mock.patch.object(lsp, "_external_draft_footprint_mib", return_value=870.0),
        mock.patch.object(mr, "get_vram_gb_override", return_value=None),
        mock.patch.object(lsp, "_auto_step_moe_cpu_offload") as mock_autostep,
        mock.patch.object(ml, "_resolve_load_mode", return_value="none"),
        mock.patch.object(ml, "_free_ram_gb", return_value=24.0),
        mock.patch.object(ml, "_help_supports", new=mock.AsyncMock(return_value=True)),
        mock.patch.object(lsp.time, "strftime", side_effect=RuntimeError("stop-before-log")),
    ):
        try:
            await lsp._phase_preflight_and_spawn(
                dm_mgr, dm_slot, dm_cmd, "minicpm-v-4.7:35b-a3b-mix",
                Path("m.gguf"), 16384, False, 1, True, "minicpm-v-4.7", "mmap+mlock",
                None, 0.0, False, load_mode_cfg=lm_cfg,
            )
        except RuntimeError:
            pass
    check("MoE drops external drafter before stepping CPU experts and refreshes --load-mode",
          "--model-draft" not in dm_cmd
          and dm_cmd[dm_cmd.index("--n-cpu-moe") + 1] == "35"
          and dm_cmd[dm_cmd.index("--load-mode") + 1] == "none"
          and not mock_autostep.called,
          f"(cmd={dm_cmd}, autostep_called={mock_autostep.called})")

    # 5e) Regression runner subprocess contention split
    check("Heavy subprocess suite helper separates git/temp-repo suites from fast suites",
          rr._is_heavy_spawn_suite("git_gate")
          and rr._is_heavy_spawn_suite("apply_patch")
          and not rr._is_heavy_spawn_suite("dflash_and_recovery_fixes"))


asyncio.run(_test_moe_and_vision_preflight_fixes())

sys.exit(h.finish("dflash_and_recovery_fixes"))
