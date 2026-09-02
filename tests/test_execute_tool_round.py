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

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


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
        duo_deadline_at=time.time() + 60,
        verify_mutation_serial=0, verify_last_ok_serial=0,
        last_run_bash_failure=None, changed_since_failure=set(),
        last_learned_insight_sig="", last_too_large_path=[None],
        attempts_per_file={}, tool_error_retries={}, call_sigs=[],
        recent_focus_paths=[], file_changes={}, duo_seen_web_queries=set(),
        cached_coder_port=[None], task_complete_blocked_count=None, total_tool_errors=None,
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
    duo_deadline_at=time.time() - 1)
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
_HANDLERS["run_bash"] = "2 passed in 0.5s"
_HANDLERS["task_complete"] = "ok"
res, hooks, msgs, rs, seen, ltl = run_round([
    tc("run_bash", {"cmd": "pytest"}),
    tc("task_complete", {"summary": "done"}),
])
check("task_complete_called after verified bash", res.task_complete_called is True)

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

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)
