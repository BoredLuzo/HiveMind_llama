# -*- coding: utf-8 -*-
"""P1 (edge audit 2026-11-03): Telegram counts UTF-16 units.

A 3000-emoji answer has len() 3000 but 6000 UTF-16 units - the splitter kept
it as one chunk, Telegram rejected it and the whole answer was lost.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hivemind_gateway.render import split_message, _tg_len   # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


LIMIT = 4096
emoji = "\U0001f600" * 3000
chunks = split_message(emoji, LIMIT)
check("1 a long emoji answer is split", len(chunks) > 1, f"({len(chunks)} chunks)")
check("2 every chunk fits the UTF-16 limit",
      all(_tg_len(c) <= LIMIT for c in chunks),
      f"(max {max(_tg_len(c) for c in chunks)})")
check("3 nothing is lost", "".join(chunks) == emoji)

ascii_text = "x" * 9000
parts = split_message(ascii_text, LIMIT)
check("4 plain ASCII still splits", all(len(p) <= LIMIT for p in parts)
      and "".join(parts) == ascii_text)

fenced = "```python\n" + ("print('x')\n" * 900) + "```"
parts = split_message(fenced, LIMIT)
check("5 fences stay balanced", all(_tg_len(p) <= LIMIT for p in parts)
      and all(p.count("```") % 2 == 0 for p in parts),
      f"({len(parts)} chunks)")

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
