"""Same-file repeat-read loop guard (replaces the old Read-Ladder, 2026-11-27).

Reading many DIFFERENT files (or sequential line ranges of a large file) is
normal repo exploration and must NEVER inject a [READ LADDER] hint or abort.
Only looping repeatedly on the SAME file/slice (including [SKIP: ... already
read] repeats) without an intervening write/edit/test/bash aborts the run
with loop_detected=True.

Run: python tests/test_read_ladder.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402
from core.tool_exec_helpers import ToolRoundState, _mode_can_write  # noqa: E402
from hive_functions.memory import ToolContextLRU  # noqa: E402
from core.agentic_duo_state import DuoRoundState  # noqa: E402
from tools.limits import READ_SAME_FILE_ABORT  # noqa: E402
import core.tool_executor as TE  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check

# ── (a) mode capability helper stays intact ─────────────────────────────────
check("duo_full can write", _mode_can_write("duo_full") is True)
check("tool_agent can write", _mode_can_write("tool_agent") is True)
check("direct_full can write", _mode_can_write("direct_full") is True)
for _ro in ("duo_readonly", "pre_explore", "critic_verify", "direct",
            "direct_python", ""):
    check(f"{_ro or '<empty>'} cannot write", _mode_can_write(_ro) is False)

# ── (b) source guard: no cross-file Read-Ladder hint in tool_executor ───────
_src = (ROOT / "core" / "tool_executor.py").read_text(encoding="utf-8", errors="replace")
check("tool_executor has no [READ LADDER] hint injection", "[READ LADDER]" not in _src)
check("tool_executor uses READ_SAME_FILE_ABORT", "READ_SAME_FILE_ABORT" in _src)


async def _noop_emit(_ev):
    return None


def _call(idx, path, tool="read_file", extra=None):
    payload = {"path": path}
    if extra:
        payload.update(extra)
    return {
        "id": f"c{idx}",
        "type": "function",
        "function": {"name": tool, "arguments": json.dumps(payload)},
    }


class _Harness:
    def __init__(self, tool_mode: str = "duo_full", fake_result: str = "[read_file: x - 3 lines]"):
        self.tool_mode = tool_mode
        self.fake_result = fake_result
        self.msgs = [{"role": "system", "content": "sys"}]
        self.state = DuoRoundState()
        self.trs = ToolRoundState(
            tool_ctx_lru=ToolContextLRU(default_ttl=3),
            duo_deadline_at=time.monotonic() + 300,
        )

    def round(self, calls: list[dict]):
        self.msgs.append({"role": "assistant", "content": "", "tool_calls": calls})

        async def _run():
            async def _fake_one(*_a, **_kw):
                return self.fake_result
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

        return asyncio.run(_run())

    def hints(self):
        return [m for m in self.msgs if "READ LADDER" in str(m.get("content", ""))]


# C1: 20 different files across 20 separate rounds -> NO hint, NO abort
_h_diff = _Harness()
_diff_aborted = False
for i in range(20):
    _r = _h_diff.round([_call(i, f"file_{i}.js")])
    if _r.loop_detected:
        _diff_aborted = True
check("20 different files across separate rounds: no hint, no abort",
      not _diff_aborted and len(_h_diff.hints()) == 0,
      f" (aborted={_diff_aborted}, hints={len(_h_diff.hints())})")

# C2: parallel batch of 12 different files -> NO hint, NO abort
_h_par = _Harness()
_r_par = _h_par.round([_call(i, f"p{i}.js") for i in range(12)])
check("parallel batch of 12 different files: no hint, no abort",
      not _r_par.loop_detected and len(_h_par.hints()) == 0)

# C3: sequential distinct line ranges of one big file -> NO abort
_h_ranges = _Harness()
_range_aborted = False
for i in range(8):
    _r = _h_ranges.round([_call(i, "big.py", extra={"start_line": i * 100 + 1, "end_line": (i + 1) * 100})])
    if _r.loop_detected:
        _range_aborted = True
check("8 distinct line ranges of one large file: no abort", not _range_aborted)

# C4: same file repeated READ_SAME_FILE_ABORT times with [SKIP:] -> ABORTS
_h_skip = _Harness(fake_result="[SKIP: 'loop.py' already read in this session.]")
_skip_res = None
for i in range(READ_SAME_FILE_ABORT):
    _skip_res = _h_skip.round([_call(i, "loop.py")])
check(f"same file [SKIP:] repeated {READ_SAME_FILE_ABORT}x aborts as loop_detected",
      bool(_skip_res and _skip_res.loop_detected) and len(_h_skip.hints()) == 0)

# C5: same file repeated across rounds interleaved with other files -> ABORTS at threshold
_h_inter = _Harness()
_inter_res = None
for i in range(READ_SAME_FILE_ABORT):
    _h_inter.round([_call(2 * i, f"other_{i}.py")])
    _inter_res = _h_inter.round([_call(2 * i + 1, "stuck.py")])
check(f"same file interleaved with other reads aborts at {READ_SAME_FILE_ABORT}x",
      bool(_inter_res and _inter_res.loop_detected))

# C6: write between same-file reads resets counter -> NO abort
_h_rst = _Harness(fake_result="[SKIP: 'a.py' already read]")
_rst_aborted = False
for i in range(READ_SAME_FILE_ABORT - 1):
    if _h_rst.round([_call(i, "a.py")]).loop_detected:
        _rst_aborted = True
_h_rst.fake_result = "[write_file: ok]"
_h_rst.round([_call(99, "a.py", tool="write_file")])
_h_rst.fake_result = "[SKIP: 'a.py' already read]"
for i in range(READ_SAME_FILE_ABORT - 1):
    if _h_rst.round([_call(100 + i, "a.py")]).loop_detected:
        _rst_aborted = True
check("write resets same-file read counter (no abort)", not _rst_aborted)

sys.exit(h.finish("read_ladder"))
