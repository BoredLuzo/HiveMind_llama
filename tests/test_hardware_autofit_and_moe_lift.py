# -*- coding: utf-8 -*-
"""Regression suite for 1A: hardware-autofit vs 8-GB reference profile,
Linux CUDA autodetect / Windows installer wiring, and runtime MoE All-GPU lift.

All cases are hermetic (temp dirs + mocks; no real install or network calls).
Run: python tests/test_hardware_autofit_and_moe_lift.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import backend.gguf_meta as GM  # noqa: E402
import backend.llama_config as LC  # noqa: E402
import backend.llama_start_phases as LSP  # noqa: E402
import backend.llama_vram_table as LVT  # noqa: E402
import backend.runtime_flags as RF  # noqa: E402
import deploy.hardware_autofit as HAF  # noqa: E402
from settings_defaults import DEFAULT_SETTINGS  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


def test_hardware_autofit_tiers_and_validation() -> None:
    check("parse_vram_gb_arg handles integers, comma decimals, units, rejects junk",
          HAF.parse_vram_gb_arg("24") == 24.0
          and HAF.parse_vram_gb_arg("7,5") == 7.5
          and HAF.parse_vram_gb_arg(" 16.0 GB ") == 16.0
          and HAF.parse_vram_gb_arg("eight") is None
          and HAF.parse_vram_gb_arg("-4") is None
          and HAF.parse_vram_gb_arg("") is None
          and HAF.parse_vram_gb_arg(None) is None
          and HAF.parse_vram_gb_arg(True) is None)

    for prof, vram, ram, be in (
        ("8gb", 80.0, 128.0, "cuda"),
        ("reference", 24.0, 64.0, "vulkan"),
        ("autofit", 6.0, 16.0, "vulkan"),
        ("autofit", 8.0, 16.0, "vulkan"),
        ("autofit", 16.0, 32.0, "vulkan"),
        ("autofit", 24.0, 64.0, "cuda"),
        ("autofit", 40.0, 128.0, "cuda"),
        ("autofit", 80.0, 256.0, "cuda"),
        ("autofit", 0.0, 32.0, "cpu"),
    ):
        tier, patch = HAF.compute_profile_patch(profile=prof, vram_gb=vram, ram_gb=ram, backend=be)
        bad_keys = [k for k in patch if k not in DEFAULT_SETTINGS]
        ub_val = patch["duo_ubatch"]
        check(f"patch keys canonical ({prof} {vram}GB {be} -> {tier})",
              not bad_keys
              and patch["llama_kv_cache_type"] in RF._KV_CACHE_CHOICES
              and isinstance(ub_val, int) and ub_val <= 1024,
              f"bad_keys={bad_keys}, patch={patch}")

    t80, p80 = HAF.compute_profile_patch(profile="autofit", vram_gb=80.0, ram_gb=256.0, backend="cuda")
    t40, p40 = HAF.compute_profile_patch(profile="autofit", vram_gb=40.0, ram_gb=64.0, backend="cuda")
    t24, p24 = HAF.compute_profile_patch(profile="autofit", vram_gb=24.0, ram_gb=48.0, backend="cuda")
    t16, p16 = HAF.compute_profile_patch(profile="autofit", vram_gb=16.0, ram_gb=32.0, backend="vulkan")
    t6, p6 = HAF.compute_profile_patch(profile="autofit", vram_gb=6.0, ram_gb=16.0, backend="vulkan")
    t_ref, p_ref = HAF.compute_profile_patch(profile="8gb", vram_gb=80.0, ram_gb=256.0, backend="cuda")

    check("A100 80GB tier sets datacenter defaults",
          t80 == "datacenter_40gb_plus"
          and p80["vram_budget_gb"] == 75.2
          and p80["max_concurrent_models"] == 4
          and p80["llama_max_slots"] == 4
          and p80["duo_ubatch"] == 1024
          and p80["llama_kv_cache_type"] == "q8_0"
          and p80["duo_parallel_preexplore"] is False
          and p80["duo_evict_planner_on_coder"] is False
          and p80["llama_cache_ram_mib"] == 4096)
    check("A100 40GB tier scales budget to 37.6GB",
          t40 == "datacenter_40gb_plus" and p40["vram_budget_gb"] == 37.6)
    check("24GB GPU tier sets 22.1GB + concurrency 3",
          t24 == "high_vram_24gb"
          and p24["vram_budget_gb"] == 22.1
          and p24["max_concurrent_models"] == 3
          and p24["duo_ubatch"] == 1024)
    check("16GB GPU tier sets 14.4GB + q4_0",
          t16 == "mid_vram_12_16gb"
          and p16["vram_budget_gb"] == 14.4
          and p16["duo_ubatch"] == 512
          and p16["llama_kv_cache_type"] == "q4_0")
    check("6GB GPU tier sets low_vram 5.5GB",
          t6 == "low_vram" and p6["vram_budget_gb"] == 5.5)
    check("explicit 8gb profile ignores 80GB GPU and applies 7.5GB reference defaults",
          t_ref == "8gb_reference"
          and p_ref["vram_budget_gb"] == 7.5
          and p_ref["max_model_size_gb"] == 6.0
          and p_ref["prefer_smaller_models"] is True
          and p_ref["duo_parallel_preexplore"] is False
          and p_ref["llama_kv_cache_type"] == "q4_0")

    bad_prof = False
    try:
        HAF.compute_profile_patch(profile="unknown", vram_gb=8.0)
    except ValueError:
        bad_prof = True
    bad_be = False
    try:
        HAF.compute_profile_patch(profile="autofit", vram_gb=8.0, backend="metal")
    except ValueError:
        bad_be = True
    check("invalid profile or backend raises ValueError", bad_prof and bad_be)


def test_hardware_autofit_file_apply_and_cli() -> None:
    with tempfile.TemporaryDirectory(prefix="hm1a_af_") as td:
        sf = Path(td) / "settings.json"
        res_new = HAF.apply_hardware_profile(
            sf, profile="autofit", backend="cuda", vram_gb=80.0, ram_gb=128.0,
        )
        data = json.loads(sf.read_text(encoding="utf-8"))
        check("fresh settings.json seeded with DEFAULT_SETTINGS + A100 autofit",
              res_new["written"] is True
              and res_new["tier"] == "datacenter_40gb_plus"
              and data["vram_budget_gb"] == 75.2
              and data["gpu_backend"] == "cuda"
              and "agents" in data and "server_port" in data)

        # Preserve existing user values unless --force
        sf.write_text(
            json.dumps({
                "workspace": "C:/my/ws",
                "server_port": 8099,
                "vram_budget_gb": 19.5,
                "duo_ubatch": 512,
                "moe_cpu_experts": {"air:26b-a4b": 10},
            }),
            encoding="utf-8",
        )
        res_noforce = HAF.apply_hardware_profile(
            sf, profile="autofit", backend="cuda", vram_gb=80.0, ram_gb=128.0, force=False,
        )
        data_nf = json.loads(sf.read_text(encoding="utf-8"))
        check("existing keys preserved without --force while missing profile keys added",
              data_nf["vram_budget_gb"] == 19.5
              and data_nf["duo_ubatch"] == 512
              and data_nf["workspace"] == "C:/my/ws"
              and data_nf["llama_kv_cache_type"] == "q8_0"
              and "vram_budget_gb" not in res_noforce["changes"])

        res_kv = HAF.apply_hardware_profile(
            sf, profile="autofit", backend="cuda", vram_gb=24.0, ram_gb=64.0,
            force=True, keep_vram_budget=True,
        )
        data_kv = json.loads(sf.read_text(encoding="utf-8"))
        check("--keep-vram-budget + --force updates tuning while keeping explicit VRAM/user keys",
              data_kv["vram_budget_gb"] == 19.5
              and data_kv["duo_ubatch"] == 1024
              and data_kv["server_port"] == 8099
              and data_kv["workspace"] == "C:/my/ws"
              and data_kv["moe_cpu_experts"] == {"air:26b-a4b": 10}
              and res_kv["written"] is True)

        before_raw = sf.read_text(encoding="utf-8")
        res_dry = HAF.apply_hardware_profile(
            sf, profile="8gb", backend="vulkan", vram_gb=8.0, ram_gb=16.0,
            force=True, dry_run=True,
        )
        check("--dry-run reports changes without modifying settings.json",
              res_dry["written"] is False
              and bool(res_dry["changes"])
              and sf.read_text(encoding="utf-8") == before_raw)

        cli_sf = Path(td) / "cli_settings.json"
        proc = subprocess.run(
            [
                sys.executable, str(ROOT / "deploy" / "hardware_autofit.py"),
                "--profile", "autofit", "--vram-gb", "40", "--ram-gb", "128",
                "--backend", "cuda", "--settings", str(cli_sf), "--json",
            ],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        cli_out = json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else {}
        check("CLI round-trip writes 40GB A100 config and prints valid JSON",
              proc.returncode == 0
              and cli_out.get("tier") == "datacenter_40gb_plus"
              and cli_out.get("written") is True
              and cli_sf.is_file(),
              f"rc={proc.returncode} err={proc.stderr}")


def test_installer_integration_guards() -> None:
    sh_bytes = (ROOT / "deploy" / "install_linux.sh").read_bytes()
    sh = sh_bytes.decode("utf-8", errors="replace")
    check("install_linux.sh is LF-only", b"\r" not in sh_bytes)
    check("install_linux.sh has CUDA autodetect, --autofit/--8gb, hardware_autofit.py, no silent || true",
          "nvidia-smi --query-gpu=name" in sh
          and 'LLAMA_BACKEND="cuda"' in sh
          and "--autofit)" in sh
          and "--8gb|--reference-8gb)" in sh
          and "deploy/hardware_autofit.py" in sh
          and "|| true" not in sh)

    bat_bytes = (ROOT / "install.bat").read_bytes()
    bat = bat_bytes.decode("ascii", errors="strict")
    check("install.bat is ASCII+CRLF with --autofit/--8gb and goto hardware_autofit wiring",
          all(b < 128 for b in bat_bytes)
          and b"\n" not in bat_bytes.replace(b"\r\n", b"")
          and "--autofit" in bat
          and "--8gb" in bat
          and 'goto apply_hw_profile_autofit' in bat
          and 'deploy\\hardware_autofit.py' in bat
          and "from settings import load_settings, save_settings" in bat)

    if os.name == "nt":
        sim_parent = Path(tempfile.mkdtemp(prefix="hm1a_bat_")) / "HiveMind (9)"
        sim_parent.mkdir(parents=True)
        try:
            shutil.copy2(ROOT / "install.bat", sim_parent / "install.bat")
            proc = subprocess.run(
                ["cmd", "/d", "/c", "install.bat", "--autofit"],
                cwd=str(sim_parent), input="N\n",
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            combined = (proc.stdout + "\n" + proc.stderr).lower()
            check("install.bat --autofit runs cleanly on parenthesized path",
                  proc.returncode == 0
                  and "aborted - nothing was installed." in combined
                  and "was unexpected at this time" not in combined,
                  f"rc={proc.returncode}")
        finally:
            shutil.rmtree(sim_parent.parent, ignore_errors=True)


class _DummyMgr:
    _moe_flag_supported = True
    _load_mode_supported = True
    _kv_flag_supported = True
    _slots: list[object] = []


class _LogCapHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[tuple[int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.levelno, record.getMessage()))


async def _run_phase_moe_case(
    model: str,
    num_ctx: int,
    *,
    budget_gb: float,
    free_mib: float | None,
    file_size_gb: float,
    moe_override: dict | None = None,
    user_profile: bool = False,
    gpu_backend: str = "cuda",
    ubatch: int = 512,
    slots: list[object] | None = None,
) -> tuple[LSP._MoePhase, list[tuple[int, str]]]:
    slot = SimpleNamespace(slot_id=0, _moe_cache_used=False)
    mgr = _DummyMgr()
    mgr._slots = slots or []
    cmd = ["llama-server", "--ctx-size", str(num_ctx), "--threads", "16", "--threads-batch", "16"]
    meta = {"file_size_gb": file_size_gb, "block_count": 40} if file_size_gb > 0 else {}
    cap = _LogCapHandler()
    LSP.logger.addHandler(cap)
    old_level = LSP.logger.level
    LSP.logger.setLevel(logging.INFO)
    try:
        with (
            mock.patch.object(GM, "read_gguf_metadata", return_value=meta),
            mock.patch.object(LVT, "_is_user_profile", return_value=user_profile),
            mock.patch.object(LC, "MOE_CPU_EXPERTS", moe_override or {}),
            mock.patch.object(LC, "LLAMA_UBATCH", ubatch),
            mock.patch.object(LC, "GPU_BACKEND", gpu_backend),
            mock.patch.object(LSP, "VRAM_BUDGET_GB", budget_gb),
            mock.patch.object(LSP._llama_utils, "VRAM_BUDGET_GB", budget_gb),
            mock.patch.object(LSP, "GPU_BACKEND", gpu_backend),
            mock.patch.object(LSP, "get_live_gpu_free_mib", return_value=free_mib),
        ):
            mph = await LSP._phase_moe(
                mgr, slot, cmd, model, num_ctx, False, 16, 16, gpu_layers=99,
            )
    finally:
        LSP.logger.setLevel(old_level)
        LSP.logger.removeHandler(cap)
    return mph, cap.records


def test_runtime_moe_all_gpu_lift() -> None:
    async def _cases() -> None:
        # 1. qwen3.6:35b-a3b on A100 80GB -> lifted to 0 CPU experts
        mph80, logs80 = await _run_phase_moe_case(
            "qwen3.6:35b-a3b", 65536, budget_gb=75.2, free_mib=78000.0, file_size_gb=21.5, ubatch=1024,
        )
        check("qwen3.6:35b-a3b on A100 80GB lifts --n-cpu-moe -> 0 cleanly without false calibration warn",
              mph80.all_gpu_lifted is True
              and mph80.all_gpu_extra_mib > 10000.0
              and "--n-cpu-moe" not in mph80.cmd
              and any("[MOE-ALL-GPU]" in m for _, m in logs80)
              and not any("[VRAM-CALIBRATION]" in m for _, m in logs80),
              f"lifted={mph80.all_gpu_lifted}, extra={mph80.all_gpu_extra_mib}")

        # 2. qwen3.6:35b-a3b on A100 40GB -> lifted at 32k ctx
        mph40, _ = await _run_phase_moe_case(
            "qwen3.6:35b-a3b", 32768, budget_gb=37.6, free_mib=39000.0, file_size_gb=21.5, ubatch=1024,
        )
        check("qwen3.6:35b-a3b @32k on A100 40GB lifts to all-GPU",
              mph40.all_gpu_lifted is True and "--n-cpu-moe" not in mph40.cmd)

        # 3. qwen3.6:35b-a3b on RTX 4090 24GB -> NOT lifted (21.5 * 1.08 + overhead > 22.1GB budget)
        mph24_q36, _ = await _run_phase_moe_case(
            "qwen3.6:35b-a3b", 32768, budget_gb=22.1, free_mib=22800.0, file_size_gb=21.5, ubatch=1024,
        )
        check("qwen3.6:35b-a3b on 24GB GPU stays offloaded (not lifted)",
              mph24_q36.all_gpu_lifted is False
              and mph24_q36.all_gpu_extra_mib == 0.0
              and "--n-cpu-moe" in mph24_q36.cmd)

        # 4. air:26b-a4b (12.545GB) on 24GB GPU -> lifted to all-GPU; on 8GB GPU -> NOT lifted
        mph24_air, _ = await _run_phase_moe_case(
            "air:26b-a4b", 32768, budget_gb=22.1, free_mib=22800.0, file_size_gb=12.545, ubatch=512,
        )
        mph8_air, _ = await _run_phase_moe_case(
            "air:26b-a4b", 32768, budget_gb=7.5, free_mib=6500.0, file_size_gb=12.545, ubatch=512,
        )
        check("air:26b-a4b lifts on 24GB GPU and keeps --n-cpu-moe 26 on 8GB GPU",
              mph24_air.all_gpu_lifted is True
              and "--n-cpu-moe" not in mph24_air.cmd
              and mph8_air.all_gpu_lifted is False
              and "--n-cpu-moe" in mph8_air.cmd
              and mph8_air.cmd[mph8_air.cmd.index("--n-cpu-moe") + 1] == "26")

        # 5. Explicit MOE_CPU_EXPERTS override NOT overridden on A100
        mph_exp, _ = await _run_phase_moe_case(
            "air:26b-a4b", 16384, budget_gb=75.2, free_mib=78000.0, file_size_gb=12.545,
            moe_override={"air:26b-a4b": 10},
        )
        check("explicit MOE_CPU_EXPERTS override (10) preserved on 80GB A100",
              mph_exp.all_gpu_lifted is False
              and "--n-cpu-moe" in mph_exp.cmd
              and mph_exp.cmd[mph_exp.cmd.index("--n-cpu-moe") + 1] == "10")

        # 6. User profile override NOT overridden on A100
        mph_usr, _ = await _run_phase_moe_case(
            "air:26b-a4b", 16384, budget_gb=75.2, free_mib=78000.0, file_size_gb=12.545,
            user_profile=True,
        )
        check("user profile override prevents auto-all-gpu lift",
              mph_usr.all_gpu_lifted is False and "--n-cpu-moe" in mph_usr.cmd)

        # 7. Missing GGUF metadata fails closed (NOT lifted)
        mph_nometa, _ = await _run_phase_moe_case(
            "air:26b-a4b", 16384, budget_gb=75.2, free_mib=78000.0, file_size_gb=0.0,
        )
        check("missing GGUF metadata fails closed without lifting",
              mph_nometa.all_gpu_lifted is False and "--n-cpu-moe" in mph_nometa.cmd)

        # 8. CPU backend NOT lifted even with huge budget
        mph_cpu, _ = await _run_phase_moe_case(
            "air:26b-a4b", 16384, budget_gb=75.2, free_mib=None, file_size_gb=12.545,
            gpu_backend="cpu",
        )
        check("cpu backend never lifts MoE to all-GPU",
              mph_cpu.all_gpu_lifted is False and "--n-cpu-moe" not in mph_cpu.cmd)

        # 9. Formula-fallback (live_free=None) subtracts loaded peer slot VRAM
        peer = SimpleNamespace(slot_id=1, model="qwen3.5:9b-mtp", _num_ctx=16384)
        mph_peer, _ = await _run_phase_moe_case(
            "qwen3.6:35b-a3b", 16384, budget_gb=26.0, free_mib=None, file_size_gb=21.5,
            slots=[peer],
        )
        check("live_free=None subtracts peer slot VRAM so lift skips safely",
              mph_peer.all_gpu_lifted is False and "--n-cpu-moe" in mph_peer.cmd)

    asyncio.run(_cases())


def main() -> int:
    test_hardware_autofit_tiers_and_validation()
    test_hardware_autofit_file_apply_and_cli()
    test_installer_integration_guards()
    test_runtime_moe_all_gpu_lift()
    return h.finish("hardware_autofit_and_moe_lift")


if __name__ == "__main__":
    sys.exit(main())
