# -*- coding: utf-8 -*-
"""Behavior test: the round-gate policies extracted from run_code_duo
(F13 maintainability sprint, 2026-11-08).

core/duo_round_gate.py holds three verbatim moves; the runner keeps loop
control and syncs the results back. This suite pins their behavior with a
minimal fake ChunkState (no model runs):

  1. grace-round gate: proceed / one-shot nudge (continue) / expired stop
     (break, budget exhausted, ld_setter(3137)),
  2. max-rounds gate: below-budget no-op / first exhaustion arms the grace
     round (budget +1 + exact nudge + status) / second exhaustion ends the
     chunk (break, budget exhausted, ld_setter(3551)) / the optional
     unverified-files warning,
  3. auto-test gate: clean verdict, flaky rerun, re-awaken fix round and
     the max-retries note - with the EXACT status wordings of the two
     call sites (agentic "remain" vs critic-approved "persist") and the
     exact run_auto_test / handle_auto_test_result arguments
     (chat_id only where the old call sites passed it).

Source pins: the runner keeps loop control (the loop guard and the break)
and the caller-specific bookkeeping (mark_chunk_done / _di advance /
chunk_checkpoint).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.duo_round_gate as _G  # noqa: E402
from core.duo_round_gate import (  # noqa: E402
    _grace_round_gate, _max_rounds_gate, _run_auto_test_gate,
    _tool_budget_gate,
)
from hive_functions.chunking import ChunkAction  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


# ── 1. grace-round gate ──────────────────────────────────────────────────
_ld_lines: list[int] = []
_g = _grace_round_gate(grace_round_active=False, dr_tcs=[{"function": {"name": "edit_file"}}],
                       grace_round_used=False, ld_setter=_ld_lines.append)
check("grace inactive: falls through to the exec round",
      _g.action == "proceed" and not _g.events
      and _g.grace_round_used is False and not _g.chunk_budget_exhausted
      and not _g.loop_detected and _ld_lines == [])

_g = _grace_round_gate(grace_round_active=True,
                       dr_tcs=[{"function": {"name": "task_complete"}}],
                       grace_round_used=False, ld_setter=_ld_lines.append)
check("task_complete-only round proceeds to the exec round",
      _g.action == "proceed" and not _g.events and _g.grace_round_used is False)

_g = _grace_round_gate(grace_round_active=True,
                       dr_tcs=[{"function": {"name": "edit_file"}},
                               {"function": {"name": "task_complete"}}],
                       grace_round_used=False, ld_setter=_ld_lines.append)
check("first bundled round gets the one-shot nudge (continue)",
      _g.action == "continue" and _g.grace_round_used is True
      and not _g.chunk_budget_exhausted and not _g.loop_detected
      and _ld_lines == [] and len(_g.events) == 1)
check("nudge names the rejected tools and the message shape",
      _g.events[0]["role"] == "user"
      and _g.events[0]["content"].startswith("[GRACE ROUND] Call ONLY task_complete")
      and "bundled 1 other tool(s) (edit_file)" in _g.events[0]["content"])

_g = _grace_round_gate(grace_round_active=True,
                       dr_tcs=[{"function": {"name": "run_bash"}}],
                       grace_round_used=True, ld_setter=_ld_lines.append)
check("expired grace round stops with budget exhaustion",
      _g.action == "break" and _g.chunk_budget_exhausted is True
      and _g.loop_detected is True and _g.grace_round_used is True
      and _ld_lines == [3137] and len(_g.events) == 1
      and _g.events[0]["content"].startswith("[GRACE ROUND EXPIRED]"))


# ── 2. max-rounds gate ───────────────────────────────────────────────────
_lmr_lines: list[int] = []
_fc = {"a.py": 1, "b.py": 2}

# below budget: no-op (nothing emitted, nothing stamped, inputs echoed)
_g = _max_rounds_gate(total_tool_rounds=9, max_tool_rounds=10,
                      grace_round_active=False,
                      verify_mutation_serial=5, verify_last_ok_serial=2,
                      file_changes=_fc, ld_setter=_lmr_lines.append)
check("max rounds: below budget is a no-op",
      _g.action == "proceed" and _g.grace_round_active is False
      and _g.max_tool_rounds == 10 and _g.chunk_budget_exhausted is False
      and not _g.events and not _g.messages and _lmr_lines == [])

# first exhaustion: arms the grace round
_g = _max_rounds_gate(total_tool_rounds=10, max_tool_rounds=10,
                      grace_round_active=False,
                      verify_mutation_serial=3, verify_last_ok_serial=3,
                      file_changes=_fc, ld_setter=_lmr_lines.append)
check("max rounds: first exhaustion arms the grace round (+1 budget)",
      _g.action == "proceed" and _g.grace_round_active is True
      and _g.max_tool_rounds == 11 and _g.chunk_budget_exhausted is False
      and _lmr_lines == [] and len(_g.messages) == 1 and len(_g.events) == 1)
check("max rounds: exact [GRACE ROUND] wrap-up nudge message",
      _g.messages[0]["role"] == "user"
      and _g.messages[0]["content"] == (
          "[GRACE ROUND] You have 1 final round. "
          "Call task_complete now with what was completed, "
          "any blockers, and build_status. No other tool calls."))
check("max rounds: exact grace status (renders the pre-arming budget)",
      _g.events[0] == {"type": "status",
          "content": "\u23f8 Tool budget exhausted (10/10 rounds) "
                     "\u2014 granting one grace round to wrap up."})

# second exhaustion: chunk containment stop
_lmr_lines.clear()
_g = _max_rounds_gate(total_tool_rounds=11, max_tool_rounds=11,
                      grace_round_active=True,
                      verify_mutation_serial=3, verify_last_ok_serial=3,
                      file_changes=_fc, ld_setter=_lmr_lines.append)
check("max rounds: second exhaustion ends the chunk (break)",
      _g.action == "break" and _g.chunk_budget_exhausted is True
      and _g.grace_round_active is True and _g.max_tool_rounds == 11
      and _lmr_lines == [3551] and not _g.messages and len(_g.events) == 1)
check("max rounds: exact expired status (budget minus the grace slot)",
      _g.events[0]["content"] ==
      "\u23f9 Tool budget exhausted (11/10 rounds incl. grace) "
      "\u2014 ending this chunk.")

# unverified-files warning: prepended when the mutation serial is ahead
_fc12 = {f"f{i}.py": i for i in range(12)}
_g = _max_rounds_gate(total_tool_rounds=10, max_tool_rounds=10,
                      grace_round_active=False,
                      verify_mutation_serial=5, verify_last_ok_serial=2,
                      file_changes=_fc12, ld_setter=_lmr_lines.append)
check("max rounds: unverified-files status first, exact wording",
      len(_g.events) == 2
      and _g.events[0]["content"] ==
      "\u26a0\ufe0f Budget exhausted with 12 unverified file(s): "
      "f0.py, f1.py, f10.py, f11.py, f2.py, f3.py, f4.py, f5.py, f6.py, "
      "f7.py and 2 more \u2014 changes may be broken."
      and _g.events[1]["content"].startswith("\u23f8 Tool budget exhausted"))
_g = _max_rounds_gate(total_tool_rounds=10, max_tool_rounds=10,
                      grace_round_active=False,
                      verify_mutation_serial=5, verify_last_ok_serial=2,
                      file_changes={}, ld_setter=_lmr_lines.append)
check("max rounds: no unverified status with matching serials or no changes",
      len(_g.events) == 1
      and _g.events[0]["content"].startswith("\u23f8 Tool budget exhausted"))


# ── 3. auto-test gate ────────────────────────────────────────────────────
class _FakeTestResult:
    language = "python"
    failure_count = 3


class _FakeCS:
    """Minimal ChunkState double: records the exact kwargs it is called with."""

    def __init__(self, is_clean, action=None, rerun=False):
        self._is_clean = is_clean
        self._action = action
        self.rerun_without_fix = rerun
        self.test_retries = 1
        self.max_test_retries = 2
        self.run_kwargs = None
        self.handle_kwargs = None

    async def run_auto_test(self, **kw):
        self.run_kwargs = kw
        return _FakeTestResult(), self._is_clean

    def handle_auto_test_result(self, **kw):
        self.handle_kwargs = kw
        return self._action


class _FakeEmit:
    pass  # identity marker: the helper must pass emit_fn through untouched


_emit = _FakeEmit()


def _run(cs, **over):
    kw = dict(workspace="/ws", di=3, n_items=5, subtask="task b",
              emit_fn=_emit, chat_id="chat-9")
    kw.update(over)
    return asyncio.run(_run_auto_test_gate(cs, **kw))


# clean verdict (both call sites share this wording)
_cs = _FakeCS(is_clean=True)
_r = _run(_cs)
check("clean: no failure policy runs, action None",
      _r.is_clean is True and _r.action is None
      and _cs.handle_kwargs is None and len(_r.events) == 1)
check("clean: exact status wording",
      _r.events[0]["content"] == "\u2705 Tests passed (python)")
check("clean: run_auto_test called without chat_id (agentic site)",
      _cs.run_kwargs == {"workspace": "/ws", "di": 3, "n_items": 5,
                         "emit_fn": _emit, "test_timeout": 90})

_cs = _FakeCS(is_clean=True)
_run(_cs, include_chat_id=True)
check("clean: run_auto_test receives chat_id on the critic site",
      _cs.run_kwargs.get("chat_id") == "chat-9"
      and _cs.run_kwargs.get("test_timeout") == 90)

# flaky rerun without LLM
_cs = _FakeCS(is_clean=False, action=ChunkAction.CONTINUE, rerun=True)
_r = _run(_cs)
check("flaky: rerun_without_fix is reset exactly in the flaky branch",
      _cs.rerun_without_fix is False
      and _r.action == ChunkAction.CONTINUE and len(_r.events) == 1)
check("flaky: exact status wording (1/2 retries)",
      _r.events[0]["content"]
      == "🔁 Flaky test detected — re-running without LLM (1/2)")

# re-awaken fix round (rerun_without_fix stays untouched)
_cs = _FakeCS(is_clean=False, action=ChunkAction.CONTINUE, rerun=False)
_r = _run(_cs)
check("fix round: rerun_without_fix is NOT reset",
      _cs.rerun_without_fix is False
      and _r.action == ChunkAction.CONTINUE)
check("fix round: exact status wording",
      _r.events[0]["content"]
      == "\U0001f501 Chunk 4: 3 test failures — fix round (1/2)")
check("fix round: handle_auto_test_result gets the full kwargs",
      _cs.handle_kwargs == {"test_result": _cs.handle_kwargs["test_result"],
                            "subtask": "task b", "di": 3, "n_items": 5,
                            "emit_fn": _emit, "chat_id": "chat-9"})

# max retries reached — the two call-site wordings differ by one word
_cs = _FakeCS(is_clean=False, action=ChunkAction.INCREMENT, rerun=True)
_r = _run(_cs)
check("max retries (agentic): rerun_without_fix reset, 'remain' wording",
      _cs.rerun_without_fix is False
      and _r.action == ChunkAction.INCREMENT
      and _r.events[0]["content"]
      == "\u26a0\ufe0f Chunk 4: test failures remain after 3 attempts — note and continue")

_cs = _FakeCS(is_clean=False, action=ChunkAction.BREAK)
_r = _run(_cs, fail_final_verb="persist")
check("max retries (critic site): 'persist' wording",
      _r.events[0]["content"]
      == "\u26a0\ufe0f Chunk 4: test failures persist after 3 attempts — note and continue")

# ── 4. tool-budget gate ──────────────────────────────────────────────────
_tb_ld: list[int] = []


def _tb(**over):
    kw = dict(max_tokens_round=2000, min_viable_budget=4096,
              ctx_full_skips=0, est_now=9000, dtool_ctx=10000,
              guard_tokens=5000, can_compress=False, total_tool_rounds=3,
              dr=5, last_force_compress_round=None,
              ld_setter=lambda line: _tb_ld.append(line))
    kw.update(over)
    return _tool_budget_gate(**kw)


_t = _tb(max_tokens_round=5000, ctx_full_skips=2)
check("budget above the floor resets the skip streak",
      _t.action == "proceed" and _t.ctx_full_skips == 0
      and _t.max_tokens_round == 5000 and not _t.events)

_t = _tb(max_tokens_round=300)
check("budget below the floor keeps the 512-token fallback",
      _t.action == "proceed" and _t.ctx_full_skips == 1
      and _t.max_tokens_round == 512)

_t = _tb(ctx_full_skips=3)
check("fourth unusable round stops the run",
      _t.action == "break" and _t.loop_detected is True
      and _t.ctx_full_skips == 4 and _tb_ld == [3726]
      and len(_t.events) == 1)

_orig_afc = _G._allow_force_compress
try:
    _G._allow_force_compress = lambda *_a, **_k: True
    _t = _tb(can_compress=True)
    check("allowed force compression continues with a status",
          _t.action == "continue" and _t.force_compress_next is True
          and _t.last_force_compress_round == 5 and len(_t.events) == 1)

    _G._allow_force_compress = lambda *_a, **_k: False
    _t = _tb(can_compress=True, ctx_full_skips=1)
    check("storm guard throttles the force compression",
          _t.action == "continue" and _t.force_compress_next is False
          and _t.last_force_compress_round is None and not _t.events)
finally:
    _G._allow_force_compress = _orig_afc

# ── 5. source pins: the runner keeps loop control and the bookkeeping ────
_dr = (_ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8",
                                                   errors="replace")
_gm = (_ROOT / "core" / "duo_round_gate.py").read_text(encoding="utf-8")

check("grace gate: runner owns the think-only bump paired with continue",
      'if _gr.action == "continue":' in _dr
      and "_dr_think_only_retries += 1" in _dr.split('if _gr.action == "continue":')[1]
      .split('if _gr.action ==')[0])
check("grace gate: runner syncs budget/loop flags and the used marker",
      "_grace_round_used = _gr.grace_round_used" in _dr
      and "if _gr.chunk_budget_exhausted:" in _dr and "if _gr.loop_detected:" in _dr)
check("grace gate: the ld stamp 3137 lives in the helper now",
      "3137" not in _dr.split("GRACE-ROUND GATE")[1].split("_exec_ps")[0]
      and "ld_setter(3137)" in _gm)
check("max rounds: runner keeps the loop guard and the break",
      "if (not _loop_detected and not ctx.aborted()" in _dr
      and "and _total_tool_rounds >= _max_tool_rounds):" in _dr
      and 'if _mr.action == "break":' in _dr)
_seg_mr = _dr.split("_mr = _max_rounds_gate(")[1]
_seg_mr = _seg_mr[:_seg_mr.index('if _mr.action == "break":')]
check("max rounds: runner appends messages, then emits events in order",
      "_dtool_msgs.extend(_mr.messages)" in _seg_mr
      and "for _mr_ev in _mr.events:" in _seg_mr
      and "yield await ctx.emit(_mr_ev)" in _seg_mr
      and _seg_mr.index("_dtool_msgs.extend(_mr.messages)")
      < _seg_mr.index("for _mr_ev in _mr.events:"))
check("max rounds: runner syncs grace flag + budget, exhausted flag only set",
      "_grace_round_active = _mr.grace_round_active" in _seg_mr
      and "_max_tool_rounds = _mr.max_tool_rounds" in _seg_mr
      and "if _mr.chunk_budget_exhausted:" in _seg_mr
      and "_chunk_budget_exhausted = _mr.chunk_budget_exhausted" not in _dr)
check("max rounds: the 3551 ld stamp + nudge wording live in the helper now",
      "3551" not in _dr
      and "[GRACE ROUND] You have 1 final round" not in _dr
      and "ld_setter(3551)" in _gm
      and "[GRACE ROUND] You have 1 final round" in _gm)
check("agentic site: CONTINUE keeps the re-awaken loop, no _di bump",
      "_at.action == ChunkAction.CONTINUE" in _dr
      and _dr.count("continue  # re-run auto-test / fix round on same chunk") == 1)
check("critic site: mark_chunk_done stays AFTER the auto-test verdict",
      _dr.rindex("_cs.mark_chunk_done")
      > _dr.rindex("_at = await _run_auto_test_gate("))
_seg_c = _dr.split("elif _at.action == ChunkAction.CONTINUE:")[1]
_seg_c = _seg_c[:_seg_c.index("# No auto-test or no files written")]
# F13 (2026-11-08): the advance/reset/checkpoint trio is now a runner-local
# closure (_advance_chunk) so every chunk_checkpoint site shares it; the
# bookkeeping must stay IN the runner (reset + checkpoint in the closure
# body), and the critic site must call it instead of mark_chunk_done.
_seg_adv = _dr.split("def _advance_chunk() -> None:")[1]
_seg_adv = _seg_adv[:_seg_adv.index("_drop_names: list = []")]
check("critic site: advance/reset/checkpoint bookkeeping kept in the runner",
      "continue  # DON'T increment _di" in _seg_c
      and "_advance_chunk()" in _seg_c
      and "_cs.mark_chunk_done" not in _seg_c
      and "_cs.reset_test_retries()" in _seg_adv
      and '"chunk_checkpoint"' in _seg_adv
      and "nonlocal _di" in _seg_adv)

sys.exit(h.finish("duo_round_gate"))
