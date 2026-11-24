


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
                with _LOCK:
                    _entry = _REGISTRY.get(handle)
                    if _entry is None:
                        return
                    _entry["buf"] = (_entry["buf"] + _line)[-_MAX_OUTPUT_CHARS:]
        else:
            for _line in stream:
                if not _line:
                    continue
                with _LOCK:
                    _entry = _REGISTRY.get(handle)
                    if _entry is None:
                        return
                    _entry["buf"] = (_entry["buf"] + _line)[-_MAX_OUTPUT_CHARS:]
    except Exception:
        pass


def start_background(cmd: str, cwd: str | Path | None = None) -> dict:
    cmd = (cmd or "").strip()
    if not cmd:
        return {"ok": False, "error": "start_background requires a non-empty 'cmd'."}
    evicted = None
    with _LOCK:
        if len(_REGISTRY) >= _MAX_PROCESSES:
            _oldest_handle = min(_REGISTRY, key=lambda h: _REGISTRY[h]["started"])
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
        # POSIX-SHELL (2026-09-19): shell=True läuft auf Ubuntu unter /bin/sh
        # (dash) — Bash-isms ([[ ]], source, &>) vom Modell sterben dort.
        # Explizit bash via 'executable'; Windows-cmd bleibt unverändert.
        import os as _os_bg
        # X6 (audit r3): bash-less distros - resolve once via exec_tools
        from tools.handlers.exec_tools import _BASH_BIN as _bg_bash
        _shell_kwargs = {"executable": _bg_bash} if _os_bg.name != "nt" else {}
        from infra.child_env import tool_env as _tool_env
        try:
            _bg_env = _tool_env(workspace=cwd) if cwd is not None else _tool_env()
        except TypeError:
            _bg_env = _tool_env()
        _popen_kw = {
            "shell": True,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": _bg_env,
            **_spawn_kwargs(),
            **_shell_kwargs,
        }
        if cwd is not None:
            _popen_kw["cwd"] = str(cwd)
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

