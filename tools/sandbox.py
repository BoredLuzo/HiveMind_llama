# -*- coding: utf-8 -*-


from __future__ import annotations

import asyncio
import ctypes
import functools
import os
import signal
import subprocess
import sys
from ctypes import wintypes as _wt

_IS_WIN = sys.platform == "win32"

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_TERMINATE_EXIT_CODE = 1

# Defaults — overridable via settings (duo_tool_sandbox_max_mem_mb /
# duo_tool_sandbox_max_procs). 0 = no limit.
_DEFAULT_MAX_MEM_MB = 4096
_DEFAULT_MAX_PROCS = 64


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BASIC_LIMIT_INFO(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", _wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", _wt.DWORD),
        ("Affinity", ctypes.POINTER(_wt.ULONG)),
        ("PriorityClass", _wt.DWORD),
        ("SchedulingClass", _wt.DWORD),
    ]


class _EXT_LIMIT_INFO(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMIT_INFO),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def enabled() -> bool:
    """Setting-driven (duo_tool_sandbox, default True); Windows job objects only.

    POSIX has no job objects: there the sandbox is the process group from
    spawn_kwargs() plus the optional rlimits from _posix_preexec()."""
    if not _IS_WIN:
        return False
    try:
        from settings import load_settings
        return bool(load_settings().get("duo_tool_sandbox", True))
    except Exception:
        return True


def _int_setting(key: str, default: int, _settings_map: dict | None = None) -> int:
    try:
        if _settings_map is None:
            from settings import load_settings
            _settings_map = load_settings()
        val = (_settings_map or {}).get(key, default)
        if val is None or val == "":
            return default
        return int(val)
    except (ImportError, OSError, TypeError, ValueError):
        return default


def _posix_preexec():
    """Optional per-child resource limits on Linux/macOS (opt-in, default off).

    WHY: Windows has job objects (memory + process count per tool, see
    ToolJob.confine). POSIX has no job objects, so before this the only limits on
    a tool child were the systemd unit's MemoryMax/TasksMax for the WHOLE service
    - one runaway run_python could take the server down with it.

    rlimits are the closest equivalent that needs no cgroups and no root:
    RLIMIT_AS (address space, the memory cap), RLIMIT_NPROC (process count for
    this uid) and RLIMIT_CPU (CPU seconds). Note that settings_defaults ships
    4096 MB / 64 procs for Windows Job Objects; applying those to POSIX UID-wide
    RLIMIT_NPROC or V8/Go virtual address reservations breaks normal runs unless
    explicitly opted in via `duo_tool_sandbox_posix_rlimits`. `RLIMIT_CPU` stays
    independently configurable via `duo_tool_sandbox_max_cpu_s` (default 0).

    preexec_fn runs in the child between fork and exec. Python documents that as
    unsafe in multi-threaded programs, so _apply() must remain minimal, perform
    zero imports, and never acquire Python logging locks.
    """
    try:
        from settings import load_settings as _ls
        _s = _ls()
    except (ImportError, OSError, ValueError, TypeError):
        return None
    if not bool((_s or {}).get("duo_tool_sandbox", True)):
        return None
    _posix_rlimits = bool((_s or {}).get("duo_tool_sandbox_posix_rlimits", False))
    _mem_mb = _int_setting("duo_tool_sandbox_max_mem_mb", 0, _s) if _posix_rlimits else 0
    _procs = _int_setting("duo_tool_sandbox_max_procs", 0, _s) if _posix_rlimits else 0
    _cpu_s = _int_setting("duo_tool_sandbox_max_cpu_s", 0, _s)
    if not (_mem_mb > 0 or _procs > 0 or _cpu_s > 0):
        return None
    try:
        import resource as _r
    except ImportError:
        return None
    _setrlimit = getattr(_r, "setrlimit", None)
    _getrlimit = getattr(_r, "getrlimit", None)
    if _setrlimit is None:
        return None
    _rlim_inf = getattr(_r, "RLIM_INFINITY", -1)
    _want: list[tuple[int, int]] = []
    if _mem_mb > 0:
        _rlim_as = getattr(_r, "RLIMIT_AS", None)
        if _rlim_as is not None:
            _want.append((_rlim_as, _mem_mb * 1024 * 1024))
    if _procs > 0:
        _rlim_nproc = getattr(_r, "RLIMIT_NPROC", None)
        if _rlim_nproc is not None:
            _want.append((_rlim_nproc, _procs))
    if _cpu_s > 0:
        _rlim_cpu = getattr(_r, "RLIMIT_CPU", None)
        if _rlim_cpu is not None:
            _want.append((_rlim_cpu, _cpu_s))
    if not _want:
        return None

    def _apply() -> None:            # runs in the CHILD — no imports, no logging
        for _res, _val in _want:
            try:
                _soft = _val
                _hard = _val
                if callable(_getrlimit):
                    _cur = _getrlimit(_res)
                    if isinstance(_cur, tuple) and len(_cur) == 2:
                        _, _cur_hard = _cur
                        if _cur_hard != _rlim_inf:
                            _hard = min(_val, int(_cur_hard))
                            _soft = _hard
                _setrlimit(_res, (_soft, _hard))
            except (OSError, ValueError):
                continue
    return _apply


def spawn_kwargs() -> dict:
    """Extra kwargs for the child process.

    Windows: nothing (the job object is applied right after the spawn).
    POSIX:   own session/process group (so kill_tree can killpg), plus the
             optional rlimits from _posix_preexec().
    """
    if _IS_WIN:
        return {}
    _kw: dict = {"start_new_session": True}
    _pe = _posix_preexec()
    if _pe is not None:
        _kw["preexec_fn"] = _pe
    return _kw


