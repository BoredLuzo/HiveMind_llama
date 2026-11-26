# -*- coding: utf-8 -*-
"""Shared hardware probing and atomic file helpers for deploy scripts (stdlib-only)."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

PROBE_ERRORS = (OSError, ValueError, TypeError, AttributeError, ImportError, subprocess.SubprocessError)


def atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically via same-dir temp file + os.replace."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def load_json_dict(path: Path) -> dict:
    """Best-effort JSON object loader (returns {} on error or non-dict)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def probe_ram_gb() -> float:
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
    except PROBE_ERRORS:
        return 0.0
    return 0.0


def probe_nvidia_smi_vram() -> tuple[float, str]:
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
    except PROBE_ERRORS:
        return 0.0, ""


def probe_windows_registry_vram() -> tuple[float, str]:
    """Windows display adapter VRAM probe via registry."""
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
                except PROBE_ERRORS:
                    continue
        return best_vram, best_name
    except PROBE_ERRORS:
        return 0.0, ""


def probe_linux_sysfs_vram() -> float:
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
            except PROBE_ERRORS:
                continue
            if val > max_bytes:
                max_bytes = val
        return round(max_bytes / (1024 ** 3), 1) if max_bytes > 0.0 else 0.0
    except PROBE_ERRORS:
        return 0.0
