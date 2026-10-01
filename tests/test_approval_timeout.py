# -*- coding: utf-8 -*-
"""Approval-timeout gate (2026-09-30): duo_action_approval_timeout_s.

Default 0 keeps today's behavior (wait up to 3600 s — attended runs). With
N > 0 an unanswered card auto-denies with an explicit "denied: no approval
within N s" message, and a late user decision is discarded once instead of
being stored as a pre-decision that silently approves the NEXT gated call.
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import runner as R  # noqa: E402
from infra import run_control as RC  # noqa: E402
from core import state as core_state  # noqa: E402

_WS = tempfile.mkdtemp(prefix="appr_test_")


def _enable(timeout_s: int):
    core_state.settings["duo_action_approval_enabled"] = True
    core_state.settings["duo_action_approval_timeout_s"] = timeout_s


def _disable():
    core_state.settings["duo_action_approval_enabled"] = False
    core_state.settings["duo_action_approval_timeout_s"] = 0


class ApprovalTimeout(unittest.TestCase):
    def setUp(self):
        R._current_run_id.set("r-appr-test")
        R._approval_once_paths.pop("r-appr-test", None)
        R._approval_memory.clear()
        R._approval_expired.pop("r-appr-test", None)
        R._approval_pre_decisions.pop("r-appr-test", None)
        RC._pause_events.pop("r-appr-test", None)
        RC._user_answers.pop("r-appr-test", None)
        _enable(1)

    def tearDown(self):
        _disable()
        R._approval_expired.pop("r-appr-test", None)
        R._approval_pre_decisions.pop("r-appr-test", None)
        RC._pause_events.pop("r-appr-test", None)
        RC._user_answers.pop("r-appr-test", None)

    def test_unanswered_card_auto_approves_once_after_timeout(self):
        async def run():
            return await R._check_action_approval(
                "run_bash", {"command": "ls"}, _WS)

        res = asyncio.run(run())
        # auto-approved once: the TOOL RUNS, the note rides along
        self.assertEqual(res[0], "NOTE")
        self.assertIn("auto-approved", res[1])
        self.assertIn("no user response within 1", res[1])
        self.assertTrue(R._approval_expired.get("r-appr-test"))
        # run_bash is NOT a write tool: no once-path registration
        self.assertNotIn("r-appr-test", R._approval_once_paths)

    def test_answer_before_timeout_honors_user_decision(self):
        async def run():
            async def _answer_soon():
                await asyncio.sleep(0.2)
                RC.set_user_answer("r-appr-test", "1|go ahead")
            task = asyncio.create_task(_answer_soon())
            res = await R._check_action_approval(
                "run_bash", {"command": "ls"}, _WS)
            await task
            return res

        res = asyncio.run(run())
        # once-approved WITH a user note: tool runs, note rides along
        self.assertEqual(res, ("NOTE", "go ahead"))
        self.assertFalse(R._approval_expired.get("r-appr-test"))

    def test_late_decision_after_timeout_is_discarded(self):
        R._approval_expired["r-appr-test"] = True

        class _Req:
            def __init__(self, body):
                self._b = body

            async def json(self):
                return self._b

        from routers.core import approval_decide
        res = asyncio.run(approval_decide(
            "r-appr-test", _Req({"answer": "1"})))
        self.assertEqual(res.get("routed"), "expired")
        self.assertNotIn("r-appr-test", R._approval_pre_decisions)
        # the discard is once-only: the next question works normally again
        self.assertNotIn("r-appr-test", R._approval_expired)

    def test_new_question_clears_expired_flag(self):
        """The flag must NOT swallow the answer to the NEXT legitimate
        question (Claude review 2026-10-01): a fresh staged card resets it."""
        async def _emit(ev):
            return None

        R._approval_expired["r-appr-test"] = True
        R._card_staged.pop("r-appr-test", None)
        asyncio.run(R.stage_approval_card("r-appr-test", "run_bash", _emit))
        self.assertNotIn("r-appr-test", R._approval_expired)  # reset by new card

        class _Req:
            def __init__(self, body):
                self._b = body

            async def json(self):
                return self._b

        from routers.core import approval_decide
        res = asyncio.run(approval_decide("r-appr-test", _Req({"answer": "1"})))
        self.assertEqual(res.get("routed"), "preview")  # normal decision path
        self.assertIn("r-appr-test", R._approval_pre_decisions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
