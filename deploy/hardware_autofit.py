#!/usr/bin/env python3
"""Hardware-aware settings autofit vs. 8 GB reference profile for HiveMind.

Probes GPU VRAM, system RAM, and GPU vendor cleanly without reading existing
settings.json, computes tier-appropriate defaults (or exact 8 GB reference
defaults), and applies them atomically to settings.json while preserving
user state (workspace, models_dir, server_port, agent model assignments).
"""
from __future__ import annotations

import argparse
import copy
import ctypes
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import TypedDict

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from settings_defaults import DEFAULT_SETTINGS  # noqa: E402

_VALID_BACKENDS: frozenset[str] = frozenset({"cuda", "vulkan", "rocm", "cpu"})
_VRAM_ARG_RE = re.compile(r"\d+(?:\.\d+)?")
_PROBE_ERRORS = (OSError, ValueError, TypeError, AttributeError, ImportError, subprocess.SubprocessError)


class HardwareInfo(TypedDict):
    vram_gb: float
    ram_gb: float
    gpu_name: str
    nvidia: bool
    backend: str


class AutofitResult(TypedDict):
    profile: str
    tier: str
    backend: str
    vram_gb: float
    ram_gb: float
    gpu_name: str
    changes: dict[str, object]
    written: bool
    settings_path: str


