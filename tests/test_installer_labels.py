# -*- coding: utf-8 -*-
"""Static label/goto reachability + wiring guards for the Windows installer.

Two live incidents motivated this suite:

1. install.bat carried a COMPLETE language-server step [6c] (banner, prompt,
   picker) that no `goto` ever reached: both exits of the decider step jumped
   straight to :searxng_step, so the block was dead code and the LSP prompt
   never appeared during an install. Dead steps are invisible to "it worked
   on my machine" testing - only reachability analysis catches them.
2. deploy/install_lsp_packs.bat invoked another .bat WITHOUT `call` (control
   would never return to the installer), and `call :pack_%%P` on a typo hit a
   missing label - cmd prints a localized "batch label not found" error.

The reachability scan mirrors cmd semantics closely enough for these files:
- labels are lines starting with a single `:` (lines starting `::` are
  comments, as used in deploy/install_windows.bat),
- `goto X` / `goto :X` target X, `call :X` targets X,
- `call :pack_%~1` style dynamic dispatch covers every label with that
  literal prefix,
- `goto :eof` is always available and never needs a label.

Run: python tests/test_installer_labels.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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


BATS = [
    "install.bat",
    "create_shortcut.bat",
    "start_hivemind.bat",
    "start_engine_headless.bat",
    "setup_models.bat",
    "deploy/install_windows.bat",
    "deploy/setup_voice.bat",
    "deploy/install_lsp_packs.bat",
    "deploy/check_optionals.bat",
]

LABEL_RE = re.compile(r"^\s*:(\w+)")
GOTO_RE = re.compile(r"\bgoto\s+:?(\w+)")
CALL_RE = re.compile(r"\bcall\s+:(\w+)")          # call :label
DYN_RE = re.compile(r"\bcall\s+:(\w*)%")          # call :pack_%~1 etc.


def _comment_free(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    low = s.lower()
    if low.startswith("rem") or s.startswith("::"):
        return False
    return True


def labels_of(text: str) -> set:
    out = set()
    for line in text.splitlines():
        if not _comment_free(line):
            continue
        m = LABEL_RE.match(line)
        if m:
            out.add(m.group(1).lower())
    return out


def targets_of(text: str) -> tuple:
    exact = set()
    prefixes = []
    for line in text.splitlines():
        if not _comment_free(line):
            continue
        for m in GOTO_RE.finditer(line):
            if m.group(1).lower() != "eof":
                exact.add(m.group(1).lower())
        for m in CALL_RE.finditer(line):
            if m.group(1).lower() == "eof":
                continue
            if line[m.end():m.end() + 1] == "%":
                prefixes.append(m.group(1).lower())  # call :pack_%~1
            else:
                exact.add(m.group(1).lower())
        for m in DYN_RE.finditer(line):
            prefixes.append(m.group(1).lower())
    return exact, prefixes


def block_lines(text: str) -> dict:
    """Map line number -> paren depth at that line (for in-block checks)."""
    depth = 0
    out = {}
    for i, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        out[i] = depth
        if not _comment_free(raw):
            continue
        if re.match(r"^\)\s*(else\b|$)", s):
            depth = max(0, depth - 1)
            continue
        if s.endswith("(") and re.match(r"^(if|for|else|\))", s.lower()):
            depth += 1
    return out


def main() -> int:
    for rel in BATS:
        p = ROOT / rel
        raw = p.read_bytes()
        check(f"{rel}: ascii", all(c < 128 for c in raw))
        check(f"{rel}: CRLF only", raw.count(b"\n") == raw.count(b"\r\n")
              and raw.count(b"\r\r\n") == 0)
        text = raw.decode("ascii", errors="replace")
        labels = labels_of(text)
        exact, prefixes = targets_of(text)

        missing = sorted(t for t in exact if t not in labels)
        check(f"{rel}: every goto/call target has a label",
              not missing, f"(missing: {missing})" if missing else "")

        untargeted = sorted(
            l for l in labels
            if l not in exact and not any(l.startswith(pfx) for pfx in prefixes))
        check(f"{rel}: no dead labels", not untargeted,
              f"(never targeted: {untargeted})" if untargeted else "")

    # ── install.bat: the LSP step [6c] must be wired, not dead ─────────────
    inst = (ROOT / "install.bat").read_text(encoding="ascii", errors="replace")
    check("install.bat: :lsp_step label exists", ":lsp_step" in inst)
    check("install.bat: decider exits jump to :lsp_step",
          inst.count("goto lsp_step") >= 2,
          f"(found {inst.count('goto lsp_step')})")
    check("install.bat: pack list is CALLED (no-call would end the installer)",
          "call deploy\\install_lsp_packs.bat --list" in inst)
    check("install.bat: no bare install_lsp_packs invocation",
          not re.search(r"^\s*deploy\\install_lsp_packs\.bat", inst, re.M))

    # ── start_hivemind.bat: no parse-time %PY% read inside a block ─────────
    sh = (ROOT / "start_hivemind.bat").read_text(encoding="ascii",
                                                 errors="replace")
    depth = block_lines(sh)
    stale = []
    for i, line in enumerate(sh.splitlines(), 1):
        if depth.get(i, 0) > 0 and 'if "%PY%"=="python"' in line:
            stale.append(i)
    check("start_hivemind.bat: no in-block parse-time %PY% compare",
          not stale, f"(lines {stale})" if stale else "")
    check("start_hivemind.bat: python chain uses top-level labels",
          ":py_found" in sh and "goto py_found" in sh
          and ":py_known_paths" in sh)
    check("start_hivemind.bat: version lookup takes server.py via env var",
          'set "HM_SERVER_PY=%~dp0server.py"' in sh
          and any("$env:HM_SERVER_PY" in l and "Select-String" in l
                  for l in sh.splitlines())
          and not any("'%%~dp0server.py'" in l.replace("^", "")
                      and "Select-String" in l for l in sh.splitlines()))

    # ── setup_models.bat: mkdir failure is surfaced, role defaults too ─────
    sm = (ROOT / "setup_models.bat").read_text(encoding="ascii",
                                               errors="replace")
    check("setup_models.bat: mkdir result is verified",
          'if not exist "%MODELS_DIR%"' in sm
          and sm.count('if not exist "%MODELS_DIR%"') >= 2)
    check("setup_models.bat: failed mkdir aborts with a clear error",
          "[ERROR] Could not create the models folder." in sm)
    check("setup_models.bat: apply_model_defaults failure is not called Done",
          re.search(r'apply_model_defaults\.py[ \t]*\r?\n'
                    r'(?:REM[^\n]*\n)*if errorlevel 1', sm)
          is not None)

    # ── deploy/install_windows.bat: venv python preferred for the service ──
    iw = (ROOT / "deploy" / "install_windows.bat").read_text(
        encoding="ascii", errors="replace")
    check("install_windows.bat: prefers the install venv python",
          'if exist "%~dp0..\\.venv\\Scripts\\python.exe" '
          'set "HMPY=%~dp0..\\.venv\\Scripts\\python.exe"' in iw)
    check("install_windows.bat: service install uses the resolved python",
          '"%HMPY%" "%~dp0hivemind_windows.py" install' in iw)
    check("install_windows.bat: bare `python` invocation is gone",
          not re.search(r'^\s*python\s+"%~dp0hivemind_windows\.py"', iw, re.M))

    # ── deploy/install_lsp_packs.bat: bad pack numbers warn, never miss ────
    lp = (ROOT / "deploy" / "install_lsp_packs.bat").read_text(
        encoding="ascii", errors="replace")
    check("install_lsp_packs.bat: pack tokens validated before dispatch",
          ":pack_check" in lp and ':pack_%~1' in lp)
    check("install_lsp_packs.bat: unknown token warns in plain text",
          "Unknown pack" in lp)

    print()
    print(f"SUMMARY installer_labels: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
