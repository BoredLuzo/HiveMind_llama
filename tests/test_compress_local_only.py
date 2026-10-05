# -*- coding: utf-8 -*-
"""Test: duo_compress_local_only — LLM summary POST is skipped entirely.

Checks (no network, no models needed):
  1. local_only=True  -> client.post is NEVER called, output structure equals
     the fallback-summary structure ([system][summary][raw tail]).
  2. local_only=False with a failing client -> same fallback structure
     (timeout path unchanged).
  3. Partial mode + local_only: the raw tail stays byte-identical.
  4. Plan anchor is re-injected into the resulting summary message.
  5. goal_pin content survives (merged into the summary message).
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


class _ForbiddenClient:
    """Client whose post() must never be called in local_only mode."""
    def post(self, *a, **kw):
        raise AssertionError("client.post must not be called in local_only mode")


class _ExplodingClient:
    """Client simulating a ReadTimeout / connection failure."""
    async def post(self, *a, **kw):
        raise TimeoutError("simulated read timeout")


SYS = "You are a coding agent."
TASK = "Build a Pac-Man clone with ghosts"
PLAN = "1. ghosts.js anlegen\n2. Bewegungslogik\n3. Kollision"
GOAL = "OVERALL GOAL: playable pacman"


def _base_messages():
    return [
        {"role": "system", "content": SYS},
        {"role": "user", "content": TASK},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "a.js"}'}}]},
        {"role": "tool", "name": "read_file", "content": "[total lines 100]\nfunction ghost() {}"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "function": {"name": "write_file", "arguments": '{"path": "ghosts.js", "content": "x"}'}}]},
        {"role": "tool", "name": "write_file", "content": "written"},
        {"role": "user", "content": "TAIL-MARKER keep raw"},
    ]


def _run(**kw):
    kwargs = dict(
        model="m", port=1, client=_ForbiddenClient(),
        system_prompt=SYS, original_task=TASK,
        written_files=["ghosts.js"], done_tasks=["setup"],
        plan_anchor_text=PLAN, goal_pin={"role": "user", "content": GOAL},
    )
    kwargs.update(kw)
    out, _condensed, _usage = asyncio.run(_compress_tool_context(**kwargs))
    return out


print("-- local_only=True: no POST, fallback structure --")
out = _run(messages=_base_messages(), local_only=True)
ok("1a structure [system, summary]", out[0]["role"] == "system"
   and out[1]["role"] == "user" and len(out) >= 2)
ok("1b local fallback summary used",
   "State Reconstruction" in out[1]["content"] and "Original Task" in out[1]["content"])
ok("1c written files preserved", "ghosts.js" in out[1]["content"])
ok("1d plan anchor reinjected", PLAN.split("\n")[0] in out[1]["content"])
ok("1e goal pin merged", GOAL in out[1]["content"])

print("-- local_only=False + failing client: timeout path unchanged --")
out2 = _run(messages=_base_messages(), client=_ExplodingClient())
ok("2a fallback structure on failure", len(out2) >= 2 and out2[0]["role"] == "system"
   and "State Reconstruction" in out2[1]["content"])
ok("2b plan anchor survives failure", PLAN.split("\n")[0] in out2[1]["content"])

print("-- partial mode + local_only: raw tail byte-identical --")
msgs = _base_messages()
cut = 4  # tail = messages[4:] must come back byte-identical
out3 = _run(messages=msgs, local_only=True,
            compression_mode="partial", cut_index=cut)
ok("3a tail byte-identical", out3[2:] == msgs[cut:])
ok("3b structure still [system, summary, tail]", out3[0]["role"] == "system"
   and out3[1]["role"] == "user")

print("-- empty history edge: returns input unchanged --")
tiny = [{"role": "system", "content": SYS}, {"role": "user", "content": "hi"}]
out4 = _run(messages=tiny, local_only=True)
ok("4 nothing to compress -> unchanged", out4 == tiny)

print()
print(f"{'=' * 50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'=' * 50}")
sys.exit(0 if failed == 0 else 1)
