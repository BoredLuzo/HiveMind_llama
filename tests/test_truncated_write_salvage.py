# -*- coding: utf-8 -*-
"""Behavior test: discarding write_file calls cut at the output limit (2026-11-09).

Never write a truncation marker into a file on disk. Truncated write_file /
write_file_append calls are discarded from validated_tcs and reported via a
[WRITE-TRUNCATED] notice in drop_notices; non-write calls stay untouched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.truncated_write_salvage import salvage_truncated_writes  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

_tcs = [
    {"function": {"name": "write_file",
                  "arguments": json.dumps({"path": "a.py", "content": "x"})}},
    {"function": {"name": "read_file", "arguments": '{"path": "b.py"}'}},
    {"function": {"name": "write_file_append",
                  "arguments": {"path": "c.py", "content": "y"}}},
]
_notices: list[str] = []
_paths = salvage_truncated_writes(_tcs, _notices)
check("both write calls are discarded and paths returned",
      _paths == ["c.py", "a.py"], f"({_paths})")
check("only non-write call remains in validated_tcs",
      len(_tcs) == 1 and _tcs[0]["function"]["name"] == "read_file",
      f"({_tcs})")
check("a single [WRITE-TRUNCATED] notice names both paths and has no file marker",
      len(_notices) == 1
      and "[WRITE-TRUNCATED]" in _notices[0]
      and "a.py" in _notices[0]
      and "c.py" in _notices[0]
      and "nothing was written" in _notices[0]
      and "TRUNCATION_MARKER" not in _notices[0])

_bad = [{"function": {"name": "write_file", "arguments": "{not json"}}]
_notices2: list[str] = []
_paths2 = salvage_truncated_writes(_bad, _notices2)
check("unparseable write call is dropped", _paths2 == [] and _bad == [])
check("nothing-written [WRITE-TRUNCATED] notice is explicit",
      len(_notices2) == 1
      and "[WRITE-TRUNCATED]" in _notices2[0]
      and "nothing was written" in _notices2[0])

_only_read = [{"function": {"name": "read_file", "arguments": '{"path": "x"}'}}]
_notices3: list[str] = []
_paths3 = salvage_truncated_writes(_only_read, _notices3)
check("non-write calls are untouched",
      _paths3 == []
      and _only_read[0]["function"]["arguments"] == '{"path": "x"}'
      and _notices3 == [])

sys.exit(h.finish("truncated_write_salvage"))
