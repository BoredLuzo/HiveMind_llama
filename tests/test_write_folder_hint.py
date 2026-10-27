# -*- coding: utf-8 -*-
"""Write-Fehler nennen bei Desktop/Dokumenten die echte Windows-Ursache.

ThinkPad-Fall (2026-10-26): write_file auf C:\\Users\\Nicolas\\Desktop\\Test
scheiterte mit WRITE_FILE_PERMISSION, und das 4B-Modell probierte danach
/Users/...-Pfade, test -w und whoami. Die Meldung sagte nur "environment
restriction", nicht dass Controlled Folder Access oder OneDrive die Ursache ist.
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


from tools.handlers.file_ops import (  # noqa: E402
    _protected_folder_hint, _WRITE_ERROR_TEXT, _write_error_code,
)


def msg_for(parent: str) -> str:
    return _WRITE_ERROR_TEXT["WRITE_FILE_PERMISSION"].format(
        parent=parent, hint=_protected_folder_hint(parent))


# ── 1. Die Hinweise erscheinen nur, wo sie hingehoeren ─────────────────
for folder in (r"C:\Users\Nicolas\Desktop\Test", r"C:\Users\N\Documents\Proj",
               r"C:\Users\N\Pictures", r"C:\Users\N\OneDrive\Work"):
    hint = _protected_folder_hint(folder)
    check(f"1 hint for {folder}", "Controlled Folder Access" in hint, f"({len(hint)} Zeichen)")
    check(f"2 hint names OneDrive for {folder}", "OneDrive" in hint)

for folder in (r"C:\Work\Proj", r"D:\code\app", r"C:\Users\N\hivemind_ws"):
    check(f"3 no hint for {folder}", _protected_folder_hint(folder) == "")

# ── 2. Die Meldung bleibt lesbar ───────────────────────────────────────
m = msg_for(r"C:\Users\Nicolas\Desktop\Test")
check("4 the base sentence ends before the hint",
      "restricting sandbox. Common cause" in m, f"({m[150:230]!r})")
check("5 the message names the folder", r"C:\Users\Nicolas\Desktop\Test" in m)
check("6 the message offers a way out", "HiveMind_ws" in m or "outside" in m)
m2 = msg_for(r"C:\Work\Proj")
check("7 a normal folder gets no Windows lecture", "Controlled Folder Access" not in m2)
check("8 no double period", ".." not in m)

# ── 3. Klassifizierung bleibt korrekt ──────────────────────────────────
check("9 PermissionError maps to WRITE_FILE_PERMISSION",
      _write_error_code(PermissionError(13, "denied")) == "WRITE_FILE_PERMISSION")
check("10 ENOSPC stays NO_SPACE",
      _write_error_code(OSError(28, "full")) == "WRITE_FILE_NO_SPACE")
check("11 an unknown error stays unclassified",
      _write_error_code(OSError(999, "weird")) is None)

print()
print(f"SUMMARY write_folder_hint: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
