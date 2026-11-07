# -*- coding: utf-8 -*-
"""Run deadline + downloader 416 pins (2026-10-31).

Deadline: 900 s configured, 1056 s alive - the check ran only at round heads
and the +300 s grace was unbounded. Now the heartbeat cancels in-round and at
most one extension is granted.
Downloader: a 416 with _have < _expected means the upstream file changed; the
partial file used to be reported as "Already complete".
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests._harness import Harness  # noqa: E402

h = Harness(style="plain", pad=True)
check = h.check


_duo = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
check("in-round deadline cancel exists", "[RUN-DEADLINE]" in _duo
      and "_exec_task.cancel()" in _duo)
check("cancelled round is reported as timeout",
      "_timed_out = True" in _duo and "ps.timed_out = _timed_out" in _duo)
# P0 from the 2026-10-31 re-audit: cancel produced _exec_result=None and the
# consumer block dereferenced it outside every guard.
check("cancel path never dereferences None",
      "if _exec_result is None:" in _duo
      and "_duo_timed_out = True" in _duo)
check("cancel does not await the dead task",
      "_exec_task.cancel()" in _duo
      and "await asyncio.sleep(0)" in _duo
      and "tool round did not stop within 10s" not in _duo)

_trs = (ROOT / "core" / "tool_exec_helpers.py").read_text(encoding="utf-8")
check("grace ledger is shared per run (not per round)",
      "grace_extensions_ref" in _trs
      and '"_grace_extensions_ref", [0]' in _duo)
_te = (ROOT / "core" / "tool_executor.py").read_text(encoding="utf-8")
check("grace is capped at one extension", "_gcount >= 1" in _te
      and "one allowed" in _te)

_defs = (ROOT / "tools" / "definitions.py").read_text(encoding="utf-8")
check("edit_file schema offers confirm_shrink",
      "confirm_shrink" in _defs)

_fm = (ROOT / "deploy" / "fetch_models.py").read_text(encoding="utf-8")
check("416 with a short file is not complete",
      "upstream file changed" in _fm and "dest.unlink()" in _fm)
check("416 with an unknown size warns",
      "unknown expected size" in _fm)
check("416 with an unknown size asks the server (HEAD)",
      'method="HEAD"' in _fm)
check("throttled ctx-full round is skipped, not shipped",
      "skipping this round instead of" in _duo)

sys.exit(h.finish("deadline_and_416"))
