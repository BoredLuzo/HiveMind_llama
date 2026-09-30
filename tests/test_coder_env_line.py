# -*- coding: utf-8 -*-
"""RUNTIME-ENV line in the duo coder system prompt (2026-09-30).

DUO_CODER_BASE promises an OS/runtime note and a workspace root "stated in
your context" — the builder appended neither, so small models guessed a
Linux container (live: Sharp searched /root before its first write).
Pins: line present with the run workspace, deterministic across rebuilds,
and never duplicated when a custom prompt already carries it.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.duo_helpers import _build_duo_coder_sys  # noqa: E402


def _fake_ctx(workspace="C:\\Users\\x\\MyProject", override=""):
    return SimpleNamespace(
        workspace=workspace,
        active_preset=None,
        use_learned=False,
        websearch_available=False,
        settings={"duo_websearch_enabled": False},
        duo_config=SimpleNamespace(test_feedback_chunk=False,
                                   test_feedback_final=False,
                                   until_finished=False),
        get_effective_prompt_with_override=lambda *a, **k: override,
    )


class RuntimeEnvLine(unittest.TestCase):
    def test_line_present_with_normalized_workspace(self):
        p = _build_duo_coder_sys(_fake_ctx(), has_plan=True,
                                 has_subtasks=False, has_explore_ctx=False)
        self.assertIn("RUNTIME: This machine is Windows", p)
        self.assertIn("Workspace root: C:/Users/x/MyProject", p)
        self.assertIn("relative paths", p)
        self.assertNotIn(chr(92) + "Users", p.split("Workspace root:")[1].split("—")[0])

    def test_rebuild_is_identical(self):
        a = _build_duo_coder_sys(_fake_ctx(), has_plan=True,
                                 has_subtasks=False, has_explore_ctx=False)
        b = _build_duo_coder_sys(_fake_ctx(), has_plan=True,
                                 has_subtasks=False, has_explore_ctx=False)
        self.assertEqual(a, b)

    def test_no_duplicate_when_custom_prompt_carries_it(self):
        marker = ("RUNTIME: This machine is Windows — there is NO Linux "
                  "container; never look in /workspace or /root. use "
                  "relative paths in tool calls.")
        p = _build_duo_coder_sys(_fake_ctx(override=marker), has_plan=True,
                                 has_subtasks=False, has_explore_ctx=False)
        self.assertEqual(p.count("RUNTIME: This machine is Windows"), 1)

    def test_works_without_workspace(self):
        p = _build_duo_coder_sys(_fake_ctx(workspace=""), has_plan=False,
                                 has_subtasks=False, has_explore_ctx=False)
        self.assertIn("RUNTIME: This machine is Windows", p)
        self.assertNotIn("Workspace root:", p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
