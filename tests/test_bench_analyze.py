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

sys.exit(h.finish("bench_analyze"))
