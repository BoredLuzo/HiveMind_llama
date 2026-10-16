"""Real chars-per-token: ask the running server instead of assuming.

Until 2026-10-17 a duo run derived every cap from `budgets.cpt_for(model)` -
a per-model EMA seeded with the module default 3.92 that came from ONE live
/tokenize probe on ONE model. When the coder slot is already up, the run now
asks the server for the true token count of the exact tool-schema JSON it is
about to send (POST /tokenize) and uses that measured ratio for the budget
resolution of this run. Failure at any step falls back to the EMA.

Pins:
  (a) cpt_from_measurement: correct ratio, plausibility window, junk = None
  (b) the window matches update_cpt_ema's sampler (both paths agree)
  (c) resolve() really uses the value it is handed
  (d) source guard: duo_runner probes with the real schema JSON, falls back
      to cpt_for, and logs which source won
  (e) tokenize_count degrades to None without a server (no exception)

Run: python tests/test_cpt_probe.py
"""
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

# ── (a) the measurement ─────────────────────────────────────────────────────
check("exact ratio", B.cpt_from_measurement(1000, 250) == 4.0)
check("rounds to 3 decimals", B.cpt_from_measurement(1000, 255) == 3.922)
check("zero tokens = None", B.cpt_from_measurement(1000, 0) is None)
check("zero chars = None", B.cpt_from_measurement(0, 100) is None)
check("None input = None", B.cpt_from_measurement(None, None) is None)
check("implausible high ratio = None", B.cpt_from_measurement(100000, 100) is None)
check("implausible low ratio = None", B.cpt_from_measurement(1000, 900) is None)
check("tiny sample rejected (overhead-dominated)",
      B.cpt_from_measurement(150, 40) is None)
check("just above the floor is accepted",
      B.cpt_from_measurement(200, 50) == 4.0)

# ── (b) same window as the EMA sampler ──────────────────────────────────────
check("probe window matches the EMA sampler",
      (B.CPT_PROBE_MIN, B.CPT_PROBE_MAX) == (1.5, 12.0))

# ── (c) resolve() honours the handed cpt ────────────────────────────────────
_r_small = B.resolve(8192, 2.5, 2000, {})
_r_big = B.resolve(8192, 6.0, 2000, {})
check("lower cpt -> smaller caps", _r_small.tool_result_cap < _r_big.tool_result_cap,
      f" ({_r_small.tool_result_cap} < {_r_big.tool_result_cap})")
check("resolve records the cpt it got", abs(_r_small.cpt - 2.5) < 1e-9,
      f" ({_r_small.cpt})")
check("nonsense cpt falls back to the default",
      B.resolve(8192, 0, 2000, {}).cpt == B.CPT_DEFAULT)

# ── (d) source guard in duo_runner ──────────────────────────────────────────
_src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
check("duo_runner imports the tokenizer probe",
      "from backend.llama_client import tokenize_count" in _src)
check("probe tokenizes the REAL schema JSON",
      "json.dumps(_xtools_ws)" in _src and "tokenize_count(_probe_port, _probe_txt)" in _src)
check("measured value feeds resolve(cpt=...)",
      "cpt=_cpt_for_run" in _src)
check("fallback to the EMA is in place",
      "cpt_from_measurement" in _src and "_cpt_for_run = _budg.cpt_for(exec_mdl)" in _src)
check("the winning source is logged",
      "_cpt_source" in _src and "cpt=%.2f(%s)" in _src)

# ── (e) no server -> None, no exception ─────────────────────────────────────
from backend.llama_client import tokenize_count                 # noqa: E402
import asyncio                                                  # noqa: E402

check("tokenize_count(0) = None", asyncio.run(tokenize_count(0, "x")) is None)
check("tokenize_count(port, '') = None", asyncio.run(tokenize_count(9999, "")) is None)
check("unreachable port = None (no raise)",
      asyncio.run(tokenize_count(59999, "hello " * 50, timeout_s=0.4)) is None)

print()
print(f"SUMMARY cpt_probe: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
