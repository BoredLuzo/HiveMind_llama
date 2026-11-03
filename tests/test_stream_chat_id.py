# -*- coding: utf-8 -*-
"""B1: /stream must validate chat_id before it reaches file paths.

routers/chats.py pins [a-f0-9-]{4,64}; /stream passed the raw value into
glob(f"*_{chat_id}.json") and standalone_{chat_id}.context.json, so "*" or
"../.." could touch a foreign chat or leave the sessions directory.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "server.py").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


check("1 the shape check exists",
      're.fullmatch(r"[a-f0-9-]{4,64}", str(chat_id))' in SRC)
_idx_check = SRC.find('re.fullmatch(r"[a-f0-9-]{4,64}", str(chat_id))')
_idx_use = SRC.find("import_dom_messages", _idx_check)
check("2 it runs before the dom import", 0 <= _idx_check < _idx_use)
check("3 it returns a client error", '{"detail": "invalid chat_id"}' in SRC)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
