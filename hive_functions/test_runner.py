


from __future__ import annotations

import re
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from settings import load_settings
_settings = load_settings()
_logger = logging.getLogger("test_runner")

try:
    from hive_functions.language_config import detect_project_languages, LANGUAGE_RUNNERS
except ImportError:
    try:
        from language_config import detect_project_languages, LANGUAGE_RUNNERS
    except ImportError:
        # Fallback: add hive_functions dir to sys.path and retry
        import sys, os
        _hf_dir = os.path.dirname(os.path.abspath(__file__))
        if _hf_dir not in sys.path:
            sys.path.insert(0, _hf_dir)
        from language_config import detect_project_languages, LANGUAGE_RUNNERS


@dataclass
class TestResult:
    success: bool
    language: str
    command: str
    failure_count: int
    error_lines: list[str]
    raw_output: str
    inject_msg: str = ""

    def is_clean(self) -> bool:
        return self.success and self.failure_count == 0

    def summary(self) -> str:
        if self.is_clean():
            return f"✅ Tests passed ({self.language})"
        return (
            f"❌ {self.failure_count} failure(s) ({self.language})\n"
            + "\n".join(self.error_lines[:10])
        )


def _parse_failures(output: str, language: str) -> tuple[int, list[str]]:
    """
    Extracts the failure count and relevant error lines from test output.
    Language-specific patterns — no regex wildcard chaos.
    """
    lines = output.splitlines()
    errors: list[str] = []
    count = 0

    if language == "python":
        for line in lines:
            m = re.search(r"(\d+) failed", line)
            if m:
                count = max(count, int(m.group(1)))
            if line.startswith("FAILED") or "AssertionError" in line or "Error:" in line:
                errors.append(line.strip())

    elif language in ("javascript", "typescript", "astro"):
        # jest/vitest: "Tests: 3 failed, 5 passed"
        for line in lines:
            m = re.search(r"(\d+) failed", line)
            if m:
                count = max(count, int(m.group(1)))
            if "✕" in line or "× " in line or "FAIL " in line or "Error:" in line:
                errors.append(line.strip())

    elif language == "rust":
        # cargo test: "test result: FAILED. 2 passed; 1 failed"
        for line in lines:
            m = re.search(r"(\d+) failed", line)
            if m:
                count = max(count, int(m.group(1)))
            if line.strip().startswith("FAILED") or "panicked" in line:
                errors.append(line.strip())

    elif language == "go":
        # go test: "FAIL" + "--- FAIL: TestName"
        for line in lines:
            if line.startswith("--- FAIL:"):
                count += 1
                errors.append(line.strip())
            elif "panic:" in line:
                errors.append(line.strip())

    elif language in ("java", "csharp"):
        # maven/dotnet: "Tests run: 5, Failures: 2" or "Failed: 3"
        for line in lines:
            m = re.search(r"[Ff]ailed[:\s]+(\d+)", line)
            if m:
                count = max(count, int(m.group(1)))
            if "[ERROR]" in line or "FAILED" in line:
                errors.append(line.strip())

    else:
        # triggerte auf "error handling", "no failures detected", "default_error_handler"
        # → false-positive failures bei erfolgreich laufenden Tests.
        _err_pat = re.compile(r'\b(error|exception|traceback|failed?|assert)\b', re.IGNORECASE)
        _fp_skip = ("no error", "no errors", "no fail", "no failure", "no failures",
                    "without error", "without errors", "handle", "handler",
                    "error handling", "error_handler", "on_error", "onerror",
                    "not fail", "not failed", "cannot fail", "should not",
                    "0 errors", "0 failed", "zero errors")
        for line in lines:
            lower = line.lower()
            if _err_pat.search(lower) and not any(fp in lower for fp in _fp_skip):
                errors.append(line.strip())
        count = len(errors)

    # limit error lines to relevant ones: max 15, prioritize stacktrace starts
    errors = [e for e in errors if e][:15]
    return count, errors


