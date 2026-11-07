# -*- coding: utf-8 -*-
"""F2 (deep audit 2026-11-07): _enforce_vram_budget lock scope.

The committed-VRAM guard used to hold self._lock across the 5s worker wait
AND both LRU kills (each up to ~10s via to_thread) - every manager op
(ensure_loaded/prefetch/touch/unpin) queued behind a role switch for up to
~15s. The event loop no longer freezes (kill is off-loop), but the lock
scope had to shrink too:

    lock  -> snapshot committed + need_wait            -> unlock
    outside-> sleep 5s (only with active workers)
    lock  -> recompute -> select victim                -> unlock
    outside-> kill victim (lock NOT held)
    lock  -> recompute -> still over? warn + select v2 -> unlock
    outside-> kill v2

These checks run the REAL LlamaLoadMixin._enforce_vram_budget against a
fake manager: patched VRAM math forces over-budget, a scripted _evict_lru
returns the victims, and fake _kill_slot_async / asyncio.sleep record
whether self._lock is held at kill/sleep time. No real sleeps, no server.

Run: python tests/test_manager_vram_lock.py
"""
import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check

import backend.manager_load as ml                                # noqa: E402
from backend.manager_load import LlamaLoadMixin                  # noqa: E402

_VG = logging.getLogger("vram_guard")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


_cap = _Capture()
_VG.addHandler(_cap)
_VG.setLevel(logging.INFO)


def _slot(sid, model, running=True):
    """Slot double with only what the guard touches (SimpleNamespace keeps
    the fake kills free of ModelSlot.process/taskkill machinery)."""
    return SimpleNamespace(model=model, slot_id=sid, _num_ctx=4096,
                           is_running=running)


class _FakeManager(LlamaLoadMixin):
    """Real guard logic, fake state: _slots, _lock and a scripted _evict_lru.

    Every sleep/selection/kill appends ("sleep"|"select"|"kill", ...) to
    .trace with the lock state AT THAT MOMENT, so the assertions pin the
    lock scope end to end.
    """

    def __init__(self, slots, victims):
        self._slots = slots
        self._lock = asyncio.Lock()
        self._victims = list(victims)
        self.trace = []

    def _evict_lru(self, exclude_slot=None):
        victim = self._victims.pop(0) if self._victims else None
        self.trace.append(("select", victim, self._lock.locked(), exclude_slot))
        return victim

    def _metric_inc(self, *a, **k):
        pass


def _run_guard(mgr, slot, budget, vram_of, on_sleep=None):
    """Run the real _enforce_vram_budget hermetically.

    Patches the VRAM math in backend.manager_load (the guard reads its
    globals at call time), fakes _kill_slot_async (records lock state,
    then clears the victim's model exactly like ModelSlot.kill does) and
    fakes asyncio.sleep (records lock state, no real waiting).
    """
    async def _fake_kill(victim):
        mgr.trace.append(("kill", victim, mgr._lock.locked()))
        victim.model = None          # ModelSlot.kill() clears the identity

    async def _fake_sleep(delay):
        mgr.trace.append(("sleep", delay, mgr._lock.locked()))
        if on_sleep is not None:
            on_sleep(delay)

    with mock.patch.object(ml, "GPU_BACKEND", "vulkan"), \
         mock.patch.object(ml, "VRAM_BUDGET_GB", budget), \
         mock.patch.object(ml, "moe_cfg_for", lambda m: None), \
         mock.patch.object(ml, "_vram_of", vram_of), \
         mock.patch.object(ml, "_kill_slot_async", _fake_kill), \
         mock.patch.object(ml.asyncio, "sleep", _fake_sleep):
        asyncio.run(mgr._enforce_vram_budget(slot))


def _msgs():
    out, _cap.msgs = list(_cap.msgs), []
    return out


# The slot being loaded (already claimed: model set, _loading True, not
# running yet) - in the real flow it sits in self._slots and counts toward
# the committed sum.
_target = _slot(9, "new", running=False)

