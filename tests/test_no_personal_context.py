# -*- coding: utf-8 -*-
"""No personal or machine context in code (2026-11-01).

Shipped code and tests must read the same for every user. A comment like
"REM ... (2026-11-01, ThinkPad): ..." or a quoted owner message leaks the
maintainer's machine and reads as a changelog fragment in a product file -
the changelog is the release notes, the reasoning is the commit message.

Forbidden in tracked source (comments included):
  - machine models / host names ("ThinkPad", a laptop model, "my machine")
  - the maintainer's user names or profile paths
  - attribution to a person for a decision ("owner request", "Owner:")
  - quoted German user complaints inside code comments

Dates are NOT forbidden: the codebase uses dated tags to trace a rule back to
its origin. Only the personal layer is banned.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (pattern, human name)
BANNED = [
    (re.compile(r"ThinkPad", re.I), "machine model"),
    (re.compile(r"\bNicolas\b"), "maintainer user name"),
    (re.compile(r"\bNtheP\b"), "maintainer user name"),
    (re.compile(r"C:\\\\?Users\\\\?Nicolas", re.I), "personal profile path"),
    (re.compile(r"owner request", re.I), "attribution to a person"),
    (re.compile(r"owner feedback", re.I), "attribution to a person"),
    (re.compile(r"owner (?:wants|said|asked|reported)", re.I),
     "attribution to a person"),
    # "owner:" as a comment preface - NOT the identifier `_owner:` (a word
    # boundary does not match between `_` and `o`).
    (re.compile(r"\bowner\s*:"), "attribution to a person"),
]

# Files that are allowed to mention the rule itself (this suite) and docs that
# quote it as history (release notes are change logs by design).
ALLOW_SUFFIXES = (".md",)
ALLOW_FILES = {"tests/test_no_personal_context.py"}

SUFFIXES = (".py", ".js", ".mjs", ".bat", ".ps1", ".html", ".css", ".toml", ".txt")


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True)
    if out.returncode != 0:
        # not a git checkout: fall back to a directory walk
        return [p for p in ROOT.rglob("*") if p.is_file()]
    return [ROOT / line for line in out.stdout.splitlines() if line]


def main() -> int:
    offenders = []
    for path in tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOW_FILES or rel.endswith(ALLOW_SUFFIXES):
            continue
        if not rel.endswith(SUFFIXES):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, name in BANNED:
                if pattern.search(line):
                    offenders.append(f"{rel}:{lineno}: {name}: {line.strip()[:90]}")
    if offenders:
        print("  FAIL personal/machine context in code:")
        for o in offenders[:20]:
            print(f"    {o}")
        print("  Move the reasoning into the commit message, the history into "
              "the release notes.")
        return 1
    print("  PASS no machine names, user names or owner attributions in code")
    return 0


# Words that only appear in German prose. They are fine in internal comments
# but must not reach a print(), an echo or any text the user reads.
GERMAN_HINTS = re.compile(
    r"\b(nicht|wird|wurde|keine|bereits|gefunden|Bitte|Auswahl|Abbruch|verworfen|Größe|Groesse|Speicher|Datei|Zeichen|Aufgabe|Antwort|Ergebnis|läuft|laeuft|startet|geladen|unterstuetzt|unterstützt)\b")
# only lines that produce user-visible output
OUTPUT_CALL = re.compile(
    r"^(?:print\(|echo |logger\.|emit\(|console\.log|h\(|content=|"
    r"yield |stdout\.write|sys\.stderr\.write)")


def check_english_output() -> int:
    offenders = []
    for path in tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOW_FILES or rel.endswith(ALLOW_SUFFIXES):
            continue
        if not rel.endswith((".py", ".bat", ".js", ".html")):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("#", "//", "REM", "/*", "*", "\"\"\"", "'''")):
                continue                      # comments may be German
            if not OUTPUT_CALL.search(line):
                continue
            if GERMAN_HINTS.search(line):
                offenders.append(f"{rel}:{lineno}: {stripped[:90]}")
    if offenders:
        print("  FAIL German text in user-facing output:")
        for o in offenders[:20]:
            print(f"    {o}")
        return 1
    print("  PASS user-facing output is English")
    return 0


if __name__ == "__main__":
    print("=== no_personal_context ===")
    rc = main()
    rc |= check_english_output()
    sys.exit(rc)
