# -*- coding: utf-8 -*-
"""Reine Helfer/Konstanten aus backend/llama_server_manager.py extrahiert (M3a)."""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
import subprocess
from collections import namedtuple
from pathlib import Path

from .llama_config import CONTEXT_SIZE_DEFAULT
from .llama_models import list_available_models, resolve_model_path, _strip_alias

logger = logging.getLogger("llama_manager")

CanFitResult = namedtuple("CanFitResult", ["ok", "needed_mib", "free_mib", "margin_mib", "source"])

_WIN_CNF = getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform.system() == "Windows" else 0

VRAM_BUDGET_GB: float = 7.5  # Standardbudget: real nutzbar
LLAMA_STARTUP_READY_TIMEOUT_SECONDS: float = 240.0

_OLLAMA_ONLY_BASES: set[str] = {
    "glm-ocr",
    "ministral-3",
}

_MMPROJ_REQUIRED_BASES: set[str] = {
    "granite3.2-vision",
    "granite3-vision",
    "llava",
    "moondream",
    "minicpm-v",
    "bunny",
    "obsidian",
    "qwen3-vl",
}

_VISION_CAPABLE_BASES: set[str] = {
    "qwen3.5",  # Unsloth UD-GGUFs + mmproj-Qwen3.5-{2B/4B/9B}-F16.gguf
    "qwen3.6",
    "hermes3.6",
    "hermes",
}

_VRAM_BASE_OVERHEAD_GB: float = 2.5

_VRAM_PRE_FLIGHT_GRACE_S: float = 40.0

# ── Pre-flight margins / ctx-down policy (2026-09-06) ─────────────────────────
# 768 MiB used to be the only, hardcoded margin. With MoE models using CPU
# expert offloading (e.g. 35b-a3b, n-cpu-moe 35) the base weights dominate
# (~4.66 GB) and context costs almost nothing (40k ctx ≈ +245 MiB). As a
# result @40960 with a 768 margin blocked by ~200 MiB while @256 fits easily —
# the old policy silently degraded to 4096 and the coder ran into a useless
# compress loop. New policy: full margin → reduced margin → ladder (only for
# explicitly small requests / allow_graceful), otherwise a clear error.
VRAM_PRE_FLIGHT_MARGIN_MIB: int = 768
VRAM_PRE_FLIGHT_REDUCED_MARGIN_MIB: int = 256

# Untergrenze fuer stille Degradation: Requests > CTX_DOWN_MIN werden nie
# still unter diesen Wert gefahren (strikte Coder-Requests degradieren gar
# nicht, sie scheitern klar).
CTX_DOWN_MIN: int = 8192
CTX_DOWN_LADDER: tuple[int, ...] = (16384, 12288, 8192)


def resolve_ctx_fit(requested_ctx: int, free_mib: float | None,
                    needs_at, *,
                    margin_mib: int = VRAM_PRE_FLIGHT_MARGIN_MIB,
                    reduced_margin_mib: int = VRAM_PRE_FLIGHT_REDUCED_MARGIN_MIB,
                    min_ctx: int = CTX_DOWN_MIN,
                    ladder: tuple[int, ...] = CTX_DOWN_LADDER,
                    allow_graceful: bool = False) -> tuple[int, int] | None:
    """Pure, testable ctx/margin decision without I/O.

    needs_at(ctx) returns the MiB required for a context (e.g.
    vram_of_moe*1024). free_mib = currently free VRAM (live or formula).

    Reihenfolge:
      1. requested_ctx mit voller Marge  → passt: (requested, margin)
      2. requested_ctx mit reduzierter Marge → passt: (requested, reduced)
      3. allow_graceful=True: ladder rungs below requested_ctx (each first
         full, then reduced margin); 4096 only if requested_ctx itself was
         small (<= min_ctx).
      4. otherwise None → caller decides (clear error instead of silent
         degradation).

    allow_graceful=False (strict coder requests) NEVER returns a smaller
    ctx rung: either full context (full/reduced margin) or None.
    """
    requested_ctx = max(1, int(requested_ctx))

    def _fits(_ctx: int, _margin: int) -> bool:
        if free_mib is None:
            return False
        try:
            _need = float(needs_at(_ctx))
        except Exception:
            return False
        return (_need + float(_margin)) <= float(free_mib)

    if _fits(requested_ctx, margin_mib):
        return (requested_ctx, margin_mib)
    if _fits(requested_ctx, reduced_margin_mib):
        return (requested_ctx, reduced_margin_mib)
    if not allow_graceful:
        return None
    for _cand in ladder:
        if _cand >= requested_ctx:
            continue
        if _fits(_cand, margin_mib):
            return (_cand, margin_mib)
        if _fits(_cand, reduced_margin_mib):
            return (_cand, reduced_margin_mib)
    # Small requests (<= min_ctx) may also fall back to 4096.
    if requested_ctx <= min_ctx:
        for _margin in (margin_mib, reduced_margin_mib):
            if _fits(4096, _margin):
                return (4096, _margin)
    return None

