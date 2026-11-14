# -*- coding: utf-8 -*-
"""Benchmark container routing: opt-in, exact workspace, no mapping = host."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools.bench_container import bash_argv, container_info, python_argv
from tests._harness import Harness

h = Harness(style="plain", pad=True)
check = h.check

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    root = Path(td)
    ws = root / "ws"
    ws.mkdir()
    mapping = root / "containers.json"
    os.environ["HIVEMIND_BENCH_CONTAINERS_FILE"] = str(mapping)

    check("no mapping file -> host execution", bash_argv("pytest", ws) is None)
    check("no mapping file -> no python route", python_argv(ws) is None)

    mapping.write_text(json.dumps({
        str(ws.resolve()): {
            "container": "hm-demo",
            "cwd": "/testbed",
            "activate": "source /opt/miniconda3/bin/activate testbed 2>/dev/null || true; ",
        }
    }), encoding="utf-8")

    info = container_info(ws)
    check("mapping is found by resolved workspace",
          info is not None and info["container"] == "hm-demo")
    check("other workspace stays on host",
          bash_argv("pytest", root / "other") is None)

    argv = bash_argv(f"cd {ws.resolve()} && pytest -q", ws)
    check("bash is routed through docker exec",
          argv is not None and argv[:5] == ["docker", "exec", "-w", "/testbed", "hm-demo"],
          f" ({argv})")
    check("host workspace path is rewritten to /testbed",
          argv is not None and "/testbed" in argv[-1]
          and str(ws.resolve()) not in argv[-1], f" ({argv})")
    check("container shell activates the testbed env",
          argv is not None and "activate testbed" in argv[-1]
          and "pytest -q" in argv[-1], f" ({argv})")

    py = python_argv(ws)
    check("python is routed through stdin-fed docker exec",
          py is not None and py[:8] == ["docker", "exec", "-i", "-w", "/testbed", "hm-demo", "bash", "-lc"]
          and py[-1].endswith("python3 -"), f" ({py})")

    mapping.write_text(json.dumps({str(ws): {"container": ""}}), encoding="utf-8")
    check("empty container is ignored", bash_argv("pytest", ws) is None)

    mapping.write_text(json.dumps({
        str(ws.resolve()): {"container": "hm-stale", "expires_at": 1.0}
    }), encoding="utf-8")
    check("expired container mapping is ignored", bash_argv("pytest", ws) is None)

sys.exit(h.finish("bench_container"))
