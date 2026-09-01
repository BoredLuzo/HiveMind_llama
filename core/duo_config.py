"""DuoConfig dataclass — bundles 18 parameters into one object."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class DuoConfig:
    """All duo/agentic parameters bundled.

    Replaces 20 separate keyword arguments in run_stream().
    """
    tool_rounds: int = 0
    use_pipeline: bool = False
    coding_mode: bool = True
    chunking: bool = False
    planner: bool = False
    pre_explore: bool = False
    agentic_mode: bool = False
    agentic_thinking: bool = False
    thinking_per_chunk: bool = False
    test_feedback_chunk: bool = False
    test_feedback_final: bool = False
    until_finished: bool = False
    runtime_profile: str | None = None
    runtime_profile_lock_override: bool = False
    important_task: bool = False
    coder_tool_thinking: bool = False
    coder_tool_thinking_explicit: bool = False
    coder_tool_thinking_auto_mode: str = "off"
    websearch_enabled: bool = False
    git_autocommit: bool = False
    evict_on_pause: bool = False
    pause_timeout_s: int = 600
    pass_explore_files: str = "touched"   # "all" | "touched" | "none"
    parallel_preexplore: bool = False

    @classmethod
    def from_legacy_params(cls, *,
                           duo_tool_rounds=0,
                           duo_use_pipeline=False,
                           duo_coding_mode=True,
                           duo_chunking=False,
                           duo_planner=False,
                           duo_pre_explore=False,
                           duo_agentic_mode=False,
                           duo_agentic_thinking=False,
                           duo_thinking_per_chunk=False,
                           duo_test_feedback_chunk=False,
                           duo_test_feedback_final=False,
                           until_finished=False,
                           duo_runtime_profile=None,
                           duo_runtime_profile_lock_override=False,
                           important_task=False,
                           duo_coder_tool_thinking=False,
                           duo_coder_tool_thinking_explicit=False,
                           duo_coder_tool_thinking_auto_mode="off",
                           duo_websearch_enabled=False,
                           duo_git_autocommit=False,
                           duo_evict_on_pause=False,
                           duo_pause_timeout_s=600,
                             duo_pass_explore_files="touched",
                           duo_parallel_preexplore=False,
                           ):
        return cls(
            tool_rounds=duo_tool_rounds,
            use_pipeline=duo_use_pipeline,
            coding_mode=duo_coding_mode,
            chunking=duo_chunking,
            planner=duo_planner,
            pre_explore=duo_pre_explore,
            agentic_mode=duo_agentic_mode,
            agentic_thinking=duo_agentic_thinking,
            thinking_per_chunk=duo_thinking_per_chunk,
            test_feedback_chunk=duo_test_feedback_chunk,
            test_feedback_final=duo_test_feedback_final,
            until_finished=until_finished,
            runtime_profile=duo_runtime_profile,
            runtime_profile_lock_override=duo_runtime_profile_lock_override,
            important_task=important_task,
            coder_tool_thinking=duo_coder_tool_thinking,
            coder_tool_thinking_explicit=duo_coder_tool_thinking_explicit,
            coder_tool_thinking_auto_mode=duo_coder_tool_thinking_auto_mode,
            websearch_enabled=duo_websearch_enabled,
            git_autocommit=duo_git_autocommit,
            evict_on_pause=duo_evict_on_pause,
            pause_timeout_s=duo_pause_timeout_s,
            pass_explore_files=duo_pass_explore_files,
            parallel_preexplore=duo_parallel_preexplore,
        )
