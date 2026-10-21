"""Shell-Umgebung: welche POSIX-Werkzeuge gibt es wirklich?

Hintergrund (Hinweis von aussen, 21.10.): Modelle sind oft mit GNU coreutils
trainiert und schreiben deshalb `grep -rn "foo" src/`, `head -50 x`, `sed -n`,
`awk`, `find . -name "*.py"`. Auf Windows gibt es die nicht, und der Agent
scheitert an Befehlen, die auf Linux selbstverstaendlich sind.

Windows Core Utils (Microsoft, offiziell) schliessen die Luecke:

    winget install Microsoft.CoreUtils

Dieses Modul prueft nur, was vorhanden ist, und liefert dem Coder-Prompt eine
ehrliche Zeile: entweder "coreutils sind da" oder "nutze die HiveMind-Tools".
Keine stillen Ersatzbefehle, keine Flag-Uebersetzung - das Modell soll wissen,
womit es arbeitet.
"""
from __future__ import annotations

import shutil
import sys

# Die Befehle, die Modelle am haeufigsten schreiben und die Windows von Haus
# aus NICHT kennt. In PowerShell sind `ls`/`cat` Aliase mit anderen Flags,
# deshalb pruefen wir echte Executables ueber shutil.which.
POSIX_TOOLS = ("grep", "sed", "awk", "head", "tail", "find", "cat", "ls",
               "wc", "sort", "uniq", "cut", "xargs")

INSTALL_HINT = "winget install Microsoft.CoreUtils"


def available() -> dict[str, bool]:
    """{tool: found} fuer die gaengigen POSIX-Befehle."""
    return {name: bool(shutil.which(name)) for name in POSIX_TOOLS}


def coreutils_present() -> bool:
    """True, wenn die wichtigsten Kernbefehle als echte Programme da sind."""
    have = available()
    return all(have.get(t) for t in ("grep", "sed", "awk", "find"))


def missing() -> list[str]:
    have = available()
    return [t for t in POSIX_TOOLS if not have[t]]


def runtime_hint() -> str:
    """Eine Zeile fuer den System-Prompt (oder '' wenn Linux/macOS)."""
    if sys.platform != "win32":
        return ""
    if coreutils_present():
        return ("POSIX coreutils are installed: grep, sed, awk, head, tail, "
                "find, cat, ls, wc work in run_bash - use them the way you "
                "were trained.")
    _miss = ", ".join(missing()[:6])
    return ("Attention: this Windows box has NO POSIX tools (" + _miss +
            "...). `grep`, `sed`, `head`, `tail`, `find` do not exist and "
            "`ls`/`cat` are PowerShell aliases with other flags. Use the "
            "HiveMind tools (search_code, read_file, find_files) or "
            "PowerShell cmdlets (Select-String, Get-Content, Get-ChildItem).")


def summary() -> dict:
    """Diagnose-Datensatz (fuer env_check / die UI)."""
    have = available()
    return {
        "platform": sys.platform,
        "coreutils": coreutils_present(),
        "present": [t for t, ok in have.items() if ok],
        "missing": [t for t, ok in have.items() if not ok],
        "install": INSTALL_HINT if sys.platform == "win32" else "",
    }
