"""Eval: tool error taxonomy (tools/errors.py) - format A/B, matching, round-trip.

Deterministic, no LLM needed. Forms the foundation for the missing
Retry/backoff taxonomy: as long as codes/tools are parsed reliably,
the harness layer can be built on top.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.errors import (
    tool_error_response,
    parse_tool_error,
    tool_call_failed,
    tool_error_has_code,
)

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


# ── Format B: text -> parse ─────────────────────────────────────────────
r = tool_error_response("RUN_BASH_NONZERO", "exit 1", tool="run_bash")
e = parse_tool_error(r)
check("B: code", e and e["code"] == "RUN_BASH_NONZERO")
check("B: tool", e and e["tool"] == "run_bash")
check("B: message", e and e["message"] == "exit 1")

r2 = tool_error_response("RUN_PYTHON_TIMEOUT", "timed out", tool="run_python", mode="tool_agent")
e2 = parse_tool_error(r2)
check("B+mode: tool", e2 and e2["tool"] == "run_python")
check("B+mode: mode", e2 and e2["mode"] == "tool_agent")
check("B+mode: code", e2 and e2["code"] == "RUN_PYTHON_TIMEOUT")

# ── Format A: JSON -> parse ──────────────────────────────────────────────
a = '[TOOL_ERROR] {"error": {"code": "READ_REQUIRED", "tool": "edit_file", "message": "read first"}}'
ea = parse_tool_error(a)
check("A: code", ea and ea["code"] == "READ_REQUIRED")
check("A: tool", ea and ea["tool"] == "edit_file")
check("A: message", ea and ea["message"] == "read first")

# ── Round-Trip ──────────────────────────────────────────────────────────
rt = tool_error_response("GIT_COMMIT_FAILED", "boom", tool="git_commit", details={"rc": 1})
ert = parse_tool_error(rt)
check("roundtrip: code", ert and ert["code"] == "GIT_COMMIT_FAILED")
check("roundtrip: tool", ert and ert["tool"] == "git_commit")
check("roundtrip: message", ert and ert["message"] == "boom")

# ── tool_call_failed ────────────────────────────────────────────────────
check("failed: error true", tool_call_failed("[TOOL_ERROR:X] t: m") is True)
check("failed: success false", tool_call_failed("all good, exit 0") is False)
check("failed: empty false", tool_call_failed("") is False)
check("failed: tool match", tool_call_failed('[TOOL_ERROR:Y] run_bash: nope', "run_bash") is True)
check("failed: tool mismatch",
      tool_call_failed('[TOOL_ERROR:Y] run_bash: nope', "run_python") is False)

# ── tool_error_has_code ─────────────────────────────────────────────────
check("has_code: match", tool_error_has_code("[TOOL_ERROR:RETRY] t: m", "RETRY") is True)
check("has_code: miss", tool_error_has_code("[TOOL_ERROR:RETRY] t: m", "NOPE") is False)
check("has_code: tool filter ok",
      tool_error_has_code("[TOOL_ERROR:RETRY] run_bash: m", "RETRY", "run_bash") is True)
check("has_code: tool filter reject",
      tool_error_has_code("[TOOL_ERROR:RETRY] run_bash: m", "RETRY", "run_python") is False)

# ── Sonderfall READ_REQUIRED (Format "Action: ... BLOCKED") ─────────────
special = "[TOOL_ERROR: READ_REQUIRED]\nAction: edit_file BLOCKED on 'x.py'"
es = parse_tool_error(special)
check("special: tool extracted", es and es["tool"] == "edit_file")
check("special: code", es and es["code"] == "READ_REQUIRED")
check("special: parse not None", es is not None)

# ── Meta-Block ──────────────────────────────────────────────────────────
r_meta = tool_error_response("C", "m", tool="t", details={"a": 1, "b": [1, 2]})
check("meta: contains META", "[TOOL_ERROR_META]" in r_meta)
em = parse_tool_error(r_meta)
check("meta: parse code intact", em and em["code"] == "C")

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
