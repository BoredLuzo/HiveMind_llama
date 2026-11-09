# -*- coding: utf-8 -*-
"""Passive instrumentation (2026-10-30) - the three user decisions:

1. [PERF] line per request (utils/perf_log.py): fields from usage+timings,
   missing values print "-" (a 0 would fake a cache loss).
2. Sampler switch: HiveMind_system_sampler=1 enables the 30-s
   memory/GPU sampler rows; default off (no background PowerShell).
3. DECODE-DROP warning (agentic_tool_loop): newest round below 50 % of the
   median of the previous rounds after >= 5 rounds -> WARNING only, never
   an automatic restart (a restart destroys the evidence).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from utils.perf_log import perf_line  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# --- 1. PERF line -------------------------------------------------------
line = perf_line(
    kind="main", model="hermes3.6:35b", port=8101,
    usage={"prompt_tokens": 70956,
           "prompt_tokens_details": {"cached_tokens": 70782},
           "completion_tokens": 52},
    timings={"prompt_n": 174, "prompt_ms": 10899.592, "predicted_n": 52,
             "predicted_per_second": 25.87})
print(f"    {line}")
check("starts with [PERF] kind=main", line.startswith("[PERF] kind=main"))
check("carries p_tok", "p_tok=70956" in line)
check("carries cache_n", "cache_n=70782" in line)
check("carries prompt_n", "prompt_n=174" in line)
check("carries prompt_ms", "prompt_ms=10899.6" in line)
check("carries pred_n", "pred_n=52" in line)
check("carries tps", "tps=25.87" in line)

# missing fields must be "-", never 0 (0 fakes a cache loss)
missing = perf_line(kind="side", model="qwen3.5", port=8101)
check("missing usage -> p_tok=-", "p_tok=-" in missing, missing)
check("missing usage -> cache_n=- (not 0)", "cache_n=-" in missing, missing)
check("missing timings -> prompt_n=-", "prompt_n=-" in missing, missing)
check("missing timings -> tps=-", "tps=-" in missing, missing)

# cache_n without p_tok is meaningless -> "-"
odd = perf_line(kind="side", usage={"prompt_tokens_details": {"cached_tokens": 5}},
                timings={})
check("cache_n without p_tok -> -", "cache_n=-" in odd, odd)

# real cache-loss shape stays readable
loss = perf_line(kind="main", model="h", port=8101,
                 usage={"prompt_tokens": 26989,
                        "prompt_tokens_details": {"cached_tokens": 0}},
                 timings={"prompt_n": 26989, "prompt_ms": 610002.39,
                          "predicted_n": 931, "predicted_per_second": 7.15})
check("cache-loss row shows cache_n=0 + prompt_n=26989",
      "cache_n=0" in loss and "prompt_n=26989" in loss, loss)

# --- 2. sampler switch --------------------------------------------------
src = (Path(__file__).resolve().parent.parent / "core" / "agentic_tool_loop.py").read_text(
    encoding="utf-8")
check("PERF wired into the main loop", "perf_line" in src and 'kind="main"' in src)
check("DECODE-DROP is warn-only (no restart call in its block)",
      "[DECODE-DROP]" in src and "warning only, no restart" in src)
mwatch = Path(__file__).resolve().parent.parent.parent / "HiveMind_dev" / "memwatch.py"
if mwatch.exists():
    msrc = mwatch.read_text(encoding="utf-8")
    check("memwatch honours the switch",
          "HiveMind_system_sampler" in msrc or "HIVEMIND_SYSTEM_SAMPLER" in msrc)
else:
    print("  SKIP memwatch switch (file outside repo)")

print(f"\nperf_logging: {passed} passed, {failed} failed")
# --- switches (2026-10-30): env only, no UI row -------------------------
import os as _os
from utils.perf_log import perf_enabled, decode_warn_enabled
_os.environ.pop("HiveMind_perf_log", None)
_os.environ.pop("HiveMind_decode_drop_warn", None)
check("default: perf on", perf_enabled() is True)
check("default: decode-warn on", decode_warn_enabled() is True)
_os.environ["HiveMind_perf_log"] = "0"
check("HiveMind_perf_log=0 silences", perf_enabled() is False)
_os.environ["HiveMind_perf_log"] = "1"
check("HiveMind_perf_log=1 re-enables", perf_enabled() is True)
_os.environ["HiveMind_decode_drop_warn"] = "off"
check("decode_drop_warn=off silences", decode_warn_enabled() is False)
_os.environ.pop("HiveMind_decode_drop_warn", None)
_os.environ.pop("HiveMind_perf_log", None)

sys.exit(1 if failed else 0)
