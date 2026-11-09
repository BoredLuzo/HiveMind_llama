"""Per-round gate policies for run_code_duo (F13, 2026-11-08).

Small decision helpers moved VERBATIM out of run_code_duo; the runner
keeps loop control and syncs the returned fields back into its locals:

  - the grace-round gate (CHUNK-CONTAINMENT): after a coder round inside
    an exhausted-budget grace round, decide between the one-shot
    task_complete nudge (continue), the expired stop (break) and
    fallthrough to the exec round;
  - the max-rounds gate (GRACE-ROUND-FIX / CHUNK-CONTAINMENT): when the
    coder tool budget is exhausted, optionally warn about unverified
    files, arm the one grace round (budget +1 + nudge message) or, if it
    is already armed, end THIS chunk (budget-exhausted flag, ld stamp,
    break). The runner keeps the loop control (the loop_detected /
    aborted / below-budget guard and the break itself);
  - the auto-test gate: run the post-chunk auto-test and apply the
    shared failure policy (flaky rerun / re-awaken fix round / note and
    continue) for BOTH call sites (agentic QC and critic-approved).
    Caller-specific bookkeeping (mark_chunk_done, _di advance,
    chunk_checkpoint) stays in run_code_duo.

They return a small result object; status events are returned for the
caller to emit in order (the grace gate's "events" are the user-role
messages the runner appends to the coder message list; the max-rounds
gate returns its status dicts in "events" and its user-role messages in
"messages").
"""
from __future__ import annotations

import dataclasses
import logging

from context.ctx_guard import (
    allow_force_compress as _allow_force_compress,
    DEFAULT_EMERGENCY_PCT as _EMERGENCY_PCT,
)
from hive_functions.chunking import ChunkAction

logger = logging.getLogger("hivemind.duo")


@dataclasses.dataclass
class _GraceRoundGateResult:
    """Outcome of the grace-round check after a coder round with tool calls."""
    action: str = "proceed"          # "continue" | "break" | "proceed"
    grace_round_used: bool = False   # echoes the input on proceed/continue-input
    chunk_budget_exhausted: bool = False
    loop_detected: bool = False
    events: list = dataclasses.field(default_factory=list)
    # events: user-role messages the runner must append to _dtool_msgs,
    # in order (nothing is emitted to the user stream here).


def _grace_round_gate(
    *, grace_round_active, dr_tcs, grace_round_used, ld_setter,
) -> _GraceRoundGateResult:
    """Decide the next step when a grace round produced non-task_complete calls.

    Moved verbatim from the `if _grace_round_active and _dr_tcs:` block in
    run_code_duo's tool-round loop: the one-shot "[GRACE ROUND]" nudge and
    the expired "[GRACE ROUND EXPIRED]" stop (budget exhausted, loop
    stamped via ld_setter). The runner owns the break/continue and the
    `_dr_think_only_retries` bump (paired with action == "continue").
    """
    res = _GraceRoundGateResult(grace_round_used=grace_round_used)
    if not (grace_round_active and dr_tcs):
        return res
    _non_tc = [tc for tc in dr_tcs
               if tc.get("function", {}).get("name", "") != "task_complete"]
    if not _non_tc:
        return res
    if grace_round_used:
        res.events.append({"role": "user", "content": (
            "[GRACE ROUND EXPIRED] Grace round already used — exiting now. "
            "The run will end."
        )})
        res.chunk_budget_exhausted = True
        ld_setter(3137); res.loop_detected = True
        res.action = "break"
        return res
    res.grace_round_used = True
    _non_tc_names = [tc.get("function", {}).get("name", "?") for tc in _non_tc]
    res.events.append({"role": "user", "content": (
        f"[GRACE ROUND] Call ONLY task_complete — no other tools. "
        f"Your last round bundled {len(_non_tc)} other tool(s) "
        f"({', '.join(_non_tc_names[:5])}) which were rejected."
    )})
    res.action = "continue"
    return res


@dataclasses.dataclass
class _MaxRoundsGateResult:
    """Outcome of the exhausted-budget check at the top of a tool round."""
    action: str = "proceed"          # "proceed" | "break"
    grace_round_active: bool = False # echoes the input (True once armed)
    max_tool_rounds: int = 0         # echoes the input (+1 when grace armed)
    chunk_budget_exhausted: bool = False
    events: list = dataclasses.field(default_factory=list)
    # events: status dicts the runner must yield via ctx.emit, in order
    # (unverified-files warning first, then the budget status).
    messages: list = dataclasses.field(default_factory=list)
    # messages: user-role dicts the runner must append to _dtool_msgs, in
    # order (the one-shot "[GRACE ROUND]" wrap-up nudge).


