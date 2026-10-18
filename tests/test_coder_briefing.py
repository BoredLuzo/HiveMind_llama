"""Coder briefing stage: thinking goes to the coder only while it is small.

Owner decision (2026-10-17): "a planner that thinks 8-16k tokens before a plan
is complete poisoning for the coder" - so the planner's thinking is injected as
"[Plan Briefing — IMPLEMENT THIS]" only up to a token limit; above it the coder
gets the pure plan instead, with one line saying the reasoning was dropped.

Contract (core/duo_runner.coder_briefing_text):
  * limit 1500 (default) -> thinking <= limit is injected as-is ("full");
  * above the limit      -> the plan text (or the subtask list) + a drop note
                            ("plan-only");
  * 0                     -> never inject thinking (always plan-only);
  * -1                    -> old behaviour, always inject;
  * no thinking           -> nothing to inject ("off").

Run: python tests/test_coder_briefing.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.duo_runner import (  # noqa: E402
    BRIEFING_FULL, BRIEFING_OFF, BRIEFING_PLAN_ONLY, _briefing_limit_tokens,
    _est_tokens, coder_briefing_text,
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


# a thinking block of a known size (cpt ~3.92 chars/token)
SMALL = "Check the router file first. " * 20          # ~600 chars, ~150 tokens
BIG = "Speculation about the architecture. " * 900    # ~32k chars, ~8k tokens

# ── limit resolution ───────────────────────────────────────────────────────
check("default limit is 1500", _briefing_limit_tokens({}) == 1500)
check("explicit limit is honoured",
      _briefing_limit_tokens({"duo_coder_briefing_think_tokens": 400}) == 400)
check("0 means plan-only", _briefing_limit_tokens({"duo_coder_briefing_think_tokens": 0}) == 0)
check("-1 means always", _briefing_limit_tokens({"duo_coder_briefing_think_tokens": -1}) == -1)
check("junk falls back to the default",
      _briefing_limit_tokens({"duo_coder_briefing_think_tokens": "lots"}) == 1500)
check("the value is clamped", _briefing_limit_tokens(
    {"duo_coder_briefing_think_tokens": 10**9}) == 200_000)

# ── token estimate ─────────────────────────────────────────────────────────
check("empty text is 0 tokens", _est_tokens("") == 0)
check("the estimate is chars/cpt", 100 <= _est_tokens(SMALL, "any") <= 250,
      f" ({_est_tokens(SMALL, 'any')})")
check("a big block is many tokens", _est_tokens(BIG, "any") > 5000,
      f" ({_est_tokens(BIG, 'any')})")

# ── the rule ───────────────────────────────────────────────────────────────
txt, mode, tok = coder_briefing_text(SMALL, "PLAN: edit router.py", None,
                                    {"duo_coder_briefing_think_tokens": 1500})
check("small thinking is injected whole", mode == BRIEFING_FULL and txt == SMALL.strip())
check("small thinking reports its token count", 0 < tok <= 1500, f" ({tok})")

txt, mode, tok = coder_briefing_text(BIG, "PLAN: edit router.py", None,
                                    {"duo_coder_briefing_think_tokens": 1500})
check("big thinking is NOT injected", mode == BRIEFING_PLAN_ONLY)
check("the pure plan is injected instead", "PLAN: edit router.py" in txt)
check("the drop is explained to the coder",
      "over the 1500-token briefing limit" in txt and "implement the PLAN" in txt)
check("the reasoning itself is gone", "Speculation about the architecture" not in txt)

txt, mode, _ = coder_briefing_text(BIG, "", ["step one: read a.py", "step two: edit a.py"],
                                  {"duo_coder_briefing_think_tokens": 1500})
check("without plan text the subtasks are used", mode == BRIEFING_PLAN_ONLY
      and "step one: read a.py" in txt and "step two: edit a.py" in txt)

txt, mode, _ = coder_briefing_text(SMALL, "PLAN: x", None,
                                  {"duo_coder_briefing_think_tokens": 0})
check("limit 0 always means plan-only", mode == BRIEFING_PLAN_ONLY and "PLAN: x" in txt)

txt, mode, _ = coder_briefing_text(BIG, "PLAN: x", None,
                                  {"duo_coder_briefing_think_tokens": -1})
check("-1 restores the old always-inject behaviour", mode == BRIEFING_FULL and txt == BIG.strip())

txt, mode, tok = coder_briefing_text("", "PLAN: x", None, {})
check("no thinking means nothing to inject", (txt, mode, tok) == ("", BRIEFING_OFF, 0))
txt, mode, _ = coder_briefing_text("   ", "PLAN: x", None, {})
check("whitespace thinking counts as empty", mode == BRIEFING_OFF)

# ── wiring ─────────────────────────────────────────────────────────────────
src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
# 1.4.x: coder_briefing_text was moved to core/duo/preflight.py (pure move) and
# is re-exported from duo_runner - the call sites must still be in the runner.
_pf = (ROOT / "core" / "duo" / "preflight.py").read_text(encoding="utf-8", errors="replace")
# 1.4.x: the definition lives in core/duo/preflight.py - the runner keeps the
# two call sites (normal + agentic), the import and no third occurrence.
check("both coder branches use the helper", src.count("coder_briefing_text(") >= 2)
check("the helper is defined in preflight.py",
      "def coder_briefing_text(" in _pf and "coder_briefing_text," in src)
check("the decision is logged",
      "[CODER-BRIEFING] mode=%s think_tokens=%d limit=%d" in src)
check("the plan text comes from PlannerResult.plan_content",
      'getattr(_plan_result, "plan_content", "")' in src)
check("chunked runs are untouched (subtasks branch first)",
      src.index("elif _subtasks:") < src.index("_brief_txt, _brief_mode, _brief_tok = coder_briefing_text(\n                    _plan_thinking, _plan_content_txt, _subtasks"))

settings = (ROOT / "settings.py").read_text(encoding="utf-8", errors="replace")
check("the setting exists with 1500", '"duo_coder_briefing_think_tokens": 1500' in settings)
html = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
check("the UI control exists", 'id="duo-briefing-think-inp"' in html)
check("the UI writes the setting",
      "postSettings({duo_coder_briefing_think_tokens:S.duoBriefingThink})" in html)
docs = (ROOT / "docs" / "settings.md").read_text(encoding="utf-8", errors="replace")
check("the docs describe it", "duo_coder_briefing_think_tokens" in docs)

print()
print(f"SUMMARY coder_briefing: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
