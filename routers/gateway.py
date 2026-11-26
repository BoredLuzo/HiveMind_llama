"""Telegram gateway supervisor API-Router."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Gateway"])

# -- Gateway supervisor (P8-lite, 2026-10-05) -------------------------------
# The running Telegram gateway POSTs a heartbeat every 5 s (mirror loop);
# GET /gateway/status exposes liveness to the UI, POST /gateway/start
# spawns `sys.executable -m hivemind_gateway.main` DETACHED with
# HIVEMIND_GATEWAY_ENABLED=1 (the user clicked - that IS the deliberate
# master switch), POST /gateway/stop kills the heartbeat pid after a
# freshness + process-name check (recycled-pid guard). Loopback +
# Host-allowlist trust only, same as every other engine endpoint.

@router.post("/gateway/heartbeat", include_in_schema=False)
async def gateway_heartbeat(req: Request):
    from infra import gateway_supervisor as _sup
    if not _sup.is_loopback_request(req):
        return JSONResponse({"ok": False, "error": "loopback client required"},
                            status_code=403)
    try:
        body = await req.json()
    except (ValueError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    _pid = _sup.parse_positive_int(body.get("pid"))
    if _pid <= 0:
        return JSONResponse({"ok": False, "error": "invalid pid"}, status_code=400)
    _raw_data = body.get("data")
    _data = _raw_data if isinstance(_raw_data, dict) else {}
    if _sup.parse_birth(_data.get("birth")) is None:
        return JSONResponse({"ok": False, "error": "invalid birth"}, status_code=400)
    _sup.note_heartbeat(_pid, _data)
    return {"ok": True}


@router.get("/gateway/status", include_in_schema=False)
async def gateway_status():
    from infra import gateway_supervisor as _sup
    return _sup.status()


_gateway_start_lock: asyncio.Lock | None = None


@router.post("/gateway/start", include_in_schema=False)
async def gateway_start(req: Request):
    import server as _srv
    os = _srv.os
    asyncio = _srv.asyncio
    _THIS_DIR = _srv._THIS_DIR
    global _gateway_start_lock
    from infra import gateway_supervisor as _sup
    if not _sup.is_loopback_request(req):
        return JSONResponse({"ok": False, "error": "loopback client required"},
                            status_code=403)
    # RE-AUDIT R2 (2026-10-06): the liveness check runs a PowerShell child
    # (~0.3-0.6 s). Serialized + re-checked under the lock, or a UI
    # double-click spawns TWO gateways (Telegram getUpdates conflict).
    if _gateway_start_lock is None:
        _gateway_start_lock = asyncio.Lock()
    async with _gateway_start_lock:
        # START-RACE FIX (2026-10-06, live): "running" is heartbeat-freshness —
        # after an UNCLEAN gateway kill the stale heartbeat still read as
        # running for up to ~15 s and the start route answered 409. Verify the
        # recorded pid is really alive (python + birth match) before refusing;
        # a dead/recycled pid clears the stale status and starts.
        _st = _sup.status()
        if _st.get("running"):
            _pid = _sup.parse_positive_int(_st.get("pid"))
            _birth = _sup.parse_birth(_st.get("birth"))
            _alive = False
            if _pid > 0 and _birth is not None and os.name != "nt":
                # X4 (audit r2): posix liveness via /proc (no PowerShell).
                import subprocess as _sp_l
                try:
                    _chk = await asyncio.to_thread(
                        _sp_l.run,
                        ["sh", "-c",
                         f"test -d /proc/{_pid} && "
                         f"grep -q python /proc/{_pid}/comm && echo py || echo no"],
                        capture_output=True, text=True, timeout=20)
                    _alive = _chk.stdout.strip() == "py"
                except _sp_l.TimeoutExpired:
                    _alive = False
            elif _pid > 0 and _birth is not None:
                _ps = (
                    "$p = Get-Process -Id " + str(_pid) + " -ErrorAction SilentlyContinue; "
                    "if (-not $p) { 'no' }"
                    " elseif ($p.ProcessName -notmatch 'python') { 'recycled' }"
                    " elseif (" + str(_birth) + " -eq 0) { 'py' }"
                    " else {"
                    "  $b = [DateTimeOffset]::FromFileTime(" + str(_birth) + "); "
                    "  if ([Math]::Abs(($p.StartTime.ToUniversalTime() - $b.UtcDateTime).TotalSeconds) -gt 1.0)"
                    "    { 'recycled' } else { 'py' }"
                    " }"
                )
                import subprocess as _sp_start
                # TIMEOUT (audit 2026-10-30): this probe ran unbounded - a
                # hung PowerShell (profile load, AV scan) blocked the settings
                # endpoint forever. 20 s matches the other PowerShell probe.
                try:
                    _chk = await asyncio.to_thread(
                        _sp_start.run,
                        ["powershell", "-NoProfile", "-Command", _ps],
                        capture_output=True, text=True, timeout=20)
                    _alive = _chk.stdout.strip() == "py"
                except _sp_start.TimeoutExpired:
                    _alive = False
            if _alive:
                return JSONResponse({"ok": False,
                                     "error": "gateway already running"},
                                    status_code=409)
            _sup.clear()
        import subprocess
        import sys as _sys
        _logs = Path(_THIS_DIR) / "logs"
        try:
            _logs.mkdir(exist_ok=True)
            _out = open(_logs / "gateway_supervised.log", "ab")
        except OSError as _e:
            return JSONResponse({"ok": False, "error": f"log open failed: {_e}"},
                                status_code=500)
        _env = dict(os.environ)
        _env["HIVEMIND_GATEWAY_ENABLED"] = "1"
        _flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) \
            if os.name == "nt" else 0
        subprocess.Popen([_sys.executable, "-m", "hivemind_gateway.main"],
                         cwd=str(_THIS_DIR), env=_env,
                         stdout=_out, stderr=subprocess.STDOUT,
                         creationflags=_flags)
        _out.close()
        return {"ok": True,
                "hint": "status flips to running within one heartbeat (~5 s)"}


@router.post("/gateway/stop", include_in_schema=False)
async def gateway_stop(req: Request):
    import subprocess
    import server as _srv
    os = _srv.os
    asyncio = _srv.asyncio
    from infra import gateway_supervisor as _sup
    if not _sup.is_loopback_request(req):
        return JSONResponse({"ok": False, "error": "loopback client required"},
                            status_code=403)
    st = _sup.status()
    pid = _sup.parse_positive_int(st.get("pid"))
    birth = _sup.parse_birth(st.get("birth"))
    if not st.get("running") or pid <= 0:
        return JSONResponse({"ok": False, "error": "gateway not running"},
                            status_code=409)
    if birth is None:
        _sup.clear()
        return JSONResponse(
            {"ok": False, "error": "invalid heartbeat birth - status reset"},
            status_code=400)
    # recycled-pid guard (deep audit 2026-10-05): the heartbeat is <=15 s
    # old, but the pid could have been reused by ANY process in that
    # window. Verify process name AND the process birth (FILETIME from
    # the heartbeat) before killing - a mismatch resets the status
    # instead of killing an innocent process.
    if os.name != "nt":
        # X1 (audit r2): the PowerShell birth-check does not exist on
        # Linux (and FILETIME is meaningless against /proc jiffies) -
        # posix verifies name+birth via /proc and kills with os.kill.
        try:
            _stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            _comm = _stat.rsplit(")", 1)[0].split("(", 1)[1]
            _birth_ticks = int(_stat.rsplit(")", 1)[1].split()[19])
        except (OSError, ValueError, IndexError):
            _sup.clear()
            return JSONResponse(
                {"ok": False, "error": "gateway process not found - status reset"},
                status_code=409)
        if "python" not in _comm:
            _sup.clear()
            return JSONResponse(
                {"ok": False, "error": "pid was recycled by another process - refusing, status reset"},
                status_code=409)
        if birth and abs(birth - _birth_ticks) > 5:
            _sup.clear()
            return JSONResponse(
                {"ok": False, "error": "pid was recycled by another process - refusing, status reset"},
                status_code=409)
        import signal as _sig
        try:
            os.kill(pid, _sig.SIGTERM)
        except OSError as _ke:
            return JSONResponse({"ok": False, "error": f"kill failed: {_ke}"},
                                status_code=500)
        _sup.clear()
        return {"ok": True}
    _ps = (
        "$p = Get-Process -Id " + str(pid) + " -ErrorAction SilentlyContinue; "
        "if (-not $p) { 'no' }"
        " elseif ($p.ProcessName -notmatch 'python') { 'no' }"
        " elseif (" + str(birth) + " -eq 0) { 'py' }"
        " else {"
        "  $birth = [DateTimeOffset]::FromFileTime(" + str(birth) + "); "
        "  if ([Math]::Abs(($p.StartTime.ToUniversalTime() - $birth.UtcDateTime).TotalSeconds) -gt 1.0)"
        "    { 'recycled' } else { 'py' }"
        " }"
    )
    try:
        _chk = await asyncio.to_thread(
            subprocess.run, ["powershell", "-NoProfile", "-Command", _ps],
            capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return JSONResponse(
            {"ok": False, "error": "gateway liveness probe timed out"},
            status_code=504)
    _verdict = _chk.stdout.strip()
    if _verdict not in ("py", "recycled", "no"):
        return JSONResponse(
            {"ok": False,
             "error": f"ps failed rc={_chk.returncode} "
                      f"out={_chk.stdout[:60]!r} err={_chk.stderr[:100]!r} "
                      f"pid={pid} birth={birth}"},
            status_code=500)
    if _verdict == "recycled":
        _sup.clear()
        return JSONResponse(
            {"ok": False,
             "error": "pid was recycled by another process - refusing, status reset"},
            status_code=409)
    if _verdict != "py":
        _sup.clear()
        return JSONResponse(
            {"ok": False, "error": "gateway process not found - status reset"},
            status_code=409)
    try:
        await asyncio.to_thread(subprocess.run,
                                 ["taskkill", "/F", "/T", "/PID", str(pid)],
                                 capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        return JSONResponse(
            {"ok": False, "error": "gateway kill timed out"},
            status_code=504)
    _sup.clear()
    return {"ok": True}

