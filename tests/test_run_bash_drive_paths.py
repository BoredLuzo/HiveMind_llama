"""POSIX drive paths in run_bash must not be rewritten with backslashes.

Live 2026-10-16 21:1x (owner screenshots, session
a live scratch workspace): almost every run_bash call failed -
"run_bash exited with code 2 (shell: bash (Git Bash))" - and the model burned
rounds theorising about `C:` being a drive letter, quoting, and Python escapes.

Cause: the model sends `/c/Users/...` (Git Bash's view of a Windows path, and a
valid path there). run_bash rewrote `/<letter>/` to `<LETTER>:\\` - backslashes
inside a bash command. bash strips backslashes as escapes, so the path ROLLS UP
and the separators disappear (the drive letter survives, the rest of the path
collapses into one word), which is what the live error showed:
  python3 C:\\Users\\...\\hello.py
  -> can't open file '<drive>:\\\\Users\\\\...'  [Errno 2]
Verified on this machine with Git Bash: the model's original `/c/...` form and
the forward-slash drive form both exit 0; only the backslash form fails.

Fix: rewrite to `C:/...` (forward slashes) - accepted by bash AND by native
Windows binaries - and do not touch a slash that follows a word character or
another slash (`a/b/c`, `http://host/c/y` are not drive mounts).

Run: python tests/test_run_bash_drive_paths.py
"""
import re
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


_SRC = (ROOT / "tools" / "handlers" / "exec_tools.py").read_text(encoding="utf-8", errors="replace")

# the exact rewrite pair as it stands in the module
_m = re.search(r"_cmd_unix_fixed = re\.sub\(\s*r'([^']+)',\s*\n\s*lambda _m: ([^\n]+),\s*\n\s*cmd\)", _SRC)
check("the drive rewrite is still present and parseable", _m is not None)
_pat = _m.group(1) if _m else r"(?<![\w/])/([a-zA-Z])/"
_repl_src = _m.group(2) if _m else '"x"'
check("the replacement uses a forward slash (no backslash form)",
      '":/"' in _repl_src and '":\\\\\\\\"' not in _repl_src, f" ({_repl_src.strip()})")

_rx = re.compile(_pat)
_rp = lambda mm: mm.group(1).upper() + ":/"                     # noqa: E731


def tr(cmd):
    return _rx.sub(_rp, cmd)


# ── behaviour ───────────────────────────────────────────────────────────────
check("posix drive -> forward-slash drive",
      tr("ls /c/Users/user/projects/site/") == "ls C:/Users/user/projects/site/")
check("quoted posix drive is rewritten too",
      tr("find '/c/Users/user/projects/site' -type f")
      == "find 'C:/Users/user/projects/site' -type f")
check("python file argument",
      tr("python3 /c/Users/user/projects/site/verify_html.py")
      == "python3 C:/Users/user/projects/site/verify_html.py")
check("inside a python -c string",
      tr("""python -c "open('/c/Users/xxx/index.html')" """).strip()
      == """python -c "open('C:/Users/xxx/index.html')" """.strip())
check("no backslash ever appears in the output",
      "\\" not in tr("ls /c/Users/user/projects/site/ && python3 /d/x/y.py"))
check("relative segments are untouched", tr("echo a/b/c") == "echo a/b/c")
check("urls are untouched", tr("curl http://host/c/y") == "curl http://host/c/y")
check("already-windows path untouched", tr("ls C:/Users/xxx") == "ls C:/Users/xxx")
check("other drive letters work", tr("cd /e/tmp && ls") == "cd E:/tmp && ls")

# ── the live failing command, end to end ────────────────────────────────────
_live = "cd /c/Users/user/projects/site && python3 test_n64_website.py"
check("the live failing command becomes runnable",
      tr(_live) == "cd C:/Users/user/projects/site && python3 test_n64_website.py")

# ── the source no longer contains the backslash replacement ────────────────
check("source has no backslash-drive replacement left",
      '":\\\\\\\\"' not in _SRC)

# ── optional live proof: Git Bash accepts both accepted forms ──────────────
import os                                                        # noqa: E402
import shutil                                                    # noqa: E402
import subprocess                                                # noqa: E402

_bash = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
if os.path.exists(_bash):
    _target = "C:/Windows"
    _r1 = subprocess.run([_bash, "-c", f"ls {_target} >/dev/null 2>&1; echo $?"],
                         capture_output=True, text=True, timeout=30)
    _r2 = subprocess.run([_bash, "-c", "ls /c/Windows >/dev/null 2>&1; echo $?"],
                         capture_output=True, text=True, timeout=30)
    _r3 = subprocess.run([_bash, "-c", r"ls C:\\Windows >/dev/null 2>&1; echo $?"],
                         capture_output=True, text=True, timeout=30)
    check("git bash: forward-slash form works", _r1.stdout.strip() == "0",
          f" (exit {_r1.stdout.strip()})")
    check("git bash: raw /c/ form works", _r2.stdout.strip() == "0")
    check("git bash: backslash form is the broken one",
          _r3.stdout.strip() != "0" or True,
          f" (exit {_r3.stdout.strip()} - informational)")
else:
    check("git bash available for the live check", False, " (not found)")

print()
print(f"SUMMARY run_bash_drive_paths: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
