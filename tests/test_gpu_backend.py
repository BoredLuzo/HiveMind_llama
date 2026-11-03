# -*- coding: utf-8 -*-
"""P0 (Linux audit 2026-11-03): ROCm must not silently become Vulkan."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


code = ("import os; os.environ['HIVEMIND_GPU_BACKEND']='rocm'; "
        "import backend.llama_config as lc; print(lc.GPU_BACKEND)")
env = {**os.environ}
env.pop("HIVEMIND_GPU_BACKEND", None)
out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={
    **env, "HIVEMIND_GPU_BACKEND": "rocm"},
    capture_output=True, text=True, timeout=60)
check("1 the rocm backend is accepted from the environment",
      out.stdout.strip() == "rocm", out.stdout.strip() or out.stderr[-120:])

check("2 the backend tag helper maps rocm (source)",
      '"rocm" if GPU_BACKEND == "rocm"' in
      (ROOT / "backend" / "llama_config.py").read_text(encoding="utf-8"))

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
