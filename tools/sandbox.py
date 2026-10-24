# -*- coding: utf-8 -*-


from __future__ import annotations

import ctypes
import logging
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


def _int_setting(key: str, default: int) -> int:
    try:
        from settings import load_settings
        return int(load_settings().get(key, default) or default)
    except Exception:
        return default


def _posix_preexec():
    """Optional per-child resource limits on Linux/macOS (opt-in, default off).

    WHY: Windows has job objects (memory + process count per tool, see
    ToolJob.confine). POSIX has no job objects, so before this the only limits on
    a tool child were the systemd unit's MemoryMax/TasksMax for the WHOLE service
    - one runaway run_python could take the server down with it.

    rlimits are the closest equivalent that needs no cgroups and no root:
    RLIMIT_AS (address space, the memory cap), RLIMIT_NPROC (process count for
    this uid) and RLIMIT_CPU (CPU seconds). They use the SAME settings keys as the
    Windows job object, so one value works on both platforms, and every limit is
    0 = unset (default behaviour unchanged).

    preexec_fn runs in the child between fork and exec. Python documents that as
    unsafe in threaded programs; this process IS threaded (uvicorn), so the
    function stays tiny: it only calls resource.setrlimit and admits a failure to
    the log instead of raising. Worst case: a limit is not applied.
    """
    try:
        from settings import load_settings as _ls
        _s = _ls()
    except (ImportError, OSError, ValueError, TypeError):
        return None
    _mem_mb = _int_setting("duo_tool_sandbox_max_mem_mb", 0)
    _procs = _int_setting("duo_tool_sandbox_max_procs", 0)
    _cpu_s = _int_setting("duo_tool_sandbox_max_cpu_s", 0)
    if not (_mem_mb or _procs or _cpu_s):
        return None
    _lg = logging.getLogger(__name__)

    def _apply() -> None:            # runs in the CHILD
        try:
            import resource as _r
        except ImportError:
            return
        _want: list = []
        if _mem_mb > 0:
            _want.append((_r.RLIMIT_AS, _mem_mb * 1024 * 1024))
        if _procs > 0:
            _want.append((_r.RLIMIT_NPROC, _procs))
        if _cpu_s > 0:
            _want.append((_r.RLIMIT_CPU, _cpu_s))
        for _res, _val in _want:
            try:
                _r.setrlimit(_res, (_val, _val))
            except (OSError, ValueError) as _exc:
                # the child continues without that limit; the parent never sees
                # this directly, so it goes to the log for post-mortems
                _lg.debug("[SANDBOX] rlimit %s=%s not applied: %s",
                          _res, _val, _exc)
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
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
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
    kill_tree(proc, taskkill_timeout=taskkill_timeout)
    try:
        await proc.wait()
    except Exception:
        pass


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = _wt.HANDLE
    k.CreateJobObjectW.argtypes = [_wt.LPCVOID, _wt.LPCWSTR]
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
