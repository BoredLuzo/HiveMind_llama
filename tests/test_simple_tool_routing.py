# -*- coding: utf-8 -*-
"""A3 behavior: simple tool prompts must not become Code Duo runs (2026-11-07).

Two layers are covered:
  * is_simple_tool_prompt: narrow read/list detection with coding-verb veto
  * _chat_route_decision: persisted agentic mode / judge duo cannot hijack a
    simple list/read prompt; explicit agentic_force and explicit modes still do
"""
import asyncio
import sys
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.chat_route_decision as CRD  # noqa: E402
from routing.agent_intent import is_simple_tool_prompt  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

# ── predicate ──────────────────────────────────────────────────────────────
check("simple: list files", is_simple_tool_prompt("List files in the repository"))
check("simple: read the file", is_simple_tool_prompt("read the file app.py"))
check("simple: read by extension", is_simple_tool_prompt("Read app.py"))
check("simple: git status", is_simple_tool_prompt("git status"))
check("simple: german show file", is_simple_tool_prompt("Zeig mir die Datei"))
check("simple: german list folder", is_simple_tool_prompt("Bitte liste die Dateien im Ordner"))
check("simple: search for", is_simple_tool_prompt("search for TODO in src"))
check("simple: coding verb vetoes", not is_simple_tool_prompt(
    "Read app.py and fix the validation bug"))
check("simple: implement vetoes", not is_simple_tool_prompt(
    "Implement a new endpoint in server.py"))
check("simple: empty is not a tool prompt", not is_simple_tool_prompt(""))
check("simple: long prompt is not simple", not is_simple_tool_prompt(
    "list files " + "and also tell me about the history of this project " * 6))
check("simple: a question is not a tool prompt",
      not is_simple_tool_prompt("Was ist die Hauptstadt von Frankreich?"))


# ── route decision ─────────────────────────────────────────────────────────
class _Decision:
    def __init__(self, judge=None):
        self.route = "simple"
        self.source = "judge"
        self.confidence = 0.9
        self.planner = False
        # _chat_route_decision reads decision.details["judge_verdict"].
        self.details = {"judge_verdict": dict(judge or {})}


def _ps(text, *, agentic=False, mode="auto", force=False, decider="off",
        decision=None):
    events = []

    async def emit(ev):
        events.append(ev)
        return ""

    async def decide(_text):
        return decision or _Decision()

    return types.SimpleNamespace(
        _decide_route=decide, _prepro_success=True,
        _run_settings={"routing_decider": decider},
        _sess_compress_threshold=999, _task_type="general",
        agentic_force=force, duo_config=types.SimpleNamespace(agentic_mode=agentic),
        emit=emit, force_complexity=None, images=[], mode=mode,
        model_overrides={}, user_input=text, _complexity_source="",
        _effective_task_type="", _judge_toolsize="", _judge_verdict={},
        _pipeline_sess_msgs=[], _route_as_tool=False, complexity="",
    ), events


async def _consume(ps):
    async for _ in CRD._chat_route_decision(ps):
        pass


def _route(ps, *, settings=None):
    _memory = types.SimpleNamespace(get_session_messages=lambda **kw: [])
    with mock.patch.object(CRD, "settings", settings or {"routing_decider": "off"}), \
         mock.patch.object(CRD._state, "memory", _memory), \
         mock.patch("routing.model_automap.get_routing_suggestion", return_value=None):
        asyncio.run(_consume(ps))


def _status(events):
    return " ".join(str(e.get("content", "")) for e in events
                    if e.get("type") == "status")

_p1, _e1 = _ps("List files in the repo", agentic=True)
_route(_p1)
check("route: agentic + list prompt -> simple", _p1.complexity == "simple",
      f" ({_p1.complexity})")
check("route: source is tool_intent", _p1._complexity_source == "tool_intent",
      f" ({_p1._complexity_source})")
check("route: status names direct tools", "Simple tool request" in _status(_e1),
      f" ({_status(_e1)!r})")

_p2, _ = _ps("Fix the bug in chat_run.py", agentic=True)
_route(_p2)
check("route: agentic + coding prompt -> code_duo", _p2.complexity == "code_duo",
      f" ({_p2.complexity})")

_p3, _ = _ps("List files in the repo", agentic=True, force=True)
_route(_p3)
check("route: explicit agentic force wins", _p3.complexity == "code_duo",
      f" ({_p3.complexity})")

_p4, _ = _ps("List files in the repo", agentic=False, decider="on",
             decision=_Decision({"route": "duo", "task_type": "general",
                                 "tool_model": "small"}))
with mock.patch.object(CRD._routing_decider, "complexity_for", return_value="simple"):
    _route(_p4)
check("route: judge duo cannot hijack a simple list prompt",
      _p4.complexity == "simple", f" ({_p4.complexity})")

_p5, _ = _ps("Implement the new endpoint", agentic=False, decider="on",
             decision=_Decision({"route": "duo", "task_type": "general",
                                 "tool_model": "small"}))
with mock.patch.object(CRD._routing_decider, "complexity_for", return_value="simple"):
    _route(_p5)
check("route: judge duo still routes real coding work to code_duo",
      _p5.complexity == "code_duo", f" ({_p5.complexity})")

_p6, _ = _ps("List files in the repo", agentic=False, mode="code_duo")
_route(_p6)
check("route: explicit code_duo mode is untouched",
      _p6.complexity == "code_duo", f" ({_p6.complexity})")

sys.exit(h.finish("simple_tool_routing"))
