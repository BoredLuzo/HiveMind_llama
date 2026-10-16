"""IN-USE GUARD: the pre-flight may not sacrifice a pinned or in-use slot.

Live failure 2026-10-16 17:57:36 (live/logs/hivemind.log): a coder run had torn
down (its pin was released), its last coder POST was still in flight, and the
pre-flight for another role's 35B load killed that very slot -
[PRE-FLIGHT-EVICT] qwen3.5:4b-mtp (slot 1) sacrificed for load hermes3.6:35b...
The coder round then died with ConnectError instead of a clean refusal.

Rule (owner spec): unpinned/idle slots first; a pinned slot is untouchable; so
is a slot with a round in flight, even after the pin is gone; if nothing can be
freed, the LOAD is refused with role/model/reason instead of killing the coder.

Uses the real LlamaSlot class with a fake process (no server started).

Run: python tests/test_preflight_evict_guard.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


from backend.llama_slots import ModelSlot as LlamaSlot                       # noqa: E402
from backend.manager_load import (_pre_flight_evict_candidates,  # noqa: E402
                                  _slot_evictable)


class _Proc:
    def poll(self):
        return None          # running


def _slot(sid, model, pinned=False, in_flight=0, loading=False, last_used=0.0):
    s = LlamaSlot(sid)
    s.model = model
    s.process = _Proc()
    s.set_pinned(pinned)
    s.in_flight = in_flight
    s._loading = loading
    s.last_used = last_used
    return s


# ── slot semantics ──────────────────────────────────────────────────────────
_s = _slot(0, "coder")
check("fresh slot is evictable", _s.evictable is True)
_s.mark_in_flight()
check("in-flight slot is not evictable", _s.evictable is False)
_s.clear_in_flight()
check("cleared slot is evictable again", _s.evictable is True)
_s.mark_in_flight()
_s.mark_in_flight()
_s.clear_in_flight()
check("mark/clear counts (nested rounds)", _s.in_flight == 1)
_s.clear_in_flight()
_s.clear_in_flight()
check("clear never goes negative", _s.in_flight == 0)
_s.set_pinned(True)
check("pinned slot is not evictable", _s.evictable is False)
_s.set_pinned(False)
_s._loading = True
check("loading slot is not evictable", _s.evictable is False)

# test doubles without .evictable fall back to the same three rules
class _Bare:
    is_running = True
    pinned = False
    _loading = False
    in_flight = 0
    last_used = 1.0


_b = _Bare()
check("bare double (no evictable attr) accepted", _slot_evictable(_b) is True)
_b.pinned = True
check("bare double pinned -> refused", _slot_evictable(_b) is False)
_b.pinned = False
_b.in_flight = 1
check("bare double in-flight -> refused", _slot_evictable(_b) is False)

# ── candidate selection ─────────────────────────────────────────────────────
_target = _slot(1, "hermes")            # the slot we want to LOAD into
_old = _slot(2, "planner", last_used=100.0)
_new = _slot(3, "coder", last_used=200.0)

check("(a) free slots are candidates, LRU first",
      [s.model for s in _pre_flight_evict_candidates([_target, _old, _new], _target)]
      == ["planner", "coder"])
check("(b) the slot being loaded is never a candidate",
      _target not in _pre_flight_evict_candidates([_target, _old], _target))

_pinned_idle = _slot(4, "pinned-idle", pinned=True, last_used=1.0)
_pinned_busy = _slot(5, "pinned-busy", pinned=True, in_flight=1, last_used=2.0)
_busy = _slot(6, "busy-unpinned", in_flight=1, last_used=3.0)
check("(c) pinned+idle is not a candidate (run holds it)",
      _pinned_idle not in _pre_flight_evict_candidates([_target, _pinned_idle], _target))
check("(d) pinned+in-use is not a candidate",
      _pinned_busy not in _pre_flight_evict_candidates([_target, _pinned_busy], _target))
check("(e) UNPINNED but in-flight is not a candidate either",
      _busy not in _pre_flight_evict_candidates([_target, _busy], _target))
check("(f) nothing evictable -> empty list (load gets refused)",
      _pre_flight_evict_candidates([_target, _pinned_busy, _busy], _target) == [])
check("(g) after the round ends the same slot becomes evictable",
      _busy in (_busy.clear_in_flight() or
                _pre_flight_evict_candidates([_target, _busy], _target)))
check("(h) after run end a released pin is evictable",
      _pinned_idle in (_pinned_idle.set_pinned(False) or
                       _pre_flight_evict_candidates([_target, _pinned_idle], _target)))

# ── wiring ──────────────────────────────────────────────────────────────────
_src_ml = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8", errors="replace")
_src_duo = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
check("pre-flight uses the guarded candidate helper",
      "_cands = _pre_flight_evict_candidates(self._slots, slot)" in _src_ml)
check("the old inline list comprehension is gone",
      "and not s.pinned and not getattr(s, \"_loading\", False)" not in _src_ml)
check("refusal names model, ctx and the blocking slots",
      "[PRE-FLIGHT-REFUSE]" in _src_ml and "blocked by " in _src_ml)
check("duo marks the coder slot in-flight for the round",
      "_inuse_slot.mark_in_flight()" in _src_duo)
check("duo clears the mark on success", "_inuse_slot.clear_in_flight()" in _src_duo)
check("duo clears the mark on the failure path too",
      _src_duo.count("_inuse_slot.clear_in_flight()") >= 2)

print()
print(f"SUMMARY preflight_evict_guard: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