def parse_vram_gb_arg(raw: str | float | None) -> float | None:
    """Parse a VRAM GB string/number (e.g. '24', '7,5', '16 GB') into positive GB."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return round(val, 2) if math.isfinite(val) and val > 0.0 else None
    text = str(raw).strip().replace(",", ".")
    if not text:
        return None
    if text.startswith("-"):
        return None
    match = _VRAM_ARG_RE.match(text)
    if not match:
        return None
    try:
        val = float(match.group(0))
    except ValueError:
        return None
    return round(val, 2) if math.isfinite(val) and val > 0.0 else None


def _probe_ram_gb() -> float:
    """Return physical system RAM in GiB (0.0 if unavailable)."""
    try:
        if sys.platform == "win32":
            windll = getattr(ctypes, "windll", None)
            if windll is not None:
                class _MemoryStatusEx(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                status = _MemoryStatusEx()
                status.dwLength = ctypes.sizeof(_MemoryStatusEx)
                if windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                    return round(status.ullTotalPhys / (1024 ** 3), 1)
        elif Path("/proc/meminfo").is_file():
            for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return round(kb / (1024 ** 2), 1)
    except _PROBE_ERRORS:
        return 0.0
    return 0.0


def _probe_nvidia_smi() -> tuple[float, str]:
    """Query max GPU VRAM (GiB) and GPU name via nvidia-smi."""
    nsmi = shutil.which("nvidia-smi")
    if not nsmi:
        return 0.0, ""
    try:
        cnf = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        res = subprocess.run(
            [nsmi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            creationflags=cnf,
        )
        if res.returncode != 0:
            return 0.0, ""
        best_vram = 0.0
        best_name = ""
        for line in res.stdout.splitlines():
            clean = line.strip()
            if "," not in clean:
                continue
            name_part, mem_part = clean.rsplit(",", 1)
            try:
                mib = float(mem_part.strip())
            except ValueError:
                continue
            gb = round(mib / 1024.0, 1)
            if gb > best_vram:
                best_vram = gb
                best_name = name_part.strip()
        return best_vram, best_name
    except _PROBE_ERRORS:
        return 0.0, ""


def _probe_windows_registry_vram() -> tuple[float, str]:
    """Fallback Windows display adapter VRAM probe via registry."""
    if sys.platform != "win32":
        return 0.0, ""
    try:
        import winreg

        base = r"SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        best_vram = 0.0
        best_name = ""
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as key:
            idx = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, idx)
                    idx += 1
                except OSError:
                    break
                try:
                    with winreg.OpenKey(key, sub) as subkey:
                        raw_sz, _ = winreg.QueryValueEx(subkey, "HardwareInformation.qwMemorySize")
                        desc, _ = winreg.QueryValueEx(subkey, "DriverDesc")
                        byte_sz = (
                            int.from_bytes(raw_sz, "little")
                            if isinstance(raw_sz, (bytes, bytearray))
                            else int(raw_sz)
                        )
                        gb = round(byte_sz / (1024 ** 3), 1)
                        if gb > best_vram:
                            best_vram = gb
                            best_name = str(desc).strip()
                except _PROBE_ERRORS:
                    continue
        return best_vram, best_name
    except _PROBE_ERRORS:
        return 0.0, ""


def _probe_linux_sysfs_vram() -> float:
    """Fallback Linux DRM sysfs VRAM probe in GiB."""
    if sys.platform == "win32":
        return 0.0
    try:
        max_bytes = 0.0
        for card in Path("/sys/class/drm").glob("card[0-9]*/device"):
            tf = card / "mem_info_vram_total"
            if not tf.is_file():
                continue
            try:
                val = float(tf.read_text(encoding="utf-8", errors="replace").strip())
            except _PROBE_ERRORS:
                continue
            if val > max_bytes:
                max_bytes = val
        return round(max_bytes / (1024 ** 3), 1) if max_bytes > 0.0 else 0.0
    except _PROBE_ERRORS:
        return 0.0


def probe_system_hardware() -> HardwareInfo:
    """Detect GPU VRAM, system RAM, GPU name, and default backend (ignores settings.json)."""
    ram_gb = _probe_ram_gb()
    vram_gb, gpu_name = _probe_nvidia_smi()
    nvidia = bool(gpu_name and vram_gb > 0.0)
    if vram_gb <= 0.0:
        if sys.platform == "win32":
            vram_gb, gpu_name = _probe_windows_registry_vram()
        else:
            vram_gb = _probe_linux_sysfs_vram()
        if gpu_name and "nvidia" in gpu_name.lower() and vram_gb > 0.0:
            nvidia = True
    if nvidia or (gpu_name and "nvidia" in gpu_name.lower()):
        backend = "cuda"
    elif vram_gb > 0.0:
        backend = "vulkan"
    else:
        backend = "cpu"
    return {
        "vram_gb": float(vram_gb),
        "ram_gb": float(ram_gb),
        "gpu_name": gpu_name,
        "nvidia": nvidia,
        "backend": backend,
    }


def _reference_8gb_patch() -> dict[str, object]:
    """Exact 8 GB reference defaults aligned with settings_defaults.py."""
    return {
        "vram_budget_gb": 7.5,
        "max_model_size_gb": 6.0,
        "prefer_smaller_models": True,
        "max_concurrent_models": 2,
        "llama_max_slots": 3,
        "duo_ubatch": 512,
        "llama_ubatch_size": 512,
        "duo_parallel_preexplore": False,
        "duo_evict_planner_on_coder": True,
        "llama_kv_cache_type": "q4_0",
        "llama_cache_ram_mib": 1024,
    }


def compute_profile_patch(
    *,
    profile: str,
    vram_gb: float,
    ram_gb: float = 0.0,
    backend: str = "",
) -> tuple[str, dict[str, object]]:
    """Compute (tier_name, settings_patch) for 'autofit' or '8gb' profile."""
    prof_norm = str(profile or "").strip().lower()
    if prof_norm in ("8gb", "reference", "reference-8gb"):
        norm_profile = "8gb"
    elif prof_norm == "autofit":
        norm_profile = "autofit"
    else:
        raise ValueError(f"Unsupported profile: {profile!r} (expected 'autofit' or '8gb')")

    be_norm = str(backend or "").strip().lower()
    if be_norm and be_norm not in _VALID_BACKENDS:
        raise ValueError(f"Unsupported backend: {backend!r}")

    vram_val = float(vram_gb or 0.0)
    ram_val = float(ram_gb or 0.0)

    if norm_profile == "8gb":
        tier = "8gb_reference"
        patch = _reference_8gb_patch()
    elif be_norm == "cpu":
        tier = "cpu_only"
        patch = _reference_8gb_patch()
        patch["llama_cache_ram_mib"] = 2048 if ram_val >= 30.0 else 1024
    elif vram_val >= 38.0:
        tier = "datacenter_40gb_plus"
        patch = {
            "vram_budget_gb": round(vram_val * 0.94, 1),
            "max_model_size_gb": round(vram_val * 0.85, 1),
            "prefer_smaller_models": False,
            "max_concurrent_models": 4,
            "llama_max_slots": 4,
            "duo_ubatch": 1024,
            "llama_ubatch_size": 1024,
            "duo_parallel_preexplore": False,
            "duo_evict_planner_on_coder": False,
            "llama_kv_cache_type": "q8_0",
            "llama_cache_ram_mib": 4096 if ram_val >= 48.0 else 2048,
        }
    elif vram_val >= 22.0:
        tier = "high_vram_24gb"
        patch = {
            "vram_budget_gb": round(vram_val * 0.92, 1),
            "max_model_size_gb": round(vram_val * 0.82, 1),
            "prefer_smaller_models": False,
            "max_concurrent_models": 3,
            "llama_max_slots": 3,
            "duo_ubatch": 1024,
            "llama_ubatch_size": 1024,
            "duo_parallel_preexplore": False,
            "duo_evict_planner_on_coder": False,
            "llama_kv_cache_type": "q8_0",
            "llama_cache_ram_mib": 4096 if ram_val >= 48.0 else 2048,
        }
    elif vram_val >= 10.5:
        tier = "mid_vram_12_16gb"
        patch = {
            "vram_budget_gb": round(vram_val * 0.90, 1),
            "max_model_size_gb": round(vram_val * 0.78, 1),
            "prefer_smaller_models": False,
            "max_concurrent_models": 2,
            "llama_max_slots": 3,
            "duo_ubatch": 512,
            "llama_ubatch_size": 512,
            "duo_parallel_preexplore": False,
            "duo_evict_planner_on_coder": True,
            "llama_kv_cache_type": "q4_0",
            "llama_cache_ram_mib": 2048 if ram_val >= 30.0 else 1024,
        }
    elif 0.0 < vram_val < 7.0:
        tier = "low_vram"
        patch = _reference_8gb_patch()
        patch["vram_budget_gb"] = round(max(2.0, vram_val - 0.5), 1)
    else:
        tier = "8gb_reference"
        patch = _reference_8gb_patch()

    if be_norm:
        patch["gpu_backend"] = be_norm
    return tier, patch


def _write_json_atomic(path: Path, data: dict[str, object]) -> None:
    """Atomically write JSON settings to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f".settings_af_{os.getpid()}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError as unlink_err:
                sys.stderr.write(f"[autofit] warning: cleanup of {tmp} failed: {unlink_err}\n")
        raise


