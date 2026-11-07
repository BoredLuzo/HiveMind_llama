# -*- coding: utf-8 -*-
"""duo_ubatch setting (2026-10-30) - the prefill lever.

Measured on the 35B MoE (b11889, pp 8192, fresh process per variant):
  ub 256 -> 77.64 t/s, shared 164 MiB
  ub 512 -> 119.96 t/s, shared 206 MiB   <- measured opt-in
  ub 1024 -> aborted (shared 80 -> 340 MiB)
  ub 2048 -> 193.59 t/s but shared peak 2348 MiB (spills to system RAM)
Prefill was 89-94 % of wall time in the 80k run, so this is the only knob
with a measurable effect on total run time.

Rules covered here: duo_ubatch wins, llama_ubatch_size stays as the older
alias, value clamped to [64, 4096] and aligned to 32, -b always >= ub and
never below 1024 (llama.cpp rejects ubatch > batch).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.llama_config import (  # noqa: E402
    resolve_ubatch_sizes, LLAMA_UBATCH, LLAMA_UBATCH_DEFAULT, LLAMA_BATCH_DEFAULT)

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="plain")
check = h.check

# default
check("module default is 256", LLAMA_UBATCH_DEFAULT == 256, f"({LLAMA_UBATCH_DEFAULT})")
check("module constant matches the default", LLAMA_UBATCH == 256, f"({LLAMA_UBATCH})")
ub, b = resolve_ubatch_sizes({})
check("no settings -> ub 256", ub == 256, f"({ub})")
check("no settings -> batch 1024 (>= ub)", b == 1024, f"({b})")
check("batch >= ubatch (llama.cpp requirement)", b >= ub)

# duo_ubatch wins over the alias
ub, b = resolve_ubatch_sizes({"duo_ubatch": 1024, "llama_ubatch_size": 256})
check("duo_ubatch wins over llama_ubatch_size", ub == 1024, f"({ub})")
check("batch follows ub 1024", b == max(1024, 1024), f"({b})")

# alias still works
ub, b = resolve_ubatch_sizes({"llama_ubatch_size": 256})
check("alias llama_ubatch_size honored", ub == 256, f"({ub})")
check("alias path keeps batch 1024 >= ub", b == 1024, f"({b})")

# ub above 1024 raises the batch (never ubatch > batch)
ub, b = resolve_ubatch_sizes({"duo_ubatch": 2048})
check("ub 2048 -> batch >= 2048", b >= 2048, f"({b})")

# clamping + alignment
check("clamp low (16 -> 64)", resolve_ubatch_sizes({"duo_ubatch": 16})[0] == 64)
check("clamp high (9999 -> 4096)", resolve_ubatch_sizes({"duo_ubatch": 9999})[0] == 4096)
check("align down (500 -> 480)", resolve_ubatch_sizes({"duo_ubatch": 500})[0] == 480,
      f"({resolve_ubatch_sizes({'duo_ubatch': 500})[0]})")
check("invalid string -> default",
      resolve_ubatch_sizes({"duo_ubatch": "auto"})[0] == LLAMA_UBATCH_DEFAULT)
check("zero -> default (not a valid chunk)",
      resolve_ubatch_sizes({"duo_ubatch": 0})[0] == LLAMA_UBATCH_DEFAULT)
check("None -> default", resolve_ubatch_sizes(None)[0] == LLAMA_UBATCH_DEFAULT)

# the wiring exists in both places
root = Path(__file__).resolve().parent.parent
srv = (root / "server.py").read_text(encoding="utf-8")
mgr = (root / "backend" / "manager_load.py").read_text(encoding="utf-8")
check("server syncs duo_ubatch via the resolver",
      "resolve_ubatch_sizes" in srv and "duo_ubatch" in srv)
check("-b follows the ubatch in the loader CMD",
      'str(max(1024, int(LLAMA_UBATCH)))' in mgr)
check("-ubatch-size uses the same value", "str(int(LLAMA_UBATCH))" in mgr)
# PRESET-SYNC (2026-10-31): a preset load must not drop an explicit duo_ubatch.
cfg = (root / "routers" / "config.py").read_text(encoding="utf-8")
check("preset sync uses the runtime-flag helper",
      "from backend.runtime_flags import apply_runtime_flags" in cfg
      and "_apply_flags(settings)" in cfg)
_rf = (root / "backend" / "runtime_flags.py").read_text(encoding="utf-8")
check("helper resolves ubatch via the resolver",
      "resolve_ubatch_sizes(settings)" in _rf)
check("duo_ubatch triggers the preset flag sync", '"duo_ubatch"' in cfg)

sys.exit(h.finish("duo_ubatch"))
