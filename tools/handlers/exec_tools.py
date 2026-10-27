"""Tool handlers: execution tools (bash/python/tests/install/background) (part of tools/handlers, extracted from tools/handlers.py)."""

from __future__ import annotations

from pathlib import Path
from utils.file import fuzzy_resolve_path as _fuzzy_resolve_path, _inline_resolve_path, _inline_check_workspace
from tools.errors import tool_error_response as _tool_error_response
import asyncio
import os
import re
import shutil
import sys

_BASH_BIN = shutil.which("bash") or "/bin/sh"  # X6 (audit r2): bash-less distros
_WIN_BASH_CACHE: str | None = None


def _windows_bash_path() -> str:
    """RUN-BASH-SHELL FIX (2026-10-07): small coder models emit POSIX bash
    (`npm run dev ... &`, /tmp paths, pipes) and powershell.exe chokes on
    every line (14 no-progress rounds in the live test run). Prefer the
    Git-for-Windows bash; explicitly avoid System32\bash.exe (WSL:
    different mounts, usually no npm)."""
    global _WIN_BASH_CACHE
    if _WIN_BASH_CACHE is not None:
        return _WIN_BASH_CACHE
    _cand = ""
    for _p in (r"C:\Program Files\Git\bin\bash.exe",
               r"C:\Program Files\Git\usr\bin\bash.exe",
               r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if os.path.isfile(_p):
            _cand = _p
            break
    if not _cand:
        _w = shutil.which("bash")
        if _w and "system32" not in _w.lower():
            _cand = _w
    _WIN_BASH_CACHE = _cand
    return _cand


def _run_bash_shell_mode() -> str:
    # "auto" | "bash" | "powershell" (settings.run_bash_shell)
    #
    # POSIX-SHELL (2026-10-17): "powershell" is a WINDOWS mode. On a non-Windows
    # host the old code asked _windows_bash_path() (which only looks for
    # C:\Program Files\Git\... plus a `which bash`) and answered "powershell"
    # on a bash-less distro (Alpine: /bin/sh only) - the tool then reported a
    # shell that does not exist there. Execution was already correct
    # ([_BASH_BIN, "-c", cmd], _BASH_BIN falls back to /bin/sh); only the
    # label/hint lied. Unix always has a POSIX shell, so answer "bash".
    if sys.platform != "win32":
        return "bash"
    try:
        from core import state as _cs
        _m = str((_cs.settings or {}).get("run_bash_shell", "auto")).lower()
    except ImportError:
        _m = "auto"
    if _m == "bash" and not _windows_bash_path():
        # SHELL-FALLBACK (2026-10-16): a pinned "bash" without a Git Bash
        # install used to report "bash" while execution silently ran
        # powershell.exe - hint, error shell-name and actual shell disagreed
        # (same failure class as the PowerShell-into-bash loop, just on
        # machines without Git Bash).
        return "powershell"
    if _m in ("bash", "powershell"):
        return _m
    return "bash" if _windows_bash_path() else "powershell"


def shell_os_hint(shell_mode: str, platform: str) -> str:
    """SHELL-HINT (2026-10-15): guidance text for the shell run_bash will
    ACTUALLY use. The old hint claimed "Windows PowerShell" unconditionally
    while auto-mode runs Git Bash whenever it is installed - the model
    followed the prompt, sent PowerShell syntax into bash and looped on
    exit 2 (live: Test-Path / Write-Host). Kept here (import-light) so
    tests can exercise it without importing core.duo_runner."""
    if platform == "win32" and shell_mode == "bash":
        return (
            "\nRUNTIME SHELL: bash (Git Bash) on Windows. Use POSIX syntax in "
            "run_bash - ls, cat, grep, rm, cp, mv, sed, test -d, "
            "./script.sh; && and pipes work. PowerShell cmdlets "
            "(Get-ChildItem, Test-Path, Write-Host, Remove-Item, $env:) do "
            "NOT exist in this shell - translating them wastes a call.\n"
        )
    if platform == "win32" and shell_mode == "powershell":
        return (
            "\nCRITICAL - Windows PowerShell. Use PowerShell-native commands in "
            "run_bash - NOT bash/Linux commands. Example mappings: "
            "'ls' -> 'Get-ChildItem', 'cat' -> 'Get-Content', 'rm' -> 'Remove-Item', "
            "'&&' -> '; if ($?) {'. Use PowerShell syntax ONLY.\n"
            "More PowerShell traps (2026-10-14, moved out of the run_bash schema): "
            "backticks are escape characters (JS template literals break); run code "
            "from files, not node/python -e one-liners with nested quotes; native "
            "tools can print progress to stderr and still succeed - check OUTPUT "
            "before retrying; add '-m 10' to curl health checks.\n"
        )
    if platform.startswith("linux") or platform == "darwin":
        return (
            "\nRUNTIME SHELL: bash/sh on Unix. Use bash commands in run_bash "
            "(ls, cat, grep, ...) - not PowerShell.\n"
        )
    return ""
import tempfile

from . import _shared

_DANGEROUS_PATTERNS = [
    "rm -rf /", "rm -rf ~", "rm -rf .", "rm -rf /*", "rm -rf ~/*",
    "dd if=", "mkfs.", ":(){ :|:& };:", "chmod 777 /", "chmod -R 777 /",
    "> /dev/sda", "> /dev/nvme", "> /dev/hda",
]

_INSTALL_CMDS = {
    "npm":      "npm install {pkgs}",
    # Use the active interpreter (python on PATH / venv) — a bare `pip` can
    # point at a broken/removed Python (e.g. leftover Python311\Scripts\pip.exe)
    # that exits 1 silently on Windows.
    "pip":      "python -m pip install {pkgs}",
    "cargo":    "cargo add {pkgs}",
    "go":       "go get {pkgs}",
    "dotnet":   "dotnet add package {pkgs}",
    "composer": "composer require {pkgs}",
}

_PKG_RE = re.compile(r"[A-Za-z0-9@/._~+=,\- ]+")  # H-audit: < > are PowerShell redirects

# PIP-SOURCE-BUILD (2026-10-17): markers that mean "pip has no wheel for this
# Python and is building from source". Shared by install_package and run_bash.
_PIP_SOURCE_BUILD_MARKERS = (
    "Building wheel for", "Preparing metadata", "config_win.py",
    "Getting requirements to build wheel", "Installing build dependencies",
    "metadata-generation-failed", "subprocess-exited-with-error",
    "Microsoft Visual C++", "error: command", "No module named 'setuptools'",
)

_PIP_INSTALL_RX = re.compile(r"(^|[\s;&|(])(?:\S*python\S*\s+-m\s+)?pip\s+install\s+(.+)",
                             re.IGNORECASE)


def _pip_packages_from_cmd(cmd: str) -> str:
    """Package list of a `pip install ...` command ('' when it is not one)."""
    m = _PIP_INSTALL_RX.search(str(cmd or ""))
    if not m:
        return ""
    _tail = m.group(2).strip()
    # a requirements-file install is not a single package name - no hint
    if re.search(r"(^|\s)(-r|--requirement)\b", _tail):
        return ""
    # drop redirections/pipes and flags; keep the package-ish tokens
    _tail = _tail.split(" 2>&1")[0].split("|")[0].strip()
    _pkgs = [t for t in _tail.split() if not t.startswith("-")]
    return " ".join(_pkgs[:6])


def _pip_source_build_hint(packages: str, venv_py: str = "") -> str:
    """The actionable "no wheel for this Python" hint, shared by both paths."""
    _py = venv_py or "python"
    _pkgs = packages or "<package>"
    return (
        "\n\n[HINT] pip fell back to building from SOURCE - this package has no "
        "prebuilt wheel for your venv's Python version (e.g. pygame on 3.14). "
        "Do NOT retry the same install. Try, in this order:\n"
        f"  1. prove there is no wheel: run_bash(cmd='\"{_py}\" -m pip install "
        f"--only-binary=:all: {_pkgs} 2>&1') - a fast, clear 'no matching "
        "distribution' is the answer, not a compiler wall\n"
        "  2. a wheel-distributed variant (pygame -> 'pygame-ce') or a pure-python "
        "alternative\n"
        "  3. recreate the venv with a Python version that ships wheels for this "
        "package (run_bash: '<that python> -m venv .venv')"
    )

_WIN_DANGEROUS_PATTERNS = [
    "remove-item -recurse -force c:\\", "remove-item -recurse -force c:/",
    "rd /s /q c:\\", "rd /s /q c:/",
    "del /f /s /q c:\\", "del /f /s /q c:/",
    "format ", "diskpart", "cipher /w",
    "\\\\.\\physicaldrive", "bootrec /fixmbr", "bcdedit /delete",
]


async def _inline_tool_run_python(args: dict, workspace: Path, _workspace_lock: str | None) -> str:
    code = args.get("code", "")
    fname = None
    try:
        def _write_temp_python() -> str:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
                f.write(code)
                return f.name

        fname = await asyncio.to_thread(_write_temp_python)
        _py_cmd = "python" if sys.platform == "win32" else "python3"
        # UTF-8 child IO (2026-09-18): without this, child Python on Windows
        # pipes stdout through cp1252 and ANY non-ASCII print (✓/✗, umlauts)
        # raises UnicodeEncodeError inside the snippet — the code crashed on
        # a cosmetic character, not on its logic.
        import os as _os
        _env = {**_os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        from tools.sandbox import ToolJob as _ToolJob, spawn_kwargs as _spawn_kwargs, kill_tree as _kill_tree
        r = await asyncio.create_subprocess_exec(
            _py_cmd,
            fname,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
            env=_env,
            **_spawn_kwargs())
        _job = _ToolJob.confine(r)
        try:
            out, err = await asyncio.wait_for(r.communicate(), 10)
        except asyncio.TimeoutError:
            if _job is not None:
                _job.terminate()
                _job.close()
            _kill_tree(r)
            try:
                r.kill()
            except Exception:
                pass
            return _tool_error_response(
                "RUN_PYTHON_TIMEOUT",
                "Python execution timed out after 10s.",
                tool="run_python" )

        stdout = out.decode(errors="replace").replace("\r\n", "\n").replace("\r", "").strip()
        stderr = err.decode(errors="replace").replace("\r\n", "\n").replace("\r", "").strip()
        if stderr and not stdout:
            return _tool_error_response(
                "RUN_PYTHON_EXEC_ERROR",
                "Python snippet failed.",
                tool="run_python" ,
                details={"stderr": stderr[:2000]})
        result = stdout
        if stderr:
            result += f"\n[stderr]: {stderr[:1000]}"
        # EXIT-CODE-MARKER (2026-09-13): non-empty output previously swallowed
        # a non-zero exit entirely — the verify gates saw success. Append the
        # same marker run_bash uses, as the LAST line.
        if result:
            return result[:4000] + f"\n[exit code: {r.returncode}]"
        if r.returncode == 0:
            return "[run_python: executed successfully — no output produced]"
        return _tool_error_response(
            "RUN_PYTHON_FAILED",
            f"Script exited with code {r.returncode} and no output. "
            "Check for silent exceptions or import errors.",
            tool="run_python")
    except Exception as e:
        return _tool_error_response(
            "RUN_PYTHON_FAILED",
            f"run_python failed: {e}",
            tool="run_python" )
    finally:
        if fname:
            try:
                await asyncio.to_thread(os.unlink, fname)
            except Exception:
                pass


async def _inline_tool_run_bash(args: dict, workspace: Path, _workspace_lock: str | None) -> str:
    cmd = args.get("cmd", "")
    _bl_hit = _bash_blocklisted(cmd)
    if _bl_hit is not None:
        return _tool_error_response(
            "RUN_BASH_BLOCKED",
            f"Command blocked: dangerous pattern '{_bl_hit}' detected. Use safer alternatives.",
            tool="run_bash" , details={"cmd": cmd[:400]})
    try:
        if sys.platform == "win32":
            _re_win = re
            from pathlib import Path as _WPath

            def _do_mkdir(dirs_resolved):
                _created, _errors = [], []
                for _d in dirs_resolved:
                    try:
                        _absd = _WPath(_d)
                        if not _absd.is_absolute():
                            _absd = workspace / _absd
                        if (werr := _inline_check_workspace(_absd, _workspace_lock, "run_bash")) is not None:
                            return werr
                        _absd.mkdir(parents=True, exist_ok=True)
                        _created.append(str(_d))
                    except Exception as _me:
                        _errors.append(f"{_d}: {_me}")
                if _errors:
                    return "[mkdir error]: " + "; ".join(_errors)
                n = len(_created)
                return f"[exit 0 - created {n} director{'y' if n == 1 else 'ies'}: {', '.join(_created)}]"

            # P1-FIX (2026-08-10): Semikolon-verkettete Kommandos (z.B.
            _HAS_SEMICOLON = ";" in cmd
            # SHELL-CHAIN-GUARD (2026-10-26, live): the shims below split their
            # arguments on whitespace and act on EVERY token. A chained command
            # like `touch a.py && echo done` therefore created files named
            # "&&", "echo" and "done" in the workspace. Anything with a shell
            # operator is refused with instructions instead of half-run.
            # & covers && and the single-ampersand separator (both cmd and sh
            # chain with it). A newline starts a second command line, ^ is cmd's
            # escape character. Each of them used to reach the shims as a token
            # and became a file name.
            _SHELL_OPS = ("&", "|", ";", ">", "<", "`", "$(", "\n", "\r", "^")
            _HAS_SHELL_CHAIN = any(op in cmd for op in _SHELL_OPS)
            _cd_mkdir = _re_win.match(
                r'cd\s+("([^"]+)"|(\S+))\s+&&\s+mkdir\s+-p\s+(.*)',
                cmd.strip(), _re_win.DOTALL
            )
            if _cd_mkdir and not _HAS_SHELL_CHAIN:
                _base = _WPath(_cd_mkdir.group(2) or _cd_mkdir.group(3))
                _dirs_raw = _cd_mkdir.group(4).strip()
                _dir_parts = _re_win.findall(r'"([^"]+)"|(\S+)', _dirs_raw)
                _dirs = [_base / (a or b) for a, b in _dir_parts]
                return _do_mkdir(_dirs)

            _mkdir_p_match = _re_win.match(r'mkdir\s+-p\s+(.*)', cmd.strip(), _re_win.DOTALL)
            if _mkdir_p_match and not _HAS_SHELL_CHAIN:
                _raw_paths = _mkdir_p_match.group(1).strip()
                _path_parts = _re_win.findall(r'"([^"]+)"|(\S+)', _raw_paths)
                _dirs = [_WPath(a or b) for a, b in _path_parts]
                return _do_mkdir(_dirs)

            _mkdir_plain = _re_win.match(r'mkdir\s+(.*)', cmd.strip(), _re_win.DOTALL)
            if _mkdir_plain and not _HAS_SHELL_CHAIN:
                _raw_paths = _mkdir_plain.group(1).strip()
                _path_parts = _re_win.findall(r'"([^"]+)"|(\S+)', _raw_paths)
                _dirs = [_WPath(a or b) for a, b in _path_parts]
                return _do_mkdir(_dirs)

            _touch_match = _re_win.match(r'touch\s+(.*)', cmd.strip())
            if _touch_match and _HAS_SHELL_CHAIN:
                return ("[TOOL_ERROR: SHELL_CHAIN_UNSUPPORTED]\n"
                        "run_bash does not chain commands on Windows (found: "
                        "'&&', '||', ';', '|', '>', '<' or a subshell). The "
                        "shell shims would treat the operators as file names.\n"
                        "Run one command per call, or create the file with "
                        "write_file.")
            if _touch_match:
                _touch_raw = _touch_match.group(1).strip()
                _touch_parts = _re_win.findall(r'"([^"]+)"|(\S+)', _touch_raw)
                _touch_files = [a or b for a, b in _touch_parts]
                # AUDIT-FIX M1 (2026-08-25): Exceptions pro Datei sammeln und
                _touch_done, _touch_errs = [], []
                for _tf in _touch_files:
                    try:
                        _tp = _WPath(_tf)
                        if not _tp.is_absolute():
                            _tp = workspace / _tp
                        # S-SEC (2026-08-23): Lock-Check wie bei mkdir.
                        if (werr := _inline_check_workspace(_tp, _workspace_lock, "run_bash")) is not None:
                            return werr
                        _tp.parent.mkdir(parents=True, exist_ok=True)
                        _tp.touch(exist_ok=True)
                        _touch_done.append(str(_tf))
                    except Exception as _te:
                        _touch_errs.append(f"{_tf}: {_te}")
                if _touch_errs:
                    return "[touch error]: " + "; ".join(_touch_errs)
                return f"[exit 0 - touched {len(_touch_done)} file(s)]"

            _rm_match = _re_win.match(r'rm\s+(?:-[rRfF]+\s+)?(.*)', cmd.strip())
            if _rm_match and cmd.strip().startswith("rm ") and _HAS_SHELL_CHAIN:
                return ("[TOOL_ERROR: SHELL_CHAIN_UNSUPPORTED]\n"
                        "run_bash does not chain commands on Windows (found a "
                        "shell operator). Run one command per call.")
            if _rm_match and cmd.strip().startswith("rm "):
                _rm_raw = _rm_match.group(1).strip()
                _rm_parts = _re_win.findall(r'"([^"]+)"|(\S+)', _rm_raw)
                _rm_targets = [a or b for a, b in _rm_parts]
                _rm_done, _rm_errs = [], []
                for _rt in _rm_targets:
                    _rp = _WPath(_rt)
                    if not _rp.is_absolute():
                        _rp = (workspace / _rt).resolve()
                    # Validate path is within workspace
                    _checked = _inline_check_workspace(_rp, _workspace_lock, "run_bash")
                    if _checked is not None:
                        _rm_errs.append(f"{_rt}: {_checked}")
                        continue
                    try:
                        if _rp.is_dir():
                            shutil.rmtree(_rp, ignore_errors=True)
                        elif _rp.exists():
                            _rp.unlink()
                        _rm_done.append(_rt)
                    except Exception as _rme:
                        _rm_errs.append(f"{_rt}: {_rme}")
                if _rm_errs:
                    return "[rm error]: " + "; ".join(_rm_errs)
                return f"[exit 0 - removed: {', '.join(_rm_done)}]"

            # POSIX-DRIVE-FIX (2026-10-17, live): the model writes `/c/Users/...`
            # (what Git Bash shows on Windows). The old rewrite turned that into
            # `C:\\Users\\...` - BACKSLASHES inside a bash command. bash strips
            # them as escapes, so the path collapsed into garbage:
            #   python3 C:\\Users\\<user>\\Desktop\\x.py
            #     -> can't open file 'C:\\\\Users\\\\<user>\\\\Desktop\\\\x.py'
            #     (live: "bash exiting", exit 2 on every absolute path, and the
            #      model then looped on quoting/`C:` theories)
            # Forward slashes are accepted by bash itself AND by native Windows
            # binaries (python.exe/node.exe), so `C:/Users/...` is the only form
            # that works on both sides. The lookbehind also refuses a slash in
            # front (`a/b/c`, `http://x/c/y`) - those are not drive mounts.
            _cmd_unix_fixed = re.sub(
                r'(?<![\w/])/([a-zA-Z])/',
                lambda _m: _m.group(1).upper() + ":/",
                cmd)
            if _cmd_unix_fixed != cmd:
                cmd = _cmd_unix_fixed
                # BLOCKLIST-RECHECK (audit 2026-10-24): the rewrite itself can
                # COMPLETE a dangerous pattern — "/c/users" -> "C:/users" turns
                # `rd /s /q /c/users` into a blocklisted form. The old second
                # entry-check was removed as "redundant on unchanged cmd";
                # cmd IS reassigned above, so the rewritten string needs its
                # own check.
                _bl_rewrite = _bash_blocklisted(cmd)
                if _bl_rewrite is not None:
                    return _tool_error_response(
                        "RUN_BASH_BLOCKED",
                        f"Command blocked: dangerous pattern '{_bl_rewrite}' detected. Use safer alternatives.",
                        tool="run_bash" , details={"cmd": cmd[:400]})

            _cmd_translated = cmd
            _cd_strip = _re_win.match(
                r'cd\s+("([^"]+)"|([^&\s]+))\s+&&\s+(.*)', cmd.strip(), _re_win.DOTALL
            )
            if _cd_strip:
                _cd_target = _cd_strip.group(2) or _cd_strip.group(3)
                _cd_rest = _cd_strip.group(4).strip()
                from pathlib import Path as _PCD
                _pc = _PCD(_cd_target)
                if not _pc.is_absolute():
                    _pc = workspace / _pc
                try:
                    _cd_resolved = _pc.resolve()
                except Exception:
                    _cd_resolved = _pc
                try:
                    _ws_resolved = workspace.resolve()
                except Exception:
                    _ws_resolved = workspace
                _ws_prefix = str(_ws_resolved).rstrip("\\/") + "\\"
                if (str(_cd_resolved).rstrip("\\/").lower() == str(_ws_resolved).rstrip("\\/").lower()
                        or str(_cd_resolved).lower().startswith(_ws_prefix.lower())):
                    _cmd_translated = _cd_rest

            if " && " in _cmd_translated:
                _and_parts = _cmd_translated.split(" && ")
                _ps_seq = []
                for _ap in _and_parts[:-1]:
                    _ps_seq.append(_ap.strip())
                    _ps_seq.append("if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }")
                _ps_seq.append(_and_parts[-1].strip())
                _cmd_translated = "; ".join(_ps_seq)

            if re.search(r"(?<![-\w])(curl|wget)(?=[\s.]|$)", _cmd_translated, re.IGNORECASE):
                _cmd_translated = (
                    "Remove-Item alias:curl,alias:wget -ErrorAction SilentlyContinue; "
                    + _cmd_translated
                )

            if "2>&1" in _cmd_translated:
                _cmd_translated = (
                    "$ErrorActionPreference='SilentlyContinue'; "
                    + "& { " + _cmd_translated + " } 2>&1 | ForEach-Object { \"$_\" }; "
                    + "if ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 0 }"
                )

        _cmd_l = cmd.lower()
        _is_build = any(k in _cmd_l for k in (
            "mvn ", "gradle", "npm install", "npm ci", "npm run build",
            "cargo build", "cargo test", "dotnet build", "dotnet restore",
            "pip install", "go build", "go test", "docker build",
            "docker compose", "docker-compose"))
        _build_timeout = 600
        try:
            from core.state import settings as _hs_settings
            _build_timeout = int(_hs_settings.get("duo_run_bash_build_timeout_s", 600))
        except Exception:
            _build_timeout = 600
        _build_timeout = max(90, min(3600, _build_timeout))
        _bash_timeout = _build_timeout if _is_build else 90

        async def _keep_alive():
            try:
                from backend.llama_server_manager import manager as _ka_mgr
                while True:
                    await asyncio.sleep(30)
                    for _sl in _ka_mgr._slots:
                        if _sl.model and _sl.is_running:
                            _sl.touch()
            except Exception:
                pass

        _ka_task = asyncio.create_task(_keep_alive()) if _is_build else None
        try:
            # OEM-Codepage (z.B. cp850) — bytes.decode() (UTF-8) produzierte
            _ps_cmd = (
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + _cmd_translated
                if sys.platform == "win32" else _cmd_translated
            )
            _win_bash_exe = ""
            if sys.platform == "win32" and _run_bash_shell_mode() == "bash":
                _win_bash_exe = _windows_bash_path()
            if _win_bash_exe:
                # AUDIT FIX (1.3.3): the model's command runs in bash
                # verbatim - the powershell translation layer above is
                # skipped (POSIX syntax works natively there).
                _stream_cmd = [_win_bash_exe, "-c", cmd]
            else:
                _stream_cmd = (
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _ps_cmd]
                    if sys.platform == "win32"
                    else [_BASH_BIN, "-c", cmd]  # X6: not every distro ships /bin/bash
                )
            stdout_raw, stderr_raw, returncode, timed_out = await _stream_proc(
                _stream_cmd, _bash_timeout, str(workspace), os.environ,
            )
        finally:
            if _ka_task:
                _ka_task.cancel()

        if timed_out:
            # S-SEC (2026-08-23): tree kill now happens IN _stream_proc per
            # the result actively coaches there; server/watcher patterns
            _longrun = re.search(
                r"(http\.server|uvicorn|flask|gunicorn|npm\s+(run\s+)?(dev|start)|vite|next\s+dev|watch)",
                str(cmd).lower())
            _hint = (
                "\n\nThis looks like a LONG-RUNNING process (server/watcher). "
                "Do NOT retry it with run_bash — use the start_background tool instead, "
                "then check with get_background_output and stop with stop_background."
                if _longrun else
                "\n\nIf this is a long-running process (dev server, watcher), use the "
                "start_background tool instead — run_bash is for commands that finish."
            )
            return _tool_error_response(
                "RUN_BASH_TIMEOUT",
                f"run_bash timed out after {_bash_timeout}s. Consider splitting the command." + _hint,
                tool="run_bash" ,
                details={"cmd": str(cmd)[:400], "timeout_s": int(_bash_timeout)})

        stdout = stdout_raw.strip()
        stderr = stderr_raw.strip()
        # SHELL-NAME (2026-10-15): computed unconditionally - the NONZERO
        # error must name the shell that ran so a PS-syntax-into-bash
        # mismatch is diagnosable even when stdout is non-empty.
        _shell_name = ("bash (Git Bash)" if _win_bash_exe
                       else "powershell.exe (Windows)" if sys.platform == "win32"
                       else "bash")
        result = ""
        if stderr:
            result += f"[stderr]\n{stderr}\n---\n"
        result += stdout
        if returncode != 0:
            result += f"\n[exit code: {returncode}]"
        if returncode != 0 and not stdout:
            result += f"\n[shell: {_shell_name}]"
        if returncode != 0 and _win_bash_exe \
                and "command not found" in (stderr or "").lower() \
                and re.search(r"(Test-Path|Get-ChildItem|Get-Content|Remove-Item|"
                              r"\$LASTEXITCODE|\$env:)", cmd):
            # the model emitted POWERSHELL syntax into the bash shell -
            # coach it instead of letting it loop on the same error
            result += ("\n[shell note: the shell is bash (Git Bash). Use POSIX syntax "
                       "- ls, cat, rm, cp, test -d; PowerShell cmdlets like "
                       "Get-ChildItem/Test-Path do not exist here.]")

        _is_docker_detached = (
            returncode == 0 and not result
            and ("docker compose" in _cmd_l or "docker-compose" in _cmd_l)
            and " -d" in _cmd_l
        )
        if _is_docker_detached:
            result = (
                "[exit 0 - docker compose started detached. Containers running in background.]\n"
                "Check status: docker compose ps\n"
                "Check logs:   docker compose logs --tail=50"
            )

        if len(result) > 6000:
            _dropped = len(result) - 5900
            result = f"[OUTPUT TRUNCATED: hard cap 6000 chars — dropped {_dropped} chars from start]\n" + result[-5900:]

        if returncode != 0:
            _result_tail = result[-2500:] if result else "(no output)"
            _err = _tool_error_response(
                "RUN_BASH_NONZERO",
                f"run_bash exited with code {returncode} (shell: {_shell_name}).\n{_result_tail}",
                tool="run_bash" ,
                details={
                    "cmd": str(cmd)[:400],
                    "exit_code": int(returncode),
                    "stdout_tail": stdout[-1200:] if stdout else "",
                    "stderr_tail": stderr[-1200:] if stderr else "",
                })
            # PIP-HINT-IN-RUN_BASH (2026-10-17): the same "no wheel for this
            # Python" guidance must reach the model when IT runs pip itself.
            # install_package is tier 99 and is dropped at small windows, so at
            # exactly the sizes where a small model needs the hint, run_bash is
            # the only remaining path.
            _pkgs = _pip_packages_from_cmd(str(cmd))
            if _pkgs and any(_p in _err for _p in _PIP_SOURCE_BUILD_MARKERS):
                _err += _pip_source_build_hint(_pkgs)
            return _err
        return f"[run_bash: exit 0]\n{result}" if result else "[run_bash: exit 0 — no output]"
    except Exception as e:
        return _tool_error_response(
            "RUN_BASH_EXEC_ERROR",
            f"run_bash execution error: {e}",
            tool="run_bash" ,
            details={"cmd": str(cmd)[:400]})


# ── Deterministic error rollup (core/error_rollup.py) ────────────────────────

def _error_rollup_enabled() -> bool:
    try:
        from core import state as _er_st
        return bool(_er_st.settings.get("duo_error_rollup", True))
    except Exception:
        return True


def _error_rollup_state():
    """Return (run_id, RollupState) or (None, None) when no active run context."""
    try:
        from tools.runner import _current_run_id as _crid_rollup
    except Exception:
        return None, None
    run_id = _crid_rollup.get() or ""
    if not run_id:
        return None, None
    try:
        from core.error_rollup import get_run_state
        return run_id, get_run_state(run_id)
    except Exception:
        return None, None


def _rollup_test_failure(tr, original: str) -> str:
    """Compact repeated failures (PERSISTS/FIXED/REOPENED). First failing round
    stays untouched; the rollup only kicks in once a signature repeats."""
    if not _error_rollup_enabled():
        return original
    _run_id, _st = _error_rollup_state()
    if _st is None:
        return original
    try:
        from core.error_rollup import parse_errors, render_rollup
        _items = parse_errors(getattr(tr, "raw_output", None) or "")
    except Exception:
        return original
    if not _items:
        return original
    try:
        _res = _st.ingest(_items)
    except Exception:
        return original
    if not _res.get("changed"):
        return original
    try:
        _lines = render_rollup(_res)
        _head = (
            f"[TEST-RESULT] ❌ {getattr(tr, 'failure_count', 0)} test failure(s) "
            f"({getattr(tr, 'language', '?')})\nCommand: {getattr(tr, 'command', '')}"
        )
        return _head + "\n\nErrors:\n" + "\n".join(_lines) + \
            "\n\nFix the above failures. Run tests again after fixing."
    except Exception:
        return original


def _rollup_note_clean() -> None:
    """A green run marks all still-active signatures as resolved (so an identical
    later failure renders as REOPENED, not NEW)."""
    if not _error_rollup_enabled():
        return
    _run_id, _st = _error_rollup_state()
    if _st is None:
        return
    try:
        _st.mark_all_fixed()
    except Exception:
        pass


async def _inline_tool_run_tests(args: dict, _workspace: Path, workspace_lock: str | None) -> str:


    if _shared._run_test_suite is None:
        return _tool_error_response(
            "RUN_TESTS_UNAVAILABLE",
            "run_tests is unavailable (test runner not wired). Use run_bash with the test command instead.",
            tool="run_tests" )
    _ws_str = str(workspace_lock or _workspace or os.environ.get("HIVEMIND_WORKSPACE", "."))
    try:
        _timeout = int(args.get("timeout") or 90)
    except (TypeError, ValueError):
        _timeout = 90
    _timeout = max(10, min(300, _timeout))
    _lang = str(args.get("lang_override") or "").strip() or None
    _chat_id = ""
    try:
        from tools.runner import _current_run_id as _crid_run_tests
        _chat_id = _crid_run_tests.get() or ""
    except Exception:
        pass
    try:
        _tr = await _shared._run_test_suite(
            workspace=str(Path(_ws_str).resolve()),
            timeout=_timeout,
            lang_override=_lang,
            chat_id=_chat_id,
        )
    except Exception as e:
        return _tool_error_response(
            "RUN_TESTS_EXEC_ERROR",
            f"run_tests crashed: {type(e).__name__}: {str(e)[:200]}",
            tool="run_tests" )
    if _tr.is_clean():
        _rollup_note_clean()
        return f"[TEST-RESULT] ✅ All tests passed ({_tr.language}). Command: {_tr.command}"
    if _tr.failure_count == 0:
        return (
            f"[TEST-RESULT] ⚠️ No tests found or no test command available "
            f"({_tr.language}). Command: {_tr.command}\n{(_tr.inject_msg or '')[:400]}"
        )
    return _rollup_test_failure(_tr, _tr.inject_msg or "[TEST-RESULT] No result from test runner.")


async def _inline_tool_install_package(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    manager = str(args.get("manager", "")).strip().lower()
    packages = str(args.get("packages", "")).strip()
    dev = bool(args.get("dev", False))
    if not manager or not packages:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "install_package requires 'manager' (npm/pip/cargo/go/dotnet/composer) "
            "and 'packages' (space-separated list).",
            tool="install_package" )
    if manager not in _INSTALL_CMDS:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            f"Unsupported manager '{manager}'. Supported: {', '.join(sorted(_INSTALL_CMDS))}.",
            tool="install_package" )
    ok_pkgs, pkgs_msg = _validate_install_packages(packages)
    if not ok_pkgs:
        return _tool_error_response("INVALID_ARGUMENT", f"packages rejected: {pkgs_msg}", tool="install_package")
    packages = pkgs_msg
    # PIP-VENV-GUARD (2026-09-13): "python -m pip" resolves via the inherited
    # PATH — without a workspace venv it installs into HIVEMIND'S OWN venv and
    # can break the running server. Require a workspace-local interpreter.
    _ws_s = str(workspace or "")
    if manager == "pip":
        _venv_py = None
        for _cand in (Path(_ws_s) / ".venv" / "Scripts" / "python.exe",
                      Path(_ws_s) / ".venv" / "bin" / "python",
                      Path(_ws_s) / "venv" / "Scripts" / "python.exe",
                      Path(_ws_s) / "venv" / "bin" / "python"):
            if _cand.exists():
                _venv_py = _cand
                break
        if _venv_py is None:
            return _tool_error_response(
                "PIP_NO_WORKSPACE_VENV",
                "pip refused: no .venv found in the workspace. Installing into the "
                "HiveMind server venv is not allowed (it would break the server). "
                "Create one first via run_bash: 'python -m venv .venv', then retry "
                "install_package (it will use .venv automatically).",
                tool="install_package")
        _cmd = f'"{_venv_py}" -m pip install --prefer-binary {packages}'
    else:
        _cmd = _INSTALL_CMDS[manager].format(pkgs=packages)
    if dev and manager == "npm":
        _cmd += " --save-dev"
    from tools.runner import _run_inline_tool as _dispatch_bash
    _res = await _dispatch_bash("run_bash", {"cmd": _cmd}, workspace_lock=workspace_lock)
    # TOOL-LABEL-FIX (2026-10-17, live): install_package runs its command
    # through the run_bash handler, so every failure came back labelled
    #   [TOOL_ERROR:RUN_BASH_NONZERO] run_bash: run_bash exited with code 1
    # for a call the model made as install_package (screenshot: "install_package
    # · pip: pygame" followed by a run_bash error). Confusing in the chat, in
    # the error-rollup rollup keys and in the telemetry. Re-label the wrapper.
    if "RUN_BASH_NONZERO" in _res or "RUN_BASH_EXEC_ERROR" in _res:
        _code = "INSTALL_FAILED" if "RUN_BASH_NONZERO" in _res else "INSTALL_EXEC_ERROR"
        _body = _res
        for _tag in ("[TOOL_ERROR:RUN_BASH_NONZERO]", "[TOOL_ERROR:RUN_BASH_EXEC_ERROR]"):
            _body = _body.replace(_tag, "")
        _body = _body.replace("run_bash: run_bash exited with code",
                              f"install_package: '{manager} install {packages}' failed, exit code")
        _body = _body.replace("run_bash exited with code",
                              f"install_package: '{manager} install {packages}' failed, exit code")
        _body = _body.replace("run_bash execution error",
                              f"install_package: '{manager} install {packages}' execution error")
        _body = _body.replace('"tool": "run_bash"', '"tool": "install_package"')
        _res = f"[TOOL_ERROR:{_code}]" + _body
    # SOURCE-BUILD-HINT (2026-09-15, broadened + shared 2026-10-17): when pip
    # has no prebuilt wheel for this venv's Python it silently switches to an
    # sdist build and the model gets a compiler wall. The old trigger only
    # matched "Building wheel for"/"Preparing metadata"/"config_win.py" - the
    # live pygame/3.14 run showed "Installing build dependencies" +
    # "Getting requirements to build wheel ... status 'error'" + a
    # FileNotFoundError instead, so no hint appeared and the model retried.
    # The same hint now also fires for a pip install run through run_bash (see
    # _pip_source_build_hint below): install_package is tier 99 and drops out at
    # small windows, and then run_bash is the only path the model has.
    if any(_p in _res for _p in _PIP_SOURCE_BUILD_MARKERS):
        _res += _pip_source_build_hint(packages, str(_venv_py))
    return _res