async def run_tests(
    workspace: str,
    timeout: int = 60,
    lang_override: Optional[str] = None,
    chat_id: str = "",
) -> TestResult:


    langs = [lang_override] if lang_override else detect_project_languages(workspace)

    if not langs:
        return TestResult(
            success=False, language="unknown", command="",
            failure_count=0, error_lines=["No detectable language/test runner in workspace"],
            raw_output="", inject_msg="⚠️ No test runner detected — verify manually."
        )

    lang = None
    cmd = None
    for l in langs:
        c = LANGUAGE_RUNNERS.get(l, {}).get("test_cmd")
        if c and "{file}" not in c:
            lang = l
            cmd = c
            break

    if not cmd:
        return TestResult(
            success=False, language=langs[0], command="",
            failure_count=0, error_lines=["No test_cmd configured for this language"],
            raw_output="", inject_msg="⚠️ No test command configured — verify manually."
        )

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return TestResult(
                success=False, language=lang, command=cmd,
                failure_count=0,
                error_lines=[f"Test command timed out after {timeout}s"],
                raw_output="TIMEOUT",
                inject_msg=f"⚠️ Test timeout ({timeout}s) — command: {cmd}"
            )

        raw = stdout.decode("utf-8", errors="replace")
        success = proc.returncode == 0
        failure_count, error_lines = _parse_failures(raw, lang)

        if success and failure_count > 0:
            success = False

        # Test history: record per-test outcomes for flaky detection
        if chat_id:
            try:
                _ids = _parse_test_ids(raw, lang)
                _outcome = "fail" if not success else "pass"
                _results = {tid: _outcome for tid in _ids}
                if _results:
                    from context.chat import update_test_history
                    update_test_history(chat_id, _results)
            except Exception:
                pass

        # (suggested_action="rerun" in the dead function). Now: a failed run
        if not success:
            try:
                _hist: dict = {}
                if chat_id:
                    from context.chat import _load_chat_context
                    _cc = _load_chat_context(chat_id)
                    if isinstance(_cc, dict):
                        _hist = _cc
                _cls = classify_test_failure(raw, lang, _hist)
                if _cls.get("type") == "flaky" and _cls.get("suggested_action") == "rerun":
                    proc2 = await asyncio.create_subprocess_shell(
                        cmd,
                        cwd=workspace,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    try:
                        stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=timeout)
                    except asyncio.TimeoutError:
                        proc2.kill()
                        await proc2.communicate()
                        stdout2 = b"TIMEOUT"
                    raw2 = stdout2.decode("utf-8", errors="replace")
                    success2 = proc2.returncode == 0
                    failure2, errors2 = _parse_failures(raw2, lang)
                    if success2 and failure2 > 0:
                        success2 = False
                    if success2:
                        if chat_id:
                            try:
                                _ids2 = _parse_test_ids(raw2, lang)
                                from context.chat import update_test_history
                                update_test_history(chat_id, {tid: "pass" for tid in _ids2})
                            except Exception:
                                pass
                        # im Erfolgsfall nirgends ausgegeben).
                        _logger.info("[FLAKY-RERUN] %s", "clean rerun")
                        inject2 = _build_inject_message(
                            success=True, lang=lang, cmd=cmd,
                            failure_count=0, error_lines=[],
                            raw=raw2,
                        )
                        return TestResult(
                            success=True, language=lang, command=cmd,
                            failure_count=0, error_lines=[],
                            raw_output=raw2,
                            inject_msg=f"[flaky rerun clean] {inject2}",
                        )
            except Exception:
                pass

        inject = _build_inject_message(
            success=success,
            lang=lang,
            cmd=cmd,
            failure_count=failure_count,
            error_lines=error_lines,
            raw=raw,
        )

        return TestResult(
            success=success, language=lang, command=cmd,
            failure_count=failure_count, error_lines=error_lines,
            raw_output=raw, inject_msg=inject,
        )

    except Exception as e:
        return TestResult(
            success=False, language=lang or "unknown", command=cmd or "",
            failure_count=0, error_lines=[str(e)],
            raw_output="", inject_msg=f"⚠️ Test runner error: {e}"
        )


def _build_inject_message(
    success: bool,
    lang: str,
    cmd: str,
    failure_count: int,
    error_lines: list[str],
    raw: str,
) -> str:


    if success:
        return f"[TEST-RESULT] ✅ All tests passed ({lang}). Command: {cmd}"

    header = f"[TEST-RESULT] ❌ {failure_count} test failure(s) ({lang})\nCommand: {cmd}\n"

    if error_lines:
        errors = "\n".join(f"  {l}" for l in error_lines)
        body = f"Errors:\n{errors}"
    else:
        body = f"Output (tail):\n{raw[-500:].strip()}"

    footer = "\n\nFix the above failures. Run tests again after fixing."
    return header + body + footer


