# -*- coding: utf-8 -*-
"""run_bash: verkettete Kommandos duerfen keine Muell-Dateien erzeugen.

Live-Befund 2026-10-26 (Chunking-Lauf): im Workspace lagen 0-Byte-Dateien
namens "&&", "echo" und "Created __init__.py". Der Windows-Shim fuer `touch`
splittet die Argumente und legt fuer JEDEN Token eine Datei an, also auch fuer
die Operatoren einer Shell-Kette. Dieselbe Luecke hatte `rm` (loescht Tokens).

Der Test faehrt den echten Handler gegen einen Wegwerf-Workspace.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
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


from tools.handlers import exec_tools as ET  # noqa: E402

SRC = (ROOT / "tools" / "handlers" / "exec_tools.py").read_text(encoding="utf-8", errors="replace")


def run_bash(cmd: str, ws: Path) -> str:
    """Der echte Handler, wie ihn der Tool-Loop aufruft."""
    try:
        out = asyncio.run(ET._inline_tool_run_bash({"cmd": cmd}, ws, None))
        return str(out)
    except Exception as exc:  # noqa: BLE001 - report, not raise
        return f"[raise {type(exc).__name__}: {exc}]"


# ── 1. Quelle: der Guard existiert und deckt alle Operatoren ───────────
# The tuple lists single characters now: "&" covers "&&" and "&", "|" covers
# "|" and "||". Checking for the doubled forms would be a string-only pin.
for op in ("&", "|", ";", ">", "<", "`", "$(", "\\n", "\\r", "^"):
    check(f"1 operator {op!r} is covered by the chain guard",
          f'"{op}"' in SRC.split("_SHELL_OPS = (")[1].split(")")[0])
check("1b a single ampersand is refused at runtime",
      "SHELL_CHAIN_UNSUPPORTED" in run_bash("touch a.py & echo done", Path(
          tempfile.mkdtemp(prefix="amp_pin_"))))
check("1c a second command line is refused too",
      "SHELL_CHAIN_UNSUPPORTED" in run_bash("touch b.py\nrm -rf x", Path(
          tempfile.mkdtemp(prefix="nl_pin_"))))
check("2 touch refuses chained commands", "if _touch_match and _HAS_SHELL_CHAIN:" in SRC)
check("3 rm refuses chained commands",
      'if _rm_match and cmd.strip().startswith("rm ") and _HAS_SHELL_CHAIN:' in SRC)
check("4 mkdir guards use the chain flag, not only semicolons",
      "_HAS_SEMICOLON:" not in SRC.split("_HAS_SHELL_CHAIN =")[1])

# ── 2. Verhalten: keine Muell-Dateien mehr ─────────────────────────────
ws = Path(tempfile.mkdtemp(prefix="shellchain_"))
out = run_bash("touch Created __init__.py && echo done", ws)
files = sorted(p.name for p in ws.iterdir())
check("5 a chained touch is refused, not half-run",
      "SHELL_CHAIN_UNSUPPORTED" in out, f"({out[:70]!r})")
check("6 no junk files were created", files == [], f"({files})")

out2 = run_bash('touch "Created __init__.py" && echo done', ws)
files2 = sorted(p.name for p in ws.iterdir())
check("7 the quoted variant is refused too",
      "SHELL_CHAIN_UNSUPPORTED" in out2, f"({out2[:70]!r})")
check("8 still no junk files", files2 == [], f"({files2})")

# ── 3. Einfache Kommandos laufen weiter ────────────────────────────────
out3 = run_bash("touch ok.txt", ws)
check("9 a plain touch still works", (ws / "ok.txt").exists(), f"({out3[:60]!r})")

print()
print(f"SUMMARY shell_chain_guard: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
