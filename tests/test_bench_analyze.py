# -*- coding: utf-8 -*-
"""Behavior checks: bench/analyze.py (2026-11-08).

Pure statistics: Wilson interval, exact McNemar, majority verdicts and the
per-category grouping. No model, no files.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bench.analyze import (  # noqa: E402
    majority_verdicts, mcnemar_exact, summarize, wilson,
)
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

_lo, _hi = wilson(50, 100)
check("wilson: 50/100 is centered near 0.5",
      0.39 <= _lo <= 0.42 and 0.58 <= _hi <= 0.61, f" ({_lo:.3f},{_hi:.3f})")
_lo, _hi = wilson(10, 10)
check("wilson: 10/10 upper bound is 1.0 and lower < 1",
      abs(_hi - 1.0) < 1e-9 and _lo < 1.0, f" ({_lo:.3f},{_hi:.3f})")
check("wilson: empty sample is (0,0)", wilson(0, 0) == (0.0, 0.0))

check("mcnemar: 10 vs 0 is significant",
      mcnemar_exact(10, 0) < 0.01, f" ({mcnemar_exact(10, 0):.4f})")
check("mcnemar: no discordant pairs -> p=1", mcnemar_exact(0, 0) == 1.0)
check("mcnemar: symmetric 5 vs 5 -> p=1", mcnemar_exact(5, 5) == 1.0)

_rows = [
    # task A: aider 2/3 (majority pass), direct 1/3 (majority fail)
    {"task": "fix_a", "condition": "aider", "ok": True, "seconds": 1, "tokens": 1},
    {"task": "fix_a", "condition": "aider", "ok": True, "seconds": 1, "tokens": 1},
    {"task": "fix_a", "condition": "aider", "ok": False, "seconds": 1, "tokens": 1},
    {"task": "fix_a", "condition": "direct", "ok": True, "seconds": 1, "tokens": 1},
    {"task": "fix_a", "condition": "direct", "ok": False, "seconds": 1, "tokens": 1},
    {"task": "fix_a", "condition": "direct", "ok": False, "seconds": 1, "tokens": 1},
    # task B: both pass
    {"task": "feature_b", "condition": "aider", "ok": True, "seconds": 1, "tokens": 1},
    {"task": "feature_b", "condition": "direct", "ok": True, "seconds": 1, "tokens": 1},
]
_verdicts = majority_verdicts(_rows)
check("majority: 2/3 passes", _verdicts["aider"]["fix_a"] is True)
check("majority: 1/3 fails", _verdicts["direct"]["fix_a"] is False)
check("majority: 1/1 passes", _verdicts["aider"]["feature_b"] is True)

_sum = summarize(_rows)
check("summary: condition cell counts",
      _sum["conditions"]["aider"]["cells"] == 4
      and _sum["conditions"]["direct"]["cells"] == 4,
      f" ({_sum['conditions']})")
check("summary: paired discordant counts",
      _sum["paired"]["aider vs direct"]["aider_only"] == 1
      and _sum["paired"]["aider vs direct"]["direct_only"] == 0,
      f" ({_sum['paired']})")
check("summary: categories split bugfix/feature",
      _sum["categories"]["aider"].get("bugfix") == 1.0
      and _sum["categories"]["aider"].get("feature") == 1.0,
      f" ({_sum['categories']})")

# 2026-11-08: overlapping result files must not double-vote a cell.
_dup = summarize([
    {"task": "dup", "condition": "raw", "run": 1, "ok": True},
    {"task": "dup", "condition": "raw", "run": 1, "ok": False},
])
check("summary: duplicate (condition, task, run) is deduped",
      _dup["conditions"]["raw"]["cells"] == 1
      and _dup["conditions"]["raw"]["pass_rate"] == 0.0,
      f" ({_dup['conditions']})")

# ── D1 relative block (2026-11-09) ────────────────────────────────────────
# Cells: raw 0/4, raw_files 1/4 (fix_a), hivemind_direct 2/4 (fix_a+fix_c),
# aider 3/4. Tokens: direct 400/cell with 200 cached, 300 net; aider 50/cell.
_rel_rows = [
    {"task": "fix_a", "condition": "raw", "ok": False, "seconds": 1, "tokens": 100},
    {"task": "fix_c", "condition": "raw", "ok": False, "seconds": 1, "tokens": 100},
    {"task": "feature_b", "condition": "raw", "ok": False, "seconds": 1, "tokens": 100},
    {"task": "multi_e", "condition": "raw", "ok": False, "seconds": 1, "tokens": 100},
    {"task": "fix_a", "condition": "raw_files", "ok": True, "seconds": 1, "tokens": 100},
    {"task": "fix_c", "condition": "raw_files", "ok": False, "seconds": 1, "tokens": 100},
    {"task": "feature_b", "condition": "raw_files", "ok": False, "seconds": 1, "tokens": 100},
    {"task": "multi_e", "condition": "raw_files", "ok": False, "seconds": 1, "tokens": 100},
    {"task": "fix_a", "condition": "hivemind_direct", "ok": True, "seconds": 2,
     "tokens": 400, "cached_tokens": 200, "net_tokens": 300, "rounds": 4},
    {"task": "fix_c", "condition": "hivemind_direct", "ok": True, "seconds": 2,
     "tokens": 400, "cached_tokens": 200, "net_tokens": 300, "rounds": 0},
    {"task": "feature_b", "condition": "hivemind_direct", "ok": False, "seconds": 2,
     "tokens": 400, "cached_tokens": 200, "net_tokens": 300, "rounds": 2},
    {"task": "multi_e", "condition": "hivemind_direct", "ok": False, "seconds": 2,
     "tokens": 400, "cached_tokens": 200, "net_tokens": 300, "rounds": 0},
    {"task": "fix_a", "condition": "aider", "ok": True, "seconds": 1, "tokens": 50},
    {"task": "fix_c", "condition": "aider", "ok": True, "seconds": 1, "tokens": 50},
    {"task": "feature_b", "condition": "aider", "ok": True, "seconds": 1, "tokens": 50},
    {"task": "multi_e", "condition": "aider", "ok": False, "seconds": 1, "tokens": 50},
]
_sum = summarize(_rel_rows)
_rel = _sum["relative"]
_direct = _rel["conditions"]["hivemind_direct"]
check("relative: block present with both baselines",
      _rel["baselines_present"] == ["raw", "raw_files"], f" ({_rel.get('baselines_present')})")
check("relative: condition deltas vs raw and raw_files",
      _direct["delta_vs_raw"] == 0.5 and _direct["delta_vs_raw_files"] == 0.25,
      f" ({_direct.get('delta_vs_raw')}/{_direct.get('delta_vs_raw_files')})")
check("relative: per-category deltas (bugfix 1.0 vs raw, 0.5 vs raw_files)",
      _direct["categories"]["bugfix"] == {"pass_rate": 1.0, "delta_vs_raw": 1.0,
                                          "delta_vs_raw_files": 0.5},
      f" ({_direct.get('categories')})")
check("relative: multi-file category carried",
      _direct["categories"].get("multi-file", {}).get("pass_rate") == 0.0
      and _direct["categories"]["multi-file"]["delta_vs_raw"] == 0.0,
      f" ({_direct.get('categories')})")
check("relative: token ratios vs aider reference",
      _direct["tokens"]["net_vs_aider_ratio"] == 6.0
      and _direct["tokens"]["total_vs_aider_ratio"] == 8.0
      and _direct["tokens"]["cached_share"] == 0.5,
      f" ({_direct.get('tokens')})")
check("relative: rounds totals and median",
      _direct["rounds"]["total"] == 6 and _direct["rounds"]["median"] == 1.0,
      f" ({_direct.get('rounds')})")
check("relative: aider reference ratios are 1.0",
      _sum["relative"]["conditions"]["aider"]["tokens"]["net_vs_aider_ratio"] == 1.0,
      f" ({_sum['relative']['conditions']['aider']['tokens']})")
check("relative: legacy summarize keys unchanged",
      set(_sum["conditions"]) == {"raw", "raw_files", "hivemind_direct", "aider"}
      and "paired" in _sum and "categories" in _sum,
      f" ({sorted(_sum)})")

# Missing baseline: drop every raw row -> delta_vs_raw None everywhere.
_no_raw = summarize([r for r in _rel_rows if r["condition"] != "raw"])["relative"]
check("relative: missing raw -> deltas None, absent flagged",
      _no_raw["baselines_present"] == ["raw_files"]
      and all(v["delta_vs_raw"] is None for v in _no_raw["conditions"].values()),
      f" ({_no_raw['baselines_present']})")

# Zero tokens: shares/ratios None, never ZeroDivisionError.
_zero = summarize([
    {"task": "fix_a", "condition": "raw", "ok": True, "seconds": 1, "tokens": 0},
    {"task": "fix_a", "condition": "hivemind_direct", "ok": True, "seconds": 1,
     "tokens": 0, "cached_tokens": 0, "net_tokens": 0},
    {"task": "fix_a", "condition": "aider", "ok": True, "seconds": 1, "tokens": 0},
])["relative"]
check("relative: zero tokens -> None share and ratios",
      _zero["conditions"]["hivemind_direct"]["tokens"]["cached_share"] is None
      and _zero["conditions"]["hivemind_direct"]["tokens"]["net_vs_aider_ratio"] is None,
      f" ({_zero['conditions']['hivemind_direct']['tokens']})")
check("relative: all-pass -> deltas 0.0",
      _zero["conditions"]["hivemind_direct"]["delta_vs_raw"] == 0.0,
      f" ({_zero['conditions']['hivemind_direct']['delta_vs_raw']})")

# All-fail: deltas 0.0, rows without token keys tolerated.
_fail = summarize([
    {"task": "fix_a", "condition": "raw", "ok": False, "seconds": 1},
    {"task": "fix_a", "condition": "hivemind_direct", "ok": False, "seconds": 1},
])["relative"]
check("relative: all-fail -> deltas 0.0 without token keys",
      _fail["conditions"]["hivemind_direct"]["delta_vs_raw"] == 0.0
      and _fail["conditions"]["hivemind_direct"]["tokens"]["total"] == 0,
      f" ({_fail['conditions']['hivemind_direct']})")

# Readable analyzer output carries the relative lines.
import io  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402
from bench.analyze import _print  # noqa: E402

_buf = io.StringIO()
with redirect_stdout(_buf):
    _print(_sum)
_out = _buf.getvalue()
check("relative: printed in readable output",
      "relative" in _out and "d_raw_files" in _out and "net/aider" in _out
      and "6.00x" in _out, f" ({_out[-300:]!r})")
check("relative: category deltas printed",
      "bugfix" in _out and "multi-file" in _out, f" ({_out[-300:]!r})")

sys.exit(h.finish("bench_analyze"))