def build_server_integration_snippet() -> str:


    return '''
# ── P3: Test Feedback Loop — inject after chunk completion ──────────────────
# Importiert oben: from hive_functions.test_runner import run_tests
# Importiert oben: from hive_functions.loop_machine import AgentState

_test_fix_attempts: dict[int, int] = {}  # chunk_idx → number of fix attempts (before loop)

# In the post-chunk block:
if _workspace and duo_agentic_mode and settings.get("duo_test_feedback_chunk", False):
    _emit_status("🧪 Verifying tests...")
    _test_result = await run_tests(workspace=_workspace, timeout=90)
    ctrl.transition(AgentState.VERIFY)

    if not _test_result.is_clean():
        _chunk_fix_count = _test_fix_attempts.get(_current_chunk_idx, 0)
        _test_fix_attempts[_current_chunk_idx] = _chunk_fix_count + 1

        _dtool_msgs.append({
            "role": "tool",
            "content": _test_result.inject_msg,
            "name": "run_bash",
        })

        max_attempts = _settings.get("duo_p3_max_fix_attempts", 3)
        if _chunk_fix_count >= max_attempts:
            _emit_status(f"⚠️ Chunk {_current_chunk_idx}: {max_attempts} fix attempts without success — continuing")
            _dtool_msgs.append({
                "role": "tool",
                "content": "[TEST-RESULT] ⛔ Max fix attempts reached. Declare as blocker and continue.",
                "name": "run_bash",
            })
        elif until_finished and ctrl.can_continue(is_aborted_callback=_is_aborted):
            _emit_status(f"🔁 {_test_result.failure_count} errors — coder fixes...")
            # continue → next loop iteration sees inject_msg and fixes
            continue
        else:
            _emit_status(f"⚠️ Tests: {_test_result.failure_count} failure(s) — critic is reviewing")
    else:
        _emit_status("✅ Tests bestanden")

    ctrl.transition(AgentState.CODING)
# ─────────────────────────────────────────────────────────────────────────────
'''


# ── Test Failure Classification ────────────────────────────────────────────

_CLASSIFY: dict[str, dict[str, list[tuple[str, float]]]] = {
    "python": {
        "dependency": [
            (r"ModuleNotFoundError: No module named '([\w.-]+)'", 0.95),
            (r"ImportError:.*No module named '([\w.-]+)'", 0.90),
        ],
        "compile": [
            (r'^\s*File "([^"]+)", line (\d+)', 0.85),
            (r"SyntaxError:", 0.95),
            (r"IndentationError:", 0.95),
        ],
        "logic": [
            (r"AssertionError:", 0.90),
            (r"assert .+ ==", 0.80),
        ],
        "timeout": [
            (r"Test command timed out after (\d+)s", 0.95),
        ],
    },
    "rust": {
        "compile": [
            (r"error\[E\d+\]:", 0.95),
            (r"\s+-->\s+([^:]+):(\d+):(\d+)", 0.95),
        ],
        "logic": [
            (r"thread '.*' panicked at", 0.90),
            (r"assertion .* failed", 0.85),
        ],
        "dependency": [
            (r"error\[E0432\]:.*import", 0.85),
            (r"error\[E0433\]:.*use of undeclared", 0.85),
        ],
    },
    "go": {
        "compile": [
            (r"^(.+?\.go):(\d+):(\d+): (.+)", 0.95),
            (r"syntax error:", 0.95),
        ],
        "logic": [
            (r"--- FAIL: (\w+)", 0.90),
            (r"panic:", 0.85),
        ],
        "dependency": [
            (r'cannot find package "([^"]+)"', 0.95),
        ],
    },
    "javascript": {
        "dependency": [
            (r"Cannot find module '([^']+)'", 0.95),
            (r"Error: Cannot find module '([^']+)'", 0.95),
        ],
        "compile": [
            (r"SyntaxError:", 0.95),
            (r"TypeError:", 0.80),
            (r"ReferenceError:", 0.85),
        ],
        "logic": [
            (r"Expected:", 0.75),
            (r"Received:", 0.75),
        ],
    },
    "typescript": {
        "dependency": [
            (r"Cannot find module '([^']+)'", 0.95),
        ],
        "compile": [
            (r"error TS\d+:", 0.95),
            (r"SyntaxError:", 0.95),
        ],
        "logic": [
            (r"Expected:", 0.75),
            (r"Received:", 0.75),
        ],
    },
    "java": {
        "compile": [
            (r"error:\s", 0.90),
            (r"\[ERROR\].*:[0-9]+,\d+", 0.85),
        ],
        "logic": [
            (r"FAILED", 0.70),
            (r"junit\.framework\.AssertionFailedError", 0.95),
        ],
        "dependency": [
            (r"package ([^\s]+) does not exist", 0.95),
        ],
    },
    "csharp": {
        "compile": [
            (r"error CS\d+:", 0.95),
        ],
        "logic": [
            (r"FAILED", 0.70),
        ],
        "dependency": [
            (r"CS0246:.*type or namespace name", 0.85),
        ],
    },
}


def _extract_missing_package(output: str, language: str = "") -> str | None:
    """Extract the missing package name from a dependency error."""
    dep_patterns = _CLASSIFY.get(language, {}).get("dependency", [])
    for pattern, _conf in dep_patterns:
        m = re.search(pattern, output, re.MULTILINE)
        if m and m.groups():
            return m.group(1).strip()
    return None


