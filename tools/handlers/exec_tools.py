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

_PKG_RE = re.compile(r"[A-Za-z0-9@/._~+!<>=,\- ]+")

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
        from tools.sandbox import ToolJob as _ToolJob, spawn_kwargs as _spawn_kwargs, kill_tree as _kill_tree
        r = await asyncio.create_subprocess_exec(
            _py_cmd,
            fname,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
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
        if result:
            return result[:4000]
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
            _cd_mkdir = _re_win.match(
                r'cd\s+("([^"]+)"|(\S+))\s+&&\s+mkdir\s+-p\s+(.*)',
                cmd.strip(), _re_win.DOTALL
            )
            if _cd_mkdir and not _HAS_SEMICOLON:
                _base = _WPath(_cd_mkdir.group(2) or _cd_mkdir.group(3))
                _dirs_raw = _cd_mkdir.group(4).strip()
                _dir_parts = _re_win.findall(r'"([^"]+)"|(\S+)', _dirs_raw)
                _dirs = [_base / (a or b) for a, b in _dir_parts]
                return _do_mkdir(_dirs)

            _mkdir_p_match = _re_win.match(r'mkdir\s+-p\s+(.*)', cmd.strip(), _re_win.DOTALL)
            if _mkdir_p_match and not _HAS_SEMICOLON:
                _raw_paths = _mkdir_p_match.group(1).strip()
                _path_parts = _re_win.findall(r'"([^"]+)"|(\S+)', _raw_paths)
                _dirs = [_WPath(a or b) for a, b in _path_parts]
                return _do_mkdir(_dirs)

            _mkdir_plain = _re_win.match(r'mkdir\s+(.*)', cmd.strip(), _re_win.DOTALL)
            if _mkdir_plain and not _HAS_SEMICOLON:
                _raw_paths = _mkdir_plain.group(1).strip()
                _path_parts = _re_win.findall(r'"([^"]+)"|(\S+)', _raw_paths)
                _dirs = [_WPath(a or b) for a, b in _path_parts]
                return _do_mkdir(_dirs)

            _touch_match = _re_win.match(r'touch\s+(.*)', cmd.strip())
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
                    _checked = _inline_check_workspace(_rp, _workspace_lock)
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

            _cmd_unix_fixed = re.sub(
                r'(?<![\w])/([a-zA-Z])/',
                lambda _m: _m.group(1).upper() + ":\\\\",
                cmd)
            if _cmd_unix_fixed != cmd:
                cmd = _cmd_unix_fixed

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

            if sys.platform == "win32" and re.search(r"(?<![-\w])(curl|wget)(?=[\s.]|$)", _cmd_translated, re.IGNORECASE):
                _cmd_translated = (
                    "Remove-Item alias:curl,alias:wget -ErrorAction SilentlyContinue; "
                    + _cmd_translated
                )

            if "2>&1" in _cmd_translated and sys.platform == "win32":
                _cmd_translated = (
                    "$ErrorActionPreference='SilentlyContinue'; "
                    + "& { " + _cmd_translated + " } 2>&1 | ForEach-Object { \"$_\" }; "
                    + "if ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 0 }"
                )

        _cmd_l = cmd.lower()
        _bl_hit2 = _bash_blocklisted(cmd)
        if _bl_hit2 is not None:
            return _tool_error_response(
                "RUN_BASH_BLOCKED",
                f"Command blocked: dangerous pattern '{_bl_hit2}' detected. Use safer alternatives.",
                tool="run_bash" , details={"cmd": cmd[:400]})
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
            _stream_cmd = (
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _ps_cmd]
                if sys.platform == "win32"
                else ["/bin/bash", "-c", cmd]
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
        result = ""
        if stderr:
            result += f"[stderr]\n{stderr}\n---\n"
        result += stdout
        if returncode != 0:
            result += f"\n[exit code: {returncode}]"
        if returncode != 0 and not stdout:
            _shell_name = "powershell.exe (Windows)" if sys.platform == "win32" else "bash"
            result += f"\n[shell: {_shell_name}]"

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
            return _tool_error_response(
                "RUN_BASH_NONZERO",
                f"run_bash exited with code {returncode}.\n{_result_tail}",
                tool="run_bash" ,
                details={
                    "cmd": str(cmd)[:400],
                    "exit_code": int(returncode),
                    "stdout_tail": stdout[-1200:] if stdout else "",
                    "stderr_tail": stderr[-1200:] if stderr else "",
                })
        return f"[run_bash: exit 0]\n{result}" if result else "[run_bash: exit 0 — no output]"
    except Exception as e:
        return _tool_error_response(
            "RUN_BASH_EXEC_ERROR",
            f"run_bash execution error: {e}",
            tool="run_bash" ,
            details={"cmd": str(cmd)[:400]})


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
        return f"[TEST-RESULT] ✅ All tests passed ({_tr.language}). Command: {_tr.command}"
    if _tr.failure_count == 0:
        return (
            f"[TEST-RESULT] ⚠️ No tests found or no test command available "
            f"({_tr.language}). Command: {_tr.command}\n{(_tr.inject_msg or '')[:400]}"
        )
    return _tr.inject_msg or "[TEST-RESULT] No result from test runner."


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
    _cmd = _INSTALL_CMDS[manager].format(pkgs=packages)
    if dev and manager == "npm":
        _cmd += " --save-dev"
    from tools.runner import _run_inline_tool as _dispatch_bash
    return await _dispatch_bash("run_bash", {"cmd": _cmd}, workspace_lock=workspace_lock)


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


def _stage_split(content: str, limit: int) -> tuple[str, int]:

    cut = content.rfind("\n", 0, limit)
    if cut <= 0:
        cut = min(limit, len(content))
    else:
        cut += 1
    return content[:cut], cut


def _ps_quote_path(cmd: str, raw_path: str) -> str:


    q = "'" + raw_path.replace("'", "''") + "'"
    return cmd.replace(raw_path, q, 1)
