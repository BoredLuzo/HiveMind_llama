# -*- coding: utf-8 -*-
"""Live 2026-11-03: "Create note.txt ..." was hijacked as a memory command.

_is_memory_request matched the bare words "note" and "store" with word
boundaries; a short coding task mentioning note.txt returned memory_request
instead of running. The command forms are now phrases.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hive_functions.pipeline import Pipeline     # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


class Dummy:
    _MEMORY_CMD_MAX_CHARS = Pipeline._MEMORY_CMD_MAX_CHARS

    def _is_forget_request(self, t):
        return Pipeline._is_forget_request(self, t)


d = Dummy()


def is_mem(text):
    return Pipeline._is_memory_request(d, text)


check("1 a note.txt task is NOT a memory command",
      not is_mem("Create note.txt containing exactly the word alpha. "
                 "Then task_complete."))
check("2 'note that ...' is still a memory command",
      is_mem("Note that my editor is vim"))
check("3 'remember ...' still works", is_mem("Remember my name is Alex"))
check("4 'store this' still works", is_mem("Store this: my key is 123"))
check("5 a task with 'store' is not hijacked",
      not is_mem("Store one piece of candy in the box"))
check("6 a long task is never a memory command",
      not is_mem("Remember to write the parser. " * 8))
check("7 German command forms still work", is_mem("Merke dir: ich nutze vim"))
check("8 'note: ...' is still a memory command", is_mem("note: I prefer dark themes"))
check("9 'note this down' is still a memory command", is_mem("note this down: vim"))

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
