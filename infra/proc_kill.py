# -*- coding: utf-8 -*-
"""Shared async subprocess tree termination (stdlib-only)."""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys


async def kill_tree(proc, *, force: bool = False, to_thread=None) -> None:
    """Best-effort async kill of the whole child process tree without blocking the loop."""
    pid = getattr(proc, "pid", None)
    if not pid:
        return
    try:
        if sys.platform == "win32":
            _runner = to_thread or asyncio.to_thread
            await _runner(
                subprocess.run,
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            sig = signal.SIGKILL if force else signal.SIGTERM
            os.killpg(os.getpgid(pid), sig)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        pass
