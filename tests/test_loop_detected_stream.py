# -*- coding: utf-8 -*-
"""A5: a loop-detected stop must not look like a missing stream.

Every loop-detected break in agentic_tool_loop.py leaves the POST loop before
the normal stream bookkeeping; without dr_stream_ok the caller raised
RuntimeError and threw the result (including loop_detected and the auto-commit
signal) away. The 400-overflow path already did it correctly.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "core" / "agentic_tool_loop.py").read_text(encoding="utf-8")

sys.path.insert(0, str(ROOT))

from tests._harness import Harness  # noqa: E402

h = Harness(style="results", pad=True)
check = h.check


_hits = re.findall(
    r'result\["dr_stream_ok"\] = True[^\n]*\n\s*(?:#.*\n\s*)?result\["loop_detected"\] = True',
    SRC)
check("1 every loop-detected break marks the stream as received",
      len(_hits) == 3, f"({len(_hits)}/3)")

check("2 the overflow path still marks it",
      'result["dr_stream_ok"] = True  # Signal' in SRC)
check("3 the tail-guard hard stop is the fourth marker (it raises)",
      SRC.count('result["loop_detected"] = True') == 4)

sys.exit(h.finish())
