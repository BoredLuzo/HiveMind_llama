"""Tool handlers: execution tools (bash/python/tests/install/background) (part of tools/handlers, extracted from tools/handlers.py)."""

from __future__ import annotations

from pathlib import Path
from utils.file import _inline_check_workspace, _is_junction, _is_protected_path, is_secret_path
from tools.errors import tool_error_response as _tool_error_response
import asyncio
import glob
import os
import re
import shutil
import stat
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
            "THIS MACHINE IS WINDOWS. Absolute file paths look like "
            "C:/Users/<name>/file.js. Paths like /Users/... are macOS and do "
            "NOT exist here; /c/Users/... is just Git Bash's spelling of "
            "C:/Users/.... Never 're-create' a file because a /Users/... "
            "path failed - list_dir or find_files first.\n"
            "Use forward slashes for paths (e.g. .venv/Scripts/python.exe, "
            "never unquoted backslashes like .venv\\Scripts\\python.exe).\n"
            "To open or verify a local HTML page use the browser tool "
            "(Playwright is pre-installed) - do NOT pip-install playwright "
            "or run browser downloads.\n"
            "Validate syntax with the RIGHT runtime: JS with "
            "'node --check file.js', Python with "
            "'python -m py_compile file.py'. Python's compile() on "
            "JavaScript ALWAYS fails and does not mean the JS is broken.\n"
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
            "THIS MACHINE IS WINDOWS. Absolute file paths look like "
            "C:\\Users\\<name>\\file.js. Paths like /Users/... are macOS and do "
            "NOT exist here. Never 're-create' a file because a /Users/... "
            "path failed - list_dir or find_files first.\n"
            "To open or verify a local HTML page use the browser tool "
            "(Playwright is pre-installed) - do NOT pip-install playwright "
            "or run browser downloads.\n"
        )
    if platform.startswith("linux") or platform == "darwin":
        return (
            "\nRUNTIME SHELL: bash/sh on Unix. Use bash commands in run_bash "
            "(ls, cat, grep, ...) - not PowerShell.\n"
            "Validate syntax with the RIGHT runtime: JS with "
            "'node --check file.js', Python with "
            "'python -m py_compile file.py'. Python's compile() on "
            "JavaScript ALWAYS fails and does not mean the JS is broken.\n"
        )
    return ""
import tempfile

from . import _shared


_PIP_INSTALL_SEG_RE = re.compile(
    r"^\s*(?:\w+=\S+\s+)*(?:pip3?(?:\.exe)?|(?:python3?|py)(?:\.exe)?\s+-m\s+pip|uv(?:\.exe)?\s+pip)\s+install\b",
    re.IGNORECASE,
)
_EXPLICIT_VENV_FLAG_RE = re.compile(
    r"(?:--python\s+['\"]?(?:\.?[/\\])?(?:\.venv|venv)\b|--target\s+|-t\s+)",
    re.IGNORECASE,
)
_CREATES_VENV_RE = re.compile(
    r"\b(?:python3?|py|uv)(?:\.exe)?\s+(?:-m\s+venv|venv)\b",
    re.IGNORECASE,
)
_ACTIVATES_VENV_RE = re.compile(
    r"(?:^|\s)(?:source|\.)\s+['\"]?(?:\.?[/\\])?(?:\.venv|venv)[/\\](?:Scripts|bin)[/\\]activate\b",
    re.IGNORECASE,
)
from ._win_shims import (
    _POSIX_DRIVE_PATH_RE,  # noqa: F401 (re-export)
    _POSIX_TMP_PATH_RE,  # noqa: F401 (re-export)
    _SEARCH_NO_MATCH_VERBS,
    _SEARCH_OR_AWK_VERBS,  # noqa: F401 (re-export)
    _SED_AWK_VERBS,  # noqa: F401 (re-export)
    _WIN_BASH_VENV_PATH_RE,  # noqa: F401 (re-export)
    _WIN_NATIVE_DEV_NULL_RE,  # noqa: F401 (re-export)
    _WIN_NATIVE_DEV_NULL_VERBS,  # noqa: F401 (re-export)
    _build_win_shell_cmd,
    _command_verb_before,  # noqa: F401 (re-export)
    _extract_cmd_verb,
    _format_timeout_partial,
    _has_unquoted_token,
    _iter_ps_unquoted,  # noqa: F401 (re-export)
    _normalize_win_bash_venv_paths,
    _parse_shim_tokens,
    _parse_shim_tokens_quoted,
    _rewrite_posix_drive_paths,  # noqa: F401 (re-export)
    _rewrite_win_native_dev_null,
    _rewrite_win_tmp_paths,
    _should_rewrite_posix_drive,
    _split_shim_args,
    _split_shim_args_quoted,
    _split_unquoted_and,
    _tail_slice,
)


def _is_search_no_match(cmd: str, returncode: int, stdout: str, stderr: str) -> bool:
    """Return True when a standalone grep/rg command exited 1 strictly with 0 matches."""
    if returncode != 1 or (stdout or "").strip() or (stderr or "").strip():
        return False
    s = str(cmd or "")
    quote: str | None = None
    last_pipe_start = 0
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if quote is None:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch in ("'", '"'):
                quote = ch
                i += 1
                continue
            if ch == "&":
                # Allow fd redirections like 2>&1 or &>file inside a single grep command
                if i > 0 and s[i - 1] == ">":
                    i += 1
                    continue
                if i + 1 < n and s[i + 1] == ">":
                    i += 2
                    continue
                return False
            if ch in ";\n\r":
                return False
            if s.startswith("||", i):
                return False
            if ch == "|":
                last_pipe_start = i + 1
                i += 1
                continue
            i += 1
        elif quote == "'":
            if ch == "'":
                quote = None
            i += 1
        else:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1

    last_seg = s[last_pipe_start:].strip()
    if not last_seg:
        return False
    return _extract_cmd_verb(last_seg) in _SEARCH_NO_MATCH_VERBS




_UV_PIP_INSTALL_SEG_RE = re.compile(
    r"^\s*(?:\w+=\S+\s+)*uv(?:\.exe)?\s+pip\s+install\b",
    re.IGNORECASE,
)