def _parse_test_ids(output: str, language: str) -> list[str]:
    """Extract individual test names/IDs from test runner output."""
    ids: list[str] = []

    if language == "python":
        # pytest: "FAILED test_file.py::test_name - ..." or "test_file.py::test_name PASSED"
        for m in re.finditer(r"(\S+::\S+)\s+(PASSED|FAILED)", output):
            ids.append(m.group(1))
        # Also catch: "test_file.py::test_name" in error traces
        for m in re.finditer(r"FAILED\s+(\S+\.py::\S+)", output):
            tid = m.group(1).strip()
            if tid not in ids:
                ids.append(tid)

    elif language == "rust":
        # cargo: "test test_name ... FAILED" or "test test_name ... ok"
        for m in re.finditer(r"test\s+(\S+)\s+\.\.\.\s+(FAILED|ok)", output):
            ids.append(m.group(1))

    elif language in ("javascript", "typescript", "astro"):
        # jest/vitest: "✕ test name" or "× test name" or "✓ test name"
        # Examples: "✕ Button > renders correctly (45ms)" → "Button > renders correctly"
        #           "✓ App (23ms)" → "App"
        #           "× ComponentName > nested > deep test" → "ComponentName > nested > deep test"
        for m in re.finditer(r"[✕×✓]\s+(.+)(?:\s+\(\d+\s*m?s\))?\s*$", output, re.MULTILINE):
            ids.append(m.group(1).strip())

    elif language == "go":
        # go test: "--- FAIL: TestName"
        for m in re.finditer(r"--- (FAIL|PASS):\s+(\S+)", output):
            ids.append(m.group(2))

    elif language in ("java", "csharp"):
        # maven/dotnet: "  TestName.testMethod FAILED"
        for m in re.finditer(r"^\s+(\S+\.\S+)\s+(FAILED|PASSED)", output, re.MULTILINE):
            ids.append(m.group(1))

    return ids


def is_flaky(outcomes: list[str]) -> bool:
    """True if last 4 outcomes show alternating pass/fail pattern (genuine flakiness).

    Requires: minimum 3 outcomes, both pass AND fail present, AND at least one
    direction switch in each direction (pass->fail AND fail->pass). This prevents
    false-positives on genuine regressions like [pass, pass, fail, fail]."""
    recent = outcomes[-4:]
    if len(recent) < 3:
        return False
    if "pass" not in recent or "fail" not in recent:
        return False
    pass_to_fail = any(recent[i] == "pass" and recent[i + 1] == "fail" for i in range(len(recent) - 1))
    fail_to_pass = any(recent[i] == "fail" and recent[i + 1] == "pass" for i in range(len(recent) - 1))
    return pass_to_fail and fail_to_pass


def classify_test_failure(output: str, language: str, chat_history: dict | None = None) -> dict:
    """
    Classify test failure into: dependency | logic | flaky | compile | timeout | unknown.

    Returns {"type": str, "confidence": float, "affected_files": [...], "suggested_action": str}.
    Uses deterministic regex patterns + test history for flaky detection — no LLM call needed.
    """
    # 1. Check test history for flaky tests (multi-run comparison)
    if chat_history:
        _ids = _parse_test_ids(output, language)
        _hist = chat_history.get("test_history", {})
        if _ids and any(is_flaky(_hist.get(tid, {}).get("outcomes", [])) for tid in _ids):
            return {
                "type": "flaky",
                "confidence": 0.9,
                "affected_files": [],
                "suggested_action": "rerun",
            }

    # 2. Regex-based classification
    patterns = _CLASSIFY.get(language, {})
    best_type = "unknown"
    best_conf = 0.0

    for fail_type, rules in patterns.items():
        for pattern, confidence in rules:
            m = re.search(pattern, output, re.MULTILINE)
            if m and confidence > best_conf:
                best_type = fail_type
                best_conf = confidence

    if best_type == "unknown":
        if "TIMEOUT" in output or "timed out" in output.lower():
            best_type = "timeout"
            best_conf = 0.9

    affected_files: list[str] = []
    for m in re.finditer(r'(?:File "|--> |\.\/)([^\n"():]+)(?:"|:|\()', output):
        f = m.group(1).strip()
        if f and f not in affected_files and "." in f:
            affected_files.append(f)
    affected_files = affected_files[:5]

    actions = {
        "dependency": "install_dep",
        "logic":       "fix_code",
        "flaky":       "rerun",
        "compile":     "fix_compile",
        "timeout":     "ask_user",
        "unknown":     "ask_user",
    }

    return {
        "type": best_type,
        "confidence": round(best_conf, 2),
        "affected_files": affected_files,
        "suggested_action": actions.get(best_type, "ask_user"),
    }
