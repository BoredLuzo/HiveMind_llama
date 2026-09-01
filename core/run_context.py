"""RunContext — shared state passed to extracted runner modules."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
import asyncio


@dataclass
class RunContext:
    """Bundle of all shared-state variables from run_stream().

    Contains both data and callable references to
    inner functions and module-level helpers.
    """
    # ── Request-level data ──
    user_input: str = ""
    images: list = field(default_factory=list)
    mode: str = "auto"
    iterations: int = 2
    active_preset: str = ""
    constraint_mode: bool = True
    force_complexity: str | None = None
    skip_agents: set = field(default_factory=frozenset)
    judge_bias: int = 50
    duo_config: Any = None  # DuoConfig instance
    chat_id: str | None = None

    # ── Run-level IDs ──
    run_id: str = ""
    t_total: float = 0.0

    # ── Complexity / routing ──
    complexity: str = "simple"
    complexity_source: str = ""
    effective_task_type: str = "general"
    use_learned: bool = False

    # ── Workspace (resolved per run) ──
    workspace: str = ""

    # ── Vision / prepro ──
    prepro_success: bool = False
    image_description: str = ""
    effective_images: list = field(default_factory=list)
    vision_cfg: dict = field(default_factory=dict)
    images_with_prepro_text: bool = False
    vision_agent_prompt: str = ""

    # ── Models cache & routing tables ──
    models_cache: set = field(default_factory=set)
    simple_direct_models: dict = field(default_factory=dict)
    complex_direct_models: dict = field(default_factory=dict)
    task_profiles: dict = field(default_factory=dict)
    similarity_cache: dict = field(default_factory=dict)
    vram_lookup_gb: dict = field(default_factory=dict)

    # ── Pipeline / settings / memory references ──
    pipeline: Any = None
    settings: dict = field(default_factory=dict)
    memory: Any = None
    registry: dict = field(default_factory=dict)
    exec_ctrl: Any = None
    pipeline_soul: str = ""
    duo_stop_reason: str = ""
    duo_rounds_done: int = 0
    duo_rounds_cap: int = 5
    duo_hard_stop: bool = False
    duo_timed_out: bool = False
    duo_tool_timeout_total: float = 300.0
    duo_tool_timeout_acc: float = 0.0
    duo_tool_timeout_start: float = 0.0
    duo_tool_fail_streak: int = 0
    chunks_total: int = 0
    chunks_done: int = 0
    chunks_total_original: int = 0
    combined_coder_out: str = ""
    planner_model: str = ""
    planner_step_cap: int = 6
    duo_start: float = 0.0
    duo_deadline_at: float = 0.0
    vram_budget: float = 7.5
    vram_cache: set = field(default_factory=set)
    websearch_available: bool = False

    # ── Context cache ──
    pipeline_mem_ctx: str = ""
    pipeline_sess_msgs: list = field(default_factory=list)

    # ── Per-run mutable state ──
    agent_elapsed: dict = field(default_factory=dict)
    prefetch_state: dict = field(default_factory=dict)
    prefetch_run_active: bool = True
    phase_timer: Any = None
    this_file: Any = None

    # ── Callable references (inner functions from run_stream) ──
    emit: Callable[[dict], Awaitable[str]] = lambda _: asyncio.sleep(0)
    done_event: Callable = lambda *a, **kw: {}
    aborted: Callable[[], bool] = lambda: False
    step_skipped: Callable[[], bool] = lambda: False
    maybe_preload: Callable = None
    maybe_trigger_soul_evolution: Callable = lambda *a: None
    auto_memory_from_input: Callable = lambda *a: None

    # ── Module-level function references ──
    pick_direct_model: Callable = lambda *a: ""
    get_num_ctx: Callable = lambda *a, **kw: None
    bk_load: Callable = None
    bk_pin: Callable = None
    bk_evict: Callable = None
    get_loaded_models_set: Callable = None
    smart_preload_if_needed: Callable = None
    safe_web_search: Callable = None
    extract_ws_query: Callable = lambda *a: ""
    make_messages: Callable = lambda *a, **kw: []
    pipeline_chat_stream: Callable = None
    refresh_judge_keepalive: Callable = None
    model_profile: Callable = lambda *a: {}
    is_truncated: Callable = lambda *a: False

    # ── Registry helpers ──
    increment_run_counter: Callable = lambda: 0
    collect_done_metrics: Callable = lambda: {}
    unregister_abort: Callable = lambda *a: None
    unregister_step_skip: Callable = lambda *a: None
    register_abort: Callable = lambda *a: None
    register_step_skip: Callable = lambda *a: None
    clear_step_skip: Callable = lambda *a: None
    schedule_prefetch: Callable = lambda *a, **kw: None
    update_prefetch_lead: Callable = lambda *a, **kw: None
    flush_prefetch_settings: Callable = lambda *a: None

    # ── Prefetch / runtime helpers ──
    prefetch_judge_once: Callable = None
    do_prefetch: Callable = None
    runtime_telemetry_snapshot: Callable = lambda: {}
    runtime_delta: Callable = lambda *a: 0
    resolve_duo_runtime_profile: Callable = lambda *a, **kw: "balanced"
    resolve_duo_run_timeout_seconds: Callable = lambda *a: 300
    # Additional module-level function references
    registry_get: Callable = lambda *a: ""
    get_effective_prompt_with_override: Callable = lambda *a, **kw: ""
    run_peer_ratings: Callable = lambda *a, **kw: None
    run_soul_cycle: Callable = lambda *a, **kw: None
    get_effective_config: Callable = lambda *a, **kw: {}
    is_aborted_global: Callable = lambda *a: False
    clear_resume_block: Callable = lambda *a: None
    run_insight_extractor: Callable = lambda *a, **kw: None
    run_skill_distillation: Callable = lambda *a, **kw: None
    resolve_resume_block: Callable = lambda *a, **kw: {}
    load_resume_block: Callable = lambda *a, **kw: {}
    resolve_duo_rounds_cap: Callable = lambda *a: 5
    is_aborted_chat: Callable = lambda *a: False
    append_learning_log: Callable = lambda *a, **kw: None
    build_soul_prompt_layer: Callable = lambda *a, **kw: ""