def _max_rounds_gate(
    *, total_tool_rounds, max_tool_rounds, grace_round_active,
    verify_mutation_serial, verify_last_ok_serial, file_changes, ld_setter,
) -> _MaxRoundsGateResult:
    """Decide what happens when the coder tool budget is exhausted.

    Moved verbatim from the `if (not _loop_detected and not ctx.aborted()
    and _total_tool_rounds >= _max_tool_rounds):` block in run_code_duo's
    tool-round loop. The runner keeps the loop control (the loop_detected /
    aborted guard and the break) and calls this once the budget is hit;
    the helper re-checks the budget defensively and no-ops below it.

    Branches (in order):
      - below budget: no-op (fields echo the inputs, nothing emitted);
      - unverified mutation(s) left: prepend the exact "Budget exhausted
        with N unverified file(s)" status (top-10 names, "and X more");
      - grace round not armed yet: arm it (grace_round_active, budget +1),
        return the exact "[GRACE ROUND] You have 1 final round" user
        message and the "granting one grace round" status;
      - grace round already armed: end THIS chunk only —
        chunk_budget_exhausted, ld stamp 3551, the "ending this chunk"
        status and action "break" (the run continues with the next chunk;
        no exec_ctrl.abort here).
    """
    res = _MaxRoundsGateResult(
        grace_round_active=grace_round_active,
        max_tool_rounds=max_tool_rounds,
    )
    if total_tool_rounds < max_tool_rounds:
        return res
    if verify_mutation_serial > verify_last_ok_serial and file_changes:
        _uv_files = ", ".join(sorted(file_changes.keys())[:10])
        _uv_count = len(file_changes)
        if _uv_count > 10:
            _uv_files += f" and {_uv_count - 10} more"
        res.events.append({"type": "status",
            "content": f"\u26a0\ufe0f Budget exhausted with {_uv_count} unverified file(s): {_uv_files} — changes may be broken."})
    if not grace_round_active:
        # GRACE-ROUND-FIX: the round loop runs two extra
        # slots; this arms the first one. The old code set
        # _loop_detected + abort here, so the grace prompt
        # was appended but the round never ran.
        res.grace_round_active = True
        res.max_tool_rounds = max_tool_rounds + 1
        res.messages.append({"role": "user", "content": (
            "[GRACE ROUND] You have 1 final round. "
            "Call task_complete now with what was completed, "
            "any blockers, and build_status. No other tool calls."
        )})
        res.events.append({"type": "status",
            "content": f"\u23f8 Tool budget exhausted ({total_tool_rounds}/{max_tool_rounds} rounds) \u2014 granting one grace round to wrap up."})
        return res
    # CHUNK-CONTAINMENT: grace round spent without
    # task_complete -> end THIS chunk only. No
    # exec_ctrl.abort anymore: the run continues with
    # the next chunk (gate after the round loop).
    res.chunk_budget_exhausted = True
    ld_setter(3551)
    res.events.append({"type": "status",
        "content": f"\u23f9 Tool budget exhausted ({total_tool_rounds}/{max_tool_rounds - 1} rounds incl. grace) \u2014 ending this chunk."})
    res.action = "break"
    return res


@dataclasses.dataclass
class _ToolBudgetGateResult:
    """Outcome of the per-round tool-output budget check."""
    action: str = "proceed"          # "continue" | "break" | "proceed"
    max_tokens_round: int = 0
    ctx_full_skips: int = 0
    force_compress_next: bool = False
    last_force_compress_round: int | None = None
    loop_detected: bool = False
    events: list = dataclasses.field(default_factory=list)


