"""
run.py  Hivemind Launcher
=========================
Setzt PYTHONPATH und sys.path VOR uvicorn-Start.
os.environ wird von multiprocessing.spawn-Children vererbt (sys.path nicht).
"""
from __future__ import annotations
import sys
import os
import warnings
import socket
import json
import urllib.request
import urllib.error
import logging
from logging.handlers import RotatingFileHandler

# ── DEATH FORENSICS (2026-10-14, task 5): the engine died several times
# without any traceback (last: mid-coder-call, log ended on a console ^C).
# This block makes every exit path VISIBLE:
#   - faulthandler dumps threads on native crashes AND on SIGTERM/SIGBREAK
#   - signal handlers log which signal arrived
#   - atexit logs a normal-exit marker
#   - a heartbeat file (logs/engine_heartbeat.json) timestamps liveness
# Diagnosis rule: process gone + NO atexit/signal line = hard TerminateProcess
# from outside; signal line = console event; faulthandler dump = native crash.
_LOG_FMT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"

_LOG_DIR_EARLY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_LOG_DIR_EARLY, exist_ok=True)
_DEATH_LOG_PATH = os.path.join(_LOG_DIR_EARLY, "engine_death.log")
_faulthandler_stream = None
try:
    import faulthandler as _fh
    import signal as _signal
    import time as _time_mod
    _T0 = _time_mod.time()
    _faulthandler_stream = open(_DEATH_LOG_PATH, "a", encoding="utf-8")
    _fh.enable(file=_faulthandler_stream)
    # WINDOWS-FIX (2026-10-17, live): faulthandler.register() is POSIX-only.
    # On Windows it raised AttributeError, the except below swallowed it and
    # the WHOLE forensics block (heartbeat, signal handlers, atexit marker)
    # never ran - every start logged "death forensics unavailable". Guard the
    # POSIX-only call instead of losing the feature.
    if hasattr(_fh, "register"):
        _fh.register(_faulthandler_stream)

    def _log_death_signal(signum, frame):
        try:
            with open(_DEATH_LOG_PATH, "a", encoding="utf-8") as _f:
                _f.write(f"{_time_mod.strftime('%Y-%m-%d %H:%M:%S')} SIGNAL signum={signum}\n")
        except Exception:
            pass
        # restore default so the process still terminates as expected
        _signal.signal(signum, _signal.SIG_DFL)
        _signal.raise_signal(signum)

    for _sig_name in ("SIGTERM", "SIGBREAK", "SIGABRT", "SIGINT"):
        _sig = getattr(_signal, _sig_name, None)
        if _sig is not None:
            try:
                _signal.signal(_sig, _log_death_signal)
            except (ValueError, OSError):
                pass

    import atexit as _atexit

    @_atexit.register
    def _death_forensics_atexit():
        try:
            with open(_DEATH_LOG_PATH, "a", encoding="utf-8") as _f:
                _f.write(f"{_time_mod.strftime('%Y-%m-%d %H:%M:%S')} ATEXIT normal exit "
                         f"uptime={_time_mod.time() - _T0:.0f}s\n")
        except Exception:
            pass

    def _heartbeat_loop():
        import json as _json
        import threading as _threading
        import time as _t2

        def _loop():
            while True:
                try:
                    with open(os.path.join(_LOG_DIR_EARLY, "engine_heartbeat.json"),
                              "w", encoding="utf-8") as _f:
                        _json.dump({"pid": os.getpid(),
                                    "ts": _t2.strftime("%Y-%m-%d %H:%M:%S"),
                                    "uptime_s": round(_t2.time() - _T0)}, _f)
                except Exception:
                    pass
                _t2.sleep(10)

        _t = _threading.Thread(target=_loop, daemon=True, name="engine-heartbeat")
        _t.start()

    _heartbeat_loop()
except Exception as _forensics_err:
    print(f"[WARN] death forensics unavailable: {_forensics_err}")

