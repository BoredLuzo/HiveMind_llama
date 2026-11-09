# -*- coding: utf-8 -*-
"""Behavior test: the ctx-shrink round seam (F13, 2026-11-08).

`_ctx_shrink_round` (core/duo_ctx_meter.py) owns the emergency-evict +
mask-before-compress decision block that used to sit inline in
`run_code_duo`. The fake `_decide_context_action` / `_evict_stale_tool_outputs`
/ `_estimate_ctx_tokens` hooks stand in for the real guards; the REAL
`_calib_tokens` runs so the est/guard math is exercised end to end.

Seam pins (this helper is a kwargs + result-carrier, NOT a ps-state helper,
so it is deliberately not registered in test_duo_split_integrity HELPERS):
the runner must call the helper, emit its events, keep the compression
cycle and no longer carry the moved log lines.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.duo_ctx_meter as M  # noqa: E402
from core.duo_ctx_meter import _ctx_shrink_round  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness(pad=True)
check = h.check

_COMPRESS = SimpleNamespace(action="compress", reason="threshold")
_NONE = SimpleNamespace(action="none", reason="below-threshold")
_EMERGENCY = SimpleNamespace(action="emergency_evict", reason="emergency-90")


class _FakeDecide:
    """Returns the canned decisions in order, records the kwargs."""

    def __init__(self, *decisions):
        self._decisions = list(decisions)
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return self._decisions.pop(0)


class _FakeEvict:
    """Returns the canned eviction counts in order, records the kwargs."""

    def __init__(self, *counts, marker=False):
        self._counts = list(counts)
        self.marker = marker
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.marker:
            kw["messages"].append({"role": "user", "content": "[recall marker]"})
        return self._counts.pop(0) if self._counts else 0


@contextmanager
def _patched(decide, evict, estimate):
    saved = (M._decide_context_action, M._evict_stale_tool_outputs,
             M._estimate_ctx_tokens)
    M._decide_context_action = decide
    M._evict_stale_tool_outputs = evict
    M._estimate_ctx_tokens = estimate
    try:
        yield
    finally:
        (M._decide_context_action, M._evict_stale_tool_outputs,
         M._estimate_ctx_tokens) = saved


def _est_fake(*vals):
    seq = list(vals)

    def _est(_msgs):
        return seq.pop(0) if seq else 0
    return _est


def _run(**over):
    kw = dict(
        dtool_ctx=10000, compress_threshold=6000,
        est_tokens=7000, guard_tokens=7000,
        can_compress=True, force_compress_next=False,
        total_tool_rounds=3,
        dtool_msgs=[{"role": "user", "content": "hi"}],
        tool_ctx_lru=SimpleNamespace(),
        coder_real_prompt_tokens=[0],
        coder_est_at_real=[0],
        recent_focus_paths=[],
        mask_enabled=True,
    )
    kw.update(over)
    return _ctx_shrink_round(**kw)


# ── 1. normal compress ─────────────────────────────────────────────────────
_dec = _FakeDecide(_COMPRESS)
_ev = _FakeEvict()
with _patched(_dec, _ev, _est_fake()):
    _r = _run()
check("normal compress: decision carried out",
      _r.compress_ok is True and _r.guard_decision is _COMPRESS)
check("normal compress: only the mask attempt runs (masks 0), no events",
      len(_ev.calls) == 1 and _ev.calls[0]["target_token_budget"] == 6000
      and _r.events == [] and _r.masked_n == 0)
check("normal compress: est/guard untouched",
      _r.est_tokens == 7000 and _r.guard_tokens == 7000)
check("normal compress: decide got the seam kwargs",
      _dec.calls[0]["guard_tokens"] == 7000 and _dec.calls[0]["ctx_tokens"] == 10000
      and _dec.calls[0]["threshold"] == 6000 and _dec.calls[0]["can_compress"] is True
      and _dec.calls[0]["force_compress"] is False)

# ── 2. first-round threshold protection ────────────────────────────────────
_ev2 = _FakeEvict()
with _patched(_FakeDecide(_COMPRESS), _ev2, _est_fake()):
    _r = _run(total_tool_rounds=0)
check("first round: threshold compress suppressed, decision kept",
      _r.compress_ok is False and _r.guard_decision.reason == "threshold")
check("first round: no eviction and no mask fired",
      _ev2.calls == [] and _r.events == [])

# ── 3. emergency eviction (bounded two-pass) ───────────────────────────────
_dec = _FakeDecide(_EMERGENCY, _EMERGENCY, _EMERGENCY)
_ev = _FakeEvict(2, 3, marker=True)
_msgs = [{"role": "user", "content": "hi"}]
_cpt = [12345]
_car = [12000]
with _patched(_dec, _ev, _est_fake(9000, 9500)):
    _r = _run(can_compress=False, dtool_msgs=_msgs,
              coder_real_prompt_tokens=_cpt, coder_est_at_real=_car,
              recent_focus_paths=["src/app.py"])
check("emergency: bounded two-pass evicts twice", len(_ev.calls) == 2)
check("emergency: decide = initial + one re-decide per pass", len(_dec.calls) == 3)
check("emergency: can_compress=False reaches the evict branch",
      all(c["can_compress"] is False for c in _dec.calls))
check("emergency: budgets use the _EVICT_* constants",
      _ev.calls[0]["target_token_budget"] == int(10000 * M._EVICT_TARGET_PCT)
      and _ev.calls[0]["hard_floor_tokens"] == int(10000 * M._EVICT_HARD_FLOOR_PCT)
      and _ev.calls[1]["target_token_budget"] == _ev.calls[0]["target_token_budget"]
      and _ev.calls[1]["hard_floor_tokens"] == _ev.calls[0]["hard_floor_tokens"])
check("emergency: focus paths are protected",
      _ev.calls[0]["protect_paths"] == ["src/app.py"])
check("emergency: same messages list is mutated in place",
      _msgs[-1]["content"] == "[recall marker]" and len(_msgs) == 3)
check("emergency: stale real prompt tokens reset to 0", _cpt[0] == 0 and _car[0] == 0)
check("emergency: est refreshed via calib (real=0 -> raw est)",
      _r.est_tokens == 9500)
check("emergency: guard stays stale across passes (mask-only update)",
      _r.guard_tokens == 7000 and _dec.calls[1]["guard_tokens"] == 7000)
check("emergency: one status event per pass, exact order and text",
      [e["type"] for e in _r.events] == ["status", "status"]
      and [e["content"] for e in _r.events] == [
          "🧹 Emergency context eviction (no compression left): 2 stale tool "
          "output(s) replaced by recall markers.",
          "🧹 Emergency context eviction (no compression left): 3 stale tool "
          "output(s) replaced by recall markers."])
check("emergency: compression stays off", _r.compress_ok is False)

# emergency that resolves after one pass: second pass must not evict again
_ev3 = _FakeEvict(5)
with _patched(_FakeDecide(_EMERGENCY, _NONE), _ev3, _est_fake(8000)):
    _r = _run(can_compress=False)
check("emergency: resolved after pass 1 -> single eviction",
      _r.compress_ok is False and _r.masked_n == 0)
check("emergency: resolved -> exactly one evict and one event",
      len(_ev3.calls) == 1 and len(_r.events) == 1)

# ── 4. mask clears compression when under threshold ────────────────────────
_seen = {}


def _ev_mask(**kw):
    _seen.update(kw)
    return 4


with _patched(_FakeDecide(_COMPRESS), _ev_mask, _est_fake(500)):
    _r = _run()
check("mask: budget is the compress threshold, floor the _EVICT constant",
      _seen.get("target_token_budget") == 6000
      and _seen.get("hard_floor_tokens") == int(10000 * M._EVICT_HARD_FLOOR_PCT))
check("mask clears compression when guard drops under threshold",
      _r.compress_ok is False and _r.masked_n == 4)
check("mask: guard recomputed as max(real, est) with real=0",
      _r.guard_tokens == 500 and _r.est_tokens == 500)
check("mask event: exact type and text",
      _r.events == [{"type": "status",
                     "content": "🧹 Masked 4 stale tool output(s) — no summary needed."}])

# ── 5. mask event when real tokens dominate (mask does NOT clear) ──────────
with _patched(_FakeDecide(_COMPRESS), _FakeEvict(3), _est_fake(500)):
    _r = _run(coder_real_prompt_tokens=[6500], coder_est_at_real=[5000])
check("mask: guard = max(real=6500, calib est=650)",
      _r.guard_tokens == 6500 and _r.est_tokens == 650)
check("mask above threshold keeps compression on",
      _r.compress_ok is True and _r.masked_n == 3 and len(_r.events) == 1)

# ── 6. mask toggle off / mask no-op / mask failure ─────────────────────────
with _patched(_FakeDecide(_COMPRESS), _FakeEvict(), _est_fake()):
    _r = _run(mask_enabled=False)
check("mask toggle off: no eviction call, compression kept",
      _r.compress_ok is True and _r.masked_n == 0 and _r.events == [])

with _patched(_FakeDecide(_COMPRESS), _FakeEvict(0), _est_fake()):
    _r = _run()
check("mask no-op (0 masked): no event, compression kept",
      _r.compress_ok is True and _r.masked_n == 0 and _r.events == []
      and _r.guard_tokens == 7000)


def _ev_boom(**kw):
    raise RuntimeError("boom")


with _patched(_FakeDecide(_COMPRESS), _ev_boom, _est_fake()):
    _r = _run()
check("mask failure is contained (moved try/except), compression kept",
      _r.compress_ok is True and _r.masked_n == 0 and _r.events == [])

# ── seam pins: the runner kept the cycle, the block really moved ───────────
_SRC = (Path(_ROOT / "core" / "duo_runner.py")).read_text(encoding="utf-8")
_RUNSEG = _SRC[_SRC.index("async def run_code_duo"):]
_MSEG = (Path(_ROOT / "core" / "duo_ctx_meter.py")).read_text(encoding="utf-8")
check("seam: run_code_duo calls the shrink helper and emits its events",
      "_shr = _ctx_shrink_round(" in _RUNSEG
      and "for _shr_ev in _shr.events:" in _RUNSEG
      and "_compress_ok = _shr.compress_ok" in _RUNSEG)
check("seam: runner keeps the compression cycle",
      "if _compress_ok:" in _RUNSEG and "_duo_compress_cycle(ctx, _cc_ps)" in _RUNSEG)
check("seam: the moved log lines live only in duo_ctx_meter",
      "[CTX-EVICT-EMERGENCY]" not in _RUNSEG and "[CTX-MASK]" not in _RUNSEG
      and "[CTX-EVICT-EMERGENCY]" in _MSEG and "[CTX-MASK]" in _MSEG)
check("seam: runner no longer evicts or re-decides inline",
      "_evict_stale_tool_outputs(" not in _RUNSEG
      and "_decide_context_action(" not in _RUNSEG)
check("seam: helper is keyword-only with the documented result fields",
      all(p.kind.name == "KEYWORD_ONLY"
          for p in __import__("inspect").signature(_ctx_shrink_round).parameters.values())
      and set(M._CtxShrinkResult().__dataclass_fields__) == {
          "compress_ok", "guard_decision", "est_tokens", "guard_tokens",
          "masked_n", "events"})

sys.exit(h.finish("duo_ctx_shrink"))
