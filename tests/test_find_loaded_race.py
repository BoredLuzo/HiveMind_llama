# -*- coding: utf-8 -*-
"""F5-RACE (deep audit 2026-11-07): _find_loaded must not clobber a slot that
is claimed while the manager lock is released for the orphan port probe.

backend/manager_process.py LlamaProcessMixin._find_loaded releases self._lock
to await _verify_port_api(port) for orphan slots, then re-acquires and
mutates the slot (stale-port reset: model=None / _loading=False /
_ready_event.clear(), or rehabilitation: _ready_event.set()). Between release
and re-acquire another coroutine (ensure_loaded under the lock) can claim the
very same slot: _loading = True, model = model, slot.process swapped. The old
code only re-checked the MODEL NAME after the probe, so a clobbered in-flight
load lost its bookkeeping and waited out the full 240 s ready timeout.

Fix under test: the probe runs on a snapshot (port + model + slot identity,
_probe_snapshot) and EVERY post-probe mutation is gated on
_probe_snapshot_changed(), which re-validates under the lock (same slot
object still registered, same port, same model, NOT _loading, same process —
still dead if it was dead). On any change the slot is left untouched and the
normal lookup path continues (_rescan_active_slot).

Harness: FakeManager carries ONLY what the mixin touches (a real asyncio.Lock,
real ModelSlot instances); _verify_port_api is a fake that records whether the
manager lock was held WHILE probing and can run a race hook mid-probe (the
claim happens exactly in the released window). No network, no sockets, no
llama processes, no real subprocess objects.

Run: python tests/test_find_loaded_race.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import backend.manager_process as MP  # noqa: E402
from backend.llama_slots import ModelSlot  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

MODEL = "race-model"

ALL_PROBE_LOCKED = []                    # every lock.locked() ever seen probing


class FakeManager(MP.LlamaProcessMixin):
    """Only the state LlamaProcessMixin._find_loaded touches."""

    def __init__(self):
        self._slots = []
        self._lock = asyncio.Lock()          # REAL asyncio.Lock
        self.metrics = {}
        # fake probe behaviour
        self.probe_port_alive = False        # what _verify_port_api answers
        self.probe_locked_seen = []          # lock.locked() AT probe time
        self.probe_ports_seen = []           # ports handed to the probe
        self.race_hook = None                # async fn run mid-probe

    def _metric_inc(self, name):
        self.metrics[name] = self.metrics.get(name, 0) + 1

    async def _verify_port_api(self, port):
        # Hermetic stand-in for the TCP+HTTP probe: it records the lock state
        # DURING the probe (must be released) and can run a race hook (a
        # concurrent claimer mutating the slot) before answering.
        _locked = self._lock.locked()
        self.probe_locked_seen.append(_locked)
        ALL_PROBE_LOCKED.append(_locked)
        self.probe_ports_seen.append(port)
        if self.race_hook is not None:
            hook, self.race_hook = self.race_hook, None
            await hook()
        return self.probe_port_alive


def _dead_process(pid=41000):
    return SimpleNamespace(pid=pid, poll=lambda: 1)      # exited


def _live_process(pid=42000):
    return SimpleNamespace(pid=pid, poll=lambda: None)   # running


def _orphan_slot(mgr, model=MODEL):
    """Probe-branch-A slot: bookkeeping says running (orphan marker) but the
    port no longer serves (process died externally)."""
    s = ModelSlot(1)
    s.model = model
    s.process = _dead_process()
    s._orphan_port = s.port
    mgr._slots.append(s)
    return s


def _stale_slot(mgr, model=MODEL):
    """Probe-branch-B slot: NOT running (dead process, no orphan marker),
    NOT loading — the rehabilitation candidate."""
    s = ModelSlot(2)
    s.model = model
    s.process = _dead_process()
    mgr._slots.append(s)
    return s


def _claim_hook(mgr, slot, model=MODEL, live=True):
    """Race hook: another coroutine (ensure_loaded under the lock) claims the
    slot while _find_loaded probes with the lock released."""
    async def _hook():
        async with mgr._lock:            # claimer holds the manager lock
            slot.model = model
            slot._loading = True
            if live:
                slot.process = _live_process()
    return _hook


def _steal_hook(mgr, slot):
    """Race hook: the slot is re-claimed for a DIFFERENT model mid-probe."""
    async def _hook():
        async with mgr._lock:
            slot.model = "other-model"
            slot.process = _live_process(43001)
    return _hook


async def amain():

    # ── 1. orphaned slot, dead port -> probe outside the lock, reset under it ─
    mgr = FakeManager()
    s = _orphan_slot(mgr)
    s._ready_event.set()                 # stale junk a reset must clear
    async with mgr._lock:                # caller holds the lock (ensure_loaded)
        out = await mgr._find_loaded(MODEL)
    check("A-dead: returns None (slot discarded, lookup continues)", out is None)
    check("A-dead: slot.model cleared under the lock", s.model is None)
    check("A-dead: orphan marker cleared", s._orphan_port is None)
    check("A-dead: _loading stays False", s._loading is False)
    check("A-dead: ready event cleared", not s._ready_event.is_set())
    check("A-dead: probe ran with the lock released", mgr.probe_locked_seen == [False],
          f"({mgr.probe_locked_seen})")
    check("A-dead: probed the snapshot port", mgr.probe_ports_seen == [s.port])

    mgr = FakeManager()
    s = _stale_slot(mgr)
    s._ready_event.set()
    async with mgr._lock:
        out = await mgr._find_loaded(MODEL)
    check("B-dead: returns None", out is None)
    check("B-dead: slot.model cleared (reset under lock)", s.model is None)
    check("B-dead: ready event cleared", not s._ready_event.is_set())
    check("B-dead: no rehabilitation metric on a reset",
          mgr.metrics.get("orphan_rehabilitations", 0) == 0)
    check("B-dead: probe ran with the lock released", mgr.probe_locked_seen == [False])

    # ── 2. alive port: the slot is returned, nothing is reset ────────────────
    mgr = FakeManager()
    s = _orphan_slot(mgr)
    mgr.probe_port_alive = True
    async with mgr._lock:
        out = await mgr._find_loaded(MODEL)
    check("A-alive: returns the slot", out is s)
    check("A-alive: slot.model untouched", s.model == MODEL)
    check("A-alive: no reset (orphan marker kept)", s._orphan_port == s.port)
    check("A-alive: ready event set for the caller", s._ready_event.is_set())
    check("A-alive: probe ran with the lock released", mgr.probe_locked_seen == [False])

    mgr = FakeManager()
    s = _stale_slot(mgr)
    mgr.probe_port_alive = True
    async with mgr._lock:
        out = await mgr._find_loaded(MODEL)
    check("B-alive: returns the slot (rehabilitated)", out is s)
    check("B-alive: slot.model untouched", s.model == MODEL)
    check("B-alive: rehabilitation counted", mgr.metrics.get("orphan_rehabilitations") == 1)
    check("B-alive: probe ran with the lock released", mgr.probe_locked_seen == [False])

    # ── 3. race: slot claimed while the probe is in flight ───────────────────
    # 3a. Branch A, stale port, claim mid-probe. The OLD code reset the slot
    #     (model=None, _loading=False, ready cleared) and clobbered the
    #     in-flight load; the fixed code must leave the slot alone.
    mgr = FakeManager()
    s = _orphan_slot(mgr)
    s._ready_event.set()
    mgr.race_hook = _claim_hook(mgr, s)
    async with mgr._lock:
        out = await mgr._find_loaded(MODEL)
    check("race-A: slot NOT reset (model intact)", s.model == MODEL)
    check("race-A: in-flight claim NOT clobbered (_loading stays True)",
          s._loading is True)
    check("race-A: swapped-in live process NOT dropped",
          s.process is not None and s.process.poll() is None)
    check("race-A: ready event NOT cleared", s._ready_event.is_set())
    check("race-A: lookup returns the claimed slot (caller attaches to the load)",
          out is s)
    check("race-A: probe ran with the lock released", mgr.probe_locked_seen == [False])

    # 3b. Branch A, port ALIVE, claim mid-probe: the ready-set mutation is
    #     gated too — no premature ready on a slot another loader claimed.
    mgr = FakeManager()
    s = _orphan_slot(mgr)
    mgr.probe_port_alive = True
    mgr.race_hook = _claim_hook(mgr, s)
    async with mgr._lock:
        out = await mgr._find_loaded(MODEL)
    check("race-A-alive: claimed slot NOT touched (model intact)", s.model == MODEL)
    check("race-A-alive: _loading stays True", s._loading is True)
    check("race-A-alive: lookup returns the claimed slot", out is s)
    check("race-A-alive: probe ran with the lock released", mgr.probe_locked_seen == [False])

    # 3c. Model changed mid-probe (claimed by ANOTHER model): no mutation of
    #     the stolen slot; our model is not loaded -> None.
    mgr = FakeManager()
    s = _orphan_slot(mgr)
    mgr.race_hook = _steal_hook(mgr, s)
    async with mgr._lock:
        out = await mgr._find_loaded(MODEL)
    check("race-model: stolen slot keeps its new model (NOT reset to None)",
          s.model == "other-model")
    check("race-model: no active slot for our model -> None", out is None)
    check("race-model: probe ran with the lock released", mgr.probe_locked_seen == [False])

    # 3d. Branch B race: the FREE slot is claimed for OUR model during the
    #     probe (loader bound to the port not yet) — a stale reset would set
    #     model=None / clear the ready event of that in-flight load.
    mgr = FakeManager()
    s = _stale_slot(mgr)
    s._ready_event.set()
    mgr.race_hook = _claim_hook(mgr, s)
    async with mgr._lock:
        out = await mgr._find_loaded(MODEL)
    check("race-B: claimed slot NOT reset (model intact)", s.model == MODEL)
    check("race-B: _loading stays True", s._loading is True)
    check("race-B: ready event NOT cleared", s._ready_event.is_set())
    check("race-B: lookup returns the claimed slot", out is s)
    check("race-B: probe ran with the lock released", mgr.probe_locked_seen == [False])

    # ── 4. re-validation conditions (unit, directly on the helper) ───────────
    mgr = FakeManager()
    s = _orphan_slot(mgr)
    snap = MP._probe_snapshot(s)
    check("snap: records port/model/process/dead",
          snap[0] == s.port and snap[1] == MODEL and snap[2] is s.process and snap[3] is True)
    check("revalidate: unchanged slot passes", not mgr._probe_snapshot_changed(s, MODEL, snap))

    mgr._slots.clear()
    check("revalidate: slot left the registry -> changed",
          mgr._probe_snapshot_changed(s, MODEL, snap))
    mgr._slots.append(s)

    s._loading = True
    check("revalidate: claimed slot (_loading) -> changed",
          mgr._probe_snapshot_changed(s, MODEL, snap))
    s._loading = False

    s.model = "other-model"
    check("revalidate: model replaced -> changed",
          mgr._probe_snapshot_changed(s, MODEL, snap))
    s.model = MODEL

    old_proc = s.process
    s.process = _live_process()
    check("revalidate: process swapped -> changed",
          mgr._probe_snapshot_changed(s, MODEL, snap))
    s.process = old_proc

    flip = {"ret": 1}                    # dead at snapshot, "resurrected" after
    s.process = SimpleNamespace(pid=44001, poll=lambda: flip["ret"])
    snap2 = MP._probe_snapshot(s)
    check("revalidate: snapshot process is dead", snap2[3] is True)
    flip["ret"] = None
    check("revalidate: snapshot-dead process no longer dead -> changed",
          mgr._probe_snapshot_changed(s, MODEL, snap2))

    # ── 5. unlocked caller still works (LOCK-GUARD, audit 2026-11-03) ────────
    mgr = FakeManager()
    s = _orphan_slot(mgr)
    out = await mgr._find_loaded(MODEL)          # nobody holds the lock here
    check("unlocked-caller: no release-crash, dead port -> reset + None",
          out is None and s.model is None)
    check("unlocked-caller: lock stays free after the call", not mgr._lock.locked())
    check("unlocked-caller: probe saw an unlocked manager", mgr.probe_locked_seen == [False])

    mgr = FakeManager()
    s = _orphan_slot(mgr)
    mgr.probe_port_alive = True
    out = await mgr._find_loaded(MODEL)
    check("unlocked-caller: alive port returns the slot without a reset",
          out is s and s.model == MODEL)

    # ── 6. aggregate: every probe in this suite ran with the lock released ───
    check("aggregate: all 10 probes ran with the manager lock released",
          len(ALL_PROBE_LOCKED) == 10 and not any(ALL_PROBE_LOCKED),
          f"({len(ALL_PROBE_LOCKED)} probes)")


asyncio.run(amain())
sys.exit(h.finish("find_loaded_race"))