def kill_tree(proc, taskkill_timeout: int = 5) -> None:
    """Kills the entire process tree. No-op-safe.

    Windows: the job object usually takes over; taskkill /T as fallback —
    and proc.kill() when taskkill itself fails (audit 2026-10-24: callers
    without a job object — bare shell spawns like the test runner — relied
    on that fallback; without it a surviving wrapper hangs every wait()).
    Non-Windows: killpg auf die Prozessgruppe des Kinds (start_new_session).
    """
    pid = getattr(proc, "pid", None)
    if not pid:
        return
    if _IS_WIN:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=taskkill_timeout,
                creationflags=0x08000000,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return
    # POSIX-only attrs (absent from the win32 stdlib surface) - getattr with
    # a fallback keeps the non-Windows branch platform-honest; on Linux the
    # attribute is always present, so behavior is unchanged there.
    _killpg = getattr(os, "killpg", None)
    _getpgid = getattr(os, "getpgid", None)
    _getpgrp = getattr(os, "getpgrp", None)
    _sigkill = getattr(signal, "SIGKILL", None)
    if _killpg is not None and _getpgid is not None and _sigkill is not None:
        try:
            _pgid = _getpgid(pid)
            _own_pgrp = _getpgrp() if _getpgrp is not None else -1
            if _pgid != _own_pgrp:
                _killpg(_pgid, _sigkill)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


async def kill_tree_and_reap(proc, taskkill_timeout: int = 5) -> None:
    """kill_tree + reap, for subprocesses killed on a timeout: the shell
    wrapper is dead but must be waited on, or the event loop's child
    watcher keeps it as a zombie. THE shared timeout-kill path (test
    runner, sandbox) — do not hand-roll copies."""
    await asyncio.to_thread(kill_tree, proc, taskkill_timeout=taskkill_timeout)
    try:
        await proc.wait()
    except Exception:
        pass


@functools.lru_cache(maxsize=1)
def _k32():
    _win_dll = getattr(ctypes, "WinDLL")
    k = _win_dll("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = _wt.HANDLE
    k.CreateJobObjectW.argtypes = [_wt.LPCVOID, _wt.LPCWSTR]
    k.SetInformationJobObject.restype = _wt.BOOL
    k.SetInformationJobObject.argtypes = [_wt.HANDLE, _wt.INT, _wt.LPVOID, _wt.DWORD]
    k.AssignProcessToJobObject.restype = _wt.BOOL
    k.AssignProcessToJobObject.argtypes = [_wt.HANDLE, _wt.HANDLE]
    k.TerminateJobObject.restype = _wt.BOOL
    k.TerminateJobObject.argtypes = [_wt.HANDLE, _wt.UINT]
    k.CloseHandle.restype = _wt.BOOL
    k.CloseHandle.argtypes = [_wt.HANDLE]
    return k


def _proc_handle(proc) -> int | None:
    """Extract a Windows process handle from sync Popen OR asyncio Process."""
    h = getattr(proc, "_handle", None)
    if h is not None:
        try:
            return int(h)
        except Exception:
            pass
    transport = getattr(proc, "_transport", None)
    for getter in (
        lambda: getattr(transport, "get_proc", lambda: None)(),
        lambda: getattr(transport, "_proc", None),
    ):
        try:
            sp = getter()
            h2 = getattr(sp, "_handle", None)
            if h2 is not None:
                return int(h2)
        except Exception:
            continue
    return None


class ToolJob:
    """Confined job around a child process. All methods are no-op-safe."""

    __slots__ = ("handle",)

    def __init__(self, handle: int):
        self.handle = handle

    @classmethod
    def confine(cls, proc) -> "ToolJob | None":


        if not enabled():
            return None
        ph = _proc_handle(proc)
        if ph is None:
            return None
        try:
            k = _k32()
            hjob = k.CreateJobObjectW(None, None)
            if not hjob:
                return None
            info = _EXT_LIMIT_INFO()
            info.BasicLimitInformation.LimitFlags |= _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _mem_mb = _int_setting("duo_tool_sandbox_max_mem_mb", _DEFAULT_MAX_MEM_MB)
            if _mem_mb > 0:
                info.JobMemoryLimit = int(_mem_mb) * 1024 * 1024
                info.BasicLimitInformation.LimitFlags |= _JOB_OBJECT_LIMIT_JOB_MEMORY
            _max_procs = _int_setting("duo_tool_sandbox_max_procs", _DEFAULT_MAX_PROCS)
            if _max_procs > 0:
                info.BasicLimitInformation.ActiveProcessLimit = int(_max_procs)
                info.BasicLimitInformation.LimitFlags |= _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            if not k.SetInformationJobObject(
                    hjob, _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                    ctypes.byref(info), ctypes.sizeof(info)):
                k.CloseHandle(hjob)
                return None
            if not k.AssignProcessToJobObject(hjob, ph):
                k.CloseHandle(hjob)
                return None
            return cls(int(hjob))
        except Exception:
            return None

    def terminate(self) -> bool:
        if self.handle is None:
            return False
        try:
            k = _k32()
            ok = bool(k.TerminateJobObject(self.handle, _TERMINATE_EXIT_CODE))
            return ok
        except Exception:
            return False

    def close(self) -> None:
        if self.handle is None:
            return
        try:
            _k32().CloseHandle(self.handle)
        except Exception:
            pass
        self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
