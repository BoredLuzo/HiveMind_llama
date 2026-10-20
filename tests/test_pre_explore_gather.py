"""Pre-Explore darf nicht an `asyncio.gather` scheitern.

Regression fuer den Live-Fehler (2026-10-20):
    "Pre-Explore task error: a coroutine was expected, got <_GatheringFuture pending>"

`asyncio.create_task()` verlangt eine Coroutine, `gather()` liefert ein Future.
Der Test prueft (a) dass das Muster im Code nicht mehr vorkommt und (b) dass das
benutzte Muster mit echten Coroutinen funktioniert - inklusive Fehler-Rueckgabe
wie im Runner (return_exceptions=True).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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


SRC = (ROOT / "hive_functions" / "pre_explore" / "runner.py").read_text(encoding="utf-8", errors="replace")

check("gather is not wrapped in create_task any more",
      "create_task(asyncio.gather" not in SRC)
check("gather uses ensure_future", "ensure_future(asyncio.gather" in SRC)

# (b) das Muster mit echten Coroutinen: ein Worker wirft, einer liefert
async def _ok():
    await asyncio.sleep(0)
    return {"worker": "ok"}


async def _boom():
    await asyncio.sleep(0)
    raise RuntimeError("worker crashed")


async def _pattern():
    tasks = [asyncio.ensure_future(_ok()), asyncio.ensure_future(_boom())]
    gather_task = asyncio.ensure_future(asyncio.gather(*tasks, return_exceptions=True))
    while not gather_task.done():
        await asyncio.sleep(0.01)
    return await gather_task


try:
    _out = asyncio.run(_pattern())
    check("ensure_future(gather(..., return_exceptions=True)) completes", isinstance(_out, list),
          f"({type(_out).__name__})")
    check("worker results and exceptions both survive",
          any(isinstance(r, dict) for r in _out) and any(isinstance(r, BaseException) for r in _out),
          f"({[type(r).__name__ for r in _out]})")
except (RuntimeError, TypeError, ValueError) as exc:
    # konkrete Fehlerarten: der Test soll sie zeigen, nicht verschlucken
    check("ensure_future(gather(...)) does not raise", False, f"({type(exc).__name__}: {exc})")

# create_task mit gather MUSS weiterhin scheitern - sonst waere der Test wertlos
async def _wrong():
    asyncio.create_task(asyncio.gather(_ok(), return_exceptions=True))


try:
    asyncio.run(_wrong())
    check("create_task(gather(...)) is indeed invalid (pattern check is meaningful)", False)
except TypeError as exc:
    check("create_task(gather(...)) is indeed invalid (pattern check is meaningful)",
          "coroutine" in str(exc), f"({str(exc)[:60]})")

print()
print(f"SUMMARY pre_explore_gather: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
