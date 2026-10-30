# -*- coding: utf-8 -*-
"""Shared preflight for the llama loader and the bench/measurement scripts.

One place for the checks that the freeze lesson (2026-10-28: an 18.3 GB
commit at 14 MB free RAM pagged the machine) demands before any model
or benchmark starts:
  - free RAM vs model size + headroom,
  - no second llama process running alongside,
plus the tree-kill helper for bench timeouts.
Pure helpers + thin process queries, no GPU calls - unit-testable.
"""
import os
import subprocess
import time
from pathlib import Path

_RAM_HEADROOM_GB = 8.0
LOAD_MODES = ("none", "mmap", "mlock", "mmap+mlock", "dio")
_BENCH_LLAMA_NAMES = ("llama-server.exe", "llama-server", "llama-bench.exe",
                      "llama-bench")


def free_ram_gb() -> float:
    """Free physical RAM in GB; -1.0 when the platform cannot report it."""
    if os.name == "nt":
        try:
            import ctypes

            class _MSX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            _msx = _MSX()
            _msx.dwLength = ctypes.sizeof(_MSX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(_msx))
            return _msx.ullAvailPhys / (1024 ** 3)
        except (OSError, AttributeError, ValueError):
            return -1.0
    try:
        return (os.sysconf("SC_AVPHYS_PAGES")
                * os.sysconf("SC_PAGE_SIZE") / (1024 ** 3))
    except (ValueError, OSError, AttributeError):
        return -1.0


def gguf_size_gb(model: str) -> float:
    """GGUF size in GB; 0.0 when unresolvable. Accepts an alias OR a direct
    path - the bench scripts pass paths, the loader passes aliases.
    FAIL-CLOSED (2026-10-30): 0.0 makes ram_guard_blocks BLOCK - an unknown
    model cannot be size-checked, so it must not start."""
    _p = Path(model)
    if _p.suffix.lower() == ".gguf" and _p.exists():
        return _p.stat().st_size / (1024 ** 3)
    try:
        from backend.llama_models import resolve_model_path as _rmp
        _ggf = _rmp(model)
        if _ggf:
            return Path(_ggf).stat().st_size / (1024 ** 3)
    except (ImportError, OSError):
        pass
    return 0.0


def ram_guard_blocks(model: str, free_ram_gb: float | None,
                     headroom_gb: float = _RAM_HEADROOM_GB) -> bool:
    """True when a none load must be rejected: free RAM unknown, model size
    unknown (fail-closed, 2026-10-30), or below GGUF size + headroom.
    RATIONALE (live 2026-10-28): a none load commits the FULL model as
    private memory; three stacked hermes commits (engine slot + two bench
    processes) pushed the commit to 56 GB on a 32 GB machine - SSD at
    100 %, system frozen."""
    if free_ram_gb is None or free_ram_gb < 0:
        return True
    _size = gguf_size_gb(model)
    if _size <= 0.0:
        return True  # unknown model -> cannot verify size -> block
    return free_ram_gb < _size + headroom_gb


def llama_process_count() -> int:
    """Number of running llama-server/llama-bench processes. FAIL-CLOSED
    (2026-10-30): -1 when the query fails (tasklist broken) - the caller
    must treat anything != 0 as 'not free'. errors="replace": tasklist
    prints OEM-codepage headers on a German Windows - text=True alone
    raised UnicodeDecodeError in the reader thread and the count silently
    stayed 0 (2026-10-29, live)."""
    count = 0
    if os.name == "nt":
        try:
            for image in ("llama-server.exe", "llama-bench.exe"):
                out = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {image}"],
                    capture_output=True, text=True, timeout=30,
                    encoding="utf-8", errors="replace")
                count += (out.stdout or "").count(image.split(".")[0])
        except (OSError, subprocess.SubprocessError):
            return -1
    else:
        try:
            out = subprocess.run(["pgrep", "-f", "llama-(server|bench)"],
                                 capture_output=True, text=True, timeout=30)
            count += len([x for x in out.stdout.splitlines() if x.strip()])
        except (OSError, subprocess.SubprocessError):
            return -1
    return count

def kill_process_tree(pid: int, timeout_s: float = 10.0) -> bool:
    """Kill a process and its whole tree; returns True when it ended.

    TIMEOUT-KILL (2026-10-28, freeze lesson): a bench that hangs must not
    outlive its harness - subprocess.run(timeout=...) alone kills only the
    direct child, the grandchild tree keeps running (and holds VRAM)."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=timeout_s,
                           encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            return False
    else:
        try:
            subprocess.run(["pkill", "-TERM", "-P", str(pid)],
                           capture_output=True, timeout=timeout_s)
        except (OSError, subprocess.SubprocessError):
            return False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if subprocess.run(["tasklist" if os.name == "nt" else "ps", "-FI"
                           if os.name == "nt" else "-p", str(pid)]
                          if os.name == "nt" else ["ps", "-p", str(pid)],
                          capture_output=True, text=True).returncode != 0:
            return True
        time.sleep(0.2)
    return False

