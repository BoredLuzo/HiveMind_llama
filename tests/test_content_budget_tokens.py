"""Content budget: real overheads instead of the hardcoded 800/1200.

Construction site #1 from the pre-release audit. compute_content_budget()
subtracted a fixed 800 tokens for the system prompt and a fixed 1200 for the
tool definitions. The tool schema is not 1200 tokens: the derived schema budget
is SCHEMA_FRAC (0.25) x n_ctx, i.e. ~2050 tokens at 8k - so the planner/explore
content window was over-committed at exactly the small contexts where it hurts.

Now the duo run passes measured numbers (core/budgets.tokens_for_text on the
real coder prompt constant and on the tool-schema JSON, capped by the schema
budget that run will allow). Callers that pass nothing keep the old behaviour.

Pins:
  (a) tokens_for_text maths + fallbacks
  (b) compute_content_budget with None keeps the legacy numbers (back-compat)
  (c) measured overheads shrink the content window at 8k and 16k
  (d) the floor of 2048 still applies
  (e) source guard: the duo run computes and passes both numbers

Run: python tests/test_content_budget_tokens.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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


import core.budgets as B                                        # noqa: E402
from hive_functions.ctx_utils import compute_content_budget      # noqa: E402
from tools.definitions import _get_inline_tools                  # noqa: E402
from hive_functions.prompts import PROMPTS                       # noqa: E402

# ── (a) the measurement helper ──────────────────────────────────────────────
check("tokens_for_text = chars / cpt", B.tokens_for_text("x" * 1000, "m", cpt=4.0) == 250)
check("tokens_for_text falls back to the model EMA",
      B.tokens_for_text("x" * int(1000), "unknown-model") == int(1000 / B.CPT_DEFAULT))
check("empty text = 0", B.tokens_for_text("", "m") == 0)
check("junk cpt falls back to the default",
      B.tokens_for_text("x" * 392, "m", cpt=0) == int(392 / B.CPT_DEFAULT))
check("None text tolerated", B.tokens_for_text(None, "m") == 0)

# ── (b) back-compat ─────────────────────────────────────────────────────────
check("None/None keeps the legacy numbers",
      compute_content_budget(8192, None, None) == compute_content_budget(8192))
check("explicit legacy values identical",
      compute_content_budget(8192, 800, 1200) == compute_content_budget(8192))

# ── (c) measured overheads shrink the window ────────────────────────────────
_tools = _get_inline_tools(include_websearch=False, mode="duo_full")
_schema_json = json.dumps(_tools)
_prompt_txt = max(str(PROMPTS.get("duo_coder_execution") or ""),
                  str(PROMPTS.get("duo_coder_autonomous") or ""), key=len)
_sys_tok = B.tokens_for_text(_prompt_txt, "test-model")
for _ctx in (8192, 16384):
    _cap = B.schema_budget_for(_ctx, "test-model")
    _chars = min(len(_schema_json), _cap) if _cap > 0 else len(_schema_json)
    _tool_tok = B.tokens_for_text("x" * _chars, "test-model")
    _old = compute_content_budget(_ctx)
    _new = compute_content_budget(_ctx, system_prompt_tokens=_sys_tok,
                                  tool_def_tokens=_tool_tok)
    check(f"ctx {_ctx}: schema budget caps the tool estimate",
          _chars <= _cap or _cap == 0, f" ({_chars} <= {_cap})")
    check(f"ctx {_ctx}: measured budget is smaller than the legacy one",
          _new <= _old, f" ({_new} <= {_old})")
    check(f"ctx {_ctx}: tool estimate is well above the old 1200 assumption",
          _tool_tok > 1200, f" ({_tool_tok})")

# ── (d) floor ───────────────────────────────────────────────────────────────
check("floor of 2048 still applies",
      compute_content_budget(8192, 10 ** 9, 10 ** 9) == 2048)

# ── (e) wiring ──────────────────────────────────────────────────────────────
_src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
check("duo run measures the coder prompt", "_budg_ov.tokens_for_text(_ov_prompt_txt, exec_mdl)" in _src)
check("duo run measures the schema JSON", "len(json.dumps(_xtools_ws or []))" in _src)
check("duo run caps it by the schema budget", "schema_budget_for(int(_exec_ctx or 16384), exec_mdl)" in _src)
check("both numbers reach compute_content_budget",
      "system_prompt_tokens=_ov_sys_tok or None" in _src
      and "tool_def_tokens=_ov_tool_tok or None" in _src)
check("failure falls back to the legacy path",
      "_ov_sys_tok = _ov_tool_tok = 0" in _src)

print()
print(f"SUMMARY content_budget_tokens: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
