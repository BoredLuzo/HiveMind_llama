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
from core.duo_ctx_meter import _ctx_meter_round  # noqa: E402
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
finally:
    M._lsm._slots = _orig_slots

sys.exit(h.finish("duo_ctx_meter"))
