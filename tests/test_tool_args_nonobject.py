# -*- coding: utf-8 -*-
"""A6: tool arguments must always be a dict.

Valid non-object JSON ("5", "null", "[]") used to pass through and the caller
then did {**args, ...} -> TypeError, killing the whole tool round.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.tool import parse_tool_args      # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


for raw in ("5", "null", "[]", '"text"', "true", "3.14"):
    check(f"1 {raw!r} becomes an empty dict", parse_tool_args(raw) == {},
          repr(parse_tool_args(raw)))

check("2 a JSON object passes through",
      parse_tool_args('{"path": "a.txt"}') == {"path": "a.txt"})
check("3 a dict passes through", parse_tool_args({"x": 1}) == {"x": 1})
check("4 a list is rejected", parse_tool_args([1, 2]) == {})
check("5 invalid JSON stays empty", parse_tool_args("{nope") == {})
check("6 Windows backslashes are still repaired",
      parse_tool_args(r'{"path": "C:\Users\me"}') == {"path": "C:\\Users\\me"})

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
