# -*- coding: utf-8 -*-
"""A9b: `await manager.unpin(...)` must reach the async variant.

LlamaServerManager's MRO put LlamaEvictMixin before LlamaHealthMixin; the sync
`unpin` in the evict mixin shadowed the async health method, so the
pre-explore path awaited an int (TypeError, swallowed) and never unpinned.
"""
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.llama_server_manager import LlamaServerManager   # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


check("1 manager.unpin is the async keep-alive variant",
      inspect.iscoroutinefunction(LlamaServerManager.unpin))
check("2 the sync pin reset is reachable as clear_pin",
      hasattr(LlamaServerManager, "clear_pin")
      and not inspect.iscoroutinefunction(LlamaServerManager.clear_pin))

_pre = (ROOT / "core" / "duo" / "_pre_explore.py").read_text(encoding="utf-8")
check("3 the pre-explore path awaits unpin", "await _lsm_workers.unpin(" in _pre)
_dr = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
check("4 the sync caller uses clear_pin", "_lsm_unpin.clear_pin(" in _dr
      and "_lsm_unpin.unpin(" not in _dr)

# 5) canonical clearing: a slot stored as "Qwen3.6:35B#2" must be unpinned
#    by the same name the loader canonicalizes to ("qwen3.6:35b").
from backend.manager_evict import LlamaEvictMixin  # noqa: E402

class _PinProbe(LlamaEvictMixin):
    def __init__(self, slots):
        self._slots = slots

_slot = SimpleNamespace(model="Qwen3.6:35B#2", pinned=True, pinned_at=1.0)
_probe = _PinProbe([_slot])
check("5 clear_pin matches case/#N aliases canonically",
      _probe.clear_pin("qwen3.6:35b") == 1 and not _slot.pinned,
      f" (pinned={_slot.pinned})")

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
