# -*- coding: utf-8 -*-
"""One runtime-flag helper (2026-10-31).

Four hand-rolled sync blocks drifted: ubatch ignored duo_ubatch and three
int() casts were unguarded (a garbage preset raised a 500 after the settings
had already been mutated). The helper must be total and land the values in
all three flag holders.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import llama_config as lc
from backend import llama_server_manager as lsm
from backend import manager_load as ml
from backend.runtime_flags import apply_runtime_flags

from tests._harness import Harness  # noqa: E402

h = Harness(style="plain", pad=True)
check = h.check


_FIELDS = ("CACHE_REUSE", "CACHE_RAM_MIB", "CTX_CHECKPOINTS", "MLOCK_MODEL",
           "LLAMA_UBATCH", "LLAMA_BATCH")
_saved = {m: {f: getattr(m, f) for f in _FIELDS if hasattr(m, f)}
          for m in (lc, lsm, ml)}
try:
    apply_runtime_flags({
        "llama_cache_reuse": "not-an-int",
        "llama_cache_ram_mib": None,
        "llama_ctx_checkpoints": {},
        "duo_ubatch": 1024,
        "llama_ubatch_size": 256,
        "llama_mlock": False,
    })
    check("garbage ints do not raise", True)
    check("garbage falls back to the current value",
          lc.CACHE_REUSE == _saved[lc]["CACHE_REUSE"], lc.CACHE_REUSE)
    check("duo_ubatch wins the resolver", lc.LLAMA_UBATCH == 1024,
          lc.LLAMA_UBATCH)
    check("values land in llama_config",
          lc.LLAMA_UBATCH == 1024 and lc.MLOCK_MODEL is False)
    check("values land in manager_load",
          hasattr(ml, "LLAMA_UBATCH") and ml.LLAMA_UBATCH == 1024)
    check("values land in llama_server_manager",
          hasattr(lsm, "LLAMA_UBATCH") and lsm.LLAMA_UBATCH == 1024)
finally:
    for _m, _vals in _saved.items():
        for _f, _v in _vals.items():
            setattr(_m, _f, _v)

sys.exit(h.finish("runtime_flags"))
