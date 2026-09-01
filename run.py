"""
run.py  Hivemind Launcher
=========================
Setzt PYTHONPATH und sys.path VOR uvicorn-Start.
os.environ wird von multiprocessing.spawn-Children vererbt (sys.path nicht).

Author: Luzo (BoredLuzo) — https://github.com/BoredLuzo
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

_LOG_FMT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FMT,
)

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
                    "pre_explore.py", "prompts.py", "soul_engine.py", "skill_distiller.py"]
    for f in _required_hf:
        if not os.path.exists(os.path.join(_hf_dir, f)):
            _missing.append(f"hive_functions/{f}")

if _missing:
    print("[ERROR] Missing files:")
    for f in _missing:
        print(f"  {f}")
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
