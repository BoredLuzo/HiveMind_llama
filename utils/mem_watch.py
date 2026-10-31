"""Commit-charge memory snapshots for the [MEM] log line (2026-10-31).

The user saw "Zugesichert" (commit charge) grow across model loads/unloads
and stay high until a restart. This module makes that visible in the engine
log: one cheap line per event (model load, eviction, run end) with

  [MEM] load:groot=... commit=18.3/50.9GB engine=95MB llama[8101]=3.7GB
        tts=2.0GB lsp[pyright]=385MB

System commit comes from GlobalMemoryStatusEx; per-process PrivateUsage
(commit) and WorkingSet come from GetProcessMemoryInfo with
PROCESS_QUERY_LIMITED_INFORMATION (no admin needed). All child PIDs come
from HiveMind's own state - the llama manager's slots, the TTS sidecar and
the LSP managers - so nothing scans the process table.

Every failure path is silent (a missing PID, a closed handle, a non-Windows
host); the memory line must never break a load or a run.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("hivemind.mem")

_IS_WINDOWS = os.name == "nt"
_QUERY_LIMITED = 0x1000


def _settings_enabled() -> bool:
    try:
        from core.state import settings as _live
        raw = (_live or {}).get("mem_watch_enabled", True)
    except (ImportError, AttributeError):
        raw = True
    if isinstance(raw, str):
        return raw.strip().lower() not in ("0", "false", "off", "no")
    return bool(raw)


def system_commit_gb() -> tuple[float, float] | None:
    """(used, limit) commit charge in GB, or None when unavailable."""
    if not _IS_WINDOWS:
        return None
    try:
        import ctypes

        class _MEMORYSTATUSEX(ctypes.Structure):
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

        ms = _MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return None
        gb = 1024 ** 3
        used = (ms.ullTotalPageFile - ms.ullAvailPageFile) / gb
        return round(used, 1), round(ms.ullTotalPageFile / gb, 1)
    except Exception:
        return None


def process_commit_mb(pid: int) -> tuple[float, float] | None:
    """(private commit, working set) in MB for one PID, or None."""
    if not pid or not _IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi

        class _COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        pmc = _COUNTERS()
        pmc.cb = ctypes.sizeof(_COUNTERS)
        handle = kernel32.OpenProcess(_QUERY_LIMITED, False, int(pid))
        if not handle:
            return None
        try:
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return None
            mb = 1024 * 1024
            return (round(pmc.PrivateUsage / mb, 1),
                    round(pmc.WorkingSetSize / mb, 1))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _llama_children() -> list[tuple[str, int]]:
    """(label, pid) for every llama-server this manager started."""
    out: list[tuple[str, int]] = []
    try:
        from backend.llama_server_manager import manager as _mgr
        for slot in getattr(_mgr, "_slots", None) or []:
            proc = getattr(slot, "process", None)
            model = getattr(slot, "model", "") or f"slot{getattr(slot, 'slot_id', '?')}"
            if proc is not None and getattr(proc, "pid", None):
                port = getattr(slot, "port", 0)
                out.append((f"llama[{model}@{port}]" if port
                            else f"llama[{model}]", int(proc.pid)))
    except Exception:
        pass
    return out


def _tts_child() -> list[tuple[str, int]]:
    try:
        from core.tts_engine import _state
        proc = (_state or {}).get("proc")
        if proc is not None and getattr(proc, "pid", None):
            return [("tts", int(proc.pid))]
    except Exception:
        pass
    return []


def _lsp_children() -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    try:
        from tools.handlers.lsp import _MANAGERS
        for mgr in list((_MANAGERS or {}).values()):
            conns = getattr(mgr, "_conns", None) or {}
            for lang, conn in conns.items():
                proc = getattr(conn, "_proc", None)
                if proc is not None and getattr(proc, "pid", None):
                    out.append((f"lsp[{lang}]", int(proc.pid)))
    except Exception:
        pass
    return out


def snapshot_parts(label: str) -> str | None:
    """The [MEM] payload for one event, or None when nothing was collectable."""
    sys_c = system_commit_gb()
    parts: list[str] = []
    if sys_c:
        parts.append(f"commit={sys_c[0]}/{sys_c[1]}GB")
    own = process_commit_mb(os.getpid())
    if own:
        parts.append(f"engine={own[0]}MB(ws {own[1]}MB)")
    seen_pids: set[int] = set()
    for name, pid in _llama_children() + _tts_child() + _lsp_children():
        if pid in seen_pids:
            continue
        seen_pids.add(pid)
        mem = process_commit_mb(pid)
        if mem:
            parts.append(f"{name}={mem[0]}MB(ws {mem[1]}MB)")
    if len(parts) <= 1 and not sys_c:
        return None
    return f"{label} " + " ".join(parts)


def log_snapshot(label: str) -> None:
    """Emit the [MEM] line. Never raises; respects mem_watch_enabled."""
    if not _settings_enabled():
        return
    try:
        payload = snapshot_parts(str(label or ""))
        if payload:
            logger.info("[MEM] %s", payload)
    except Exception:
        pass


def settings_path_hint() -> str:
    """Where the log file lands (for docs/tests), repo-relative."""
    return str(Path("logs") / "hivemind.log")
