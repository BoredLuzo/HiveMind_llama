# -*- coding: utf-8 -*-
"""Keep-workers-warm pinning (core/duo/preflight.warm_workers_after_run).

Run: python tests/test_worker_warm_pin.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


import unittest.mock

import core.duo.preflight as PF

SLOTS = [{"model": "analyst-m", "num_ctx": 16384},
         {"model": "critic-m", "ctx": 8192}]


class FakeMgr:
    def __init__(self):
        self.calls = []

    async def evict(self, model):
        self.calls.append(("evict", model))

    async def ensure_loaded(self, model, num_ctx=16384, n_parallel=1):
        self.calls.append(("load", model, num_ctx))
        return 8101


async def flow(pin: bool):
    calls = []
    fake = FakeMgr()
    fake.calls = calls

    async def fake_pin(model, num_ctx=None):
        calls.append(("pin", model, num_ctx))

    with unittest.mock.patch("backend.llama_server_manager.manager", fake), \
         unittest.mock.patch("vram.loader._bk_pin", fake_pin):
        await PF.warm_workers_after_run(SLOTS, "coder-m", pin=pin)
    return calls


# pin=False: loads only, no pin calls
calls = asyncio.run(flow(pin=False))
check("P1 pin=False loads both workers",
      [c[:2] for c in calls if c[0] == "load"] == [("load", "analyst-m"),
                                                   ("load", "critic-m")])
check("P2 pin=False never pins", not any(c[0] == "pin" for c in calls))

# pin=True: every loaded worker is pinned with its ctx
calls = asyncio.run(flow(pin=True))
pins = [c for c in calls if c[0] == "pin"]
check("P3 pin=True pins both workers", len(pins) == 2, f"({pins})")
check("P4 the pin carries the worker's num_ctx",
      ("pin", "analyst-m", 16384) in pins and ("pin", "critic-m", 8192) in pins)
check("P5 the coder is evicted first", calls[0] == ("evict", "coder-m"))

# a failing ensure_loaded must not abort the remaining workers (old contract)
class HalfBroken(FakeMgr):
    async def ensure_loaded(self, model, num_ctx=16384, n_parallel=1):
        self.calls.append(("load", model))
        if model == "analyst-m":
            raise RuntimeError("boom")
        return 8101


calls2 = []
fake2 = HalfBroken()
fake2.calls = calls2

async def fake_pin2(model, num_ctx=None):
    calls2.append(("pin", model))


async def flow2():
    with unittest.mock.patch("backend.llama_server_manager.manager", fake2), \
         unittest.mock.patch("vram.loader._bk_pin", fake_pin2):
        await PF.warm_workers_after_run(SLOTS, "coder-m", pin=True)

asyncio.run(flow2())
check("P6 a broken worker load does not stop the rest",
      any(c[:2] == ("load", "critic-m") for c in calls2))

print()
print(f"SUMMARY worker_warm_pin: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
