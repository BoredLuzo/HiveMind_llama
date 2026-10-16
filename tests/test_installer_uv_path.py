"""Installer must tell the user when PATH changed (uv installed but not visible).

Owner report from a colleague: "uv was installed but not in PATH and the
installer crashed". The crash itself was the 2026-10-14 UV-ROBUST fix (batch
expands %PATH% at parse time, so the old in-block `set PATH` + `where uv` never
saw a fresh install and exited /b 1 with "uv could not be installed").

What was left: an install that SUCCEEDS still only fixes the PATH of the
installer's own process. Every other terminal - and the next one opened from an
old shortcut - may keep "uv is not recognized", which is the second half of the
same report. Both installers now say so:

- install.bat sets UV_PATH_ADDED when it had to extend PATH, prints a NOTE right
  after the uv step, and repeats it in the closing summary.
- install_linux.sh names the /usr/local/bin PATH problem and the fix instead of
  running `command -v uv && uv --version` and moving on (step [4/8] falls back
  to python3+pip in that case).

Run: python tests/test_installer_uv_path.py
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


_bat = (ROOT / "install.bat").read_text(encoding="utf-8", errors="replace")
_sh = (ROOT / "deploy" / "install_linux.sh").read_text(encoding="utf-8", errors="replace")

# ── install.bat ─────────────────────────────────────────────────────────────
check("batch sets a PATH-added flag", "set \"UV_PATH_ADDED=1\"" in _bat)
check("flag only when uv was NOT already on PATH",
      _bat.index('if not "%UV_DIR%"=="on_path" set "UV_PATH_ADDED=1"')
      < _bat.index("echo   [uv] ok:"))
check("immediate NOTE after the uv step", "uv was added to THIS window's PATH only" in _bat)
check("NOTE tells the user to open a new terminal",
      "open a NEW terminal before running uv yourself" in _bat)
check("closing summary repeats it, guarded by the flag",
      "if defined UV_PATH_ADDED (" in _bat
      and "open a NEW terminal first" in _bat)
check("the summary note sits in the closing block, after the gateway hint",
      _bat.index("if defined UV_PATH_ADDED (") > _bat.index("Optional:     Telegram gateway")
      and _bat.index("if defined UV_PATH_ADDED (") < _bat.rindex("echo  =========="))

# the 2026-10-14 crash fix must still be there (do not regress it)
check("uv detection stays existence-based over all known locations",
      '"%USERPROFILE%\\.local\\bin\\uv.exe"' in _bat
      and '"%LOCALAPPDATA%\\Microsoft\\WinGet\\Links\\uv.exe"' in _bat)
check("PATH is still applied outside the parenthesized block",
      _bat.index('set "PATH=%PATH%;%UV_DIR%"') > _bat.index("if not defined UV_DIR ("))
check("hard failure path is still explicit",
      "[ERROR] uv could not be installed." in _bat)

# ── install_linux.sh ────────────────────────────────────────────────────────
check("linux installer checks uv after the install",
      "if command -v uv >/dev/null 2>&1; then" in _sh)
check("linux names the PATH problem", "uv is installed but NOT on this shell's PATH" in _sh)
check("linux gives the fix", r'export PATH=\"\$PATH:/usr/local/bin\"' in _sh)
check("linux mentions the python3+pip fallback", "falls back to python3+pip" in _sh)
check("no bare && uv --version left", "&& uv --version" not in _sh)
check("uv still installs to /usr/local/bin",
      'UV_INSTALL_PATH="/usr/local/bin"' in _sh)
check("the uv block is if/then/else/fi closed",
      "if command -v uv >/dev/null 2>&1; then" in _sh
      and _sh.split("if command -v uv >/dev/null 2>&1; then", 1)[1].split("\nfi\n", 1)[0]
      .count("else") == 1)

print()
print(f"SUMMARY installer_uv_path: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
