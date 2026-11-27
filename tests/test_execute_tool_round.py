"""Eval: execute_tool_round (core/tool_executor.py) - Kernpfade.

Sichert den Refactor (Orchestrator + _RoundState) ab: gleiche Ergebnisse
vorher/nachher. Kein LLM, kein echtes Subprocess - _run_inline_tool gemockt.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.tool_executor as TE
from core.tool_exec_helpers import ToolRoundState
from core.agentic_duo_state import DuoRoundState

from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


# ── Mocks ───────────────────────────────────────────────────────────────
_HANDLERS: dict = {}


async def _fake_run_inline(name, args, workspace_lock=None, tool_mode=None, include_websearch=False):
    h = _HANDLERS.get(name)
    if h is None:
        return ""
    if callable(h):
        return h(name, args)
    return h


TE._run_inline_tool = _fake_run_inline
try:
    import core.tool_exec_helpers as _helpers
    _helpers._run_inline_tool = _fake_run_inline
except ImportError:
    pass


class _LRU:
    default_ttl = 600

    def __init__(self):
        self.registered = []

    def decay(self, path):
        pass

    def register(self, idx, **kw):
        self.registered.append((idx, kw))


class _Hooks:
    def __init__(self):
        self.events = []

    async def emit(self, ev):
        self.events.append(ev)

    def is_aborted(self, chat_id):
        return False

    async def on_tool_result(self, name, args, result):
        return None

    async def remember_insight(self, *a, **k):
        pass

    async def evict_model(self, model):
        pass


def run_round(tool_calls, dtool_msgs=None, round_state=None, **over):
    hooks = _Hooks()
    msgs = dtool_msgs if dtool_msgs is not None else []
    slot_defaults = dict(
        tool_ctx_lru=_LRU(),
        duo_deadline_at=time.monotonic() + 60,
        verify_mutation_serial=0, verify_last_ok_serial=0,
        last_run_bash_failure=None, changed_since_failure=set(),
        last_learned_insight_sig="", last_too_large_path=[None],
        attempts_per_file={}, tool_error_retries={}, call_sigs=[],
        recent_focus_paths=[], file_changes={}, duo_seen_web_queries=set(),
        cached_coder_port=[None], task_complete_blocked_count=None, total_tool_errors=None,
        allow_send_file=False,
    )
    trs = over.pop("trs", None)
    if trs is None:
        slot_kw = {k: over.pop(k, v) for k, v in slot_defaults.items()}
        trs = ToolRoundState(**slot_kw)
    kw = dict(
        tool_mode="tool_agent", duo_ws=False, workspace_lock=str(Path.cwd()),
        exec_model="m:9b", auto_test_before_complete=False,
        exec_has_thinking=True, tool_think_auto_mode="on_fail",
        run_id_global="eval", chat_id="eval_chat", subtask_index=0,
        _MAX_FOCUS_PATHS=5,
    )
    kw.update(over)
    rs = round_state if round_state is not None else DuoRoundState()
    res = asyncio.run(TE.execute_tool_round(
        tool_calls=tool_calls, dtool_msgs=msgs, round_state=rs, hooks=hooks, trs=trs, **kw))
    run_round.last_trs = trs  # test hook: grace tests need trs.duo_deadline_at
    return res, hooks, msgs, rs, trs.duo_seen_web_queries, trs.last_too_large_path


def tc(name, args=None, raw_args=None):
    return {"function": {"name": name, "arguments": raw_args if raw_args is not None else (args or {})}, "id": name}


# ── 1. Erfolgspfad ──────────────────────────────────────────────────────
_HANDLERS["read_file"] = "content of x.py"
res, hooks, msgs, rs, seen, ltl = run_round([tc("read_file", {"path": "x.py"})])
check("success last_tool_name", res.last_tool_name == "read_file")
check("success last_tool_result", "content of x.py" in res.last_tool_result)
check("success tool_result event", any(e.get("type") == "tool_result" for e in hooks.events))
check("success focus paths", res.recent_focus_paths_updated == ["x.py"])

# ── 2. Deadline-Break ───────────────────────────────────────────────────
res, hooks, msgs, rs, seen, ltl = run_round(
    [tc("read_file", {"path": "a.py"}), tc("read_file", {"path": "b.py"})],
    duo_deadline_at=time.monotonic() - 1)
check("deadline duo_timed_out", res.duo_timed_out is True)
check("deadline loop_detected", res.loop_detected is True)
check("deadline retry hint", any("were NOT executed" in str(m.get("content", "")) for m in msgs))

# ── 3. INVALID_JSON ─────────────────────────────────────────────────────
res, hooks, msgs, rs, seen, ltl = run_round([tc("read_file", raw_args="not json{{{")])
check("invalid_json no crash", True)
check("invalid_json message", any("INVALID_JSON" in str(m.get("content", "")) for m in msgs))

# ── 4. ask_user ─────────────────────────────────────────────────────────
res, hooks, msgs, rs, seen, ltl = run_round([tc("ask_user", {"question": "ok?"})])
check("ask_user agent_asking", any(e.get("type") == "agent_asking" for e in hooks.events))
check("ask_user resumed", any(e.get("type") == "agent_resumed" for e in hooks.events))

# ── 4b. ASK-USER-TOOL-GUARD (2026-09-02): S6 announce must NOT fire for
#        non-ask_user tools even when the gate is open (previously every
#        write_file/read_file round emitted a spurious "input needed"). ─────
try:
    from tools.runner import _ask_user_gate as _ask_gate_test_cv
except Exception:
    _ask_gate_test_cv = None
if _ask_gate_test_cv is not None:
    _ask_gate_test_cv.set("open")
    _HANDLERS["write_file"] = "[write_file: created 'g.txt' (+1 lines)]"
    res, hooks, msgs, rs, seen, ltl = run_round([tc("write_file", {"path": "g.txt", "content": "x"})])
    check("non-ask_user no agent_asking",
          not any(e.get("type") == "agent_asking" for e in hooks.events))
    check("non-ask_user tool still executed",
          any(e.get("type") == "tool_result" for e in hooks.events))
    _ask_gate_test_cv.set("open")
    res, hooks, msgs, rs, seen, ltl = run_round([tc("ask_user", {"question": "guard ok?"})])
    check("ask_user still announces under open gate",
          any(e.get("type") == "agent_asking" for e in hooks.events))

# ── 5. web_search dedup ─────────────────────────────────────────────────
res, hooks, msgs, rs, seen, ltl = run_round([
    tc("web_search", {"query": "HiveMind"}),
    tc("web_search", {"query": "HiveMind"}),
])
check("web dedup seen set", "hivemind" in seen)
check("web dedup second skipped", any("dedup: duplicate query" in str(m.get("content", "")) or
                                       "dedup: duplicate query" in res.last_tool_result for m in msgs))

# ── 5b. READ-BEFORE-EDIT / FRESHNESS (revived 2026-11-07) ────────────────
def _fresh_state():
    return ToolRoundState(
        tool_ctx_lru=_LRU(), duo_deadline_at=time.monotonic() + 60,
        reads_seen={}, freshness_warned={})

_HANDLERS["edit_file"] = "[edit_file: rewrote 'blind.py']"
_fresh_trs = _fresh_state()
res, hooks, msgs, rs, seen, ltl = run_round(
    [tc("edit_file", {"path": "blind.py", "old_text": "a", "new_text": "b"})],
    trs=_fresh_trs)
check("blind edit gets exactly one freshness nudge",
      sum("FRESHNESS" in str(m.get("content", "")) for m in msgs) == 1)
check("freshness path recorded", "blind.py" in _fresh_trs.freshness_warned)
res, hooks, msgs2, rs, seen, ltl = run_round(
    [tc("edit_file", {"path": "blind.py", "old_text": "b", "new_text": "c"})],
    dtool_msgs=msgs, trs=_fresh_trs)
check("same path is nudged only once",
      sum("FRESHNESS" in str(m.get("content", "")) for m in msgs2) == 1)

_HANDLERS["read_file"] = "content of seen.py"
_read_trs = _fresh_state()
run_round([tc("read_file", {"path": "seen.py"})], trs=_read_trs)
_HANDLERS["edit_file"] = "[edit_file: rewrote 'seen.py']"
msgs3 = []
run_round([tc("edit_file", {"path": "seen.py", "old_text": "a", "new_text": "b"})],
          dtool_msgs=msgs3, trs=_read_trs)
check("read before edit -> no freshness nudge",
      not any("FRESHNESS" in str(m.get("content", "")) for m in msgs3))

# ── 6. run_bash fail -> last_run_bash_failure ────────────────────────────
_HANDLERS["run_bash"] = "[TOOL_ERROR:RUN_BASH_NONZERO] run_bash: exited with code 1."
res, hooks, msgs, rs, seen, ltl = run_round([tc("run_bash", {"cmd": "pytest"})])
check("bash fail recorded", bool(res.last_run_bash_failure) and res.last_run_bash_failure.get("cmd") == "pytest")
check("bash fail changed cleared", res.changed_since_failure == set())

# ── 7. Reactive thinking (on_fail, think_runtime=False) ─────────────────
rs0 = DuoRoundState(think_runtime=False)
res, hooks, msgs, rs, seen, ltl = run_round([tc("run_bash", {"cmd": "x"})], round_state=rs0)
check("reactive think activated", rs.reactive_think_activated is True)
check("reactive think runtime on", rs.think_runtime is True)

# ── 8. Too-large content -> SPLIT REQUIRED ───────────────────────────────
_HANDLERS["edit_file"] = "[TOOL_ERROR:EDIT_FILE_CONTENT_TOO_LARGE] edit_file: content too large"
res, hooks, msgs, rs, seen, ltl = run_round([tc("edit_file", {"path": "big.py"})])
check("too large path boxed", ltl[0] == "big.py")
check("too large split required", any("SPLIT REQUIRED" in str(m.get("content", "")) for m in msgs))
check("too large token event", any(e.get("type") == "token" for e in hooks.events))

# ── 9. task_complete nach verifiziertem bash ────────────────────────────
# ANTI-SPOOF-2 (audit 2026-10-24): verification needs the genuine trailing
# [exit code: 0] marker - keywords alone are model-echoable and no longer count.
_HANDLERS["run_bash"] = "2 passed in 0.5s\n---\n[exit code: 0]"
_HANDLERS["task_complete"] = "ok"
res, hooks, msgs, rs, seen, ltl = run_round([
    tc("run_bash", {"cmd": "pytest"}),
    tc("task_complete", {"summary": "done"}),
])
check("task_complete_called after verified bash", res.task_complete_called is True)
# keywords WITHOUT the exit-code marker must NOT count as verified
_HANDLERS["run_bash"] = "all tests passed, success, 0 errors"
res, hooks, msgs, rs, seen, ltl = run_round([
    tc("run_bash", {"cmd": "pytest"}),
    tc("task_complete", {"summary": "done"}),
])
check("echoed keywords without exit-code marker stay unverified",
      res.task_complete_called is False)

# ── 10. Loop-Detection ──────────────────────────────────────────────────
# 10a. run_bash ABAB loop -> BASH-LOOP-REINJECT: NOT aborted, all tools
#      re-injected into the message stream (new behaviour 2026-09-02).
_HANDLERS["run_bash"] = "ok"
res, hooks, msgs, rs, seen, ltl = run_round([
    tc("run_bash", {"cmd": "a"}), tc("run_bash", {"cmd": "b"}),
    tc("run_bash", {"cmd": "a"}), tc("run_bash", {"cmd": "b"}),
])
check("run_bash loop NOT aborted", res.loop_detected is False)
check("run_bash loop re-inject tools", any("LOOP DETECTED" in str(m.get("content", "")) and
                                           "full toolset" in str(m.get("content", "")) for m in msgs))
check("run_bash loop non-bash hint", any("Choose a non-run_bash action" in str(m.get("content", "")) for m in msgs))

# 10b. Non-bash ABAB loop -> still aborts (unchanged behaviour).
_HANDLERS["read_file"] = "file content"
res, hooks, msgs, rs, seen, ltl = run_round([
    tc("read_file", {"path": "a.py"}), tc("read_file", {"path": "b.py"}),
    tc("read_file", {"path": "a.py"}), tc("read_file", {"path": "b.py"}),
])
check("non-bash loop detected", res.loop_detected is True)
check("non-bash loop aborted msg", any("loop-detection: aborted" in str(m.get("content", "")) for m in msgs))

# 10c. edit_file 3x identical loop -> 1x soft recovery, 2nd loop aborts.
_HANDLERS["edit_file"] = "[TOOL_ERROR:EDIT_FILE_NOOP] edit_file: no change"
_edit_trs = _fresh_state()
_edit_msgs = []
_edit_call = tc("edit_file", {"path": "presets.js", "old_text": "x", "new_text": "y"})
res1, hooks1, _edit_msgs, _, _, _ = run_round(
    [_edit_call, _edit_call, _edit_call], dtool_msgs=_edit_msgs, trs=_edit_trs)
check("edit_file 1st loop uses soft recovery (not aborted)",
      res1.loop_detected is False
      and _edit_trs.edit_loop_recoveries[0] == 1
      and len(_edit_trs.call_sigs) == 0
      and any("LOOP DETECTED" in str(m.get("content", "")) and "write_file" in str(m.get("content", ""))
              for m in _edit_msgs if m.get("role") == "user"))
_edit_call2 = tc("edit_file", {"path": "presets.js", "old_text": "a", "new_text": "b"})
res2, hooks2, _edit_msgs, _, _, _ = run_round(
    [_edit_call2, _edit_call2, _edit_call2], dtool_msgs=_edit_msgs, trs=_edit_trs)
check("edit_file 2nd loop hard-aborts",
      res2.loop_detected is True
      and any("loop-detection: aborted" in str(m.get("content", "")) for m in _edit_msgs))

# ── 11. File-change tracking (write_file) ───────────────────────────────
_HANDLERS["write_file"] = "[write_file: created 'x.txt' (+3 lines)]"
res, hooks, msgs, rs, seen, ltl = run_round([tc("write_file", {"path": "x.txt", "content": "a\nb\nc"})])
check("file_changes write op", res.file_changes.get("x.txt", {}).get("op") == "write")
check("file_change event", any(e.get("type") == "file_change" for e in hooks.events))

# ── 12. READ_REQUIRED 3x-Cap ────────────────────────────────────────────
_HANDLERS["edit_file"] = "[TOOL_ERROR:READ_REQUIRED] edit_file: read first"
res, hooks, msgs, rs, seen, ltl = run_round([
    tc("edit_file", {"path": "r.py"}), tc("edit_file", {"path": "r.py"}),
    tc("edit_file", {"path": "r.py"}),
])
check("read_required cap injected", any("3+ READ_REQUIRED" in str(m.get("content", "")) for m in msgs))

# ── 13. STUB-ECHO-GUARD (2026-09-09): write args containing the
#        ARG-COMPACT stub must be blocked before execution (spark-4b copied
#        the stub verbatim back into a file). ─────────────────────────────
_STUB_TEXT = ("[executed write_file_append: 4931 arg chars (sha 286c3f73) - "
              "content NOT in context anymore - read_file this path before "
              "any further write/edit to avoid full-file rewrites]")
_HANDLERS["write_file_append"] = "[write_file_append: would have been executed!]"
res, hooks, msgs, rs, seen, ltl = run_round(
    [tc("write_file_append", {"path": "game.js", "content": _STUB_TEXT})])
check("stub-echo blocked", any("STUB_ECHO_BLOCKED" in str(m.get("content", "")) for m in msgs))
check("stub-echo not executed",
      res.last_tool_name != "write_file_append"
      or "would have been executed" not in res.last_tool_result)
check("stub-echo no tool_result event",
      not any(e.get("type") == "tool_result" and "would have been executed" in str(e)
              for e in hooks.events))

# Clean content with a passing mention of the phrase must still be blocked
# only for write tools; a read_file containing the text stays executable.
_HANDLERS["read_file"] = "note: content not in context anymore is fine to read"
res, hooks, msgs, rs, seen, ltl = run_round([tc("read_file", {"path": "a.md"})])
check("stub-echo read still works", "fine to read" in res.last_tool_result)

# ── 14. DEADLINE-GRACE (2026-09-09): a successful write pushes the run
#        deadline; a failed write does not. ────────────────────────────────
_deadline = time.monotonic() + 10
_HANDLERS["write_file"] = "[write_file: created 'g.txt' (+1 lines)]"
res, hooks, msgs, rs, seen, ltl = run_round(
    [tc("write_file", {"path": "g.txt", "content": "x"})],
    duo_deadline_at=_deadline)
check("deadline-grace extended", run_round.last_trs.duo_deadline_at >= time.monotonic() + 299)
check("deadline-grace reported", res.deadline_extended_to >= time.monotonic() + 299)

_deadline = time.monotonic() + 10
_HANDLERS["write_file"] = "[write_file error: disk full]"
res, hooks, msgs, rs, seen, ltl = run_round(
    [tc("write_file", {"path": "g.txt", "content": "x"})],
    duo_deadline_at=_deadline)
check("deadline-grace not on failure", run_round.last_trs.duo_deadline_at < time.monotonic() + 299)

# ── 15. SAME-FILE READ-LOOP GUARD (2026-11-27): replaces Read-Ladder;
#        only repeated reads of the SAME file abort (5x), different files stay silent. ──
_HANDLERS["read_file"] = "content"
_prior_write = [{"role": "tool", "name": "write_file", "content": "[write_file: ok]", "tool_call_id": "w1"}]
# 5x SAME path with differing non-range args -> aborts as loop_detected
res, hooks, msgs, rs, seen, ltl = run_round([
    tc("read_file", {"path": "a.py", "offset": 1}),
    tc("read_file", {"path": "a.py", "offset": 2}),
    tc("read_file", {"path": "a.py", "offset": 3}),
    tc("read_file", {"path": "a.py", "offset": 4}),
    tc("read_file", {"path": "a.py", "offset": 5}),
], dtool_msgs=list(_prior_write))
check("same-file read loop aborts at 5x without READ LADDER hint",
      res.loop_detected is True and not any("[READ LADDER]" in str(m.get("content", "")) for m in msgs))
# 3x DIFFERENT paths -> no abort, no hint
res, hooks, msgs, rs, seen, ltl = run_round([
    tc("read_file", {"path": "a.py"}),
    tc("read_file", {"path": "b.py"}),
    tc("read_file", {"path": "c.py"}),
])
check("different paths stay silent without abort",
      res.loop_detected is False and not any("[READ LADDER]" in str(m.get("content", "")) for m in msgs))
# A-B-A pattern: below threshold, no abort
res, hooks, msgs, rs, seen, ltl = run_round([
    tc("read_file", {"path": "a.py"}),
    tc("read_file", {"path": "b.py"}),
    tc("read_file", {"path": "a.py"}),
])
check("A-B-A reads stay silent without abort",
      res.loop_detected is False and not any("[READ LADDER]" in str(m.get("content", "")) for m in msgs))
# write between re-reads resets counter
_HANDLERS["write_file"] = "[write_file: created 'w.txt']"
res, hooks, msgs, rs, seen, ltl = run_round([
    tc("read_file", {"path": "a.py", "offset": 1}),
    tc("read_file", {"path": "a.py", "offset": 2}),
    tc("read_file", {"path": "a.py", "offset": 3}),
    tc("write_file", {"path": "w.txt", "content": "x"}),
    tc("read_file", {"path": "a.py", "offset": 4}),
    tc("read_file", {"path": "a.py", "offset": 5}),
])
check("same-file read counter resets after write", res.loop_detected is False)

# ── 16. SAME-FILE READ COUNTER PERSISTS ACROSS ROUNDS ──
from core.tool_exec_helpers import ToolRoundState as _TRS
_slot_defs = dict(
    tool_ctx_lru=_LRU(), duo_deadline_at=time.monotonic() + 60,
    verify_mutation_serial=0, verify_last_ok_serial=0,
    last_run_bash_failure=None, changed_since_failure=set(),
    last_learned_insight_sig="", last_too_large_path=[None],
    attempts_per_file={}, tool_error_retries={}, call_sigs=[],
    recent_focus_paths=[], file_changes={}, duo_seen_web_queries=set(),
    cached_coder_port=[None], task_complete_blocked_count=None, total_tool_errors=None,
)
_persist_trs = _TRS(**_slot_defs)
_prior_write = [{"role": "tool", "name": "write_file", "content": "[write_file: ok]", "tool_call_id": "w1"}]
# round 1: 3 reads on same file (varying non-range args) -> not yet at 5x
run_round([
    tc("read_file", {"path": "a.py", "offset": 1}),
    tc("read_file", {"path": "a.py", "offset": 2}),
    tc("read_file", {"path": "a.py", "offset": 3}),
], trs=_persist_trs, dtool_msgs=list(_prior_write))
# round 2: 2 more reads on same file -> reaches 5x total across rounds and aborts
res, hooks, msgs, rs, seen, ltl = run_round([
    tc("read_file", {"path": "a.py", "offset": 4}),
    tc("read_file", {"path": "a.py", "offset": 5}),
], trs=_persist_trs, dtool_msgs=list(_prior_write))
check("same-file read counter persists across rounds and aborts at 5x", res.loop_detected is True)

# ── RUN-BASH-REPEAT HINT (2026-10-16): identical failing run_bash twice
# with nothing changed in between -> hint injected (no abort, no loop flag).
# Live: PowerShell syntax in bash, exit 2, identical retry.
_HANDLERS["run_bash"] = "[stderr]\nbash: Test-Path: command not found\n---\n[exit code: 2]"
_rep_trs = _TRS(**_slot_defs)
run_round([tc("run_bash", {"cmd": "Test-Path x"})], trs=_rep_trs)
_res2, _h2, _m2, _, _, _ = run_round([tc("run_bash", {"cmd": "Test-Path x"})],
                                     trs=_rep_trs)
check("run_bash repeat: hint injected on 2nd identical failure",
      any("failed twice with identical args" in str(m.get("content", ""))
          for m in _m2 if m.get("role") in ("tool", "user")))
check("run_bash repeat: no loop abort at 2x", not _res2.loop_detected)
# 3rd identical call -> BASH-LOOP-REINJECT still fires (hard stop preserved)
_res3, _h3, _m3, _, _, _ = run_round([tc("run_bash", {"cmd": "Test-Path x"})],
                                     trs=_rep_trs)
check("run_bash repeat: 3x reinject still fires",
      any("[LOOP DETECTED]" in str(m.get("content", "")) for m in _m3))

# ── SEND-FILE (2026-11-04): the gateway gets a dedicated event and the
# coder a hard "finish now" nudge, so a delivered file cannot be followed by
# a web-search loop that labels the whole run loop_detected.
_HANDLERS["send_file"] = "[FILE_QUEUED] demo.html (12 bytes) queued"
_sf_res, _sf_hooks, _, _, _, _ = run_round(
    [tc("send_file", {"path": "demo.html"})], tool_mode="duo_full",
    allow_send_file=True)
check("send_file queues the path for the runner",
      _sf_res.send_files == ["demo.html"])
check("send_file does not deliver early",
      not any(e.get("type") == "send_file" for e in _sf_hooks.events))
check("send_file sets the finish nudge",
      "task_complete" in _sf_res.extra_user_msg)
_no_sf_res, _no_sf_hooks, _, _, _, _ = run_round(
    [tc("send_file", {"path": "demo.html"})], tool_mode="duo_full")
check("send_file is blocked outside Telegram runs",
      not any(e.get("type") == "send_file" for e in _no_sf_hooks.events)
      and not _no_sf_res.extra_user_msg)

# ── 17. REPEAT-TIMEOUT GUARD & VERIFIED-EXEC TASK-COMPLETE (2026-11-19) ──
# Re-running an identical run_bash command that already timed out with no
# file edits since must be rejected instantly instead of burning another 90s.
_to_calls = []
def _counting_timeout(_name, args):
    _to_calls.append(args)
    return "[TOOL_ERROR:RUN_BASH_TIMEOUT] run_bash: run_bash timed out after 90s."
_HANDLERS["run_bash"] = _counting_timeout
_to_trs = _TRS(**{**_slot_defs, "tool_error_retries": {}, "call_sigs": []})
run_round([tc("run_bash", {"cmd": "python main.py"})], trs=_to_trs)
_to_r2, _, _to_m2, _, _, _ = run_round([tc("run_bash", {"cmd": "python  main.py"})], trs=_to_trs)
check("repeat timed-out run_bash is rejected without executing again",
      len(_to_calls) == 1
      and any("Blocked duplicate run_bash call" in str(m.get("content", "")) for m in _to_m2),
      f"calls={len(_to_calls)}")
# After a file edit bumps verify_mutation_serial, the command may run once more
_to_trs.verify_mutation_serial = 1
run_round([tc("run_bash", {"cmd": "python main.py"})], trs=_to_trs)
check("timed-out run_bash allowed again after file edit", len(_to_calls) == 2, f"calls={len(_to_calls)}")

# task_complete with 0 mutations accepts passing run_python + later GUI timeout,
# and recognizes [run_bash: exit 0] prefix for verified commands.
_HANDLERS["task_complete"] = "[task_complete] Build: passing\nCompleted: smoke tested\nBlockers: none"
_tc_trs1 = _TRS(**{**_slot_defs, "tool_error_retries": {}, "call_sigs": []})
_tc_msgs1 = [
    {"role": "tool", "name": "run_python", "tool_call_id": "rp1",
     "content": "Pygame initialized successfully.\n[exit code: 0]"},
    {"role": "tool", "name": "run_bash", "tool_call_id": "rb1",
     "content": "[TOOL_ERROR:RUN_BASH_TIMEOUT] run_bash: run_bash timed out after 90s."},
]
_tc_r1, _, _, _, _, _ = run_round([tc("task_complete", {"summary": "done"})],
                                  trs=_tc_trs1, dtool_msgs=_tc_msgs1)
check("0-mutation task_complete accepted after passing smoke test + GUI timeout",
      _tc_r1.task_complete_called is True)

_tc_trs2 = _TRS(**{**_slot_defs, "tool_error_retries": {}, "call_sigs": []})
_tc_msgs2 = [
    {"role": "tool", "name": "run_bash", "tool_call_id": "rb2",
     "content": "[run_bash: exit 0]\nAll 5 checks passed"},
]
_tc_r2, _, _, _, _, _ = run_round([tc("task_complete", {"summary": "done"})],
                                  trs=_tc_trs2, dtool_msgs=_tc_msgs2)
check("0-mutation task_complete recognizes [run_bash: exit 0] verification",
      _tc_r2.task_complete_called is True)

# ── 18. TASK-COMPLETE CHECKPOINT / MORE_WORK_REMAINING (2026-11-27) ───────
from tools.handlers.misc import _inline_tool_task_complete
from tools.definitions import _INLINE_CODING_TOOLS

_tc_def = next(t["function"] for t in _INLINE_CODING_TOOLS if t["function"]["name"] == "task_complete")
_tc_props = _tc_def["parameters"]["properties"]
check("task_complete schema exposes optional boolean more_work_remaining",
      _tc_props.get("more_work_remaining", {}).get("type") == "boolean"
      and _tc_props.get("more_work_remaining", {}).get("default") is False
      and _tc_def["parameters"].get("required") == ["status"])

_cp_top = asyncio.run(_inline_tool_task_complete({
    "status": {"completed": ["part 1"], "blockers": [], "build_status": "passing"},
    "more_work_remaining": True,
}, ".", False))
check("task_complete handler emits CHECKPOINT marker when more_work_remaining=True",
      "[task_complete: CHECKPOINT - continuing run]" in _cp_top
      and "the run stays OPEN" in _cp_top)

_cp_nested = asyncio.run(_inline_tool_task_complete({
    "status": {"completed": ["part 1"], "blockers": [], "build_status": "untested",
               "more_work_remaining": "true"},
}, ".", False))
check("task_complete handler accepts nested string more_work_remaining='true'",
      "[task_complete: CHECKPOINT - continuing run]" in _cp_nested)

_cp_inprog = asyncio.run(_inline_tool_task_complete({
    "status": {"completed": ["part 1"], "blockers": [], "build_status": "in_progress"},
}, ".", False))
check("task_complete handler treats build_status='in_progress' as checkpoint",
      "[task_complete: CHECKPOINT - continuing run]" in _cp_inprog)

_cp_final = asyncio.run(_inline_tool_task_complete({
    "status": {"completed": ["part 1", "part 2"], "blockers": [], "build_status": "passing"},
    "more_work_remaining": False,
}, ".", False))
check("task_complete handler emits normal [task_complete] when more_work_remaining=False",
      _cp_final.startswith("[task_complete] Build: passing")
      and "CHECKPOINT" not in _cp_final)

_HANDLERS["task_complete"] = _cp_top
_cp_trs = _TRS(**{**_slot_defs, "tool_error_retries": {}, "call_sigs": []})
_cp_msgs = [
    {"role": "tool", "name": "run_bash", "tool_call_id": "rb_cp",
     "content": "[run_bash: exit 0]\nAll checks passed"},
]
_cp_r1, _, _cp_m1, _, _, _ = run_round(
    [tc("task_complete", {
        "status": {"completed": ["subtask 1"], "blockers": [], "build_status": "passing"},
        "more_work_remaining": True,
    })],
    trs=_cp_trs, dtool_msgs=_cp_msgs,
)
check("checkpoint task_complete keeps run open and resets tc_consecutive",
      _cp_r1.task_complete_called is False
      and _cp_trs.tc_consecutive == [0]
      and any("[task_complete: CHECKPOINT" in str(m.get("content", "")) for m in _cp_m1))

_HANDLERS["task_complete"] = _cp_final
_cp_r2, _, _, _, _, _ = run_round(
    [tc("task_complete", {
        "status": {"completed": ["subtask 1", "subtask 2"], "blockers": [], "build_status": "passing"},
        "more_work_remaining": False,
    })],
    trs=_cp_trs, dtool_msgs=_cp_m1,
)
check("final task_complete after checkpoint ends run cleanly",
      _cp_r2.task_complete_called is True)

sys.exit(h.finish("execute_tool_round"))
