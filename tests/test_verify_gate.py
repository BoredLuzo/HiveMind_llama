"""The verify gate must never let an unverified completion look verified."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.duo.preflight import completion_verdict, needs_test_verification  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── no write since the last green test -> nothing to verify ──────────────
check("no write since last green test -> ok",
      completion_verdict(mutation_serial=1, last_ok_serial=1,
                         has_written_files=True, vetoes=0) == "ok")
check("write but no file recorded (e.g. read-only task) -> ok",
      completion_verdict(mutation_serial=5, last_ok_serial=1,
                         has_written_files=False, vetoes=0) == "ok")

# ── first completion after an unverified write -> veto ───────────────────
check("first completion after a write -> veto",
      completion_verdict(mutation_serial=5, last_ok_serial=1,
                         has_written_files=True, vetoes=0) == "veto")

# ── the veto budget: three nags, then accepted-but-marked ────────────────
for _v in (1, 2):
    check(f"veto {_v+1} still asks for the test run",
          completion_verdict(mutation_serial=5, last_ok_serial=1,
                             has_written_files=True, vetoes=_v) == "veto")
check("after the budget -> unverified (never ok)",
      completion_verdict(mutation_serial=5, last_ok_serial=1,
                         has_written_files=True, vetoes=3) == "unverified")
check("a green test after the write flips it back to ok",
      completion_verdict(mutation_serial=5, last_ok_serial=5,
                         has_written_files=True, vetoes=2) == "ok")

# ── static assets do not trigger the workspace test suite ────────────────
check("a lone HTML file needs no test run",
      needs_test_verification(["C:/ws/page.html"]) is False)
check("CSS/MD/SVG need no test run",
      needs_test_verification([
          "ws/style.css", "ws/README.md", "ws/icon.svg",
      ]) is False)
check("JSON/Makefile/Dockerfile/extensionless still need verification",
      all(needs_test_verification([p]) for p in (
          "ws/package.json", "ws/Makefile", "ws/Dockerfile", "scripts/entry")))
check("a Python/Go/TS change still needs verification",
      all(needs_test_verification([p]) for p in (
          "ws/app.py", "ws/main.go", "ws/ui.ts", "ws/lib.rs")))
check("one code file in a mixed change needs verification",
      needs_test_verification(["ws/page.html", "ws/app.py"]) is True)
check("no recorded files needs no test run",
      needs_test_verification(None) is False)

# ── the runner must surface it, not swallow it ───────────────────────────
src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
# split 2.5: the tool-round coder prompt (bridge message) lives in duo_dtool_setup.py
src += (ROOT / "core" / "duo_dtool_setup.py").read_text(encoding="utf-8", errors="replace")
# C3 (2026-11-07): the verify gate lives in duo_task_complete_gate.py now
src += (ROOT / "core" / "duo_task_complete_gate.py").read_text(encoding="utf-8", errors="replace")
check("runner uses the verdict helper", "completion_verdict(" in src)
check("runner skips the veto for static-only changes",
      "not needs_test_verification(_written_files)" in src)
check("runner also skips the final verify hard-stop for static-only changes",
      (src + (ROOT / "core" / "duo_finalgate.py").read_text(encoding="utf-8", errors="replace"))
      .count("needs_test_verification(") >= 3)
te = (ROOT / "core" / "tool_executor.py").read_text(encoding="utf-8", errors="replace")
check("the pre-complete auto-test skips static-only changes",
      "needs_test_verification" in te
      and "task_complete accepted without tests" in te)
check("the static-only acceptance respects the chunk-incomplete guard",
      "deferring to the chunk-complete check" in te)
check("a static-only test skip is not treated as a failed test",
      '"⏭" not in _content' in te)
check("the static-only skip tells the model to stop calling run_tests",
      "call run_tests again in this run." in te
      and "call run_tests again in this run." in
      (ROOT / "tools" / "runner.py").read_text(encoding="utf-8", errors="replace"))
_fin = (ROOT / "core" / "duo_finalize.py").read_text(encoding="utf-8", errors="replace")
check("loop_detected still delivers explicitly queued files",
      "_loop_file_delivery" in _fin
      and "explicitly queued file(s) now" in _fin)
check("the agentic prompt exempts static assets from the test suite",
      "do NOT run the project test suite for those" in src)
check("the agentic prompt forbids tests before the first write",
      "Do not run tests before your first write/edit" in src)
ex = (ROOT / "tools" / "handlers" / "exec_tools.py").read_text(
    encoding="utf-8", errors="replace")
check("run_tests skips static-only runs itself",
      "No testable code changed (static assets only)" in ex)

# Behavioral: the handler returns the skip result and never calls the suite.
import asyncio  # noqa: E402
from tools import runner as _tr  # noqa: E402
from tools.handlers import _shared as _hshared  # noqa: E402
from tools.handlers.exec_tools import _inline_tool_run_tests  # noqa: E402

_orig_suite = _hshared._run_test_suite
_called = {"n": 0}
async def _must_not_run(**_kw):
    _called["n"] += 1
    raise AssertionError("the suite must not run for static-only changes")
_hshared._run_test_suite = _must_not_run
_tok = _tr._files_written_in_run.set({"C:/ws/page.html"})
try:
    _out = asyncio.run(_inline_tool_run_tests({}, Path("C:/ws"), "C:/ws"))
finally:
    _tr._files_written_in_run.reset(_tok)
    _hshared._run_test_suite = _orig_suite
check("static-only run_tests returns the skip result without a suite run",
      "test run skipped" in _out and _called["n"] == 0)
run_src = (ROOT / "tools" / "runner.py").read_text(
    encoding="utf-8", errors="replace")
check("static-only run_tests is skipped before the approval card",
      'if name == "run_tests" and _written_set' in run_src
      or 'if name == "run_tests":' in run_src)
check("written files are recorded run-wide, not only in one task context",
      "_files_written_by_run" in run_src)
check("the tool executor skips run_tests for static-only changes",
      'if _dname == "run_tests" and (trs.file_changes or trs.run_written_files)' in te
      and "only static assets changed" in te)
check("the executor receives the run-wide written-file list",
      "run_written_files=_written_files" in src
      and "run_written_files" in
      (ROOT / "core" / "tool_exec_helpers.py").read_text(
          encoding="utf-8", errors="replace"))
check("runner tracks the unverified completion",
      "_completion_unverified = True" in src)
check("runner tells the user",
      "treat this result as unverified" in src)
check("the accept branch says it was not verified",
      "WITHOUT a green test run" in src
      and "result marked unverified" in src)

# ── AUTO-VERIFY (2026-10-23): the veto must not depend on the model ──────
check("the harness runs the suite itself on veto",
      "[AUTO-VERIFY] The harness ran the test suite itself" in src)
check("green auto-verify accepts the completion as verified",
      "completion accepted as verified" in src
      and "_verify_last_ok_serial = _verify_mutation_serial" in src)
check("green auto-verify re-arms the veto budget",
      "_verify_gate_blocks = 0" in src)
check("red auto-verify feeds concrete failures back",
      "re-ran the tests itself and they FAILED" in src)
check("auto-verify runs the mapped tests, not always the full suite",
      "paths=_av_paths" in src)
check("no-suite veto asks for a smoke test instead of claiming failure",
      "not _av_res.command" in src)
check("the chunk loop passes written files to the mapping",
      "written_files=written_files or list(self.written_files)" in
      (ROOT / "hive_functions" / "chunking.py").read_text(encoding="utf-8", errors="replace"))

print()
print(f"SUMMARY verify_gate: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
