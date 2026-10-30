# -*- coding: utf-8 -*-
"""Cache budget vs compression threshold (risk item 1, 2026-10-30).

Measured: one prompt-cache entry costs ~18 KiB per token (llama.dll
eviction line: 526 MiB / 29 383 tokens). The run compresses at
`auto_floor * ctx` (0.70), and the entry it needs to keep at that moment
must fit into `--cache-ram`. Shipped: 1024 MiB.

    ctx 85000  -> threshold 59 499 -> needs 1 307 MiB  -> 283 MiB short
    ctx 131072 -> threshold 91 750 -> needs 2 016 MiB  -> 992 MiB short
    ctx 32768  -> threshold 22 937 -> needs   504 MiB  -> fits

A shortfall is not a crash: it silently turns every compression into a full
re-prefill (measured 9-11 min). The loader now warns, and this test pins the
arithmetic.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from context.ctx_guard import (  # noqa: E402
    cache_ram_required_mib, resolve_compress_threshold,
    PROMPT_CACHE_KIB_PER_TOKEN)

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


check("entry cost constant matches the measurement",
      abs(PROMPT_CACHE_KIB_PER_TOKEN - 18.0) < 0.01)

# the three configurations that matter
ctx85 = resolve_compress_threshold(ctx_tokens=85000, num_predict=4096,
                                   auto_floor=0.70, overflow_reserve=1024)
need85 = cache_ram_required_mib(ctx85)
check("ctx 85000 -> threshold ~59.5k", 59000 <= ctx85 <= 60000, f"({ctx85})")
check("ctx 85000 needs more than the shipped 1024 MiB", need85 > 1024, f"({need85} MiB)")

ctx131 = resolve_compress_threshold(ctx_tokens=131072, num_predict=4096,
                                    auto_floor=0.70, overflow_reserve=1024)
need131 = cache_ram_required_mib(ctx131)
check("ctx 131072 needs ~2 GiB", 1900 < need131 < 2150, f"({need131} MiB)")

ctx32 = resolve_compress_threshold(ctx_tokens=32768, num_predict=4096,
                                    auto_floor=0.70, overflow_reserve=1024)
need32 = cache_ram_required_mib(ctx32)
check("ctx 32768 fits into 1024 MiB", need32 < 1024, f"({need32} MiB)")

# monotonic + degenerate inputs
check("requirement grows with the threshold", need32 < need85 < need131)
check("zero threshold -> zero", cache_ram_required_mib(0) == 0)
check("negative threshold -> zero", cache_ram_required_mib(-5) == 0)
check("headroom parameter scales linearly",
      cache_ram_required_mib(10000, headroom=2.0)
      == 2 * cache_ram_required_mib(10000, headroom=1.0))

# the loader carries the warning
ml = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
check("loader warns about the cache/threshold shortfall",
      "cache_ram_required_mib" in ml and "CACHE-RAM" in ml.upper())

print(f"\ncache_budget: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