async def _inline_tool_start_background(args: dict, _workspace: Path, _workspace_lock: str | None) -> str:
    from tools.background import start_background
    cmd = str(args.get("cmd", "")).strip()
    if not cmd:
        return _tool_error_response("INVALID_ARGUMENT", "start_background requires a non-empty 'cmd'.", tool="start_background")
    # run_bash-Blocklist (format/diskpart/Remove-Item -Recurse -Force etc.).
    _bl_hit = _bash_blocklisted(cmd)
    if _bl_hit:
        return _tool_error_response(
            "COMMAND_BLOCKED",
            f"Command blocked: dangerous pattern '{_bl_hit}' detected. "
            "Background processes cannot bypass the destructive-command blocklist.",
            tool="start_background", details={"cmd": cmd[:400]})
    try:
        res = await asyncio.to_thread(start_background, cmd)
    except Exception as e:
        return _tool_error_response("BACKGROUND_START_FAILED", f"{type(e).__name__}: {str(e)[:160]}", tool="start_background")
    if not res.get("ok"):
        return _tool_error_response("BACKGROUND_START_FAILED", str(res.get("error", "unknown")), tool="start_background")
    _evict_note = ""
    if res.get("evicted"):
        _ev = res["evicted"]
        _evict_note = (
            f"\n[NOTE] Process limit reached — OLDEST background process was "
            f"evicted and terminated: handle={_ev.get('handle')} pid={_ev.get('pid')} "
            f"cmd={_ev.get('cmd', '')!r}. Its old handle is now dead."
        )
    return (f"[background started] handle={res['handle']} pid={res['pid']}{_evict_note}\n"
            f"Check output with get_background_output('{res['handle']}'), stop with stop_background('{res['handle']}').")


