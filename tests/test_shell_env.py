"""Shell-Umgebung: weiss der Agent, welche POSIX-Werkzeuge es gibt?

Hinweis von aussen (21.10.): Modelle sind mit GNU coreutils trainiert und
schreiben `grep -rn`, `head -50`, `sed -n`, `awk`, `find`. Auf Windows fehlen
die, und der Coder scheitert an Befehlen, die auf Linux normal sind.
Microsoft Core Utils schliessen die Luecke (`winget install Microsoft.CoreUtils`).
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


from infra import shell_env as S  # noqa: E402

DUO = (ROOT / "core" / "duo_helpers.py").read_text(encoding="utf-8", errors="replace")
ENVCHECK = (ROOT / "tools" / "env_check.py").read_text(encoding="utf-8", errors="replace")

# ── 1. Probe: echte Executables, keine Aliase ───────────────────────────
real_which = S.shutil.which
try:
    S.shutil.which = lambda name: f"/usr/bin/{name}"      # alles vorhanden
    full = S.available()
    check("a complete set is reported present", all(full.values()), f"({full})")
    check("coreutils_present() is true then", S.coreutils_present() is True)
    check("nothing is missing", S.missing() == [])

    S.shutil.which = lambda name: None                    # nichts vorhanden
    empty = S.available()
    check("a bare Windows reports everything missing",
          not any(empty.values()), f"({empty})")
    check("coreutils_present() is false then", S.coreutils_present() is False)
    check("missing() lists the tools", "grep" in S.missing())

    S.shutil.which = lambda name: "/usr/bin/grep" if name in ("grep", "find") else None
    check("a partial install is not 'present'", S.coreutils_present() is False)
    check("partial: present part is reported",
          set(S.summary()["present"]) == {"grep", "find"},
          f"({S.summary()['present']})")
finally:
    S.shutil.which = real_which

# ── 2. Prompt-Zeile: ehrlich, je nach Lage ─────────────────────────────
hint_win = None
try:
    S.shutil.which = lambda name: None
    sys.platform = "win32"
    hint_win = S.runtime_hint()
    hint_full = None
    S.shutil.which = lambda name: f"/usr/bin/{name}"
    hint_full = S.runtime_hint()
    S.shutil.which = lambda name: None
    sys.platform = "linux"
    hint_linux = S.runtime_hint()
finally:
    S.shutil.which = real_which
    sys.platform = "win32"

check("missing tools produce a warning", "NO POSIX tools" in (hint_win or ""), f"({hint_win[:60]})")
check("the warning names the workaround",
      "search_code" in (hint_win or "") and "Select-String" in (hint_win or ""))
check("a full install yields a positive line", "coreutils are installed" in (hint_full or ""),
      f"({(hint_full or '')[:60]})")
check("non-Windows gets no line", hint_linux == "")

# ── 3. Verdrahtung: Coder-Prompt und env_check ─────────────────────────
check("the coder prompt imports the probe", "from infra import shell_env" in DUO)
check("the hint is appended to the runtime line", "_env_line += \" \" + _sh" in DUO)
check("the probe failure cannot break the prompt",
      "except (ImportError, AttributeError, TypeError)" in DUO)
check("env_check reports the coreutils state", "POSIX coreutils" in ENVCHECK)
check("env_check prints the install command", "_sh.get('install')" in ENVCHECK)
check("env_check shows the state on the happy path too",
      "_print_shell_tools()" in ENVCHECK and "POSIX coreutils: yes" in ENVCHECK)

# ── 4. Install-Hinweis ist der offizielle Weg ──────────────────────────
check("install hint uses winget", S.INSTALL_HINT == "winget install Microsoft.CoreUtils",
      f"({S.INSTALL_HINT})")
check("POSIX_TOOLS covers what models write",
      {"grep", "sed", "awk", "head", "tail", "find"} <= set(S.POSIX_TOOLS))

print()
print(f"SUMMARY shell_env: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
