"""
HiveMind Windows Service Installer (via NSSM)

NSSM (Non-Sucking Service Manager) wraps any executable as a Windows service
with automatic restart on crash. More reliable than pywin32 for long-running
Python processes.

Prerequisites:
  - NSSM: https://nssm.cc/download (place nssm.exe in PATH or this directory)
  - Python installation with HiveMind dependencies

Usage:
  python hivemind_windows.py install
  python hivemind_windows.py uninstall
  python hivemind_windows.py start
  python hivemind_windows.py stop
  python hivemind_windows.py status
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path

SERVICE_NAME = "HiveMind"
HIVEMIND_DIR = Path(__file__).resolve().parent.parent
RUN_SCRIPT = HIVEMIND_DIR / "run.py"
LOG_DIR = Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "HiveMind" / "Logs"


def _find_nssm():
    nssm = shutil.which("nssm")
    if nssm:
        return nssm
    local = Path(__file__).parent / "nssm.exe"
    if local.exists():
        return str(local)
    return None


def _run_nssm(*args):
    nssm = _find_nssm()
    if not nssm:
        print("Error: nssm.exe not found.")
        print("Download from https://nssm.cc/download and place in PATH or deploy/ directory.")
        sys.exit(1)
    cmd = [nssm] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 and result.stderr.strip():
        print(f"NSSM error: {result.stderr.strip()}")
    return result


def install():
    python_exe = sys.executable
    if not RUN_SCRIPT.exists():
        print(f"Error: {RUN_SCRIPT} not found.")
        sys.exit(1)

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Installing {SERVICE_NAME} service...")
    print(f"  Python:    {python_exe}")
    print(f"  Script:    {RUN_SCRIPT}")
    print(f"  WorkDir:   {HIVEMIND_DIR}")
    print(f"  Logs:      {LOG_DIR}")

    _run_nssm("install", SERVICE_NAME, python_exe, str(RUN_SCRIPT))
    _run_nssm("set", SERVICE_NAME, "AppDirectory", str(HIVEMIND_DIR))
    _run_nssm("set", SERVICE_NAME, "AppStdout", str(LOG_DIR / "hivemind.log"))
    _run_nssm("set", SERVICE_NAME, "AppStderr", str(LOG_DIR / "hivemind_err.log"))
    _run_nssm("set", SERVICE_NAME, "AppStdoutCreationDisposition", "4")
    _run_nssm("set", SERVICE_NAME, "AppStderrCreationDisposition", "4")
    _run_nssm("set", SERVICE_NAME, "AppRestartDelay", "10000")
    _run_nssm("set", SERVICE_NAME, "AppExit", "Default", "Restart")
    _run_nssm("set", SERVICE_NAME, "AppEnvironmentExtra",
              "PYTHONUNBUFFERED=1")
    _run_nssm("set", SERVICE_NAME, "Description",
              "HiveMind AI Coding Assistant - Local Multi-Agent Pipeline")
    _run_nssm("set", SERVICE_NAME, "Start", "SERVICE_AUTO_START")

    print(f"\n{SERVICE_NAME} service installed successfully.")
    print(f"\nCommands:")
    print(f"  sc start {SERVICE_NAME}")
    print(f"  sc stop {SERVICE_NAME}")
    print(f"  sc query {SERVICE_NAME}")
    print(f"  nssm restart {SERVICE_NAME}")


def uninstall():
    print(f"Removing {SERVICE_NAME} service...")
    result = _run_nssm("remove", SERVICE_NAME, "confirm")
    if result.returncode == 0:
        print("Service removed.")
    else:
        print("Service removal failed (may not be installed).")


def start():
    _run_nssm("start", SERVICE_NAME)
    print(f"{SERVICE_NAME} started.")


def stop():
    _run_nssm("stop", SERVICE_NAME)
    print(f"{SERVICE_NAME} stopped.")


def status():
    result = _run_nssm("status", SERVICE_NAME)
    print(result.stdout.strip() if result.stdout.strip() else "Service not found.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python hivemind_windows.py [install|uninstall|start|stop|status]")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    commands = {"install": install, "uninstall": uninstall,
                "start": start, "stop": stop, "status": status}

    if cmd not in commands:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(commands.keys())}")
        sys.exit(1)

    commands[cmd]()