def apply_hardware_profile(
    settings_path: Path,
    *,
    profile: str = "autofit",
    backend: str = "",
    vram_gb: float | None = None,
    ram_gb: float | None = None,
    force: bool = False,
    keep_vram_budget: bool = False,
    dry_run: bool = False,
) -> AutofitResult:
    """Compute hardware-matched settings and write/update `settings_path` atomically."""
    need_probe = vram_gb is None or ram_gb is None or not str(backend or "").strip()
    probed: HardwareInfo | None = probe_system_hardware() if need_probe else None

    eff_ram = float(ram_gb) if ram_gb is not None else float(probed["ram_gb"] if probed is not None else 0.0)
    eff_backend = (
        str(backend).strip().lower()
        if str(backend or "").strip()
        else str(probed["backend"] if probed is not None else "")
    )
    gpu_name = str(probed["gpu_name"] if probed is not None else "")

    is_new_file = not settings_path.is_file()
    existing: dict[str, object] = {}
    if not is_new_file:
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Refusing to overwrite unreadable/invalid settings file {settings_path}: {exc}"
            ) from exc
        if not isinstance(loaded, dict):
            raise RuntimeError(
                f"Refusing to overwrite non-object settings file {settings_path}"
            )
        existing = loaded

    if vram_gb is not None:
        eff_vram = float(vram_gb)
    else:
        eff_vram = float(probed["vram_gb"] if probed is not None else 0.0)
        if eff_vram <= 0.0:
            raw_cfg_vram = existing.get("vram_budget_gb")
            if isinstance(raw_cfg_vram, (int, float)) and not isinstance(raw_cfg_vram, bool) and raw_cfg_vram > 0:
                eff_vram = float(raw_cfg_vram)

    tier, patch = compute_profile_patch(
        profile=profile,
        vram_gb=eff_vram,
        ram_gb=eff_ram,
        backend=eff_backend,
    )

    filtered_patch = dict(patch)
    existing_vram = existing.get("vram_budget_gb")
    if (
        keep_vram_budget
        and not is_new_file
        and isinstance(existing_vram, (int, float))
        and not isinstance(existing_vram, bool)
        and float(existing_vram) > 0.0
    ):
        filtered_patch.pop("vram_budget_gb", None)

    out_data: dict[str, object] = (
        copy.deepcopy(DEFAULT_SETTINGS) if is_new_file else dict(existing)
    )
    changes: dict[str, object] = {}
    for key, val in filtered_patch.items():
        if not is_new_file and not force and key in out_data:
            continue
        if is_new_file or out_data.get(key) != val:
            out_data[key] = val
            changes[key] = val

    written = False
    if not dry_run and (is_new_file or bool(changes)):
        out_data["settings_saved_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _write_json_atomic(settings_path, out_data)
        written = True

    return {
        "profile": "8gb" if str(profile).strip().lower() in ("8gb", "reference", "reference-8gb") else "autofit",
        "tier": tier,
        "backend": eff_backend,
        "vram_gb": eff_vram,
        "ram_gb": eff_ram,
        "gpu_name": gpu_name,
        "changes": changes,
        "written": written,
        "settings_path": str(settings_path),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply hardware-autofit or 8 GB reference defaults to HiveMind settings.json."
    )
    parser.add_argument(
        "--profile",
        choices=("autofit", "8gb", "reference"),
        default="autofit",
        help="Settings profile: 'autofit' (hardware-matched) or '8gb' / 'reference'.",
    )
    parser.add_argument(
        "--backend",
        choices=("", "cuda", "vulkan", "rocm", "cpu"),
        default="",
        help="Explicit GPU/CPU backend (defaults to auto-detected).",
    )
    parser.add_argument(
        "--vram-gb",
        default=None,
        help="Explicit GPU VRAM in GB (optional override).",
    )
    parser.add_argument(
        "--ram-gb",
        type=float,
        default=None,
        help="Explicit system RAM in GB (optional override).",
    )
    parser.add_argument(
        "--settings",
        default=str(_ROOT / "settings.json"),
        help="Path to settings.json.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing profile keys in settings.json while keeping user workspace/models.",
    )
    parser.add_argument(
        "--keep-vram-budget",
        action="store_true",
        help="Preserve an existing positive vram_budget_gb in settings.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute profile changes without writing settings.json.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output summary as JSON.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    vram_override = parse_vram_gb_arg(args.vram_gb) if args.vram_gb is not None else None
    result = apply_hardware_profile(
        Path(args.settings),
        profile=args.profile,
        backend=args.backend,
        vram_gb=vram_override,
        ram_gb=args.ram_gb,
        force=bool(args.force),
        keep_vram_budget=bool(args.keep_vram_budget),
        dry_run=bool(args.dry_run),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=True, indent=2))
    else:
        status = "written" if result["written"] else ("dry-run" if args.dry_run else "unchanged")
        print(
            f"[autofit] profile={result['profile']} tier={result['tier']} "
            f"backend={result['backend'] or 'auto'} vram={result['vram_gb']}GB "
            f"changes={len(result['changes'])} ({status})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
