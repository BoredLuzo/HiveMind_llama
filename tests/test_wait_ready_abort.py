# -*- coding: utf-8 -*-
"""Regression suite for wait_ready() abort, semaphore release, and slot ownership.

Covers the 2026-11-16 live deadlock where /vram/kill_all cleared slot.process
to None mid-load:
  1. wait_ready() ignored process=None and spun the full 240s holding
     _vulkan_init_sem while blocking queued loads.
  2. When wait_ready() finally returned exit=-1 after 240s, ensure_loaded()
     triggered --n-gpu-layers 0 CPU fallback and called _kill_slot_async(slot)
     on a slot already claimed by another task.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.llama_manager_utils import SlotAbortedError  # noqa: E402
from backend.llama_slots import ModelSlot  # noqa: E402
import backend.llama_start_phases as LSP  # noqa: E402
import backend.manager_health as MH  # noqa: E402
import backend.manager_load as ML  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


class _Handler(BaseHTTPRequestHandler):
    mode = "loading_503"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            if _Handler.mode == "plain_ok":
                body = b"OK"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = b'{"status":"loading"}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


class _FakeMgr(ML.LlamaLoadMixin, MH.LlamaHealthMixin):
    _binary_build_number = 12001

    def __init__(self, slot: ModelSlot):
        self._lock = asyncio.Lock()
        self._vulkan_init_sem = asyncio.Semaphore(1)
        self._slots = [slot]
        self._idle_task = None
        self._prefetch_tasks = set()
        self._pending_prefetch = []
        self._load_seq = 0
        self._last_orphan_scan_ts = time.time()
        self._shared_http_client = None
        self._metrics = {}

    def _get_shared_http_client(self):
        return None

    def _prune_prefetch_tasks(self):
        self._prefetch_tasks = {t for t in self._prefetch_tasks if not t.done()}

    async def _enforce_vram_budget(self, slot):
        return None

    def _find_loaded_sync(self, model):
        return None

    async def _find_loaded(self, model, lock_held=False):
        return None

    async def _find_free_slot(self):
        for s in self._slots:
            if s.model is None and not getattr(s, "_loading", False):
                return s
        return None

    async def _evict_lru_async(self):
        return None

    async def _kill_port(self, port):
        return None

    def can_fit(self, model, num_ctx, exclude_slot_id=None, extra_needed_mib=0.0):
        return SimpleNamespace(ok=True, needed_mib=1024, free_mib=6000, margin_mib=768, source="test")

    async def _pre_flight_grace_recheck(self, *a, **kw):
        return self.can_fit(a[0], a[1])

    def _metric_inc(self, name, amount=1):
        self._metrics[name] = self._metrics.get(name, 0) + amount


srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def _fake_proc():
    return SimpleNamespace(
        pid=999999,
        poll=lambda: None,
        terminate=lambda: None,
        kill=lambda: None,
        wait=lambda **kw: 0,
    )


async def test_wait_ready_abort_and_sem():
    # 1. wait_ready aborts immediately when process=None + abort_wait() (loading 503 loop)
    _Handler.mode = "loading_503"
    s = ModelSlot(0)
    s.port = port
    proc = _fake_proc()
    s.process = proc
    s._loading = True
    t0 = time.monotonic()
    task = asyncio.create_task(
        s.wait_ready(timeout=240.0, expected_process=proc, lifecycle_gen=s._lifecycle_gen)
    )
    await asyncio.sleep(0.12)
    s.process = None
    s._loading = False
    s.abort_wait()
    res = await asyncio.wait_for(task, timeout=2.0)
    dt = time.monotonic() - t0
    check("1a wait_ready aborts fast when process cleared + abort_wait",
          res is False and dt < 1.0, f" (res={res} dt={dt:.2f}s)")

    # 2. wait_ready aborts immediately inside plain-text 3-poll stability loop
    _Handler.mode = "plain_ok"
    s2 = ModelSlot(0)
    s2.port = port
    proc2 = _fake_proc()
    s2.process = proc2
    s2._loading = True
    t0 = time.monotonic()
    task2 = asyncio.create_task(
        s2.wait_ready(timeout=240.0, expected_process=proc2, lifecycle_gen=s2._lifecycle_gen)
    )
    await asyncio.sleep(0.15)
    s2.abort_wait()
    res2 = await asyncio.wait_for(task2, timeout=2.0)
    dt2 = time.monotonic() - t0
    check("1b wait_ready aborts fast inside plain-text fallback loop",
          res2 is False and dt2 < 1.0, f" (res={res2} dt={dt2:.2f}s)")

    # 3. force_kill_all releases _vulkan_init_sem immediately for a waiting peer
    _Handler.mode = "loading_503"
    s3 = ModelSlot(0)
    s3.port = port
    proc3 = _fake_proc()
    s3.process = proc3
    s3._loading = True
    s3.model = "qwen3.5:4b-mtp"
    mgr = _FakeMgr(s3)
    sem_order = []

    async def _holder():
        async with mgr._vulkan_init_sem:
            sem_order.append("holder_acquired")
            ok = await s3.wait_ready(timeout=240.0, expected_process=proc3, lifecycle_gen=s3._lifecycle_gen)
            sem_order.append(f"holder_exit:{ok}")

    async def _waiter():
        await asyncio.sleep(0.05)
        async with mgr._vulkan_init_sem:
            sem_order.append("waiter_acquired")

    t0 = time.monotonic()
    h_task = asyncio.create_task(_holder())
    w_task = asyncio.create_task(_waiter())
    await asyncio.sleep(0.15)

    async def _async_abort(sl):
        sl.abort_wait()

    with mock.patch.object(MH.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
         mock.patch.object(MH, "_kill_slot_async", new=_async_abort):
        await mgr.force_kill_all(include_foreign=False)
    await asyncio.wait_for(asyncio.gather(h_task, w_task), timeout=2.5)
    dt3 = time.monotonic() - t0
    check("1c force_kill_all frees _vulkan_init_sem promptly for queued waiter",
          sem_order == ["holder_acquired", "holder_exit:False", "waiter_acquired"] and dt3 < 1.5,
          f" (order={sem_order} dt={dt3:.2f}s)")


async def test_ensure_loaded_no_cpu_fallback_or_peer_kill():
    s = ModelSlot(0)
    mgr = _FakeMgr(s)
    start_calls = []
    kill_calls = []

    async def _fake_kill(sl):
        kill_calls.append((sl.slot_id, sl.model))

    async def _fake_start(sl, model, num_ctx, **kw):
        start_calls.append((model, kw.get("gpu_layers_override")))
        # Simulate force_kill_all + peer claim during Task A's failed wait_ready
        sl.abort_wait()
        sl._loading = True
        sl.model = "peer:model"
        sl.process = _fake_proc()
        raise RuntimeError(f"llama-server for '{model}' crashed (exit=-1).")

    mgr._start_process = _fake_start
    caught = None
    with mock.patch.object(ML, "_kill_slot_async", new=_fake_kill), \
         mock.patch.object(ML, "_vram_of", return_value=2.6):
        try:
            await mgr.ensure_loaded("qwen3.5:4b-mtp", num_ctx=8192)
        except (SlotAbortedError, RuntimeError) as exc:
            caught = exc

    check("2a aborted ensure_loaded raises SlotAbortedError",
          isinstance(caught, SlotAbortedError), f" (got={type(caught).__name__}: {caught})")
    check("2b aborted ensure_loaded never triggers CPU fallback (--n-gpu-layers 0)",
          len(start_calls) == 1 and all(gl is None for _, gl in start_calls),
          f" (calls={start_calls})")
    check("2c aborted ensure_loaded never kills peer's newly claimed slot",
          kill_calls == [] and s.model == "peer:model" and s._loading is True,
          f" (kills={kill_calls} slot_model={s.model} loading={s._loading})")


async def test_phase_preflight_finally_preserves_peer_loading():
    _Handler.mode = "loading_503"
    s = ModelSlot(0)
    s.port = port
    s._loading = True
    s.model = "qwen3.5:4b-mtp"
    mgr = _FakeMgr(s)
    spawn_terminated = []

    def _rec_proc():
        p = _fake_proc()
        p.terminate = lambda: spawn_terminated.append("terminated")
        return p

    fake_log = SimpleNamespace(write=lambda *_: None, flush=lambda: None, close=lambda: None)
    orig_wait = ModelSlot.wait_ready

    async def _aborting_wait(self_slot, *a, **kw):
        async def _steal_later():
            await asyncio.sleep(0.1)
            self_slot.abort_wait()
            # Peer claims the same slot immediately after kill/purge
            self_slot._loading = True
            self_slot.model = "other:model"
            self_slot.process = _fake_proc()

        steal = asyncio.create_task(_steal_later())
        res = await orig_wait(self_slot, *a, **kw)
        await steal
        return res

    async def _async_abort_kill(sl):
        sl.abort_wait()

    caught = None
    with mock.patch.object(ML, "_archive_old_slot_logs"), \
         mock.patch("builtins.open", return_value=fake_log), \
         mock.patch.object(LSP.subprocess, "Popen", return_value=_rec_proc()), \
         mock.patch.object(LSP, "_kill_slot_async", new=_async_abort_kill), \
         mock.patch.object(ModelSlot, "wait_ready", new=_aborting_wait):
        try:
            await LSP._phase_preflight_and_spawn(
                mgr, s, ["llama-server", "--port", str(port)], "qwen3.5:4b-mtp",
                "C:/fake/model.gguf", 8192, True, 1, True,
                "qwen3.5", "mmap", None, 0.0, False,
            )
        except (SlotAbortedError, RuntimeError) as exc:
            caught = exc

    check("3a _phase_preflight_and_spawn raises SlotAbortedError when aborted",
          isinstance(caught, SlotAbortedError), f" (got={type(caught).__name__}: {caught})")
    check("3b _phase_preflight_and_spawn finally does not clear peer's _loading",
          s._loading is True and s.model == "other:model" and spawn_terminated == ["terminated"],
          f" (loading={s._loading} model={s.model} term={spawn_terminated})")


asyncio.run(test_wait_ready_abort_and_sem())
asyncio.run(test_ensure_loaded_no_cpu_fallback_or_peer_kill())
asyncio.run(test_phase_preflight_finally_preserves_peer_loading())

srv.shutdown()
sys.exit(h.finish("wait_ready_abort"))