def _is_unscoped_pip_install(cmd: str, workspace: Path | None) -> bool:
    """Return True if *cmd* runs pip install without a workspace .venv present."""
    from infra.child_env import workspace_venv_python
    if workspace_venv_python(workspace) is not None:
        return False
    _creates_venv = False
    _activates_venv = False
    for seg in re.split(r"&&|\|\||[;\n|]", str(cmd or "")):
        if _CREATES_VENV_RE.search(seg):
            _creates_venv = True
        if _ACTIVATES_VENV_RE.search(seg):
            _activates_venv = True
        if (
            _PIP_INSTALL_SEG_RE.match(seg)
            and not _EXPLICIT_VENV_FLAG_RE.search(seg)
            and not (_creates_venv and (_activates_venv or _UV_PIP_INSTALL_SEG_RE.match(seg)))
        ):
            return True
    return False


def _tool_env_with_python(workspace: Path | None = None) -> dict:
    """Child env for tool runs: workspace .venv -> real system Python -> engine venv fallback."""
    from infra.child_env import tool_env as _tool_env
    return _tool_env(workspace=workspace)

_DANGEROUS_PATTERNS = [
    "dd if=", "mkfs.", ":(){ :|:& };:",
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
    "Failed building wheel", "pyproject_hooks",
    "metadata-generation-failed", "subprocess-exited-with-error",
    "Microsoft Visual C++", "error: command", "No module named 'setuptools'",
)

_PIP_INSTALL_RX = re.compile(
    r"(?:^|[\s;&|(])(?:(?:\"[^\"]*python[^\"]*\"|'[^']*python[^']*'|\S*python\S*)\s+-m\s+)?pip3?(?:\.exe)?\s+install\s+(.+)",
    re.IGNORECASE,
)


def _pip_packages_from_cmd(cmd: str) -> str:
    """Package list of a `pip install ...` command ('' when it is not one)."""
    m = _PIP_INSTALL_RX.search(str(cmd or ""))
    if not m:
        return ""
    _tail = m.group(1).strip()
    # a requirements-file install is not a single package name - no hint
    if re.search(r"(^|\s)(-r|--requirement)\b", _tail):
        return ""
    # drop chained commands, redirections/pipes and flags; keep the package-ish tokens
    _tail = re.split(r"\s*(?:&&|\|\||;|\||2>&1|>|<)", _tail, maxsplit=1)[0].strip()
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
    "cipher /w",
    "\\\\.\\physicaldrive", "bootrec /fixmbr", "bcdedit /delete",
]


