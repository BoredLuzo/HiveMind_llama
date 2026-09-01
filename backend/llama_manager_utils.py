# -*- coding: utf-8 -*-
"""Reine Helfer/Konstanten aus backend/llama_server_manager.py extrahiert (M3a)."""
from __future__ import annotations

import asyncio
import logging
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
    # Tiel-Coder (2026-08-31): erbt Ornith-1.5 Vision-Tower, mmproj-BF16.gguf.
    "tiel-coder",
}

_VRAM_BASE_OVERHEAD_GB: float = 2.5

_VRAM_PRE_FLIGHT_GRACE_S: float = 40.0

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

def _available_ram_gb() -> float:
    """Freier physischer RAM in GB (Windows GlobalMemoryStatusEx). -1.0 bei Fehler."""
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


    import glob as _glob_dll
    exe = Path(llama_bin)
    dll_dir = exe.parent
    try:
        if backend == "cuda":
            if not (dll_dir / "ggml-cuda.dll").exists():
                return False
            for _base in ("cudart64", "cublas64", "cublasLt64"):
                if not _glob_dll.glob(str(dll_dir / f"{_base}*.dll")):
                    return False
        else:
            if not (dll_dir / "ggml-vulkan.dll").exists():
                return False
        return True
    except Exception:
        return None

def _prefetch_key(model: str, num_ctx: int) -> tuple[str, int]:
    return (_strip_alias(model), int(num_ctx or CONTEXT_SIZE_DEFAULT))

def _tcp_alive(port: int, timeout: float = 0.4) -> bool:
    """Blocking TCP-Check — nur via run_in_executor aufrufen."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False

def _kill_port_sync(port: int):


    if platform.system() != "Windows":
        try:
            subprocess.run(["fuser", "-k", f"{port}/tcp"],
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
