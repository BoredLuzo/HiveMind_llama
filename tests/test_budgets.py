"""BUDGETS (2026-10-14, Sonnet redesign): one resolver per run, derived sizes
only. Pins the three invariants the design rests on:

1. Monotonicity  — n_ctx up => every cap up (or clamped, never down)
2. Base case     — 16k ctx / cpt 3.92 reproduces the tuned values
                   (tool cap ~7.7k, read cap ~32k, compress trigger ~0.70)
3. Override rule — settings "budget_<field>" = number wins, None = auto
Plus: the tool-schema continuum (fit_tools_to_budget) and a grep test that
keeps model-name staffels OUT of the budget/tool-selection code.
"""
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.budgets import (resolve, set_active, update_cpt_ema,
                          update_prefill_ema, cpt_for)  # noqa: E402
from tools.limits import SCHEMA_FRAC  # noqa: E402

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


print("=== budgets: derivation, base case, monotonicity, overrides ===")

# ── 1. 16k base case reproduces the tuned values ─────────────────────────
# PREFILL-AWARE (task 4): the 60-second safety cap binds at the default
# tps=20 -> tool cap ~4.7k at 16k (fast slots keep the 7.7k fraction).
b16 = resolve(16384, 3.92, 8000, {})
check("tool_result_cap ~4.7k at 16k (60s prefill safety)", 4400 <= b16.tool_result_cap <= 5000,
      f"({b16.tool_result_cap})")
check("read_char_cap ~32k at 16k", 30000 <= b16.read_char_cap <= 33500,
      f"({b16.read_char_cap})")
check("injection budget also capped by prefill safety",
      b16.injection_budget <= b16.tool_result_cap + 1,
      f"({b16.injection_budget})")
check("compress trigger ~0.70 x ctx at 16k/8k-out",
      abs(b16.compress_trigger_tokens - int(0.70 * 16384)) <= 200,
      f"({b16.compress_trigger_tokens} vs {int(0.70*16384)})")
check("rest band below trigger", b16.compress_rest_tokens < b16.compress_trigger_tokens)
check("anchor window in named clamp", 3 <= b16.anchor_lines <= 15,
      f"(+{b16.anchor_lines})")
check("outline derived from anchor", b16.outline_items == b16.anchor_lines * 6)
check("schema budget = SCHEMA_FRAC of ctx*cpt",
      abs(b16.schema_budget - int(SCHEMA_FRAC * 16384 * 3.92)) <= 2)

# ── 2. monotonicity ──────────────────────────────────────────────────────
b8 = resolve(8192, 3.92, 8000, {})
b32 = resolve(32768, 3.92, 8000, {})
for field in ("tool_result_cap", "read_char_cap", "schema_budget",
              "injection_budget", "compress_trigger_tokens"):
    check(f"monotone in n_ctx: {field}",
          getattr(b8, field) <= getattr(b16, field) <= getattr(b32, field),
          f"({getattr(b8, field)}/{getattr(b16, field)}/{getattr(b32, field)})")

# ── 2b. prefill-aware caps: fast slots keep the fraction dial ────────────
bfast = resolve(16384, 3.92, 8000, {}, prefill_tps=90)
check("fast slot (90 tps) keeps fraction cap",
      bfast.tool_result_cap > b16.tool_result_cap,
      f"({bfast.tool_result_cap})")
check("prefill_tps recorded on budgets", bfast.prefill_tps == 90.0)
slower = resolve(16384, 3.92, 8000, {}, prefill_tps=10)
check("slower slot -> tighter cap", slower.tool_result_cap < b16.tool_result_cap,
      f"({slower.tool_result_cap})")

# ── 2c. persisted prefill EMA roundtrip ──────────────────────────────────
import core.budgets as _budg
_tmp_state = Path(tempfile.mkdtemp(prefix="hvm_budgets_")) / "prefill_ema.json"
_orig_state = _budg._PREFILL_STATE
_budg._PREFILL_STATE = _tmp_state
try:
    check("no state -> default tps", _budg.prefill_tps_for("bench-model") == 20.0)
    # MIN-PROMPT FLOOR (task 2b): a prompt_n=65 reading is overhead noise and
    # must not touch the EMA in any way.
    _ignored = _budg.update_prefill_ema("bench-model", 45.0, prompt_n=65)
    check("prompt_n=65 measurement ignored", _ignored is None
          and _budg.prefill_tps_for("bench-model") == 20.0,
          f"(tps_for={_budg.prefill_tps_for('bench-model')})")
    _set = _budg.update_prefill_ema("bench-model", 45.0, prompt_n=4000)
    check("prompt_n=4000 FIRST measurement sets the value",
          _set == 45.0 and _budg.prefill_tps_for("bench-model") == 45.0,
          f"({_budg.prefill_tps_for('bench-model')})")
    _budg.update_prefill_ema("bench-model", 65.0, prompt_n=4000)
    check("second valid sample blends", abs(_budg.prefill_tps_for("bench-model") - 49.0) < 0.1,
          f"({_budg.prefill_tps_for('bench-model')})")
    _budg.update_prefill_ema("bench-model", 10**7, prompt_n=4000)  # implausible tps - ignored
    check("implausible tps ignored", abs(_budg.prefill_tps_for("bench-model") - 49.0) < 0.1)
finally:
    _budg._PREFILL_STATE = _orig_state
    shutil.rmtree(_tmp_state.parent, ignore_errors=True)

