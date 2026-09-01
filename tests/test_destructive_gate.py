"""Test: destructive-action gate - classification + no false positives.

Part A (existing): _is_destructive_bash classification.
Part B (phase 1+2, 2026-08-31): checks the _destructive_gate outcome SEPARATELY for
log output (level + classification) and the model-visible tool-result string.
The model text must be byte-identical to the existing template in all deny cases
(deliberately kept): [DESTRUCTIVE GATE: DECLINED] ... The command was NOT executed.
timeout / no pause event / empty answer -> WARNING log with classification;
real 'No' -> INFO DECLINED; confirmation -> INFO CONFIRMED + None (execute).
"""
import asyncio
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.runner import (
    _is_destructive_bash,
    _destructive_gate,
    _current_run_id,
    _pause_timeout_s,
)
from infra import run_control

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _mock_wait(canned):
    async def _m(*a, **k):
        return canned
    return _m


def _expected_deny(answer):
    return (
        f"[DESTRUCTIVE GATE: DECLINED] The action was declined by the user: "
        f"'{answer}'. The command was NOT executed. "
        f"Try a less destructive alternative."
    )


def _run_gate(canned_answer):
    """Mocks wait_for_resume and calls _destructive_gate.

    Returns: (tool_result, [log_records]) - log and model text separate.
    """
    capture = _Capture()
    logger = logging.getLogger("hivemind.tools")
    prev_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(capture)
    _current_run_id.set("gate_test")
    _pause_timeout_s.set(600)
    orig = run_control.wait_for_resume
    run_control.wait_for_resume = _mock_wait(canned_answer)
    try:
        result = asyncio.run(_destructive_gate("run_bash", {"cmd": "rm -rf /tmp/foo"}))
    finally:
        run_control.wait_for_resume = orig
        logger.setLevel(prev_level)
        logger.removeHandler(capture)
        run_control.cleanup_pause("gate_test")
        _current_run_id.set("")
    return result, capture.records


def _has_record(records, level, fragment):
    for r in records:
        if r.levelno == level and fragment in (r.getMessage() or ""):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
#  Part A - destructive classification (existing)
# ═══════════════════════════════════════════════════════════════════════════
def check_detection(label, cmd, expected):
    result = _is_destructive_bash(cmd)
    is_detected = result is not None
    extra = f" (matched: {result})" if result else ""
    check(f"detect {label}: {cmd[:60]}", is_detected == expected, extra)


# ── Destructive (expected: True) ──
check_detection("PowerShell Remove-Item recursive force", "Remove-Item -Recurse -Force C:\\temp", True)
check_detection("PowerShell Remove-Item force recursive", "Remove-Item -Force -Recurse C:\\temp", True)
check_detection("Unix rm -rf", "rm -rf /tmp/foo", True)
check_detection("Unix rm -r -f", "rm -r -f /tmp/foo", True)
check_detection("Windows del /F /S /Q", "del /F /S /Q C:\\temp\\*", True)
check_detection("format", "format D:", True)
check_detection("diskpart", "diskpart", True)
check_detection("git reset --hard", "git reset --hard HEAD~1", True)
check_detection("git push --force", "git push --force origin main", True)
check_detection("git push --force-with-lease", "git push --force-with-lease origin main", True)
check_detection("git clean -fdx", "git clean -fdx", True)
check_detection("DROP TABLE", "DROP TABLE users;", True)
check_detection("DROP DATABASE", "DROP DATABASE production;", True)
check_detection("TRUNCATE", "TRUNCATE TABLE logs;", True)
check_detection("Stop-Computer", "Stop-Computer", True)
check_detection("Restart-Computer", "Restart-Computer", True)
check_detection("Shutdown", "shutdown /s", True)
check_detection("taskkill", "taskkill /F /IM chrome.exe", True)
check_detection("icacls", "icacls file.txt /deny User:(W)", True)
check_detection("chmod -w", "chmod -w file.txt", True)
check_detection("cacls", "cacls file.txt /E /D User", True)
check_detection("reg add", "reg add HKLM\\Software\\...", True)
check_detection("reg delete", "reg delete HKLM\\Software\\...", True)
check_detection("Set-ItemProperty", "Set-ItemProperty -Path HKLM:\\...", True)
check_detection("New-ItemProperty", "New-ItemProperty -Path HKLM:\\...", True)

