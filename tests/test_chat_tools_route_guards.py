# -*- coding: utf-8 -*-
"""A4 behavior: legacy tool-route prompt/round guard (2026-11-07).

The route itself needs a model, but the pieces that fixed the max-round loop
are pure and covered here:
  * _tool_system_prompt: shared direct-tools note + explicit stop rule
  * _tool_signatures: stable name/arguments keys
  * _repeat_guard: one nudge for an identical repeat, none for new calls
  * _tool_round_limit: direct_tools_max_rounds clamped like direct_runner
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.chat_tools_route import (  # noqa: E402
    _repeat_guard, _tool_round_limit, _tool_signatures, _tool_system_prompt,
)
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

# ── prompt parity ──────────────────────────────────────────────────────────
_prompt = _tool_system_prompt(False)
check("prompt: shares the direct-tools advertisement",
      "TOOLS ARE AVAILABLE" in _prompt, f" ({_prompt[:60]!r})")
check("prompt: carries the stop rule", "STOP RULE" in _prompt)
check("prompt: stop rule forbids another call when done",
      "do NOT call another tool" in _prompt)
check("prompt: websearch variant still has the rule",
      "STOP RULE" in _tool_system_prompt(True))

# ── signatures ─────────────────────────────────────────────────────────────
_msg = {"tool_calls": [{"function": {"name": "list_dir",
                                     "arguments": '{"path": "."}'}}]}
check("signatures: string args kept verbatim",
      _tool_signatures(_msg) == ['list_dir:{"path": "."}'],
      f" ({_tool_signatures(_msg)})")
_dict_msg = {"tool_calls": [{"function": {"name": "read_file",
                                          "arguments": {"b": 1, "path": "x"}}}]}
check("signatures: dict args are stable JSON",
      _tool_signatures(_dict_msg) == ['read_file:{"b": 1, "path": "x"}'],
      f" ({_tool_signatures(_dict_msg)})")
check("signatures: no tool calls -> empty", _tool_signatures({}) == [])
check("signatures: nameless call is ignored",
      _tool_signatures({"tool_calls": [{"function": {"arguments": "{}"}}]}) == [])

# ── repeat guard ───────────────────────────────────────────────────────────
_msgs = []
_guard = _repeat_guard()
asyncio.run(_guard(_msg, _msgs, 0, None))
check("guard: first call is not nudged", _msgs == [], f" ({_msgs})")
asyncio.run(_guard(_msg, _msgs, 1, None))
check("guard: identical second call gets one nudge", len(_msgs) == 1,
      f" ({_msgs})")
check("guard: nudge explains the repeat",
      _msgs and "already called this tool" in _msgs[0]["content"])
asyncio.run(_guard(_msg, _msgs, 2, None))
check("guard: third identical call does not stack nudges", len(_msgs) == 1,
      f" ({len(_msgs)} messages)")
_other = {"tool_calls": [{"function": {"name": "list_dir",
                                       "arguments": '{"path": "src"}'}}]}
asyncio.run(_guard(_other, _msgs, 3, None))
check("guard: different args are a new call", len(_msgs) == 1,
      f" ({len(_msgs)} messages)")
asyncio.run(_guard({"role": "assistant", "content": "done"}, _msgs, 4, None))
check("guard: final answer without tools never nudges", len(_msgs) == 1)

# ── round limit ────────────────────────────────────────────────────────────
check("rounds: default is 12", _tool_round_limit({}) == 12)
check("rounds: setting is honoured",
      _tool_round_limit({"direct_tools_max_rounds": 7}) == 7)
check("rounds: zero clamps to 1",
      _tool_round_limit({"direct_tools_max_rounds": 0}) == 1)
check("rounds: huge value clamps to 300",
      _tool_round_limit({"direct_tools_max_rounds": 9999}) == 300)
check("rounds: junk falls back to 12",
      _tool_round_limit({"direct_tools_max_rounds": "nope"}) == 12)

sys.exit(h.finish("chat_tools_route_guards"))
