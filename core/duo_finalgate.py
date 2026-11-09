"""Duo final verify gate (2026-11-05 split 2.3).

Runs after the chunk loop: the run-timeout token and the final verification
hard stop (unverified writes) including the resume-block write. Pure emit
order, sets `_duo_hard_stop`/`final_verdict`/`critic_issues` back through
the state object.
"""
from __future__ import annotations

import dataclasses

import logging

from core.duo.preflight import needs_test_verification

logger = logging.getLogger("hivemind.duo")


async def _duo_final_verify_gate(ctx, ps):
    from context.resume import _write_resume_block
    if ps._duo_timed_out:
        if ps._written_files:
            yield await ctx.emit({
                "type": "token",
                "content": (
                    f"\n[Run-Timeout-Guard: intermediate state output — {len(ps._written_files)} file(s) on disk. "
                    "For full execution: ctx.duo_config.important_task=true or ctx.duo_config.until_finished enable.]\n"
                ),
            })
        else:
            yield await ctx.emit({
                "type": "token",
                "content": (
                    "\n[Run-Timeout: no output on disk — the model only planned, did not write. "
                    "Tip: ctx.duo_config.until_finished=true gives the coder more time for tool calls.]\n"
                ),
            })
            yield await ctx.emit({
                "type": "status",
                "content": "⏱ Timeout without file output — no intermediate state available.",
            })

    # unverified writes -> block correctly (true-positive protection,
    # written_files=[]-true-positives) must not slip through.
    if (
        ps._verify_mutation_serial > ps._verify_last_ok_serial
        and needs_test_verification(ps._written_files)
        and not ctx.aborted()
        and not ps._verify_warned
    ):
        ps._duo_hard_stop = True
        _verify_issue_final = "Verification missing: run_bash must pass after latest file changes."
        if _verify_issue_final not in ps.critic_issues:
            ps.critic_issues.append(_verify_issue_final)
        if not ps.final_verdict:
            ps.final_verdict = "verification_required_after_write"
        yield await ctx.emit({
            "type": "status",
            "content": "⛔ Run not marked completed: verification after file changes is missing.",
        })
        _vg_start = ps._di if ps._di < ps._n_items else ps._n_items - 1
        _rem_vg = [{"title": str(t)} for t in ps._loop_items[_vg_start:]]
        _rem_vg_titles = {r["title"] for r in _rem_vg}
        _done_vg = [t for t in ps._done_tasks if str(t) not in _rem_vg_titles]
        if ctx.chat_id and _rem_vg:
            _write_resume_block(
                chat_id=ctx.chat_id,
                workspace=ps._ws_str,
                chunks_total=ps._n_items,
                chunks_done=_done_vg,
                chunks_remaining=_rem_vg,
                written_files=ps._written_files,
                last_summary=" | ".join(_done_vg),
                plan_msgs=[],
                explore_ctx=ps._explore_ctx,
                halt_reason="verification_required",
                pending_send_files=list(ps._pending_send_files),
            )
    elif (ps._verify_mutation_serial > ps._verify_last_ok_serial
          and needs_test_verification(ps._written_files)
          and not ctx.aborted()):
        logger.warning(
            "[VERIFY-GATE-FINAL] no hard stop (coder warned): written_files=%d _verify_warned=True (A-P0-2 fallback)",
            len(ps._written_files or []),
        )



@dataclasses.dataclass
class _DuoFinalGateState:
    """Final-gate state (split 2.3, 2026-11-05): fields the verify-gate tail
    reads/writes; `critic_issues` is the shared list (appends in place)."""
    _duo_hard_stop: bool = False
    final_verdict: str = ""
    _duo_timed_out: bool = False
    _written_files: list = dataclasses.field(default_factory=list)
    _verify_mutation_serial: int = 0
    _verify_last_ok_serial: int = 0
    _verify_warned: bool = False
    _ws_str: str = ""
    _explore_ctx: str = ""
    _di: int = 0
    _n_items: int = 1
    _loop_items: list = dataclasses.field(default_factory=list)
    _done_tasks: list = dataclasses.field(default_factory=list)
    _pending_send_files: list = dataclasses.field(default_factory=list)
    critic_issues: list = dataclasses.field(default_factory=list)
