


import logging
from pathlib import Path
import subprocess
import threading
import time
import uuid

logger = logging.getLogger("hivemind.background")

_MAX_OUTPUT_CHARS = 20000
_READ_CHUNK_CHARS = 4096
_MAX_PROCESSES = 8

_REGISTRY: dict[str, dict] = {}
_LOCK = threading.Lock()


def _terminate(entry: dict):
    # SANDBOX: job terminate first (tree, race-free); legacy kill as fallback.
    _job = entry.get("job")
    if _job is not None:
        try:
            _job.terminate()
        except Exception:
            pass
        finally:
            # never took effect (handle stayed open).
            try:
                _job.close()
            except Exception:
                pass
    try:
        proc = entry.get("proc")
        if proc is not None and proc.poll() is None:
            from tools.sandbox import kill_tree as _kill_tree
            _kill_tree(proc)
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    except Exception:
        pass


def _append_bg_line(handle: str, line: str) -> bool:
    with _LOCK:
        entry = _REGISTRY.get(handle)
        if entry is None:
            return False
        entry["buf"] = (entry["buf"] + line)[-_MAX_OUTPUT_CHARS:]
        return True


def _reader(handle: str, proc: subprocess.Popen):
    try:
        stream = proc.stdout
        if stream is None:
            return
        if callable(getattr(stream, "readline", None)):
            while True:
                _line = stream.readline(_READ_CHUNK_CHARS)
                if not _line:
                    break
                if not _append_bg_line(handle, _line):
                    return
        else:
            for _line in stream:
                if _line and not _append_bg_line(handle, _line):
                    return
    except Exception:
        pass