async def _inline_tool_run_python(args: dict, workspace: Path, _workspace_lock: str | None) -> str:
    code = args.get("code", "")
    fname = None
    bench_cmd = None
    try:
        # BENCH-CONTAINER: run snippets in the task image, like run_bash.
        # The temp file lives in the bind-mounted workspace so the container
        # sees the same bytes; no mapping -> normal host Python below.
        try:
            from tools.bench_container import bash_argv as _bench_bash_argv
            if _bench_bash_argv("true", workspace) is not None:
                _fd, _tmp = tempfile.mkstemp(suffix=".py", dir=workspace)
                os.close(_fd)
                fname = Path(_tmp)
                fname.write_text(code, encoding="utf-8")
                bench_cmd = _bench_bash_argv(f"python3 {fname.name}", workspace)
        except (ImportError, OSError, ValueError):
            bench_cmd = None

        if fname is None:
            def _write_temp_python() -> str:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
                    f.write(code)
                    return f.name

            fname = await asyncio.to_thread(_write_temp_python)
        # 3-tier resolution (same as run_bash and Python LSP):
        # workspace .venv -> real system Python -> engine venv fallback.
        from infra.child_env import resolve_workspace_python as _resolve_ws_py
        _resolved_py = _resolve_ws_py(workspace)
        _py_cmd = str(_resolved_py) if _resolved_py is not None else (
            sys.executable or ("python" if sys.platform == "win32" else "python3"))
        # UTF-8 child IO (2026-09-18): without this, child Python on Windows
        # pipes stdout through cp1252 and ANY non-ASCII print (✓/✗, umlauts)
        # raises UnicodeEncodeError inside the snippet — the code crashed on
        # a cosmetic character, not on its logic.
        _env = _tool_env_with_python(workspace)
        _ws_path = Path(workspace)
        _py_parts = [str(_ws_path)]
        if (_ws_path / "src").is_dir():
            _py_parts.insert(0, str(_ws_path / "src"))
        if _env.get("PYTHONPATH"):
            _py_parts.append(_env["PYTHONPATH"])
        _env["PYTHONPATH"] = os.pathsep.join(_py_parts)
        _py_argv = list(bench_cmd) if bench_cmd else [_py_cmd, str(fname)]
        _py_timeout = 60 if bench_cmd else 45
        stdout_raw, stderr_raw, _rc, _timed_out = await _stream_proc(
            _py_argv, _py_timeout, str(workspace), _env, line_cap=300
        )
        if _timed_out:
            _py_partial_out, _py_partial_err, _py_partial_msg = _format_timeout_partial(
                stdout_raw, stderr_raw
            )
            return _tool_error_response(
                "RUN_PYTHON_TIMEOUT",
                "Python execution timed out." + _py_partial_msg,
                tool="run_python",
                details={
                    "timeout_s": int(_py_timeout),
                    "stdout_tail": _py_partial_out[-800:] if _py_partial_out else "",
                    "stderr_tail": _py_partial_err[-600:] if _py_partial_err else "",
                },
            )
        stdout = stdout_raw.replace("\r\n", "\n").replace("\r", "").strip()
        stderr = stderr_raw.replace("\r\n", "\n").replace("\r", "").strip()
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
            _capped = (
                result[:2200] + "\n...[truncated]...\n" + result[-1700:]
                if len(result) > 4000
                else result
            )
            if _rc != 0:
                return _tool_error_response(
                    "RUN_PYTHON_EXEC_ERROR",
                    f"Python snippet exited with code {_rc}.\n{_capped}\n[exit code: {_rc}]",
                    tool="run_python",
                    details={
                        "exit_code": int(_rc),
                        "stdout_tail": stdout[-2000:] if stdout else "",
                        "stderr_tail": stderr[-2000:] if stderr else "",
                    },
                )
            return f"{_capped}\n[exit code: 0]"
        if _rc == 0:
            return "[run_python: executed successfully — no output produced]"
        return _tool_error_response(
            "RUN_PYTHON_FAILED",
            f"Script exited with code {_rc} and no output. "
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
    cmd = str((args or {}).get("cmd", "") or "")
    if not cmd.strip():
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "run_bash requires a non-empty 'cmd' string.",
            tool="run_bash",
        )
    _bl_hit = _bash_blocklisted(cmd)
    if _bl_hit is not None:
        return _tool_error_response(
            "RUN_BASH_BLOCKED",
            f"Command blocked: dangerous pattern '{_bl_hit}' detected. Use safer alternatives.",
            tool="run_bash" , details={"cmd": cmd[:400]})
    # A3 (audit 2026-11-03): POSIX has no translation layer. This used to be
    # assigned only inside the win32 block below, so every POSIX run_bash read
    # an unbound name and died as RUN_BASH_EXEC_ERROR.
    _cmd_translated = cmd
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

            _mkdir_match = _re_win.match(r'mkdir\s+(.*)', cmd.strip(), _re_win.DOTALL)
            if _mkdir_match and not _HAS_SHELL_CHAIN:
                _raw_paths = _mkdir_match.group(1).strip()
                _, _mkdir_ops = _parse_shim_tokens(
                    _split_shim_args(_raw_paths), value_flags=("-m", "--mode")
                )
                if not _mkdir_ops:
                    return "[mkdir error]: no directory specified"
                _dirs = [_WPath(_d) for _d in _mkdir_ops]
                return _do_mkdir(_dirs)

            _touch_match = _re_win.match(r'touch\s+(.*)', cmd.strip(), _re_win.DOTALL)
            if _touch_match and _HAS_SHELL_CHAIN:
                return ("[TOOL_ERROR: SHELL_CHAIN_UNSUPPORTED]\n"
                        "run_bash does not chain commands on Windows (found: "
                        "'&&', '||', ';', '|', '>', '<' or a subshell). The "
                        "shell shims would treat the operators as file names.\n"
                        "Run one command per call, or create the file with "
                        "write_file.")
            if _touch_match:
                _touch_raw = _touch_match.group(1).strip()
                _touch_flags, _touch_files = _parse_shim_tokens(
                    _split_shim_args(_touch_raw),
                    value_flags=("-t", "-d", "-r", "--date", "--reference"),
                )
                if not _touch_files:
                    return "[touch error]: no target path specified"
                _touch_no_create = any(
                    f == "--no-create"
                    or (f.startswith("-") and not f.startswith("--") and "c" in f[1:])
                    for f in _touch_flags
                )
                # AUDIT-FIX M1 (2026-08-25): collect per-file exceptions
                _touch_done, _touch_errs = [], []
                for _tf in _touch_files:
                    try:
                        _tp = _WPath(_tf)
                        if not _tp.is_absolute():
                            _tp = workspace / _tp
                        # S-SEC (2026-08-23): workspace lock check like mkdir.
                        if (werr := _inline_check_workspace(_tp, _workspace_lock, "run_bash")) is not None:
                            return werr
                        if _touch_no_create and not _tp.exists():
                            continue
                        _tp.parent.mkdir(parents=True, exist_ok=True)
                        _tp.touch(exist_ok=True)
                        _touch_done.append(str(_tf))
                    except Exception as _te:
                        _touch_errs.append(f"{_tf}: {_te}")
                if _touch_errs:
                    return "[touch error]: " + "; ".join(_touch_errs)
                return f"[exit 0 - touched {len(_touch_done)} file(s)]"

            _rm_match = _re_win.match(r'rm\s+(.*)', cmd.strip(), _re_win.DOTALL)
            if _rm_match and cmd.strip().startswith("rm ") and _HAS_SHELL_CHAIN:
                return ("[TOOL_ERROR: SHELL_CHAIN_UNSUPPORTED]\n"
                        "run_bash does not chain commands on Windows (found a "
                        "shell operator). Run one command per call.")
            if _rm_match and cmd.strip().startswith("rm "):
                _rm_raw = _rm_match.group(1).strip()
                _rm_flags, _rm_targets = _parse_shim_tokens_quoted(_split_shim_args_quoted(_rm_raw))
                if not _rm_targets:
                    return "[rm error]: no target path specified"
                _rm_force = any(
                    f == "--force"
                    or (f.startswith("-") and not f.startswith("--") and "f" in f[1:].lower())
                    for f in _rm_flags
                )
                _rm_recursive = any(
                    f == "--recursive"
                    or (f.startswith("-") and not f.startswith("--") and "r" in f[1:].lower())
                    for f in _rm_flags
                )
                _rm_dir = any(
                    f == "--dir"
                    or (f.startswith("-") and not f.startswith("--") and "d" in f[1:].lower())
                    for f in _rm_flags
                )
                try:
                    _ws_res = Path(workspace).resolve()
                except OSError:
                    _ws_res = Path(workspace)
                _lock_res = None
                if _workspace_lock is not None:
                    try:
                        _lock_res = Path(_workspace_lock).resolve()
                    except OSError:
                        _lock_res = Path(_workspace_lock)
                _rm_done, _rm_errs = [], []
                _concrete_targets: list[tuple[str, Path]] = []
                for _rt, _was_quoted in _rm_targets:
                    if not _rt.strip():
                        _rm_errs.append(f"{_rt!r}: refusing empty path")
                        continue
                    if not _was_quoted and glob.has_magic(_rt):
                        _rt_path = _WPath(_rt)
                        if ".." in _rt_path.parts:
                            _rm_errs.append(f"{_rt}: refusing '..' in glob pattern (RUN_BASH_BLOCKED)")
                            continue
                        if _rt_path.is_absolute():
                            _base_p = _rt_path
                            while glob.has_magic(str(_base_p)) and _base_p.parent != _base_p:
                                _base_p = _base_p.parent
                            _pat_p = _rt
                        else:
                            _non_magic_parts: list[str] = []
                            for _pt in _rt_path.parts:
                                if glob.has_magic(_pt):
                                    break
                                _non_magic_parts.append(_pt)
                            _base_p = workspace.joinpath(*_non_magic_parts) if _non_magic_parts else workspace
                            _magic_tail = _rt_path.parts[len(_non_magic_parts):]
                            _pat_p = str(_WPath(glob.escape(str(_base_p))).joinpath(*_magic_tail))
                        try:
                            _base_res = _base_p.resolve()
                        except OSError:
                            _base_res = _base_p
                        _checked_base = _inline_check_workspace(_base_res, _workspace_lock, "run_bash")
                        if _checked_base is not None:
                            _rm_errs.append(f"{_rt}: {_checked_base}")
                            continue
                        _matches = sorted(glob.glob(_pat_p, recursive="**" in _rt))
                        if not _matches:
                            if not _rm_force:
                                _rm_errs.append(f"{_rt}: no matching files")
                            continue
                        for _m_str in _matches:
                            _mp = _WPath(_m_str)
                            try:
                                _disp = str(_mp.resolve().relative_to(_ws_res))
                            except (ValueError, OSError):
                                _disp = _m_str
                            _concrete_targets.append((_disp, _mp))
                    else:
                        _rp = _WPath(_rt)
                        if not _rp.is_absolute():
                            _rp = workspace / _rp
                        _concrete_targets.append((_rt, _rp))

                def _rm_onexc(func, path, _exc):
                    try:
                        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                    except OSError:
                        pass
                    func(path)

                for _disp, _rp in _concrete_targets:
                    _is_junc = _is_junction(_rp)
                    if _is_junc:
                        try:
                            _junc_abs = Path(os.path.abspath(str(_rp)))
                        except OSError:
                            _junc_abs = _rp
                        if (
                            _junc_abs == _ws_res
                            or (_lock_res is not None and _junc_abs == _lock_res)
                            or _ws_res.is_relative_to(_junc_abs)
                            or (_lock_res is not None and _lock_res.is_relative_to(_junc_abs))
                        ):
                            _rm_errs.append(f"{_disp}: refusing to delete workspace root (RUN_BASH_BLOCKED)")
                            continue
                        if _is_protected_path(_junc_abs) or is_secret_path(_junc_abs, _ws_res):
                            _rm_errs.append(f"{_disp}: protected or secret path blocked (RUN_BASH_BLOCKED)")
                            continue
                        _checked_j = _inline_check_workspace(_rp.parent, _workspace_lock, "run_bash")
                        if _checked_j is not None:
                            _rm_errs.append(f"{_disp}: {_checked_j}")
                            continue
                        try:
                            os.rmdir(_rp)
                            _rm_done.append(_disp)
                        except OSError as _rme:
                            _rm_errs.append(f"{_disp}: {_rme}")
                        continue

                    _checked_pre = _inline_check_workspace(_rp, _workspace_lock, "run_bash")
                    if _checked_pre is not None:
                        _rm_errs.append(f"{_disp}: {_checked_pre}")
                        continue
                    try:
                        _rp_res = _rp.resolve()
                    except OSError:
                        _rp_res = _rp
                    # Guard against deleting the workspace root (e.g. `rm -r .` or resolved == root)
                    if (
                        _rp_res == _ws_res
                        or (_lock_res is not None and _rp_res == _lock_res)
                        or _ws_res.is_relative_to(_rp_res)
                        or (_lock_res is not None and _lock_res.is_relative_to(_rp_res))
                    ):
                        _rm_errs.append(f"{_disp}: refusing to delete workspace root (RUN_BASH_BLOCKED)")
                        continue
                    # Validate path is within workspace
                    _checked = _inline_check_workspace(_rp_res, _workspace_lock, "run_bash")
                    if _checked is not None:
                        _rm_errs.append(f"{_disp}: {_checked}")
                        continue
                    if not _rp.is_symlink() and not _rp_res.exists():
                        if not _rm_force:
                            _rm_errs.append(f"{_disp}: no such file or directory")
                        continue
                    try:
                        if _rp.is_symlink():
                            _rp.unlink()
                        elif _rp_res.is_dir():
                            if not (_rm_recursive or _rm_dir):
                                _rm_errs.append(f"{_disp}: is a directory (use -r)")
                                continue
                            if _rm_recursive:
                                if _rm_force:
                                    shutil.rmtree(_rp_res, onexc=_rm_onexc)
                                else:
                                    shutil.rmtree(_rp_res)
                            else:
                                os.rmdir(_rp_res)
                        else:
                            _rp_res.unlink()
                        _rm_done.append(_disp)
                    except Exception as _rme:
                        _rm_errs.append(f"{_disp}: {_rme}")
                if _rm_errs:
                    return "[rm error]: " + "; ".join(_rm_errs)
                if not _rm_done:
                    return "[exit 0 - nothing to remove]"
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
            # that works on both sides. Only genuine existing drive mounts are
            # rewritten so sed/grep/awk patterns (`/a/`, `/c/foo`) stay intact.
            _cmd_unix_fixed = re.sub(
                r'(?<![^\s=:\x27"])/([a-zA-Z])/(?=[A-Za-z0-9._~-]{2}|[A-Za-z0-9._~-]/[A-Za-z0-9._~-])',
                lambda _m: (_m.group(1).upper() + ":/") if _should_rewrite_posix_drive(cmd, _m) else _m.group(0),
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
                except OSError:
                    _cd_resolved = _pc
                try:
                    _ws_resolved = workspace.resolve()
                except OSError:
                    _ws_resolved = workspace
                if str(_cd_resolved).rstrip("\\/").lower() == str(_ws_resolved).rstrip("\\/").lower():
                    _cmd_translated = _cd_rest

            _and_parts = _split_unquoted_and(_cmd_translated)
            if len(_and_parts) > 1:
                _ps_seq = []
                for _ap in _and_parts[:-1]:
                    _ps_seq.append(_ap.strip())
                    _ps_seq.append(
                        "if (-not $? -or ($LASTEXITCODE -and $LASTEXITCODE -ne 0)) "
                        "{ exit $(if ($LASTEXITCODE) { $LASTEXITCODE } else { 1 }) }"
                    )
                _ps_seq.append(_and_parts[-1].strip())
                _cmd_translated = "; ".join(_ps_seq)

            if re.search(r"(?<![-\w])(curl|wget)(?=[\s.]|$)", _cmd_translated, re.IGNORECASE):
                _cmd_translated = (
                    "Remove-Item alias:curl,alias:wget -ErrorAction SilentlyContinue; "
                    + _cmd_translated
                )

            if _has_unquoted_token(_cmd_translated, "2>&1"):
                _cmd_translated = (
                    "$ErrorActionPreference='Continue'; $__ok=$true; "
                    + "& { " + _cmd_translated + " } 2>&1 | ForEach-Object { "
                    + "if ($_ -is [System.Management.Automation.ErrorRecord] "
                    + "-and $_.Exception -isnot [System.Management.Automation.RemoteException]) { $__ok=$false }; "
                    + "\"$_\" }; "
                    + "if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE } "
                    + "elseif (-not $__ok) { exit 1 } else { exit 0 }"
                )

        _cmd_l = cmd.lower()
        _is_build = any(k in _cmd_l for k in (
            "mvn ", "gradle", "npm install", "npm ci", "npm run build",
            "cargo build", "cargo test", "dotnet build", "dotnet restore",
            "pip install", "go build", "go test", "docker build",
            "docker compose", "docker-compose",
            "pytest", "py.test", "python -m pytest", "python3 -m pytest", "unittest"))
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
            _win_bash_exe = ""
            if sys.platform == "win32" and _run_bash_shell_mode() == "bash":
                _win_bash_exe = _windows_bash_path()
            if _win_bash_exe:
                # AUDIT FIX (1.3.3): the model's command runs in bash
                # verbatim - the powershell translation layer above is
                # skipped (POSIX syntax works natively there).
                # VENV-SHIMS (2026-11-19): uv venvs ship python.exe without
                # pip.exe or python3.exe; bare `pip`/`python3` fell through PATH
                # to global/broken Python installations.
                _bash_cmd = _rewrite_win_native_dev_null(
                    _rewrite_win_tmp_paths(_normalize_win_bash_venv_paths(cmd))
                )
                _stream_cmd = _build_win_shell_cmd(
                    _bash_cmd, mode="bash", win_bash=_win_bash_exe, orig_cmd=cmd
                )
            elif sys.platform == "win32":
                # OEM-Codepage (e.g. cp850) — bytes.decode() (UTF-8) corrupted non-ASCII
                _stream_cmd = _build_win_shell_cmd(
                    _cmd_translated, mode="powershell", orig_cmd=cmd
                )
            else:
                _stream_cmd = [_BASH_BIN, "-c", cmd]  # X6: not every distro ships /bin/bash
            # BENCH-CONTAINER (2026-11-10): external benchmarks can bind-mount
            # the workspace into the task image. Commands must then run in that
            # image (real deps/test runner), while file tools keep editing the
            # host workspace. No mapping -> unchanged host execution.
            try:
                from tools.bench_container import bash_argv as _bench_bash_argv
                _bench_cmd = _bench_bash_argv(cmd, workspace)
            except (ImportError, OSError, ValueError):
                _bench_cmd = None
            if _bench_cmd:
                _stream_cmd = _bench_cmd
                _bash_timeout = max(_bash_timeout, 1800)
            elif _is_unscoped_pip_install(cmd, workspace):
                return _tool_error_response(
                    "PIP_NO_WORKSPACE_VENV",
                    "pip install refused: no .venv found in the workspace. Installing into "
                    "the global system Python or HiveMind server venv is not allowed. "
                    "Create a project venv first: 'python -m venv .venv' (or 'uv venv --seed'), "
                    "then re-run 'pip install ...' — run_bash will automatically use .venv.",
                    tool="run_bash",
                    details={"cmd": str(cmd)[:400]},
                )
            stdout_raw, stderr_raw, returncode, timed_out = await _stream_proc(
                _stream_cmd, _bash_timeout, str(workspace),
                _tool_env_with_python(workspace),
            )
        finally:
            if _ka_task:
                _ka_task.cancel()

        if timed_out:
            # S-SEC (2026-08-23): tree kill now happens IN _stream_proc per
            # the result actively coaches there; server/watcher patterns
            _partial_out, _partial_err, _partial_block = _format_timeout_partial(
                stdout_raw, stderr_raw
            )
            _longrun = re.search(
                r"(http\.server|uvicorn|flask|gunicorn|npm\s+(run\s+)?(dev|start)|vite|next\s+dev|watch)",
                str(cmd).lower())
            _hint = (
                "\n\nThis looks like a LONG-RUNNING process (server/watcher). "
                "Do NOT retry it with run_bash — use the start_background tool instead, "
                "then check with get_background_output and stop with stop_background."
                if _longrun else
                "\n\nIf this is a long-running process (dev server, watcher, or interactive GUI/game main loop), "
                "do NOT re-run it directly in run_bash — use start_background for servers, or verify "
                "GUI/game modules with a non-blocking smoke test / headless script instead."
            )
            return _tool_error_response(
                "RUN_BASH_TIMEOUT",
                f"run_bash timed out after {_bash_timeout}s. Consider splitting the command."
                + _partial_block
                + _hint,
                tool="run_bash",
                details={
                    "cmd": str(cmd)[:400],
                    "timeout_s": int(_bash_timeout),
                    "stdout_tail": _partial_out[-800:] if _partial_out else "",
                    "stderr_tail": _partial_err[-600:] if _partial_err else "",
                })

        stdout = stdout_raw.strip("\r\n")
        stderr = stderr_raw.strip()
        # SHELL-NAME (2026-10-15): computed unconditionally - the NONZERO
        # error must name the shell that ran so a PS-syntax-into-bash
        # mismatch is diagnosable even when stdout is non-empty.
        _shell_name = ("bash (Git Bash)" if _win_bash_exe
                       else "powershell.exe (Windows)" if sys.platform == "win32"
                       else "bash")
        if _is_search_no_match(cmd, returncode, stdout, stderr):
            return "[run_bash: exit 1 — no matches found]"
        result = ""
        if stderr:
            result += f"[stderr]\n{stderr}\n---\n"
        result += stdout
        if returncode != 0:
            result += f"\n[exit code: {returncode}]"
        if returncode != 0 and not stdout.strip():
            result += f"\n[shell: {_shell_name}]"
        _shell_note = ""
        if returncode != 0 and _win_bash_exe \
                and "command not found" in (stderr or "").lower() \
                and re.search(r"(Test-Path|Get-ChildItem|Get-Content|Remove-Item|"
                              r"\$LASTEXITCODE|\$env:)", cmd):
            # the model emitted POWERSHELL syntax into the bash shell -
            # coach it instead of letting it loop on the same error
            _shell_note = ("\n[shell note: the shell is bash (Git Bash). Use POSIX syntax "
                           "- ls, cat, rm, cp, test -d; PowerShell cmdlets like "
                           "Get-ChildItem/Test-Path do not exist here.]")
            result += _shell_note

        _is_docker_detached = (
            returncode == 0 and not result.strip()
            and ("docker compose" in _cmd_l or "docker-compose" in _cmd_l)
            and " -d" in _cmd_l
        )
        if _is_docker_detached:
            result = (
                "[exit 0 - docker compose started detached. Containers running in background.]\n"
                "Check status: docker compose ps\n"
                "Check logs:   docker compose logs --tail=50"
            )

        if len(result) > 16000:
            _head = 4000
            _tail = 12000
            _dropped = len(result) - (_head + _tail)
            result = (
                result[:_head]
                + f"\n\n[OUTPUT TRUNCATED: hard cap 16000 chars — dropped {_dropped} middle chars]\n\n"
                + result[-_tail:]
            )

        if returncode != 0:
            if not result:
                _result_tail = "(no output)"
            elif len(result) <= 3400:
                _result_tail = result
            elif stderr and stdout.strip():
                _err_part = _tail_slice(stderr, 1400)
                _out_budget = max(600, 3400 - len(_err_part) - 60 - len(_shell_note))
                _out_part = _tail_slice(stdout, _out_budget)
                _result_tail = (
                    f"[stderr]\n{_err_part}\n---\n{_out_part}"
                    f"\n[exit code: {returncode}]{_shell_note}"
                )
            else:
                _result_tail = _tail_slice(result, 3400)
            _err = _tool_error_response(
                "RUN_BASH_NONZERO",
                f"run_bash exited with code {returncode} (shell: {_shell_name}).\n{_result_tail}",
                tool="run_bash" ,
                details={
                    "cmd": str(cmd)[:400],
                    "exit_code": int(returncode),
                    "stdout_tail": stdout[-2000:] if stdout else "",
                    "stderr_tail": stderr[-2000:] if stderr else "",
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
        _out_ok = f"[run_bash: exit 0]\n{result}" if result else "[run_bash: exit 0 — no output]"
        _pkgs_ok = _pip_packages_from_cmd(str(cmd))
        _low_ok = result.lower()
        if (
            _pkgs_ok
            and "|" in str(cmd)
            and "successfully installed" not in _low_ok
            and any(_p in result for _p in _PIP_SOURCE_BUILD_MARKERS)
            and any(_em in _low_ok for _em in ("error", "exit code:", "failed", "did not run successfully", "traceback"))
        ):
            _out_ok += _pip_source_build_hint(_pkgs_ok)
        return _out_ok
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

    # BENCH-CONTAINER: run_tests uses the host test runner, which is the wrong
    # environment for a task-container benchmark. Make the limitation explicit
    # instead of feeding the coder host-side import errors.
    try:
        from tools.bench_container import container_info as _bench_container_info
        if _bench_container_info(_workspace) is not None:
            return _tool_error_response(
                "RUN_TESTS_UNAVAILABLE",
                "run_tests is unavailable in a benchmark container workspace. "
                "Use run_bash with the repository's test command.",
                tool="run_tests")
    except (ImportError, OSError, ValueError):
        pass

    if _shared._run_test_suite is None:
        return _tool_error_response(
            "RUN_TESTS_UNAVAILABLE",
            "run_tests is unavailable (test runner not wired). Use run_bash with the test command instead.",
            tool="run_tests" )
    _ws_str = str(workspace_lock or _workspace or os.environ.get("HIVEMIND_WORKSPACE", "."))
    # STATIC-ONLY (2026-11-04): a run that only wrote static assets has no
    # testable code. Running the workspace suite here surfaced unrelated
    # pre-existing failures and sent small models into an edit/test loop.
    _needs_tests_rt = None
    _rt_written = []
    try:
        from core.duo.preflight import (
            needs_test_verification as _needs_tests_rt)
        from tools.runner import _files_written_in_run as _fw_ctx_rt
        _rt_written = [str(_p) for _p in (_fw_ctx_rt.get(None) or ())]
    except (ImportError, AttributeError, TypeError):
        _needs_tests_rt = None
    if not _rt_written:
        try:
            from tools.runner import (
                _current_run_id as _rt_rid_ctx,
                _files_written_by_run as _rt_registry)
            _rt_rid = str(_rt_rid_ctx.get(None) or "")
            if _rt_rid:
                _rt_written = [str(_p) for _p in
                               _rt_registry.get(_rt_rid, set())]
        except (ImportError, AttributeError, TypeError):
            pass
    if (_needs_tests_rt is not None and _rt_written
            and not _needs_tests_rt(_rt_written)):
        return ("[TEST-RESULT] ⏭ No testable code changed (static assets only) "
                "- test run skipped. Do not call run_tests again in this run.")
    try:
        _timeout = int(args.get("timeout") or 90)
    except (TypeError, ValueError):
        _timeout = 90
    _timeout = max(10, min(300, _timeout))
    _lang = str(args.get("lang_override") or "").strip() or None
    # SELECTIVE-TESTS (D4, 2026-11-09): run only the tests mapped to the
    # files this run wrote. An explicit target list wins; otherwise the
    # testmap maps written source/test paths and the full suite stays the
    # fallback when nothing maps. Opt out with duo_selective_tests=false.
    _scoped_paths: list[str] = []
    _ws_root = Path(_ws_str).resolve()
    try:
        _raw_targets = args.get("paths") or args.get("targets")
        if isinstance(_raw_targets, str):
            _raw_targets = [p for p in re.split(r"[,\s]+", _raw_targets) if p]
        if isinstance(_raw_targets, list) and _raw_targets:
            for _raw_p in _raw_targets[:40]:
                _p_str = str(_raw_p or "").strip()
                if not _p_str:
                    continue
                if _p_str.startswith("-"):
                    return _tool_error_response(
                        "INVALID_ARGUMENT",
                        f"Test target '{_p_str}' must not start with '-'.",
                        tool="run_tests",
                        details={"path": _p_str},
                    )
                _file_part, _sep, _node_id = _p_str.partition("::")
                if not _file_part:
                    return _tool_error_response(
                        "INVALID_ARGUMENT",
                        f"Test target '{_p_str}' has no file path before '::'.",
                        tool="run_tests",
                        details={"path": _p_str},
                    )
                try:
                    _cand = (_ws_root / _file_part).resolve()
                    _rel = _cand.relative_to(_ws_root).as_posix()
                except ValueError:
                    return _tool_error_response(
                        "PATH_OUTSIDE_WORKSPACE",
                        f"Test target '{_p_str}' resolves outside workspace '{_ws_root}'.",
                        tool="run_tests",
                        details={"path": _p_str, "workspace": str(_ws_root)},
                    )
                except (OSError, RuntimeError) as _tpe:
                    return _tool_error_response(
                        "INVALID_ARGUMENT",
                        f"Invalid test target '{_p_str}': {_tpe}",
                        tool="run_tests",
                        details={"path": _p_str},
                    )
                if _rel.startswith("-"):
                    return _tool_error_response(
                        "INVALID_ARGUMENT",
                        f"Test target '{_p_str}' must not start with '-'.",
                        tool="run_tests",
                        details={"path": _p_str},
                    )
                _scoped_paths.append((_rel or ".") + (_sep + _node_id if _sep else ""))
        else:
            try:
                from core.state import settings as _hs_settings
                _selective = bool(_hs_settings.get("duo_selective_tests", True))
            except (ImportError, AttributeError, TypeError):
                _selective = True
            if _selective and _rt_written:
                try:
                    from hive_functions.testmap import map_edits_to_tests
                    _scoped_paths = map_edits_to_tests(_ws_str, _rt_written)
                except (ImportError, OSError, ValueError, TypeError):
                    _scoped_paths = []
    except (AttributeError, TypeError, ValueError):
        _scoped_paths = []
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
            paths=(_scoped_paths or None),
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
        _tail = (_tr.raw_output or "").strip()[-400:]
        if not _tail and _tr.inject_msg and "0 test failure(s)" not in _tr.inject_msg:
            _tail = _tr.inject_msg[:400]
        return (
            f"[TEST-RESULT] ⚠️ No tests found or no test command available "
            f"({_tr.language}). Command: {_tr.command}"
            + (f"\n{_tail}" if _tail else "")
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
    # PLAYWRIGHT-PREINSTALLED (2026-11-05, live): the engine's browser tool
    # already ships Playwright + Chromium (installed by install.bat). Models
    # kept burning whole rounds on "pip install playwright" / "playwright
    # install chromium" (the latter also blows the run_bash timeout). Point
    # at the browser tool instead of running a doomed install.
    if "playwright" in packages.lower():
        return _tool_error_response(
            "INSTALL_PLAYWRIGHT_UNNECESSARY",
            "Playwright is already installed for this engine - the browser "
            "tool uses it directly (Chromium included). Do NOT install "
            "playwright or run 'playwright install'. To open or verify a "
            "local page, call the browser tool with action 'navigate'.",
            tool="install_package")
    # PIP-VENV-GUARD (2026-09-13): "python -m pip" resolves via the inherited
    # PATH — without a workspace venv it installs into HIVEMIND'S OWN venv and
    # can break the running server. Require a workspace-local interpreter.
    _ws_s = str(workspace or "")
    _venv_py = None
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
        if _run_bash_shell_mode() == "powershell":
            _q_py = "'" + str(_venv_py).replace("'", "''") + "'"
            _cmd = f"& {_q_py} -m pip install --prefer-binary {packages}"
        else:
            _cmd = f'"{_venv_py.as_posix()}" -m pip install --prefer-binary {packages}'
    else:
        _cmd = _INSTALL_CMDS[manager].format(pkgs=packages)
    if dev and manager == "npm":
        _cmd += " --save-dev"
    from tools.runner import (
        _in_approved_funnel as _ap_funnel_cv,
        _run_inline_tool as _dispatch_bash,
    )
    _ap_tok = _ap_funnel_cv.set(True)
    try:
        _res = await _dispatch_bash(
            "run_bash", {"cmd": _cmd}, workspace_lock=workspace_lock or str(workspace),
        )
    finally:
        _ap_funnel_cv.reset(_ap_tok)
    _relabel_map = (
        ("RUN_BASH_NONZERO", "INSTALL_FAILED"),
        ("RUN_BASH_EXEC_ERROR", "INSTALL_EXEC_ERROR"),
        ("RUN_BASH_TIMEOUT", "INSTALL_TIMEOUT"),
        ("RUN_BASH_BLOCKED", "INSTALL_BLOCKED"),
    )
    _res_s = _res.lstrip()
    _matched_relabel = next(
        (pair for pair in _relabel_map if _res_s.startswith(f"[TOOL_ERROR:{pair[0]}]")),
        None,
    )
    if _matched_relabel is not None:
        _old_tag, _code = _matched_relabel
        _body = _res_s[len(f"[TOOL_ERROR:{_old_tag}]"):]
        _prefix_note = f"install_package: '{manager} install {packages}'"
        for _pat, _rep in (
            ("run_bash: run_bash exited with code", f"{_prefix_note} failed, exit code"),
            ("run_bash exited with code", f"{_prefix_note} failed, exit code"),
            ("run_bash: run_bash execution error", f"{_prefix_note} execution error"),
            ("run_bash execution error", f"{_prefix_note} execution error"),
            ("run_bash: run_bash timed out", f"{_prefix_note} timed out"),
            ("run_bash timed out", f"{_prefix_note} timed out"),
            ("run_bash: Command blocked:", f"{_prefix_note} blocked:"),
            ("run_bash blocked", f"{_prefix_note} blocked"),
        ):
            if _pat in _body:
                _body = _body.replace(_pat, _rep, 1)
                break
        if _body.lstrip().startswith("run_bash:"):
            _body = " " + _prefix_note + ": " + _body.lstrip()[len("run_bash:"):].lstrip()
        _body = _body.replace('"tool": "run_bash"', '"tool": "install_package"')
        _body = re.sub(r'"code"\s*:\s*"' + re.escape(_old_tag) + r'"', f'"code": "{_code}"', _body)
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
    if (
        _venv_py is not None
        and any(_p in _res for _p in _PIP_SOURCE_BUILD_MARKERS)
        and "[HINT] pip fell back to building from SOURCE" not in _res
    ):
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
        _bg_cwd = str(_workspace) if _workspace else None
        res = await asyncio.to_thread(start_background, cmd, cwd=_bg_cwd)
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
    from tools.sandbox import ToolJob as _ToolJob, spawn_kwargs as _spawn_kwargs, kill_tree_and_reap as _kill_tree_and_reap
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        **_spawn_kwargs(),
    )
    _job = _ToolJob.confine(proc)
    LINE_CAP = line_cap
    MAX_LINE_BYTES = 65536

    class _HeadTailLineBuf:
        def __init__(self, cap: int) -> None:
            self.cap = max(1, int(cap))
            self.head_cap = min(60, self.cap // 4) if self.cap >= 4 else 0
            self.tail_cap = max(1, self.cap - self.head_cap)
            self.head: list[str] = []
            self.tail: deque[str] = deque(maxlen=self.tail_cap)
            self.total = 0

        def append(self, line: str) -> None:
            self.total += 1
            if len(self.head) < self.head_cap:
                self.head.append(line)
            else:
                self.tail.append(line)

        def render(self) -> str:
            if self.total <= self.cap:
                return "\n".join(self.head + list(self.tail))
            dropped = self.total - (len(self.head) + len(self.tail))
            banner = (
                f"[... {dropped} line(s) omitted of {self.total} total "
                f"(line cap {self.cap}) — narrow output with head/tail/sed -n/grep ...]"
            )
            return "\n".join(self.head + [banner] + list(self.tail))

    stdout_buf = _HeadTailLineBuf(LINE_CAP)
    stderr_buf = _HeadTailLineBuf(LINE_CAP)

    async def drain(stream, buf: _HeadTailLineBuf):
        if stream is None:
            return
        pending = bytearray()
        truncated = False
        try:
            while True:
                chunk = await stream.read(16384)
                if not chunk:
                    break
                start = 0
                while True:
                    nl = chunk.find(b"\n", start)
                    if nl == -1:
                        tail = chunk[start:]
                        if not truncated:
                            room = MAX_LINE_BYTES - len(pending)
                            if len(tail) > room:
                                pending.extend(tail[:room])
                                truncated = True
                            else:
                                pending.extend(tail)
                        break
                    seg = chunk[start:nl]
                    if not truncated:
                        room = MAX_LINE_BYTES - len(pending)
                        if len(seg) > room:
                            pending.extend(seg[:room])
                            truncated = True
                        else:
                            pending.extend(seg)
                    line_txt = pending.decode(errors="replace").rstrip("\r")
                    if truncated:
                        line_txt += " ...[line truncated]"
                    buf.append(line_txt)
                    pending.clear()
                    truncated = False
                    start = nl + 1
        finally:
            if pending:
                line_txt = pending.decode(errors="replace").rstrip("\r")
                if truncated:
                    line_txt += " ...[line truncated]"
                buf.append(line_txt)

    try:
        try:
            await asyncio.wait_for(
                asyncio.gather(drain(proc.stdout, stdout_buf), drain(proc.stderr, stderr_buf), proc.wait()),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            # nachgelagertem /IM-Massaker auf powershell.exe.
            if _job is not None:
                _job.terminate()
            try:
                await _kill_tree_and_reap(proc)
            except (OSError, RuntimeError, asyncio.CancelledError):
                try:
                    proc.kill()
                except OSError:
                    pass
            return stdout_buf.render(), stderr_buf.render(), -1, True
        except asyncio.CancelledError:
            if _job is not None:
                _job.terminate()
            try:
                await asyncio.shield(_kill_tree_and_reap(proc))
            except (OSError, RuntimeError, asyncio.CancelledError):
                try:
                    proc.kill()
                except OSError:
                    pass
            raise

        return stdout_buf.render(), stderr_buf.render(), proc.returncode or 0, False
    finally:
        _tr = getattr(proc, "_transport", None)
        if _tr is not None:
            _tr.close()
        if _job is not None:
            _job.close()


_DANGEROUS_RM_ROOT_RE = re.compile(
    r"\brm\s+(?:[^|&;\n]*\s)?(?:-[a-z]*[rR][a-z]*|--recursive)\s+"
    r"(?:(?:-[a-z]+|--\S*)\s+)*(?:[^|&;\n)\s]+\s+)*"
    r"""['"]?(?:/+(?:\*|['"]?(?:[\s;&|)]|$))|/[a-z]/(?:\*|['"]?(?:[\s;&|)]|$))|~(?:/+(?:\*|['"]?(?:[\s;&|)]|$))|['"]?(?:[\s;&|)]|$))|\.\.?(?:/+(?:\*|['"]?(?:[\s;&|)]|$))|['"]?(?:[\s;&|)]|$))|[a-z]:[/\\](?:\*|['"]?(?:[\s;&|)]|$)))""",
    re.IGNORECASE,
)
_DANGEROUS_CHMOD_ROOT_RE = re.compile(
    r"\bchmod\s+(?:(?:-[a-z]*R[a-z]*|--recursive)\s+)*0?777\s+/(?:\*|[\s;&|)]|$)",
    re.IGNORECASE,
)
_WIN_DANGEROUS_FORMAT_RE = re.compile(
    r"(?:^|[;&|(\n])\s*(?:cmd(?:\.exe)?\s+/[ck]\s+)?(?:[a-z]:[/\\][^\s;|&]*[/\\])?format(?:\.com|\.exe)?\s+(?:(?:/\S+\s+)*[a-z]:|\S+\s+[a-z]:)",
    re.IGNORECASE,
)
_WIN_DANGEROUS_DISKPART_RE = re.compile(
    r"(?:^|[;&|(\n])\s*(?:cmd(?:\.exe)?\s+/[ck]\s+)?(?:[a-z]:[/\\][^\s;|&]*[/\\])?diskpart(?:\.exe)?(?:\s|$|[;&|)])",
    re.IGNORECASE,
)


def _bash_blocklisted(cmd: str) -> str | None:
    _c = (cmd or "").strip().lower()
    pats = _DANGEROUS_PATTERNS + (_WIN_DANGEROUS_PATTERNS if sys.platform == "win32" else [])
    for _d in pats:
        if _d in _c:
            return _d
    if _DANGEROUS_RM_ROOT_RE.search(_c):
        return "rm -r <root/home/cwd>"
    if _DANGEROUS_CHMOD_ROOT_RE.search(_c):
        return "chmod 777 /"
    if sys.platform == "win32":
        if _WIN_DANGEROUS_FORMAT_RE.search(_c):
            return "format <drive>:"
        if _WIN_DANGEROUS_DISKPART_RE.search(_c):
            return "diskpart"
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
