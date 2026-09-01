


import logging
import subprocess
import threading
import time
import uuid

logger = logging.getLogger("hivemind.background")

_MAX_OUTPUT_CHARS = 20000
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
        for _line in proc.stdout:
            if not _line:
                continue
            with _LOCK:
                _entry = _REGISTRY.get(handle)
                if _entry is None:
                    return
                _entry["buf"] = (_entry["buf"] + _line)[-_MAX_OUTPUT_CHARS:]
    except Exception:
        pass


def start_background(cmd: str) -> dict:
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
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_spawn_kwargs(),
        )
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


def list_background() -> list[dict]:
    with _LOCK:
        out = []
        for h, e in _REGISTRY.items():
            running = e["proc"].poll() is None
            out.append({"handle": h, "pid": e["proc"].pid, "running": running, "cmd": e["cmd"][:120]})
    return out

