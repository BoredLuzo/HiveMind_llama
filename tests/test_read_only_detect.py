"""Behavioral Test: Read-only request classification (2026-09-03).

Live-Befund: Der Pac-Man-Prompt enthaelt die Zeile
  "## CRITICAL – MAZE DATA (GROUND TRUTH – DO NOT MODIFY)"
waehrend die eigentliche Aufgabe "Create a fully playable Pac-Man game …" ist.
`_READ_ONLY_KEYWORDS` matcht "do not modify" → der Tool-Round wurde deaktiviert
und das Modell (hermes v12) free-stylte Text-Tool-Calls
("[TOOL_CALL] read_file …", "edit_file<argkey>…"), die niemand ausfuehrt.

Regel (is_read_only_request): read-only nur, wenn eine Read-only-Phrase UND
keine (nicht-negierte) Implementierungs-Absicht im Request steckt.

Run: python tests/test_read_only_detect.py
Exit 0 = all pass, Exit 1 = failures.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.duo_helpers import (  # noqa: E402
    is_read_only_request,
    _READ_ONLY_KEYWORDS,
)

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


def _check(name, text, expected):
    got = is_read_only_request(text)
    if got is expected:
        ok(f"{name}: read_only={got}")
    else:
        fail(f"{name}: expected read_only={expected}, got {got}", text[:100])


def main():
    print("\n=== Read-only detection (2026-09-03) ===\n")
    _check("A1 PacMan (EN, DO NOT MODIFY as content constraint)",
           ("Create a fully playable Pac-Man game as a single HTML file. "
            "## CRITICAL – MAZE DATA (GROUND TRUTH – DO NOT MODIFY) "
            "Copy it 1:1 into your code."),
           False)
    _check("A2 PacMan (DE, constraint)",
           ("Erstelle ein Pac-Man-Spiel. Achtung – MAZE-Daten (DO NOT MODIFY) "
            "muessen 1:1 uebernommen werden."),
           False)
    _check("A3 fix + do not modify tests",
           "Please fix the build, but do not modify the test files.",
           False)
    _check("B1 pure read-only (DE)",
           "Bitte nur lesen und fasse die wichtigsten Dateien kurz zusammen.",
           True)
    _check("B2 pure read-only (EN)",
           "Do not modify anything, just explain how the app works.",
           True)
    _check("B3 don't write, only read",
           "Don't write any file, only read index.html and summarize it.",
           True)
    _check("B4 nichts schreiben, nur lesen",
           "Nur lesen, nichts schreiben, erklaere kurz was logic.js tut.",
           True)
    _check("C1 no RO phrase at all",
           "Implement a sorting function in sorting.js",
           False)
    if not _READ_ONLY_KEYWORDS:
        fail("D1 keyword list must not be empty")
    else:
        ok("D1 keyword list intact")

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
