# -*- coding: utf-8 -*-
"""D6 outcome hints: summarize run_end entries and suggest a better route."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from routing.outcome_hints import summarize_outcomes, suggest_route
from tests._harness import Harness

h = Harness(style="plain", pad=True)
check = h.check


def _run(route, stop):
    return {"event": "run_end", "route": route, "stop_reason": stop}


entries = [
    _run("code_duo", "completed"),
    _run("code_duo", "loop_detected"),
    _run("code_duo", "loop_detected"),
    _run("simple", "completed"),
    _run("simple", "completed"),
    _run("simple", "completed"),
]
stats = summarize_outcomes(entries)
check("code_duo has three runs and one success",
      stats["code_duo"] == {"runs": 3, "ok": 1, "rate": 0.333},
      f"({stats.get('code_duo')})")
check("simple has three runs and three successes",
      stats["simple"]["rate"] == 1.0)
check("a clear gap produces a hint",
      suggest_route(entries, "code_duo") == {
          "from": "code_duo", "to": "simple",
          "from_rate": 0.333, "to_rate": 1.0,
          "runs": {"from": 3, "to": 3},
      })
check("no hint when the current route is already best",
      suggest_route(entries, "simple") is None)
check("no hint below three runs",
      suggest_route(entries[:2] + entries[3:5], "code_duo") is None)
check("non-run_end entries and unknown routes are ignored",
      summarize_outcomes([{"event": "planner"}, _run("x", "")]) == {})

sys.exit(h.finish("outcome_hints"))
