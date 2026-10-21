"""Installer: Core Utils werden optional mitinstalliert (wenn verfuegbar).

Modelle schreiben `grep`, `sed`, `awk`, `find`; Windows kennt sie nicht.
Microsoft Core Utils sind der offizielle Port. Der Installer fragt, installiert
ueber winget und bricht NIE deswegen ab.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


RAW = (ROOT / "install.bat").read_bytes()
TEXT = RAW.decode("ascii", errors="replace")
LINES = TEXT.splitlines()

# ── 1. Der Schritt existiert und ist nummeriert ─────────────────────────
check("install.bat is ascii", all(ord(c) < 128 for c in TEXT))
check("install.bat is CRLF only", RAW.count(b"\n") == RAW.count(b"\r\n"))
check("no doubled CR", RAW.count(b"\r\r\n") == 0)

for n in range(1, 10):
    check(f"step [{n}/9] listed in the overview",
          f"echo    [{n}/9]" in TEXT)
check("no stale /8 numbering left", "/8]" not in TEXT)

check("the tools step has a header",
      "REM [4/9] Windows tools (optional: POSIX tools for the agent)" in TEXT)
check("the step explains why", "trained on Linux" in TEXT
      and 'grep -rn "foo" src/' in TEXT)

# ── 2. Erkennung + Installationsbefehl ──────────────────────────────────
check("detects an existing install", "where grep >nul 2>&1" in TEXT)
check("checks for winget", "where winget >nul 2>&1" in TEXT)
check("installs the official package id",
      "winget install --id Microsoft.CoreUtils -e --silent" in TEXT)
check("accepts the agreements (unattended)",
      "--accept-package-agreements" in TEXT and "--accept-source-agreements" in TEXT)
check("asks before installing", 'choice /c YN /n /m "Install Core Utils? [Y/N] "' in TEXT)

# ── 3. Nie fatal: jeder Ausgang endet im gemeinsamen Label ─────────────
for label in (":coreutils_done", ":coreutils_failed", ":coreutils_nowinget",
              ":coreutils_skipped", ":coreutils_ask"):
    check(f"label {label} exists", label in TEXT)
check("every branch jumps to coreutils_done", TEXT.count("goto coreutils_done") >= 4,
      f"({TEXT.count('goto coreutils_done')})")
check("the skipped path mentions the manual command",
      "Skipped - add it later with: winget install Microsoft.CoreUtils" in TEXT)
check("the missing-winget path points at the docs",
      "learn.microsoft.com/en-us/windows/core-utils" in TEXT)
check("no exit /b inside the tools step",
      "exit /b" not in TEXT[TEXT.index("REM [4/9] Windows tools"):
                           TEXT.index("REM [5/9] Desktop shortcut")])

# ── 4. Der Schritt laeuft VOR den Modelldownloads ──────────────────────
idx_tools = TEXT.index("REM [4/9] Windows tools")
idx_models = TEXT.index("REM [6/9] Models")
check("tools step comes before the model downloads", idx_tools < idx_models)
idx_shortcut = TEXT.index("REM [5/9] Desktop shortcut")
check("order is llama, tools, shortcut", TEXT.index("REM [3/9] llama.cpp backend")
      < idx_tools < idx_shortcut < idx_models)

print()
print(f"SUMMARY installer_coreutils: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
