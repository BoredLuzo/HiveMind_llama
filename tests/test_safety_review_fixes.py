# -*- coding: utf-8 -*-
"""Two safety fixes from the external review (2026-11-02).

1. `force_kill_all` used to sweep by IMAGE NAME (`taskkill /IM
   llama-server.exe`, POSIX `pkill -f llama-server`) — that also kills LM
   Studio, a second HiveMind install or a manual llama-server. It now kills
   the PIDs this manager started; the name sweep needs an explicit
   `include_foreign=True` (route: `/vram/kill_all?all=true`).
2. Linux live VRAM was nvidia-smi only. AMD/Intel expose it in sysfs
   (`/sys/class/drm/card*/device/mem_info_vram_{total,used}`), which needs no
   driver tool.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HEALTH = (ROOT / "backend" / "manager_health.py").read_text(encoding="utf-8")
COMPAT = (ROOT / "backend" / "llama_compat.py").read_text(encoding="utf-8")
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
VRAM = (ROOT / "backend" / "llama_vram_table.py").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── 1. PID-scoped kill ─────────────────────────────────────────────────────
check("1a kill_all takes the explicit flag",
      "async def force_kill_all(self, include_foreign: bool = False)" in HEALTH)
_fn_start = HEALTH.index("async def force_kill_all")
_BODY = HEALTH[_fn_start:_fn_start + 4000]          # the method body
_i_pid = _BODY.index('killed.append(f"pid:{_pid}")')
_i_sweep = _BODY.index('"/IM", "llama-server.exe"')
check("1b our own PIDs are killed first", _i_pid < _i_sweep)
check("1c the name sweep only runs behind the flag",
      "if include_foreign:" in _BODY and
      _BODY.index("if include_foreign:") < _i_sweep)
check("1d POSIX kills by PID, not by pkill",
      'subprocess.run(["kill", "-9", str(_pid)]' in HEALTH)
check("1e the compat layer forwards the flag",
      "force_kill_all(include_foreign=include_foreign)" in COMPAT)
check("1f the route exposes ?all=true",
      "async def vram_kill_all_ep(all: bool = False)" in SERVER and
      "_fka(include_foreign=bool(all))" in SERVER)

# ── 2. AMD/Intel VRAM via sysfs ────────────────────────────────────────────
_vstart = VRAM.index("def get_live_gpu_free_mib")
_VFN = VRAM[_vstart:_vstart + 2600]
check("2a sysfs is read before the nvidia-smi call",
      _VFN.index("mem_info_vram_total") < _VFN.index('_shutil.which("nvidia-smi")'))
check("2b both files are used", "mem_info_vram_used" in VRAM)
check("2c the result is converted to MiB",
      "_sysfs_total - _sysfs_used) / (1024 * 1024)" in VRAM)
check("2d an unreadable sysfs falls through to nvidia-smi",
      "no sysfs (container) -> try nvidia-smi" in VRAM)

# ── 3. the probe still works on this host ──────────────────────────────────
from backend.llama_vram_table import get_live_gpu_free_mib  # noqa: E402
_val = get_live_gpu_free_mib()
check("3a the live probe answers", _val is None or _val >= 0.0, f"({_val})")

# ── 4. unlimited runs are bounded, shutdown is PID-scoped ────────────────
SET = (ROOT / "settings.py").read_text(encoding="utf-8")
RUN = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
check("4a until_finished has a wall-clock bound in settings",
      "duo_until_finished_max_hours" in SET)
check("4b the 24 h literal is gone",
      "86400.0 if ctx.duo_config.until_finished" not in RUN)
check("4c the deadline reads the setting",
      'duo_until_finished_max_hours", 24' in RUN)
check("4d the default allows a long run (24 h)", "duo_until_finished_max_hours\": 24" in SET)
_sh_start = HEALTH.index("async def shutdown(")
_sh = HEALTH[_sh_start:_sh_start + 2600]          # the shutdown method only
check("4d shutdown no longer sweeps by image name",
      '"/IM", "llama-server.exe"' not in _sh)
check("4e shutdown kills tracked PIDs instead",
      'taskkill", "/F", "/T", "/PID"' in HEALTH)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
