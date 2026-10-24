# -*- coding: utf-8 -*-
"""Audit round 2026-10-24 (crash-recovered + Windows fixes): working-set
eviction protection, read-dedup consistency (eviction drops read signatures),
plan-tracker advance gating, run_bash wedges, exit-code-verified run_bash.

Behavioral checks for the pure helpers; structural pins for the in-loop
wiring (the loop itself is covered by the executor suites)."""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── 1. WORKING SET: protected focus files survive eviction ───────────────
from context.compression import _evict_stale_tool_outputs  # noqa: E402
from hive_functions.memory import _recall_marker           # noqa: E402


class _FakeLRU:
    """candidates() contract: lowest-TTL entries first, entries carry
    idx/path/kind; mark_evicted(idx) bookkeeping."""

    def __init__(self, entries):
        self._entries = entries
        self.evicted = []

    def candidates(self):
        return self._entries

    def mark_evicted(self, idx):
        self.evicted.append(idx)


def _ws_messages():
    # read_file output headers carry str(Path): backslashes + original
    # case on Windows — the protect match must survive both.
    return [
        {"role": "system", "content": "sys"},
        {"role": "tool", "name": "read_file",
         "content": "C:\\Proj\\Src\\A.js total lines: 10\n" + "a-code " * 500},
        {"role": "assistant", "content": ""},
        {"role": "tool", "name": "read_file",
         "content": "C:\\Proj\\Src\\B.js total lines: 10\n" + "b-code " * 500},
    ]


def _ws_lru():
    return _FakeLRU([
        {"idx": 3, "path": "Src/B.js", "kind": "read_file"},
        {"idx": 1, "path": "Src/A.js", "kind": "read_file"},
    ])


msgs = _ws_messages()
n = _evict_stale_tool_outputs(messages=msgs, lru=_ws_lru(),
                              target_token_budget=10, hard_floor_tokens=0,
                              protect_paths=["C:/Proj/Src/A.js"])
check("1a protected focus read stays verbatim",
      n == 1 and "a-code" in msgs[1]["content"])
check("1b unprotected read becomes recall marker",
      msgs[3]["content"] == _recall_marker("Src/B.js"))

msgs = _ws_messages()
n = _evict_stale_tool_outputs(messages=msgs, lru=_ws_lru(),
                              target_token_budget=10, hard_floor_tokens=0)
check("1c without protect both reads are evicted",
      n == 2 and "a-code" not in msgs[1]["content"])

# ── 2. READ-DEDUP consistency: eviction drops signatures ─────────────────
import tools.runner as _tr                                   # noqa: E402
from tools.handlers.file_ops import _inline_tool_read_file   # noqa: E402
from utils.file import normalize_tool_path as _ntp           # noqa: E402

ws = Path(tempfile.mkdtemp(prefix="hm_vh_"))
f = ws / "dedup.js"
f.write_text("function one() { return 1; }\n" * 40, encoding="utf-8")
key = _ntp(str(f), str(ws))


async def _read():
    return await _inline_tool_read_file({"path": f.name}, ws, None)


st = f.stat()
_tr._read_signatures[key] = (st.st_mtime_ns, st.st_size)
_tok = _tr._files_read_in_run.set({key})   # read happened THIS run
out = asyncio.run(_read())
_tr._files_read_in_run.reset(_tok)
check("2a unchanged re-read answers compact UNCHANGED",
      "UNCHANGED" in out and "function one()" not in out, out[:70])
# same signature, but a FRESH run (empty read-set) must get real content —
# the signature dict is module-global and survives across runs
_tok = _tr._files_read_in_run.set(set())
out = asyncio.run(_read())
_tr._files_read_in_run.reset(_tok)
check("2a2 cross-run: matching signature, fresh context -> full content",
      "function one()" in out and "UNCHANGED" not in out)

msgs = [{"role": "tool", "name": "read_file",
         "content": str(f) + "\n" + "x" * 800}]
n = _evict_stale_tool_outputs(
    messages=msgs, lru=_FakeLRU([{"idx": 0, "path": f.name, "kind": "read_file"}]),
    target_token_budget=5, hard_floor_tokens=0)
check("2b eviction drops the read signature (relative LRU path)",
      n == 1 and key not in _tr._read_signatures)
out = asyncio.run(_read())
check("2c next read returns real content after eviction",
      "function one()" in out and "UNCHANGED" not in out)
