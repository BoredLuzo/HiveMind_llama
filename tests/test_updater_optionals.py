# -*- coding: utf-8 -*-
"""Updater offers the optional components that are not installed yet.

A release can add optional pieces (voice in/out, routing decider, browser
engine, POSIX tools). After an update the user is asked ONCE for the ones
that are missing; a fully equipped install gets a single line and no prompt.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


UPDATE = (ROOT / "update.bat").read_text(encoding="utf-8", errors="replace")
SCRIPT_PATH = ROOT / "deploy" / "check_optionals.bat"
SCRIPT = SCRIPT_PATH.read_text(encoding="utf-8", errors="replace") if SCRIPT_PATH.exists() else ""

# ── 1. wiring ───────────────────────────────────────────────────────────────
check("1a the updater calls the optional step", "check_optionals.bat" in UPDATE)
_i = UPDATE.index("check_optionals.bat") if "check_optionals.bat" in UPDATE else -1
check("1b it runs after the dependency refresh",
      _i > 0 and UPDATE.index("Refresh Python dependencies") < _i)
check("1c it runs before the final self-swap",
      _i > 0 and "FINAL SELF-SWAP" in UPDATE[_i:])
check("1d the call is guarded (older zips without the file still update)",
      'if exist "%~dp0deploy\\check_optionals.bat"' in UPDATE)

# ── 2. detection markers ────────────────────────────────────────────────────
for marker, what in (
        ("stt\\bin\\*.exe", "speech-to-text binaries"),
        ("stt\\models\\*", "speech-to-text model"),
        ("models--kyutai--pocket-tts", "text-to-speech cache"),
        ("models\\decider\\gliner25-decide", "routing decider"),
        ('import playwright', "browser engine (package probe)"),
        ("Git\\usr\\bin\\grep.exe", "Git Bash covers POSIX")):
    check(f"2 detects {what}", marker in SCRIPT)

# ── 3. install actions ──────────────────────────────────────────────────────
for action, what in (
        ("setup_voice.bat\"", "voice download"),
        ("setup_voice.bat\" --tts", "TTS pre-warm"),
        ("fetch_decider.py", "decider download"),
        ("playwright install chromium", "browser install"),
        ("winget install --id Microsoft.CoreUtils", "POSIX tools")):
    check(f"3 installs {what}", action in SCRIPT)
check("3f every install is failure-tolerant (no bare exit /b)",
      "exit /b" not in SCRIPT)

# ── 4. the question is asked, and only when something is missing ────────────
check("4a there is a single Y/N question",
      SCRIPT.count("choice /c YN") == 1)
check("4b nothing is installed without the answer",
      SCRIPT.index("choice /c YN") < SCRIPT.index('setup_voice.bat"'))
check("4c an equipped install exits before the question",
      "Everything optional is already installed" in SCRIPT and
      SCRIPT.index("Everything optional is already installed") < SCRIPT.index("choice /c YN"))
check("4d a skipped answer names the manual commands",
      "Add them later with the commands above" in SCRIPT)

# ── 5. dry run really installs nothing ─────────────────────────────────────
if SCRIPT_PATH.exists():
    r = subprocess.run(["cmd", "/c", str(SCRIPT_PATH), "--dry-run"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    check("5a --dry-run exits clean", r.returncode == 0, f"(rc={r.returncode})")
    check("5b --dry-run prints the table", "Optional components" in out)
    check("5c --dry-run reaches no installer",
          "dry run" in out.lower() or "already installed" in out)
else:
    check("5 the script exists", False)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
