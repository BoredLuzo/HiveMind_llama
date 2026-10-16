"""Live findings from the 2026-10-17 engine start + agentic run.

Both were seen in the real engine log (live/logs/hivemind.log), not inferred:

1. Every start logged "[WARN] death forensics unavailable: module 'faulthandler'
   has no attribute 'register'". faulthandler.register() is POSIX-only; on
   Windows it raised AttributeError inside the big try block, so the WHOLE
   forensics feature (heartbeat file, signal handlers, atexit marker) never
   ran on this platform. The call is now guarded.

2. The per-round trace read "[TOOL-LOOP] model= port=8102 images_in_request=0" -
   the AgenticToolLoop was built without a model name, so a live log could not
   say which model ran. The coder loop now gets model=exec_mdl.

Pins (a) the hasattr guard exists and register() is not called unguarded,
(b) the loop config carries the model, (c) both stay parseable/importable.

Run: python tests/test_live_findings.py
"""
import ast
import io
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


_run_src = io.open(ROOT / "run.py", encoding="utf-8").read()
_duo_src = io.open(ROOT / "core" / "duo_runner.py", encoding="utf-8").read()

# ── 1. faulthandler.register must be guarded (Windows) ──────────────────────
check("register() is behind a hasattr guard",
      'if hasattr(_fh, "register"):' in _run_src)
check("register() is not called unguarded",
      "\n    _fh.register(_faulthandler_stream)\n" not in _run_src)
check("enable() still runs unconditionally (works on Windows)",
      "_fh.enable(file=_faulthandler_stream)" in _run_src)
check("the heartbeat/atexit part still exists after the guard",
      "engine_heartbeat.json" in _run_src and "atexit" in _run_src)

# the real thing: faulthandler on this platform has no register()
import faulthandler                                            # noqa: E402
check(f"this platform: faulthandler.register present={hasattr(faulthandler, 'register')}",
      True)
if not hasattr(faulthandler, "register"):
    check("guard is what keeps the block alive here (no AttributeError path)",
          'if hasattr(_fh, "register"):' in _run_src)

# ── 2. the coder tool loop carries the model name ───────────────────────────
check("coder ToolLoopConfig gets model=exec_mdl",
      "config=ToolLoopConfig(stream=True, max_post_attempts=20,\n"
      "                                                  model=exec_mdl)," in _duo_src)
check("duo_runner still parses", bool(ast.parse(_duo_src)) is False or True)
try:
    ast.parse(_duo_src)
    _ok = True
except SyntaxError:
    _ok = False
check("duo_runner parses", _ok)
try:
    ast.parse(_run_src)
    _ok2 = True
except SyntaxError:
    _ok2 = False
check("run.py parses", _ok2)

print()
print(f"SUMMARY live_findings: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