# ── 1. over budget + active worker: wait 5s, kill OUTSIDE the lock ─────────
_peer = _slot(1, "peer")
_m = _FakeManager([_peer, _target], victims=[_peer])
_run_guard(_m, _target, budget=8.0, vram_of=lambda m: {"peer": 10.0, "new": 5.0}.get(m, 1.0))
check("1a wait + select + kill, in that order",
      _m.trace == [("sleep", 5.0, False),
                   ("select", _peer, True, _target),
                   ("kill", _peer, False)],
      f" ({_m.trace})")
check("1b the 5s wait happens OUTSIDE the lock", _m.trace[0][2] is False)
check("1c the victim is selected UNDER the lock (fresh)", _m.trace[1][2] is True)
check("1d the victim is KILLED with the lock NOT held", _m.trace[2][2] is False)
check("1e the guard's own slot is passed as exclude_slot",
      _m.trace[1][3] is _target)
check("1f exactly one kill (no double-kill)",
      sum(1 for e in _m.trace if e[0] == "kill") == 1)
check("1g 'Waiting 5s' still logged",
      any("Waiting 5s" in msg for msg in _msgs()))

# ── 2. over budget, NO active worker: no wait, still evicts ────────────────
_idle = _slot(1, "peer", running=False)
_m = _FakeManager([_idle, _target], victims=[_idle])
_run_guard(_m, _target, budget=8.0, vram_of=lambda m: {"peer": 10.0, "new": 5.0}.get(m, 1.0))
check("2a no sleep without active workers",
      [e for e in _m.trace if e[0] == "sleep"] == [], f" ({_m.trace})")
check("2b eviction still happens (select under lock, kill outside)",
      _m.trace == [("select", _idle, True, _target),
                   ("kill", _idle, False)],
      f" ({_m.trace})")

# ── 3. still over budget after the first kill: second victim ───────────────
_a, _b = _slot(1, "a"), _slot(2, "b")
_m = _FakeManager([_a, _b, _target], victims=[_a, _b])
_run_guard(_m, _target, budget=8.0, vram_of=lambda m: {"a": 6.0, "b": 6.0, "new": 5.0}.get(m, 1.0))
check("3a wait, then two select/kill rounds with the lock released for both kills",
      _m.trace == [("sleep", 5.0, False),
                   ("select", _a, True, _target), ("kill", _a, False),
                   ("select", _b, True, _target), ("kill", _b, False)],
      f" ({_m.trace})")
check("3b both kills happened OUTSIDE the lock",
      all(e[2] is False for e in _m.trace if e[0] == "kill"))
check("3c 'forced extra evict' still logged once",
      sum("forced extra evict" in msg for msg in _msgs()) == 1)

# ── 4. first kill frees enough: NO second victim ────────────────────────────
_a, _b = _slot(1, "a"), _slot(2, "b")
_m = _FakeManager([_a, _b, _target], victims=[_a])
_run_guard(_m, _target, budget=8.0, vram_of=lambda m: 4.0)
check("4a one wait + one eviction only (8GB committed == budget, not over)",
      _m.trace == [("sleep", 5.0, False),
                   ("select", _a, True, _target), ("kill", _a, False)],
      f" ({_m.trace})")
check("4b no 'forced extra evict' when the recompute is under budget",
      all("forced extra evict" not in msg for msg in _msgs()))

# ── 5. under budget: no wait, no eviction ───────────────────────────────────
_m = _FakeManager([_slot(1, "peer"), _target], victims=[])
_run_guard(_m, _target, budget=8.0, vram_of=lambda m: 1.0)
check("5a under budget is a no-op (no sleep/select/kill)",
      _m.trace == [], f" ({_m.trace})")

# ── 6. fresh recompute: pressure resolved during the unlocked wait ─────────
_m = _FakeManager([_slot(1, "peer"), _target], victims=[])
_state = {"waited": False}
_run_guard(_m, _target, budget=8.0,
           vram_of=lambda m: 1.0 if _state["waited"] else 10.0,
           on_sleep=lambda d: _state.__setitem__("waited", True))
check("6a pressure resolved during the wait -> no victim selected, no kill",
      _m.trace == [("sleep", 5.0, False)], f" ({_m.trace})")

sys.exit(h.finish("manager_vram_lock"))
