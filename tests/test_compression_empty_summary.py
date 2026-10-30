# -*- coding: utf-8 -*-
"""Test: EMPTY-SUMMARY-FALLBACK in context.compression._compress_tool_context.

A model that ignores enable_thinking answers the compression POST with an
empty `content` (only a think block). That is not an exception, so before
EMPTY-SUMMARY-FALLBACK (2026-10-30) the coder received an empty summary
block. Pins (no network, no models needed):

  1. local_only=False + client returning choices[0].message.content == ""
     -> no raise, non-empty message list, the summary message carries the
        non-trivial local fallback text (not an empty summary block).
  2. The LLM was really asked (exactly one POST) - this is the LLM path,
     not local_only.
  3. A client returning a REAL summary -> the LLM text is used instead of
     the fallback, and usage is tracked.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from context.compression import _compress_tool_context

passed = 0
failed = 0


def ok(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FixedClient:
    """client.post() -> response whose .json() returns the given payload."""

    def __init__(self, data):
        self._data = data
        self.calls = 0

    async def post(self, *a, **kw):
        self.calls += 1
        return _Resp(self._data)


SYS = "You are a coding agent."
TASK = "Build a Pac-Man clone with ghosts"
REAL_SUMMARY = (
    "KEY DECISIONS: arcade loop in ghosts.js, collision via AABB.\n"
    "CURRENT BLOCKER: none\n"
    "PROGRESS: 1/3 files written, last tool round was 2."
)


def _base_messages():
    return [
        {"role": "system", "content": SYS},
        {"role": "user", "content": TASK},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "read_file",
                                      "arguments": '{"path": "a.js"}'}}]},
        {"role": "tool", "name": "read_file", "content": "[total lines 100]\nfunction ghost() {}"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "write_file",
                                      "arguments": '{"path": "ghosts.js", "content": "x"}'}}]},
        {"role": "tool", "name": "write_file", "content": "written"},
        {"role": "user", "content": "TAIL-MARKER keep raw"},
    ]


def _run(client, **kw):
    kwargs = dict(
        model="m", port=1, client=client,
        system_prompt=SYS, original_task=TASK,
        written_files=["ghosts.js"], done_tasks=["setup"],
        local_only=False,
    )
    kwargs.update(kw)
    return asyncio.run(_compress_tool_context(**kwargs))


print("-- EMPTY-SUMMARY-FALLBACK: empty choices[].message.content --")
client = _FixedClient({"choices": [{"message": {"content": ""}}]})
# no try/except: if the call raises, the test fails loudly with the traceback
raised = None
out, condensed, usage = _run(client, messages=_base_messages())
ok("1a call did not raise", raised is None, f" ({raised!r})" if raised else "")
ok("1b exactly one POST went to the LLM", client.calls == 1,
   f" ({client.calls})" if client.calls != 1 else "")
ok("1c message list non-empty", isinstance(out, list) and len(out) >= 2,
   f" (len={len(out)})")
summary_msg = out[1] if len(out) >= 2 else {}
content = summary_msg.get("content", "") if isinstance(summary_msg, dict) else ""
ok("1d structure [system, user-summary, ...]",
   out[0].get("role") == "system" and summary_msg.get("role") == "user")
# the summary block between the markers must be non-trivial, not empty
_start = content.find("[CONTEXT SUMMARY")
_end = content.find("[END SUMMARY]")
_block = content[_start:_end] if _start != -1 and _end > _start else ""
ok("1e summary block is non-trivial local fallback",
   "State Reconstruction (compression model unavailable)" in content
   and "Original Task:" in content and TASK in content
   and len(_block) > 40,
   f" (block={len(_block)} chars)")
ok("1f written files preserved in fallback", "ghosts.js" in content)
ok("1g no usage tracked on the empty reply", usage == {} or usage == {"noop": True},
   f" ({usage})")

print("-- real summary: LLM text wins over the fallback --")
client2 = _FixedClient({
    "choices": [{"message": {"content": REAL_SUMMARY}}],
    "usage": {"completion_tokens": 42, "prompt_tokens": 100,
              "prompt_tokens_details": {"cached_tokens": 7}},
})
out2, _cond2, usage2 = _run(client2, messages=_base_messages())
c2 = out2[1]["content"]
ok("2a LLM summary text is used", "KEY DECISIONS" in c2
   and "PROGRESS: 1/3 files written" in c2)
ok("2b fallback marker absent", "State Reconstruction" not in c2)
ok("2c usage tracked from the reply",
   usage2.get("completion_tokens") == 42 and usage2.get("cached_tokens") == 7,
   f" ({usage2})")

print()
print(f"{'=' * 50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'=' * 50}")
sys.exit(0 if failed == 0 else 1)
