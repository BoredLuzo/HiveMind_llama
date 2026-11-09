# -*- coding: utf-8 -*-
"""Behavior test: the round-finish bookkeeping extracted from run_code_duo
(F13 maintainability sprint, 2026-11-08).

core/duo_finish_round.py holds three verbatim moves from the
``_can_finish_round`` tail; the runner keeps loop control (emit + break /
fall-through) and syncs the result back:

  1. verification-blocked finish: optional rollback (only when the
     workspace transaction has snapshots), the hard verification-required
     status, and the verification-fix override (subtask only),
  2. critic-rejected finish: optional rollback ("critic rejected"), the
     fix-round status with the 50-char subtask prefix, the critic-fix
     override and the cleared critic_issues handed back,
  3. the no-subtask round status wording (approved vs last pass).

The transaction seam is the module-level ``get_transaction`` import, so a
fake transaction is patched in; ctx is a bare exec_ctrl recorder and cs a
minimal ChunkState stand-in. Two integration checks use the REAL
ChunkState (no model runs) to pin the override arming and the
critic_issues clearing exactly where the old inline code did.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.duo_finish_round as _finish_mod  # noqa: E402
from core.duo_finish_round import (  # noqa: E402
    _finish_round_verification_blocked, _finish_round_critic_rejected,
    _no_subtask_round_status,
)
from hive_functions.loop_machine import AgentState  # noqa: E402
from hive_functions.chunking import ChunkState  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


# ── fakes (transaction / ctx / cs) ───────────────────────────────────────
class _FakeExecCtrl:
    def __init__(self):
        self.transitions = []

    def transition(self, state):
        self.transitions.append(state)


class _FakeCtx:
    def __init__(self):
        self.exec_ctrl = _FakeExecCtrl()


class _FakeTx:
    """Mimics WorkspaceTransaction's has_changes / rollback seam."""

    def __init__(self, changes, restored=("a.py", "b.py")):
        self._changes = changes
        self._restored = list(restored)
        self.rolled_back = False

    @property
    def has_changes(self):
        return self._changes

    def rollback(self):
        self.rolled_back = True
        self._changes = False          # real rollback clears the snapshots
        return list(self._restored)


class _FakeCS:
    def __init__(self, critic_issues=None):
        self.critic_issues = list(critic_issues or [])
        self.verify_calls = []
        self.critic_calls = []

    def set_verification_fix_override(self, subtask):
        self.verify_calls.append(subtask)

    def set_critic_fix_override(self, subtask, issues):
        self.critic_calls.append((subtask, list(issues)))
        self.critic_issues = []        # ChunkState clears them


def _use_tx(tx):
    _finish_mod.get_transaction = lambda: tx


_VROLLBACK = "\u23ea Rollback: changes discarded (2 file(s)), because verification failed."
_CROLLBACK = "\u23ea Rollback: changes discarded (2 file(s)), because critic rejected."
_VREQUIRED = "\u26d4 Verification required: run_bash must pass after the latest file changes."


# ── 1. verification-blocked branch ───────────────────────────────────────
ctx, cs, tx = _FakeCtx(), _FakeCS(), _FakeTx(changes=True)
_use_tx(tx)
res = _finish_round_verification_blocked(ctx, cs, "wire the parser")
check("verify-blocked + changes: rolls back via ROLLBACK transition",
      ctx.exec_ctrl.transitions == [AgentState.ROLLBACK] and tx.rolled_back)
check("verify-blocked + changes: rollback status precedes the hard stop",
      len(res.events) == 2
      and res.events[0] == {"type": "status", "content": _VROLLBACK}
      and res.events[1] == {"type": "status", "content": _VREQUIRED})
check("verify-blocked + changes: arms the verification-fix override once",
      cs.verify_calls == ["wire the parser"] and not cs.critic_calls)
check("verify-blocked: result leaves the caller's critic_issues alone",
      res.critic_issues is None)

ctx, cs, tx = _FakeCtx(), _FakeCS(), _FakeTx(changes=False)
_use_tx(tx)
res = _finish_round_verification_blocked(ctx, cs, "wire the parser")
check("verify-blocked without changes: no transition, no rollback",
      ctx.exec_ctrl.transitions == [] and not tx.rolled_back)
check("verify-blocked without changes: only the verification-required status",
      len(res.events) == 1 and res.events[0]["content"] == _VREQUIRED)
