# -*- coding: utf-8 -*-
"""Installer batches must not abort on a ')' inside an echo block.

cmd parses an `if (...)` block as one unit. A ')' inside an ECHO line ends
that block early; the following `) else (` line is then a stray ')' and cmd
aborts the whole script - the installer window just closes, which is exactly
what happened after answering "y" to the optional decider.

Rule: inside a block, every parenthesis in an echo line is escaped as ^( / ^).
This suite parses every shipped .bat with a block-aware scan.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BATS = ["install.bat", "update.bat", "setup_models.bat", "start_hivemind.bat",
        "start_gateway.bat", "start_gateway_headless.bat", "create_shortcut.bat",
        "start_engine_headless.bat", "start_llama.bat", "stop_llama.bat",
        "clean_release.bat", "release_checks.bat", "deploy/check_optionals.bat",
        "deploy/setup_voice.bat", "setup_models.bat"]


def risky_lines(path: Path) -> list[tuple[int, str]]:
    """Echo lines inside an if/for block that contain an unescaped paren."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    depth = 0
    out: list[tuple[int, str]] = []
    for i, raw in enumerate(lines, 1):
        s = raw.strip()
        low = s.lower()
        if not s or low.startswith("rem") or low.startswith("::"):
            continue
        if re.match(r"^\)\s*(else|$)", s):
            depth = max(0, depth - 1)
            continue
        if s.endswith("(") and re.match(r"^(if|for|else|\))", low):
            depth += 1
            continue
        if depth > 0 and low.startswith("echo"):
            bare = s.replace("^(", "").replace("^)", "")
            if "(" in bare or ")" in bare:
                out.append((i, s[:110]))
    return out


def main() -> int:
    checked = 0
    bad = 0
    for rel in dict.fromkeys(BATS):
        p = ROOT / rel
        if not p.exists():
            continue
        checked += 1
        hits = risky_lines(p)
        if hits:
            bad += len(hits)
            print(f"  FAIL {rel}")
            for i, t in hits[:6]:
                print(f"        line {i}: {t}")
    if bad:
        print(f"\n  {bad} unescaped parenthesis/parentheses inside echo blocks.")
        print("  Escape them as ^( and ^) - otherwise cmd aborts the installer.")
        return 1
    print(f"  PASS {checked} batch files: no echo line can close its block")
    return 0


if __name__ == "__main__":
    print("=== bat_block_parens ===")
    sys.exit(main())
