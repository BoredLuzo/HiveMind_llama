"""READ-LADDER: the "you are exploring but not implementing" hint must fire
for a model that only reads, and must stay silent in read-only modes.

Regression for the old condition in core/tool_executor.py: it required a
write/bash call SOMEWHERE in the message history, so a coder that read ten
files before its first edit never got the hint - the guard was dead for
exactly the case it was written for.

Two triggers (tools/limits.py):
  READ_LADDER_SAME_PATH = 3  - the same path read 3x in a row
  READ_LADDER_WIDE      = 8  - 8 reads across different paths, no write at all
(`consecutive_reads` resets on every path change - PATH-RESET 2026-09-09 - so
the wide case needs its own counter.)

Pins:
  (a) _mode_can_write: write modes True, read-only modes False
  (b) source guard: the dead "prior write in history" condition is gone
  (c) end to end through the real execute_tool_round: same-path streak fires,
      wide streak fires, a write resets both, duo_readonly stays silent

Run: python tests/test_read_ladder.py
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── (a) mode capability ─────────────────────────────────────────────────────
from core.tool_exec_helpers import _mode_can_write          # noqa: E402

check("duo_full can write", _mode_can_write("duo_full") is True)
check("tool_agent can write", _mode_can_write("tool_agent") is True)
check("direct_full can write", _mode_can_write("direct_full") is True)
for _ro in ("duo_readonly", "pre_explore", "critic_verify", "direct",
            "direct_python", ""):
    check(f"{_ro or '<empty>'} cannot write", _mode_can_write(_ro) is False)

# ── (b) source guard: the dead history condition is gone ────────────────────
_src = (ROOT / "core" / "tool_executor.py").read_text(encoding="utf-8",
                                                      errors="replace")
check("read-ladder no longer requires a prior write in the history",
      '"replace_lines", "run_bash", "run_python")\n            for _m in dtool_msgs'
      not in _src)
check("read-ladder asks the mode instead", "_mode_can_write(tool_mode, duo_ws)" in _src)
check("read-ladder has the wide trigger", "READ_LADDER_WIDE" in _src)

# ── (c) end to end through the real executor ────────────────────────────────
import core.tool_executor as TE                                  # noqa: E402
from core.tool_exec_helpers import ToolRoundState                # noqa: E402
from hive_functions.memory import ToolContextLRU                 # noqa: E402
from core.agentic_duo_state import DuoRoundState                 # noqa: E402


async def _noop_emit(_ev):
    return None


def _call(idx, path, tool="read_file"):
    args = '{"path": "%s"}' % path
    return {"id": f"c{idx}", "type": "function",
            "function": {"name": tool, "arguments": args}}


class _Harness:
    """One run: persistent trs across rounds, fresh message list."""

    def __init__(self, tool_mode: str = "duo_full"):
        self.tool_mode = tool_mode
        self.msgs = [{"role": "system", "content": "sys"}]
        self.state = DuoRoundState()
        self.trs = ToolRoundState(
            tool_ctx_lru=ToolContextLRU(default_ttl=3),
            duo_deadline_at=time.monotonic() + 300,   # 0.0 counts as "deadline passed"
        )

    def round(self, calls: list[dict]):
        self.msgs.append({"role": "assistant", "content": "", "tool_calls": calls})

        async def _run():
            async def _fake_one(*_a, **_kw):
                return "[read_file: x - 3 lines]"
            _orig = TE._execute_one_tool
            TE._execute_one_tool = _fake_one
            try:
                return await TE.execute_tool_round(
                    tool_calls=calls,
                    dtool_msgs=self.msgs,
                    round_state=self.state,
                    hooks=SimpleNamespace(emit=_noop_emit,
                                          request_stop=lambda *a, **k: None,
                                          on_tool_result=None,
                                          is_aborted=lambda _c: False),
                    trs=self.trs,
                    tool_mode=self.tool_mode,
                    duo_ws=False,
                    workspace_lock=str(ROOT),
                    exec_model="test-model",
                    auto_test_before_complete=False,
                    exec_has_thinking=False,
                    tool_think_auto_mode="off",
                    run_id_global="r1",
                    chat_id="c1",
                    subtask_index=0,
                )
            finally:
                TE._execute_one_tool = _orig

        asyncio.run(_run())
        return self.hints()

    def hints(self):
        return [m for m in self.msgs if "READ LADDER" in str(m.get("content", ""))]


# C1: the same path three times in one round is caught by the LOOP DETECTOR,
# not by the ladder (identical signatures). The ladder's live trigger for
# "reads but never writes" is the wide streak below - pinned here so the
# division of labour is visible.
_h1 = _Harness()
_h1.round([_call(0, "a.txt"), _call(1, "a.txt"), _call(2, "a.txt")])
check("same path 3x: loop detector owns identical repeats (no ladder hint)",
      len(_h1.hints()) == 0, f" ({len(_h1.hints())} hint(s))")
check("same path 3x: only the loop-detection result, no hint appended",
      len([m for m in _h1.msgs if m.get("role") == "user"]) == 0,
      f" ({len([m for m in _h1.msgs if m.get('role') == 'user'])} user msg(s))")

# C2: eight DIFFERENT paths -> wide trigger fires
_h2 = _Harness()
_h2.round([_call(i, f"f{i}.txt") for i in range(8)])
check("8 different paths: hint fires", len(_h2.hints()) == 1,
      f" ({len(_h2.hints())} hint(s))")
check("wide hint names the kind", "different files" in str(_h2.hints()[0]["content"])
      if _h2.hints() else False)

# C3: seven different paths -> still silent (no early nagging)
_h3 = _Harness()
_h3.round([_call(i, f"g{i}.txt") for i in range(7)])
check("7 different paths: still silent", len(_h3.hints()) == 0,
      f" ({len(_h3.hints())} hint(s))")

# C4: the hint fires once per run, even with more reads afterwards
_h4 = _Harness()
_h4.round([_call(i, f"h{i}.txt") for i in range(8)])
_h4.round([_call(i, f"i{i}.txt") for i in range(8)])
check("hint fires once per run", len(_h4.hints()) == 1,
      f" ({len(_h4.hints())} hint(s))")

# C5: a write resets both counters -> no hint after 7 reads + write + 7 reads
_h5 = _Harness()
_h5.round([_call(i, f"j{i}.txt") for i in range(7)])
_h5.round([_call(99, "out.py", tool="write_file")])
_h5.round([_call(i, f"k{i}.txt") for i in range(7)])
check("write resets the streak (no hint)", len(_h5.hints()) == 0,
      f" ({len(_h5.hints())} hint(s))")

# C6: read-only mode stays silent even with a wide streak
_h6 = _Harness(tool_mode="duo_readonly")
_h6.round([_call(i, f"m{i}.txt") for i in range(8)])
check("duo_readonly: no read-ladder hint", len(_h6.hints()) == 0,
      f" ({len(_h6.hints())} hint(s))")

print()
print(f"SUMMARY read_ladder: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