def _tool_budget_gate(
    *, max_tokens_round, min_viable_budget, ctx_full_skips, est_now,
    dtool_ctx, guard_tokens, can_compress, total_tool_rounds, dr,
    last_force_compress_round, ld_setter,
) -> _ToolBudgetGateResult:
    """Decide after a context-derived tool-output budget was computed.

    Moved verbatim from the CTX-FULL block: 4 consecutive unusable rounds
    stop the run; otherwise a force compression is requested (storm-guarded)
    or the tiny-budget fallback kept. The runner owns break/continue.
    """
    res = _ToolBudgetGateResult(
        max_tokens_round=int(max_tokens_round),
        ctx_full_skips=ctx_full_skips,
        last_force_compress_round=last_force_compress_round,
    )
    if int(max_tokens_round) >= min_viable_budget:
        res.ctx_full_skips = 0
        return res
    res.ctx_full_skips += 1
    if res.ctx_full_skips >= 4:
        logger.error(
            "[CTX-FULL] 4 consecutive rounds without enough headroom "
            "(est=%d ctx=%d) - compression cannot shrink enough",
            int(est_now), int(dtool_ctx),
        )
        res.events.append({
            "type": "status",
            "content": "⛔ Context cannot be reduced enough for a tool round — run stopped.",
        })
        ld_setter(3726)
        res.loop_detected = True
        res.action = "break"
        return res
    if ((can_compress or guard_tokens > int(dtool_ctx * _EMERGENCY_PCT))
            and total_tool_rounds > 0):
        _ctx_emergency = (
            guard_tokens > int(dtool_ctx * _EMERGENCY_PCT)
            or res.ctx_full_skips >= 2
        )
        if not _allow_force_compress(dr, last_force_compress_round, 3,
                                     emergency=_ctx_emergency):
            logger.warning(
                "[CTX-FULL] storm guard: force compression throttled "
                "(last=%d round=%d) - skipping this round instead of "
                "shipping a doomed budget",
                last_force_compress_round, dr,
            )
            res.action = "continue"
            return res
        logger.warning(
            "[CTX-FULL] insufficient headroom for a safe tool round "
            "(max_tokens=%d est=%d real=%d skip=%d/4) - compressing first",
            int(max_tokens_round), int(est_now), int(guard_tokens),
            res.ctx_full_skips,
        )
        res.events.append({
            "type": "status",
            "content": f"⛽ Context too full for a safe tool round — compressing first. ({res.ctx_full_skips}/4)",
        })
        res.force_compress_next = True
        res.last_force_compress_round = dr
        res.action = "continue"
        return res
    # Compression exhausted & not at emergency level: keep the old
    # tiny-budget behaviour instead of looping.
    res.max_tokens_round = max(int(max_tokens_round), 512)
    return res


@dataclasses.dataclass
class _AutoTestGateResult:
    """Outcome of one post-chunk auto-test (execution + failure policy)."""
    test_result: object = None
    is_clean: bool = False
    action: ChunkAction | None = None  # None when the test was clean
    events: list = dataclasses.field(default_factory=list)


async def _run_auto_test_gate(
    cs, *, workspace, di, n_items, subtask, emit_fn,
    chat_id, include_chat_id=False, fail_final_verb="remain",
) -> _AutoTestGateResult:
    """Run the chunk auto-test and apply the shared failure policy.

    Moved verbatim from the duplicated agentic / critic-approved blocks in
    run_code_duo. ``run_auto_test`` receives ``chat_id`` only when
    ``include_chat_id`` is set: the agentic call site historically omits
    the kwarg, the critic-approved site passes it (byte-equivalent to the
    old calls). ``handle_auto_test_result`` always receives ``chat_id``.
    ``rerun_without_fix`` is reset exactly where the old code did (flaky
    branch and max-retries branch, never the fix-round branch).

    ``fail_final_verb`` preserves the two wordings of the max-retries
    status: "remain" (agentic) vs "persist" (critic-approved).

    Caller-specific logic stays in run_code_duo: mark_chunk_done, the
    _di advance / reset_test_retries / chunk_checkpoint and the
    break-on-last-chunk.
    """
    if include_chat_id:
        _chunk_test_result, _is_clean = await cs.run_auto_test(
            workspace=workspace,
            di=di,
            n_items=n_items,
            emit_fn=emit_fn,
            test_timeout=90,
            chat_id=chat_id,
        )
    else:
        _chunk_test_result, _is_clean = await cs.run_auto_test(
            workspace=workspace,
            di=di,
            n_items=n_items,
            emit_fn=emit_fn,
            test_timeout=90,
        )
    res = _AutoTestGateResult(test_result=_chunk_test_result, is_clean=_is_clean)
    if _is_clean:
        res.events.append({"type": "status",
            "content": f"\u2705 Tests passed ({_chunk_test_result.language})"})
        return res
    _action = cs.handle_auto_test_result(
        test_result=_chunk_test_result,
        subtask=subtask,
        di=di,
        n_items=n_items,
        emit_fn=emit_fn,
        chat_id=chat_id,
    )
    res.action = _action
    if _action == ChunkAction.CONTINUE:
        # Check for flaky-test rerun without LLM
        if cs.rerun_without_fix:
            cs.rerun_without_fix = False
            res.events.append({"type": "status",
                "content": f"🔁 Flaky test detected — re-running without LLM ({cs.test_retries}/2)"})
            return res
        # RE-AWAKEN: Stay on same chunk, fix test failures
        res.events.append({"type": "status",
            "content": f"\U0001f501 Chunk {di+1}: {_chunk_test_result.failure_count} test failures — fix round ({cs.test_retries}/{cs.max_test_retries})"})
        return res
    # Max retries reached — note and move on
    cs.rerun_without_fix = False
    res.events.append({"type": "status",
        "content": f"\u26a0\ufe0f Chunk {di+1}: test failures {fail_final_verb} after {cs.max_test_retries+1} attempts — note and continue"})
    return res
