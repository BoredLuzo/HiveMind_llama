# -*- coding: utf-8 -*-
"""Side-call empty-content fallback (2026-10-30).

Measured: LFM2.5 2.6B ignores `chat_template_kwargs.enable_thinking=false`,
spends all 800 max_tokens in the think block and returns
`content = ""` with `reasoning_content` filled (125+ chars). Side calls
(summary, title, memory) would receive an empty string and silently produce
nothing.

Fix: `LlamaClient.chat()` falls back to the reasoning tail (last 1500 chars)
when content is empty, and logs `[SIDE-FALLBACK]`. `core/fix_agent.py` has
its own POST and carries the same fallback.

This test exercises the decision logic without a model (the response dict is
fed straight into the same branch).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def pick(msg, limit=1500):
    """Mirror of the branch in llama_client.chat / fix_agent."""
    content = msg.get("content") or ""
    if content.strip():
        return content, False
    reason = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    if reason:
        return reason[-limit:], True
    return "", False


# 1. the measured LFM2.5 shape: empty content, reasoning present
content, used = pick({"content": "", "reasoning_content": "x" * 300})
check("empty content + reasoning -> reasoning used", used and len(content) == 300)
check("content alone wins when present",
      pick({"content": "real", "reasoning_content": "x" * 50}) == ("real", False))
check("whitespace-only content counts as empty",
      pick({"content": "   ", "reasoning_content": "y" * 10})[1] is True)
check("reasoning under the alias 'reasoning' also works",
      pick({"content": "", "reasoning": "z" * 20})[1] is True)
check("nothing at all -> empty, no crash", pick({"content": ""}) == ("", False))
check("long reasoning is capped at 1500 chars",
      len(pick({"content": "", "reasoning_content": "q" * 5000})[0]) == 1500)
check("the CAP keeps the TAIL (last chars)", 
      pick({"content": "", "reasoning_content": "A" * 1600 + "TAIL"})[0].endswith("TAIL"))

# 2. wiring: both call sites carry the fallback
llc = (ROOT / "backend" / "llama_client.py").read_text(encoding="utf-8")
fa = (ROOT / "core" / "fix_agent.py").read_text(encoding="utf-8")
check("llama_client: fallback wired", "[SIDE-FALLBACK]" in llc and "reasoning_content" in llc)
check("llama_client: logs the model and the char count",
      "using %d chars of reasoning" in llc)
check("fix_agent: fallback wired", "reasoning_content" in fa)
check("fix_agent: no bare content-only return left",
      not re.search(r"final_text = \(msg\.get\(\"content\"\) or \"\"\)\.strip\(\)\n\s*if not tcs",
                    fa))

print(f"\nside_fallback: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
