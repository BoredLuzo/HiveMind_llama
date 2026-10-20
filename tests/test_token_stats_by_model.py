"""Test: aggregate_by_model + neue Recorder-Felder (phases_ms, mode)."""
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import threading  # noqa: E402
import infra.token_stats as ts  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── aggregation: coder model is the run identity, planner-only runs count ─
daily = {
    "2026-10-19": {"run_list": [
        {"t": "10:00:00", "tokens": 1000, "elapsed_s": 50, "stop_reason": "completed",
         "models": {"coder": "m-a", "planner": "p-1"},
         "phases": {"coder": 1000}, "phases_ms": {"coder": 20000}},
        {"t": "10:10:00", "tokens": 500, "elapsed_s": 25, "stop_reason": "hard_stop",
         "models": {"coder": "m-a", "planner": "p-1"},
         "phases": {"coder": 500}, "phases_ms": {"coder": 10000}},
        # planner-only run: no coder, but the planner phase did produce work
        {"t": "10:20:00", "tokens": 0, "elapsed_s": 5, "stop_reason": "aborted",
         "models": {"planner": "p-1"}, "phases": {"planner": 120},
         "phases_ms": {"planner": 4000}},
    ]},
}
rows = ts.aggregate_by_model(daily)
by_model = {(r["role"], r["model"]): r for r in rows}
check("rows are per model+role", len(rows) == 2, f"({[(r['model'], r['role']) for r in rows]})")
check("coder runs grouped under (coder, m-a)",
      by_model[("coder", "m-a")]["runs"] == 2, f"({by_model[('coder', 'm-a')]['runs']})")
check("planner shows up with its own row",
      by_model[("planner", "p-1")]["role"] == "planner")
check("tok/s uses the coder phase (1500 tok / 30 s = 50)",
      abs(by_model[("coder", "m-a")]["tps"] - 50.0) < 0.2,
      f"({by_model[('coder', 'm-a')]['tps']})")
check("completed share is honest (1 of 2)",
      abs(by_model[("coder", "m-a")]["completed_share"] - 0.5) < 0.01,
      f"({by_model[('coder', 'm-a')]['completed_share']})")
check("stop reasons counted", by_model[("coder", "m-a")]["stop"].get("hard_stop") == 1)
check("biggest token row first", rows[0]["model"] == "m-a")

check("planner row keeps its own numbers",
      by_model[("planner", "p-1")]["tokens"] == 120
      and abs(by_model[("planner", "p-1")]["tps"] - 30.0) < 0.2,
      f"({by_model[('planner', 'p-1')]['tokens']} tok, "
      f"{by_model[('planner', 'p-1')]['tps']} tok/s)")
# 0/0-Zeilen (konfiguriert, aber nie gelaufen) werden verworfen
_sparse = ts.aggregate_by_model({"2026-10-19": {"run_list": [
    {"t": "11:00:00", "tokens": 10, "elapsed_s": 2, "stop_reason": "completed",
     "models": {"coder": "m-b", "critic": "m-c"}}]}})
check("a configured-but-unused role is dropped",
      all(r["role"] != "critic" for r in _sparse),
      f"({[(r['role'], r['model']) for r in _sparse]})")


# ── recorder round-trip: the new fields survive the write ───────────────
tmp = Path(tempfile.mkdtemp(prefix="hm_stats_")) / "token_stats.json"
ts.init_token_stats(str(tmp), threading.Lock())
ts._record_run({
    "run_id": "r1", "tokens": 300, "elapsed_s": 12.5, "stop_reason": "completed",
    "models": {"coder": "m-a"}, "phases_ms": {"coder": 5000, "planner": 1200},
    "mode": "code_duo", "prompt_tokens": 900, "cached_tokens": 400, "requests": 3,
})
saved = json.loads(tmp.read_text(encoding="utf-8"))
run = saved["daily"][sorted(saved["daily"])[0]]["run_list"][0]
check("phases_ms stored", run.get("phases_ms", {}).get("coder") == 5000,
      f"({run.get('phases_ms')})")
check("mode stored", run.get("mode") == "code_duo")
check("aggregation works on the stored record",
      ts.aggregate_by_model(saved["daily"])[0]["tps"] == 60.0,
      f"({ts.aggregate_by_model(saved['daily'])[0]['tps']})")


# ── user abort is not a failure ──────────────────────────────────────────
_ab = ts.aggregate_by_model({"2026-10-19": {"run_list": [
    {"t": "10:00:00", "tokens": 100, "elapsed_s": 10, "stop_reason": "completed",
     "models": {"coder": "m-a"}, "phases": {"coder": 100}, "phases_ms": {"coder": 5000}},
    {"t": "10:05:00", "tokens": 50, "elapsed_s": 5, "stop_reason": "aborted",
     "models": {"coder": "m-a"}, "phases": {"coder": 50}, "phases_ms": {"coder": 2000}},
]}})
_row = {r["model"]: r for r in _ab}["m-a"]
check("abort counted separately", _row["aborted"] == 1, f"({_row['aborted']})")
check("ok% ignores the abort (1 of 1 judged)", abs(_row["completed_share"] - 1.0) < 0.01,
      f"({_row['completed_share']})")

# ── day scope ───────────────────────────────────────────────────────────
_two_days = {"2026-10-18": {"run_list": [
    {"t": "09:00:00", "tokens": 10, "elapsed_s": 2, "stop_reason": "completed",
     "models": {"coder": "m-old"}, "phases": {"coder": 10}, "phases_ms": {"coder": 1000}}]},
    "2026-10-19": {"run_list": [
        {"t": "09:00:00", "tokens": 20, "elapsed_s": 2, "stop_reason": "completed",
         "models": {"coder": "m-new"}, "phases": {"coder": 20}, "phases_ms": {"coder": 1000}}]}}
check("day scope returns only that day",
      [r["model"] for r in ts.aggregate_by_model(_two_days, day="2026-10-19")] == ["m-new"])
check("total scope keeps both",
      len(ts.aggregate_by_model(_two_days)) == 2)


# ── graceful_stop is a user stop, not a crash ───────────────────────────
_gs = ts.aggregate_by_model({"2026-10-19": {"run_list": [
    {"t": "10:00:00", "tokens": 100, "elapsed_s": 10, "stop_reason": "completed",
     "models": {"coder": "m-g"}, "phases": {"coder": 100}, "phases_ms": {"coder": 5000}},
    {"t": "10:05:00", "tokens": 40, "elapsed_s": 4, "stop_reason": "graceful_stop",
     "models": {"coder": "m-g"}, "phases": {"coder": 40}, "phases_ms": {"coder": 2000}},
    {"t": "10:07:00", "tokens": 30, "elapsed_s": 3, "stop_reason": "hard_stop",
     "models": {"coder": "m-g"}, "phases": {"coder": 30}, "phases_ms": {"coder": 1500}},
]}})
_g = _gs[0]
check("graceful counted separately", _g["graceful"] == 1, f"({_g['graceful']})")
check("excluded = abort + graceful", _g["excluded"] == 1, f"({_g['excluded']})")
check("ok% = 1 of 2 judged (graceful not a fail)",
      abs(_g["completed_share"] - 0.5) < 0.01, f"({_g['completed_share']})")

print()
print(f"SUMMARY token_stats_by_model: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
