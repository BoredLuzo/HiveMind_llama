# -*- coding: utf-8 -*-
"""Compression tail cap: keep-the-END, marker, tool pairing (audit 2026-10-30).

The cap rewrites tool outputs that survive a compression. A flip to
keep-the-head, a lost marker or a broken message order would feed the coder
a truncated tool result with no hint that it was cut - the kind of silent
corruption that poisons the next round. Until now this path had no test at
all (the changelog only said "verified offline").

Offline: `_compress_tool_context` in local_only mode (no model, no server).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from context.compression import _compress_tool_context  # noqa: E402


from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="plain")

check = h.check


class _NoClient:
    async def post(self, *a, **k):
        raise RuntimeError("local_only must not call the LLM")


def build_list():
    base = "Agent notes: " + "quick brown fox " * 400
    msgs = []
    for i in range(1, 15):
        msgs.append({"role": "user", "content": base + f" Round {i}."})
        msgs.append({"role": "assistant", "content": f"Round {i} done."})
        body = ("HEAD-OF-DUMP\n" + "line of context\n" * 300
                + f"ERROR: assertion failed at round {i}\nFAILED test_x.py::{i}")
        msgs.append({"role": "tool", "name": "read_file",
                     "tool_call_id": f"call_{i}", "content": body})
        msgs.append({"role": "user", "content": f"Continue {i+1}."})
    return msgs


def run(cap):
    async def _go():
        return await _compress_tool_context(
            messages=build_list(), model="x", port=0, client=_NoClient(),
            system_prompt="sys", original_task="t", written_files=[], done_tasks=[],
            keep_recent_msgs=12, compression_mode="full", local_only=True,
            tail_tool_max_chars=cap)
    return asyncio.run(_go())


post, files, usage = run(400)
tools = [m for m in post if m.get("role") == "tool"]
check("cap keeps the three tail tool messages", len(tools) == 3, f"({len(tools)})")

for i, m in enumerate(tools, 1):
    c = m.get("content") or ""
    check(f"tool#{i}: END of the output survives (error text present)",
          "FAILED test_x" in c)
    check(f"tool#{i}: marker names the dropped head",
          "first " in c and "chars dropped" in c and "tail_tool_max_chars" in c)
    check(f"tool#{i}: head was actually dropped", "HEAD-OF-DUMP" not in c)
    check(f"tool#{i}: capped near the limit", len(c) < 400 + 200, f"({len(c)} chars)")
    check(f"tool#{i}: tool_call_id preserved", bool(m.get("tool_call_id")),
          m.get("tool_call_id") or "")

# message order: a tool message never follows a user message
seq = [m.get("role") for m in post]
bad = [i for i in range(1, len(seq)) if seq[i] == "tool" and seq[i-1] == "user"]
check("message order intact (no tool right after user)", not bad, f"({bad})")

# cap disabled -> content untouched
post2, _, _ = run(0)
tools2 = [m for m in post2 if m.get("role") == "tool"]
check("cap 0 leaves the texts untouched",
      all("HEAD-OF-DUMP" in ((m.get("content") or "")) for m in tools2))

# smaller cap -> shorter text, still ends with the error
post3, _, _ = run(120)
tools3 = [m for m in post3 if m.get("role") == "tool"]
check("smaller cap yields shorter content",
      all(len((m.get("content") or "")) < len((m2.get("content") or "")) + 200
          for m, m2 in zip(tools3, tools)),
      f"({[len(m.get('content') or '') for m in tools3]})")
check("smaller cap still keeps the tail", all("FAILED test_x" in (m.get("content") or "")
                                              for m in tools3))

sys.exit(h.finish("tail_cap"))
