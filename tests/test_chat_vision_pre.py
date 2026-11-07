# -*- coding: utf-8 -*-
"""Behavior test: _ChatVisionPreState roundtrip (2026-11-07 audit).

The split (dc13661 class of bugs) is about the SYNC-BACK: the helper mutates
`ps.effective_images`, `ps.image_description` and sets `ps._judge_gb`.
Pins only checked strings; this drives the real helper with a fake `server`
module and a no-image request (the _judge_gb None path).
"""
import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# `server` is imported lazily inside the helper; a stub keeps this suite
# hermetic (no FastAPI app, no model registry startup).
_fake_server = types.ModuleType("server")
_fake_server.S_models_cache = []
_fake_server._vision_cfg = {}


class _Registry:
    def get(self, role, default=None):
        return "judge-stub"


_fake_server._registry = _Registry()
sys.modules["server"] = _fake_server

from core.chat_vision_pre import _ChatVisionPreState, _chat_vision_pre  # noqa: E402

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}{extra}")
    else:
        failed += 1
        print(f"  FAIL {name}{extra}")


async def _emit(event):
    return event


async def _run(ps):
    events = []
    ps.carry = {"emit": _emit}
    async for ev in _chat_vision_pre(ps):
        events.append(ev)
    return events


ps = _ChatVisionPreState(
    images=[],
    user_input="draw a cat",
    mode="direct",
    effective_images=["kept"],
    image_description="pre-existing description",
)
_events = asyncio.run(_run(ps))
check("no-image path emits no status", _events == [], f" ({_events})")
check("effective_images roundtrips", ps.effective_images == ["kept"],
      f" ({ps.effective_images})")
check("image_description roundtrips",
      ps.image_description == "pre-existing description",
      f" ({ps.image_description!r})")
check("_judge_gb stays None without a vision pass", ps._judge_gb is None)
check("_saved_image_paths is initialized", ps._saved_image_paths == [])

# empty defaults must not leak stale values into the runner
ps2 = _ChatVisionPreState(images=[], user_input="hi", mode="direct")
asyncio.run(_run(ps2))
check("defaults: effective_images empty", ps2.effective_images == [])
check("defaults: image_description empty", ps2.image_description == "")
check("defaults: judge_gb None", ps2._judge_gb is None)

print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
