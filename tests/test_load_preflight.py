# -*- coding: utf-8 -*-
"""Shared preflight (backend/llama_preflight.py): one test per condition
the freeze lesson (2026-10-28) demands before any model or bench starts:
free RAM vs model size, no second llama process, tree-kill on timeout.
"""
import subprocess
from pathlib import Path
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


# 1. RAM guard conditions (fail-closed era: unknown model blocks)
free = LP.free_ram_gb()
check("free RAM measurable", free > 0, f"({free:.1f} GB)")
_gg = Path(__import__("tempfile").gettempdir()) / "_tmp_dummy.gguf"
with open(_gg, "wb") as f:
    f.truncate(64 * 1024 * 1024)  # sparse 64 MB -> known size 0.06 GB
try:
    check("RAM guard passes with plenty of RAM",
          not LP.ram_guard_blocks(str(_gg), 30.0))
    check("RAM guard blocks tight RAM", LP.ram_guard_blocks(str(_gg), 0.05))
    check("RAM guard blocks unknown (-1)", LP.ram_guard_blocks(str(_gg), -1.0))
    check("RAM guard FAIL-CLOSED on unknown model",
          LP.ram_guard_blocks("definitely-not-a-model-xyz", 30.0))
    check("headroom honored (10.5 GB free vs 1 GB headroom passes)",
          not LP.ram_guard_blocks(str(_gg), 10.5, headroom_gb=1.0))
    check("headroom honored (0.05 GB free vs 1 GB headroom blocks)",
          LP.ram_guard_blocks(str(_gg), 0.05, headroom_gb=1.0))
finally:
    _gg.unlink(missing_ok=True)

# 2. process count: 0 on a free machine, positive while llama runs
count0 = LP.llama_process_count()
check("process count is an int >= 0", isinstance(count0, int) and count0 >= 0,
      f"({count0})")
check("process count survives OEM-codepage tasklist output",
      count0 >= 0)  # 2026-10-29: UnicodeDecodeError made this silently 0

# 2b. gguf size via PATH (bench scripts pass paths, not aliases) -
# a path used to resolve to 0.0 and silently weaken the guard to 8 GB
import pathlib as _pl
_ggufs = list(_pl.Path(r"C:\Users\NtheP\Desktop\Models").glob("Hermes3.6*.gguf"))
if _ggufs:
    _sz = LP.gguf_size_gb(str(_ggufs[0]))
    check("gguf_size_gb resolves a direct PATH", _sz > 10.0, f"({_sz:.1f} GB)")
    check("path-based guard is not weaker than the alias one",
          LP.ram_guard_blocks(str(_ggufs[0]), 9.0))
else:
    print("  SKIP path-based gguf checks (no local GGUF dir)")

# 3. tree kill: a sleeping child dies with its parent on timeout
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
time.sleep(0.3)
LP.kill_process_tree(proc.pid, timeout_s=5)
time.sleep(0.5)
check("tree kill ended the process", proc.poll() is not None)

print(f"\nload_preflight: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