# ── Not destructive (expected: False) ──
check_detection("pytest", "pytest tests/ -x -q", False)
check_detection("npm test", "npm test", False)
check_detection("npm run build", "npm run build", False)
check_detection("pip install", "pip install requests", False)
check_detection("npm install", "npm install", False)
check_detection("git status", "git status", False)
check_detection("git diff", "git diff", False)
check_detection("git add", "git add file.py", False)
check_detection("git commit", "git commit -m 'fix'", False)
check_detection("git log", "git log --oneline", False)
check_detection("git checkout", "git checkout -b new-branch", False)
check_detection("mkdir", "mkdir new_dir", False)
check_detection("touch", "touch new_file.py", False)
check_detection("echo", "echo hello", False)
check_detection("python script.py", "python script.py", False)
check_detection("ls", "ls -la", False)
check_detection("cat file", "cat file.txt", False)
check_detection("rm single file", "rm file.txt", False)  # no -r/-f flag
check_detection("del single file", "del file.txt", False)  # no /F /S /Q
check_detection("git push normal", "git push origin main", False)  # no --force
check_detection("git pull", "git pull origin main", False)
check_detection("Remove-Item without flags", "Remove-Item file.txt", False)
check_detection("Write-Output", "Write-Output 'hello world'", False)
check_detection("node server.js", "node server.js", False)
# ── Live-Befund 2026-08-07: \bformat\b matchte Format-Table -> False-Positive ──
check_detection("PowerShell Format-Table", "Get-ChildItem -Recurse -File | Sort-Object FullName | Format-Table Name, Length", False)
check_detection("PowerShell Format-List", "Get-ChildItem | Format-List *", False)
check_detection("Python format()", "python -c \"print('{}'.format(42))\"", False)
check_detection("format with drive (really destructive)", "format D:", True)
check_detection("format /q drive", "format D: /q", True)


# ═══════════════════════════════════════════════════════════════════════════
#  Part B - gate outcome: log output and tool-result string checked separately
# ═══════════════════════════════════════════════════════════════════════════
print()
print("-- Part B: _destructive_gate outcome (log + model text separate) --")

# B1: confirmation -> None (execute), log INFO BESTAETIGT/CONFIRMED (language-robust)
res, recs = _run_gate("yes")
check("B1 confirm -> None", res is None)
check("B1 log INFO BESTAETIGT",
      _has_record(recs, logging.INFO, "BESTAETIGT") or _has_record(recs, logging.INFO, "CONFIRMED"))

# B2: real 'no' -> DECLINED template (byte-identical), log INFO DECLINED
res, recs = _run_gate("no")
check("B2 deny result == template", res == _expected_deny("no"))
check("B2 result contains NOT executed", res is not None and "The command was NOT executed." in res)
check("B2 log INFO ABGELEHNT",
      _has_record(recs, logging.INFO, "ABGELEHNT") or _has_record(recs, logging.INFO, "DECLINED"))

# B3: timeout -> SAME template (byte-identical), log WARNING (timeout)
_timeout = "[ask_user TIMEOUT: no response after 600s]"
res, recs = _run_gate(_timeout)
check("B3 timeout result == template", res == _expected_deny(_timeout))
check("B3 result contains NOT executed", res is not None and "The command was NOT executed." in res)
check("B3 log WARNING (timeout)", _has_record(recs, logging.WARNING, "NOT EXECUTED (timeout)"))

# B4: no pause event -> SAME template, log WARNING (no_pause_event)
_noev = "[ask_user ERROR: no pause event]"
res, recs = _run_gate(_noev)
check("B4 no_event result == template", res == _expected_deny(_noev))
check("B4 result contains NOT executed", res is not None and "The command was NOT executed." in res)
check("B4 log WARNING (no_pause_event)", _has_record(recs, logging.WARNING, "NOT EXECUTED (no_pause_event)"))

# B5: empty answer -> SAME template, log WARNING (empty_answer)
res, recs = _run_gate("")
check("B5 empty result == template", res == _expected_deny(""))
check("B5 result contains NOT executed", res is not None and "The command was NOT executed." in res)
check("B5 log WARNING (empty_answer)", _has_record(recs, logging.WARNING, "NOT EXECUTED (empty_answer)"))

# B6: no dead TIMEOUT branch - all deny cases use the DECLINED template
res, _recs = _run_gate("")
check("B6 no dead TIMEOUT text",
      res is not None and "DESTRUCTIVE GATE: DECLINED" in res and "DESTRUCTIVE GATE: TIMEOUT" not in res)

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
