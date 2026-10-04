# -*- coding: utf-8 -*-
"""Does the plan (and its VISUAL details) survive tool-context compression?

(2026-10-04, user question: 'Ob die Planner-Details die Kompression
ueberleben, ist unverifiziert'.) Answered by running the REAL
_compress_tool_context offline in the live configuration (local_only=True -
exactly what the live runs logged) against a realistic message list:

  - system prompt with the [VISION] note
  - first user message: plan text WITH visual details + image parts
  - 14 older tool/assistant messages that exceed keep_recent
  - recent tail

Graded:
  1. image parts are GONE after compression (known - measured live 1 -> 0)
  2. the plan ANCHOR (subtask titles) is re-injected - ALWAYS, by design
  3. subtask titles carrying visual details => details survive WITH the
     anchor (the planner must put the essentials in TITLES)
  4. the plan BODY (long-form text in the first user message) does NOT
     survive local-only compression - only titles + original task do.
     This is the honest answer to the rest risk.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from context.compression import _compress_tool_context  # noqa: E402
from core.duo_helpers import CODER_VISION_NOTE  # noqa: E402

PLAN_DETAILS = ("Layout: red top half (#C0392B), blue bottom half (#2471A3), "
                "yellow diagonal stripe (#F1C40F) top-left to bottom-right, "
                "white 200px square centered, code XY42 as <title>")
SUBTASK_TITLES = [
    "Write repro.html: red top half #C0392B + blue bottom half #2471A3 + "
    "yellow diagonal stripe + white center square + title XY42",
]


def _build_messages():
    msgs = [
        {"role": "system", "content": "coder sys" + CODER_VISION_NOTE},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
            {"type": "text", "text": "PLAN:\n" + PLAN_DETAILS},
        ]},
    ]
    for i in range(14):
        msgs.append({"role": "assistant",
                     "content": f'thought {i}: proceed with step', "tool_calls": []})
        msgs.append({"role": "tool", "name": "read_file",
                     "content": f"file{i}.py: line1\nline2\npath=file{i}.py"})
    msgs.append({"role": "assistant", "content": "latest round output"})
    return msgs


def test_image_parts_are_dropped_by_compression():
    out, _condensed, _meta = asyncio.run(_compress_tool_context(
        _build_messages(), "qwen3.5:9b-ud", 8101, None,
        system_prompt="coder sys" + CODER_VISION_NOTE,
        original_task="reproduce the attached image",
        written_files=[], done_tasks=[], keep_recent_msgs=6,
        local_only=True))
    n_img = sum(1 for m in out for p in (m.get("content") or [])
                if isinstance(p, dict) and p.get("type") == "image_url")
    assert n_img == 0, f"image parts survived compression: {n_img}"


def test_plan_anchor_with_details_survives():
    out, _c, _m = asyncio.run(_compress_tool_context(
        _build_messages(), "qwen3.5:9b-ud", 8101, None,
        system_prompt="coder sys" + CODER_VISION_NOTE,
        original_task="reproduce the attached image",
        written_files=[], done_tasks=[], keep_recent_msgs=6,
        plan_anchor_text=", ".join(SUBTASK_TITLES),
        local_only=True))
    blob = " ".join(str(m.get("content")) for m in out if isinstance(m.get("content"), str))
    assert "PLAN - must continue" in blob, "plan anchor not re-injected"
    assert "#C0392B" in blob and "XY42" in blob, \
        "subtask-title visual details did not survive via the anchor"


def test_plan_body_does_not_survive_local_only():
    # THE HONEST FINDING: the long-form plan body lives in the first user
    # message; local-only compression condenses older messages to a task/
    # files/done summary + the title anchor. The body itself is gone.
    out, _c, _m = asyncio.run(_compress_tool_context(
        _build_messages(), "qwen3.5:9b-ud", 8101, None,
        system_prompt="coder sys" + CODER_VISION_NOTE,
        original_task="reproduce the attached image",
        written_files=[], done_tasks=[], keep_recent_msgs=6,
        plan_anchor_text=", ".join(SUBTASK_TITLES),
        local_only=True))
    blob = " ".join(str(m.get("content")) for m in out if isinstance(m.get("content"), str))
    assert "top-left to bottom-right" not in blob, \
        "plan BODY survived local-only compression (would contradict the design)"


def test_anchor_carries_full_plan_body():
    # USER DECISION (2026-10-04): "600 zu wenig, der fertige Plan soll als
    # Ganzes durchgegeben werden" - the anchor carries the COMPLETE plan
    # body, no truncation. Every visual detail survives compression.
    from core.duo_runner import _build_plan_anchor_text
    long_plan = "Full plan:\n" + PLAN_DETAILS + "\n" + ("more detail line\n" * 120)
    anchor = _build_plan_anchor_text(SUBTASK_TITLES, None, plan_content=long_plan)
    assert "PLAN:\nFull plan:" in anchor
    assert "red top half" in anchor and "XY42" in anchor
    assert anchor.endswith("more detail line")  # nothing cut
    assert "more detail line" in anchor
    # empty plan -> unchanged legacy behavior
    assert _build_plan_anchor_text(SUBTASK_TITLES, None) == ", ".join(SUBTASK_TITLES)


def test_vision_note_replacement_survives_compression():
    # the [VISION] note sits in the SYSTEM prompt - compression rebuilds the
    # system message from the given system_prompt verbatim; the round-level
    # replacement (image gone) must therefore still reach the model
    out, _c, _m = asyncio.run(_compress_tool_context(
        _build_messages(), "qwen3.5:9b-ud", 8101, None,
        system_prompt="coder sys" + CODER_VISION_NOTE,
        original_task="x", written_files=[], done_tasks=[],
        keep_recent_msgs=6, local_only=True))
    sys_msg = next(m for m in out if m.get("role") == "system")
    assert CODER_VISION_NOTE in sys_msg["content"], \
        "system prompt (with the note) was altered by compression"
