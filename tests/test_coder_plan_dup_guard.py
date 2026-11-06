# -*- coding: utf-8 -*-
"""The coder must not be handed a second plan-format instruction.

Live report (2026-11-03, big-prompt coding run): the planner produced a plan
("## Plan: ... ### Open Tasks ...") and the CODER still wrote a plan in the
same planner markdown format afterwards. Cause: the in-loop planner gate in
run_code_duo only checked the planner TOGGLE (``not duo_config.planner``),
not whether a plan already exists. The UI forces duo_agentic_thinking=True
whenever chunking is on (static/app.js onChunkingChange), so every chunked
run with the planner toggle off ran the planner phase AND then asked the
coder model (exec_mdl) via run_inloop_planner + make_thinking_planner_sys to
produce a SECOND plan in the planner's markdown format — streamed as visible
answer text ("type": "token" with the "**Plan:**" label), NOT as
reasoning_content.

The gate now also requires "no plan in this run" (duo_helpers.
inloop_plan_should_run). Agentic runs without a planner (in-loop plan is the
only plan) and follow-ups (planner phase skipped, no plan in run) keep the
in-loop planner; the compression plan-pin is untouched.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RUN = ((ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
       + (ROOT / "core" / "duo_inloop_planner.py").read_text(encoding="utf-8")
       + (ROOT / "core" / "duo_compress_cycle.py").read_text(encoding="utf-8"))
HELP = (ROOT / "core" / "duo_helpers.py").read_text(encoding="utf-8")
PROMPTS_PY = (ROOT / "hive_functions" / "prompts.py").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


from core.duo_helpers import inloop_plan_should_run, _build_duo_coder_sys  # noqa: E402


class _PromptCtx:
    """Minimal ctx for _build_duo_coder_sys (same shape as test_vision_gate)."""
    settings = {}
    workspace = ""
    websearch_available = False
    active_preset = None
    use_learned = False

    class duo_config:
        test_feedback_chunk = False
        test_feedback_final = False
        until_finished = False

    def get_effective_prompt_with_override(self, *_a, **_k):
        return ""


# ── 1. the gate: big-prompt chunked run + existing planner plan ───────────
# Exact live configuration: duo_chunking=true (settings), planner toggle off,
# UI forces agentic_thinking=True, planner phase produced a plan.
BIG_PROMPT = "Build a complete, polished Tetris game ... " * 200  # ~7k chars, big prompt
check("1a big prompt does not change the decision",
      inloop_plan_should_run(
          agentic_thinking=True, thinking_per_chunk=False,
          aborted=False, planner_enabled=False,
          has_plan=bool(BIG_PROMPT) and True,   # plan content exists
      ) is False,
      "(plan exists -> no in-loop re-plan)")
check("1b chunked run + UI-forced thinking + planner off + plan exists: no second plan",
      inloop_plan_should_run(True, False, False, False, True) is False)
check("1c thinking_per_chunk + plan exists: no per-chunk re-plan either",
      inloop_plan_should_run(False, True, False, False, True) is False)
check("1d planner toggle on: unchanged (already gated)",
      inloop_plan_should_run(True, True, False, True, False) is False)
check("1e aborted: no planning",
      inloop_plan_should_run(True, False, True, False, False) is False)

# ── 2. the gate: kept behavior ────────────────────────────────────────────
check("2a agentic, planner off, thinking on, NO plan: in-loop plan still runs",
      inloop_plan_should_run(True, False, False, False, False) is True)
check("2b follow-up (planner phase skipped -> no plan in run): in-loop plan still runs",
      inloop_plan_should_run(True, False, False, False, False) is True)
check("2c planner ran but produced nothing parseable (has_plan=False): rescue kept",
      inloop_plan_should_run(False, True, False, False, False) is True)
check("2d planner completely off (planner + chunking off): no in-loop plan",
      inloop_plan_should_run(True, True, False, False, False,
                             chunking_enabled=False) is False)

# ── 3. the runner uses the plan-aware gate ────────────────────────────────
check("3a the runner delegates the gate to inloop_plan_should_run",
      "_run_thinking_now = _inloop_plan_should_run(" in RUN
      and "inloop_plan_should_run as _inloop_plan_should_run" in RUN)
_i_gate = RUN.index("_run_thinking_now = _inloop_plan_should_run(")
_gate_call = RUN[_i_gate:_i_gate + 600]
check("3b the gate passes the real plan-existence flag (_has_plan)",
      "_has_plan," in _gate_call)
check("3c the gate still passes the planner toggle + thinking toggles",
      "ctx.duo_config.agentic_thinking" in _gate_call
      and "ctx.duo_config.thinking_per_chunk" in _gate_call
      and "ctx.duo_config.planner" in _gate_call
      and "chunking_enabled=ctx.duo_config.chunking" in _gate_call)
check("3d the old toggle-only gate is gone",
      "and not ctx.duo_config.planner\n                )" not in RUN)

# ── 4. the coder system prompt under a big prompt + existing plan ─────────
sys_with_plan = _build_duo_coder_sys(
    _PromptCtx(), has_plan=True, has_subtasks=False, has_explore_ctx=False)
check("4a with a plan the coder gets EXECUTION mode (plan already decided)",
      "The plan is already decided" in sys_with_plan)
check("4b the coder system prompt carries NO planner-format template",
      "## Plan:" not in sys_with_plan and "### Open Tasks" not in sys_with_plan)
sys_no_plan = _build_duo_coder_sys(
    _PromptCtx(), has_plan=False, has_subtasks=False, has_explore_ctx=False)
check("4c without a plan the coder still gets AUTONOMOUS mode",
      "AUTONOMOUS MODE" in sys_no_plan and "A plan already exists" not in sys_no_plan)

# ── 5. the second plan was visible answer text, not reasoning ─────────────
# (documents the failure mode the guard removes: the in-loop plan block must
# stay behind the gate; its emit remains a token event when it DOES run)
# split 2.6: the gated block + emit live in duo_inloop_planner.py; the
# runner delegates it before the PLAN-TO-EXEC log
_IP = (ROOT / "core" / "duo_inloop_planner.py").read_text(encoding="utf-8")
_i_gate_if = _IP.index("if _run_thinking_now:")
_i_emit = _IP.index('"content": "\\n**" + _plan_label + ":**\\n" + _inloop_plan_text')
_i_after_block = RUN.index("[PLAN-TO-EXEC]")          # first log AFTER the gated block
check("5a the in-loop plan emit sits inside the gated block",
      _i_gate_if < _i_emit
      and RUN.index("async for _ev in _duo_inloop_planner") < _i_after_block)
check("5b the in-loop plan emit is a visible token event",
      '{"type": "token"' in _IP[_i_emit - 200:_i_emit + 200]
      or '{"type": "token", "content"' in _IP[_i_emit - 200:_i_emit + 300])

# ── 6. neighbors untouched ────────────────────────────────────────────────
check("6a follow-up hint still reaches the coder system prompt",
      '_follow_up_hint = state.get("_follow_up_hint", "") or ""' in RUN
      and '_duo_coder_sys += "\\n\\n" + _follow_up_hint' in RUN)
check("6b the planner phase still runs on chunking-or-planner",
      "(ctx.duo_config.chunking or ctx.duo_config.planner) and not ctx.aborted()" in RUN)
check("6c follow-up runs still skip the planner phase",
      "and not _planner_skipped and not _follow_up_active" in RUN)
check("6d compression plan-pin helper still exists",
      "_build_plan_pin_msg" in (ROOT / "core" / "duo" / "preflight.py").read_text(encoding="utf-8")
      and "_build_plan_pin_msg(_subtasks, _di, _n_items)" in RUN)
check("6e the plan-format template stays with the real planners only",
      PROMPTS_PY.count("### Open Tasks") == 0
      and (ROOT / "hive_functions" / "planner.py").read_text(encoding="utf-8").count("### Open Tasks") >= 1)

# ── 7. planner completely off: no chunking, no plan, only the user prompt ──
check("7a a stale chunking setting is cleared when the planner is off",
      "[PLANNER-OFF] chunking disabled" in RUN)
_appjs = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
check("7b the UI clears chunking when the planner is switched off",
      "S.duoChunking = false" in _appjs
      and "postSettings({duo_chunking: false})" in _appjs)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