def start_background(cmd: str, cwd: str | Path | None = None) -> dict:
    cmd = (cmd or "").strip()
    if not cmd:
        return {"ok": False, "error": "start_background requires a non-empty 'cmd'."}

    import os as _os_bg
    import tools.handlers.exec_tools as _et_bg
    _bg_bash = getattr(_et_bg, "_BASH_BIN", "/bin/sh")
    _win_bash = ""
    _win_ps = False
    if _os_bg.name == "nt":
        _mode_fn = getattr(_et_bg, "_run_bash_shell_mode", None)
        _path_fn = getattr(_et_bg, "_windows_bash_path", None)
        if callable(_mode_fn) and callable(_path_fn):
            _mode_val = _mode_fn()
            if _mode_val == "bash":
                _win_bash = _path_fn() or ""
            elif _mode_val == "powershell":
                _win_ps = True

    _bl_fn = getattr(_et_bg, "_bash_blocklisted", None)
    if callable(_bl_fn):
        _bl_hit = _bl_fn(cmd)
        if _bl_hit:
            return {"ok": False, "error": f"Command blocked: dangerous pattern '{_bl_hit}' detected."}

    _bg_cmd = cmd
    if _win_bash:
        _norm_venv_fn = getattr(_et_bg, "_normalize_win_bash_venv_paths", None)
        _rewrite_drv_fn = getattr(_et_bg, "_rewrite_posix_drive_paths", None)
        _rewrite_tmp_fn = getattr(_et_bg, "_rewrite_win_tmp_paths", None)
        _rewrite_null_fn = getattr(_et_bg, "_rewrite_win_native_dev_null", None)
        _bg_cmd = _norm_venv_fn(cmd) if callable(_norm_venv_fn) else cmd
        if callable(_rewrite_drv_fn):
            _bg_cmd = _rewrite_drv_fn(_bg_cmd)
        if callable(_rewrite_tmp_fn):
            _bg_cmd = _rewrite_tmp_fn(_bg_cmd)
        if callable(_rewrite_null_fn):
            _bg_cmd = _rewrite_null_fn(_bg_cmd)
        if callable(_bl_fn):
            _bl_hit = _bl_fn(_bg_cmd)
            if _bl_hit:
                return {"ok": False, "error": f"Command blocked: dangerous pattern '{_bl_hit}' detected."}

    evicted = None
    with _LOCK:
        if len(_REGISTRY) >= _MAX_PROCESSES:
            _exited = [
                h for h, e in _REGISTRY.items()
                if e.get("proc") is None or e["proc"].poll() is not None
            ]
            _oldest_handle = min(_exited or _REGISTRY, key=lambda h: _REGISTRY[h]["started"])
            _oldest_entry = _REGISTRY.pop(_oldest_handle)
        else:
            _oldest_entry = None
    if _oldest_entry:
        _terminate(_oldest_entry)
        evicted = {
            "handle": _oldest_handle,
            "pid": (_oldest_entry.get("proc").pid
                    if _oldest_entry.get("proc") else None),
            "cmd": str(_oldest_entry.get("cmd", ""))[:120],
        }

    try:
        from tools.sandbox import spawn_kwargs as _spawn_kwargs
        # POSIX-SHELL (2026-09-19): shell=True runs under /bin/sh (dash) on
        # Ubuntu where bashisms fail; on Windows match run_bash (Git Bash when
        # available, fallback shell=True otherwise).
        from infra.child_env import tool_env as _tool_env
        try:
            _bg_env = _tool_env(workspace=cwd) if cwd is not None else _tool_env()
        except TypeError:
            _bg_env = _tool_env()

        _build_win_cmd = getattr(_et_bg, "_build_win_shell_cmd", None)
        _popen_kw = {
            "shell": False,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": _bg_env,
            **_spawn_kwargs(),
        }
        if cwd is not None:
            _popen_kw["cwd"] = str(cwd)

        if _win_bash and callable(_build_win_cmd):
            _argv = _build_win_cmd(_bg_cmd, mode="bash", win_bash=_win_bash, orig_cmd=cmd)
            proc = subprocess.Popen(_argv, **_popen_kw)
        elif _win_ps and callable(_build_win_cmd):
            _argv = _build_win_cmd(cmd, mode="powershell", orig_cmd=cmd)
            proc = subprocess.Popen(_argv, **_popen_kw)
        else:
            _popen_kw["shell"] = True
            if _os_bg.name != "nt":
                _popen_kw["executable"] = _bg_bash
            proc = subprocess.Popen(cmd, **_popen_kw)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}

    from tools.sandbox import ToolJob as _ToolJob
    _job = _ToolJob.confine(proc)

    handle = uuid.uuid4().hex[:12]
    entry = {"proc": proc, "buf": "", "started": time.time(), "cmd": cmd, "job": _job}
    with _LOCK:
        _REGISTRY[handle] = entry
    threading.Thread(target=_reader, args=(handle, proc), daemon=True).start()
    logger.info("[BACKGROUND] started %s pid=%d cmd=%s", handle, proc.pid, cmd[:120])
    result = {"ok": True, "handle": handle, "pid": proc.pid}
    if evicted:
        result["evicted"] = evicted
    return result


def get_background_output(handle: str) -> str:
    with _LOCK:
        entry = _REGISTRY.get(handle)
    if entry is None:
        return f"[background: no process '{handle}' — stopped or never started]"
    proc = entry["proc"]
    running = proc.poll() is None
    status = "running" if running else f"exited (code {proc.returncode})"
    with _LOCK:
        buf = entry["buf"]
    return f"[background {handle} {status}]\n{buf or '(no output yet)'}"


def stop_background(handle: str) -> bool:
    with _LOCK:
        entry = _REGISTRY.pop(handle, None)
    if entry is None:
        return False
    _terminate(entry)
    logger.info("[BACKGROUND] stopped %s", handle)
    return True


def stop_all_background() -> int:
    """Terminate all active background processes on shutdown."""
    with _LOCK:
        entries = list(_REGISTRY.values())
        _REGISTRY.clear()
    stopped = 0
    for entry in entries:
        try:
            _terminate(entry)
            stopped += 1
        except (OSError, RuntimeError, ValueError) as exc:
            logger.debug("[BACKGROUND] error stopping entry: %s", exc)
    if stopped:
        logger.info("[BACKGROUND] stopped %d background process(es)", stopped)
    return stopped


def list_background() -> list[dict]:
    with _LOCK:
        out = []
        for h, e in _REGISTRY.items():
            running = e["proc"].poll() is None
            out.append({"handle": h, "pid": e["proc"].pid, "running": running, "cmd": e["cmd"][:120]})
    return out

