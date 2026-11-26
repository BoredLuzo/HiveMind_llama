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
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import TypedDict

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from settings_defaults import DEFAULT_SETTINGS  # noqa: E402
try:
    from deploy._common import (
        probe_linux_sysfs_vram as _common_sysfs_vram,
        probe_nvidia_smi_vram as _common_nvidia_smi,
        probe_ram_gb as _common_ram_gb,
        probe_windows_registry_vram as _common_winreg_vram,
    )
except ImportError:
    from _common import (  # type: ignore[import-not-found]
        probe_linux_sysfs_vram as _common_sysfs_vram,
        probe_nvidia_smi_vram as _common_nvidia_smi,
        probe_ram_gb as _common_ram_gb,
        probe_windows_registry_vram as _common_winreg_vram,
    )

_VALID_BACKENDS: frozenset[str] = frozenset({"cuda", "vulkan", "rocm", "cpu"})
_VRAM_ARG_RE = re.compile(r"\d+(?:\.\d+)?")


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
    return _common_ram_gb()


def _probe_nvidia_smi() -> tuple[float, str]:
    """Query max GPU VRAM (GiB) and GPU name via nvidia-smi."""
    return _common_nvidia_smi()


def _probe_windows_registry_vram() -> tuple[float, str]:
    """Fallback Windows display adapter VRAM probe via registry."""
    return _common_winreg_vram()


def _probe_linux_sysfs_vram() -> float:
    """Fallback Linux DRM sysfs VRAM probe in GiB."""
    return _common_sysfs_vram()


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
