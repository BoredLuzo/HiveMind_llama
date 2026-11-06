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


# ── 1. approval toggle is authoritative (taint does not escalate) ───────────
_saved_gate = R._approval_gate_on
try:
    R._approval_gate_on = lambda: False       # the default: approval feature off
    T.clear()
    check("1a approvals off: run_tests needs no card",
          not R._needs_approval("run_tests"))
    T.mark("web_search")
    check("1b tainted + approvals off: run_tests stays card-free",
          not R._needs_approval("run_tests")
          and not R._approval_active_for("run_tests"))
    check("1c tainted + approvals off: MCP stays card-free",
          not R._needs_approval("mcp_github")
          and not R._approval_active_for("mcp_github"))
    R._approval_gate_on = lambda: True        # the user switched approvals ON
    check("1d approvals on: run_tests and MCP are gated",
          R._approval_active_for("run_tests")
          and R._approval_active_for("mcp_github"))
    check("1e trusted reads stay ungated",
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
_saved = (C.is_pause_pending, C.signal_abort_during_pause,
          C.pause_abort_signal_pending, C._abort_event)
try:
    C.is_pause_pending = lambda run_id: True
    C.pause_abort_signal_pending = lambda run_id: False   # no manual-pause signal
    C.signal_abort_during_pause = lambda run_id: False
    C._abort_event = lambda run_id: _ev
    _res = asyncio.run(C.abort_run("run-1"))
finally:
    (C.is_pause_pending, C.signal_abort_during_pause,
     C.pause_abort_signal_pending, C._abort_event) = _saved
check("2a abort during approval pause falls back to the abort event",
      _res.get("ok") is True and _res.get("action") == "abort" and _ev.set_called,
      str(_res))

_ev2 = _Ev()
_saved = (C.is_pause_pending, C.signal_abort_during_pause,
          C.pause_abort_signal_pending, C._abort_event)
try:
    C.is_pause_pending = lambda run_id: True
    C.pause_abort_signal_pending = lambda run_id: True    # manual pause, unset signal
    C.signal_abort_during_pause = lambda run_id: True
    C._abort_event = lambda run_id: _ev2
    _res2 = asyncio.run(C.abort_run("run-2"))
finally:
    (C.is_pause_pending, C.signal_abort_during_pause,
     C.pause_abort_signal_pending, C._abort_event) = _saved
check("2b manual pause still uses the pause signal",
      _res2.get("action") == "abort_during_pause" and not _ev2.set_called,
      str(_res2))

# 2c: a signal left over from an EARLIER manual pause is already set; the
# helper must report "no pending manual pause" so /abort falls through.
from infra import run_control as RC       # noqa: E402
_rc_ev = asyncio.Event()
RC._ABORT_DURING_PAUSE_SIGNALS["stale-run"] = _rc_ev
check("2c an unset manual-pause signal is pending",
      RC.pause_abort_signal_pending("stale-run") is True)
_rc_ev.set()
check("2d a stale (already set) pause signal is not pending",
      RC.pause_abort_signal_pending("stale-run") is False)
RC._ABORT_DURING_PAUSE_SIGNALS.pop("stale-run", None)


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
check("3e a healthy round resets the SHARED counter",
      "self.round_state.tail_guard_fires = 0" in ATL)


# ── 4. repo-map delta survives the compression that follows it ──────────────
_i_append = DQ.find("append_repo_map_delta")
_i_recap = DQ.find("_delta_sys", _i_append)
check("4a the delta is appended before compression",
      _i_append > 0 and "append_repo_map_delta" in DQ)
check("4b the compression system prompt is re-captured after the append",
      _i_recap > _i_append and "_sys_for_compress = str(_delta_sys)" in DQ)
check("4c the compression KWARGS carry the updated system prompt",
      '_comp_kwargs["system_prompt"] = _sys_for_compress' in DQ)

# ── 4d. skipped-planner runs still save the sidecar (UnboundLocal fix) ──────
# 4d (split 2.2): the "_inloop_plan_text or" consumer moved to the epilogue
# 4d (split 2.2): the "_inloop_plan_text or" consumer moved to the epilogue
EPI4D = (ROOT / "core" / "duo_epilogue.py").read_text(encoding="utf-8", errors="replace")
check("4d the in-loop plan text is initialized before the planner",
      '_inloop_plan_text = ""' in DQ
      and "_inloop_plan_text or" in EPI4D)
# 4e/f (split 2.2): the sidecar save moved to core/duo_epilogue.py — the
# runner keeps the session-write point, the epilogue owns the context save.
EPI = (ROOT / "core" / "duo_epilogue.py").read_text(encoding="utf-8", errors="replace")
check("4e aborted runs still save last_run/task context (epilogue)",
      "ctx.chat_id and not ctx.is_aborted_chat" not in DQ
      and '_eff_stop = "aborted"' in EPI)
check("4f the sidecar session is updated after the assistant turn",
      "_mutate_chat_context(" in DQ
      and DQ.index('add_to_session("assistant"') < DQ.index("_mutate_chat_context(")
      and "_mutate_chat_context(" in EPI)


# ── 5. writes without any execution go through the verify ladder ────────────
check("5a the no-bash branch routes through the ladder",
      "VERIFY REQUIRED] Code was changed this run" in TE
      and "_task_complete_ladder(" in TE.split("elif _last_bash_idx == -1:", 1)[-1][:1200])
check("5b an unset deadline is not treated as expired",
      "if trs.duo_deadline_at and time.monotonic() >= trs.duo_deadline_at:" in TE)

# 5c: ladder semantics — "block" must keep the loop going (first two tries),
# the exhausted third try accepts. The no-bash branch must not convert a
# "block" into an immediate acceptance.
from core.tool_executor import _task_complete_ladder   # noqa: E402


class _LadderTRS:
    task_complete_blocked_count = [0]


class _LadderResult:
    task_complete_called = False


_msgs: list = []
_res_obj = _LadderResult()
_g1 = _task_complete_ladder(_LadderTRS(), _msgs, _res_obj, False, "please verify")
check("5c the first ladder hit blocks without accepting",
      _g1 == "block" and not _res_obj.task_complete_called and len(_msgs) == 1)
_g2 = _task_complete_ladder(_LadderTRS(), _msgs, _res_obj, False, "please verify")
check("5d the second ladder hit still blocks", _g2 == "block")
_g3 = _task_complete_ladder(_LadderTRS(), _msgs, _res_obj, False, "please verify")
check("5e the third ladder hit accepts", _g3 == "allow" and _res_obj.task_complete_called)


# ── 6. terminal stream frames + browser-side gap marker ─────────────────────
check("6a the done frame bypasses the queue cap",
      "DONE-ALWAYS" in SRV and 'elif isinstance(_chunk, str) and \'"type": "done"\' in _chunk:' in SRV)
check("6b the server emits a stream_gap frame", '"type": "stream_gap"' in SRV)
check("6c the web UI handles stream_gap", "d.type === 'stream_gap'" in APPJS)
check("6d the journal carries and filters the chat id",
      '"chat_id": str(reg.get("chat_id")' in SRV
      and "j.chat_id && (!S.currentChatId" in APPJS)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
