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
        self.pinned_at     = 0.0   # PIN EXPIRY (2026-10-02): set on pin, LRU ignores pins older than 30 min
        self._orphan_port: Optional[int] = None
        self._vision       = False
        self._num_ctx: int = 0
        self._n_parallel: int = 1
        self._jinja: bool = False
        self.swa_window: int = 0               # SWA sliding-window size (0 = no SWA)
        # VISION TRUTH (2026-10-04): what /props.modalities.vision actually
        # reported for the running server. True/False = server truth,
        # None = unknown (/props never fetched). Distinct from the REQUESTED
        # self._vision so the duo gate can catch a silently dropped projector.
        self.vision_active: Optional[bool] = None
        # IN-USE GUARD (2026-10-17): count of in-flight rounds that are using
        # this slot RIGHT NOW. The pre-flight eviction refuses to sacrifice a
        # slot with in_flight > 0 even when the pin is already gone: a run
        # unpins its coder when the run tears down, but the last coder POST can
        # still be in flight - live run 17:57:36 killed exactly that slot for
        # another role's load and the round died with ConnectError.
        self.in_flight: int = 0
    def set_pinned(self, val: bool) -> None:
        # PIN EXPIRY (2026-10-02): set/clear the pin and stamp the time so
        # LRU eviction can reclaim pins older than 30 minutes.
        # MONOTONIC (audit A3): the whole slot lifecycle clock is monotonic —
        # an NTP wall-clock jump used to expire pins and mass-evict slots.
        import time as _t
        self.pinned = bool(val)
        self.pinned_at = _t.monotonic() if val else 0.0

    def mark_in_flight(self) -> None:
        """A round starts using this slot (see in_flight in __init__)."""
        self.in_flight = int(getattr(self, "in_flight", 0)) + 1

    def clear_in_flight(self) -> None:
        self.in_flight = max(0, int(getattr(self, "in_flight", 0)) - 1)

    @property
    def evictable(self) -> bool:
        """MAY the pre-flight/LRU path sacrifice this slot?

        False for pinned slots (a run holds them) and for slots with an
        in-flight round - killing those produces a ConnectError mid-round
        instead of a clean "does not fit" refusal."""
        if self.pinned:
            return False
        if int(getattr(self, "in_flight", 0)) > 0:
            return False
        return not getattr(self, "_loading", False)


    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def is_running(self) -> bool:
        if self._orphan_port is not None:
            return True
        return self.process is not None and self.process.poll() is None

    def touch(self):
        # MONOTONIC (audit A3): pairs with the idle monitor's clock
        self.last_used = time.monotonic()

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
                            # status=="loading": keep waiting, 0.5s pause
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
        self.pinned_at      = 0.0
        self._idle_timeout  = None
        self._loading       = False
        self._orphan_port   = None
        self._vision        = False
        self._num_ctx       = 0
        self._n_parallel    = 1
        self._jinja         = False
        # AUDIT M1 (2026-10-24): the AMD commit-wait token must not survive a
        # recycle — a stale equal seq made the wait skip on slots whose next
        # start came from a path that never bumps the sequence.
        self._entry_load_seq = None
        self.swa_window     = 0
        self.vision_active  = None
        # A.2 FIX: Object-swap wake — set() on old Event instance wakes all
        # already-registered waiters; new Event instance prevents new callers
        # from ever hitting a stale event. model=None (line above) ensures
        # _find_loaded never matches this slot anyway — double-safe.
        old = self._ready_event
        old.set()
        self._ready_event = asyncio.Event()