_tr._read_signatures.pop(key, None)

# ── 3. PlanTracker.should_advance gating ─────────────────────────────────
from core.plan_tracker import PlanTracker, Plan, PlanStep  # noqa: E402

plan = Plan(steps=[
    PlanStep(id="s1", intent="edit a", expected_paths=["src/a.py"]),
    PlanStep(id="s2", intent="run anything"),
], current_step_id="s1")
t = PlanTracker(plan=plan)

check("3a read_file never advances",
      t.should_advance("read_file", "content", touched_paths=["src/a.py"]) is False)
check("3b error result never advances",
      t.should_advance("edit_file", "[TOOL_ERROR x]", touched_paths=["src/a.py"]) is False)
check("3c edit outside the step scope does not advance",
      t.should_advance("edit_file", "ok", touched_paths=["other/z.py"]) is False)
check("3d successful edit on the expected path advances",
      t.should_advance("edit_file", "ok wrote", touched_paths=["src/a.py"]) is True)
t.advance("edit_file")
check("3e advance moves to the next step",
      t._plan.current_step_id == "s2" and "s1" in t._plan.completed_step_ids)
check("3f step without expected paths advances on any successful run",
      t.should_advance("run_tests", "[TEST-RESULT] 3 passed", touched_paths=[]) is True)

# ── 4. run_bash wedge keying ─────────────────────────────────────────────
from hive_functions.memory import ToolContextLRU             # noqa: E402
from core.tool_exec_helpers import _track_focus_path         # noqa: E402

lru4 = ToolContextLRU()
rfp = []
fp = _track_focus_path({"cmd": "npm test -- --watch"}, "run_bash", lru4, rfp, 4)
check("4a run_bash focus key = first command word", fp == "npm")
check("4b run_bash does not enter the file focus ring", rfp == [])
fp = _track_focus_path({"path": "src/a.py"}, "read_file", lru4, rfp, 4)
check("4c file tools still enter the focus ring",
      fp == "src/a.py" and rfp == ["src/a.py"])

# ── 5. Structural pins ───────────────────────────────────────────────────
_dr = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
check("5a protect_paths wired at all 3 eviction sites, no duplicates",
      _dr.count("protect_paths=_recent_focus_paths,") == 3)
check("5b plan advance carries touched paths",
      "touched_paths=list((_file_changes or {}).keys())" in _dr)

_te = (ROOT / "core" / "tool_executor.py").read_text(encoding="utf-8", errors="replace")
check("5c run_bash verify requires the genuine [exit code: 0] marker",
      "_ec_matches and int(_ec_matches[-1]) == 0" in _te)

_wh = (ROOT / "core" / "wedge_handoff.py").read_text(encoding="utf-8", errors="replace")
check("5d run_bash is wedge-tracked",
      '"run_bash"' in _wh.split("EDIT_FAMILY_TOOLS")[1][:200])

import settings  # noqa: E402
check("5e wedge handoff on by default",
      settings.DEFAULT_SETTINGS["duo_wedge_handoff_enabled"] is True)

import hive_functions.language_config as _lc  # noqa: E402
_lc.detect_project_languages = lambda ws_: ["python"]
_hint = _lc.build_test_hint(str(ws))
check("5f test hint prefers run_tests over concrete run_bash",
      "run_tests (full suite)" in _hint and "run_bash(cmd=" not in _hint)

_pr = (ROOT / "hive_functions" / "prompts.py").read_text(encoding="utf-8", errors="replace")
check("5g dead UNTIL_FINISHED_BLOCK removed", "UNTIL_FINISHED_BLOCK" not in _pr)

_df = (ROOT / "tools" / "definitions.py").read_text(encoding="utf-8", errors="replace")
check("5h run_tests description honest about pre-existing failures",
      "pre-existing failures count too" in _df)

_fo = (ROOT / "tools" / "handlers" / "file_ops.py").read_text(encoding="utf-8", errors="replace")
check("5i re-read dedup reads the flat signature dict, not .get(None)",
      "_rr_sig.get(_rr_key)" in _fo and "_rr_sig.get(None)" not in _fo)
check("5j UNCHANGED answer is gated on a read THIS run (module-global sig dict)",
      "_rr_this_run and _rr_prev is not None" in _fo)

print(f"\nverification_honesty: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
