# -*- coding: utf-8 -*-
"""Master regression runner.

Runs all standalone regression suites (tests/test_*.py) in one go and forces
exit 1 on any failure - so fixed behaviors do not silently regress. Each suite
is a standalone script with `sys.exit(0|1)`. If a suite file is missing, it
counts as FAIL.

Run:
    python tests/run_regressions.py            # all suites
    python tests/run_regressions.py --only stuck_bash   # name filter (substring)
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent

# (name, relPath, timeout seconds) - only actually present tests.
SUITES = [
    ("compression_plan_anchor",   "tests/test_compression_plan_anchor.py",     120),
    ("compress_plan_reinject",    "tests/test_compress_plan_reinject.py",      120),
    ("destructive_gate",          "tests/test_destructive_gate.py",            120),
    ("direct_tools",              "tests/test_direct_tools.py",                120),
    ("ensure_loaded_stale_port",  "tests/test_ensure_loaded_stale_port.py",     60),
    ("loop_detect_calibration",   "tests/test_loop_detect_calibration.py",     120),
    ("loop_detect_chunk_read",    "tests/test_loop_detect_chunk_read.py",      120),
    ("loop_detect_run_bash",      "tests/test_loop_detect_run_bash.py",        120),
    ("models_registry",           "tests/test_models_registry.py",              60),
    ("safe_profile_choice",       "tests/test_safe_profile_choice.py",          60),
    ("linux_paths",               "tests/test_linux_paths.py",                  60),
    ("planner_coder_port_verify", "tests/test_planner_coder_port_verify.py",    60),
    ("tool_error_taxonomy",       "tests/test_tool_error_taxonomy.py",          60),
    ("tool_arg_schemas",          "tests/test_tool_arg_schemas.py",             60),
    ("run_audit",                 "tests/test_run_audit.py",                    60),
    ("workspace_guards",          "tests/test_workspace_guards.py",             60),
    ("tool_budgets",              "tests/test_tool_budgets.py",                 60),
    ("ctx_budget",                "tests/test_ctx_budget.py",                   60),
    ("dispatch_smoke",            "tests/test_dispatch_smoke.py",               60),
    ("destructive_python",        "tests/test_destructive_python.py",           60),
    ("execute_tool_round",        "tests/test_execute_tool_round.py",           60),
    ("ctx_lru_invalidation",      "tests/test_ctx_lru_invalidation.py",          60),
    ("planner_connect_recovery",  "tests/test_planner_connect_recovery.py",    120),
    ("planner_thinking_cap",      "tests/test_planner_thinking_cap.py",        120),
    ("stuck_bash_reset_threshold", "tests/test_stuck_bash_reset_threshold.py", 120),
    ("stuck_edit_false_positive", "tests/test_stuck_edit_false_positive.py",   120),
    ("error_rollup",              "tests/test_error_rollup.py",                  60),
    ("repomap_pin",               "tests/test_repomap_pin.py",                   60),
    ("toolcall_sanitize",         "tests/test_toolcall_sanitize.py",             60),
    ("ctx_fit_and_floor_guard",   "tests/test_ctx_fit_and_floor_guard.py",       60),
    ("write_guard_and_explore_truth", "tests/test_write_guard_and_explore_truth.py", 60),
    ("noop_hint_and_zero_lines",  "tests/test_noop_hint_and_zero_lines.py",       60),
    ("fallback_ctx_stall",     "tests/test_fallback_ctx_stall.py",             60),
    ("mismatch_reload",        "tests/test_mismatch_reload.py",                60),
    ("mini_shrink_retry",      "tests/test_mini_shrink_retry.py",              60),
    ("output_reserve",         "tests/test_output_reserve.py",                  60),
    ("auto_split_pending",     "tests/test_auto_split_pending.py",              60),
    ("ctx_guard",              "tests/test_ctx_guard.py",                       60),
    ("planner_model_persist",  "tests/test_planner_model_persist.py",           60),
    ("read_only_detect",       "tests/test_read_only_detect.py",                60),
    ("tool_arg_compact",       "tests/test_tool_arg_compact.py",                60),
    ("lint",                   "tests/test_lint.py",                            60),
    ("no_new_silent_excepts",  "tests/test_no_new_silent_excepts.py",           60),
    ("compress_local_only",    "tests/test_compress_local_only.py",             60),
    ("presets_bom",            "tests/test_presets_bom.py",                     30),
    ("browser_fileserve",      "tests/test_browser_fileserve.py",               30),
    ("partial_cut",            "tests/test_partial_cut.py",                     30),
    ("memory_routing",         "tests/test_memory_routing.py",                  30),
]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    only = ""
    if "--only" in sys.argv:
        i = sys.argv.index("--only")
        only = sys.argv[i + 1].lower() if i + 1 < len(sys.argv) else ""

    results = []
    failed_total = 0
    print("=" * 64)
    print("REGRESSION RUN - HiveMind Feature-Checks")
    print("=" * 64)
    for name, rel, timeout in SUITES:
        if only and only not in name.lower():
            continue
        target = ROOT / rel
        if not target.exists():
            results.append((name, False, 0.0))
            failed_total += 1
            print(f"[FAIL] {name:<20} ({rel} missing)")
            continue
        t0 = time.time()
        r = subprocess.run(
            [sys.executable, str(target)],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            cwd=str(ROOT), timeout=timeout)
        dur = time.time() - t0
        ok = r.returncode == 0
        failed_total += 0 if ok else 1
        results.append((name, ok, dur))
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<20} ({dur:.0f}s)")
        if not ok:
            tail = (r.stdout or "").strip().splitlines()[-8:]
            errtail = (r.stderr or "").strip().splitlines()[-5:]
            for l in tail:
                print(f"    | {l}")
            for l in errtail:
                print(f"    ! {l}")

    print("=" * 64)
    ran = len(results)
    npass = sum(1 for _, ok, _ in results if ok)
    print(f"Suites: {npass}/{ran} PASS, {failed_total} suite(s) failed")
    return 1 if failed_total else 0


if __name__ == "__main__":
    sys.exit(main())
