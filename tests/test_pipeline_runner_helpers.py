# -*- coding: utf-8 -*-
"""Behavior checks: core/pipeline_runner.py helpers (B3/B8, 2026-11-07).

The module is dominated by the 800-line run_pipeline generator; its extracted
helpers are the hermetic seams. These tests drive them with a fake ctx and
mock the two VRAM loader calls, so no model/server is needed:

  * _role_vision: iteration, images, role switch, model capability
  * _pin_pipeline_models: budget gate, skip_agents, loader failure
  * _unpin_pipeline_models: keep-alive restore, per-model failure isolation
"""
import asyncio
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.pipeline_runner as P  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


class _Ctx:
    def __init__(self, *, skip_agents=(), registry=None, vram=None,
                 budget=7.5, profile=None, nctx=4096):
        self.skip_agents = set(skip_agents)
        self._registry = dict(registry or {"analyst": "analyst_m"})
        self.vram_lookup_gb = dict(vram or {"analyst_m": 4.0})
        self.vram_budget = budget
        self.vram_cache = set()
        self.model_profile = profile
        self._nctx = nctx

    def registry_get(self, key):
        return self._registry.get(key, "")

    def get_num_ctx(self, model, agent_role=None):
        return self._nctx


# ── _role_vision ───────────────────────────────────────────────────────────
def _vis(*, iteration=0, images=("img",), cfg=None, profile=None):
    return P._role_vision(_Ctx(profile=profile), "analyst", iteration,
                          list(images), cfg if cfg is not None else {"analyst": True})


check("role vision: later iteration is never a vision role",
      _vis(iteration=1) is False)
check("role vision: no images -> False", _vis(images=()) is False)
check("role vision: disabled role -> False",
      _vis(cfg={"analyst": False}) is False)
check("role vision: missing profile -> False", _vis(profile=None) is False)
check("role vision: non-vision model -> False",
      _vis(profile=lambda _m: {"vision": False}) is False)
check("role vision: vision-capable model -> True",
      _vis(profile=lambda _m: {"vision": True}) is True)

# ── _pin_pipeline_models ───────────────────────────────────────────────────
async def _pin_case(ctx, loaded=("cached",)):
    pinned = set()
    with mock.patch.object(P, "_get_loaded_models_set",
                           new=mock.AsyncMock(return_value=set(loaded))), \
         mock.patch.object(P, "_bk_load", new=mock.AsyncMock()) as load:
        await P._pin_pipeline_models(ctx, pinned)
    return pinned, load


ctx = _Ctx()
pinned, load = asyncio.run(_pin_case(ctx))
check("pin: first agent model is pinned", pinned == {"analyst_m"}, f" ({pinned})")
check("pin: loader gets keep_alive -1",
      load.await_args.kwargs.get("keep_alive") == "-1", f" ({load.await_args})")
check("pin: loader gets the role ctx",
      load.await_args.kwargs.get("num_ctx") == 4096, f" ({load.await_args})")
check("pin: loaded cache is merged into vram_cache",
      ctx.vram_cache == {"cached", "analyst_m"}, f" ({ctx.vram_cache})")

big = _Ctx(vram={"analyst_m": 99.0})
pinned_big, load_big = asyncio.run(_pin_case(big))
check("pin: model over budget is not loaded",
      pinned_big == set() and load_big.await_count == 0)

empty = _Ctx(skip_agents=("analyst", "refiner", "critic", "synthesizer"))
pinned_empty, load_empty = asyncio.run(_pin_case(empty))
check("pin: no active agents -> no loader call",
      pinned_empty == set() and load_empty.await_count == 0)

async def _pin_failure():
    pinned = set()
    with mock.patch.object(P, "_get_loaded_models_set",
                           new=mock.AsyncMock(side_effect=RuntimeError("vram down"))), \
         mock.patch.object(P, "_bk_load", new=mock.AsyncMock()) as load:
        await P._pin_pipeline_models(_Ctx(), pinned)  # must not raise
    return pinned, load

pinned_fail, load_fail = asyncio.run(_pin_failure())
check("pin: loader failure is swallowed (run continues)",
      pinned_fail == set() and load_fail.await_count == 0)

# ── _unpin_pipeline_models ─────────────────────────────────────────────────
async def _unpin_case(models, fail_on=(), nctx_error_on=()):
    ctx = _Ctx()

    def _nctx(model, agent_role=None):
        if model in nctx_error_on:
            raise RuntimeError("ctx lookup failed")
        return 2048

    ctx.get_num_ctx = _nctx
    calls = []

    async def _load(model, **kw):
        calls.append((model, kw))
        if model in fail_on:
            raise RuntimeError("unload failed")

    with mock.patch.object(P, "_bk_load", new=_load):
        await P._unpin_pipeline_models(ctx, set(models), "10m")
    return calls

check("unpin: empty pin set is a no-op",
      asyncio.run(_unpin_case(set())) == [])
calls = asyncio.run(_unpin_case({"a", "b"}))
check("unpin: every pinned model is restored",
      sorted((m, kw.get("keep_alive"), kw.get("num_ctx")) for m, kw in calls)
      == [("a", "10m", 2048), ("b", "10m", 2048)], f" ({calls})")
calls_fail = asyncio.run(_unpin_case({"a", "b"}, fail_on={"a"}))
check("unpin: one failed model does not skip the next",
      sorted(m for m, _kw in calls_fail) == ["a", "b"], f" ({calls_fail})")
calls_ctx = asyncio.run(_unpin_case({"a", "b"}, nctx_error_on={"a"}))
check("unpin: ctx lookup failure is isolated per model",
      [m for m, _kw in calls_ctx] == ["b"], f" ({calls_ctx})")

sys.exit(h.finish("pipeline_runner_helpers"))
