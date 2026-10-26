# -*- coding: utf-8 -*-
"""Chunking fixes 2026-10-27 (owner: chunking/pre_explore stay default-off
but must WORK when enabled):

  1. honest chunking-off status (once, with the real subtask count;
     the old text claimed the run "continues with chunking")
  2. chunk marked done AFTER the auto-test verdict (auto-approve and
     critic paths) - a failing test re-processes the chunk
  3. the LAST chunk passes the critic gate like every other
     (the old `_approved or _is_last` waved it through unreviewed)
  4. iteration budget: verified NOT coupled to subtask count
     (replan bonus is once-only, capped; documented, no code change)
Structural pins - the loop is covered by the executor/duo suites."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


_dr = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")

# 1. honest status: fires once (_di == 0), carries the real count, no lie
# 1a/1b after the live fix: the message is emitted exactly where the subtasks
# are dropped, and it reads the RUN flag (ctx.duo_config.chunking), not the
# setting. Keys on the discard site, not on the chunk loop.
_discard = _dr.index("if not ctx.duo_config.chunking and _subtasks:")
_emit = _dr.index("subtask(s) are skipped")
_clear = _dr.index("_subtasks = []", _discard)
check("1a chunking-off status fires at the discard site",
      _discard < _emit < _clear)
check("1b status names the skipped subtask count",
      "the planner's {len(_subtasks)}" in _dr and "subtask(s) are skipped" in _dr)
check("1f the message does not depend on settings[duo_chunking]",
      'ctx.settings.get("duo_chunking"' not in _dr[_discard:_clear])
# SOURCE-PIN (2026-10-26, live found): the message must read the RUN flag, not
# the setting. The body can set duo_chunking=true while settings.json says
# false; that combination fired the "skipped" text while the chunks ran.
check("1d the skip message lives at the discard site, not in the chunk loop",
      _dr.index("subtask(s) are skipped") < _dr.index("while _di < _n_items"))
check("1e no status reads ctx.settings[\"duo_chunking\"] for that decision",
      'ctx.settings.get("duo_chunking"' not in _dr.split("while _di < _n_items")[1].split("def ")[0][:4000])
# the old string survives only inside the explanatory comment
check("1c old false claim only survives inside the explanatory comment",
      all("chunking (value from start)" not in ln or ln.strip().startswith("#")
          for ln in _dr.splitlines()))

# 2. auto-approve path: mark AFTER commit/test block
_seg_aa = _dr[_dr.index("if _auto_approve:"):][:2200]
check("2a auto-approve: commit runs before mark_chunk_done",
      _seg_aa.index("_auto_commit_chunk") < _seg_aa.index("_cs.mark_chunk_done"))
# critic path: mark AFTER the auto-test call inside _can_finish_round.
# The LAST mark_chunk_done in the segment is the post-test one.
_seg_cr = _dr[_dr.index("_can_finish_round = _approved"):]
_seg_cr = _seg_cr[:_seg_cr.index("if _duo_timed_out")]
check("2b critic path: mark_chunk_done is the last write, after auto-test",
      "_cs.run_auto_test" in _seg_cr
      and _seg_cr.rindex("_cs.mark_chunk_done") > _seg_cr.rindex("_cs.run_auto_test"))
check("2c old pre-test mark in critic path gone",
      "_cs.mark_chunk_done(_subtask)\n"
      "                    critic_issues = _cs.critic_issues" not in _dr)

# 3. last chunk passes the same critic gate
check("3a last chunk no longer auto-passes the critic",
      "(_approved or _is_last) and not _verify_gate_blocked" not in _dr
      and "_can_finish_round = _approved and not _verify_gate_blocked" in _dr)
check("3b _is_last still only means no-next-chunk (break preserved)",
      "if _is_last:\n                    break" in _dr)

# 4. defaults stay off (duo_chunking was True - flipped 2026-10-27)
import settings  # noqa: E402
check("4a duo_chunking default off",
      settings.DEFAULT_SETTINGS.get("duo_chunking") is False)
check("4b pre_explore default off",
      settings.DEFAULT_SETTINGS.get("duo_pre_explore",
                                    settings.DEFAULT_SETTINGS.get("pre_explore")) is False)

print(f"\nchunking_fixes: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
