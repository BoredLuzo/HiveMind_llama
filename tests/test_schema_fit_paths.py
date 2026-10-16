"""Every tool-driving path fits its schema to the window.

Before 2026-10-17 only the duo coder did: `_get_inline_tools(..., budget_chars=
schema_budget)` was called from core/duo_runner.py alone. The direct tiers and
the tool agent shipped the FULL schema regardless of ctx - measured for
direct_full at 8k ctx: 23 tools / 14,588 chars against a budget of 8,028 chars
(~2x over). The helper agents (subagent_lite, fix_agent) are small enough to
fit already; they are pinned here so a growing tool set is noticed.

Pins:
  (a) schema_budget_for: SCHEMA_FRAC of the window, 0 on junk input
  (b) fitting really shrinks direct_full at 8k and keeps it complete at 16k
  (c) source guards: direct_runner and chat_run pass budget_chars; duo_runner
      still does
  (d) helper schemas stay under the 8k budget (documented, not fixed)

Run: python tests/test_schema_fit_paths.py
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


from core.budgets import schema_budget_for                    # noqa: E402
from tools.definitions import _get_inline_tools               # noqa: E402


def _chars(tools):
    return sum(len(json.dumps(t)) for t in tools)


# ── (a) the helper ──────────────────────────────────────────────────────────
check("8k budget uses SCHEMA_FRAC of the window",
      schema_budget_for(8192, "m") == 8028, f" ({schema_budget_for(8192, 'm')})")
check("16k budget is twice the 8k one",
      schema_budget_for(16384, "m") == 2 * schema_budget_for(8192, "m"))
check("None ctx falls back to 8192",
      schema_budget_for(None, "m") == schema_budget_for(8192, "m"))
check("junk ctx does not raise", isinstance(schema_budget_for("x", "m"), int))

# ── (b) the fit does something at 8k and nothing at 16k ─────────────────────
for _mode in ("direct_full", "tool_agent"):
    _full = _get_inline_tools(include_websearch=False, mode=_mode)
    _b8 = schema_budget_for(8192, "test-model")
    _fit8 = _get_inline_tools(include_websearch=False, mode=_mode, budget_chars=_b8)
    _b16 = schema_budget_for(16384, "test-model")
    _fit16 = _get_inline_tools(include_websearch=False, mode=_mode, budget_chars=_b16)
    check(f"{_mode}: 8k schema within budget", _chars(_fit8) <= _b8,
          f" ({_chars(_fit8)} <= {_b8})")
    check(f"{_mode}: 8k dropped tools", len(_fit8) < len(_full),
          f" ({len(_fit8)} < {len(_full)})")
    check(f"{_mode}: 16k keeps every tool", len(_fit16) == len(_full),
          f" ({len(_fit16)} vs {len(_full)})")
    check(f"{_mode}: no budget -> unchanged (old behaviour)",
          _chars(_get_inline_tools(include_websearch=False, mode=_mode)) == _chars(_full))

# ── (c) wiring ──────────────────────────────────────────────────────────────
_dr = (ROOT / "core" / "direct_runner.py").read_text(encoding="utf-8", errors="replace")
_cr = (ROOT / "core" / "chat_run.py").read_text(encoding="utf-8", errors="replace")
_du = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
check("direct_runner computes a schema budget",
      "schema_budget_for(_num_ctx, model)" in _dr)
check("direct_runner passes it to the tool builder",
      "budget_chars=_schema_budget or None" in _dr)
check("tool agent computes a schema budget",
      "_sbf(_tool_ctx_override, _tool_model)" in _cr)
check("tool agent passes it", "budget_chars=_tool_schema_budget or None" in _cr)
check("duo coder still fits", "budget_chars=_run_budgets.schema_budget" in _du)

# ── (d) the helper agents fit already ───────────────────────────────────────
from core.subagent_lite import _tool_schemas as _sub_schemas  # noqa: E402
from core.fix_agent import _tool_schemas as _fix_schemas      # noqa: E402

_b8 = schema_budget_for(8192, "test-model")
check("subagent_lite schema fits the 8k budget",
      _chars(_sub_schemas()) <= _b8, f" ({_chars(_sub_schemas())} <= {_b8})")
check("fix_agent schema fits the 8k budget",
      _chars(_fix_schemas()) <= _b8, f" ({_chars(_fix_schemas())} <= {_b8})")

print()
print(f"SUMMARY schema_fit_paths: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
