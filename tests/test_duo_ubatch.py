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
    resolve_ubatch_sizes, LLAMA_UBATCH, LLAMA_UBATCH_DEFAULT)

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
check("duo_ubatch triggers the preset flag sync", '"duo_ubatch"' in cfg)

# ── B3 (2026-11-09): the helper pin as a behavior probe ────────────────────
# "resolve_ubatch_sizes(settings)" in runtime_flags.py only proves the call
# exists. Run the helper: duo_ubatch must beat the llama_ubatch_size alias
# and land on all three flag holders (manager_load's by-value LLAMA_UBATCH
# is what the loader CMD actually reads).
import backend.llama_config as _lc        # noqa: E402
import backend.llama_server_manager as _lsm  # noqa: E402
import backend.manager_load as _ml        # noqa: E402
from backend.runtime_flags import apply_runtime_flags  # noqa: E402

_HOLDERS = (_lc, _lsm, _ml)
_KEYS = ("CACHE_REUSE", "CACHE_RAM_MIB", "CTX_CHECKPOINTS",
         "MLOCK_MODEL", "LLAMA_UBATCH")
_saved = {k: tuple(getattr(m, k, None) for m in _HOLDERS) for k in _KEYS}
try:
    apply_runtime_flags({"duo_ubatch": 512, "llama_ubatch_size": 1024,
                         "llama_cache_reuse": 100, "llama_cache_ram_mib": 200,
                         "llama_ctx_checkpoints": 3, "llama_mlock": False})
    check("runtime-flag helper: duo_ubatch 512 wins and reaches all 3 holders",
          all(getattr(m, "LLAMA_UBATCH") == 512 for m in _HOLDERS),
          f" ({[getattr(m, 'LLAMA_UBATCH', None) for m in _HOLDERS]})")
    check("runtime-flag helper: cache/checkpoint/mlock flags pushed too",
          _lc.CACHE_REUSE == 100 and _lsm.CACHE_RAM_MIB == 200
          and _ml.CTX_CHECKPOINTS == 3
          and _lsm.MLOCK_MODEL is False and _lc.MLOCK_MODEL is False,
          f" ({_lc.CACHE_REUSE}/{_lsm.CACHE_RAM_MIB}/{_ml.CTX_CHECKPOINTS})")
    apply_runtime_flags({"llama_ubatch_size": 384})
    check("runtime-flag helper: alias resolves when duo_ubatch is unset",
          _ml.LLAMA_UBATCH == 384, f" ({_ml.LLAMA_UBATCH})")
finally:
    for k, vals in _saved.items():
        for m, v in zip(_HOLDERS, vals):
            if v is not None:
                setattr(m, k, v)

sys.exit(h.finish("duo_ubatch"))