check("verify-blocked without changes: override still armed",
      cs.verify_calls == ["wire the parser"])

ctx, cs, tx = _FakeCtx(), _FakeCS(), _FakeTx(changes=True)
_use_tx(tx)
res = _finish_round_verification_blocked(ctx, cs, None)
check("verify-blocked without a subtask: rollback + status, no override",
      ctx.exec_ctrl.transitions == [AgentState.ROLLBACK]
      and len(res.events) == 2 and cs.verify_calls == [])

# ── 2. critic-rejected branch ────────────────────────────────────────────
ctx, cs, tx = _FakeCtx(), _FakeCS(critic_issues=["Bug_1", "Bug_2"]), _FakeTx(changes=True)
_use_tx(tx)
res = _finish_round_critic_rejected(ctx, cs, "wire the parser", 1, 4, cs.critic_issues)
check("critic-rejected + changes: rolls back via ROLLBACK transition",
      ctx.exec_ctrl.transitions == [AgentState.ROLLBACK] and tx.rolled_back)
check("critic-rejected + changes: rollback status precedes the fix-round status",
      len(res.events) == 2
      and res.events[0] == {"type": "status", "content": _CROLLBACK}
      and res.events[1] == {"type": "status",
                            "content": "\u21bb 2/4: wire the parser \u2014 issues found, fix round running\u2026"})
check("critic-rejected: override gets the exact (subtask, issues) pair",
      cs.critic_calls == [("wire the parser", ["Bug_1", "Bug_2"])]
      and not cs.verify_calls)
check("critic-rejected: cleared critic_issues handed back to the runner",
      res.critic_issues == [] and res.critic_issues == cs.critic_issues)

ctx, cs, tx = _FakeCtx(), _FakeCS(critic_issues=["Bug_1"]), _FakeTx(changes=False)
_use_tx(tx)
res = _finish_round_critic_rejected(ctx, cs, "wire the parser", 0, 4, cs.critic_issues)
check("critic-rejected without changes: no transition, no rollback",
      ctx.exec_ctrl.transitions == [] and not tx.rolled_back)
check("critic-rejected without changes: fix-round status + override still fire",
      len(res.events) == 1
      and res.events[0]["content"] == "\u21bb 1/4: wire the parser \u2014 issues found, fix round running\u2026"
      and len(cs.critic_calls) == 1)

_long = "A" * 50 + "B" * 10
ctx, cs, tx = _FakeCtx(), _FakeCS(), _FakeTx(changes=False)
_use_tx(tx)
res = _finish_round_critic_rejected(ctx, cs, _long, 0, 1, [])
check("critic-rejected: subtask truncated to 50 chars in the status",
      res.events[0]["content"] == f"\u21bb 1/1: {_long[:50]} \u2014 issues found, fix round running\u2026"
      and "B" not in res.events[0]["content"])

# ── 3. no-subtask round status ───────────────────────────────────────────
check("no-subtask approved round reads the critic-approved wording",
      _no_subtask_round_status(di=2, approved=True)
      == "\u2713 Critic: code approved \u2014 round 3")
check("no-subtask last-pass round reads the last-pass wording",
      _no_subtask_round_status(di=2, approved=False)
      == "\u2691 Last pass \u2014 round 3 completed")

# ── 4. real ChunkState integration (override arming, no model) ──────────
ctx, tx = _FakeCtx(), _FakeTx(changes=False)
_use_tx(tx)
rcs = ChunkState()
rcs.critic_issues = ["Missing_Element_1", "Logic_Error_2"]
res = _finish_round_critic_rejected(ctx, rcs, "wire the parser", 0, 3, rcs.critic_issues)
check("real ChunkState: critic override builds the numbered fix block",
      rcs.fix_override.startswith("Fix the following issues in the code you just wrote for subtask 'wire the parser':")
      and "1. Missing_Element_1" in rcs.fix_override
      and "2. Logic_Error_2" in rcs.fix_override)
check("real ChunkState: critic_issues cleared and re-bound via the result",
      rcs.critic_issues == [] and res.critic_issues is rcs.critic_issues)

rcs2 = ChunkState()
_finish_round_verification_blocked(_FakeCtx(), rcs2, "wire the parser")
check("real ChunkState: verification override demands a passing run_bash",
      "run verification now" in rcs2.fix_override and "exit code 0" in rcs2.fix_override)

sys.exit(h.finish("test_duo_finish_round"))