async def _inline_tool_get_background_output(args: dict, _workspace: Path, _workspace_lock: str | None) -> str:
    from tools.background import get_background_output, list_background
    handle = str(args.get("handle", "")).strip()
    if not handle:
        _lst = list_background()
        if not _lst:
            return "[background: no running processes]"
        _lines = ["[background processes]"]
        for _e in _lst:
            _lines.append(f"- {_e['handle']} pid={_e['pid']} running={_e['running']} {_e['cmd']}")
        return "\n".join(_lines)
    return await asyncio.to_thread(get_background_output, handle)


async def _inline_tool_stop_background(args: dict, _workspace: Path, _workspace_lock: str | None) -> str:
    from tools.background import stop_background
    handle = str(args.get("handle", "")).strip()
    if not handle:
        return _tool_error_response("INVALID_ARGUMENT", "stop_background requires a 'handle'.", tool="stop_background")
    ok = await asyncio.to_thread(stop_background, handle)
    return f"[background stopped: {handle}]" if ok else f"[background: no process '{handle}']"


async def _stream_proc(cmd: list[str], timeout_s: int, cwd: str, env: dict,
                       line_cap: int = 300) -> tuple[str, str, int, bool]:
    from collections import deque
    from tools.sandbox import ToolJob as _ToolJob, spawn_kwargs as _spawn_kwargs, kill_tree as _kill_tree
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        **_spawn_kwargs(),
    )
    _job = _ToolJob.confine(proc)
    LINE_CAP = line_cap
    stdout_buf: deque = deque(maxlen=LINE_CAP)
    stderr_buf: deque = deque(maxlen=LINE_CAP)

    async def drain(stream, buf: deque):
        async for line in stream:
            buf.append(line.decode(errors="replace").rstrip("\r\n"))

    try:
        try:
            await asyncio.wait_for(
                asyncio.gather(drain(proc.stdout, stdout_buf), drain(proc.stderr, stderr_buf)),
                timeout=timeout_s,
            )
            await proc.wait()
        except asyncio.TimeoutError:
            # nachgelagertem /IM-Massaker auf powershell.exe.
            if _job is not None:
                _job.terminate()
            _kill_tree(proc)
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
            return "\n".join(stdout_buf), "\n".join(stderr_buf), -1, True

        return "\n".join(stdout_buf), "\n".join(stderr_buf), proc.returncode or 0, False
    finally:
        if _job is not None:
            _job.close()


def _bash_blocklisted(cmd: str) -> str | None:
    _c = (cmd or "").lower()
    pats = _DANGEROUS_PATTERNS + (_WIN_DANGEROUS_PATTERNS if sys.platform == "win32" else [])
    for _d in pats:
        if _d in _c:
            return _d
    return None


def _validate_install_packages(packages: str) -> tuple[bool, str]:


    p = str(packages or "").strip()
    if not p:
        return False, "packages is empty"
    if not _PKG_RE.fullmatch(p):
        bad = "".join(sorted({ch for ch in p if not _PKG_RE.fullmatch(ch)}))
        return False, f"unsupported characters: {bad!r}"
    # CLI-Flags durch ('-r http://evil/x.txt' → pip install -r <URL>,
    for _tok in p.split():
        if _tok.startswith("-"):
            return False, f"package tokens must not start with '-' (flag injection): {_tok!r}"
    return True, p


def _ps_quote_path(cmd: str, raw_path: str) -> str:


    q = "'" + raw_path.replace("'", "''") + "'"
    return cmd.replace(raw_path, q, 1)
