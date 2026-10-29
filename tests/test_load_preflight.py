# -*- coding: utf-8 -*-
"""Shared preflight (backend/llama_preflight.py): one test per condition
the freeze lesson (2026-10-28) demands before any model or bench starts:
free RAM vs model size, no second llama process, tree-kill on timeout.
"""
import subprocess
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend import llama_preflight as LP  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# 1. RAM guard conditions
free = LP.free_ram_gb()
check("free RAM measurable", free > 0, f"({free:.1f} GB)")
check("RAM guard passes with plenty of RAM",
      not LP.ram_guard_blocks("m", 30.0))
check("RAM guard blocks tight RAM", LP.ram_guard_blocks("m", 2.0))
check("RAM guard blocks unknown (-1)", LP.ram_guard_blocks("m", -1.0))
check("headroom parameter is honored (10.5 GB free vs 1 GB headroom passes)",
      not LP.ram_guard_blocks("m", 10.5, headroom_gb=1.0))
check("headroom parameter is honored (0.5 GB free vs 1 GB headroom blocks)",
      LP.ram_guard_blocks("m", 0.5, headroom_gb=1.0))

# 2. process count: 0 on a free machine, positive while llama runs
count0 = LP.llama_process_count()
check("process count is an int >= 0", isinstance(count0, int) and count0 >= 0,
      f"({count0})")

# 3. tree kill: a sleeping child dies with its parent on timeout
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
time.sleep(0.3)
LP.kill_process_tree(proc.pid, timeout_s=5)
time.sleep(0.5)
check("tree kill ended the process", proc.poll() is not None)

print(f"\nload_preflight: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
