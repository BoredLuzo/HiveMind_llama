# -*- coding: utf-8 -*-
"""Side-call empty-content fallback (2026-10-30).

Measured: LFM2.5 2.6B ignores `chat_template_kwargs.enable_thinking=false`,
spends all 800 max_tokens in the think block and returns
`content = ""` with `reasoning_content` filled (125+ chars). Side calls
(summary, title, memory) would receive an empty string and silently produce
nothing.

Policy (2026-10-30, revised): reasoning is NOT substituted. Titles, memory
writes and insights persist what they get, and a think-block fragment must
never become stored knowledge. `LlamaClient.chat()` logs `[SIDE-SKIP]` and
returns "" when content is empty; `core/fix_agent.py` does the same. The
compression path has its own local fallback summary for an empty answer.

This test exercises the decision logic without a model (the response dict is
fed straight into the same branch).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="plain")
check = h.check

def pick(msg):
    """Mirror of the branch in llama_client.chat / fix_agent (skip policy)."""
    content = (msg.get("content") or "").strip()
    if content:
        return content, False
    reason = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    return "", bool(reason)   # (result, would_skip_reasoning)


# 1. the measured LFM2.5 shape: empty content, reasoning present
content, skipped = pick({"content": "", "reasoning_content": "x" * 300})
check("empty content + reasoning -> EMPTY result (not persisted)",
      content == "" and skipped)
check("content alone wins when present",
      pick({"content": "real", "reasoning_content": "x" * 50}) == ("real", False))
check("whitespace-only content counts as empty and is skipped",
      pick({"content": "   ", "reasoning_content": "y" * 10})[1] is True)
check("alias 'reasoning' is recognised for the skip log",
      pick({"content": "", "reasoning": "z" * 20})[1] is True)
check("nothing at all -> empty, no crash", pick({"content": ""}) == ("", False))
check("no reasoning chars leak into the result",
      pick({"content": "", "reasoning_content": "SECRET" * 100})[0] == "")

# 2. wiring: both call sites carry the fallback
llc = (ROOT / "backend" / "llama_client.py").read_text(encoding="utf-8")
fa = (ROOT / "core" / "fix_agent.py").read_text(encoding="utf-8")
check("llama_client: SIDE-SKIP wired", "[SIDE-SKIP]" in llc and "reasoning_content" in llc)
check("llama_client: returns '' on empty content", 'return ""' in llc)
check("llama_client: no reasoning substitution left", "SIDE-FALLBACK" not in llc)
check("fix_agent: SIDE-SKIP wired", "[SIDE-SKIP]" in fa)
check("fix_agent: no reasoning substitution left",
      "final_text = ((msg.get" not in fa)
check("fix_agent: no bare content-only return left",
      not re.search(r"final_text = \(msg\.get\(\"content\"\) or \"\"\)\.strip\(\)\n\s*if not tcs",
                    fa))

sys.exit(h.finish("side_fallback"))
