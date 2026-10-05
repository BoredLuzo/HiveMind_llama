"""Gateway WP1: render (pure) — splitting at line boundaries, balanced
code fences, secret filtering. Full output behavior is wired+tested in
WP2; this pins the pure function contracts now.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hivemind_gateway.render import (
    TELEGRAM_HARD_LIMIT,
    escape_html,
    filter_secrets,
    split_message,
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


# ── splitting ───────────────────────────────────────────────────────────
check("short text single chunk", split_message("hello") == ["hello"])
check("empty -> no chunks", split_message("") == [])

lines = "\n".join(f"line {i} " + "x" * 20 for i in range(400))
chunks = split_message(lines)
check("long text split", len(chunks) > 1)
check("all chunks under limit", all(len(c) <= TELEGRAM_HARD_LIMIT for c in chunks))
check("content ends preserved",
      chunks[0].startswith("line 0") and "line 399" in chunks[-1])

# line boundaries: chunks end at newlines (except fence-closers)
multi = "\n".join(f"row-{i}" for i in range(1000))
ch2 = split_message(multi)
check("splits at line boundaries",
      all(c.endswith(tuple("0123456789") + ("```",)) for c in ch2))

# fences balanced across chunk borders
code_text = "intro\n```python\n" + "\n".join(f"x{i} = {i}" for i in range(400)) + "\n```\nend"
ch3 = split_message(code_text)
if len(ch3) > 1:
    check("fences balanced per chunk",
          all(c.count("```") % 2 == 0 for c in ch3), f" (chunks={len(ch3)})")
else:
    check("fences balanced per chunk", code_text.count("```") % 2 == 0,
          " (single chunk)")

# oversized single line hard-split, nothing lost
huge = "y" * 10_000
ch4 = split_message(huge)
check("single oversized line preserved",
      "".join(ch4) == huge and all(len(c) <= TELEGRAM_HARD_LIMIT for c in ch4))

# ── secrets ─────────────────────────────────────────────────────────────
s = filter_secrets("call bot123456789:AAHHHHHHHHHHHHHHHHHHHHHHHHH now")
check("bot token url filtered", "AAHHHHHHHHHHHHHHHHHHHHHHHHH" not in s)
s2 = filter_secrets("key sk-abcdefghijklmnopqrstuvwx end")
check("sk- key filtered", "sk-abcdefghijklmnopqrstuvwx" not in s2)
s3 = filter_secrets("OPENAI_API_KEY=sk-secret123\nnormal=line\nMY_TOKEN=abc123")
check("env-style token lines masked", "sk-secret123" not in s3 and "abc123" not in s3
      and "OPENAI_API_KEY=***" in s3 and "normal=line" in s3)
s4 = filter_secrets("-----BEGIN RSA PRIVATE KEY-----")
check("private key header filtered", "RSA PRIVATE KEY" not in s4)

# ── escape (HTML path only) ─────────────────────────────────────────────
check("escape_html basics", escape_html("<b>&x") == "&lt;b&gt;&amp;x")

print()
print(f"passed={passed} failed={failed}")
sys.exit(0 if failed == 0 else 1)

# F3 (2026-10-05): markdown -> Telegram HTML for run answers.\n# Escape FIRST, then translate only COMPLETE constructs.\nfrom hivemind_gateway.render import md_to_telegram_html as _md\n\n_h = _md("vor\n```html\n<b>x</b>\n```\n`c` und **f** <att>")\ncheck("md: fence -> <pre> with escaped body",\n      "<pre>" in _h and "&lt;b&gt;x&lt;/b&gt;" in _h)\ncheck("md: inline code", "<code>c</code>" in _h)\ncheck("md: bold", "<b>f</b>" in _h)\ncheck("md: raw angle brackets escaped", "&lt;att&gt;" in _h)\n_h2 = _md("```js\nnope")\ncheck("md: incomplete fence stays escaped plain",\n      "<pre>" not in _h2 and "nope" in _h2)\n\nprint()
print(f"passed={passed} failed={failed}")
sys.exit(0 if failed == 0 else 1)
