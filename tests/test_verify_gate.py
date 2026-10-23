"""The verify gate must never let an unverified completion look verified."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.duo.preflight import completion_verdict  # noqa: E402

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

# ── the retry is accepted, but marked ────────────────────────────────────
check("retry without verification -> unverified (never ok)",
      completion_verdict(mutation_serial=5, last_ok_serial=1,
                         has_written_files=True, vetoes=1) == "unverified")
check("a green test after the write flips it back to ok",
      completion_verdict(mutation_serial=5, last_ok_serial=5,
                         has_written_files=True, vetoes=1) == "ok")

# ── the runner must surface it, not swallow it ───────────────────────────
src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
check("runner uses the verdict helper", "completion_verdict(" in src)
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
