# -*- coding: utf-8 -*-
"""Wave 2 Workstream B3 regression tests: speculative decode, -ud lookup,
_phase_claim reclaim skip, dynamic TOTAL_VRAM_MIB, and tight-GPU overflow gate.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests._harness import Harness  # noqa: E402
import backend.llama_config as LC  # noqa: E402
import backend.llama_start_phases as lsp  # noqa: E402
import backend.llama_vram_table as lvt  # noqa: E402
import backend.manager_load as ML  # noqa: E402
from backend.llama_server_manager import CanFitResult  # noqa: E402


h = Harness()
check = h.check


def _make_slot(
    slot_id: int,
    *,
    is_running: bool = False,
    model: str | None = None,
    pinned: bool = False,
    num_ctx: int = 8192,
):
    """Build a minimal fake slot compatible with llama_start_phases."""
    return types.SimpleNamespace(
        slot_id=slot_id,
        port=9000 + slot_id,
        model=model,
        is_running=is_running,
        pinned=pinned,
        pinned_at=0.0,
        loaded_at=1.0,
        last_used=1.0,
        in_flight=0,
        in_flight_at=0.0,
        _loading=False,
        _num_ctx=num_ctx,
        _vision=False,
        _moe_cache_used=False,
        _orphan_port=None,
    )


def _make_mgr(*, build: int = 12001):
    """Build a minimal fake manager instance compatible with llama_start_phases."""

    class _FakeMgr:
        _binary_build_number = build
        _dspark_flag_supported = True
        _device_flag_supported = True
        _kv_flag_supported = True
        _moe_flag_supported = True
        _load_mode_supported = True
        _backend_dlls_ok = True
        _reasoning_override = None

        def __init__(self):
            self._slots = []
            self._last_orphan_scan_ts = 0.0
            self.can_fit_calls = []
            self.killed_slots = []

        def can_fit(self, model, num_ctx, **kwargs):
            self.can_fit_calls.append((model, num_ctx, dict(kwargs)))
            extra = float(kwargs.get("extra_needed_mib", 0.0) or 0.0)
            margin = int(kwargs.get("safety_margin_mib", 768))
            needed = 3000.0 + extra
            free = 5000.0
            return CanFitResult(
                ok=(needed + margin <= free),
                needed_mib=needed,
                free_mib=free,
                margin_mib=margin,
                source="fake",
            )

        async def _kill_slot(self, slot):
            self.killed_slots.append(slot.slot_id)
            slot.is_running = False

        async def _kill_port(self, _port):
            raise RuntimeError("stop-before-spawn")

        async def _port_alive(self, _port):
            return False

        async def _adopt_orphan_slots(self):
            return 0

        async def _pre_flight_grace_recheck(self, model, num_ctx, slot, **_kwargs):
            return self.can_fit(model, num_ctx, exclude_slot_id=slot.slot_id)

    return _FakeMgr()


def _run_gemma4_and_spec_tests() -> None:
    print("[1] Speculative decoding unification & Gemma-4 MTP fix")

    async def _help_all_true(flag: str) -> bool:
        return flag in {
            "--fit",
            "--spec-draft-p-min",
            "--spec-draft-ngl",
            "--spec-draft-n-max",
            "draft-dflash",
        }

    async def _help_no_nmax_flag(flag: str) -> bool:
        return flag in {"--fit", "--spec-draft-p-min", "--spec-draft-ngl", "draft-dflash"}

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        gemma_draft = tmp / "gemma-4-draft-mtp.gguf"
        gemma_draft.write_bytes(b"\x00" * (120 * 1024))  # small file -> floor 600 MiB
        dflash_draft = tmp / "nemotron-dflash.gguf"
        dflash_draft.write_bytes(b"\x00" * (1024 * 1024))

        gemma_profile = {
            "mtp": True,
            "mtp_draft_filename": gemma_draft.name,
            "mtp_draft_n_max": 2,
            "mtp_draft_p_min": 0.4,
            "gpu_layers": None,
        }

        mgr = _make_mgr(build=12001)
        with (
            mock.patch.object(lsp, "MODELS_DIR", tmp),
            mock.patch.object(LC, "LLAMA_FIT", True),
            mock.patch.object(LC, "GPU_BACKEND", "vulkan"),
            mock.patch.object(lsp, "GPU_BACKEND", "vulkan"),
            mock.patch.object(lsp, "GPU_LAYERS", 99),
            mock.patch.object(ML, "_help_supports", side_effect=_help_no_nmax_flag),
            mock.patch.object(LC, "_try_registry_mtp", return_value=True),
            mock.patch.object(LC, "_try_registry_mtp_draft", return_value=gemma_draft.name),
            mock.patch.object(LC, "_try_registry_dspark_draft", return_value=None),
            mock.patch.object(LC, "_try_registry_dflash_draft", return_value=None),
            mock.patch.object(LC, "_try_registry_gpu_layers", return_value=None),
            mock.patch("model_configs.models_registry.get_profile", return_value=gemma_profile),
            mock.patch.dict(os.environ, {"HIVEMIND_MTP_SPEC": "1"}, clear=False),
        ):
            flags = asyncio.run(lsp._phase_flags(mgr, "gemma-4:e4b-it-qat", None))
            check(
                "Gemma-4 mtp_draft_filename disables --fit (fit_supported=False)",
                flags.fit_supported is False,
            )
            check(
                "Gemma-4 mtp_draft_filename pins auto gpu_layers to GPU_LAYERS",
                flags.gpu_layers == 99,
            )

            cmd = ["llama-server", "--ctx-size", "8192"]
            asyncio.run(lsp._phase_model_flags(mgr, cmd, "gemma-4:e4b-it-qat", "gemma-4", None, False))
            check(
                "Gemma-4 emits single --spec-type draft-mtp",
                cmd.count("--spec-type") == 1 and cmd[cmd.index("--spec-type") + 1] == "draft-mtp",
            )
            check(
                "Gemma-4 emits --model-draft pointing to sidecar",
                "--model-draft" in cmd and cmd[cmd.index("--model-draft") + 1] == str(gemma_draft),
            )
            check(
                "Gemma-4 external MTP emits --spec-draft-n-max 2 even when binary help omits n-max flag",
                "--spec-draft-n-max" in cmd and cmd[cmd.index("--spec-draft-n-max") + 1] == "2",
            )
            check(
                "Gemma-4 emits --spec-draft-ngl 999 and --spec-draft-p-min 0.4",
                "--spec-draft-ngl" in cmd
                and cmd[cmd.index("--spec-draft-ngl") + 1] == "999"
                and "--spec-draft-p-min" in cmd
                and cmd[cmd.index("--spec-draft-p-min") + 1] == "0.4",
            )

            # _external_draft_footprint_mib: fallback vs calibrated profile
            with mock.patch("model_configs.models_registry.get_draft_overhead_mib", return_value=None):
                fp_fallback = lsp._external_draft_footprint_mib(cmd, "gemma-4:e4b-it-qat")
                check(
                    "Gemma-4 external draft footprint floor >= 600 MiB",
                    fp_fallback >= 600.0,
                    f" ({fp_fallback:.1f})",
                )
            with mock.patch("model_configs.models_registry.get_draft_overhead_mib", return_value=950.0):
                fp_cal = lsp._external_draft_footprint_mib(cmd, "gemma-4:e4b-it-qat")
                check(
                    "Gemma-4 external draft footprint uses calibrated draft_overhead_mib",
                    fp_cal == 950.0,
                    f" ({fp_cal})",
                )

        # Missing sidecar keeps --fit on and omits --model-draft / --spec-type
        with (
            mock.patch.object(lsp, "MODELS_DIR", tmp),
            mock.patch.object(LC, "LLAMA_FIT", True),
            mock.patch.object(LC, "GPU_BACKEND", "vulkan"),
            mock.patch.object(lsp, "GPU_BACKEND", "vulkan"),
            mock.patch.object(ML, "_help_supports", side_effect=_help_all_true),
            mock.patch.object(LC, "_try_registry_mtp", return_value=True),
            mock.patch.object(LC, "_try_registry_mtp_draft", return_value="missing-sidecar.gguf"),
            mock.patch.object(LC, "_try_registry_dspark_draft", return_value=None),
            mock.patch.object(LC, "_try_registry_dflash_draft", return_value=None),
            mock.patch.dict(os.environ, {"HIVEMIND_MTP_SPEC": "1"}, clear=False),
        ):
            flags_miss = asyncio.run(lsp._phase_flags(mgr, "gemma-4:e4b-it-qat", None))
            check(
                "Missing MTP sidecar keeps --fit enabled",
                flags_miss.fit_supported is True,
            )
            cmd_miss = ["llama-server"]
            asyncio.run(lsp._phase_model_flags(mgr, cmd_miss, "gemma-4:e4b-it-qat", "gemma-4", None, False))
            check(
                "Missing MTP sidecar emits neither --spec-type nor --model-draft",
                "--spec-type" not in cmd_miss and "--model-draft" not in cmd_miss,
            )

        # HIVEMIND_MTP_SPEC=0 disables Gemma-4 external MTP in BOTH flags and model_flags
        with (
            mock.patch.object(lsp, "MODELS_DIR", tmp),
            mock.patch.object(LC, "LLAMA_FIT", True),
            mock.patch.object(LC, "GPU_BACKEND", "vulkan"),
            mock.patch.object(lsp, "GPU_BACKEND", "vulkan"),
            mock.patch.object(ML, "_help_supports", side_effect=_help_all_true),
            mock.patch.object(LC, "_try_registry_mtp", return_value=True),
            mock.patch.object(LC, "_try_registry_mtp_draft", return_value=gemma_draft.name),
            mock.patch.object(LC, "_try_registry_dspark_draft", return_value=None),
            mock.patch.object(LC, "_try_registry_dflash_draft", return_value=None),
            mock.patch.dict(os.environ, {"HIVEMIND_MTP_SPEC": "0"}, clear=False),
        ):
            flags_off = asyncio.run(lsp._phase_flags(mgr, "gemma-4:e4b-it-qat", None))
            cmd_off = ["llama-server"]
            asyncio.run(lsp._phase_model_flags(mgr, cmd_off, "gemma-4:e4b-it-qat", "gemma-4", None, False))
            check(
                "HIVEMIND_MTP_SPEC=0 keeps --fit enabled and omits spec flags",
                flags_off.fit_supported is True and "--spec-type" not in cmd_off,
            )

        # Preflight _draft_save_mib double-count guard (vram_gb_override set vs None)
        def _run_preflight_probe(vram_override_val):
            import builtins as _builtins
            probe_mgr = _make_mgr(build=12001)
            gguf = tmp / "target.gguf"
            if not gguf.exists():
                gguf.write_bytes(b"\x00" * 1024)
            slot = _make_slot(0)
            cmd_pf = [
                "llama-server",
                "--ctx-size", "8192",
                "--model-draft", str(gemma_draft),
            ]
            real_open = _builtins.open

            def _sandbox_log_open(path, *args, **kwargs):
                p = Path(path)
                if "llama_server_" in p.name and p.suffix == ".log":
                    return real_open(tmp / p.name, *args, **kwargs)
                return real_open(path, *args, **kwargs)

            with (
                mock.patch("builtins.open", side_effect=_sandbox_log_open),
                mock.patch.object(ML, "_archive_old_slot_logs", return_value=None),
                mock.patch("model_configs.models_registry.get_vram_gb_override", return_value=vram_override_val),
                mock.patch("model_configs.models_registry.get_draft_overhead_mib", return_value=800.0),
                mock.patch.object(lsp, "vram_of_moe", return_value=3.0),
                mock.patch.object(lsp, "ubatch_extra_mib", return_value=0.0),
                mock.patch.object(LC, "LLAMA_UBATCH", 256),
                mock.patch.object(LC, "VRAM_PREFLIGHT_GATE", True),
                mock.patch("backend.gguf_meta.read_gguf_metadata", return_value={}),
            ):
                raised = None
                try:
                    asyncio.run(
                        lsp._phase_preflight_and_spawn(
                            probe_mgr, slot, cmd_pf, "gemma-4:e4b-it-qat", gguf,
                            8192, False, 1, False, "gemma-4", "mmap", None, 0.0, False,
                        )
                    )
                except RuntimeError as exc:
                    raised = str(exc)
                return probe_mgr.can_fit_calls, raised

        calls_no_ovr, raised_no_ovr = _run_preflight_probe(None)
        check(
            "Preflight accounts _draft_save_mib (800 MiB) when vram_gb_override is None",
            raised_no_ovr == "stop-before-spawn"
            and len(calls_no_ovr) >= 1
            and float(calls_no_ovr[0][2].get("extra_needed_mib", 0.0)) == 800.0,
            f" calls={calls_no_ovr}",
        )
        calls_with_ovr, raised_with_ovr = _run_preflight_probe(6.2)
        check(
            "Preflight skips adding _draft_save_mib when vram_gb_override is set (no double-count)",
            raised_with_ovr == "stop-before-spawn"
            and len(calls_with_ovr) >= 1
            and float(calls_with_ovr[0][2].get("extra_needed_mib", -1.0)) == 0.0,
            f" calls={calls_with_ovr}",
        )

        # Negative clamp inside _try_drop_external_drafter
        clamp_mgr = _make_mgr(build=12001)
        clamp_slot = _make_slot(1)

        def _strict_can_fit(model, num_ctx, **kwargs):
            clamp_mgr.can_fit_calls.append((model, num_ctx, dict(kwargs)))
            extra = float(kwargs.get("extra_needed_mib", 0.0) or 0.0)
            margin = int(kwargs.get("safety_margin_mib", 768))
            needed = 3200.0 + extra
            return CanFitResult(
                ok=(needed + margin <= 4000.0),
                needed_mib=needed,
                free_mib=4000.0,
                margin_mib=margin,
                source="fake",
            )

        clamp_mgr.can_fit = _strict_can_fit
        initial_fit = CanFitResult(ok=False, needed_mib=4200.0, free_mib=4000.0, margin_mib=768, source="fake")
        cmd_clamp = [
            "llama-server",
            "--spec-type", "draft-mtp",
            "--model-draft", str(gemma_draft),
            "--spec-draft-n-max", "2",
            "--jinja",
        ]
        with mock.patch.object(LC, "VRAM_PREFLIGHT_GATE", True):
            fit_out, extra_out, dropped = lsp._try_drop_external_drafter(
                clamp_mgr, clamp_slot, cmd_clamp, "gemma-4:e4b-it-qat", 8192, initial_fit, 0.0, 1250.0,
            )
            all_extras = [float(c[2].get("extra_needed_mib", 0.0)) for c in clamp_mgr.can_fit_calls]
            check(
                "_try_drop_external_drafter clamps extra_needed_mib >= 0.0 (never negative)",
                all(e >= 0.0 for e in all_extras) and extra_out >= 0.0 and not dropped,
                f" all_extras={all_extras} extra_out={extra_out}",
            )

        # When moe_cache_used >= draft_save_mib (e.g. 1500 - 1250 = 250 >= 0),
        # reduced margin with drafter still overflows free=4100 (3000+1500+256=4756 > 4100),
        # whereas undrafted (3000+250+768=4018 <= 4100) fits and drops the drafter.
        drop_mgr = _make_mgr(build=12001)

        def _drop_can_fit(model, num_ctx, **kwargs):
            drop_mgr.can_fit_calls.append((model, num_ctx, dict(kwargs)))
            extra = float(kwargs.get("extra_needed_mib", 0.0) or 0.0)
            margin = int(kwargs.get("safety_margin_mib", 768))
            needed = 3000.0 + extra
            return CanFitResult(
                ok=(needed + margin <= 4100.0),
                needed_mib=needed,
                free_mib=4100.0,
                margin_mib=margin,
                source="fake",
            )

        drop_mgr.can_fit = _drop_can_fit
        cmd_drop = list(cmd_clamp)
        with mock.patch.object(LC, "VRAM_PREFLIGHT_GATE", True):
            fit_d, extra_d, did_drop = lsp._try_drop_external_drafter(
                drop_mgr, clamp_slot, cmd_drop, "gemma-4:e4b-it-qat", 8192, initial_fit, 1500.0, 1250.0,
            )
            check(
                "_try_drop_external_drafter drops drafter cleanly when reduced extra >= 0 fits",
                did_drop is True
                and fit_d.ok is True
                and extra_d == 250.0
                and "--model-draft" not in cmd_drop
                and "--jinja" in cmd_drop,
            )

        # DFlash via unified _resolve_spec_config + _append_spec_flags
        with (
            mock.patch.object(lsp, "MODELS_DIR", tmp),
            mock.patch.object(LC, "LLAMA_FIT", True),
            mock.patch.object(LC, "GPU_BACKEND", "vulkan"),
            mock.patch.object(lsp, "GPU_BACKEND", "vulkan"),
            mock.patch.object(ML, "_help_supports", side_effect=_help_all_true),
            mock.patch.object(LC, "_try_registry_dspark_draft", return_value=None),
            mock.patch.object(LC, "_try_registry_dflash_draft", return_value=dflash_draft.name),
            mock.patch("model_configs.models_registry.get_dflash_draft_n_max", return_value=7),
            mock.patch("model_configs.models_registry.get_dflash_draft_p_min", return_value=0.4),
            mock.patch("model_configs.models_registry.get_dflash_draft_alternates", return_value=[]),
            mock.patch.dict(os.environ, {"HIVEMIND_DFLASH_SPEC": "1", "HIVEMIND_DSPARK_SPEC": "1"}, clear=False),
        ):
            flags_df = asyncio.run(lsp._phase_flags(mgr, "nemotron-3.5-bolt:e4b", None))
            cmd_df = ["llama-server"]
            asyncio.run(lsp._phase_model_flags(mgr, cmd_df, "nemotron-3.5-bolt:e4b", "nemotron", None, False))
            check(
                "DFlash disables --fit and emits single draft-dflash spec flags",
                flags_df.fit_supported is False
                and cmd_df.count("--spec-type") == 1
                and cmd_df[cmd_df.index("--spec-type") + 1] == "draft-dflash"
                and "--model-draft" in cmd_df,
            )


def _run_ud_lookup_tests() -> None:
    print("[2] -ud exact lookup before suffix strip")

    check(
        "_ud_candidates preserves exact -ud before stripped and strips #alias/:latest",
        LC._ud_candidates("qwen3.5:9b-ud#2") == ("qwen3.5:9b-ud", "qwen3.5:9b")
        and LC._ud_candidates("gemma-4:e4b:latest") == ("gemma-4:e4b",),
    )

    fake_profiles = {
        "probe-model:9b-ud": {
            "moe_cpu_experts": 12,
            "gpu_layers": 40,
            "mtp": True,
            "mtp_draft_filename": "probe-ud-draft.gguf",
            "kv_cache_type": "q8_0",
            "vram_gb_override": 5.5,
        },
        "probe-model:9b": {
            "moe_cpu_experts": 30,
            "gpu_layers": 99,
            "mtp": False,
            "kv_cache_type": "q4_0",
            "vram_gb_override": 4.2,
        },
        "onlybase-model:9b": {
            "moe_cpu_experts": 24,
            "gpu_layers": 80,
            "kv_cache_type": "q4_0",
            "vram_gb_override": 4.1,
        },
    }

    def _fake_get_profile(key: str):
        return fake_profiles.get(key)

    def _fake_get_moe_cpu_experts(key: str):
        p = fake_profiles.get(key)
        return p.get("moe_cpu_experts") if p else None

    def _fake_get_gpu_layers(key: str):
        p = fake_profiles.get(key)
        return p.get("gpu_layers") if p else None

    def _fake_get_mtp(key: str):
        p = fake_profiles.get(key)
        return p.get("mtp") if p else None

    with (
        mock.patch("model_configs.models_registry.get_profile", side_effect=_fake_get_profile),
        mock.patch("model_configs.models_registry.get_moe_cpu_experts", side_effect=_fake_get_moe_cpu_experts),
        mock.patch("model_configs.models_registry.get_gpu_layers", side_effect=_fake_get_gpu_layers),
        mock.patch("model_configs.models_registry.is_mtp", side_effect=_fake_get_mtp),
    ):
        check(
            "llama_config registry exact -ud wins over base profile",
            LC._try_registry_moe("probe-model:9b-ud") == 12
            and LC._try_registry_gpu_layers("probe-model:9b-ud") == 40
            and LC._try_registry_mtp("probe-model:9b-ud") is True
            and LC._try_registry_mtp_draft("probe-model:9b-ud") == "probe-ud-draft.gguf"
            and LC.moe_kv_cache_types("probe-model:9b-ud") == "q8_0"
            and LC.dense_kv_cache_type("probe-model:9b-ud") == "q8_0",
        )
        check(
            "llama_config unknown -ud falls back to base profile",
            LC._try_registry_moe("onlybase-model:9b-ud") == 24
            and LC._try_registry_gpu_layers("onlybase-model:9b-ud") == 80
            and LC._try_registry_moe("probe-model:9b-custom-ud") == 0
            and LC._try_registry_moe("probe-model:9b") == 30
            and LC._try_registry_moe("probe-model:9b-ud") == 12,
        )

    # Exact -ud wins before stripped fallback in llama_vram_table.vram_of
    with mock.patch("model_configs.models_registry.get_profile", side_effect=_fake_get_profile):
        check(
            "llama_vram_table.vram_of resolves exact -ud (5.5) before stripping to base (4.2) and falls back cleanly",
            lvt.vram_of("probe-model:9b-ud") == 5.5
            and lvt.vram_of("probe-model:9b") == 4.2
            and lvt.vram_of("onlybase-model:9b-ud") == 4.1,
        )

    # Unregistered -ud suffix cleanly falls back to -ud-stripped entry in _VRAM_TABLE
    fake_dense = {"probe-dense:7b": 4.8, "probe-dense:7b-ud": 6.1}
    with mock.patch.dict(lvt._VRAM_TABLE, fake_dense, clear=False):
        check(
            "_VRAM_TABLE exact -ud (6.1) wins over base (4.8); fallback works on base-only",
            lvt.vram_of("probe-dense:7b-ud") == 6.1
            and lvt.vram_of("probe-dense:7b") == 4.8,
        )
    with mock.patch.dict(lvt._VRAM_TABLE, {"only-base:7b": 4.9}, clear=False):
        check(
            "_VRAM_TABLE falls back to base when -ud variant absent",
            lvt.vram_of("only-base:7b-ud") == 4.9,
        )

    # _MOE_TABLE exact -ud vs stripped fallback + longest-prefix
    fake_moe_tbl = {
        "moe-probe:30b": {"vram_gb_base": 5.0, "ctx_per_1k_gb": 0.05},
        "moe-probe:30b-ud": {"vram_gb_base": 6.2, "ctx_per_1k_gb": 0.06},
    }
    with mock.patch.dict(lvt._MOE_TABLE, fake_moe_tbl, clear=False):
        cfg_ud = lvt.moe_cfg_for("moe-probe:30b-ud")
        cfg_base = lvt.moe_cfg_for("moe-probe:30b")
        check(
            "moe_cfg_for selects exact -ud entry before base",
            cfg_ud is not None and cfg_ud["vram_gb_base"] == 6.2
            and cfg_base is not None and cfg_base["vram_gb_base"] == 5.0,
        )

    # _phase_moe: exact -ud in MOE_CPU_EXPERTS wins over base; stripped fallback works
    mgr = _make_mgr(build=12001)
    slot = _make_slot(3)
    cmd_ud = ["llama-server"]
    cmd_strip = ["llama-server"]
    with (
        mock.patch.object(LC, "MOE_CPU_EXPERTS", {"udmoe:30b": 20, "udmoe:30b-ud": 9}),
        mock.patch.object(LC, "GPU_BACKEND", "vulkan"),
        mock.patch.object(lsp, "GPU_BACKEND", "vulkan"),
        mock.patch.object(LC, "MLOCK_MODEL", False),
        mock.patch.object(lvt, "moe_cfg_for", return_value=None),
        mock.patch("model_configs.models_registry.get_moe_pin", return_value=None),
    ):
        asyncio.run(lsp._phase_moe(mgr, slot, cmd_ud, "udmoe:30b-ud", 4096, False, 16, 8))
        with mock.patch.object(LC, "MOE_CPU_EXPERTS", {"stripbase:30b": 17}):
            asyncio.run(lsp._phase_moe(mgr, slot, cmd_strip, "stripbase:30b-ud", 4096, False, 16, 8))
        check(
            "_phase_moe selects exact -ud MOE_CPU_EXPERTS (9) before base (20) and strips when absent (17)",
            "--n-cpu-moe" in cmd_ud
            and cmd_ud[cmd_ud.index("--n-cpu-moe") + 1] == "9"
            and "--n-cpu-moe" in cmd_strip
            and cmd_strip[cmd_strip.index("--n-cpu-moe") + 1] == "17",
        )


def _run_claim_reclaim_stall_tests() -> None:
    print("[3] _phase_claim eliminates 12.5s reclaim stall when _to_kill is empty")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        gguf = tmp / "model.gguf"
        gguf.write_bytes(b"\x00" * 1024)

        reclaim_spy = mock.AsyncMock(return_value=True)
        sleep_spy = mock.AsyncMock(return_value=None)

        # Case A: live VRAM pressure, ONLY pinned peer -> _to_kill == [], MUST NOT call wait_for_vram_reclaim
        mgr_pinned = _make_mgr()
        pinned_peer = _make_slot(10, is_running=True, model="peer:1b", pinned=True)
        target_slot = _make_slot(11)
        mgr_pinned._slots = [pinned_peer, target_slot]

        with (
            mock.patch.object(lsp, "resolve_model_path", return_value=gguf),
            mock.patch.object(lsp, "vram_of_moe", return_value=2.0),
            mock.patch.object(lsp, "VRAM_BUDGET_GB", 7.5),
            mock.patch.object(lsp, "GPU_BACKEND", "vulkan"),
            mock.patch.object(lsp, "get_live_gpu_free_mib", return_value=500.0),
            mock.patch.object(lsp, "wait_for_vram_reclaim", reclaim_spy),
            mock.patch("backend.llama_start_phases.asyncio.sleep", sleep_spy),
        ):
            claim_res = asyncio.run(lsp._phase_claim(mgr_pinned, target_slot, "new:1b", 8192))
            check(
                "Live VRAM pressure with _to_kill==[] skips wait_for_vram_reclaim and asyncio.sleep",
                reclaim_spy.await_count == 0
                and sleep_spy.await_count == 0
                and claim_res.evicted is False
                and mgr_pinned.killed_slots == [],
                f" reclaim={reclaim_spy.await_count} sleep={sleep_spy.await_count}",
            )

        # Case B: formula pressure with ONLY pinned peer -> _to_kill == [], MUST NOT call wait_for_vram_reclaim
        reclaim_spy.reset_mock()
        sleep_spy.reset_mock()
        mgr_formula_pin = _make_mgr()
        pinned_peer.is_running = True
        mgr_formula_pin._slots = [pinned_peer, target_slot]
        with (
            mock.patch.object(lsp, "resolve_model_path", return_value=gguf),
            mock.patch.object(lsp, "vram_of_moe", return_value=6.0),
            mock.patch.object(lsp, "VRAM_BUDGET_GB", 7.5),
            mock.patch.object(lsp, "GPU_BACKEND", "vulkan"),
            mock.patch.object(lsp, "get_live_gpu_free_mib", return_value=5000.0),
            mock.patch.object(lsp, "wait_for_vram_reclaim", reclaim_spy),
            mock.patch("backend.llama_start_phases.asyncio.sleep", sleep_spy),
        ):
            claim_f = asyncio.run(lsp._phase_claim(mgr_formula_pin, target_slot, "new:6b", 8192))
            check(
                "Formula pressure with _to_kill==[] skips wait_for_vram_reclaim and asyncio.sleep",
                reclaim_spy.await_count == 0
                and sleep_spy.await_count == 0
                and claim_f.evicted is False,
            )

        # Case C: evictable running peer under live pressure -> kills peer AND DOES wait for reclaim
        reclaim_spy.reset_mock()
        sleep_spy.reset_mock()
        mgr_evict = _make_mgr()
        evict_peer = _make_slot(12, is_running=True, model="peer:1b", pinned=False)
        mgr_evict._slots = [evict_peer, target_slot]

        async def _fake_kill(s):
            mgr_evict.killed_slots.append(s.slot_id)
            s.is_running = False

        with (
            mock.patch.object(lsp, "resolve_model_path", return_value=gguf),
            mock.patch.object(lsp, "vram_of_moe", return_value=2.0),
            mock.patch.object(lsp, "VRAM_BUDGET_GB", 7.5),
            mock.patch.object(lsp, "GPU_BACKEND", "vulkan"),
            mock.patch.object(lsp, "get_live_gpu_free_mib", return_value=500.0),
            mock.patch.object(lsp, "wait_for_vram_reclaim", reclaim_spy),
            mock.patch.object(lsp, "_kill_slot_async", side_effect=_fake_kill),
            mock.patch("backend.llama_start_phases.asyncio.sleep", sleep_spy),
        ):
            claim_ev = asyncio.run(lsp._phase_claim(mgr_evict, target_slot, "new:1b", 8192))
            check(
                "Live VRAM pressure with non-empty _to_kill kills peer and awaits wait_for_vram_reclaim",
                reclaim_spy.await_count == 1
                and sleep_spy.await_count == 1
                and claim_ev.evicted is True
                and mgr_evict.killed_slots == [12],
            )


def _run_dynamic_vram_and_overflow_tests() -> None:
    print("[4] Dynamic TOTAL_VRAM_MIB & tight-GPU VRAM_OVERFLOW_MODELS gating")

    # 4a: defensive rounding buffer
    check(
        "_buffered_total_vram_mib rounds 8176 MiB w/ budget 7.5 to 8192 MiB",
        lvt._buffered_total_vram_mib(8176, 7.5) == 8192,
    )
    check(
        "_buffered_total_vram_mib keeps 16384 and 24576 unmodified and does not inflate 4096",
        lvt._buffered_total_vram_mib(16384, 7.5) == 16384
        and lvt._buffered_total_vram_mib(24576, 16.0) == 24576
        and lvt._buffered_total_vram_mib(4096, 7.5) == 4096
        and lvt._buffered_total_vram_mib(None, 7.5) == 8192,
    )

    # 4b: Linux DRM sysfs mem_info_vram_total probe
    with tempfile.TemporaryDirectory() as tmpdir:
        card_dev = Path(tmpdir) / "card0" / "device"
        card_dev.mkdir(parents=True)
        (card_dev / "mem_info_vram_total").write_text(str(16384 * 1024 * 1024), encoding="utf-8")

        real_path = Path

        def _fake_path(arg):
            if arg == "/sys/class/drm":
                return real_path(tmpdir)
            return real_path(arg)

        with (
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("pathlib.Path", side_effect=_fake_path),
            mock.patch.object(lvt, "_probe_nvidia_smi_total_mib", return_value=None),
        ):
            linux_mib = lvt._probe_gpu_total_vram_mib()
            check(
                "Linux DRM mem_info_vram_total probe converts bytes to MiB (16384)",
                linux_mib == 16384,
                f" ({linux_mib})",
            )

    # 4c: Windows PDH Dedicated Limit probe + winreg fallback + nvidia-smi fallback
    fake_win32pdh = types.ModuleType("win32pdh")
    fake_win32pdh.error = RuntimeError
    fake_win32pdh.PDH_FMT_LARGE = 1
    fake_win32pdh.ExpandCounterPath = mock.Mock(return_value=["\\GPU Adapter Memory(x)\\Dedicated Limit"])
    fake_win32pdh.OpenQuery = mock.Mock(return_value=11)
    fake_win32pdh.AddCounter = mock.Mock(return_value=22)
    fake_win32pdh.CollectQueryData = mock.Mock()
    fake_win32pdh.GetFormattedCounterValue = mock.Mock(return_value=(1, 12288 * 1024 * 1024))
    fake_win32pdh.CloseQuery = mock.Mock()

    with (
        mock.patch("platform.system", return_value="Windows"),
        mock.patch.dict(sys.modules, {"win32pdh": fake_win32pdh}),
    ):
        pdh_mib = lvt._probe_gpu_total_vram_mib()
        check("Windows PDH Dedicated Limit probe returns MiB (12288)", pdh_mib == 12288, f" ({pdh_mib})")

    # PDH fails -> falls back to winreg HardwareInformation.qwMemorySize
    fake_win32pdh_fail = types.ModuleType("win32pdh")
    fake_win32pdh_fail.error = RuntimeError
    fake_win32pdh_fail.ExpandCounterPath = mock.Mock(side_effect=RuntimeError("no dedicated limit"))

    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    fake_winreg = types.ModuleType("winreg")
    fake_winreg.HKEY_LOCAL_MACHINE = object()
    fake_winreg.OpenKey = mock.Mock(return_value=_FakeKey())
    fake_winreg.EnumKey = mock.Mock(side_effect=["0000", OSError("end")])
    fake_winreg.QueryValueEx = mock.Mock(
        return_value=((24576 * 1024 * 1024).to_bytes(8, "little"), 3)
    )
    with (
        mock.patch("platform.system", return_value="Windows"),
        mock.patch.dict(sys.modules, {"win32pdh": fake_win32pdh_fail, "winreg": fake_winreg}),
    ):
        reg_mib = lvt._probe_gpu_total_vram_mib()
        check(
            "Windows winreg HardwareInformation.qwMemorySize fallback returns MiB (24576)",
            reg_mib == 24576,
            f" ({reg_mib})",
        )

    # 4d: detect_gpu_total_vram_mib cache & refresh
    saved_cache = lvt._DETECTED_TOTAL_VRAM_CACHE
    saved_probed = lvt._DETECTED_TOTAL_VRAM_PROBED
    try:
        lvt._DETECTED_TOTAL_VRAM_CACHE = None
        lvt._DETECTED_TOTAL_VRAM_PROBED = False
        probe_mock = mock.Mock(side_effect=[16384, 24576])
        with mock.patch.object(lvt, "_probe_gpu_total_vram_mib", probe_mock):
            first = lvt.detect_gpu_total_vram_mib()
            second = lvt.detect_gpu_total_vram_mib()
            refreshed = lvt.detect_gpu_total_vram_mib(refresh=True)
            check(
                "detect_gpu_total_vram_mib caches result and honors refresh=True",
                first == 16384 and second == 16384 and refreshed == 24576 and probe_mock.call_count == 2,
            )
    finally:
        lvt._DETECTED_TOTAL_VRAM_CACHE = saved_cache
        lvt._DETECTED_TOTAL_VRAM_PROBED = saved_probed

    # 4e: Monkeypatch compatibility of TOTAL_VRAM_MIB & get_live_gpu_free_mib
    saved_total = lvt.TOTAL_VRAM_MIB
    try:
        lvt.TOTAL_VRAM_MIB = 24576
        check(
            "_effective_total_vram_mib honors monkeypatched TOTAL_VRAM_MIB (24576)",
            lvt._effective_total_vram_mib() == 24576,
        )
        fake_pdh_usage = types.ModuleType("win32pdh")
        fake_pdh_usage.PDH_FMT_LARGE = 1
        fake_pdh_usage.OpenQuery = mock.Mock(return_value=11)
        fake_pdh_usage.ExpandCounterPath = mock.Mock(
            return_value=[r"\GPU Adapter Memory(luid_0x0_phys_0)\Dedicated Usage"]
        )
        fake_pdh_usage.AddCounter = mock.Mock(return_value=22)
        fake_pdh_usage.CollectQueryData = mock.Mock()
        fake_pdh_usage.GetFormattedCounterValue = mock.Mock(
            return_value=(1, 2000 * 1024 * 1024)
        )
        fake_pdh_usage.CloseQuery = mock.Mock()
        saved_sel = lvt._sel_path_cache
        try:
            lvt._sel_path_cache = None
            with (
                mock.patch("platform.system", return_value="Windows"),
                mock.patch.dict(sys.modules, {"win32pdh": fake_pdh_usage}),
            ):
                free_24g = lvt.get_live_gpu_free_mib()
                check(
                    "get_live_gpu_free_mib subtracts usage from monkeypatched TOTAL_VRAM_MIB",
                    free_24g == 22576.0,
                    f" ({free_24g})",
                )
        finally:
            lvt._sel_path_cache = saved_sel
    finally:
        lvt.TOTAL_VRAM_MIB = saved_total

    # 4f: VRAM_OVERFLOW_MODELS gating (<= 12288 MiB and GPU_BACKEND != "cpu")
    with (
        mock.patch.object(lvt, "_current_gpu_backend", return_value="vulkan"),
        mock.patch.object(lvt, "_effective_total_vram_mib", return_value=8192),
    ):
        check(
            "VRAM_OVERFLOW_MODELS enabled on 8 GiB Vulkan GPU",
            "qwen3.6:27b" in lvt.VRAM_OVERFLOW_MODELS and len(lvt.VRAM_OVERFLOW_MODELS) == 3,
        )
    with (
        mock.patch.object(lvt, "_current_gpu_backend", return_value="cuda"),
        mock.patch.object(lvt, "_effective_total_vram_mib", return_value=12288),
    ):
        check(
            "VRAM_OVERFLOW_MODELS enabled at exact 12 GiB boundary on CUDA",
            "qwen3.6:27b" in lvt.VRAM_OVERFLOW_MODELS and bool(lvt.VRAM_OVERFLOW_MODELS),
        )
    with (
        mock.patch.object(lvt, "_current_gpu_backend", return_value="vulkan"),
        mock.patch.object(lvt, "_effective_total_vram_mib", return_value=16384),
    ):
        check(
            "VRAM_OVERFLOW_MODELS disabled on 16 GiB GPU",
            "qwen3.6:27b" not in lvt.VRAM_OVERFLOW_MODELS
            and len(lvt.VRAM_OVERFLOW_MODELS) == 0
            and not bool(lvt.VRAM_OVERFLOW_MODELS),
        )
    with (
        mock.patch.object(lvt, "_current_gpu_backend", return_value="cpu"),
        mock.patch.object(lvt, "_effective_total_vram_mib", return_value=8192),
    ):
        check(
            "VRAM_OVERFLOW_MODELS disabled when GPU_BACKEND == 'cpu'",
            "qwen3.6:27b" not in lvt.VRAM_OVERFLOW_MODELS and len(lvt.VRAM_OVERFLOW_MODELS) == 0,
        )


def main() -> int:
    _run_gemma4_and_spec_tests()
    _run_ud_lookup_tests()
    _run_claim_reclaim_stall_tests()
    _run_dynamic_vram_and_overflow_tests()
    return h.finish("audit_wave2_vram_spec")


if __name__ == "__main__":
    sys.exit(main())
