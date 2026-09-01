"""Eval: context budgets (hive_functions/ctx_utils.py) - session budget + char caps.

Deterministic budget calculation: greedy session selection, caps/overrides,
ContextBudget tiers. No LLM needed.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from hive_functions.ctx_utils import (
    budget_session_msgs,
    compute_char_caps,
    ContextBudget,
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


# ── budget_session_msgs: greedy, newest first ─────────────────────────
msgs = [
    {"role": "user", "content": "oldest"},
    {"role": "assistant", "content": "mid " * 50},
    {"role": "user", "content": "newest"},
]
out = budget_session_msgs(msgs, budget_chars=100)
check("session: newest kept", out and out[-1]["content"] == "newest")
check("session: oldest dropped (over budget)", all(m["content"] != "oldest" for m in out))
check("session: mid dropped (over budget)", all(m["content"] != "mid " * 50 for m in out))
check("session: budget respected", all(len(m["content"]) <= 100 for m in out))
check("session: returned subset", isinstance(out, list) and len(out) <= len(msgs))

# assistant cap is applied
long_a = {"role": "assistant", "content": "x" * 9000}
out2 = budget_session_msgs([long_a], budget_chars=10 ** 9, assistant_cap=8000)
check("session: assistant capped", out2 and len(out2[0]["content"]) == 8000)

# max_msgs begrenzt
out3 = budget_session_msgs(
    [{"role": "user", "content": str(i)} for i in range(20)],
    budget_chars=10 ** 9, max_msgs=5,
)
check("session: max_msgs=5", len(out3) == 5)

# ── compute_char_caps ───────────────────────────────────────────────────
caps = compute_char_caps(8000)
check("caps: task within bounds", 800 <= caps.task <= 8000)
check("caps: session_budget within bounds", 4000 <= caps.session_budget <= 32000)
check("caps: explore_inject within bounds", 8000 <= caps.explore_inject <= 24000)

c2 = compute_char_caps(8000, overrides={"task": 5000})
check("caps: override applied", c2.task == 5000)

c3 = compute_char_caps(8000, overrides={"task": "junk"})
check("caps: invalid override ignored", c3.task == caps.task)

c4 = compute_char_caps(8000, overrides={"unknown_section": 9999})
check("caps: unknown key ignored", c4 == caps)

# ── ContextBudget.from_content_tokens Tiers ─────────────────────────────
b_tight = ContextBudget.from_content_tokens(500)
check("budget: tight tier (no snippets)", b_tight.max_snippet_files == 0 and b_tight.snippets == 0)
b_mid = ContextBudget.from_content_tokens(3000)
check("budget: mid tier", b_mid.max_snippet_files == 3)
b_comf = ContextBudget.from_content_tokens(6000)
check("budget: comfortable tier", b_comf.max_snippet_files == 6)
b_rich = ContextBudget.from_content_tokens(20000)
check("budget: rich tier", b_rich.max_snippet_files == 10)

# total property konsistent
check("budget: total == sum", b_comf.total == b_comf.plan + b_comf.contracts + b_comf.snippets + b_comf.index + b_comf.static_map)

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