# CONSOLE-KILL (2026-09-10, Windows): bind the whole process tree (python +
# all spawned llama-server children) to a Job object with kill-on-close.
# Closing the console window then reliably terminates the entire stack
# instead of leaving orphaned llama-servers on the GPU. Linux/uv-managed
# setups use systemd which already reaps children.
# NOTE (2026-10-14 forensics): this job also makes the process die with the
# console on ^C — engine_wmi.log ended on a console ^C mid-request. When
# starting the engine detached (pythonw/WMI), console events cannot reach it.
if sys.platform == "win32":
    _win32job = None
    try:
        import win32api
        import win32job as _win32job
    except ImportError as _pw_exc:
        # PYWIN32-HINT (2026-10-18): two different failures look identical here.
        #   a) the package is not installed at all -> install it with UV (a uv
        #      venv has no pip, so "-m pip install" answers "No module named pip")
        #   b) the package IS installed but its DLLs are not registered ->
        #      pywin32_postinstall fixes that.
        # The hint names both, so nobody is sent to a command that cannot work.
        _pw_have_pkg = False
        try:
            import importlib.util as _ilu
            _pw_have_pkg = _ilu.find_spec("win32api") is not None
        except (ImportError, ValueError):
            _pw_have_pkg = False
        print(f"[WARN] pywin32 not usable ({type(_pw_exc).__name__}: {_pw_exc})")
        print("       console-close will not kill llama-servers; everything else runs.")
        print("       Fix (uv venv - there is no pip inside):")
        print("         uv pip install --python .venv\\Scripts\\python.exe pywin32")
        if _pw_have_pkg:
            print("       The package IS installed - then a file is blocked or the")
            print("       bootstrap did not run. Reinstall it and check the")
            print("       antivirus log:")
            print("         uv pip install --python .venv\\Scripts\\python.exe "
                  "--reinstall pywin32")
        print("       Or simply re-run install.bat (it re-syncs the environment).")
    if _win32job is not None:
        try:
            _hjob = _win32job.CreateJobObject(None, "")
            _info = _win32job.QueryInformationJobObject(_hjob, _win32job.JobObjectExtendedLimitInformation)
            _info["BasicLimitInformation"]["LimitFlags"] = _win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _win32job.SetInformationJobObject(_hjob, _win32job.JobObjectExtendedLimitInformation, _info)
            _win32job.AssignProcessToJobObject(_hjob, win32api.GetCurrentProcess())
        except _win32job.error as _job_err:
            # best-effort: startup_cleanup still kills leftovers by port scan
            print(f"[WARN] Job-object console-kill unavailable: {_job_err}")

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FMT,
)

class _GatewayPollFilter(logging.Filter):
    """LOG-HYGIENE (2026-10-10): the gateway polls the engine every few
    seconds; httpx logged every poll as INFO and reached ~30% of the
    engine log. LLM POST lines stay visible."""
    _QUIET = ("/gateway/heartbeat", "/gateway/poll", "/gateway/status",
              "/run/journal", "/approval/pending", "/settings")

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(q in msg for q in self._QUIET)


logging.getLogger("httpx").addFilter(_GatewayPollFilter())
# the uvicorn access log prints every INCOMING request server-side -
# the same poll flood (owner paste, 1-2 lines per poll cycle)
logging.getLogger("uvicorn.access").addFilter(_GatewayPollFilter())

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)
os.environ.setdefault("PYTHONWARNINGS", "ignore::DeprecationWarning")

THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Wachstum (10 MB je Datei, 3 Backups). [COMPRESSION]/[PLAN-INJECT]/[PASS-FILES]
_LOG_DIR = os.path.join(THIS_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
try:
    _file_handler = RotatingFileHandler(
        os.path.join(_LOG_DIR, "hivemind.log"),
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=3,               # 3 Backups -> max ~40 MB
        encoding="utf-8",
    )
    _file_handler.setLevel(logging.INFO)            # identisch zur Console
    _file_handler.setFormatter(logging.Formatter(_LOG_FMT))
    logging.getLogger().addHandler(_file_handler)
except Exception as _log_err:
    print(f"[WARN] FileHandler logs/hivemind.log could not be activated: {_log_err}")

if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

existing = os.environ.get("PYTHONPATH", "")
if THIS_DIR not in existing:
    os.environ["PYTHONPATH"] = THIS_DIR + os.pathsep + existing if existing else THIS_DIR

os.chdir(THIS_DIR)

_missing = [f for f in [
    "server.py", "backend/__init__.py", "settings.py",
    "backend/llama_server_manager.py", "backend/llama_config.py", "backend/llama_models.py",
] if not os.path.exists(os.path.join(THIS_DIR, f))]

_hf_dir = os.path.join(THIS_DIR, "hive_functions")
if not os.path.isdir(_hf_dir):
    _missing.append("hive_functions/")
else:
    _required_hf = ["__init__.py", "planner.py", "pipeline.py", "memory.py",
                    "pre_explore/__init__.py", "prompts.py", "soul_engine.py",
                    "skill_distiller.py"]
    for f in _required_hf:
        if not os.path.exists(os.path.join(_hf_dir, f)):
            _missing.append(f"hive_functions/{f}")

if _missing:
    print("[ERROR] Missing files:")
    for f in _missing:
        print(f"  {f}")
    # TTY-GUARD (2026-09-19): detached/container starts have no TTY — the
    # input() would die with EOFError and mask the actual error listing.
    if sys.stdin.isatty():
        input("\nEnter druecken zum Beenden...")
    sys.exit(1)

import asyncio
import uvicorn


def _is_port_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        try:
            return s.connect_ex((host, port)) == 0
        except OSError:
            return False


def _is_hivemind_alive(host: str, port: int) -> bool:
    probes = [
        (f"http://{host}:{port}/automap/current", "automap"),
        (f"http://{host}:{port}/memory", "memory"),
        (f"http://{host}:{port}/settings", "settings"),
    ]
    for url, mode in probes:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status != 200:
                    continue
                body = (r.read() or b"").decode("utf-8", errors="replace")
                if mode == "automap":
                    try:
                        data = json.loads(body)
                        if isinstance(data, dict) and (
                            "active" in data or "active_preset" in data or "profile" in data
                        ):
                            return True
                    except Exception:
                        pass
                elif mode == "memory":
                    try:
                        data = json.loads(body)
                        if isinstance(data, dict) and (
                            "used_gb" in data or "vram" in data or "entries" in data
                        ):
                            return True
                    except Exception:
                        pass
                elif mode == "settings" and body.strip().startswith("{"):
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            continue
    return False


def _find_free_port(host: str, start_port: int, max_steps: int = 20) -> int | None:
    for p in range(start_port, start_port + max_steps + 1):
        if not _is_port_busy(host, p):
            return p
    return None

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    reload_enabled = os.environ.get("HIVEMIND_RELOAD", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }
    bind_host = os.environ.get("HIVEMIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    # Port-Aufloesung: HIVEMIND_PORT env > settings.json "server_port" > 8001.
    _env_port = os.environ.get("HIVEMIND_PORT", "").strip()
    base_port = int(_env_port) if _env_port.isdigit() else 0
    if not base_port:
        try:
            with open(os.path.join(THIS_DIR, "settings.json"), encoding="utf-8") as _f:
                _cfg = json.load(_f)
            base_port = int(_cfg.get("server_port") or 0)
        except Exception:
            base_port = 0
    if not base_port:
        base_port = 8001

    if _is_port_busy(bind_host, base_port):
        if _is_hivemind_alive(bind_host, base_port):
            print(f"[INFO] Hivemind is already running on http://{bind_host}:{base_port} — no second start needed.")
            sys.exit(0)
        alt_port = _find_free_port(bind_host, base_port + 1, max_steps=30)
        if alt_port is None:
            print(
                f"[ERROR] Port {base_port} is in use and no free fallback port was found.\n"
                "Set HIVEMIND_PORT to a free port, e.g. 8011."
            )
            sys.exit(1)
        print(f"[WARN] Port {base_port} is in use - starting on http://{bind_host}:{alt_port}")
        base_port = alt_port

    uvicorn.run(
        "server:app",
        host=bind_host,
        port=base_port,
        loop="auto",
        reload=reload_enabled,
        reload_dirs=[THIS_DIR] if reload_enabled else None,
    )
