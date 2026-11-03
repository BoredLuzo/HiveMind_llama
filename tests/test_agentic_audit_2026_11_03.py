# -*- coding: utf-8 -*-
"""Agentic-engine deep audit (2026-11-03): regressions from the audit.

P1: run_tests and mutating MCP tools bypassed the taint gate; /abort during
an approval pause reported success without stopping the run. P2: repo-map
delta was discarded by the compression that followed; tail/decode guards
reset every round. These checks exercise the decisions, not just substrings.
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import taint as T          # noqa: E402
import tools.runner as R              # noqa: E402
from routers import core as C         # noqa: E402
from core.agentic_duo_state import DuoRoundState   # noqa: E402

DQ = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
ATL = (ROOT / "core" / "agentic_tool_loop.py").read_text(encoding="utf-8")
TE = (ROOT / "core" / "tool_executor.py").read_text(encoding="utf-8")
SRV = (ROOT / "server.py").read_text(encoding="utf-8")
APPJS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── 1. taint gate covers run_tests and dynamic MCP tools ────────────────────
_saved_gate = R._approval_gate_on
try:
    R._approval_gate_on = lambda: False       # the default: approval feature off
    T.clear()
    check("1a clean run: run_tests needs no card",
          not R._needs_approval("run_tests"))
    T.mark("web_search")
    check("1b tainted run: run_tests is gated",
          R._needs_approval("run_tests") and R._approval_active_for("run_tests"))
    check("1c tainted run: dynamic MCP tools are gated",
          R._needs_approval("mcp_github")
          and R._approval_active_for("mcp_github"))
    check("1d trusted reads stay ungated",
          not R._approval_active_for("read_file"))
finally:
    R._approval_gate_on = _saved_gate
    T.clear()


# ── 2. /abort during an approval pause sets the run abort event ─────────────
class _Ev:
    def __init__(self):
        self.set_called = False

    def set(self):
        self.set_called = True


_ev = _Ev()
_saved = (C.is_pause_pending, C.signal_abort_during_pause, C._abort_event)
try:
    C.is_pause_pending = lambda run_id: True
    C.signal_abort_during_pause = lambda run_id: False   # approval pause: no manual signal
    C._abort_event = lambda run_id: _ev
    _res = asyncio.run(C.abort_run("run-1"))
finally:
    C.is_pause_pending, C.signal_abort_during_pause, C._abort_event = _saved
check("2a abort during approval pause falls back to the abort event",
      _res.get("ok") is True and _res.get("action") == "abort" and _ev.set_called,
      str(_res))

_ev2 = _Ev()
_saved = (C.is_pause_pending, C.signal_abort_during_pause, C._abort_event)
try:
    C.is_pause_pending = lambda run_id: True
    C.signal_abort_during_pause = lambda run_id: True    # manual pause: signal exists
    C._abort_event = lambda run_id: _ev2
    _res2 = asyncio.run(C.abort_run("run-2"))
finally:
    C.is_pause_pending, C.signal_abort_during_pause, C._abort_event = _saved
check("2b manual pause still uses the pause signal",
      _res2.get("action") == "abort_during_pause" and not _ev2.set_called,
      str(_res2))


# ── 3. loop guards live on the run-shared state ─────────────────────────────
_st = DuoRoundState()
check("3a round state carries the tail guard counter",
      hasattr(_st, "tail_guard_fires") and _st.tail_guard_fires == 0)
check("3b round state carries the decode history",
      hasattr(_st, "decode_hist") and _st.decode_hist == [])
check("3c the loop writes the counter to round_state",
      "round_state.tail_guard_fires" in ATL or "round_state, \"tail_guard_fires\"" in ATL)
check("3d the decode watch reads/writes round_state",
      'round_state, "decode_hist"' in ATL)


# ── 4. repo-map delta survives the compression that follows it ──────────────
_i_append = DQ.find("append_repo_map_delta")
_i_recap = DQ.find("_delta_sys", _i_append)
check("4a the delta is appended before compression",
      _i_append > 0 and "append_repo_map_delta" in DQ)
check("4b the compression system prompt is re-captured after the append",
      _i_recap > _i_append and "_sys_for_compress = str(_delta_sys)" in DQ)


# ── 5. writes without any execution go through the verify ladder ────────────
check("5a the no-bash branch routes through the ladder",
      "VERIFY REQUIRED] Code was changed this run" in TE
      and "_task_complete_ladder(" in TE.split("elif _last_bash_idx == -1:", 1)[-1][:1200])
check("5b an unset deadline is not treated as expired",
      "if trs.duo_deadline_at and time.monotonic() >= trs.duo_deadline_at:" in TE)


# ── 6. terminal stream frames + browser-side gap marker ─────────────────────
check("6a the done frame bypasses the queue cap",
      "DONE-ALWAYS" in SRV and 'elif isinstance(_chunk, str) and \'"type": "done"\' in _chunk:' in SRV)
check("6b the server emits a stream_gap frame", '"type": "stream_gap"' in SRV)
check("6c the web UI handles stream_gap", "d.type === 'stream_gap'" in APPJS)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
