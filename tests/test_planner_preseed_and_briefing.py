"""Regression tests for planner preseed, edge-case briefing prompt, and plan dedup.

Covers the 2026-11-21 thinking-planner -> no-think-coder improvements:
1. `append_planner_preseed` inlines tiny/small workspace files for the planner
   when LLM pre-explore did not supply contracts, with a planner-appropriate
   header (no coder tool-call instructions), while respecting caps and skipping
   when pre-explore contracts already exist.
2. `make_planner_analysis_sys` covers edge cases/invariants and concrete
   verification checks inside `### Open Tasks` (so `_distilled_execution_steps`
   preserves them for the coder) while keeping `_STRUCT_OUTPUT` intact.
3. `_inject_plan_into_coder_msgs` skips duplicate injection when
   `[YOUR EXECUTION PLAN` is already embedded in `dtool_msgs` (non-chunked
   thinking-planner runs), while still injecting once when absent.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.runner import (  # noqa: E402
    CONDITIONS as BENCH_CONDITIONS,
    DEFAULT_CONDITIONS as BENCH_DEFAULT_CONDITIONS,
    Task as BenchTask,
    _hivemind_body,
)
from core.duo_helpers import (  # noqa: E402
    _build_duo_coder_sys,
    append_planner_preseed,
    build_preseed_context,
)
from core.duo.preflight import _inject_plan_into_coder_msgs  # noqa: E402
from hive_functions.planner import (  # noqa: E402
    apply_scaffold_guard,
    make_planner_analysis_sys,
)
from hive_functions.prompts import PROMPTS  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [PASS] {name}")
    else:
        msg = f"{name}" + (f" — {detail}" if detail else "")
        print(f"  [FAIL] {msg}")
        FAILS.append(msg)


def test_planner_preseed() -> None:
    print("-- 1. Planner preseed helper & duo_runner wiring --")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        ws = Path(td)
        (ws / "tree_building.py").write_text(
            "def BuildTree(records):\n    raise ValueError('error!')\n",
            encoding="utf-8",
        )
        # Default coder preseed unchanged
        coder_blk, coder_names = build_preseed_context(str(ws), 32768)
        check("default coder preseed keeps read_file header",
              "do not call read_file" in coder_blk and coder_names == ["tree_building.py"])

        # Planner preseed inlines existing starter file with planner header
        merged, names = append_planner_preseed(
            "STATIC MAP: tree_building.py", str(ws), 32768,
            query="refactor tree_building", has_explore_contracts=False,
        )
        check("planner preseed inlines file content",
              "raise ValueError('error!')" in merged and names == ["tree_building.py"])
        check("planner preseed preserves existing planner_ctx",
              merged.startswith("STATIC MAP: tree_building.py"))
        check("planner preseed uses non-tool header",
              "[WORKSPACE FILES" in merged and "do not call read_file" not in merged)

        # Skips when pre-explore contracts already exist
        unchanged, skip_names = append_planner_preseed(
            "CONTRACT OVERVIEW", str(ws), 32768,
            query="refactor", has_explore_contracts=True,
        )
        check("planner preseed skipped when pre-explore contracts exist",
              unchanged == "CONTRACT OVERVIEW" and skip_names == [])

    # Verify duo_runner.py wires append_planner_preseed into _duo_planner_phase
    duo_src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
    check("duo_runner.py calls append_planner_preseed in planner phase",
          "append_planner_preseed" in duo_src and "[PLANNER-PRESEED]" in duo_src)


def test_planner_analysis_prompt() -> None:
    print("-- 2. make_planner_analysis_sys covers edge cases & verification --")
    for label, mdl in (("4b", "qwen3.5:4b-mtp"), ("35b", "qwen3.6:35b-a3b-mtp")):
        sys_p = make_planner_analysis_sys(model_name=mdl)
        low = sys_p.lower()
        check(f"{label} prompt asks for edge cases", "edge case" in low)
        check(f"{label} prompt asks for verification", "verification" in low)
        check(f"{label} prompt directs details into ### Open Tasks with target file",
              "### open tasks" in low and "numbered items" in low and "target file" in low)
        check(f"{label} prompt preserves _STRUCT_OUTPUT sections",
              "## Plan:" in sys_p and "### Completed Tasks" in sys_p and "### Summary" in sys_p)
        check(f"{label} prompt Completed Tasks placeholder is not future-tense",
              "What gets done in this run" not in sys_p
              and "Already completed work" in sys_p)

    # Verify apply_scaffold_guard retains edge-case and verification steps when target file is named
    steps = [
        "robot_name.py: implement Robot class with class-level _used_names set",
        "robot_name.py: edge case — reset() must never repeat old name even after random.seed reset",
        "robot_name.py: verify via run_python assertions for format, uniqueness, and reset after reseed",
    ]
    kept, meta = apply_scaffold_guard(steps, "Implement the solution in robot_name.py")
    check("apply_scaffold_guard keeps all 3 steps when target file is referenced",
          len(kept) == 3 and not meta.get("meta_dropped"), f"kept={len(kept)} meta={meta}")


def test_no_prompt_paradoxes() -> None:
    print("-- 4. No contradictions in assembled coder system/user prompts --")

    class _FakeCtx:
        settings = {"duo_websearch_enabled": False}
        workspace = "/tmp/ws"
        websearch_available = False
        active_preset = None
        use_learned = False

        class duo_config:
            test_feedback_chunk = False
            test_feedback_final = False
            until_finished = True

        def get_effective_prompt_with_override(self, *_a, **_k):
            return ""

    coder_sys = _build_duo_coder_sys(
        _FakeCtx(), has_plan=True, has_subtasks=False, has_explore_ctx=False,
    )
    check("no absolute-vs-relative path contradiction",
          "must be absolute" not in coder_sys and "relative paths" in coder_sys)
    check("no one-file-per-tool-call vs multi-file patch contradiction",
          "One file per tool call. No bundled edits." not in coder_sys
          and "One file per edit_file/write_file call" in coder_sys)
    check("lean-safe prompts omit core-only tool names (run_python/apply_patch)",
          "run_python" not in PROMPTS["duo_coder_execution"]
          and "apply_patch" not in PROMPTS["duo_coder_base"])
    check("no 'Keep verification light / was never needed' vs edge-case check tension",
          "Keep verification light" not in coder_sys
          and "was never needed" not in coder_sys
          and "edge-case check" in coder_sys)
    check("DUO_CODER_UNEXPLORED acknowledges pre-seeded files",
          "pre-seeded" in PROMPTS["duo_coder_unexplored"].lower())
    check("DUO_CODER_BASE edit discipline acknowledges pre-seeded files",
          "pre-seeded" in PROMPTS["duo_coder_base"].lower())
    ri_src = (ROOT / "core" / "duo_round_input.py").read_text(encoding="utf-8")
    check("_explore_rule acknowledges pre-seeded files and allows write_file rewrites",
          "already pre-seeded" in ri_src and "ONLY for NEW files" not in ri_src)


def test_plan_injection_dedup() -> None:
    print("-- 3. _inject_plan_into_coder_msgs skips duplicate execution plan --")
    plan = SimpleNamespace(
        subtasks=["Implement Robot.reset with used-name tracking", "Verify reset under fixed random.seed"],
        plan_content="## Plan: Robot Name\n\n### Open Tasks\n1. Implement Robot.reset\n",
    )

    # Case A: plan already embedded by duo_round_input.py (thinking planner + chunking=False)
    msgs_with_plan = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": (
                "Implement robot_name.py\n\n"
                "[YOUR EXECUTION PLAN — IMPLEMENT THIS, DO NOT RE-PLAN]:\n"
                "1. Implement Robot.reset with used-name tracking"
            ),
        },
    ]
    injected_dup = _inject_plan_into_coder_msgs(
        msgs_with_plan, plan, chunking=False, is_first_outer_round=True,
    )
    check("skips duplicate injection when [YOUR EXECUTION PLAN already present",
          injected_dup is False and len(msgs_with_plan) == 2)

    # Case B: plan NOT yet present (non-thinking planner + chunking=False)
    msgs_clean = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Implement robot_name.py"},
    ]
    injected_once = _inject_plan_into_coder_msgs(
        msgs_clean, plan, chunking=False, is_first_outer_round=True,
    )
    check("still injects once when plan not yet present",
          injected_once is True and len(msgs_clean) == 3
          and "[YOUR EXECUTION PLAN" in msgs_clean[-1]["content"])

    # Case C: calling a second time on the same list is idempotent
    injected_twice = _inject_plan_into_coder_msgs(
        msgs_clean, plan, chunking=False, is_first_outer_round=True,
    )
    check("repeated call on same messages is idempotent",
          injected_twice is False and len(msgs_clean) == 3)


def test_hivemind_duo_plan_condition() -> None:
    print("-- 5. bench/runner.py hivemind_duo_plan A/B condition --")
    check("hivemind_duo_plan registered in CONDITIONS (opt-in, not in DEFAULT_CONDITIONS)",
          "hivemind_duo_plan" in BENCH_CONDITIONS
          and "hivemind_duo_plan" not in BENCH_DEFAULT_CONDITIONS)
    t = BenchTask("t1", "fix bug", "pytest")
    b_solo = _hivemind_body(t, Path("."), duo=True, model="qwen3.6:35b-a3b-mtp",
                            duo_planner=False, duo_agentic_thinking=False, ctx_tokens=122880)
    b_plan = _hivemind_body(t, Path("."), duo=True, model="qwen3.6:35b-a3b-mtp",
                            duo_planner=True, duo_agentic_thinking=True, ctx_tokens=122880)
    check("solo duo keeps planner=False & thinking=False",
          b_solo["duo_planner"] is False and b_solo["duo_agentic_thinking"] is False
          and b_solo["duo_coder_tool_thinking"] is False)
    check("plan duo enables thinking planner + keeps no-think coder + headless=True",
          b_plan["duo_planner"] is True and b_plan["duo_agentic_thinking"] is True
          and b_plan["duo_coder_tool_thinking"] is False
          and b_plan["duo_chunking"] is False and b_plan["headless"] is True)
    runner_src = (ROOT / "bench" / "runner.py").read_text(encoding="utf-8")
    check("_one_run dispatches hivemind_duo_plan with duo_planner_override=True and duo_agentic_thinking=True",
          'elif cond == "hivemind_duo_plan":' in runner_src
          and "duo_planner_override=True, duo_agentic_thinking=True" in runner_src)


def main() -> int:
    test_planner_preseed()
    test_planner_analysis_prompt()
    test_plan_injection_dedup()
    test_no_prompt_paradoxes()
    test_hivemind_duo_plan_condition()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)}):")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