async def _kill_slot_async(slot) -> None:
    """Slot.kill() off-loop ausfuehren.

    slot.kill() laesst blockierendes taskkill/netstat/tasklist laufen (bis zu
    ~10 s). Aufrufe aus async-Methoden muessen diesen Block abkuerzen, sonst
    friert der komplette Event-Loop (alle parallelen Chats) waehrend jeder
    Eviction/Load/Ensure-Load ein.
    """
    if slot is None:
        return
    try:
        await asyncio.to_thread(slot.kill)
    except Exception as _ke:
        logger.warning("_kill_slot_async failed: %s", type(_ke).__name__)

def _parse_proc_meminfo(text: str) -> float:
    """MemAvailable in GB aus /proc/meminfo-Inhalt; -1.0 wenn nicht lesbar."""
    for _line in text.splitlines():
        if _line.startswith("MemAvailable:"):
            try:
                return float(_line.split()[1]) / (1024 * 1024)  # kB -> GB
            except Exception:
                return -1.0
    return -1.0


def _available_ram_gb() -> float:
    """Freier physischer RAM in GB (Linux: /proc/meminfo, Windows: GlobalMemoryStatusEx). -1.0 bei Fehler."""
    try:
        if platform.system() != "Windows":
            with open("/proc/meminfo", "r", encoding="utf-8") as _mi:
                return _parse_proc_meminfo(_mi.read())
    except Exception:
        return -1.0
    try:
        import ctypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_uint32),
                ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        _ms = _MEMORYSTATUSEX()
        _ms.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(_ms)):
            return -1.0
        return _ms.ullAvailPhys / (1024 ** 3)
    except Exception:
        return -1.0

class VRAMPreFlightError(RuntimeError):
    def __init__(self, model: str, num_ctx: int, needed_mib: float, free_mib: float,
                 source: str, external_usage_est_mib: int, fixed_cost_dominant: bool,
                 message: str):
        super().__init__(message)
        self.model = model
        self.num_ctx = num_ctx
        self.needed_mib = needed_mib
        self.free_mib = free_mib
        self.source = source
        self.external_usage_est_mib = external_usage_est_mib
        self.fixed_cost_dominant = fixed_cost_dominant

def _needs_mmproj(model: str, vision: bool = False) -> bool:
    base = model.split(":")[0].lower()
    if base in _MMPROJ_REQUIRED_BASES:
        return True
    if vision and base in _VISION_CAPABLE_BASES:
        return True
    return False

def _gguf_path_to_model_name(gguf_path: str) -> str | None:


    if not gguf_path:
        return None
    from pathlib import Path as _P
    gguf_norm = str(_P(gguf_path).resolve()).lower().replace("\\", "/")

    try:
        for name in list_available_models():
            p = resolve_model_path(name)
            if p and str(_P(p).resolve()).lower().replace("\\", "/") == gguf_norm:
                return name
    except Exception:
        pass

    stem = _P(gguf_path).stem.lower().replace(".", "-").replace("_", "-")
    best: str | None = None
    best_score = 0
    try:
        for name in list_available_models():
            base = name.split(":")[0].lower().replace(".", "-")
            tag  = name.split(":")[1].lower() if ":" in name else ""
            score = (base in stem) + (bool(tag) and tag.replace("b", "") in stem)
            if score > best_score:
                best_score = score
                best = name
    except Exception:
        pass

    return best if best_score > 0 else None

