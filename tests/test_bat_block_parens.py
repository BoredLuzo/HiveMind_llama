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


def silent_pauses(path: Path) -> list[tuple[int, str]]:
    """`pause >nul` without a visible prompt above it: the script then waits
    for a key while the screen shows nothing - it reads as a freeze (live:
    right after the TTS download, before step [9/9])."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[tuple[int, str]] = []
    for i, raw in enumerate(lines, 1):
        if not re.match(r"^\s*pause\s*>nul\s*$", raw, re.I):
            continue
        # the enclosing block may be the guard itself
        guard = any("HIVEMIND_NO_PAUSE" in lines[k]
                    for k in range(max(0, i - 6), i))
        above = " ".join(lines[max(0, i - 4):i - 1]).lower()
        visible = re.search(r"press any key|press a key|continue|close", above) is not None
        if not (visible or guard):
            out.append((i, raw.strip()))
    return out


def browser_refresh_present(path: Path) -> bool:
    """The updater must refresh the Playwright build on every run: a
    requirements refresh can bump the package past the installed browser."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return '"-m playwright install chromium"' in text or " -m playwright install chromium" in text


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
            print(f"  FAIL {rel} (unescaped paren in an echo block)")
            for i, t in hits[:6]:
                print(f"        line {i}: {t}")
        silent = silent_pauses(p)
        if silent:
            bad += len(silent)
            print(f"  FAIL {rel} (pause without a visible prompt)")
            for i, t in silent[:6]:
                print(f"        line {i}: {t}")
    if bad:
        print(f"\n  {bad} unescaped parenthesis/parentheses inside echo blocks.")
        print("  Escape them as ^( and ^) - otherwise cmd aborts the installer.")
        return 1
    _opt = ROOT / "deploy" / "check_optionals.bat"
    check_ok = browser_refresh_present(_opt) if _opt.exists() else True
    if not check_ok:
        print("  FAIL deploy/check_optionals.bat: no Playwright refresh")
        return 1
    print(f"  PASS {checked} batch files: no block-breaking echo, no silent pause, "
          f"browser build refreshed")
    return 0


if __name__ == "__main__":
    print("=== bat_block_parens ===")
    sys.exit(main())
