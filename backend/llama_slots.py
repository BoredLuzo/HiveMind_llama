# -*- coding: utf-8 -*-
"""ModelSlot — ein laufender llama-server-Prozess auf einem festen Port.

Aus backend/llama_server_manager.py extrahiert (M3b).
"""
from __future__ import annotations

import asyncio
import logging
import platform
import subprocess
import time
from typing import Optional

import httpx

from .llama_config import BASE_PORT
from .llama_manager_utils import (
    LLAMA_STARTUP_READY_TIMEOUT_SECONDS,
    _WIN_CNF,
    _kill_port_sync,
)

logger = logging.getLogger("llama_manager")

class ModelSlot:
    """Ein laufender llama-server-Prozess auf einem festen Port."""

    def __init__(self, slot_id: int):
        self.slot_id   = slot_id
        self.port      = BASE_PORT + slot_id
        self.model     = None
        self.process: Optional[subprocess.Popen] = None
        self.loaded_at = 0.0
        self.last_used = 0.0
        self.pinned    = False
        self._idle_timeout: Optional[float] = None
        self._ready_event  = asyncio.Event()
        self._loading      = False
        self._orphan_port: Optional[int] = None
        self._vision       = False
        self._num_ctx: int = 0
        self._n_parallel: int = 1
        self._jinja: bool = False
        self.swa_window: int = 0               # SWA sliding-window size (0 = no SWA)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def is_running(self) -> bool:
        if self._orphan_port is not None:
            return True
        return self.process is not None and self.process.poll() is None

    def touch(self):
        self.last_used = time.time()

    async def wait_ready(self, timeout: float = LLAMA_STARTUP_READY_TIMEOUT_SECONDS,
                         http_client: Optional[httpx.AsyncClient] = None) -> bool:


        deadline = time.time() + timeout
        c = http_client
        _owns_client = False
        if c is None:
            c = httpx.AsyncClient(timeout=2.0)
            _owns_client = True
        try:
            while time.time() < deadline:
                if self.process and self.process.poll() is not None:
                    return False
                try:
                    r = await c.get(f"{self.url}/health")
                    if r.status_code == 200:
                        try:
                            body = r.json()
                            if body.get("status") == "ok":
                                _server_hint = str(body.get("server", "")).lower()
                                if "mcp" in _server_hint:
                                    return False
                                try:
                                    _rm = await c.get(f"{self.url}/v1/models")
                                    if _rm.status_code == 200:
                                        return True
                                except Exception:
                                    pass
                            # status=="loading": weiter warten, 0.5s Pause
                        except Exception:
                            _stable_count = 1
                            _stable_needed = 3
                            while _stable_count < _stable_needed and time.time() < deadline:
                                await asyncio.sleep(1.0)
                                try:
                                    _r2 = await c.get(f"{self.url}/health")
                                    if _r2.status_code == 200:
                                        _stable_count += 1
                                    else:
                                        _stable_count = 0
                                except Exception:
                                    _stable_count = 0
                            if _stable_count >= _stable_needed:
                                try:
                                    _rm2 = await c.get(f"{self.url}/v1/models")
                                    if _rm2.status_code == 200:
                                        return True
                                except Exception:
                                    pass
                            return False
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            return False
        finally:
            if _owns_client:
                try:
                    await c.aclose()
                except Exception:
                    pass

    def kill(self):
        if self._orphan_port is not None:
            try:
                _kill_port_sync(self._orphan_port)
            except Exception as e:
                logger.warning(f"kill(): orphan-port cleanup failed for port {self._orphan_port}: {e}")
        if self.process and self.process.poll() is None:
            if platform.system() == "Windows" and self.process.pid:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                        capture_output=True, timeout=5,
                        creationflags=_WIN_CNF,
                    )
                except Exception as e:
                    logger.warning(f"kill(): taskkill failed for {self.model} (pid={self.process.pid}): {e}")  # Fallback: normales terminate unten
            # Re-check: taskkill /T on Windows kills process tree incl. this process.
            # If process already dead, skip terminate/kill to avoid ProcessLookupError.
            if self.process.poll() is not None:
                pass  # already dead — skip down to cleanup
            else:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    try:
                        self.process.wait(timeout=2)
                    except Exception as e:
                        logger.warning(f"kill(): process for '{self.model}' (pid={self.process.pid}, slot={self.slot_id}) did not confirm exit after kill() — possible zombie, VRAM may not be released. Slot will be marked free regardless. ({e})")
        self.process        = None
        self.model          = None
        self.pinned         = False
        self._idle_timeout  = None
        self._loading       = False
        self._orphan_port   = None
        self._vision        = False
        self._num_ctx       = 0
        self._n_parallel    = 1
        self._jinja         = False
        self.swa_window     = 0
        # A.2 FIX: Object-swap wake — set() on old Event instance wakes all
        # already-registered waiters; new Event instance prevents new callers
        # from ever hitting a stale event. model=None (line above) ensures
        # _find_loaded never matches this slot anyway — double-safe.
        old = self._ready_event
        old.set()
        self._ready_event = asyncio.Event()
