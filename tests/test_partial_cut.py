#!/usr/bin/env python3
"""Partial compression must actually condense — regression for the live failure
(2026-09-08): plan_partial_cut_index counted the pinned system prompt toward
to_remove, the cut landed before the first tool output, _compress_tool_context
silently returned the ORIGINAL list ("done before=26473 after=26473") and the
3-strike guard stopped the run at ~65% ctx.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context.compression import _compress_tool_context
from context.ctx_guard import plan_partial_cut_index
from utils.token import estimate_ctx_tokens as _est

_ok = True


def _check(name, cond):
    global _ok
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    _ok = _ok and cond


def _build_msgs() -> list:
    msgs = [
        {"role": "system", "content": "x" * 45000},  # pinned sys prompt + repo map
        {"role": "user", "content": "Build the Pac-Man game. " + "plan " * 100},
    ]
    for i in range(24):
        msgs.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{"function": {
                "name": "run_bash",
                "arguments": json.dumps({"cmd": f"echo round {i}"}),
            }}],
        })
        msgs.append({"role": "tool", "name": "run_bash", "content": f"round {i}: " + "y" * 1200})
    return msgs


def main() -> int:
    msgs = _build_msgs()
    est = int(_est(msgs))
    guard = 30000  # real prompt tokens (usage-based), ctx=40960

    cut = plan_partial_cut_index(msgs, ctx_tokens=40960, guard_tokens=guard)
    _check("cut found and behind system+user", isinstance(cut, int) and cut > 2)
    _check("old section contains condensable content",
           any(m.get("role") in ("assistant", "tool") for m in msgs[:cut]) if cut > 0 else False)

    compressed, _files, usage = asyncio.run(_compress_tool_context(
        messages=msgs, model="m", port=0, client=None, system_prompt="sys",
        original_task="t", written_files=[], done_tasks=[],
        compression_mode="partial", cut_index=cut, local_only=True,
    ))
    est_c = int(_est(compressed))
    _check("partial compression shrank >=10%",
           est_c < est * 0.90)
    _check("partial kept the raw tail", len(compressed) < len(msgs) and compressed[-1] == msgs[-1])

    # the live failure shape: cut before any tool output -> loud noop, original kept
    noop, _f2, usage2 = asyncio.run(_compress_tool_context(
        messages=msgs, model="m", port=0, client=None, system_prompt="sys",
        original_task="t", written_files=[], done_tasks=[],
        compression_mode="partial", cut_index=2, local_only=True,
    ))
    _check("empty old section -> noop marker + original kept",
           noop == msgs and isinstance(usage2, dict) and usage2.get("noop") is True)

    full, _f3, _u3 = asyncio.run(_compress_tool_context(
        messages=msgs, model="m", port=0, client=None, system_prompt="sys",
        original_task="t", written_files=[], done_tasks=[],
        compression_mode="full", cut_index=-1, local_only=True,
    ))
    _check("full mode shrinks >=10%", int(_est(full)) < est * 0.90)

    print("PASS" if _ok else "FAIL")
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
