"""run_bash shell mismatch (2026-10-15, live: PowerShell-Syntax in bash,
Exit 2, identischer Retry).

Befund: Schema + OS-Hint behaupteten unconditioned "Windows PowerShell",
waehrend auto-Modus Git Bash startet, sobald installiert - das Modell
folgte dem Prompt (Test-Path/Write-Host) und loopte. Fix: der Hint nennt
die tatsaechlich aufgeloeste Shell (shell_os_hint), die run_bash-Beschrei-
bung ist wahrheitsgemaeß konditional, und RUN_BASH_NONZERO traegt den
Namen der Shell, die lief. Kein LLM noetig; der Funktionstest startet
nur einen lokalen echo/exit-Prozess, keine Engine."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


def main():
    from tools.handlers.exec_tools import shell_os_hint, _run_bash_shell_mode

    h_bash = shell_os_hint("bash", "win32")
    check("bash-hint (win): POSIX guidance", "POSIX" in h_bash and "Git Bash" in h_bash)
    check("bash-hint (win): warnt vor PS-Cmdlets",
          "Get-ChildItem" in h_bash and "Test-Path" in h_bash)
    check("bash-hint (win): kein PS-ONLY-Kommando mehr",
          "Use PowerShell syntax ONLY" not in h_bash)
    # PLATFORM-PIN (2026-11-05, live): a 4B model mixed macOS /Users/...
    # paths into a Windows run and looped on rm/rewrite. The hint must pin
    # the OS and the path format explicitly.
    check("bash-hint (win): pinnt Windows + Pfadformat",
          "THIS MACHINE IS WINDOWS" in h_bash and "/Users/..." in h_bash)
    check("bash-hint (win): Browser-Tool statt playwright-Install",
          "Playwright is pre-installed" in h_bash)
    check("bash-hint (win): richtige Runtime fuer Syntax-Checks",
          "node --check" in h_bash and "py_compile" in h_bash)

    h_ps = shell_os_hint("powershell", "win32")
    check("powershell-hint (win): PS-native guidance",
          "PowerShell-native" in h_ps and "Get-ChildItem" in h_ps)
    check("powershell-hint (win): pinnt Windows + Pfadformat",
          "THIS MACHINE IS WINDOWS" in h_ps and "Playwright is pre-installed" in h_ps)

    h_unix = shell_os_hint("bash", "linux")
    check("unix-hint: bash guidance", "bash/sh on Unix" in h_unix)
    check("unix-hint: richtige Runtime fuer Syntax-Checks",
          "node --check" in h_unix)

    # install_package refuses doomed playwright installs with a pointer
    # to the preinstalled browser tool.
    from tools.handlers.exec_tools import _inline_tool_install_package
    import tempfile as _tf
    _ws = _tf.mkdtemp(prefix="hintpw_")
    _pw = asyncio.run(_inline_tool_install_package(
        {"manager": "pip", "packages": "playwright"}, Path(_ws), _ws))
    check("install_package blockt playwright mit Browser-Tool-Hinweis",
          "INSTALL_PLAYWRIGHT_UNNECESSARY" in _pw
          and "browser" in _pw.lower())

    # SHELL-FALLBACK (2026-10-16): beide Zweige per Monkeypatch der
    # Erkennung - ohne Git Bash darf NIE "bash" herauskommen, auch nicht
    # bei explizit gepinnter Einstellung run_bash_shell="bash".
    import core.state as _cs
    from tools.handlers import exec_tools as _et
    _orig_bash_path, _orig_setting = _et._windows_bash_path, _cs.settings.get("run_bash_shell")
    try:
        _et._windows_bash_path = lambda: ""  # Git Bash NICHT installiert
        _cs.settings["run_bash_shell"] = "auto"
        check("no git bash + auto -> powershell",
              _et._run_bash_shell_mode() == "powershell")
        _cs.settings["run_bash_shell"] = "bash"
        check("no git bash + pinned bash -> falls back to powershell",
              _et._run_bash_shell_mode() == "powershell")
        _et._windows_bash_path = lambda: r"C:\Program Files\Git\bin\bash.exe"
        check("git bash + pinned bash -> bash",
              _et._run_bash_shell_mode() == "bash")
        _cs.settings["run_bash_shell"] = "powershell"
        check("explicit powershell stays powershell",
              _et._run_bash_shell_mode() == "powershell")
    finally:
        _et._windows_bash_path = _orig_bash_path
        if _orig_setting is None:
            _cs.settings.pop("run_bash_shell", None)
        else:
            _cs.settings["run_bash_shell"] = _orig_setting

    # Source-Guards: der Prompt-Hint delegiert an die aufgeloeste Shell ...
    _dr = (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(
        encoding="utf-8", errors="replace")
    # 1.4.x: shell_os_hint moved to core/duo/preflight.py (pure move); the runner
    # still passes _run_bash_shell_mode through.
    _pf = (Path(__file__).parent.parent / "core" / "duo" / "preflight.py").read_text(
        encoding="utf-8", errors="replace")
    check("duo_runner nutzt shell_os_hint + _run_bash_shell_mode",
          # shell_os_hint is DEFINED in tools/handlers/exec_tools.py and imported
          # where the prompt is built (duo_runner or its preflight module);
          # _run_bash_shell_mode stays a runner-local name.
          ("shell_os_hint" in _dr or "shell_os_hint" in _pf)
          and ("_run_bash_shell_mode" in _dr or "_run_bash_shell_mode" in _pf))
    check("duo_runner: hartes PS-ONLY-Kommando entfernt",
          "Use PowerShell syntax ONLY" not in _dr)
    # ... und das Schema beschreibt die Shell-Konditionalitaet wahrhaftig.
    _defs = (Path(__file__).parent.parent / "tools" / "definitions.py").read_text(
        encoding="utf-8", errors="replace")
    check("run_bash schema: Git-Bash-Konditional genannt",
          "Git Bash when" in _defs)
    check("run_bash schema: falsche PS-Absolutbehauptung entfernt",
          "PowerShell 5.1 on Windows, bash on Linux/Mac" not in _defs)

    # Funktionstest am echten Handler: NONZERO-traegt den Shell-Namen.
    mode = _run_bash_shell_mode()
    check("shell mode resolvable", mode in ("bash", "powershell"), f" got {mode}")
    ws = Path(tempfile.mkdtemp(prefix="hvm_shellhint_"))
    from tools.handlers.exec_tools import _inline_tool_run_bash
    res = asyncio.run(_inline_tool_run_bash({"cmd": "exit 2"}, ws, str(ws)))
    check("nonzero -> RUN_BASH_NONZERO mit Shell-Namen",
          "RUN_BASH_NONZERO" in res and "(shell: " in res,
          f" mode={mode}")

    sys.exit(h.finish("run_bash_shell_hint"))


main()
