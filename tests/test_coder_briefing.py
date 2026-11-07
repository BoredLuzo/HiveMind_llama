"""Coder briefing: concrete steps only — never planner wording or reasoning.

PLAN-CONTRACT (2026-11-04): the coder receives a distilled, numbered execution
plan (user role). It must not analyze the planner's markdown ("## Plan /
### Open Tasks") or the planner's thinking as if that came from another model.
Chunked runs already carry the step list via build_chunk_context; the raw plan
is injected only for the first non-chunked coder round.

Run: python tests/test_coder_briefing.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.duo.preflight import (  # noqa: E402
    _inject_plan_into_coder_msgs,
    coder_briefing_text,
)
from core.duo_runner import (  # noqa: E402
    BRIEFING_OFF, BRIEFING_PLAN_ONLY,
)

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


SMALL = "Check the router file first. " * 20
BIG = "Speculation about the architecture. " * 900
PLANNER_MD = ("## Plan: build Tetris\n\n### Open Tasks\n"
              "1. file: index.html | touch: canvas setup\n"
              "2. file: game.js | touch: piece rotation\n")

# ── 1. the coder gets distilled steps, never planner thinking ───────────────
txt, mode, tok = coder_briefing_text(SMALL, PLANNER_MD, None, {}, "m")
check("1a mode is plan-only", mode == BRIEFING_PLAN_ONLY)
check("1b the plan steps survive", "canvas setup" in txt and "piece rotation" in txt)
check("1c planner headings are stripped",
      "## Plan" not in txt and "### Open Tasks" not in txt)
check("1d no planner reasoning", "Check the router file first" not in txt)
check("1e tokens are estimated for the injected text", tok > 0)

txt, mode, _ = coder_briefing_text(BIG, PLANNER_MD, None, {}, "m")
check("2a a huge thinking block is never injected",
      "Speculation about the architecture" not in txt)
check("2b the concrete plan is still there", "canvas setup" in txt)

txt, mode, _ = coder_briefing_text(BIG, "", ["read a.py", "edit a.py"], {}, "m")
check("3a structured subtasks win and are numbered",
      mode == BRIEFING_PLAN_ONLY and "1. read a.py" in txt and "2. edit a.py" in txt)

txt, mode, tok = coder_briefing_text(SMALL, "", [], {}, "m")
check("4a no plan and no subtasks means nothing to inject",
      (txt, mode, tok) == ("", BRIEFING_OFF, 0))

txt, _, _ = coder_briefing_text("only reasoning", "", [], {}, "m")
check("5a reasoning alone is never sent to the coder", txt == "")

# ── 2. injection: once, distilled, user role; chunked runs skip ────────────
class _Plan(SimpleNamespace):
    pass


_plan = _Plan(
    subtasks=["file: index.html | touch: canvas setup",
              "file: game.js | touch: piece rotation"],
    thinking="internal planner reasoning that must not leak",
    plan_content=PLANNER_MD,
)

_msgs: list = []
_ok = _inject_plan_into_coder_msgs(_msgs, _plan, chunking=True,
                                  is_first_outer_round=True)
check("6a chunked runs do not get the raw plan again", _ok is False and _msgs == [])

_ok = _inject_plan_into_coder_msgs(_msgs, _plan, chunking=False,
                                  is_first_outer_round=False)
check("6b later outer rounds do not repeat the plan", _ok is False and _msgs == [])

_ok = _inject_plan_into_coder_msgs(_msgs, _plan, chunking=False,
                                  is_first_outer_round=True)
check("7a the first non-chunked round gets the plan", _ok is True and len(_msgs) == 1)
check("7b it is a user-role execution instruction",
      _msgs and _msgs[0]["role"] == "user"
      and "YOUR EXECUTION PLAN" in _msgs[0]["content"])
check("7c it carries numbered steps, not planner markdown",
      "1. file: index.html" in _msgs[0]["content"]
      and "## Plan" not in _msgs[0]["content"])
check("7d planner reasoning never reaches the coder",
      "internal planner reasoning" not in _msgs[0]["content"])

# ── 3. wiring: no planner-thinking path, no dead setting/UI ────────────────
src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
# split 2.4: the per-round coder-input build lives in duo_round_input.py now
src_ri = (ROOT / "core" / "duo_round_input.py").read_text(encoding="utf-8", errors="replace")
# split 2.6: the inloop planner lives in duo_inloop_planner.py now
src_ip = (ROOT / "core" / "duo_inloop_planner.py").read_text(encoding="utf-8", errors="replace")
src_both = src + src_ri + src_ip
pf = (ROOT / "core" / "duo" / "preflight.py").read_text(encoding="utf-8", errors="replace")
settings = (ROOT / "settings.py").read_text(encoding="utf-8", errors="replace")
html = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")

check("8a both coder branches still use the helper",
      src_both.count("coder_briefing_text(") >= 2)
check("8b no thinking-size limit remains", "_briefing_limit_tokens" not in src
      and "_briefing_limit_tokens" not in pf)
check("8c the dead setting is gone",
      "duo_coder_briefing_think_tokens" not in settings)
check("8d the dead UI control is gone",
      "duo-briefing-think-inp" not in html)
check("8e the header says execution plan", "YOUR EXECUTION PLAN" in src_both)
check("8f the helper lives in preflight and the coder branches call it",
      "def coder_briefing_text(" in pf and "coder_briefing_text(" in src_both)
check("8g the in-loop plan is distilled before the coder sees it",
      "_distilled_execution_steps(None, _inloop_plan_text)" in src_both)
check("8h the raw in-loop plan is not appended anymore",
      '{"role": "assistant", "content": _inloop_plan_text}' not in src)

print()
print(f"SUMMARY coder_briefing: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
