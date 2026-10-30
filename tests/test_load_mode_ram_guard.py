# -*- coding: utf-8 -*-
"""Load-mode decision: unit tests for _resolve_load_mode + the RAM guard.

Covers the RAM-GUARD behaviour the live audit demanded (2026-10-28):
  - the guard applies to the profile override AND to the no-override
    default (both paths previously chose none without a check);
  - the fallback is plain mmap (NOT mlock: locking 18 GB with tight RAM
    pages-and-sinks itself, and it is the slow 2.92 t/s path);
  - unknown free RAM (-1) blocks none as well;
  - spec/mlock intents keep the historical mmap+mlock path.
"""
import sys

sys.path.insert(0, str(Path := __import__("pathlib").Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.manager_load import (  # noqa: E402
    _free_ram_gb, _resolve_load_mode)

# fail-closed era (2026-10-30): an unknown model blocks the guard, so every
# none-path test needs a model with a KNOWN size - a sparse dummy gguf.
import pathlib as _pl
_gg = _pl.Path(__file__).parent / "_tmp_dummy_guard.gguf"
with open(_gg, "wb") as _f:
    _f.truncate(3 * 1024 * 1024 * 1024)  # sparse 3 GB -> gguf_size 3.0
_M = str(_gg)

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# spec intent keeps the historical path regardless of RAM
check("1 spec_planned -> mmap+mlock (crash guard)",
      _resolve_load_mode(_M, spec_planned=True, mlock_now=False,
                         profile_lm="none", free_ram_gb=30.0) == "mmap+mlock")
check("2 mlock intent -> mmap+mlock",
      _resolve_load_mode(_M, spec_planned=False, mlock_now=True,
                         profile_lm="none", free_ram_gb=30.0) == "mmap+mlock")

# the RAM guard on the explicit profile override
check("3 override none + enough RAM -> none",
      _resolve_load_mode(_M, spec_planned=False, mlock_now=False,
                         profile_lm="none", free_ram_gb=30.0) == "none")
check("4 override none + tight RAM -> mmap (guard)",
      _resolve_load_mode(_M, spec_planned=False, mlock_now=False,
                         profile_lm="none", free_ram_gb=5.0) == "mmap")
check("5 override none + unknown RAM -> mmap (guard)",
      _resolve_load_mode(_M, spec_planned=False, mlock_now=False,
                         profile_lm="none", free_ram_gb=-1.0) == "mmap")

# the no-override default path carries the same guard
check("6 default none + tight RAM -> mmap (guard)",
      _resolve_load_mode(_M, spec_planned=False, mlock_now=False,
                         profile_lm="", free_ram_gb=5.0) == "mmap")
check("7 default none + enough RAM -> none",
      _resolve_load_mode(_M, spec_planned=False, mlock_now=False,
                         profile_lm="", free_ram_gb=30.0) == "none")

# the fallback is plain mmap, never mlock (mlock of 18 GB with tight RAM
# pages-and-sinks itself)
for free in (5.0, -1.0):
    mode = _resolve_load_mode(_M, spec_planned=False, mlock_now=False,
                              profile_lm="", free_ram_gb=free)
    check("8 fallback never mlock", "mlock" not in mode, f"({mode})")

# live sanity: on this Windows machine the free RAM must be measurable
free = _free_ram_gb()
check("9 _free_ram_gb measurable on Windows", free > 0, f"({free:.1f} GB)")

# boundary: free RAM exactly at model size + headroom passes
check("10 boundary at gguf+8GB passes",
      _resolve_load_mode(_M, spec_planned=False,
                         mlock_now=False, profile_lm="none",
                         free_ram_gb=11.0) == "none")

print(f"\nload_mode_ram_guard: {passed} passed, {failed} failed")
check("11 fail-closed: unknown model never gets none",
      _resolve_load_mode("definitely-not-a-model-xyz", spec_planned=False,
                         mlock_now=False, profile_lm="none",
                         free_ram_gb=30.0) == "mmap")

sys.exit(1 if failed else 0)
_gg.unlink(missing_ok=True)
