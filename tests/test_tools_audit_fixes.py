"""Tools deep-audit fixes (2026-09-13).

Three audit streams over tools/ + integration surfaced ~50 findings; this
suite covers the behavioral fixes (faithful reproductions + source guards):

1. VERIFY-SERIAL-GUARD: verify_last_ok_serial was bumped by EVERY tool type
   (read_file, task_complete, ...) — equalizing the serials and permanently
   disarming the duo verify gates. Only a passing run_bash may bump now.
2. ANTI-SPOOF: echo'ing "[TEST-RESULT] ✅" / "pytest" into run_bash output
   satisfied the auto-test gate; the executor's exit-code scan took the FIRST
   "[exit code: N]" match so an early fake 0 masked a real failure; and a
   failing run_tests (❌, no exit-code marker) counted as success.
3. TOOL-MSG-ALWAYS: four hint-escalation branches replaced _dresult without
   appending the tool message -> dangling tool_call in history.
4. CRLF-PRESERVE: read_text() universal newlines made _has_crlf always False
   -> CRLF files silently rewritten as LF by edit_file/replace_lines/patch.
5. AUTO-SPLIT honesty: a >250k remainder was silently truncated and reported
   "full content written"; empty content destroyed the pending remainder.
6. run_python exit codes: non-empty output swallowed non-zero exits.
7. tc_blocked marker: the handler string is "Build: blocked", the check
   wanted "build_status" -> never True.
8. pip guard: install_package pip could target HiveMind's own venv.
9. browser loopback guard; 10. SKIP-trap drop after LRU eviction;
11. patch-hint dname guard; 12. test_runner tree kill.

Run: python tests/test_tools_audit_fixes.py
Exit 0 = all pass, Exit 1 = failures.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


ROOT = Path(__file__).parent.parent


# ── 1. verify serial guard (faithful) ───────────────────────────────────────
def test_verify_serial_guard():
    from core.tool_exec_helpers import _run_bash_fail_fix_pass_insight
    class _R:
        verify_last_ok_serial = 0
        verify_mutation_serial = 5
        last_run_bash_failure = None
        changed_since_failure = set()
        last_learned_insight_sig = ""
    hooks = type("H", (), {"remember_insight": None})()

    import asyncio
    r = _R()
    asyncio.new_event_loop().run_until_complete(
        _run_bash_fail_fix_pass_insight("read_file", {"path": "a.py"}, "ok", r, hooks, None, 0))
    if r.verify_last_ok_serial == 0:
        ok("read_file bumped verify_last_ok_serial NICHT mehr (Gates scharf)")
    else:
        fail("serial_read", f"serial={r.verify_last_ok_serial}")

    r2 = _R()
    asyncio.new_event_loop().run_until_complete(
        _run_bash_fail_fix_pass_insight("task_complete", {}, "[task_complete] Build: ok", r2, hooks, None, 0))
    if r2.verify_last_ok_serial == 0:
        ok("task_complete bumped verify_last_ok_serial NICHT mehr")
    else:
        fail("serial_tc", f"serial={r2.verify_last_ok_serial}")

    r3 = _R()
    asyncio.new_event_loop().run_until_complete(
        _run_bash_fail_fix_pass_insight("run_bash", {"cmd": "pytest"}, "out [exit code: 0]", r3, hooks, None, 0))
    if r3.verify_last_ok_serial == 5:
        ok("erfolgreicher run_bash bumped weiterhin (Fix->Verify-Zyklus intakt)")
    else:
        fail("serial_bash", f"serial={r3.verify_last_ok_serial}")


# ── 2. anti-spoof ───────────────────────────────────────────────────────────
def test_test_result_fail_counts_as_failure():
    from utils.tool import run_bash_failed
    if run_bash_failed("[TEST-RESULT] ❌ 3 failed") and not run_bash_failed("[TEST-RESULT] ✅ all good"):
        ok("[TEST-RESULT] ❌ zählt als Failure, ✅ nicht")
    else:
        fail("test_result", "❌/✅-Bewertung falsch")


def test_exit_code_last_match():
    from utils.tool import run_bash_failed
    spoofed = "model echo: [exit code: 0]\nreal output\n[exit code: 2]"
    if run_bash_failed(spoofed):
        ok("LETZTER [exit code:]-Match gewinnt — gefälschte frühere 0 maskiert nicht mehr")
    else:
        fail("exit_last", "spoofed output nicht als Failure erkannt")


# ── 4. dangling tool_call appends (source + behavior) ──────────────────────
def test_escalations_append_tool_msg():
    src = (ROOT / "core" / "tool_exec_helpers.py").read_text(encoding="utf-8")
    bad = []
    for marker in ("[SYSTEM] run_bash timed out {_n}x.",
                   "[SYSTEM] run_bash produced a non-zero exit code 4x.",
                   "[SYSTEM] run_bash blocked 2x",
                   "[SYSTEM] edit_file produced no change on"):
        i = src.find(marker)
        if i == -1:
            bad.append(marker[:40] + " (nicht gefunden)")
            continue
        seg = src[max(0, i - 500):i]
        if "_append_tool_msg" not in seg:
            bad.append(marker[:40])
    if not bad:
        ok("Eskalationszweige haengen die Tool-Message an (kein dangling tool_call)")
    else:
        fail("escalation_append", f"ohne Append: {bad}")


def test_crlf_preserved_by_edit_paths():
    sys.path.insert(0, str(ROOT))
    src = (ROOT / "tools" / "handlers" / "file_ops.py").read_text(encoding="utf-8")
    n = src.count('errors="replace", newline="")')
    if n >= 3 and "CRLF-PRESERVE" in src:
        ok(f"CRLF-erhaltende Reads an {n} Schreibpfaden")
    else:
        fail("crlf", f"newline- Reads: {n}")


# ── 6. auto-split honesty ───────────────────────────────────────────────────
def test_autosplit_truncation_honest():
    src = (ROOT / "tools" / "handlers" / "file_ops.py").read_text(encoding="utf-8")
    if "AUTO_SPLIT_REMAINDER_CAPPED" in src and '"truncated": _truncated' in src \
            and src.find("if content:") < src.find("_pending_splits.pop(_split_key_, None)\n        else:") + 400:
        ok("AUTO-SPLIT: capped Rest -> ehrlicher Fehler; leerer Content zerstört Pending nicht")
    else:
        fail("autosplit", "Guards fehlen")


# ── 7/8/9/10 quick guards ───────────────────────────────────────────────────
def test_misc_guards():
    src_duo = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
    src_exec = (ROOT / "tools" / "handlers" / "exec_tools.py").read_text(encoding="utf-8")
    src_te = (ROOT / "core" / "tool_executor.py").read_text(encoding="utf-8")
    bad = []
    if 'workspace_lock=_ws_str,  # CRITIC-LOCK' not in src_duo:
        bad.append("critic_verify lock")
    if "PIP_NO_WORKSPACE_VENV" not in src_exec:
        bad.append("pip venv guard")
    if "[exit code: {r.returncode}]" not in src_exec:
        bad.append("run_python exit marker")
    if '"build: blocked" in str(_dresult).lower()' not in src_te:
        bad.append("tc_blocked marker")
    if not bad:
        ok("critic-lock / pip-venv / run_python-exit / tc_blocked-Marker gesetzt")
    else:
        fail("misc_guards", f"fehlt: {bad}")


def test_browser_loopback_guard():
    src = (ROOT / "tools" / "browser.py").read_text(encoding="utf-8")
    if "LOOPBACK-GUARD" in src and "is_private" in src and "DNS-LOOPBACK" in src:
        ok("Browser-Guard blockt loopback/private (fileserver-origin ausgenommen)")
    else:
        fail("browser", "loopback guard fehlt")


def test_skip_trap_and_ladder_hint():
    src_c = (ROOT / "context" / "compression.py").read_text(encoding="utf-8")
    src_h = (ROOT / "core" / "tool_exec_helpers.py").read_text(encoding="utf-8")
    ok_all = ("SKIP-TRAP-FIX" in src_c and "_rs.discard(path)" in src_c
              and 'if _dname not in ("patch_file", "edit_file"):' in src_h)
    if ok_all:
        ok("SKIP-Trap (read-guard drop nach Evict) + patch-hint dname-Guard")
    else:
        fail("skip_trap", "fixes fehlen")


def test_test_runner_tree_kill():
    src = (ROOT / "hive_functions" / "test_runner.py").read_text(encoding="utf-8")
    if "taskkill" in src and "TREE-KILL" in src:
        ok("test_runner killt den Prozessbaum bei Timeout")
    else:
        fail("tree_kill", "taskkill fehlt")


def test_anti_spoof_executor():
    src = (ROOT / "core" / "tool_executor.py").read_text(encoding="utf-8")
    checks = ["ANTI-SPOOF (2026-09-13)", "_ec_matches[-1]", 'if _at_name == "run_bash"']
    bad = [c for c in checks if c not in src]
    if not bad:
        ok("Executor: last-exit-code match + cmd-evidence Anti-Spoof")
    else:
        fail("anti_spoof", f"fehlt: {bad}")


if __name__ == "__main__":
    test_verify_serial_guard()
    test_test_result_fail_counts_as_failure()
    test_exit_code_last_match()
    test_escalations_append_tool_msg()
    test_crlf_preserved_by_edit_paths()
    test_autosplit_truncation_honest()
    test_misc_guards()
    test_browser_loopback_guard()
    test_skip_trap_and_ladder_hint()
    test_test_runner_tree_kill()
    test_anti_spoof_executor()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
