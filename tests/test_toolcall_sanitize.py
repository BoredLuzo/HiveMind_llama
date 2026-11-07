# -*- coding: utf-8 -*-
"""Tests: tool-call history sanitizer (core/toolcall_sanitize.py).

Run: python tests/test_toolcall_sanitize.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.toolcall_sanitize import sanitize_invalid_tool_call_history as _san  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def tc(cid, args):
    return {"id": cid, "type": "function", "function": {"name": "write_file", "arguments": args}}


# valid history -> untouched, 0 removed
msgs_ok = [
    {"role": "system", "content": "s"},
    {"role": "assistant", "content": "", "tool_calls": [tc("c1", '{"path":"a.js","content":"x"}')]},
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
]
check("S1: valid history untouched", _san(msgs_ok) == 0)
check("S2: valid messages preserved", len(msgs_ok) == 3)

# truncated JSON args (missing closing quote) -> removed + orphan tool dropped
bad = '{"path":"ghosts.js","content":"// ghosts.js ... const GHOST_STATES = { SCATTER: \'SCATTER\','
msgs_bad = [
    {"role": "system", "content": "s"},
    {"role": "assistant", "content": "", "tool_calls": [tc("c9", bad)]},
    {"role": "tool", "tool_call_id": "c9", "content": "partial"},
    {"role": "user", "content": "keep me"},
]
n = _san(msgs_bad)
check("S3: malformed args removed", n == 1, f" got={n}")
roles = [m.get("role") for m in msgs_bad]
check("S4: orphan tool result dropped", "tool" not in roles, f" roles={roles}")
check("S5: surrounding messages kept + notice appended",
      roles == ["system", "assistant", "user", "user"], f" roles={roles}")
last = str(msgs_bad[-1].get("content") or "")
check("S9: notice names tool + path", "write_file" in last and "ghosts.js" in last,
      f" last={last[:120]!r}")

# list identity preserved (in-place slice)
_ref = msgs_bad
_same = _san(msgs_bad)
check("S6: in-place mutation", msgs_bad is _ref)

# dict/list arguments are fine (already parsed)
msgs_obj = [
    {"role": "assistant", "content": "", "tool_calls": [tc("c7", {"path": "x", "content": 1})]},
]
check("S7: object arguments fine", _san(msgs_obj) == 0)

# empty input
check("S8: empty input", _san([]) == 0 and _san(None) == 0)

# suffix scan (2026-11-07): a high-water mark skips the already-clean prefix
msgs_suffix = [
    {"role": "assistant", "content": "", "tool_calls": [tc("old", bad)]},
    {"role": "user", "content": "x"},
    {"role": "assistant", "content": "", "tool_calls": [tc("new", '{"path":"ok.js"}')]},
]
check("S10: start= skips the sanitized prefix",
      _san(msgs_suffix, start=2) == 0 and len(msgs_suffix) == 3)

# pin the two reset points in the runner
_src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
check("S11: runner passes its high-water mark",
      "start=_sanitize_upto" in _src)
check("S12: compression resets the high-water mark",
      re.search(r"_dtool_msgs = _cc_ps\._dtool_msgs\s*\n\s*_sanitize_upto = 0",
                _src) is not None)

sys.exit(h.finish("toolcall_sanitize"))
