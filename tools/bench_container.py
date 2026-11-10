# -*- coding: utf-8 -*-
"""Opt-in routing for benchmark containers.

A benchmark run can bind-mount its workspace into a task container.  The
HiveMind engine still owns the workspace, but commands must execute inside
that container so the real project dependencies and test runner are used.

The mapping is deliberately a small JSON file, not request state: the engine
can serve several workspaces and the file is written by the external harness
before each run.  No file -> no routing; normal HiveMind behaviour is
unchanged.

Mapping shape::

    {
      "/abs/host/workspace": {
        "container": "hm-<instance>",
        "cwd": "/testbed",
        "activate": "source /opt/miniconda3/bin/activate testbed 2>/dev/null || true; "
      }
    }
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def mapping_path() -> Path:
    return Path(os.environ.get("HIVEMIND_BENCH_CONTAINERS_FILE",
                               "/tmp/hive_bench_containers.json"))


def _load() -> dict:
    p = mapping_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def container_info(workspace: str | Path) -> dict | None:
    """Return the container mapping for this exact workspace, if any."""
    data = _load()
    if not data:
        return None
    candidates = [str(workspace)]
    try:
        candidates.insert(0, str(Path(workspace).resolve()))
    except OSError:
        pass
    for key in candidates:
        info = data.get(key)
        if isinstance(info, dict) and info.get("container"):
            return info
    return None


def _rewrite_paths(cmd: str, workspace: str | Path, cwd: str) -> str:
    for prefix in (str(Path(workspace).resolve()), str(workspace)):
        if prefix:
            cmd = cmd.replace(prefix, cwd)
    return cmd


def bash_argv(cmd: str, workspace: str | Path) -> list[str] | None:
    """Docker-exec argv for run_bash, or None for normal host execution."""
    info = container_info(workspace)
    if info is None:
        return None
    cwd = str(info.get("cwd") or "/testbed")
    body = _rewrite_paths(cmd, workspace, cwd)
    activate = str(info.get("activate") or "").strip()
    if activate:
        body = activate.rstrip() + " " + body
    return ["docker", "exec", "-w", cwd, str(info["container"]),
            "bash", "-lc", body]


def python_argv(workspace: str | Path) -> list[str] | None:
    """Docker-exec argv for a stdin-fed Python snippet, or None."""
    info = container_info(workspace)
    if info is None:
        return None
    cwd = str(info.get("cwd") or "/testbed")
    activate = str(info.get("activate") or "").strip()
    prefix = (activate.rstrip() + " ") if activate else ""
    return ["docker", "exec", "-i", "-w", cwd, str(info["container"]),
            "bash", "-lc", prefix + "python3 -"]
