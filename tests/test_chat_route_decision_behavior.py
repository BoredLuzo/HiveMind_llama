# -*- coding: utf-8 -*-
"""Behavior tests for core/chat_route_decision.py (mode-decision tail).

The async generator is driven against the REAL _ChatRouteDecisionState.
External seams are faked: the decider closure (_decide_route) returns
canned routing.decider.Decision objects, the learned-route hint is
monkeypatched on routing.model_automap (the module re-imports it on every
call), core.state.memory is a fake for the duo re-window, and settings
keys are patched explicitly so the suite never depends on settings.json.
No model, no network.

Run: python tests/test_chat_route_decision_behavior.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.state as CS  # noqa: E402
import core.chat_route_decision as CRD  # noqa: E402
import routing.model_automap as MA  # noqa: E402
from core.chat_route_decision import _ChatRouteDecisionState  # noqa: E402
from routing.decider import Decision  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

_MISSING = object()


class Patch:
    def __init__(self):
        self._undo = []

    def set(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name, _MISSING)))
        setattr(obj, name, value)

    def restore(self):
        while self._undo:
            obj, name, old = self._undo.pop()
            if old is _MISSING:
                delattr(obj, name)
            else:
                setattr(obj, name, old)


@contextmanager
def patched_settings(**tmp):
    saved = {k: CS.settings.get(k, _MISSING) for k in tmp}
    CS.settings.update(tmp)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                CS.settings.pop(k, None)
            else:
                CS.settings[k] = v


def make_ps(**over):
    """A decision state with every field the generator reads pre-filled."""
    ps = _ChatRouteDecisionState()
    ps._decide_route = None
    ps._prepro_success = True
    ps._run_settings = {}
    ps._sess_compress_threshold = 20
    ps._task_type = "general"
    ps.agentic_force = False
    ps.duo_config = SimpleNamespace(agentic_mode=False)
    ps.emit = None
    ps.force_complexity = None
    ps.images = []
    ps.mode = "auto"
    ps.model_overrides = {}
    ps.user_input = "what is the capital of france"
    for k, v in over.items():
        setattr(ps, k, v)
    return ps


async def drive(ps):
    """Run the decision generator, collecting emitted status events."""
    events = []

    async def emit(evt):
        events.append(evt)
        return evt

    ps.emit = emit
    async for _ in CRD._chat_route_decision(ps):
        pass
    return events


def canned(route, source="decider", confidence=0.9, judge_verdict=None):
    details = {"judge_verdict": judge_verdict} if judge_verdict else {}
    return Decision(route=route, source=source, confidence=confidence,
                    details=details)


def _fake_decider(decision):
    async def _decide(user_input):
        return decision
    return _decide


async def amain():
    patch = Patch()
    # Learned-route hint is re-imported on every call - patch at the source
    # module so real routing weights on disk cannot leak into the suite.
    patch.set(MA, "get_routing_suggestion", lambda task_type: None)

    # ── mode-driven basics ──────────────────────────────────────────────────
    ps = make_ps(mode="simple")
    with patched_settings(routing_decider="off", direct_tools_enabled=True,
                          vision_agent_enabled=False):
        ev = await drive(ps)
    check("mode simple -> complexity simple", ps.complexity == "simple")
    check("mode simple -> source mode", ps._complexity_source == "mode")
    check("mode simple: no status events", ev == [])

    ps = make_ps(mode="auto", force_complexity="complex")
    with patched_settings(routing_decider="off"):
        ev = await drive(ps)
    check("force_complexity wins", ps.complexity == "complex"
          and ps._complexity_source == "manual")
    check("force_complexity announces itself",
          any("manually" in e.get("content", "") for e in ev), f"{ev}")

    # ── agentic paths: explicit force vs. persisted-flag guard ──────────────
    ps = make_ps(mode="auto", agentic_force=True,
                 duo_config=SimpleNamespace(agentic_mode=True))
    with patched_settings(routing_decider="off"):
        ev = await drive(ps)
    check("agentic force -> code_duo",
          ps.complexity == "code_duo" and ps._complexity_source == "agentic")
    check("agentic force emits status",
          any("Agentic mode active" in e.get("content", "") for e in ev))

    # The persisted agentic flag must not hijack auto while the decider is on
    # (2026-10-21 guard): a decider decision decides instead.
    ps = make_ps(mode="auto", duo_config=SimpleNamespace(agentic_mode=True),
                 _run_settings={"routing_decider": "on"})
    ps._decide_route = _fake_decider(canned("direct", source="decider"))
    with patched_settings(routing_decider="on"):
        ev = await drive(ps)
    check("agentic flag guarded while decider is on",
          ps.complexity != "code_duo" and ps._complexity_source == "decider",
          f"{ps.complexity}/{ps._complexity_source}")
    check("decider decision announced",
          any("Routing:" in e.get("content", "") for e in ev))

    # ── automap shortcuts (decider off) ─────────────────────────────────────
    ps = make_ps(mode="automap", _task_type="general",
                 user_input="please fix the login bug in the app")
    with patched_settings(routing_decider="off", direct_tools_enabled=True,
                          vision_agent_enabled=False):
        await drive(ps)
    check("automap: short successful prepro -> simple shortcut",
          ps.complexity == "simple" and ps._complexity_source == "shortcut")
    check("automap shortcut: effective task pinned to factual",
          ps._effective_task_type == "factual")

    ps = make_ps(mode="automap", _task_type="creative", _prepro_success=False,
                 user_input="write a poem about rain")
    with patched_settings(routing_decider="off", vision_agent_enabled=False):
        await drive(ps)
    check("automap: tiny creative task without prepro -> trivial",
          ps.complexity == "trivial" and ps._complexity_source == "shortcut")

    ps = make_ps(mode="automap", _prepro_success=False, images=["b64img"])
    with patched_settings(routing_decider="off"):
        await drive(ps)
    check("automap: failed vision prepro escalates to complex/image",
          ps.complexity == "complex" and ps._complexity_source == "image")

    # ── generic auto mode: vision fallback + trivial shortcut ───────────────
    ps = make_ps(mode="auto", _prepro_success=False, images=["b64img"])
    with patched_settings(routing_decider="off"):
        await drive(ps)
    check("auto: image without prepro -> simple vision fallback",
          ps.complexity == "simple"
          and ps._complexity_source == "image_fallback"
          and ps._effective_task_type == "vision")

    ps = make_ps(mode="auto", _task_type="creative",
                 user_input="hi there friend")
    with patched_settings(routing_decider="off", direct_tools_enabled=True,
                          vision_agent_enabled=False):
        await drive(ps)
    check("auto: tiny creative task -> trivial shortcut",
          ps.complexity == "trivial" and ps._complexity_source == "shortcut")

    # ── judge verdicts: tool route, pipeline upgrade, duo re-window ─────────
    def _decider_with(jv):
        return _fake_decider(canned("tool", judge_verdict=jv))

    jv_tool = {"route": "tool", "task_type": "factual", "tool_model": "large"}
    ps = make_ps(mode="auto", _run_settings={"routing_decider": "on"},
                 _decide_route=_decider_with(jv_tool))
    with patched_settings(routing_decider="on", direct_tools_enabled=True):
        await drive(ps)
    check("judge tool + direct tools on -> simple direct_tools",
          ps.complexity == "simple"
          and ps._complexity_source == "direct_tools")
    check("judge tool: no separate tool-agent route",
          ps._route_as_tool is False)
    check("judge verdict synced back",
          ps._judge_verdict.get("route") == "tool"
          and ps._judge_toolsize == "large"
          and ps._effective_task_type == "factual")

    ps = make_ps(mode="auto", _run_settings={"routing_decider": "on"},
                 _decide_route=_decider_with(jv_tool))
    with patched_settings(routing_decider="on", direct_tools_enabled=False):
        await drive(ps)
    check("judge tool + direct tools off -> route as tool",
          ps._route_as_tool is True)

    jv_pipe = {"route": "pipeline", "task_type": "reasoning"}
    ps = make_ps(mode="auto", _run_settings={"routing_decider": "on"},
                 _decide_route=_decider_with(jv_pipe))
    with patched_settings(routing_decider="on"):
        await drive(ps)
    check("judge pipeline upgrade -> complex",
          ps.complexity == "complex", f"{ps.complexity}")

    jv_duo = {"route": "duo", "task_type": "coding"}

    class FakeMemory:
        def __init__(self, n):
            self.msgs = [{"role": "user" if i % 2 == 0 else "assistant",
                          "content": f"turn {i}"} for i in range(n)]

        def get_session_messages(self, *a, **k):
            return list(self.msgs)

    patch.set(CS, "memory", FakeMemory(25))
    ps = make_ps(mode="auto", _run_settings={"routing_decider": "on"},
                 _decide_route=_decider_with(jv_duo),
                 _sess_compress_threshold=5)
    with patched_settings(routing_decider="on",
                          session_history_budget_tokens=10000):
        await drive(ps)
    check("judge duo -> code_duo with judge source",
          ps.complexity == "code_duo" and ps._complexity_source == "judge")
    check("judge duo: big session re-windowed fresh",
          len(ps._pipeline_sess_msgs) == 25, f"{len(ps._pipeline_sess_msgs)}")
    patch.restore()  # removes the fake memory again

    # ── learned routing hint ────────────────────────────────────────────────
    ps = make_ps(mode="simple")
    patch.set(MA, "get_routing_suggestion",
              lambda tt: {"preferred_mode": "pipeline", "confidence": 0.9})
    with patched_settings(routing_decider="off"):
        ev = await drive(ps)
    check("learned hint overrides to complex",
          ps.complexity == "complex" and ps._complexity_source == "learned")
    check("learned hint announced",
          any("Learned routing" in e.get("content", "") for e in ev))

    ps = make_ps(mode="simple")
    patch.set(MA, "get_routing_suggestion",
              lambda tt: {"preferred_mode": "pipeline", "confidence": 0.5})
    with patched_settings(routing_decider="off"):
        await drive(ps)
    check("low-confidence hint ignored",
          ps.complexity == "simple" and ps._complexity_source == "mode")

    patch.restore()
    return h.finish("chat_route_decision_behavior")


def main():
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
