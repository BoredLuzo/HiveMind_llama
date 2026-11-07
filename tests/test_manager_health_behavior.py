# -*- coding: utf-8 -*-
"""Behavior tests for backend/manager_health.py (LlamaHealthMixin).

The mixin is exercised against a FakeManager that provides only the
attributes/methods the mixin consumes, with REAL ModelSlot instances.
All side-effect seams are faked: _kill_slot_async, list_available_models
and subprocess.run are monkeypatched on the manager_health namespace, the
HTTP client is an in-process fake and the idle monitor's 30-35 s jitter
sleep is neutralized via random.uniform -> -30.0 (asyncio.sleep(0)).
No llama-server processes, no real taskkill, no network.

Run: python tests/test_manager_health_behavior.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import platform
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import backend.manager_health as MH  # noqa: E402
from backend.llama_slots import ModelSlot  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

_MISSING = object()


class Patch:
    """Minimal monkeypatcher (no pytest): set/restore module attributes."""

    def __init__(self):
        self._undo = []

    def set(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name, _MISSING)))
        setattr(obj, name, value)

    def restore(self):
        while self._undo:
            obj, name, old = self._undo.pop()
            if old is _MISSING:
                delattr(obj, name)
            else:
                setattr(obj, name, old)


class FakeHTTPClient:
    """AsyncClient stand-in: records posts, optionally fails them."""

    def __init__(self, fail=True):
        self.calls = []
        self._fail = fail
        self.is_closed = False

    async def post(self, url, **kw):
        self.calls.append(url)
        if self._fail:
            raise RuntimeError("connect failed")

    async def aclose(self):
        self.is_closed = True


class FakeManager(MH.LlamaHealthMixin):
    """Only the state the health mixin touches - nothing else."""

    def __init__(self):
        self._slots = []
        self._lock = asyncio.Lock()
        self._idle_task = None
        self._pending_prefetch = []
        self._prefetch_tasks = set()
        self._shared_http_client = None
        self._alive_ports = set()
        self.metrics = {}
        self.prefetch_calls = 0

    def _metric_inc(self, name):
        self.metrics[name] = self.metrics.get(name, 0) + 1

    async def _trigger_pending_prefetch(self):
        self.prefetch_calls += 1

    def _prune_prefetch_tasks(self):
        self._prefetch_tasks = {t for t in self._prefetch_tasks if not t.done()}

    async def _find_loaded(self, model):
        for s in self._slots:
            if MH._nm(s.model or "") == MH._nm(model or ""):
                return s
        return None

    def _find_loaded_sync(self, model):
        for s in self._slots:
            if MH._nm(s.model or "") == MH._nm(model or ""):
                return s
        return None

    async def _port_alive(self, port):
        return port in self._alive_ports

    def _get_shared_http_client(self):
        if self._shared_http_client is None:
            self._shared_http_client = FakeHTTPClient()
        return self._shared_http_client


def _live_slot(slot_id, model=None):
    """Slot that reports is_running() == True without any real process."""
    s = ModelSlot(slot_id)
    s.model = model
    s.process = SimpleNamespace(pid=40000 + slot_id, poll=lambda: None)
    return s


async def _cancel(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _recorder(slot):  # default recorder, replaced by scoped versions
    return None


async def amain():
    patch = Patch()

    # ── _idle_evictable predicate ────────────────────────────────────────────
    s = ModelSlot(0)
    s.model = "m"
    s.process = SimpleNamespace(pid=1, poll=lambda: None)
    s._idle_timeout = 10.0
    s.last_used = time.monotonic() - 100.0
    check("idle: running+unpinned+timed out is evictable", MH._idle_evictable(s, time.monotonic()))
    s.pinned = True
    check("idle: pinned slot never evictable", not MH._idle_evictable(s, time.monotonic()))
    s.pinned = False
    s._loading = True
    check("idle: loading slot never evictable", not MH._idle_evictable(s, time.monotonic()))
    s._loading = False
    s._idle_timeout = None
    check("idle: no timeout -> never evictable", not MH._idle_evictable(s, time.monotonic()))
    s._idle_timeout = 10.0
    s.process = None
    check("idle: dead slot not evictable", not MH._idle_evictable(s, time.monotonic()))

    # ── touch_if_loaded: alias-stripped name match refreshes the slot ────────
    mgr = FakeManager()
    slot = _live_slot(0, "Test-Model:latest")
    slot.last_used = 0.0
    mgr._slots = [slot]
    check("touch: alias name hits the loaded slot",
          await mgr.touch_if_loaded("Test-Model#2") is True)
    check("touch: last_used refreshed via touch()", slot.last_used > 0.0)
    check("touch: unknown model returns False",
          await mgr.touch_if_loaded("other-model") is False)

    # ── get_port_for ─────────────────────────────────────────────────────────
    check("port: running slot exposes its port",
          mgr.get_port_for("Test-Model") == slot.port)
    loading = ModelSlot(1)
    loading.model = "m2"
    loading._loading = True
    mgr._slots.append(loading)
    check("port: loading slot still reports its port",
          mgr.get_port_for("m2") == loading.port)
    check("port: unknown model -> None", mgr.get_port_for("nope") is None)

    # ── unpin ────────────────────────────────────────────────────────────────
    slot.pinned = True
    slot._idle_timeout = 30.0
    await mgr.unpin("Test-Model", keep_alive_seconds=120.0)
    check("unpin: pin cleared", slot.pinned is False)
    check("unpin: keep-alive timeout applied", slot._idle_timeout == 120.0)

    # ── list_loaded: live + rehabitable orphans, dead ports dropped ─────────
    mgr2 = FakeManager()
    live = _live_slot(0, "m1")
    orphan = ModelSlot(1)
    orphan.model = "m2"
    dead = ModelSlot(2)
    dead.model = "m3"
    blank = ModelSlot(3)
    mgr2._slots = [live, orphan, dead, blank]
    mgr2._alive_ports = {orphan.port}
    loaded = await mgr2.list_loaded()
    names = {e["name"] for e in loaded}
    check("list_loaded: running slot listed", "m1" in names, f"{names}")
    check("list_loaded: live orphan port rehabilitated", "m2" in names, f"{names}")
    check("list_loaded: dead orphan port dropped", "m3" not in names, f"{names}")
    check("list_loaded: empty slot skipped", "" not in names and len(names) == 2)
    m2e = next(e for e in loaded if e["name"] == "m2")
    check("list_loaded: orphan entry not marked loading", m2e["loading"] is False)

    # ── _idle_monitor: evicts a stale unpinned slot, metrics bump ───────────
    killed = []
    ev = asyncio.Event()

    async def _recorder(slot):
        killed.append(slot.slot_id)
        ev.set()

    patch.set(MH, "_kill_slot_async", _recorder)
    patch.set(MH.random, "uniform", lambda lo, hi: -30.0)  # sleep(30-30) -> 0
    mgr3 = FakeManager()
    stale = _live_slot(0, "stale-model")
    stale._idle_timeout = 10.0
    stale.last_used = time.monotonic() - 1000.0
    fresh = _live_slot(1, "fresh-model")
    fresh._idle_timeout = 10.0
    fresh.last_used = time.monotonic()
    mgr3._slots = [stale, fresh]
    mgr3.start_idle_monitor()
    try:
        await asyncio.wait_for(ev.wait(), timeout=5.0)
        check("monitor: stale slot killed", stale.slot_id in killed, f"{killed}")
        check("monitor: fresh slot untouched", fresh.slot_id not in killed)
        check("monitor: eviction metrics counted",
              mgr3.metrics.get("evictions_total", 0) >= 1
              and mgr3.metrics.get("evictions_idle", 0) >= 1,
              f"{mgr3.metrics}")
    finally:
        await _cancel(mgr3._idle_task)

    # ── _idle_monitor: touch between scan and kill saves the slot ───────────
    ev2 = asyncio.Event()

    async def _recorder2(slot):
        killed.append(slot.slot_id)
        ev2.set()

    async def _touching_prefetch():
        stale2.touch()  # runs between the scan and the kill re-check

    mgr4 = FakeManager()
    stale2 = _live_slot(0, "stale-model")
    stale2._idle_timeout = 10.0
    stale2.last_used = time.monotonic() - 1000.0
    mgr4._slots = [stale2]
    patch.set(MH, "_kill_slot_async", _recorder2)
    killed.clear()  # separate from the first monitor test's kills
    mgr4._trigger_pending_prefetch = _touching_prefetch
    mgr4._idle_task = asyncio.create_task(mgr4._idle_monitor())
    await asyncio.sleep(0.2)  # a few loop iterations with sleep(0)
    check("monitor: slot touched after scan is not evicted", killed == [])
    await _cancel(mgr4._idle_task)

    # ── _idle_monitor: iteration error does not kill the monitor ────────────
    async def _broken_prefetch():
        raise RuntimeError("prefetch exploded")

    mgr5 = FakeManager()
    mgr5._slots = []
    mgr5._trigger_pending_prefetch = _broken_prefetch
    task5 = asyncio.create_task(mgr5._idle_monitor())
    await asyncio.sleep(0.1)
    check("monitor: loop survives a failed iteration", not task5.done())
    await _cancel(task5)

    # ── shutdown: cancels monitor + prefetch, kills slots, closes client ────
    patch.set(MH, "_kill_slot_async", _recorder)
    mgr6 = FakeManager()
    killed.clear()
    ev.clear()
    a = _live_slot(0, "mA")
    b = _live_slot(1, "mB")
    a.process = None  # Windows pid sweep only touches tracked pids
    b.process = None
    mgr6._slots = [a, b]
    mgr6._pending_prefetch = [("mA", 4096)]
    mgr6.start_idle_monitor()
    pf = asyncio.create_task(asyncio.sleep(3600))
    mgr6._prefetch_tasks.add(pf)
    client = FakeHTTPClient(fail=False)
    mgr6._shared_http_client = client
    await mgr6.shutdown()
    check("shutdown: idle monitor task cancelled", mgr6._idle_task.done())
    check("shutdown: prefetch task cancelled", pf.done())
    check("shutdown: prefetch queue cleared", mgr6._pending_prefetch == [])
    check("shutdown: prefetch set cleared", mgr6._prefetch_tasks == set())
    check("shutdown: every slot killed", killed == [a.slot_id, b.slot_id],
          f"{killed}")
    check("shutdown: shared client closed", client.is_closed is True)
    check("shutdown: client reference released", mgr6._shared_http_client is None)

    # ── shutdown edge: aclose() raising still releases the client ───────────
    class _BrokenClose(FakeHTTPClient):
        async def aclose(self):
            self.is_closed = False
            raise RuntimeError("boom")

    mgr7 = FakeManager()
    mgr7._slots = [ModelSlot(0)]  # process None -> no pid sweep
    mgr7._shared_http_client = _BrokenClose(fail=False)
    await mgr7.shutdown()
    check("shutdown: failing aclose swallowed, client released",
          mgr7._shared_http_client is None)

    # ── force_kill_all: PID-scoped kills only (no name-wide sweep) ──────────
    runs = []

    def _fake_run(cmd, **kw):
        runs.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    patch.set(MH, "_kill_slot_async", _recorder)
    patch.set(MH.subprocess, "run", _fake_run)
    mgr8 = FakeManager()
    tgt = _live_slot(0, "target")
    mgr8._slots = [tgt]
    mgr8._pending_prefetch = [("target", 4096)]
    pid = tgt.process.pid
    res = await mgr8.force_kill_all(include_foreign=False)
    check("kill_all: ok=True returned", res.get("ok") is True)
    check("kill_all: own pid killed by PID",
          any(str(pid) in cmd for cmd in runs), f"{runs}")
    check("kill_all: no name-wide sweep without include_foreign",
          not any("llama-server" in " ".join(cmd) for cmd in runs), f"{runs}")
    check("kill_all: result lists the pid kill",
          res["killed"] == [f"pid:{pid}"], f"{res['killed']}")
    check("kill_all: slot bookkeeping reset",
          tgt.model is None and tgt._loading is False and tgt.pinned is False
          and tgt.in_flight == 0 and tgt._orphan_port is None)
    check("kill_all: ready event set so waiters wake",
          tgt._ready_event.is_set())
    check("kill_all: prefetch queue cleared", mgr8._pending_prefetch == [])

    # ── force_kill_all edge: explicit foreign sweep only on request ─────────
    runs.clear()
    res2 = await mgr8.force_kill_all(include_foreign=True)
    if platform.system() == "Windows":
        swept = any("llama-server.exe" in cmd for cmd in runs)
        check("kill_all: explicit foreign sweep ran taskkill /IM", swept,
              f"{runs}")
        check("kill_all: foreign sweep reported",
              "taskkill:llama-server.exe" in res2["killed"], f"{res2['killed']}")
    else:
        check("kill_all: posix foreign sweep attempted",
              any("pkill" in cmd for cmd in runs), f"{runs}")

    # ── vram_status: table-driven numbers, empty slots excluded ─────────────
    patch.set(MH, "_kill_slot_async", _recorder)
    mgr9 = FakeManager()
    vs = _live_slot(0, "qwen3.5:0.8b")
    vs._num_ctx = 0
    vs._n_parallel = 2
    empty = ModelSlot(1)
    mgr9._slots = [vs, empty]
    status = mgr9.vram_status()
    expect = MH._vram_of_ctx("qwen3.5:0.8b", 4096)
    check("vram: single running slot reported",
          len(status["slots"]) == 1 and status["slots"][0]["model"] == "qwen3.5:0.8b",
          f"{status['slots']}")
    check("vram: slot GB matches the VRAM table", status["slots"][0]["vram_gb"] == expect)
    check("vram: total equals slot sum", status["total_gb"] == round(expect, 2))
    check("vram: budget constant surfaced", status["budget_gb"] == MH.VRAM_BUDGET_GB)
    check("vram: free = budget - total (floored at 0)",
          status["free_gb"] == round(max(0.0, MH.VRAM_BUDGET_GB - expect), 2))

    # ── list_all_models: registry adapter shape ─────────────────────────────
    patch.set(MH, "list_available_models", lambda: ["m/a", "b"])
    check("models: list_available_models mapped to name dicts",
          mgr9.list_all_models() == [{"name": "m/a"}, {"name": "b"}])

    patch.restore()
    return h.finish("manager_health_behavior")


def main():
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
