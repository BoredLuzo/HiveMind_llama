# -*- coding: utf-8 -*-
"""Wave 2 B2 regression suite: slot lifecycle races, load cleanup, and port-lock scoping.

Run: python tests/test_audit_wave2_slot_locks.py
Exit 0 = all pass, Exit 1 = failures.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

from backend import llama_client as LC  # noqa: E402
from backend import llama_start_phases as LSP  # noqa: E402
from backend import manager_health as MH  # noqa: E402
from backend import manager_load as ML  # noqa: E402
from backend import manager_process as MP  # noqa: E402
from backend.llama_slots import ModelSlot  # noqa: E402
from core import chat_run as CR  # noqa: E402
from core import direct_runner as DR  # noqa: E402
from core import llm_transport as T  # noqa: E402
from core.tool_loop import ToolLoop, ToolLoopConfig, ToolLoopState  # noqa: E402

h = Harness(style="box", width=60)
check = h.check


def _fake_proc(*, pid: int | None = None, terminate_exc: BaseException | None = None):
    calls: list[str] = []

    def _poll():
        return None

    def _terminate():
        calls.append("terminate")
        if terminate_exc is not None:
            raise terminate_exc

    def _kill():
        calls.append("kill")

    def _wait(**_kw):
        calls.append("wait")
        return 0

    proc = SimpleNamespace(
        pid=pid,
        poll=_poll,
        terminate=_terminate,
        kill=_kill,
        wait=_wait,
        calls=calls,
    )
    return proc


class _DummyMgr(ML.LlamaLoadMixin, MP.LlamaProcessMixin, MH.LlamaHealthMixin):
    _binary_build_number = 12001

    def __init__(self, slots: list[ModelSlot] | None = None):
        self._lock = asyncio.Lock()
        self._vulkan_init_sem = asyncio.Semaphore(1)
        self._slots = slots if slots is not None else [ModelSlot(0)]
        self._idle_task = None
        self._prefetch_tasks = set()
        self._pending_prefetch = []
        self._load_seq = 0
        self._last_orphan_scan_ts = time.time()
        self._shared_http_client = None
        self._metrics: dict[str, int] = {}
        self.vram_calls: list[int] = []
        self._alive_ports: set[int] = set()

    def _get_shared_http_client(self):
        return self._shared_http_client

    def _prune_prefetch_tasks(self):
        self._prefetch_tasks = {t for t in self._prefetch_tasks if not t.done()}

    async def _enforce_vram_budget(self, slot):
        self.vram_calls.append(slot.slot_id)
        return None

    async def _evict_lru_async(self):
        return None

    async def _kill_port(self, port):
        return None

    async def _port_alive(self, port: int) -> bool:
        return port in self._alive_ports

    def can_fit(self, model, num_ctx, exclude_slot_id=None, extra_needed_mib=0.0):
        return SimpleNamespace(ok=True, needed_mib=1024, free_mib=6000, margin_mib=768, source="test")

    async def _pre_flight_grace_recheck(self, *args, **_kwargs):
        return self.can_fit(args[0], args[1])

    def _metric_inc(self, name: str, amount: int = 1):
        self._metrics[name] = self._metrics.get(name, 0) + amount


# ── 1. Cross-thread ModelSlot.kill() + guarded terminate/finally ─────────────


async def test_cross_thread_kill_and_abort_wait():
    slot = ModelSlot(0)
    proc = _fake_proc(pid=None, terminate_exc=ProcessLookupError("already exited"))
    slot.process = proc
    slot.model = "qwen3.5:4b"
    slot._loading = True
    slot._entry_load_seq = 7
    slot._vision = True
    slot._jinja = True
    slot.vision_active = True

    old_ready = slot._ready_event
    old_ready.clear()
    waiter_task = asyncio.create_task(old_ready.wait())

    def _health_503(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "loading"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_health_503))
    try:
        t0 = time.monotonic()
        wait_task = asyncio.create_task(
            slot.wait_ready(
                timeout=15.0,
                http_client=client,
                expected_process=proc,
                lifecycle_gen=slot._lifecycle_gen,
            )
        )
        # Wait briefly until _wait_abort_ev/_wait_abort_loop are registered inside wait_ready
        for _ in range(30):
            if slot._wait_abort_ev is not None and slot._wait_abort_loop is not None:
                break
            await asyncio.sleep(0.02)

        abort_ev_seen = slot._wait_abort_ev
        abort_loop_seen = slot._wait_abort_loop
        check(
            "kill() setup: wait_ready registered abort event + running loop",
            abort_ev_seen is not None and abort_loop_seen is asyncio.get_running_loop(),
        )

        # Invoke kill() from a worker thread
        await asyncio.to_thread(slot.kill)
        wait_res = await asyncio.wait_for(wait_task, timeout=2.0)
        await asyncio.wait_for(waiter_task, timeout=2.0)
        dt = time.monotonic() - t0
    finally:
        await client.aclose()

    check(
        "cross-thread kill(): wait_ready wakes promptly and returns False",
        wait_res is False and dt < 2.0,
        f" (res={wait_res} dt={dt:.2f}s)",
    )
    check(
        "cross-thread kill(): guarded terminate() exception does not abort cleanup",
        "terminate" in proc.calls
        and slot.process is None
        and slot.model is None
        and slot._loading is False
        and slot._entry_load_seq is None
        and slot._vision is False
        and slot._jinja is False
        and slot.vision_active is None,
    )
    check(
        "cross-thread kill(): old _ready_event woken and swapped to fresh unset Event",
        old_ready.is_set()
        and slot._ready_event is not old_ready
        and not slot._ready_event.is_set(),
    )


# ── 2. CancelledError in ensure_loaded (owner vs non-owner waiter) ───────────


async def test_cancelled_error_ensure_loaded():
    # Case A: Cancel during _enforce_vram_budget
    s_a = ModelSlot(0)
    mgr_a = _DummyMgr([s_a])
    entered_vram = asyncio.Event()

    async def _slow_vram(_slot):
        entered_vram.set()
        await asyncio.sleep(10.0)

    mgr_a._enforce_vram_budget = _slow_vram
    old_ev_a = s_a._ready_event
    peer_waiter_a = asyncio.create_task(old_ev_a.wait())
    owner_a = asyncio.create_task(mgr_a.ensure_loaded("qwen3.5:4b", num_ctx=4096))
    await asyncio.wait_for(entered_vram.wait(), timeout=2.0)
    owner_a.cancel()

    caught_a = False
    try:
        await owner_a
    except asyncio.CancelledError:
        caught_a = True
    await asyncio.wait_for(peer_waiter_a, timeout=2.0)
    check(
        "CancelledError in ensure_loaded (_enforce_vram_budget): cleaned + peer woken",
        caught_a
        and s_a._loading is False
        and s_a.model is None
        and s_a._entry_load_seq is None
        and old_ev_a.is_set()
        and s_a._ready_event is not old_ev_a
        and not s_a._ready_event.is_set(),
    )

    # Case B: Cancel during _start_process
    s_b = ModelSlot(0)
    mgr_b = _DummyMgr([s_b])
    entered_start = asyncio.Event()

    async def _slow_start(_slot, *_args, **_kwargs):
        entered_start.set()
        await asyncio.sleep(10.0)

    mgr_b._start_process = _slow_start
    old_ev_b = s_b._ready_event
    peer_waiter_b = asyncio.create_task(old_ev_b.wait())
    owner_b = asyncio.create_task(mgr_b.ensure_loaded("qwen3.5:4b", num_ctx=4096))
    await asyncio.wait_for(entered_start.wait(), timeout=2.0)
    owner_b.cancel()

    caught_b = False
    try:
        await owner_b
    except asyncio.CancelledError:
        caught_b = True
    await asyncio.wait_for(peer_waiter_b, timeout=2.0)
    check(
        "CancelledError in ensure_loaded (_start_process): cleaned + fresh Event",
        caught_b
        and s_b._loading is False
        and s_b.model is None
        and s_b._entry_load_seq is None
        and old_ev_b.is_set()
        and s_b._ready_event is not old_ev_b
        and not s_b._ready_event.is_set(),
    )

    # Case C: Cancel while owner awaits slot._ready_event.wait()
    s_c = ModelSlot(0)
    mgr_c = _DummyMgr([s_c])

    async def _spawn_leaves_waiting(slot, model, num_ctx, **_kwargs):
        slot.process = _fake_proc(pid=None)
        slot._ready_event.clear()

    mgr_c._start_process = _spawn_leaves_waiting
    owner_c = asyncio.create_task(mgr_c.ensure_loaded("qwen3.5:4b", num_ctx=4096))
    for _ in range(30):
        if s_c._loading and s_c.process is not None:
            break
        await asyncio.sleep(0.02)
    old_ev_c = s_c._ready_event
    peer_waiter_c = asyncio.create_task(old_ev_c.wait())
    await asyncio.sleep(0)
    owner_c.cancel()

    caught_c = False
    try:
        await owner_c
    except asyncio.CancelledError:
        caught_c = True
    await asyncio.wait_for(peer_waiter_c, timeout=2.0)
    check(
        "CancelledError in ensure_loaded (ready wait): owner cleans slot + wakes peers",
        caught_c
        and s_c._loading is False
        and s_c.model is None
        and s_c.process is None
        and old_ev_c.is_set()
        and s_c._ready_event is not old_ev_c
        and not s_c._ready_event.is_set(),
    )

    # Case D: Cancelling a NON-owner waiter preserves peer's active load
    s_d = ModelSlot(0)
    s_d.model = "qwen3.5:4b"
    s_d._loading = True
    s_d._vision = False
    s_d._jinja = True
    s_d.process = _fake_proc(pid=None)
    s_d._ready_event.clear()
    mgr_d = _DummyMgr([s_d])
    ev_before_d = s_d._ready_event

    waiter_d = asyncio.create_task(mgr_d.ensure_loaded("qwen3.5:4b", num_ctx=4096))
    for _ in range(20):
        await asyncio.sleep(0.01)
    waiter_d.cancel()

    caught_d = False
    try:
        await waiter_d
    except asyncio.CancelledError:
        caught_d = True
    check(
        "CancelledError on non-owner waiter preserves peer _loading + model",
        caught_d
        and s_d._loading is True
        and s_d.model == "qwen3.5:4b"
        and s_d._ready_event is ev_before_d
        and not ev_before_d.is_set(),
    )


# ── 3. _apply_slot_props sets _vision / vision_active / _jinja ───────────────


async def test_apply_slot_props_and_no_cold_reload():
    mgr = _DummyMgr()

    s1 = ModelSlot(0)
    mgr._apply_slot_props(
        s1,
        "qwen3.5:4b",
        {"modalities": {"vision": True}, "jinja": True, "default_generation_settings": {"n_ctx": 8192}},
    )
    check(
        "_apply_slot_props: modalities.vision + jinja explicit True",
        s1.vision_active is True and s1._vision is True and s1._jinja is True and s1._num_ctx == 8192,
    )

    s2 = ModelSlot(1)
    mgr._apply_slot_props(
        s2,
        "qwen3.5:4b",
        {"modalities": {"vision": False}, "chat_template": "{% for m in messages %}{% endfor %}"},
    )
    check(
        "_apply_slot_props: chat_template fallback populates _jinja=True and vision=False",
        s2.vision_active is False and s2._vision is False and s2._jinja is True,
    )

    s3 = ModelSlot(2)
    mgr._apply_slot_props(
        s3,
        "qwen3.5:4b",
        {"vision": True, "chat_template_caps": {"supports_tools": True}},
    )
    check(
        "_apply_slot_props: flat vision + chat_template_caps sets both True",
        s3.vision_active is True and s3._vision is True and s3._jinja is True,
    )

    s4 = ModelSlot(3)
    mgr._apply_slot_props(
        s4,
        "qwen3.5:4b",
        {"modalities": {"vision": True}, "jinja": False, "chat_template": "template"},
    )
    check(
        "_apply_slot_props: explicit jinja=False takes precedence over chat_template",
        s4._vision is True and s4._jinja is False,
    )

    # Adopted slot should satisfy ensure_loaded(..., vision=True) with zero cold reloads
    adopted = ModelSlot(0)
    mgr_adopt = _DummyMgr([adopted])
    mgr_adopt._alive_ports.add(adopted.port)
    mgr_adopt._apply_slot_props(
        adopted,
        "qwen3.5:4b",
        {
            "modalities": {"vision": True},
            "jinja": True,
            "default_generation_settings": {"n_ctx": 8192, "n_parallel": 1},
        },
    )
    start_calls = 0

    async def _unexpected_start(*_args, **_kwargs):
        nonlocal start_calls
        start_calls += 1

    mgr_adopt._start_process = _unexpected_start
    with mock.patch.object(mgr_adopt, "_verify_port_api", return_value=True):
        got_port = await mgr_adopt.ensure_loaded("qwen3.5:4b", num_ctx=8192, vision=True)
    check(
        "_apply_slot_props prevents redundant cold reload on ensure_loaded(vision=True)",
        got_port == adopted.port and start_calls == 0,
        f" (port={got_port} starts={start_calls})",
    )


# ── 4. _entry_load_seq in main claim path + preflight skip ───────────────────


async def test_entry_load_seq_in_claim_path():
    s0 = ModelSlot(0)
    peer = ModelSlot(1)
    peer.model = "other:model"
    peer.process = _fake_proc(pid=None)
    mgr = _DummyMgr([s0, peer])

    seen_seq: list[tuple[int | None, int]] = []

    async def _capture_start(slot, *_args, **_kwargs):
        seen_seq.append((getattr(slot, "_entry_load_seq", None), mgr._load_seq))
        slot.process = _fake_proc(pid=None)
        slot._loading = False
        slot._ready_event.set()

    mgr._start_process = _capture_start
    port = await mgr.ensure_loaded("qwen3.5:4b", num_ctx=8192)
    check(
        "ensure_loaded main claim path sets slot._entry_load_seq == mgr._load_seq",
        port == s0.port and seen_seq == [(1, 1)],
        f" (seen={seen_seq})",
    )

    # Verify _phase_preflight_and_spawn skips the 2-6s AMD Vulkan commit sleep when seq matches
    s0.process = None
    s0._loading = True
    s0.model = "qwen3.5:4b"
    s0._entry_load_seq = mgr._load_seq
    sleep_calls: list[float] = []

    async def _rec_sleep(sec: float):
        sleep_calls.append(sec)

    fake_log = SimpleNamespace(write=lambda *_: None, flush=lambda: None, close=lambda: None)

    async def _ready_immediately(_self, *_args, **_kwargs):
        return True

    with mock.patch.object(ML, "_archive_old_slot_logs"), \
         mock.patch("builtins.open", return_value=fake_log), \
         mock.patch.object(LSP.subprocess, "Popen", return_value=_fake_proc(pid=None)), \
         mock.patch.object(ModelSlot, "wait_ready", new=_ready_immediately), \
         mock.patch.object(LSP.asyncio, "sleep", new=_rec_sleep):
        await LSP._phase_preflight_and_spawn(
            mgr, s0, ["llama-server", "--port", str(s0.port)], "qwen3.5:4b",
            "C:/fake/model.gguf", 8192, True, 1, True,
            "qwen3.5", "mmap", None, 0.0, False,
        )
    check(
        "_phase_preflight_and_spawn skips 2-6s Vulkan wait when _entry_load_seq == _load_seq",
        not any(sec >= 2.0 for sec in sleep_calls),
        f" (sleep_calls={sleep_calls})",
    )


# ── 5. force_kill_all swaps fresh unset _ready_event after waking old waiters ─


async def test_force_kill_all_fresh_event():
    tgt = ModelSlot(0)
    tgt.model = "qwen3.5:4b"
    tgt.process = _fake_proc(pid=54321)
    tgt._loading = True
    tgt.pinned = True
    tgt.pinned_at = 123.4
    tgt._entry_load_seq = 9
    old_ev = tgt._ready_event
    old_ev.clear()
    waiter = asyncio.create_task(old_ev.wait())

    mgr = _DummyMgr([tgt])
    with mock.patch.object(MH.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
         mock.patch.object(MH, "_kill_slot_async", new=mock.AsyncMock()):
        res = await mgr.force_kill_all(include_foreign=False)
    await asyncio.wait_for(waiter, timeout=2.0)

    check(
        "force_kill_all wakes existing waiters on old Event and installs fresh unset Event",
        res.get("ok") is True
        and old_ev.is_set()
        and tgt._ready_event is not old_ev
        and not tgt._ready_event.is_set()
        and tgt._loading is False
        and tgt.model is None
        and tgt.pinned_at == 0.0
        and tgt._entry_load_seq is None,
    )


# ── 6. _find_loaded lock discipline (AST + unlocked probe + CancelledError) ──


async def test_find_loaded_lock_discipline():
    src_path = ROOT / "backend" / "manager_process.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    manual_lock_calls = [
        (fn.name, node.func.attr)
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name != "_unlocked_if_held"
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("acquire", "release")
    ]
    check(
        "_find_loaded / manager_process async methods have zero manual lock acquire()/release() calls",
        manual_lock_calls == [],
        f" (found={manual_lock_calls})",
    )

    orphan_slot = ModelSlot(0)
    orphan_slot.model = "probe:4b"
    orphan_slot._orphan_port = orphan_slot.port
    mgr = _DummyMgr([orphan_slot])

    lock_states_in_probe: list[bool] = []
    peer_acquired = False

    async def _probe_checks_lock(_port: int) -> bool:
        nonlocal peer_acquired
        lock_states_in_probe.append(mgr._lock.locked())
        async with mgr._lock:
            peer_acquired = True
        return True

    mgr._verify_port_api = _probe_checks_lock
    found = await mgr._find_loaded("probe:4b", lock_held=False)
    check(
        "_find_loaded runs _verify_port_api outside self._lock (peer can acquire lock)",
        found is orphan_slot
        and lock_states_in_probe == [False]
        and peer_acquired is True
        and not mgr._lock.locked(),
    )

    # Cancel mid-probe and ensure self._lock stays healthy/unlocked
    orphan_slot2 = ModelSlot(0)
    orphan_slot2.model = "probe:4b"
    orphan_slot2._orphan_port = orphan_slot2.port
    mgr2 = _DummyMgr([orphan_slot2])
    probe_started = asyncio.Event()

    async def _hanging_probe(_port: int) -> bool:
        probe_started.set()
        await asyncio.sleep(10.0)
        return True

    mgr2._verify_port_api = _hanging_probe
    find_task = asyncio.create_task(mgr2._find_loaded("probe:4b", lock_held=False))
    await asyncio.wait_for(probe_started.wait(), timeout=2.0)
    find_task.cancel()

    cancelled_mid_probe = False
    try:
        await find_task
    except asyncio.CancelledError:
        cancelled_mid_probe = True
    check(
        "_find_loaded CancelledError mid-probe leaves self._lock unlocked",
        cancelled_mid_probe and not mgr2._lock.locked(),
    )


# ── 7. get_port_lock released during recovery, sleeps, and non-stream yield ─


async def test_port_lock_released_during_recovery_and_sleep():
    # (a) LlamaClient.chat_stream: 404 -> _recover_model_port -> sleep -> 503 -> sleep -> 200 SSE
    port_lc = 19101
    lock_lc = T.get_port_lock(port_lc)
    lc_post_locks: list[bool] = []
    lc_recover_locks: list[bool] = []
    lc_sleep_locks: list[bool] = []

    sse_ok = 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n'
    attempts_lc = {"n": 0}

    def _lc_handler(request: httpx.Request) -> httpx.Response:
        attempts_lc["n"] += 1
        lc_post_locks.append(lock_lc.locked())
        if attempts_lc["n"] == 1:
            return httpx.Response(404, text="not found", request=request)
        if attempts_lc["n"] == 2:
            return httpx.Response(503, text="loading model", request=request)
        return httpx.Response(
            200, text=sse_ok, headers={"content-type": "text/event-stream"}, request=request,
        )

    client_lc = LC.LlamaClient()
    await client_lc._client.aclose()
    client_lc._client = httpx.AsyncClient(transport=httpx.MockTransport(_lc_handler))

    async def _fake_ensure_loaded(*_args, **_kwargs):
        return port_lc

    async def _fake_recover(*_args, **_kwargs):
        lc_recover_locks.append(lock_lc.locked())
        return port_lc

    async def _rec_sleep_lc(_sec: float):
        lc_sleep_locks.append(lock_lc.locked())

    tokens_lc: list[str] = []
    try:
        with mock.patch.object(LC.manager, "ensure_loaded", new=_fake_ensure_loaded), \
             mock.patch.object(client_lc, "_recover_model_port", new=_fake_recover), \
             mock.patch.object(LC.asyncio, "sleep", new=_rec_sleep_lc):
            async for tok in client_lc.chat_stream(
                "test:1b", [{"role": "user", "content": "hi"}], ctx=2048,
            ):
                tokens_lc.append(str(tok))
    finally:
        await client_lc._client.aclose()

    check(
        "LlamaClient.chat_stream holds get_port_lock during POST only (released in recovery/sleep)",
        tokens_lc == ["hello"]
        and lc_post_locks == [True, True, True]
        and lc_recover_locks == [False]
        and lc_sleep_locks == [False, False]
        and not lock_lc.locked(),
        f" (post={lc_post_locks} rec={lc_recover_locks} sleep={lc_sleep_locks})",
    )

    # (b) stream_chat_chunks: 503 -> sleep -> 200 SSE
    port_sc = 19102
    lock_sc = T.get_port_lock(port_sc)
    sc_post_locks: list[bool] = []
    sc_sleep_locks: list[bool] = []
    attempts_sc = {"n": 0}

    def _sc_handler(request: httpx.Request) -> httpx.Response:
        attempts_sc["n"] += 1
        sc_post_locks.append(lock_sc.locked())
        if attempts_sc["n"] == 1:
            return httpx.Response(503, text="busy", request=request)
        return httpx.Response(
            200, text=sse_ok, headers={"content-type": "text/event-stream"}, request=request,
        )

    async def _rec_sleep_sc(_sec: float):
        sc_sleep_locks.append(lock_sc.locked())

    c_sc = httpx.AsyncClient(transport=httpx.MockTransport(_sc_handler))
    chunks_sc: list[dict] = []
    try:
        with mock.patch.object(T.asyncio, "sleep", new=_rec_sleep_sc):
            async for ch in T.stream_chat_chunks(port_sc, {"messages": []}, client=c_sc, max_attempts=3):
                chunks_sc.append(ch)
    finally:
        await c_sc.aclose()

    check(
        "stream_chat_chunks holds get_port_lock during HTTP stream only (released in sleep)",
        len(chunks_sc) == 1
        and sc_post_locks == [True, True]
        and sc_sleep_locks == [False]
        and not lock_sc.locked(),
    )

    # (c) iter_tool_loop_post: 500 error-handler sleep/yield AND non-stream result yield
    port_tl = 19103
    lock_tl = T.get_port_lock(port_tl)
    tl_post_locks: list[bool] = []
    tl_sleep_locks: list[bool] = []
    tl_yield_locks: list[tuple[str, bool]] = []
    attempts_tl = {"n": 0}

    def _tl_handler(request: httpx.Request) -> httpx.Response:
        attempts_tl["n"] += 1
        tl_post_locks.append(lock_tl.locked())
        if attempts_tl["n"] == 1:
            return httpx.Response(500, text="temporary backend overload", request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "OK", "tool_calls": []}}]},
            request=request,
        )

    c_tl = httpx.AsyncClient(transport=httpx.MockTransport(_tl_handler))
    cfg_tl = ToolLoopConfig(stream=False, max_post_attempts=3, retry_on_5xx=True, model="fake:1b")
    loop_tl = ToolLoop(cfg_tl, c_tl, port_tl)
    loop_tl.state = ToolLoopState()
    loop_tl._usage_prompt = 0
    loop_tl._usage_completion = 0
    loop_tl._usage_cached = 0

    async def _rec_sleep_tl(_sec: float):
        tl_sleep_locks.append(lock_tl.locked())

    try:
        with mock.patch.object(T.asyncio, "sleep", new=_rec_sleep_tl):
            async for ev in T.iter_tool_loop_post(
                loop_tl,
                {"model": "fake:1b", "messages": [{"role": "user", "content": "hi"}]},
                stream=False,
                retry_allowed_fn=lambda *_args, **_kwargs: True,
                grammar_compatible_fn=lambda *_args, **_kwargs: True,
                build_grammar_fn=lambda *_args, **_kwargs: None,
                inject_feedback_fn=lambda p, *_args, **_kwargs: p,
                parse_sse_delta_fn=lambda d, tool_calls_acc=None: (d.get("content", ""), []),
                use_gbnf_grammar=False,
            ):
                kind = "tuple" if isinstance(ev, tuple) else str(ev.get("type", "ev"))
                tl_yield_locks.append((kind, lock_tl.locked()))
    finally:
        await c_tl.aclose()

    check(
        "iter_tool_loop_post releases get_port_lock before error sleep, status yield, and tuple yield",
        tl_post_locks == [True, True]
        and tl_sleep_locks == [False]
        and len(tl_yield_locks) >= 1
        and all(not locked for _, locked in tl_yield_locks)
        and not lock_tl.locked(),
        f" (post={tl_post_locks} sleep={tl_sleep_locks} yields={tl_yield_locks})",
    )


# ── 8. contextlib.aclosing closes aborted streams immediately ────────────────


async def test_aclosing_on_aborted_streams():
    # (a) chat_run.run_stream early close immediately closes _run_stream_locked
    cr_state = {"locked_closed": False}

    async def _fake_locked_stream(*_args, **_kwargs):
        try:
            yield f"data: {json.dumps({'type': 'run_id', 'run_id': 'r-aclose-1'})}\n\n"
            yield f"data: {json.dumps({'type': 'token', 'content': 'hello'})}\n\n"
            await asyncio.sleep(10.0)
        finally:
            cr_state["locked_closed"] = True

    with mock.patch.object(CR, "_run_stream_locked", new=_fake_locked_stream):
        rs_gen = CR.run_stream("hi", [], "chat", 1, "default")
        first_ev = await anext(rs_gen)
        check("run_stream yields first run_id event before early close", '"run_id"' in first_ev)
        await rs_gen.aclose()
    check(
        "run_stream aclosing immediately closes _run_stream_locked",
        cr_state["locked_closed"] is True,
    )

    # (b) direct_runner.run_direct early abort immediately closes chat_stream and releases port lock
    port_dr = 19201
    lock_dr = T.get_port_lock(port_dr)
    dr_state = {"stream_closed": False, "lock_on_done": None, "aborted": False}

    async def _aborted_chat_stream(*_args, **_kwargs):
        async with lock_dr:
            try:
                yield ("first ", "")
                yield ("second ", "")
                yield ("third", "")
            finally:
                dr_state["stream_closed"] = True

    class _FakeMemory:
        def __init__(self):
            self.session = []

        def add_to_session(self, role, content):
            self.session.append((role, content))

    events_dr: list[dict] = []

    async def _emit_dr(ev: dict):
        events_dr.append(ev)
        if ev.get("type") == "token":
            dr_state["aborted"] = True
        if ev.get("type") == "done":
            dr_state["lock_on_done"] = lock_dr.locked()
        return ev

    async def _noop_async(*_args, **_kwargs):
        return None

    fake_ctx = SimpleNamespace(
        complexity="simple",
        complexity_source="manual",
        effective_task_type="general",
        active_preset="default",
        use_learned=False,
        pipeline_mem_ctx="",
        pipeline_sess_msgs=[],
        models_cache=["fake-direct-model"],
        vision_cfg={},
        prepro_success=False,
        image_description="",
        images=[],
        effective_images=[],
        registry_get=lambda _role: "fake-direct-model",
        model_profile=lambda _m: {},
        settings={"direct_model": "fake-direct-model", "direct_tools_enabled": False, "direct_tools_tier": "off"},
        websearch_available=False,
        pipeline_soul={},
        user_input="hi",
        workspace="",
        run_id="r-dr-aclose",
        chat_id="c-dr-aclose",
        t_total=time.time(),
        pipeline=SimpleNamespace(
            agents={
                "direct": SimpleNamespace(
                    model="fake-direct-model",
                    temperature=0.4,
                    max_tokens=100,
                    thinking=False,
                    thinking_budget=0,
                )
            },
            ollama=SimpleNamespace(chat_stream=_aborted_chat_stream),
        ),
        get_num_ctx=lambda *_a, **_k: 8192,
        aborted=lambda: dr_state["aborted"],
        unregister_abort=lambda _rid: None,
        unregister_step_skip=lambda _rid: None,
        increment_run_counter=lambda: 0,
        collect_done_metrics=lambda: {},
        done_event=lambda elapsed, stop_reason, **kw: {"type": "done", "elapsed": elapsed, "stop_reason": stop_reason, **kw},
        memory=_FakeMemory(),
        maybe_preload=_noop_async,
        maybe_trigger_soul_evolution=_noop_async,
        get_effective_prompt_with_override=lambda *_a, **_k: "SYS",
        make_messages=lambda _p, sys_p, inp, *_a, **_k: [{"role": "system", "content": sys_p}, {"role": "user", "content": inp}],
        emit=_emit_dr,
    )

    with mock.patch.object(DR, "_pick_direct_model", lambda *_a, **_k: "fake-direct-model"), \
         mock.patch.object(DR, "_refresh_judge_keepalive", _noop_async), \
         mock.patch.object(DR, "run_peer_ratings", lambda **_kw: _noop_async()), \
         mock.patch("context.chat._mutate_chat_context", lambda _cid, _fn: None):
        async with contextlib.aclosing(DR.run_direct(fake_ctx)) as dr_gen:
            async for _ev in dr_gen:
                pass

    check(
        "direct_runner aclosing closes chat_stream immediately on abort and frees port lock",
        dr_state["stream_closed"] is True
        and dr_state["lock_on_done"] is False
        and not lock_dr.locked(),
        f" (state={dr_state})",
    )

    # (c) ToolLoop._post_stream_with_retry early break closes _stream_slot_once and releases port lock
    port_tl_abort = 19202
    lock_tl_abort = T.get_port_lock(port_tl_abort)
    stream_close_flag = {"closed": False}

    class _TrackedAsyncStream(httpx.AsyncByteStream):
        def __init__(self, body: bytes):
            self._body = body

        async def __aiter__(self):
            yield self._body

        async def aclose(self):
            stream_close_flag["closed"] = True

    sse_multi = (
        b'data: {"choices":[{"delta":{"content":"t1"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"t2"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"t3"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def _tl_abort_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_TrackedAsyncStream(sse_multi),
            request=request,
        )

    c_abort = httpx.AsyncClient(transport=httpx.MockTransport(_tl_abort_handler))
    cfg_abort = ToolLoopConfig(stream=True, max_post_attempts=2, model="fake:1b")
    loop_abort = ToolLoop(cfg_abort, c_abort, port_tl_abort)
    loop_abort._usage_prompt = 0
    loop_abort._usage_completion = 0
    loop_abort._usage_cached = 0

    try:
        seen_tokens: list[dict] = []
        async with contextlib.aclosing(
            loop_abort._post_stream_with_retry(
                {"model": "fake:1b", "messages": [{"role": "user", "content": "hi"}]}
            )
        ) as tl_stream:
            async for ev in tl_stream:
                if isinstance(ev, dict) and ev.get("type") == "token":
                    seen_tokens.append(ev)
                    break
        check(
            "ToolLoop._post_stream_with_retry early break closes HTTP stream + frees port lock",
            len(seen_tokens) == 1
            and stream_close_flag["closed"] is True
            and not lock_tl_abort.locked(),
            f" (tokens={seen_tokens} closed={stream_close_flag['closed']} locked={lock_tl_abort.locked()})",
        )
    finally:
        await c_abort.aclose()


async def main():
    await test_cross_thread_kill_and_abort_wait()
    await test_cancelled_error_ensure_loaded()
    await test_apply_slot_props_and_no_cold_reload()
    await test_entry_load_seq_in_claim_path()
    await test_force_kill_all_fresh_event()
    await test_find_loaded_lock_discipline()
    await test_port_lock_released_during_recovery_and_sleep()
    await test_aclosing_on_aborted_streams()


asyncio.run(main())
sys.exit(h.finish("audit_wave2_slot_locks"))
