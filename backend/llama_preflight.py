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
import logging
import os
import subprocess
import time
from pathlib import Path

# LOG-CHANNEL (2026-10-30, audit): the loader logs guards under the
# "[PRE-FLIGHT]" tag (manager_load) — this module must use the same tag so
# one grep finds the whole guard trail; plain print() bypassed log routing.
logger = logging.getLogger("hivemind.preflight")

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
    # RETRY (audit 2026-10-30): a single transient tasklist timeout used to
    # block a legitimate load hard ("unknown" -> block). One retry after a
    # short pause separates a hiccup from a broken tool.
    for attempt in (1, 2):
        count = 0
        try:
            if os.name == "nt":
                for image in ("llama-server.exe", "llama-bench.exe"):
                    out = subprocess.run(
                        ["tasklist", "/FI", f"IMAGENAME eq {image}"],
                        capture_output=True, text=True, timeout=30,
                        encoding="utf-8", errors="replace")
                    count += (out.stdout or "").count(image.split(".")[0])
            else:
                out = subprocess.run(["pgrep", "-f", "llama-(server|bench)"],
                                     capture_output=True, text=True, timeout=30)
                count += len([x for x in out.stdout.splitlines() if x.strip()])
            return count
        except (OSError, subprocess.SubprocessError) as _pc_err:
            if attempt == 1:
                time.sleep(0.3)
                continue
            logger.warning(f"[PRE-FLIGHT] process query failed twice ({_pc_err}) - "
                           f"treating as UNKNOWN and blocking (fail-closed)")
            return -1
    return -1

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
        # Linux audit 2026-11-03: pkill -P reaches only direct children; the
        # root was never signalled, so the verify loop saw it alive forever.
        try:
            import signal as _sig
            os.kill(pid, _sig.SIGTERM)
        except (OSError, AttributeError):
            pass
        try:
            subprocess.run(["pkill", "-TERM", "-P", str(pid)],
                           capture_output=True, timeout=timeout_s)
        except (OSError, subprocess.SubprocessError):
            return False
    # VERIFY-FIX (2026-10-30, audit): the wait loop ran the invalid form
    # "tasklist -FI <pid>" — tasklist exits 1 on bad arguments exactly like
    # on "no such process", so the check returned True immediately while the
    # tree was still alive. The valid form is "tasklist /FI "PID eq <pid>""
    # and, because tasklist exits 0 even for "no match", the process counts
    # as gone only when the query exits non-zero OR the pid is missing from
    # its output.
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if os.name == "nt":
                out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                     capture_output=True, text=True, timeout=10,
                                     encoding="utf-8", errors="replace")
            else:
                out = subprocess.run(["ps", "-p", str(pid)],
                                     capture_output=True, text=True, timeout=10)
            if out.returncode != 0 or str(pid) not in (out.stdout or ""):
                return True
        except (OSError, subprocess.SubprocessError):
            return True  # query itself broken -> taskkill above was best effort
        time.sleep(0.2)
    if os.name != "nt":
        # escalation: the root ignored SIGTERM; SIGKILL it once.
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        time.sleep(0.3)
        try:
            out = subprocess.run(["ps", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5)
            if out.returncode != 0 or str(pid) not in (out.stdout or ""):
                return True
        except (OSError, subprocess.SubprocessError):
            return True
    return False

