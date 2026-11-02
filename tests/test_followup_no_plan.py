# -*- coding: utf-8 -*-
"""A follow-up continues the previous plan instead of planning again.

Live (2026-11-02): "1. write the HTML … 2. the CSS looks bad, improve it" ran
the planner a second time. The 2B planner at 4k context produced text that did
not parse, the run fell back to generic steps ("clarify scope and acceptance
criteria…"), and the coder got a plan unrelated to the user's sentence.

The follow-up hint (previous task, plan, written files) already reaches the
coder, so the planner phase is skipped when the run is a follow-up.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRE = (ROOT / "core" / "duo" / "_pre_explore.py").read_text(encoding="utf-8")
RUN = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── 1. the flag is derived from a real follow-up ───────────────────────────
check("1a the state carries the follow-up flag",
      'state["_follow_up_active"]' in PRE)
check("1b it is only set when a hint was built",
      'state["_follow_up_active"] = bool(_follow_up_hint)' in PRE)
_i_hint = PRE.index('_is_follow_up = bool(')
_i_flag = PRE.index('state["_follow_up_active"]')
check("1c the hint still requires a previous run + a new task",
      "last_run.get(\"task\")" in PRE[_i_hint:_i_hint + 200]
      and "_cur_sig != _prev_sig" in PRE[_i_hint:_i_hint + 200])
check("1d the hint is built before the flag", _i_hint < _i_flag)

# ── 2. the runner skips planning on a follow-up ────────────────────────────
check("2a the runner reads the flag", '_follow_up_active = bool(state.get("_follow_up_active"))' in RUN)
_cond = RUN.index("and not _planner_skipped and not _follow_up_active")
check("2b the planner branch excludes follow-ups", _cond > 0)
check("2c the skip is announced, not silent",
      "Follow-up: continuing the previous plan" in RUN)

# ── 3. the plumbing is unchanged for normal runs ───────────────────────────
check("3a a normal run still plans when the toggle is on",
      "(ctx.duo_config.chunking or ctx.duo_config.planner) and not ctx.aborted()" in RUN)
check("3b resume still skips the planner", "not _resume_data" in RUN)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
