# -*- coding: utf-8 -*-
"""ModelSlot — a running llama-server process bound to a fixed port.

Extracted from backend/llama_server_manager.py (M3b).
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
    """A running llama-server process on a fixed port."""

    def __init__(self, slot_id: int):
        self.slot_id   = slot_id
        self.port      = BASE_PORT + slot_id
        self.model: Optional[str] = None
        self.process: Optional[subprocess.Popen] = None
        self.loaded_at = 0.0
        self.last_used = 0.0
        self.pinned    = False
        self._idle_timeout: Optional[float] = None
        self._ready_event  = asyncio.Event()
        self._ready_loop: Optional[asyncio.AbstractEventLoop] = None
        self._loading      = False
        self._lifecycle_gen: int = 0
        self._wait_abort_ev: Optional[asyncio.Event] = None
        self._wait_abort_loop: Optional[asyncio.AbstractEventLoop] = None
        self._entry_load_seq: Optional[int] = None
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
        # A9c (audit 2026-11-03): the producer has no finally, so a cancelled
        # round could leave the counter at 1 forever. The timestamp lets the
        # eviction guards age a stale mark out instead of blocking the slot
        # until restart.
        self.in_flight_since: float = 0.0
        # VRAM-HEADROOM (2026-10-27): set when a fresh load left almost no free
        # VRAM. Surfaced by /vram/ps so the UI can say "this will be slow".
        self.last_warning: str = ""
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
        import time as _t
        # P1 (deep audit 2026-11-03): after a leaked mark ages out, the NEXT
        # mark must refresh the stamp - otherwise the counter rises to 2 while
        # the stamp stays old and the slot looks free mid-round.
        if (int(getattr(self, "in_flight", 0)) <= 0
                or not self.in_flight_active()):
            self.in_flight_since = _t.monotonic()
        self.in_flight = int(getattr(self, "in_flight", 0)) + 1

    def clear_in_flight(self) -> None:
        self.in_flight = max(0, int(getattr(self, "in_flight", 0)) - 1)
        if self.in_flight == 0:
            self.in_flight_since = 0.0

    def in_flight_active(self, max_age_s: float = 1800.0) -> bool:
        """True while a round is using this slot; a mark older than
        `max_age_s` is treated as leaked (no finally on the producer)."""
        import time as _t
        if int(getattr(self, "in_flight", 0)) <= 0:
            return False
        _since = float(getattr(self, "in_flight_since", 0.0) or 0.0)
        if _since <= 0.0:
            return True          # older mark without a stamp: stay conservative
        return (_t.monotonic() - _since) < max_age_s

    @property
    def evictable(self) -> bool:
        """MAY the pre-flight/LRU path sacrifice this slot?

        False for pinned slots (a run holds them) and for slots with an
        in-flight round - killing those produces a ConnectError mid-round
        instead of a clean "does not fit" refusal."""
        if self.pinned:
            return False
        if self.in_flight_active():
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

    @staticmethod
    def _wake_event_loop_safe(
        ev: Optional[asyncio.Event],
        fallback_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        """Set an asyncio.Event safely even when invoked off the event-loop thread."""
        if ev is None:
            return
        if not isinstance(ev, asyncio.Event):
            ev.set()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        candidate_loops: list[asyncio.AbstractEventLoop] = []
        try:
            waiters = list(getattr(ev, "_waiters", ()) or ())
        except RuntimeError:
            waiters = []
        for w in waiters:
            w_loop = w.get_loop() if hasattr(w, "get_loop") else None
            if w_loop is not None and w_loop not in candidate_loops:
                candidate_loops.append(w_loop)
        if fallback_loop is not None and fallback_loop not in candidate_loops:
            candidate_loops.append(fallback_loop)

        dispatched = False
        for loop in candidate_loops:
            if loop is running:
                try:
                    ev.set()
                    return
                except RuntimeError:
                    continue
            is_running_fn = getattr(loop, "is_running", None)
            loop_running = is_running_fn() if callable(is_running_fn) else True
            if not loop.is_closed() and loop_running:
                try:
                    loop.call_soon_threadsafe(ev.set)
                    dispatched = True
                except RuntimeError:
                    pass
        if not dispatched:
            try:
                ev.set()
            except RuntimeError:
                pass

    def abort_wait(self) -> None:
        """Bump lifecycle generation and wake any active wait_ready() immediately."""
        self._lifecycle_gen = int(getattr(self, "_lifecycle_gen", 0)) + 1
        ev = getattr(self, "_wait_abort_ev", None)
        if ev is not None:
            loop = getattr(self, "_wait_abort_loop", None)
            self._wake_event_loop_safe(ev, loop)

    async def wait_ready(
        self,
        timeout: float = LLAMA_STARTUP_READY_TIMEOUT_SECONDS,
        http_client: Optional[httpx.AsyncClient] = None,
        *,
        expected_process: Optional[subprocess.Popen] = None,
        lifecycle_gen: Optional[int] = None,
    ) -> bool:
        _entry_gen = (
            lifecycle_gen
            if lifecycle_gen is not None
            else getattr(self, "_lifecycle_gen", None)
        )
        _exp_proc = expected_process
        _had_proc = _exp_proc is not None or self.process is not None
        _abort_ev: Optional[asyncio.Event] = None
        if hasattr(self, "_lifecycle_gen"):
            _abort_ev = asyncio.Event()
            try:
                _loop = asyncio.get_running_loop()
            except RuntimeError:
                _loop = None
            self._wait_abort_loop = _loop
            self._ready_loop = _loop
            self._wait_abort_ev = _abort_ev

        def _aborted() -> bool:
            if _entry_gen is not None and getattr(self, "_lifecycle_gen", _entry_gen) != _entry_gen:
                return True
            if _exp_proc is not None and self.process is not _exp_proc:
                return True
            if _had_proc and self.process is None:
                return True
            if self.process and self.process.poll() is not None:
                return True
            return False

        async def _abortable_sleep(secs: float) -> bool:
            if _abort_ev is None:
                await asyncio.sleep(secs)
                return _aborted()
            if _abort_ev.is_set() or _aborted():
                return True
            try:
                await asyncio.wait_for(_abort_ev.wait(), timeout=secs)
                return True
            except asyncio.TimeoutError:
                return _aborted()

        deadline = time.time() + timeout
        c = http_client
        _owns_client = False
        if c is None:
            # Skip OS CA-store SSLContext init (~0.7-1.3s sync block on Windows)
            # and proxy env lookups for plain loopback http:// health probes.
            c = httpx.AsyncClient(
                timeout=2.0,
                verify=not self.url.startswith("http://"),
                trust_env=False,
            )
            _owns_client = True
        try:
            while time.time() < deadline:
                if _aborted():
                    return False
                try:
                    r = await c.get(f"{self.url}/health")
                    if _aborted():
                        return False
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
                                if await _abortable_sleep(1.0):
                                    return False
                                try:
                                    _r2 = await c.get(f"{self.url}/health")
                                    if _r2.status_code == 200:
                                        _stable_count += 1
                                    else:
                                        _stable_count = 0
                                except Exception:
                                    _stable_count = 0
                            if _stable_count >= _stable_needed and not _aborted():
                                try:
                                    _rm2 = await c.get(f"{self.url}/v1/models")
                                    if _rm2.status_code == 200:
                                        return True
                                except Exception:
                                    pass
                            return False
                except Exception:
                    pass
                if await _abortable_sleep(0.5):
                    return False
            return False
        finally:
            if _abort_ev is not None and getattr(self, "_wait_abort_ev", None) is _abort_ev:
                self._wait_abort_ev = None
                self._wait_abort_loop = None
            if _owns_client:
                try:
                    await c.aclose()
                except Exception:
                    pass

    def kill(self):
        """Terminate any tracked process/orphan port and reset slot state safely."""
        wait_loop = getattr(self, "_wait_abort_loop", None) or getattr(self, "_ready_loop", None)
        try:
            self.abort_wait()
            if self._orphan_port is not None:
                try:
                    _kill_port_sync(self._orphan_port)
                except Exception as e:
                    logger.warning(f"kill(): orphan-port cleanup failed for port {self._orphan_port}: {e}")
            proc = self.process
            if proc and proc.poll() is None:
                if platform.system() == "Windows" and getattr(proc, "pid", None):
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            capture_output=True, timeout=5,
                            creationflags=_WIN_CNF,
                        )
                    except Exception as e:
                        logger.warning(f"kill(): taskkill failed for {self.model} (pid={proc.pid}): {e}")
                # Re-check: taskkill /T on Windows kills process tree incl. this process.
                # If process already dead, skip terminate/kill to avoid ProcessLookupError.
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except (OSError, ValueError, RuntimeError) as e:
                        logger.warning(
                            f"kill(): terminate failed for {self.model} (pid={getattr(proc, 'pid', None)}): {e}"
                        )
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                        except (OSError, ValueError, RuntimeError) as e:
                            logger.warning(
                                f"kill(): process.kill failed for {self.model} (pid={getattr(proc, 'pid', None)}): {e}"
                            )
                        try:
                            proc.wait(timeout=2)
                        except Exception as e:
                            logger.warning(
                                f"kill(): process for '{self.model}' (pid={getattr(proc, 'pid', None)}, slot={self.slot_id}) "
                                f"did not confirm exit after kill() — possible zombie, VRAM may not be released. "
                                f"Slot will be marked free regardless. ({e})"
                            )
                    except (OSError, ValueError, RuntimeError) as e:
                        logger.warning(
                            f"kill(): wait failed for {self.model} (pid={getattr(proc, 'pid', None)}): {e}"
                        )
        finally:
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
            self.in_flight      = 0
            self.in_flight_since = 0.0
            self.last_warning   = ""
            # A.2 FIX: Object-swap wake — set() on old Event instance wakes all
            # already-registered waiters; new Event instance prevents new callers
            # from ever hitting a stale event. model=None (line above) ensures
            # _find_loaded never matches this slot anyway — double-safe.
            old = self._ready_event
            self._ready_event = asyncio.Event()
            self._wake_event_loop_safe(old, wait_loop)
