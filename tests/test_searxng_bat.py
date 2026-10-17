"""searxng.bat must never close silently (live report 2026-10-17).

Owner report: "searxng.bat is in the zip but it just closed itself on my
system." Reproduced on the released 1.3.9 artifact:

  * no arguments (double-click) -> ACTION defaults to `status`;
  * with Docker Desktop stopped, `docker ps` fails, the script did
    `if errorlevel 1 exit /b 1` - raw docker error, no message, no pause, window
    gone. With Docker not installed at all it printed one line and closed;
  * `status` fell through into the `:running` block, so a STOPPED container was
    reported as "[OK] SearXNG is running".

What this test pins:
  (a) a `:finish` epilogue that pauses when the script was called without
      arguments (double-click) and exits with a real code otherwise;
  (b) every path routes through it (`exit /b <n>` is gone from the actions);
  (c) the Docker *daemon* is probed (`docker info`), not just the CLI on PATH;
  (d) `status` distinguishes running / stopped / not-created and cannot fall
      through into `:running`;
  (e) behaviourally: with a dead daemon the script prints the explanation and
      still asks for a key press - i.e. it does not vanish;
  (f) the file stays ASCII + CRLF (it is shipped in the zip).

Run: python tests/test_searxng_bat.py
"""
import io
import os
import re
import subprocess
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


BAT = ROOT / "searxng.bat"
raw = BAT.read_bytes()
src = raw.decode("ascii", errors="replace")

# ── (f) shipped format ──────────────────────────────────────────────────────
check("file is ASCII", len([b for b in raw if b > 127]) == 0)
check("file uses CRLF", raw.count(b"\r\n") > 50 and raw.count(b"\n") == raw.count(b"\r\n"))

# ── (a) epilogue ────────────────────────────────────────────────────────────
# the LAST ":finish" is the label; earlier hits are "goto :finish" calls
_fin_i = src.rindex(":finish")
body, epilogue = src[:_fin_i], src[_fin_i:]
check("has a :finish epilogue", ":finish" in src)
check("interactive flag is derived from 'no arguments'",
      'set "INTERACTIVE="' in src and 'set "INTERACTIVE=1"' in src
      and '%ARG1%' in src)
check("the epilogue pauses only when interactive",
      "if defined INTERACTIVE (" in epilogue and "pause >nul" in epilogue)
check("the epilogue exits with the collected code", "exit /b %RC%" in epilogue)

# ── (b) no silent exits from the action paths ───────────────────────────────
bare = re.findall(r"^\s*exit /b 1\s*$", body, re.M)
check("no bare 'exit /b 1' outside the epilogue", not bare, f" ({bare})")
_n_rc = body.count('set "RC=')
check("paths collect a code before leaving", _n_rc >= 6, f" ({_n_rc})")

# ── (c) daemon probe ────────────────────────────────────────────────────────
check("probes the docker daemon, not just the CLI", "docker info >nul 2>&1" in src)
check("says Docker Desktop is not running",
      "Docker Desktop is not running" in src)

# ── (d) status truth ────────────────────────────────────────────────────────
status_block = src.split(":status", 1)[1].split(":running", 1)[0]
check("status reads the real state",
      'docker inspect -f "{{.State.Running}}"' in status_block)
check("status can report a stopped container", "is STOPPED" in status_block)
check("status can report a missing container", "Container not created" in status_block)
check("status only enters :running when the state is true",
      'if /i "%RUNNING%"=="true" goto running' in status_block)
check("the old unconditional fall-through is gone",
      'if errorlevel 1 ( echo  [INFO] Container not created' not in src)
check("workdir is normalised", 'cd /d "%~dp0"' in src)
check("the config folder is checked before use",
      'if not exist "%~dp0searxng-config"' in src)

# ── (e) behaviour with a dead daemon ────────────────────────────────────────
if os.name != "nt":
    check("behavioural run (Windows only)", True, " (skipped on POSIX)")
else:
    env = dict(os.environ, DOCKER_HOST="tcp://127.0.0.1:1")
    for args in ([], ["status"]):
        r = subprocess.run(["cmd", "/c", str(BAT)] + args, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           env=env, timeout=120, input="\n")
        out = (r.stdout or "") + (r.stderr or "")
        label = "no args" if not args else "status"
        check(f"dead daemon / {label}: explains the problem",
              "Docker Desktop is not running" in out)
        # double-click (no args) must keep the window open; a call with an
        # argument from an open terminal must not block on a key press
        if not args:
            check("dead daemon / no args: does not vanish silently",
                  "Press any key" in out)
        else:
            check("dead daemon / status: no pause for scripted use",
                  "Press any key" not in out)
        check(f"dead daemon / {label}: exit code 1", r.returncode == 1,
              f" (exit {r.returncode})")
        check(f"dead daemon / {label}: no raw docker traceback",
              "error during connect" not in out.lower())

print()
print(f"SUMMARY searxng_bat: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