def _probe_binary_build(llama_bin: str) -> int:


    import re as _re

    def _parse_build(text: str) -> int:
        for pattern in [r'build[:\s]+(\d{4,})', r'build_info[:\s]+b(\d{4,})', r'version[:\s]+(\d+)', r'\bb(\d{4,})\b']:
            m = _re.search(pattern, text, _re.IGNORECASE)
            if m:
                return int(m.group(1))
        return 0

    for flag in ["--version", "-v", "--help"]:
        try:
            r = subprocess.run(
                [llama_bin, flag],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
                creationflags=_WIN_CNF,
            )
            text = (r.stdout or "") + (r.stderr or "")
            build = _parse_build(text)
            if build > 0:
                return build
        except Exception:
            continue
    import os as _os2
    _fname = _os2.path.basename(llama_bin)
    _fm = _re.search(r'b(\d{4,})', _fname, _re.IGNORECASE)
    if _fm:
        return int(_fm.group(1))
    return 0

def _probe_kv_flag(llama_bin: str, kv_type: str) -> bool:
    try:
        from .llama_config import LLAMA_BIN as _lb
        import re as _re2
        m = _re2.search(r"b(\d+)", str(_lb.name))
        if m and int(m.group(1)) >= 8278:
            return True
        r = subprocess.run(
            [llama_bin, "--help"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=_WIN_CNF,
        )
        help_text = (r.stdout or "") + (r.stderr or "")
        return "cache-type-k" in help_text.lower() or "cache_type_k" in help_text.lower()
    except Exception:
        return False

def _probe_moe_flag(llama_bin: str) -> bool:
    try:
        import subprocess as _sp
        _WIN_CNF = 0x08000000 if platform.system() == "Windows" else 0
        r = _sp.run(
            [str(llama_bin), "--help"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=_WIN_CNF,
        )
        help_text = (r.stdout or "") + (r.stderr or "")
        return "n-cpu-moe" in help_text.lower()
    except Exception:
        return False

def _probe_device_flag(llama_bin: str) -> bool:
    try:
        from .llama_config import LLAMA_BIN as _lb
        import re as _re2
        m = _re2.search(r"b(\d+)", str(_lb.name))
        if m and int(m.group(1)) >= 8278:
            return True
        r = subprocess.run(
            [llama_bin, "--help"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=_WIN_CNF,
        )
        help_text = (r.stdout or "") + (r.stderr or "")
        return "--device" in help_text.lower()
    except Exception:
        return False

def _probe_backend_devices(llama_bin: str, backend: str) -> bool | None:


    import re as _re_dev
    try:
        r = subprocess.run(
            [llama_bin, "--list-devices"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            creationflags=_WIN_CNF,
        )
        text = (r.stdout or "") + (r.stderr or "")
        if not text.strip():
            return None
        _kw = "cuda" if backend == "cuda" else "vulkan"
        return bool(_re_dev.search(rf"{_kw}\s*\d+", text.lower()))
    except Exception:
        return None

def _probe_backend_dlls(llama_bin: str, backend: str) -> bool | None:


    if backend == "cpu":
        return True  # CPU-BACKEND: no backend runtime libs needed
    import glob as _glob_dll
    exe = Path(llama_bin)
    dll_dir = exe.parent
    _posix = platform.system() != "Windows"
    _so = ".so" if _posix else ".dll"
    try:
        if backend == "cuda":
            if not (dll_dir / f"ggml-cuda{_so}").exists():
                return False
            if not _posix:
                for _base in ("cudart64", "cublas64", "cublasLt64"):
                    if not _glob_dll.glob(str(dll_dir / f"{_base}*.dll")):
                        return False
        else:
            if not (dll_dir / f"ggml-vulkan{_so}").exists():
                return False
        return True
    except Exception:
        return None

def _prefetch_key(model: str, num_ctx: int) -> tuple[str, int]:
    return (_strip_alias(model), int(num_ctx or CONTEXT_SIZE_DEFAULT))

def _tcp_alive(port: int, timeout: float = 0.4) -> bool:
    """Blocking TCP check — call only via run_in_executor."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False

def _inodes_for_port(proc_net: str, port: int) -> set:
    """Listening-socket inodes for port from /proc/net/tcp{,6} content (pure)."""
    _inodes: set = set()
    _want = f"{port:04X}"
    for _line in proc_net.splitlines()[1:]:
        _parts = _line.split()
        if len(_parts) < 10:
            continue
        if _parts[3] != "0A":  # TCP_LISTEN
            continue
        if _parts[1].split(":")[-1].upper() != _want:
            continue
        _inodes.add(_parts[9])
    return _inodes


def _kill_port_via_proc(port: int) -> bool:
    """Port -> PID via /proc/net/tcp + /proc/<pid>/fd, kill after cmdline check."""
    import glob as _g
    import re as _re_p
    _inodes: set = set()
    for _pf in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(_pf, "r", encoding="utf-8", errors="replace") as _f:
                _inodes |= _inodes_for_port(_f.read(), port)
        except Exception:
            continue
    if not _inodes:
        return False
    _killed = False
    for _fd_path in _g.glob("/proc/[0-9]*/fd/*"):
        try:
            _target = os.readlink(_fd_path)
        except Exception:
            continue
        _m = _re_p.match(r"socket:\[(\d+)\]", _target)
        if not _m or _m.group(1) not in _inodes:
            continue
        _pid = int(_fd_path.split("/")[2])
        try:
            with open(f"/proc/{_pid}/cmdline", "rb") as _cf:
                _cmd = _cf.read().decode("utf-8", "replace")
        except Exception:
            continue
        if "llama-server" not in _cmd:
            logger.warning(f"Port-Cleanup: PID {_pid} on port {port} is not a llama-server - skip")
            continue
        try:
            os.kill(_pid, 9)
            _killed = True
            logger.info(f"Port-Cleanup: PID {_pid} on port {port} killed (proc-scan)")
        except Exception:
            pass
    return _killed


def _kill_port_sync(port: int):


    if platform.system() != "Windows":
        # POSIX chain (2026-09-08): fuser (psmisc) -> /proc/net/tcp inode scan
        # (always available) -> pkill as the last coarse filter.
        _killed = False
        try:
            _r = subprocess.run(["fuser", "-k", f"{port}/tcp"],
                                capture_output=True, timeout=5)
            _killed = _r.returncode == 0
        except Exception:
            _killed = False
        if not _killed:
            try:
                _killed = _kill_port_via_proc(port)
            except Exception:
                _killed = False
        if not _killed:
            try:
                subprocess.run(["pkill", "-9", "-f", "llama-server"],
                               capture_output=True, timeout=5)
            except Exception:
                pass
        return
    try:
        r = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=_WIN_CNF,
        )
        pids = set()
        for line in r.stdout.splitlines():
            # Windows-Format: Proto LocalAddress ForeignAddress State PID
            parts = line.split()
            if len(parts) < 5:
                continue
            if parts[0].upper() != "TCP":
                continue
            local_addr = parts[1]
            state = parts[3].upper()
            if state != "LISTENING":
                continue
            if not local_addr.endswith(f":{port}"):
                continue
            try:
                pids.add(int(parts[-1]))
            except ValueError:
                continue
        for pid in pids:
            if pid <= 4:
                continue
            try:
                tr = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=5,
                    encoding="utf-8", errors="replace",
                    creationflags=_WIN_CNF,
                )
                tline = (tr.stdout or "").strip().lower()
                if "llama-server" not in tline and "llama_server" not in tline:
                    logger.warning(f"Port-Cleanup: PID {pid} on port {port} is not a llama-server - skip")
                    continue
            except Exception:
                logger.warning(f"Port-Cleanup: PID {pid} on port {port} could not be verified - skip")
                continue
            try:
                # Identisch zum /T beim eigenen-process kill (siehe kill()).
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=5,
                    creationflags=_WIN_CNF,
                )
                logger.info(f"Port-Cleanup: PID {pid} on port {port} killed (incl. process tree)")
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"Port-Cleanup failed for port {port}: {e}")

def _nm(m):
    import re
    # Strip :latest AND #N aliases — canonical model name for slot matching.
    # #N is a pre-explore auto-alias decorator that must not prevent reusing
    # an already-loaded slot for the same base model.
    return re.sub(r"#\d+$", "", (m or "").replace(":latest", "")).strip()
