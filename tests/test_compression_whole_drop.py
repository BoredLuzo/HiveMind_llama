# -*- coding: utf-8 -*-
"""Whole-message dropping in context compression (1.5.0-preview).

Run: python tests/test_compression_whole_drop.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from context.compression import (
    _compress_rule_based,
    _compress_tool_context,
    _drop_whole_oldest,
)

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


class _FailClient:
    async def post(self, *a, **k):
        raise RuntimeError("no model for test")


def _pair(i, size=400):
    return [
        {"role": "assistant", "content": f"call {i}",
         "tool_calls": [{"id": f"c{i}", "function":
                         {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "name": "read_file", "tool_call_id": f"c{i}",
         "content": f"[C:\\ws\\f{i}.py total lines: 5]\n" + "x" * size},
    ]


def _msgs(pairs=8):
    out = [{"role": "system", "content": "SYS"},
           {"role": "user", "content": "TASK build it"}]
    for i in range(pairs):
        out.extend(_pair(i))
    return out


# ── helper: whole turns, no orphaned tool messages ────────────────────────
_ms = _msgs(4)
_out, _n, _paths = _drop_whole_oldest(_ms, 1, keep_recent=4)
check("D1: helper drops a whole turn", _n >= 2, f" n={_n}")
check("D2: helper keeps the last four messages",
      len(_out) >= 4 and _out[-4:] == _ms[-4:])
check("D3: no orphaned tool result at the tail start",
      all(_out[i].get("role") != "tool" or _out[i - 1].get("role") == "assistant"
          for i in range(1, len(_out)) if _out[i].get("role") == "tool"))
check("D4: dropped read paths are reported",
      any("f0.py" in p for p in _paths), f" got={_paths}")

# ── rule-based fallback: whole drop + marker + tail cap ───────────────────
_rule, _rule_paths = _compress_rule_based(_msgs(8), target_tokens=5,
                                          keep_recent_msgs=4,
                                          tail_tool_max_chars=100)
_rule_text = "\n".join(str(m.get("content") or "") for m in _rule)
check("R1: rule fallback drops whole old turns", len(_rule) < len(_msgs(8)))
check("R2: rule fallback announces the drop", "[CONTEXT DROPPED" in _rule_text)
check("R3: rule fallback keeps the system prompt",
      _rule and _rule[0].get("role") == "system")
check("R4: rule fallback keeps the task message",
      any(m.get("role") == "user" and "TASK build it" in str(m.get("content"))
          for m in _rule))
check("R5: tail tool cap still applies",
      any("chars dropped" in str(m.get("content") or "") for m in _rule
          if m.get("role") == "tool"))

# ── LLM-summary path: summary + raw tail, then whole-tail drop ────────────
_out2 = asyncio.run(_compress_tool_context(
    messages=_msgs(8), model="m", port=1, client=_FailClient(),
    system_prompt="SYS", original_task="TASK build it",
    written_files=[], done_tasks=[], keep_recent_msgs=8,
    target_tokens=1, tail_tool_max_chars=0,
))
_msgs2, _condensed2, _meta2 = _out2
_text2 = "\n".join(str(m.get("content") or "") for m in _msgs2)
check("L1: summary path returns system + summary + tail", len(_msgs2) >= 4,
      f" len={len(_msgs2)}")
check("L2: whole-tail drop happened", len(_msgs2) < 2 + 8,
      f" len={len(_msgs2)}")
check("L3: drop is visible in the summary block",
      "[CONTEXT DROPPED" in str(_msgs2[1].get("content") or ""))
check("L4: summary still carries the task",
      "TASK build it" in str(_msgs2[1].get("content") or ""))
check("L5: no orphaned tool message",
      all(_msgs2[i].get("role") != "tool"
          or _msgs2[i - 1].get("role") == "assistant"
          for i in range(1, len(_msgs2))))
check("L6: dropped read paths reach the read-guard set",
      any("f0.py" in p for p in _condensed2), f" got={_condensed2}")

print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