# ── 2d. per-tool cap field: read_file bounded by read_char_cap only ──────
import core.tool_executor as _tex
set_active(b16)
check("(a) effective read_file cap = read_char_cap at 16k (32112)",
      _tex._cap_for_tool("read_file") == 32112,
      f"({_tex._cap_for_tool('read_file')})")
check("(b) tool_result_cap 4704 and injection 4704 at tps=20",
      b16.tool_result_cap == 4704 and b16.injection_budget == 4704,
      f"({b16.tool_result_cap}/{b16.injection_budget})")
_bash = _tex._cap_tool_result("x" * 6000, tool_name="run_bash")
check("(c) run_bash result >4704 chars still capped",
      len(_bash) <= b16.tool_result_cap + 200 and "truncated at" in _bash,
      f"({len(_bash)})")
_rf = _tex._cap_tool_result("y" * 30000, tool_name="read_file")
check("(d) 30k-char read_file result passes through uncut",
      len(_rf) == 30000,
      f"({len(_rf)})")
check("default (no tool name) still uses tool_result_cap",
      len(_tex._cap_tool_result("x" * 6000)) <= b16.tool_result_cap + 200)

# ── 3. floors/ceilings hold at extremes ──────────────────────────────────
btiny = resolve(2048, 3.92, 1024, {})
bhuge = resolve(131072, 3.92, 8192, {})
from tools.limits import TOOL_RESULT_MIN, TOOL_RESULT_MAX, READ_CHAR_MAX
check("floor holds at tiny ctx", btiny.tool_result_cap >= TOOL_RESULT_MIN)
check("ceiling holds at huge ctx", bhuge.tool_result_cap <= TOOL_RESULT_MAX
      and bhuge.read_char_cap <= READ_CHAR_MAX)

# ── 4. override rule: number wins, None = auto ───────────────────────────
bov = resolve(16384, 3.92, 8000, {"budget_read_char_cap": 12345})
check("override wins", bov.read_char_cap == 12345)
check("override recorded", "read_char_cap" in bov.overrides_used)
bnone = resolve(16384, 3.92, 8000, {"budget_read_char_cap": None})
check("None = auto", bnone.read_char_cap == b16.read_char_cap
      and not bnone.overrides_used)

# ── 5. compression headroom reacts to max_out (write-heavy => earlier) ──
b_small_out = resolve(16384, 3.92, 1024, {})
check("smaller max_out -> later trigger",
      b_small_out.compress_trigger_tokens >= b16.compress_trigger_tokens)

# ── 6. cpt EMA ───────────────────────────────────────────────────────────
first = update_cpt_ema("test-model-x", 3000, 1000)
check("ema sample accepted", first is not None and first < 3.92,
      f"({first})")
for _ in range(20):
    update_cpt_ema("test-model-x", 2000, 1000)  # converges toward 2.0
conv = cpt_for("test-model-x")
check("ema converges", abs(conv - 2.0) < 0.1, f"({conv})")
check("ema rejects implausible samples",
      update_cpt_ema("test-model-x", 100, 10000) is None)

# ── 7. tool-schema continuum (fit_tools_to_budget) ───────────────────────
from tools.definitions import _get_inline_tools
full = _get_inline_tools(include_websearch=True, mode="duo_full")
big_budget = resolve(16384, 3.92, 8000, {}).schema_budget
small_budget = resolve(8192, 2.5, 8000, {}).schema_budget

at_big = _get_inline_tools(include_websearch=True, mode="duo_full",
                           budget_chars=big_budget)
at_small = _get_inline_tools(include_websearch=True, mode="duo_full",
                             budget_chars=small_budget)
sz_big = sum(len(json.dumps(t)) for t in at_big)
sz_small = sum(len(json.dumps(t)) for t in at_small)
check("16k budget keeps (near-)full schema", sz_big <= big_budget + 1500
      and len(at_big) >= len(full) - 2, f"({sz_small}-{sz_big}/{big_budget})")
check("small budget shrinks the schema", sz_small < sz_big,
      f"({sz_small} < {sz_big})")
core_names = {"read_file", "edit_file", "write_file", "run_bash", "task_complete"}
small_names = {t["function"]["name"] for t in at_small}
check("tier-0 core always kept", core_names <= small_names)
descs = {t["function"]["name"]: t["function"]["description"]
         for t in at_small}
check("non-core descriptions shortened first",
      max(len(d) for n, d in descs.items() if n not in core_names)
      <= max(len(d) for n, d in descs.items() if n in core_names) + 1)

# ── 8. no model-name staffels in budget/tool-selection code ──────────────
scan_files = ["core/budgets.py", "tools/limits.py", "tools/definitions.py",
              "core/duo/_vram.py"]
name_pat = re.compile(r'[:.]\s*0?\d+(\.\d+)?b\b|"\s*:\s*\d+(\.\d+)?b"|qwen|lfm|minicpm|gemma|k2-horizon|llama3|mistral',
                      re.IGNORECASE)
bad = []
for rel in scan_files:
    src = Path(rel).read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(src.splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        if "settings.get" in line or line.lstrip().startswith('or "'):
            # settings-default fallbacks are lookups, not staffel branches
            continue
        if name_pat.search(line):
            bad.append(f"{rel}:{i}: {line.strip()[:90]}")
check("no model names / size staffels in budget+tool-selection code",
      not bad, str(bad[:3]))

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
