# -*- coding: utf-8 -*-
"""Behavior test: the ctx-meter / SWA round seam (F13, 2026-11-08)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.duo_ctx_meter as M  # noqa: E402
from core.duo_ctx_meter import _ctx_meter_round, _ctx_shrink_round  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _run(**over):
    kw = dict(dtool_ctx=10000, dport=123, est_tokens=5000,
              real_prompt_tokens=4800, real_cached_tokens=2000,
              guard_tokens=5000, ctx_compressions=0, max_compressions=3,
              ctx_pressure_peak=0.0, swa_triggered_compress=False,
              force_compress_next=False, ctx_critical_warned=False,
              ctx_pressure_warned=False)
    kw.update(over)
    return _ctx_meter_round(**kw)


_r = _run(dtool_ctx=0)
check("no ctx: no meter, no notices",
      not _r.events and not _r.messages and _r.ctx_pressure_peak == 0.0)

_r = _run()
_meter = next(e for e in _r.events if e["type"] == "ctx_meter")
check("meter carries real/cached/limit values",
      _meter["real_tokens"] == 4800 and _meter["cached_tokens"] == 2000
      and _meter["ctx_limit"] == 10000 and _meter["swa_window"] == 0)
check("pressure peak is tracked", abs(_r.ctx_pressure_peak - 0.5) < 1e-9)

_r = _run(guard_tokens=8600)
check("85% appends the critical notice once",
      len(_r.messages) == 1 and "CTX CRITICAL" in _r.messages[0]["content"]
      and _r.ctx_critical_warned is True)

_r = _run(guard_tokens=6100)
check("60% appends the pressure notice once",
      len(_r.messages) == 1 and "[CTX: ~61% full]" in _r.messages[0]["content"]
      and _r.ctx_pressure_warned is True)

_orig_slots = M._lsm._slots
try:
    M._lsm._slots = [SimpleNamespace(port=123, swa_window=1000)]
    _r = _run(est_tokens=900)
    _meter = next(e for e in _r.events if e["type"] == "ctx_meter")
    check("SWA warning is emitted and forces compression",
          _meter["swa_window"] == 1000 and _meter["swa_warning"] is True
          and any(e.get("code") == "swa_reprefill_zone" for e in _r.events)
          and _r.swa_triggered_compress is True
          and _r.force_compress_next is True)

    _r = _run(est_tokens=900, swa_triggered_compress=True)
    check("SWA force-compress fires only once",
          _r.swa_triggered_compress is True and _r.force_compress_next is False)

    _r = _run(est_tokens=900, ctx_compressions=3)
    check("SWA warning does not force compression at the cap",
          _r.force_compress_next is False)

    M._lsm._slots = [SimpleNamespace(port=123, swa_window=1024)]
    _r0_hyb = _run(dtool_ctx=65000, est_tokens=3797, total_tool_rounds=0)
    _m0_hyb = next(e for e in _r0_hyb.events if e["type"] == "ctx_meter")
    _r1_hyb = _run(
        dtool_ctx=65000, est_tokens=4200, total_tool_rounds=1,
        swa_triggered_compress=_r0_hyb.swa_triggered_compress,
    )
    check("Round-0 hybrid SWA guard suppresses futile warning/compress and suppresses Round-1 follow-up",
          _m0_hyb["swa_warning"] is False
          and not any(e.get("code") == "swa_reprefill_zone" for e in _r0_hyb.events)
          and _r0_hyb.force_compress_next is False
          and _r0_hyb.swa_triggered_compress is True
          and _r1_hyb.force_compress_next is False)

    M._lsm._slots = [SimpleNamespace(port=123, swa_window=8192)]
    _r0_swa = _run(dtool_ctx=65000, est_tokens=3500, total_tool_rounds=0)
    _r2_swa = _run(
        dtool_ctx=65000, est_tokens=7200, total_tool_rounds=2,
        swa_triggered_compress=_r0_swa.swa_triggered_compress,
    )
    check("Genuine SWA model does not pre-trigger in Round 0 and triggers normally in Round 2",
          _r0_swa.swa_triggered_compress is False
          and _r0_swa.force_compress_next is False
          and next(e for e in _r2_swa.events if e["type"] == "ctx_meter")["swa_warning"] is True
          and _r2_swa.force_compress_next is True)
finally:
    M._lsm._slots = _orig_slots


class _FakeMaskLRU:
    def __init__(self, entries):
        self._entries = [dict(e) for e in entries]

    def candidates(self):
        return [e for e in self._entries if not e.get("evicted")]

    def mark_evicted(self, idx):
        for e in self._entries:
            if e.get("idx") == idx:
                e["evicted"] = True


# ── Mask-First honors cache_horizon & resets stale real tokens ──────────────
_prefix_payload = "PREFIX_KEEP " * 60
_tail_payload = "TAIL_DROP " * 400
_mask_msgs = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "TASK"},
    {"role": "tool", "name": "search_code", "content": _prefix_payload},
    {"role": "tool", "name": "search_code", "content": _tail_payload},
]
_mask_lru = _FakeMaskLRU([
    {"idx": 2, "path": "a.py", "kind": "search_code"},
    {"idx": 3, "path": "b.py", "kind": "search_code"},
])
_real_pt = [900]
_est_real = [880]
_sr = _ctx_shrink_round(
    dtool_ctx=1000,
    compress_threshold=700,
    est_tokens=900,
    guard_tokens=900,
    can_compress=True,
    force_compress_next=False,
    total_tool_rounds=1,
    dtool_msgs=_mask_msgs,
    tool_ctx_lru=_mask_lru,
    coder_real_prompt_tokens=_real_pt,
    coder_est_at_real=_est_real,
    recent_focus_paths=[],
    mask_enabled=True,
    cache_horizon=3,
)
check("mask-first respects cache_horizon (prefix kept, tail masked)",
      _sr.masked_n == 1
      and _mask_msgs[2]["content"] == _prefix_payload
      and "[System: Content of" in _mask_msgs[3]["content"],
      f"masked={_sr.masked_n}")
check("mask-first resets stale real prompt tokens and skips LLM compression",
      _real_pt[0] == 0 and _sr.guard_tokens <= 700 and _sr.compress_ok is False,
      f"real={_real_pt[0]} guard={_sr.guard_tokens} ok={_sr.compress_ok}")

# ── Emergency-Evict updates guard_tokens before re-deciding ──────────────────
_em_msgs = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "TASK"},
    {"role": "tool", "name": "search_code", "content": "X " * 1500},
]
_em_lru = _FakeMaskLRU([{"idx": 2, "path": "x.py", "kind": "search_code"}])
_em_real = [1100]
_em_est = [1050]
_er = _ctx_shrink_round(
    dtool_ctx=1000,
    compress_threshold=700,
    est_tokens=1050,
    guard_tokens=1100,
    can_compress=False,
    force_compress_next=False,
    total_tool_rounds=1,
    dtool_msgs=_em_msgs,
    tool_ctx_lru=_em_lru,
    coder_real_prompt_tokens=_em_real,
    coder_est_at_real=_em_est,
    recent_focus_paths=[],
    mask_enabled=True,
    cache_horizon=0,
)
check("emergency-evict updates guard_tokens and clears emergency state",
      _em_real[0] == 0 and _er.guard_tokens < 500 and _er.guard_decision.action != "emergency_evict",
      f"real={_em_real[0]} guard={_er.guard_tokens} action={_er.guard_decision.action}")

sys.exit(h.finish("duo_ctx_meter"))
