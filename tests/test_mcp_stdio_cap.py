# -*- coding: utf-8 -*-
"""B8: the stdio transport needs a TOTAL request cap, not only per line.

A client can split one JSON request across many lines; the old loop appended
without a bound (and re-parsed the growing buffer on every line).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "infra" / "mcp_server.py").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


check("1 the accumulated-buffer guard exists",
      "accumulated request too large" in SRC)
check("2 it is checked after each append",
      0 <= SRC.find("buf += text") < SRC.find("if len(buf.encode(\"utf-8\")) > _buf_max:"))
check("3 the cap is MCP_MAX_BODY_BYTES", "_buf_max = MCP_MAX_BODY_BYTES" in SRC)
check("4 the cap counts bytes, not characters",
      'len(text.encode("utf-8")) > _buf_max' in SRC)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
