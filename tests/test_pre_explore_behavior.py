# -*- coding: utf-8 -*-
"""Behavior checks: core/duo/_pre_explore.py pure helpers (B8, 2026-11-09).

The module's phases talk to LLMs and the workspace; its pure seams do not.
tests/test_prefill_log_tail.py already covers _walk_code_partitions, and
tests/test_pre_explore_gather.py / _globals.py cover the hive_functions
runner. This suite drives the remaining pure helpers:

  * _validate_explore_plan: error tokens, action-token floor, partitions
  * _partitions_from_window: top-dir grouping, size-desc order, chunking
  * _get_analysis_window: memo hit, TTL-window cap eviction
  * _window_params / _rebuild_tree_ctx_from_window
"""
import asyncio
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.duo._pre_explore as PE  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

# ── _validate_explore_plan ─────────────────────────────────────────────────
ok_empty = PE._validate_explore_plan("", ["core:sz1"])
ok_error = PE._validate_explore_plan("[web_search failed] no plan", ["core"])
ok_short = PE._validate_explore_plan("Just describing the situation calmly.",
                                     ["core"], min_steps=2)
check("plan validation rejects empty, error-token and action-less plans",
      ok_empty == (False, "explore_ctx is empty")
      and ok_error[0] is False and "error token" in ok_error[1]
      and ok_short[0] is False and "action tokens" in ok_short[1],
      f" ({ok_empty}/{ok_error}/{ok_short})")

plan = ("Plan:\n1. read core:sz1 files\n2. edit core:sz1/a.py\n"
        "3. write tests:sz1/test_a.py\n4. run the suite")
ok_full = PE._validate_explore_plan(plan, ["core:sz1", "tests:sz1"])
ok_partial = PE._validate_explore_plan(plan, ["core:sz1", "never:sz1"])
ok_missing = PE._validate_explore_plan(plan, ["ghost:sz1", "phantom:sz1"])
check("plan validation: partition refs ok when touched, rejected if all missing",
      ok_full == (True, "ok")
      and ok_partial[0] is True          # one unmentioned label is tolerated
      and ok_missing[0] is False and "no touched partitions" in ok_missing[1],
      f" ({ok_full}/{ok_partial}/{ok_missing})")

# ── _partitions_from_window ────────────────────────────────────────────────
parts = PE._partitions_from_window(
    ["core/a.py", "core/b.py", "core/c.py", "docs/readme.md", "app.py"], 2)
got = [(p["label"], p["paths"]) for p in parts]
check("partitions: grouped by top dir, biggest first, chunked with sz labels",
      got == [("core:sz1", ["core/a.py", "core/b.py"]),
              ("core:sz2", ["core/c.py"]),
              ("__root__", ["app.py"]),
              ("docs", ["docs/readme.md"])],
      f" ({got})")

# ── _get_analysis_window (memo) ────────────────────────────────────────────
_calls = []


async def _fake_window(ws, max_files, max_depth):
    _calls.append((ws, max_files, max_depth))
    return [f"{ws}/f{len(_calls)}.py"]


async def _memo_scenario():
    with mock.patch.object(PE, "_WINDOW_MEMO", {}), \
            mock.patch.object(PE, "select_analysis_window_async", _fake_window):
        a1 = await PE._get_analysis_window("ws", 10, 2)
        a2 = await PE._get_analysis_window("ws", 10, 2)      # memo hit
        _ = await PE._get_analysis_window("ws", 20, 2)       # different key
        for i in range(9):                                   # push past the cap
            _ = await PE._get_analysis_window(f"w{i}", 10, 2)
        memo_len_mid = len(PE._WINDOW_MEMO)
        a4 = await PE._get_analysis_window("ws", 10, 2)      # evicted -> rewalk
    return a1, a2, a4, memo_len_mid


a1, a2, a4, memo_len_mid = asyncio.run(_memo_scenario())
check("analysis window memoizes per key and caps the memo at 8 entries",
      a1 == a2 and len(_calls) == 2 + 9 + 1
      and memo_len_mid <= 8
      and a4 == ["ws/f12.py"],
      f" (calls={len(_calls)}/memo={memo_len_mid}/a4={a4})")

# ── _window_params ─────────────────────────────────────────────────────────
params_default = PE._window_params(type("C", (), {"settings": {}})())
params_custom = PE._window_params(type("C", (), {"settings": {
    "duo_tree_scout_max_files": 50, "duo_tree_scout_max_depth": 7}})())
check("window params: defaults 200/4, settings overrides win",
      params_default == (200, 4) and params_custom == (50, 7),
      f" ({params_default}/{params_custom})")

# ── _rebuild_tree_ctx_from_window ──────────────────────────────────────────
def _fake_tree(ws, window, max_depth):
    body = f"📁 {ws}\n  " + "\n  ".join(sorted(window))
    return body


async def _rebuild_scenario():
    with mock.patch("hive_functions.tree_scout.build_tree_from_paths",
                    _fake_tree):
        rebuilt = await PE._rebuild_tree_ctx_from_window(
            "HEADER\n📁 ws_old\n  stale.py", "ws", ["core/a.py", "b.py"], 3)
        untouched_empty = await PE._rebuild_tree_ctx_from_window(
            "tree", "ws", [], 3)
    return rebuilt, untouched_empty


rebuilt, untouched_empty = asyncio.run(_rebuild_scenario())
check("tree rebuild swaps the window body under the marker, empty window no-op",
      rebuilt.startswith("HEADER\n📁 ws\n  b.py")
      and "stale.py" not in rebuilt and "core/a.py" in rebuilt
      and untouched_empty == "tree",
      f" ({rebuilt!r}/{untouched_empty!r})")

sys.exit(h.finish("pre_explore_behavior"))
