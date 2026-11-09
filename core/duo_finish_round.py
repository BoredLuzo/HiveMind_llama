"""Round-finish bookkeeping for run_code_duo (F13, 2026-11-08).

Small policy helpers moved VERBATIM out of run_code_duo's
``_can_finish_round`` tail; the runner keeps loop control (emit + break /
fall-through to the next round) and syncs the returned fields back into
its locals:

  - verification-blocked finish (``elif _verify_gate_blocked:``): roll
    pending workspace changes back, tell the user verification is
    required, and arm the ChunkState verification-fix override;
  - critic-rejected finish (``elif _subtask and not _approved:``): roll
    pending workspace changes back, announce the fix round, arm the
    critic-fix override and hand the (cleared) critic_issues back;
  - the no-subtask round-finish status string (``else:`` branch).

Both failure branches share one rollback chunk: it only fires when the
workspace transaction has snapshots; the AgentState.ROLLBACK transition
and the rollback happen first, then the status event carries the
restored-file count. Nothing here awaits - the events are returned for
the runner to emit in order (same convention as core/duo_round_gate.py).
"""
from __future__ import annotations

import dataclasses

from hive_functions.loop_machine import AgentState
from tools.workspace import get_transaction


@dataclasses.dataclass
class _RoundFinishResult:
    """Outcome of one round-finish failure branch.

    events: status events the runner must emit in order (nothing is
    emitted here). critic_issues: the ChunkState list AFTER
    set_critic_fix_override (which clears it); None when the branch does
    not touch critic_issues and the caller keeps its own value.
    """
    events: list = dataclasses.field(default_factory=list)
    critic_issues: list | None = None


def _rollback_discard_events(ctx, *, reason: str) -> list:
    """Discard pending workspace changes; return the rollback status event.

    Shared verbatim chunk of both failure branches. ``reason`` is the
    human-readable tail of the status line ("verification failed" /
    "critic rejected"). No-op (empty list) when the transaction has no
    snapshots - the ROLLBACK transition, the rollback and the status are
    skipped together, exactly as the old inline code did.
    """
    events: list = []
    if get_transaction().has_changes:
        ctx.exec_ctrl.transition(AgentState.ROLLBACK)
        _restored = get_transaction().rollback()
        events.append({
            "type": "status",
            "content": f"\u23ea Rollback: changes discarded ({len(_restored)} file(s)), because {reason}.",
        })
    return events


def _finish_round_verification_blocked(ctx, cs, subtask) -> _RoundFinishResult:
    """``elif _verify_gate_blocked:`` branch of the round-finish gate.

    Moved verbatim from run_code_duo: optional rollback (verification
    failed), the hard verification-required status, and the
    verification-fix override on the chunk state (only when a subtask is
    active). The runner falls through to the next round without touching
    ``_di`` - that control flow stays in run_code_duo.
    """
    res = _RoundFinishResult()
    res.events.extend(_rollback_discard_events(ctx, reason="verification failed"))
    res.events.append({
        "type": "status",
        "content": "\u26d4 Verification required: run_bash must pass after the latest file changes.",
    })
    if subtask:
        cs.set_verification_fix_override(subtask)
    return res


def _finish_round_critic_rejected(ctx, cs, subtask, di, n_items,
                                  critic_issues) -> _RoundFinishResult:
    """``elif _subtask and not _approved:`` branch of the round-finish gate.

    Moved verbatim from run_code_duo: optional rollback (critic
    rejected), the fix-round status, and the critic-fix override. The
    override clears ``cs.critic_issues``; the cleared list is handed back
    so the runner rebinds ``critic_issues`` exactly where the old code
    did (right after set_critic_fix_override).
    """
    res = _RoundFinishResult()
    res.events.extend(_rollback_discard_events(ctx, reason="critic rejected"))
    res.events.append({
        "type": "status",
        "content": f"\u21bb {di + 1}/{n_items}: {subtask[:50]} \u2014 issues found, fix round running\u2026",
    })
    cs.set_critic_fix_override(subtask, critic_issues)
    res.critic_issues = cs.critic_issues  # cleared by set_critic_fix_override
    return res


def _no_subtask_round_status(*, di: int, approved: bool) -> str:
    """Status string of the no-subtask round-finish ``else:`` branch.

    Moved verbatim: a finished round without an active subtask emits one
    status line - critic-approved wording vs last-pass wording - and the
    runner breaks the round loop. Pure string policy; emit + break stay
    in run_code_duo.
    """
    if approved:
        return f"\u2713 Critic: code approved \u2014 round {di + 1}"
    return f"\u2691 Last pass \u2014 round {di + 1} completed"
