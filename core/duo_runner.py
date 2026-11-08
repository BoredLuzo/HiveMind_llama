from __future__ import annotations
from collections.abc import AsyncIterator
import asyncio
from typing import TYPE_CHECKING
from utils.bg_tasks import spawn  # noqa: E402
import traceback, json, logging, re, time, uuid
from datetime import datetime
from pathlib import Path
import dataclasses
import httpx
from functools import partial

logger = logging.getLogger("hivemind.duo")

# ── VRAM / Backend ──

# ── Learning ──
from learning.peer_ratings import run_peer_ratings

# ── Context ──
from context.resume import _check_abort_and_maybe_save_resume, _write_resume_block, _load_resume_block  # noqa: F401 -- re-export: core.duo_finalize does `from core.duo_runner import _load_resume_block`
from infra.run_control import (
    is_graceful_stop_requested, clear_graceful_stop,
    is_pause_requested, clear_pause_request,
    get_resume_signal, get_abort_during_pause_signal,
    drain_steer_messages,
    cleanup_pause_state as _cleanup_pause_state,
    _get_abort_event,
)
from context.pause_state import (
    persist_pause_state as _persist_pause_state,
    clear_pause_state as _clear_pause_state,
)
from infra.ask_user_governor import cleanup_governor as _cleanup_governor
from context.compression import _evict_stale_tool_outputs
from context.ctx_guard import (
    decide_context_action as _decide_context_action,
    resolve_compress_threshold as _resolve_compress_threshold,
    clamp_request_max_tokens as _clamp_request_max_tokens,
    clamp_coder_ctx_to_slot as _clamp_coder_ctx_to_slot,
    min_viable_tool_budget as _min_viable_tool_budget,
)



# ── Tools ──
from tools.definitions import _get_inline_tools, _filter_tools_for_mode
from tools.runner import _run_inline_tool, _current_project_state
from tools.workspace import new_transaction, get_transaction

# ── Utils ──
from utils.tool import parse_tool_args as _parse_tool_args, run_bash_failed as _run_bash_failed
from utils.token import estimate_ctx_tokens as _estimate_ctx_tokens
from utils.token import CHARS_PER_TOKEN as _CHARS_PER_TOKEN

# ── SSE ──

# ── Explore ──
from explore.cache import (
    _pre_explore_cache_invalidate_workspace,
)

# ── Hive Functions ──
from hive_functions.prompts import (
    DUO_CRITIC_TOOLS_SYSTEM, PROMPTS,
)
from hive_functions.ctx_utils import (
    compute_content_budget, ContextBudget, explore_to_planner_ctx,
    compute_char_caps, derive_static_map_budget,
)
from hive_functions.planner import (
    run_planner, PlannerResult,
    make_thinking_planner_sys, make_planner_sys, fallback_planner_steps,
    make_planner_analysis_sys,
)
from hive_functions.chunking import ChunkState, ChunkAction
from hive_functions.loop_machine import AgentState, StopReason, ExecutionController
from hive_functions.memory import ToolContextLRU
from hive_functions.test_runner import (
    run_tests as _run_test_suite, TestResult as _TestResult,
)

from hive_functions.language_config import build_test_hint as _build_test_hint
from hive_functions.num_ctx_config import resolve_ctx

# ── Planner aliases (from server.py) ──
_make_duo_thinking_planner_sys = make_thinking_planner_sys
_make_duo_planner_sys = make_planner_sys
_fallback_planner_steps = fallback_planner_steps

# ── Inline constants / functions (extracted from server.py) ──

_CRITIC_VERIFY_TOOLS = _get_inline_tools(include_websearch=False, mode="critic_verify")

from core.model_sampling import get_sampling_profile

from core.agentic_duo_state import DuoRoundState
from core.tool_executor import execute_tool_round, ToolExecHooks, ToolRoundState

# ── WEDGE-HANDOFF (2026-09-17): fresh-agent handoff for wedging edits ─────
from core.wedge_handoff import (
    new_wedge_state as _wedge_new_state,
    note_edit_success as _wedge_note_success,
    streak_of as _wedge_streak_of,
    attempts_of as _wedge_attempts_of,
    build_handover as _wedge_build_handover,
    build_resolved_notice as _wedge_build_resolved,
    build_escalated_notice as _wedge_build_escalated,
    invalidate_read_signature as _wedge_invalidate_sig,
)
from core.fix_agent import run_fix_agent as _wedge_run_fix_agent

# ── Imports from extracted helpers ─────────────────────────────────────
from core.duo_epilogue import _duo_session_epilogue
from core.duo_finalgate import _duo_final_verify_gate
from core.duo_round_input import _duo_coder_round_input
from core.duo_dtool_setup import _duo_dtool_setup
from core.duo_inloop_planner import _duo_inloop_planner
from core.duo_compress_cycle import _duo_compress_cycle
from core.duo_coder_preloop import (
    _DuoCoderPreloopState, _duo_coder_preloop,
)
from core.duo_task_complete_gate import (
    _DuoTaskCompleteGateState, _duo_task_complete_gate,
)
from core.duo_loop_guards import (
    _DuoLoopGuardsState, _duo_loop_guards,
)
from core.duo_coder_load import (
    _DuoCoderPortCacheState, _duo_resolve_coder_port_cache,
    _DuoSlotCtxState, _duo_query_actual_slot_ctx,
    _DuoToolBudgetsState, _duo_resolve_tool_budgets,
)
from core.duo_finalize import _duo_run_finalize
from core.duo_helpers import (
    build_image_desc_block, resolve_image_plan,
    effective_image_plan, vision_note_for_coder,
    build_steer_user_message as _build_steer_user_message,
    RE_THINK_CLEANUP as _re_think_cleanup,
    _preprocess_think_blocks, _calculate_thinking_tokens, _build_duo_coder_sys,
    should_compress_after_truncated_drop as _should_compress_after_truncated_drop,
    _build_symbol_reference_hints,
    _explore_size_tolerance, _collect_new_explore_paths,
    _sum_edit_lines, _build_dropped_tool_retry,
    resolve_coder_tool_mode,
)
from core.plan_tracker import build_tracker_from_planner as _build_tracker_from_planner
from core.plan_tracker import derive_step_title as _derive_step_title
from core.plan_tracker import PlanTracker

if TYPE_CHECKING:
    from context.project_state import ProjectState

# ── Extracted pure utilities (core/duo/_utils.py) ──
from core.duo._utils import (
    _parse_critic_tune,
    _is_retryable_ollama_err, _build_soft_check,
)

# DUO prompt constants
_DUO_CODER_SYS_DEFAULT   = PROMPTS.get("duo_coder", "Write complete, runnable code for the given task.")

from core.duo._vram import _phase_vram
from core.duo._pre_explore import _phase_pre_explore
_DUO_CRITIC_CODE_DEFAULT = PROMPTS.get("duo_critic_code",
    "Review the code. Respond ONLY in compact format:\n"
    "approved=true issues=[] verdict=max_10_words_with_underscores\n"
    "or: approved=false issues=[Problem_1;Problem_2] verdict=max_10_words_with_underscores\n"
    "approved=true ONLY if code is complete and correctly implemented.")
_DUO_CRITIC_GEN_DEFAULT  = PROMPTS.get("duo_critic_general",
    "Review the answer. Respond ONLY in compact format:\n"
    "approved=true issues=[] verdict=max_10_words_with_underscores\n"
    "or: approved=false issues=[Missing_Element_1;Logic_Error_2] verdict=max_10_words_with_underscores\n"
    "approved=true ONLY if the core task is fully fulfilled.")

_DUO_HEARTBEAT_KEY = "duo_heartbeat_seconds"
_DUO_HEARTBEAT_DEFAULT = 10

# ROUND-LOOP CONSTANTS (2026-11-07 audit): compiled/set once instead of
# re-creating them on every coder round.
_RE_JSON_FENCE = re.compile(r'```(?:json)?\s*(\{.*?\})\s*```', re.DOTALL)
_EXPLORE_TOOLS = {"find_files", "search_code", "list_dir", "read_file"}
_WRITE_TOOLS = {"write_file", "edit_file", "patch_file", "write_file_append",
                "replace_lines", "undo_last", "run_bash", "run_python"}














# ═══════════════════════════════════════════════════════════════════
# DUO RUNNER
# ═══════════════════════════════════════════════════════════════════






# (The fallback ladder lives in core/duo/preflight.py; a second copy here
# was dead since the split - 2026-10-31 audit.)







# ── CODER-BRIEFING-STAGE (2026-11-04) ──────────────────────────────────────
# The coder executes the plan; it does not analyze the planner's markdown or
# reasoning as if that came from another model. Only the concrete steps are
# injected, numbered, as a user-role execution plan.
BRIEFING_PLAN_ONLY = "plan-only"
BRIEFING_OFF = "off"






















_READONLY_CODER_NOTE = (
    "[READ-ONLY REQUEST — file tools are DISABLED]\n"
    "Answer in plain text from the provided codebase context. If you would call a "
    "tool, describe the outcome in text instead.\n"
    "IMPORTANT: textual tool-call syntax will NOT be executed — never output "
    "[TOOL_CALL] … [END_TOOL_CALL], edit_file<argkey>…, <function=…> or bare tool JSON."
)



# PREFLIGHT-EXTRACT (1.4.x): the module-level helpers live in core/duo/preflight.py.
from core.duo.preflight import (
    stuck_handler,
    plan_emit_fn,
    plan_aborted_fn,
    plan_heartbeat_fn,
    evict_model_handler,
    calib_est_tokens as _calib_tokens,
    keepalive_vision as _keepalive_vision_fn,
    emit_chunk_planner_heartbeat as _planner_hb_fn,
    halt_with_resume as _halt_fn,
    unpin_duo_models as _unpin_duo_fn,
    warm_workers_after_run as _warm_workers_fn,
    loop_detect_file_snapshot as _ld_file_snapshot,
    _apply_thinking_kwargs,
    _auto_commit_chunk,
    _await_with_hb,
    _drain_thinking_rescue,
    _git_checkpoint_at_chunk_start,
    _ld_setter,
    _llama_prefill_progress,
    _park_on_disconnect,
    _read_hb_interval,
    _resolve_fallback_model,
)


async def _auto_push_after_commit(ws_str: str) -> str:
    """AUTO-PUSH after a successful hivemind commit (2026-11-03: was two
    identical inline try/import/await blocks in the run finalization - the
    final auto-commit branch and the checkpoint-consolidation branch).
    Delegates to git_tools.push_after_commit (panel toggle + credential gate
    live there); a failure only debug-logs, never raises into the run."""
    try:
        from hive_functions.git_tools import push_after_commit as _pap
        return await _pap(ws_str)
    except Exception as _push_err:
        logger.debug("[AUTO-PUSH] skipped: %s", _push_err)
        return ""


@dataclasses.dataclass
class _DuoPlannerPhaseState:
    """Planner-phase in/out locals (moved verbatim from run_code_duo).

    One field per local the moved block reads on entry or assigns for
    the rest of the run. The runner syncs locals <-> fields around the
    helper call so every later read (and the abort/cleanup paths) sees
    exactly the assignments the inline code used to make in the runner
    frame (the commit-2270557 NameError-ladder bug class).
    """
    subtasks: list = dataclasses.field(default_factory=list)
    plan_thinking: str = ""
    plan_content_txt: str = ""
    plan_result: "PlannerResult | None" = None
    planner_used_fallback: bool = False
    planner_fallback_reason: str = ""
    planner_parse_mode: str = "none"
    planner_context_trimmed: bool = False
    plan_guard_quality: dict = dataclasses.field(default_factory=dict)
    planner_model: str = ""
    img_plan: dict = dataclasses.field(default_factory=dict)
    plan_port: int | None = None
    plan_ctx_final: int | None = None
    duo_timed_out: bool = False
    plan_tracker: "PlanTracker | None" = None

async def _duo_planner_phase(
    ctx, ps: _DuoPlannerPhaseState, *,
    coder_mdl, critic_mdl, exec_mdl, _ws_str, _explore_ctx, _xtools_ws,
    _coder_ctx, _contracts_raw, _matched_skills, _planner_step_cap,
    _project_state, _duo_runtime_profile, _duo_deadline_at, _duo_run_timeout_s,
    _fup_task_ctx,
):
    # CODE-MOTION (duo-runner-split): planner phase moved VERBATIM from
    # run_code_duo's planner if-block; only the unpack/pack wrapper around
    # the body is new. No logic changes.
    _subtasks = ps.subtasks
    _plan_thinking = ps.plan_thinking
    _plan_content_txt = ps.plan_content_txt
    _plan_result = ps.plan_result
    _planner_used_fallback = ps.planner_used_fallback
    _planner_fallback_reason = ps.planner_fallback_reason
    _planner_parse_mode = ps.planner_parse_mode
    _planner_context_trimmed = ps.planner_context_trimmed
    _plan_guard_quality = ps.plan_guard_quality
    _planner_model = ps.planner_model
    _img_plan = ps.img_plan
    _plan_port = ps.plan_port
    _plan_ctx_final = ps.plan_ctx_final
    _duo_timed_out = ps.duo_timed_out
    _plan_tracker = ps.plan_tracker
    try:
        _planner_default_thinking = bool(ctx.settings.get("duo_planner_default_thinking", True))
        _planner_is_distilled = False
        _disable_think_planner    = bool(ctx.settings.get("disable_thinking_in_planner", False))
        _chunking_forces_thinking = ctx.duo_config.chunking and not _disable_think_planner
        _use_thinking_planner = bool(
            ctx.duo_config.agentic_thinking
            or (not _disable_think_planner and (
                ctx.duo_config.chunking
                or (_planner_default_thinking and ctx.duo_config.coding_mode)
            ))
        )
        _planner_use_exec_model = bool(ctx.settings.get("duo_planner_use_exec_model", True))
        _planner_model = exec_mdl if (_use_thinking_planner or _planner_use_exec_model) else coder_mdl
        if bool(ctx.settings.get("duo_planner_use_coder_ctx", True)):
            _planner_model = coder_mdl
        _planner_model_override = str(ctx.settings.get("duo_planner_model", "") or "").strip()
        if _planner_model_override:
            _planner_model = _planner_model_override
        # IMAGE PLAN (2026-10-01): pure decision over planner/coder roles
        # (raw | description | none) — computed once per run, before the
        # coder load so a needed projector reload is visible in the
        # status stream. Planner-aus runs: _planner_model is set anyway.
        _img_plan = resolve_image_plan(_planner_model, exec_mdl, ctx.settings, ctx)
        _rm_models = {"planner": _planner_model, "coder": exec_mdl}
        if critic_mdl:
            _rm_models["critic"] = critic_mdl
        yield await ctx.emit({"type": "run_meta", "models": _rm_models})
        _planner_profile = ctx.model_profile(_planner_model)
        _planner_supports_thinking = bool(_planner_profile.get("thinking", False))
        _planner_effective_thinking = bool(_use_thinking_planner)

        _planner_ctx_cap_cfg = int(ctx.settings.get("duo_planner_ctx_cap", 0) or 0)
        _planner_ctx_target = int(ctx.settings.get("duo_planner_ctx_target", 0) or 0)
        _planner_ctx_cap = 131072
        if _planner_ctx_cap_cfg > 0:
            _planner_ctx_cap = _planner_ctx_cap_cfg
        _planner_ctx_cap = max(4096, _planner_ctx_cap)

        # PREBIND (2026-11-07, E3): defined before the try so the except
        # paths and the port re-resolution never read an unbound name.
        _lsm_plan_pre = None

        def _planner_load_ctx(model: str, ctx_final: int) -> int:
            try:
                if model != coder_mdl:
                    return ctx_final
                _need = (
                    resolve_ctx(ctx.settings.get("duo_coder_ctx_agentic"), coder_mdl, "agentic")
                    if ctx.duo_config.agentic_mode
                    else resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "coder")
                )
                return max(ctx_final, int(_need)) if _need else ctx_final
            except (NameError, TypeError, ValueError):
                return ctx_final

        try:
            from backend.llama_server_manager import manager as _lsm_plan_pre

            if ctx.settings.get("duo_planner_use_coder_ctx", True):
                _plan_ctx_final_raw = _coder_ctx
            elif _planner_ctx_target > 0:
                _plan_ctx_final_raw = _planner_ctx_target
            else:
                _plan_ctx_base_raw = (
                    resolve_ctx(ctx.settings.get("duo_coder_ctx_agentic"), coder_mdl, "agentic")
                    if ctx.duo_config.agentic_mode
                    else resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "coder")
                )
                _plan_ctx_final_raw = max(
                    6144,
                    int(_plan_ctx_base_raw),
                )
            _plan_ctx_final = min(int(_plan_ctx_final_raw), int(_planner_ctx_cap))
            if _plan_ctx_final < int(_plan_ctx_final_raw):
                yield await ctx.emit({
                    "type": "status",
                    "content": (
                        f"⚙️ Planner ctx guard: {int(_plan_ctx_final_raw)}→{int(_plan_ctx_final)} "
                        f"(profile {_duo_runtime_profile})"
                    ),
                })
            _plan_mdl_short = _planner_model.split(":")[0]
            yield await ctx.emit({"type": "status",
                              "content": f"⚡ Preloading {_plan_mdl_short} for planner…"})
            _evicted_any = False
            try:
                _loaded_preplan = await _lsm_plan_pre.list_loaded()
                for _lm in _loaded_preplan:
                    _lpname = str(_lm.get("name") or _lm.get("model") or "")
                    if _lpname and _lpname.rsplit("#", 1)[0] != _planner_model.rsplit("#", 1)[0]:
                        await _lsm_plan_pre.evict(_lpname)
                        _evicted_any = True
            except Exception as _plan_evict_exc:
                logger.warning("Planner pre-eviction failed: %s", _plan_evict_exc)
            if _evicted_any:
                # DRIVER-LAG-SETTLE (2026-09-06): this used to wait in an
                # UNCONDITIONAL wait_for_vram_reclaim for needed(full ctx)+768 —
                # a structurally unreachable target while the target model
                # itself is still warm in VRAM (e.g. Hermes@4096 from the
                # warmup): guaranteed 45s stall BEFORE every load. The settle
                # now only runs after real peer evictions above and targets
                # the ACHIEVABLE reduced margin; the ctx-mismatch kill +
                # the manager's margin resolution cover the rest.
                await asyncio.sleep(2.5)
                try:
                    from backend.llama_vram_table import wait_for_vram_reclaim, vram_of_moe
                    from backend.llama_manager_utils import VRAM_PRE_FLIGHT_REDUCED_MARGIN_MIB as _RED_MIB
                    _planner_vram_rm = vram_of_moe(_planner_model, _plan_ctx_final) * 1024 + _RED_MIB
                    await _await_with_hb(
                        lambda: wait_for_vram_reclaim(int(_planner_vram_rm), timeout_sec=20),
                        timeout=30.0,
                        emit_fn=ctx.emit,
                        label="Waiting for VRAM to free up",
                        interval=5.0,
                    )
                except Exception:
                    pass
            _lsm_plan_pre._planner_critical_phase = True
            # PIN RESET AT RUN START (2026-10-02): stale pins from a
            # crashed previous run must not block eviction for this one.
            try:
                _lsm_plan_pre.unpin_all()
            except Exception:
                pass
            # fallback to the lighter coder model to keep Planner responsive.
            # P1-1 FIX: Capture port from first ensure_loaded — eliminates redundant
            # second ensure_loaded call (was wasting ~300-500ms on /health re-check).
            # PLANNER-HANDOFF-PIN (audit B5): when the planner IS the coder
            # model, request the coder's ctx for the planner slot (max of
            # both needs). The handoff reuses a slot only when its ctx
            # covers the coder need — otherwise the coder reloaded the
            # same weights at a bigger ctx (15-60 s + full KV loss, the
            # ctx-mismatch class of manager_load 432). The loader's
            # graceful cascade still protects tight-VRAM cases: a
            # degraded slot is refused by the existing handoff check,
            # exactly as before — worst case is today's behavior.
            _plan_port: int | None = None
            # AUDIT F4 (2026-10-24): the pin overrides an explicit
            # planner ctx target — say so instead of doing it silently.
            _pin_ctx = _planner_load_ctx(_planner_model, _plan_ctx_final)
            if _pin_ctx > _plan_ctx_final:
                yield await ctx.emit({"type": "status", "content": (
                    f"⚙️ Planner ctx pinned {_plan_ctx_final} → {_pin_ctx} tok "
                    "(planner is the coder model — slot reuse avoids a mid-run reload)")})
            _ensure_timeout = 450.0
            try:
                _ensure_timeout = float(ctx.settings.get("duo_planner_ttl_seconds", 0) or 0) or float(ctx.settings.get("duo_planner_ensure_load_timeout_s", 450.0) or 450.0)
                _plan_port = await _await_with_hb(
                    lambda: _lsm_plan_pre.ensure_loaded(
                        _planner_model, num_ctx=_pin_ctx, n_parallel=1,
                        vision=(_img_plan.get("planner") == "raw"),
                        # DUO-PIN (2026-10-01): vision-prepro load must not LRU-evict duo slots
                        # GRACEFUL-PLANNER (2026-09-07): the planner may
                        # degrade to a smaller ctx (its prompts are small).
                        # This load used to be STRICT when planner==coder —
                        # then tight VRAM already aborted planning although
                        # e.g. 16384 would fit. The coder later loads
                        # strictly at full ctx (level A: a degraded planner
                        # slot is never inherited, because the fast path
                        # reuses only when slot-ctx >= coder-ctx).
                        ctx_graceful=True,
                    ),
                    timeout=_ensure_timeout,
                    emit_fn=ctx.emit,
                    label=f"Loading model {_planner_model.split(':')[0]}",
                    interval=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[Planner] ensure_loaded timeout (%.1fs) for %s — falling back to coder model",
                    _ensure_timeout,
                    _planner_model,
                )
                yield await ctx.emit({
                    "type": "status",
                    "content": (
                        f"⚠ Preload takes too long ({int(_ensure_timeout)}s) — using lighter model for planner"
                    ),
                })
                _planner_model = coder_mdl
                _planner_used_fallback = True
                # IMAGE-PLAN REPLAN (2026-10-03): the plan was computed with
                # the original planner model; a text-only fallback would
                # otherwise still receive raw parts (or lose its raw flag).
                _img_plan = resolve_image_plan(_planner_model, exec_mdl, ctx.settings, ctx)
            except Exception as _pre_load_err:
                logger.warning("Planner pre-load failed: %s", _pre_load_err, exc_info=True)
                _err_short = str(_pre_load_err)[:120]
                # FALLBACK-RESOLVE (2026-09-07): the configured fallback
                # model may be missing (e.g. qwen3.5:4b-ud not installed) —
                # then fall back deterministically to an installed, loadable one.
                _planner_fb = _resolve_fallback_model(
                    preferred=str(ctx.settings.get("duo_coder_fallback_model", "") or "").strip(),
                    exclude_mdl=_planner_model,
                    num_ctx=int(_plan_ctx_final or 8192),
                )
                if _planner_fb and _planner_fb != _planner_model:
                    _planner_model = _planner_fb
                    _planner_used_fallback = True
                    _planner_fallback_reason = f"preload_exception→fallback_model: {_err_short[:50]}"
                    _plan_ctx_final = resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), _planner_fb, "planner")
                    yield await ctx.emit({
                        "type": "status",
                        "content": (
                            f"⚠ Planner model could not be loaded: {_err_short[:60]} — "
                            f"fallback to {_planner_fb.split(':')[0]}"
                        ),
                    })
                else:
                    _planner_model = coder_mdl
                    _planner_used_fallback = True
                    _planner_fallback_reason = f"preload_exception: {_err_short[:60]}"
                    _plan_ctx_final = resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "planner")
                    yield await ctx.emit({
                        "type": "status",
                        "content": (
                            f"⚠ Planner model could not be loaded: {_err_short[:80]} — "
                            f"fallback to {coder_mdl.split(':')[0]}"
                        ),
                    })

            _lsm_plan_pre._planner_critical_phase = False

        except Exception as _pre_load_err_outer:
            logger.warning("Planner pre-load outer failed: %s", _pre_load_err_outer, exc_info=True)
            _plan_ctx_final = resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "planner")  # Fallback
            if _lsm_plan_pre is not None:
                _lsm_plan_pre._planner_critical_phase = False

        # P1-1 FIX: Port already captured from the first ensure_loaded call above.
        # Removed redundant second ensure_loaded (was wasting ~300-500ms on /health re-check).
        # Only try a lightweight re-resolution if the port was not captured (shouldn't happen).
        if _plan_port is None:
            try:
                from backend.llama_server_manager import manager as _lsm_port_res
                _plan_port = await asyncio.wait_for(
                    _lsm_port_res.ensure_loaded(
                        _planner_model, num_ctx=_planner_load_ctx(_planner_model, _plan_ctx_final), n_parallel=1,
                        pin=True,
                        # GRACEFUL-PLANNER: see primary load above.
                        ctx_graceful=True,
                    ),
                    timeout=30.0,
                )
            except Exception as _port_err:
                logger.warning("[Planner] Port resolution failed: %s", _port_err)

        # VISION-TRUTH GATE (2026-10-04): the requested
        # vision flag can silently drop on load (mmproj resolution failed,
        # VRAM restart without projector). If the loaded planner slot
        # really runs without a projector, downgrade the plan instead of
        # letting the planner claim it sees the image. vision_active None
        # (/props unreachable) trusts the plan - no false fallback.
        if _img_plan.get("planner") == "raw" and _plan_port is not None:
            try:
                from backend.llama_server_manager import manager as _lsm_vgate
                _pslot = _lsm_vgate.get_slot_by_port(int(_plan_port))
                _pv = getattr(_pslot, "vision_active", None) if _pslot is not None else None
                if _pv is False:
                    _img_plan, _vg_warns = effective_image_plan(
                        _img_plan, planner_slot_vision=_pv,
                        has_description=bool(str(getattr(ctx, "image_description", "") or "").strip()))
                    for _w in _vg_warns:
                        logger.warning("[DUO][VISION-GATE] %s", _w)
                        yield await ctx.emit({"type": "status", "content": "⚠ " + _w})
                elif _pv is None:
                    # UNKNOWN (2026-10-04): a server build without
                    # /props modalities would make the gate a silent
                    # no-op - say so once instead.
                    logger.info("[DUO][VISION-GATE] planner vision state UNKNOWN "
                                "(/props reports no modalities?) - trusting the plan")
            except (ImportError, AttributeError, TypeError, ValueError) as _vg_err:
                logger.debug("[DUO][VISION-GATE] planner check skipped: %s", _vg_err)

        yield await ctx.emit({
            "type": "status",
            "content": (
                f"🧠 Planner (Thinking, {_planner_model.split(':')[0]})…"
                if _planner_effective_thinking
                else f"🧩 Planner ({_planner_model.split(':')[0]}): splitting task into subtasks..."
            ),
        })
        _planner_hb_stop = asyncio.Event()

        # Unique planner id for client-side mapping + additional metadata
        _planner_id = str(uuid.uuid4())
        # Will this run buffer model thinking (e.g. llama.cpp distilled models)?
        # BUG-2-FIX: Distilled models (qwen3.5, qwen3.5-d) run via llama.cpp --reasoning on
        _planner_will_buffer = False
        _planner_streaming_expected = True
        yield await ctx.emit({
            "type": "planner_start",
            "planner_id": _planner_id,
            "model": _planner_model,
            "thinking": _planner_effective_thinking,
            "streaming_expected": _planner_streaming_expected,
            "will_buffer": _planner_will_buffer,
        })

        # PERF-2 FIX: maxsize prevents unbounded queue growth when
        # the SSE consumer is slow (network hiccup, tab in background).
        # 30 heartbeats = 30s buffer; extras are silently dropped below.
        _planner_hb_queue: asyncio.Queue = asyncio.Queue(maxsize=30)


        _planner_hb_task = asyncio.create_task(_planner_hb_fn(
        ctx, _planner_hb_queue, _planner_hb_stop, _planner_model))
        if _use_thinking_planner and not _planner_supports_thinking:
            yield await ctx.emit({
                "type": "status",
                "content": (
                    f"\u2139 Thinking enabled for {_planner_model.split(':')[0]} (not in MODEL_PROFILES \u2014 "
                    "user override). The model may not produce thinking tokens."
                ),
            })
        ctx.phase_timer.start("soft_planner")

        # ── Context Pipeline: explore → planner ─────────────────────────────
        # CTX-PRIORITY (2026-09-20): the coder exec ctx resolves from the
        # coder's OWN setting (duo_coder_ctx_agentic). It previously read
        # duo_planner_ctx_target first, so a non-zero planner target (8192)
        # silently truncated coder prompts to 8k while the server held KV
        # for the full agentic ctx (live: 7762-token prefills vs 20k
        # internal count — empty coder rounds).
        _exec_ctx = resolve_ctx(ctx.settings.get("duo_coder_ctx_agentic"), coder_mdl, "agentic") if ctx.duo_config.agentic_mode else resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "coder")
        try:
            _over = ((ctx.settings.get("ctx_overrides") or {}).get("roles") or {}).get("duo_coder")
            logger.info(
                "[CTX-EFFECTIVE] mode=%s model=%s coder_ctx=%d agentic_setting=%s planner_target=%s ctx_overrides_duo_coder=%s",
                "agentic" if ctx.duo_config.agentic_mode else "duo",
                coder_mdl, _exec_ctx,
                ctx.settings.get("duo_coder_ctx_agentic"),
                ctx.settings.get("duo_planner_ctx_target"),
                _over,
            )
        except Exception:
            pass
        # CPU-CONTEXT-GUARD: on a CPU-only install the ctx setting
        # is a *time* setting - the first prompt alone can take many minutes
        # and every re-prefill repeats it. Say so once, with the number, so
        # the fix (smaller ctx) is obvious instead of looking like a hang.
        try:
            from core.duo_helpers import cpu_prefill_warning as _cpu_warn
            from backend.llama_config import GPU_BACKEND as _gpu_backend
            _pw = _cpu_warn(_exec_ctx, _gpu_backend)
            if _pw:
                logger.warning("[PERF-WARN] %s", _pw)
                yield await ctx.emit({"type": "status", "content": "⚠ " + _pw})
        except (ImportError, AttributeError, TypeError, ValueError):
            pass
        _runtime_profile = ctx.resolve_duo_runtime_profile(
            None, important_task=False, until_finished=ctx.duo_config.until_finished,
        )
        # MEASURED OVERHEADS (2026-10-17): the content budget used to
        # subtract the hardcoded assumptions 800 (system) / 1200 (tools) -
        # the tool schema really is ~2050 tokens at 8k (SCHEMA_FRAC of the
        # window), so the planner/explore content was over-committed by
        # ~850-2500 tokens. Both numbers now come from the real artefacts:
        # the coder prompt constant and the tool-schema JSON, capped by the
        # schema budget this run will actually allow.
        _ov_sys_tok = 0
        _ov_tool_tok = 0
        try:
            from core import budgets as _budg_ov
            from hive_functions.prompts import PROMPTS as _PROMPTS_OV
            _ov_prompt_txt = max(
                str(_PROMPTS_OV.get("duo_coder_execution") or ""),
                str(_PROMPTS_OV.get("duo_coder_autonomous") or ""), key=len)
            _ov_sys_tok = _budg_ov.tokens_for_text(_ov_prompt_txt, exec_mdl)
            _ov_schema_chars = len(json.dumps(_xtools_ws or []))
            _ov_cap = _budg_ov.schema_budget_for(int(_exec_ctx or 16384), exec_mdl)
            if _ov_cap > 0:
                _ov_schema_chars = min(_ov_schema_chars, _ov_cap)
            _ov_tool_tok = _budg_ov.tokens_for_text("x" * _ov_schema_chars, exec_mdl)
        except (ImportError, AttributeError, TypeError, ValueError):
            _ov_sys_tok = _ov_tool_tok = 0
        _content_tokens = compute_content_budget(
            _exec_ctx, profile=_runtime_profile,
            system_prompt_tokens=_ov_sys_tok or None,
            tool_def_tokens=_ov_tool_tok or None,
        )
        _ctx_budget = ContextBudget.from_content_tokens(_content_tokens).with_static_map_override(
            ctx.settings.get("duo_static_map_chars", 0)
        )

        # Planner gets structured architecture (NOT raw TOML)
        _planner_ctx = explore_to_planner_ctx(_explore_ctx, task=ctx.user_input, budget=_ctx_budget)

        _planner_task = ctx.user_input + build_image_desc_block(getattr(ctx, "image_description", None))
        if _fup_task_ctx:
            _planner_task = (
                ctx.user_input
                + "\n\n[Previous task in this chat — context for this follow-up]\n"
                + _fup_task_ctx
            )

        if _project_state is not None:
            from context.project_state import build_project_context
            _proj_ctx = build_project_context(_project_state)
            if _proj_ctx:
                _planner_ctx = _proj_ctx + "\n\n---\n\n" + (_planner_ctx or "")

        if _matched_skills:
            try:
                from hive_functions.skills import format_skill_planner
                _skills_plan = "\n".join(format_skill_planner(_sk) for _sk in _matched_skills[:5])
                _planner_ctx = (
                    "[Relevante Skills (use these patterns when planning)]\n"
                    + _skills_plan + "\n\n---\n\n" + (_planner_ctx or "")
                )
            except Exception as _skp_err:
                logger.debug("[SKILLS] Planner injection failed: %s", _skp_err)

        if ctx.duo_config.chunking:
            _planner_sys_est = _make_duo_thinking_planner_sys(_planner_step_cap, model_name=_planner_model) if _planner_effective_thinking else _make_duo_planner_sys(_planner_step_cap)
        else:
            _planner_sys_est = make_planner_analysis_sys(model_name=_planner_model)
        _planner_thinking_budget = _calculate_thinking_tokens(
            _planner_model,
            ctx.settings,
            input_tokens=(len(_planner_sys_est) + len(_planner_ctx) + len(ctx.user_input) + 500) // 3,
            available_ctx=_plan_ctx_final,
            agent_name="planner",
        )
        logger.info("[PLANNER-THINKING] model=%s thinking=%s budget=%d ctx=%d",
                     _planner_model, _planner_effective_thinking,
                     _planner_thinking_budget, _plan_ctx_final)

        # ── Event bridge: run_planner ctx.emits via callback → queue → SSE yield ───
        # run_planner() uses emit_fn/heartbeat_fn callbacks, but the server's
        # SSE ctx.pipeline uses yield await ctx.emit(). The bridge uses an asyncio.Queue
        # to connect the two: run_planner puts events into the queue via the
        # callback, and the main generator yields them from the queue.
        _plan_event_q: asyncio.Queue = asyncio.Queue(maxsize=100)




        # ── Port check ──────────────────────────────────────────────────────
        _plan_result: PlannerResult | None = None
        _plan_thinking = ""
        _plan_content_txt = ""
        if _plan_port is None:
            logger.warning("[Planner] No port available — skipping planner")
            _planner_used_fallback = True
            _planner_fallback_reason = "no_port_available"
            yield await ctx.emit({"type": "status", "content": "⚠ Planner: no model port available — chunking disabled"})
            yield await ctx.emit({"type": "planner_done", "summary": "⚠ no port"})
        else:
            # ── Run planner as background task ──────────────────────────────
            _plan_task = asyncio.create_task(run_planner(
                task=_planner_task,
                user_goal=ctx.user_input,
                sees_images=_img_plan.get("planner") == "raw",
                coder_sees_images=_img_plan.get("coder") == "raw",
                images=ctx.images if _img_plan.get("planner") == "raw" else None,
                explore_ctx=_planner_ctx,
                planner_model=_planner_model,
                planner_port=_plan_port,
                step_cap=_planner_step_cap,
                use_thinking=_planner_effective_thinking,
                chunking=ctx.duo_config.chunking,
                thinking_budget=_planner_thinking_budget,
                max_output_tokens=_plan_ctx_final,
                planner_ctx=_plan_ctx_final,
                websearch_available=ctx.settings.get("duo_websearch_enabled", False) and ctx.websearch_available,
                settings=ctx.settings,
                aborted_fn=partial(plan_aborted_fn, ctx),
                emit_fn=partial(plan_emit_fn, _plan_event_q),
                heartbeat_fn=partial(plan_heartbeat_fn, _plan_event_q, _planner_hb_queue),
            ))

            # PLANNER-SKIP (2026-09-04): Skip during the planner aborts the
            # planner and goes straight to the coder (before, the planner
            # kept running and the skip was later wrongly consumed as
            # "Round skipped" -> coder 0s, silent done).
            _planner_skip_mid = False
            try:
                # Yield events from the bridge as they arrive
                while not _plan_task.done() or not _plan_event_q.empty():
                    # Enforce the run deadline even while the planner runs;
                    # otherwise a 600s planner overshoots a 90-420s run timeout.
                    if _duo_deadline_at and time.monotonic() >= _duo_deadline_at:
                        _duo_timed_out = True
                        logger.warning(
                            "[PLANNER] Run deadline exceeded - planner aborted (run timeout %ss)",
                            _duo_run_timeout_s,
                        )
                        yield await ctx.emit({
                            "type": "status",
                            "content": (
                                f"⏱ Duo timeout reached ({int(_duo_run_timeout_s)}s) "
                                "- planner aborted, continuing with best effort."
                            ),
                        })
                        _plan_task.cancel()
                        break
                    # PLANNER-SKIP (2026-09-04): Skip pressed while the
                    # planner is running -> abort planner, go to coder.
                    if ctx.step_skipped() and not ctx.aborted():
                        _planner_skip_mid = True
                        ctx.clear_step_skip()
                        try:
                            _plan_task.cancel()
                        except Exception:
                            pass
                        yield await ctx.emit({"type": "status",
                            "content": "⏭ Planner skipped — going straight to coder"})
                        yield await ctx.emit({"type": "planner_done", "summary": "⏭ skipped mid-plan"})
                        break
                    # Yield pending events from the bridge
                    while not _plan_event_q.empty():
                        try:
                            _evt = _plan_event_q.get_nowait()
                            yield await ctx.emit(_evt)
                        except asyncio.QueueEmpty:
                            break
                    # Also drain heartbeat queue directly (for events not routed through run_planner)
                    while not _planner_hb_queue.empty():
                        try:
                            yield await ctx.emit(_planner_hb_queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                    if _plan_task.done():
                        break
                    await asyncio.sleep(0.05)

                # Drain any remaining events after task completion
                while not _plan_event_q.empty():
                    try:
                        _evt = _plan_event_q.get_nowait()
                        yield await ctx.emit(_evt)
                    except asyncio.QueueEmpty:
                        break

                # Get result from completed task
                try:
                    _plan_result = _plan_task.result()
                except asyncio.CancelledError:
                    _plan_result = None
                    _planner_used_fallback = True
                    _planner_fallback_reason = "planner_step_skipped" if _planner_skip_mid else "run_deadline"
                    _planner_parse_mode = "skipped" if _planner_skip_mid else "timeout"
                except asyncio.InvalidStateError:
                    # DEADLINE-CANCEL-RACE (2026-09-09): cancel() is only
                    # SCHEDULED — calling result() immediately after the
                    # deadline break hits a still-pending task ("Result is
                    # not set", live traceback). Same handling as a clean
                    # cancellation.
                    _plan_result = None
                    _planner_used_fallback = True
                    _planner_fallback_reason = "run_deadline"
                    _planner_parse_mode = "timeout"
            except Exception as _plan_exc:
                logger.error("[Planner] Exception: %s", _plan_exc, exc_info=True)
                _subtasks = []
                _plan_thinking = ""
                _plan_content_txt = ""
                _planner_used_fallback = True
                _planner_fallback_reason = "planner_exception"
                _planner_parse_mode = "error"
                _plan_guard_quality = {}
                ctx.phase_timer.end("soft_planner", status="error")
                _exc_short = f"{type(_plan_exc).__name__}: {str(_plan_exc)[:120]}"
                yield await ctx.emit({"type": "status", "content": f"⚠ Planner error — {_exc_short}"})
                yield await ctx.emit({"type": "planner_done", "summary": f"⚠ Error: {_exc_short}"})
            finally:
                _planner_hb_stop.set()
                try:
                    _planner_hb_task.cancel()
                except Exception as _hb_err:
                    logger.debug("[DUO] Heartbeat task cancel failed: %s", _hb_err)
                # Cancel the planner task if it's still running (e.g. after exception)
                if not _plan_task.done():
                    _plan_task.cancel()
                    try:
                        await _plan_task
                    except asyncio.CancelledError:
                        pass

            # ── Extract result from PlannerResult ────────────────────────────
            if _plan_result is not None:
                _subtasks = _plan_result.subtasks
                _plan_thinking = _plan_result.thinking
                # CODER-BRIEFING-STAGE: the pure plan text, used instead of
                # the thinking when the reasoning is over the briefing limit.
                _plan_content_txt = str(getattr(_plan_result, "plan_content", "") or "")
                # Merge fallback flags: pre-load fallback + planner-internal fallback
                _planner_used_fallback = _plan_result.used_fallback or _planner_used_fallback
                _planner_fallback_reason = _plan_result.fallback_reason or _planner_fallback_reason
                _planner_parse_mode = _plan_result.parse_mode
                _planner_context_trimmed = _plan_result.context_trimmed
                _plan_guard_quality = _plan_result.plan_guard

                try:
                    from core.run_audit import record_run_audit
                    record_run_audit(ctx.chat_id, {
                        "run_id": ctx.run_id,
                        "event": "planner",
                        "plan_content_len": len(_plan_result.plan_content or ""),
                        "thinking_len": len(_plan_result.thinking or ""),
                        "subtasks": len(_plan_result.subtasks or []),
                        "used_fallback": bool(_plan_result.used_fallback),
                        "fallback_reason": str(_plan_result.fallback_reason or ""),
                        "parse_mode": str(_plan_result.parse_mode or ""),
                        "context_trimmed": bool(_plan_result.context_trimmed),
                        "explore_ctx_len": len(_explore_ctx or ""),
                        "planner_mdl": str(_planner_model or ""),
                    })
                except Exception:
                    pass

                if _project_state is not None and _plan_result.plan_content:
                    try:
                        _pt = _plan_result.plan_content
                        # Offene Tasks
                        _ot = re.search(r"###\s*(?:Offene Tasks|Open Tasks)\s*\n((?:\d+\.\s*.*\n?)+)", _pt, re.IGNORECASE)
                        if _ot:
                            # OPEN-TASKS DISPLAY FIX (2026-10-07, owner):
                            # strip leading numbers, a leading "file: "
                            # and a trailing empty pipe segment - the raw
                            # plan lines rendered with a dangling "|"
                            _ot_lines = []
                            for _l in _ot.group(1).strip().split("\n"):
                                _t = re.sub(r"^\d+\.\s*", "", _l).strip()
                                _t = re.sub(r"\s*\|\s*$", "", _t)
                                _t = re.sub(r"^file:\s*", "", _t).strip()
                                if _t:
                                    _ot_lines.append(_t)
                            _project_state.open_tasks = _ot_lines
                        # Abgeschlossene Tasks
                        _ct = re.search(r"###\s*(?:Abgeschlossene Tasks|Completed Tasks)\s*\n((?:-\s*\[x\]\s*.*\n?)+)", _pt, re.IGNORECASE)
                        if _ct:
                            _new_completed = [
                                re.sub(r"^-\s*\[x\]\s*", "", l).strip()
                                for l in _ct.group(1).strip().split("\n") if l.strip()
                            ]
                            _project_state.completed_tasks.extend(_new_completed)
                            _seen = set()
                            _deduped = []
                            for _t in reversed(_project_state.completed_tasks):
                                if _t not in _seen:
                                    _seen.add(_t)
                                    _deduped.append(_t)
                            _project_state.completed_tasks = list(reversed(_deduped))[-50:]
                        # Plan-Summary
                        _sm = re.search(r"##\s*Plan:\s*(.+)", _pt)
                        _plan_summary_set = False
                        if _sm:
                            _project_state.plan_summary = _sm.group(1).strip()[:500]
                            _plan_summary_set = True
                        else:
                            for _line in _pt.split("\n"):
                                _ls = _line.strip()
                                if _ls and not _ls.startswith("#"):
                                    _project_state.plan_summary = _ls[:500]
                                    _plan_summary_set = True
                                    break
                        if not _plan_summary_set:
                            _project_state.plan_summary = ""
                        _project_state.plan_version += 1
                    except Exception:
                        pass

                # PATCH-1: Plan-Tracker aus Planner-Result anreichern.
                if _plan_tracker is None or _plan_tracker.total <= 1:
                    _enriched = _build_tracker_from_planner(_plan_result, _contracts_raw if _contracts_raw else None, workspace=_ws_str)
                    if _enriched is not None and _enriched.total > 0:
                        _plan_tracker = _enriched
                        logger.debug("[PlanTracker] Enriched from planner: %d steps", _plan_tracker.total)

        if not ctx.duo_config.chunking and _subtasks:
            # CHUNKING-OFF HONESTY (2026-10-26, live): say it here, where the
            # subtasks actually go away, and read the RUN flag. The body can set
            # duo_chunking=true while settings.json says false; reading the
            # setting fired this while the chunks did run.
            yield await ctx.emit({"type": "status",
                "content": f"⚙️ Chunking is off - the planner's {len(_subtasks)} "
                           f"subtask(s) are skipped; the coder runs the task in one pass"})
            logger.debug("[CHUNKING-GUARD] ctx.duo_config.chunking=False - %d subtasks discarded, planner briefing kept", len(_subtasks))
            _subtasks = []

        if not ctx.phase_timer.is_ended("soft_planner"):
            _planner_status = "ok" if _subtasks else "empty"
            if not _subtasks and _plan_result is not None:
                _planner_status += f"(reason={getattr(_plan_result, 'fallback_reason', None) or 'none'})"
            ctx.phase_timer.end("soft_planner", status=_planner_status)
        # When chunking is off, don't send chunks to the UI — prevents
        # confusing "CHUNK-PLAN" panel when only a briefing was requested.
        # PLAN-TITLES (2026-09-17): ship {"title","raw"} objects so the UI
        # checklist shows a concise readable title per chunk instead of the
        # raw pipe-format step line ("file: ... | read: ... | risk: ...").
        # The frontend already prefers c.title (app.js planner_result
        # handler) and falls back to the string. DISPLAY ONLY: _loop_items
        # below keep the RAW strings — the coder contract is unchanged.
        _emit_chunks = (
            [{"title": _derive_step_title(_s), "raw": _s} for _s in _subtasks]
            if ctx.duo_config.chunking else []
        )
        yield await ctx.emit({
            "type":          "planner_result",
            "chunks":        _emit_chunks,
            "thinking":      _plan_thinking if _plan_result is not None else "",
            "model":         _planner_model,
            "used_thinking": _planner_effective_thinking,
            "quality": {
                "used_fallback": bool(_planner_used_fallback),
                "fallback_reason": str(_planner_fallback_reason or ""),
                "parse_mode": str(_planner_parse_mode or "none"),
                "context_trimmed": bool(_planner_context_trimmed),
                "step_limit": int(_planner_step_cap),
                "plan_guard": _plan_guard_quality,
            },
        })
    finally:
        ps.subtasks = _subtasks
        ps.plan_thinking = _plan_thinking
        ps.plan_content_txt = _plan_content_txt
        ps.plan_result = _plan_result
        ps.planner_used_fallback = _planner_used_fallback
        ps.planner_fallback_reason = _planner_fallback_reason
        ps.planner_parse_mode = _planner_parse_mode
        ps.planner_context_trimmed = _planner_context_trimmed
        ps.plan_guard_quality = _plan_guard_quality
        ps.planner_model = _planner_model
        ps.img_plan = _img_plan
        ps.plan_port = _plan_port
        ps.plan_ctx_final = _plan_ctx_final
        ps.duo_timed_out = _duo_timed_out
        ps.plan_tracker = _plan_tracker


@dataclasses.dataclass
class _DuoCriticReviewState:
    """Critic-review in/out locals (moved verbatim from run_code_duo).

    Holds every local the critic block (input build, tool-loop variant,
    plain variant, verdict parse, verify-gate patch, duo_critic_done emit)
    reads from or writes back to the runner loop. Same unpack/pack
    contract as _DuoPlannerPhaseState.
    """
    parts: list = dataclasses.field(default_factory=list)
    t: float = 0.0
    verify_last_ok_serial: int = 0
    critic_issues: list = dataclasses.field(default_factory=list)
    final_verdict: str = ""
    approved: bool = False
    verify_gate_blocked: bool = False


async def _duo_critic_review(
    ctx, ps: _DuoCriticReviewState, *,
    coder_mdl, coder_out, critic_mdl, _ws_str, _di, _subtask,
    _duo_critic_sys, _duo_critic_temp, _duo_critic_tok,
    _critic_profile, _critic_thinking, _critic_tools_enabled,
    _verify_mutation_serial,
):
    # CODE-MOTION (duo-runner-split): the critic section (input build,
    # tool-capable variant, plain variant, TUNE/JSON verdict parse,
    # verify-gate patch and the duo_critic_done emit) moved VERBATIM
    # from run_code_duo. No logic changes.
    _parts = ps.parts
    _t = ps.t
    _verify_last_ok_serial = ps.verify_last_ok_serial
    critic_issues = ps.critic_issues
    final_verdict = ps.final_verdict
    _approved = ps.approved
    _verify_gate_blocked = ps.verify_gate_blocked
    try:
        _critic_task_ref = _subtask if _subtask else ctx.user_input
        _content_label = "Code" if ctx.duo_config.coding_mode else "Answer"
        _review_verb   = "Review the code." if ctx.duo_config.coding_mode else "Review the answer."
        _critic_input = (
            f"Task: {_critic_task_ref}\n\n"
            f"{_content_label} (round {_di + 1}):\n{coder_out}\n\n"
            f"{_review_verb}"
        )
        ctx.exec_ctrl.transition(AgentState.CRITIC_REVIEW)
        ctx.exec_ctrl.reset_stuck_detection()  # BUG-NO-RESET FIX

        # Use tool-capable system prompt when tools are enabled
        _effective_critic_sys = DUO_CRITIC_TOOLS_SYSTEM if _critic_tools_enabled else _duo_critic_sys

        _critic_msgs = [
            {"role": "system", "content": _effective_critic_sys},
            {"role": "user",   "content": _critic_input},
        ]

        yield await ctx.emit({"type": "duo_critic", "model": critic_mdl, "round": _di + 1})
        _parts, _t = [], time.time()

        _critic_tc_read_s = float(ctx.settings.get("duo_llm_slow_timeout_s", 300))
        _critic_tc_timeout = httpx.Timeout(connect=10.0, read=_critic_tc_read_s, write=10.0, pool=5.0)
        from backend.llama_server_manager import manager as _lsm_c
        _critic_port = await _lsm_c.ensure_loaded(critic_mdl, num_ctx=resolve_ctx(ctx.settings.get("duo_critic_ctx") or ctx.settings.get("duo_coder_ctx_normal"), critic_mdl, "critic"))

        if _critic_tools_enabled:
            # ── Critic Tool-Loop ─────────────────────────────────────
            # Critic can read files and run tests before giving verdict.
            # Max 3 rounds to keep VRAM pressure low on 8GB GPUs.
            _critic_tc_msgs  = list(_critic_msgs)
            _critic_tc_max   = 3
            _cresp = None
            _c_gen_t0 = 0.0
            try:
                async with httpx.AsyncClient(timeout=_critic_tc_timeout) as _chttpc:
                  for _ctr in range(_critic_tc_max):
                    if ctx.aborted() or (ctx.chat_id and ctx.is_aborted_chat(ctx.chat_id)):
                        break
                    # Retry transient server errors (up to 3 attempts per tool round)
                    for _critic_post_attempt in range(3):
                        try:
                            _c_gen_t0 = time.monotonic()  # GEN-TIME: critic POST duration
                            _critic_tc_payload = {
                                "model": critic_mdl,
                                "messages": _critic_tc_msgs,
                                "tools": _CRITIC_VERIFY_TOOLS,
                                "stream": False,
                                "tool_choice": "auto",
                                "temperature": _duo_critic_temp if _duo_critic_temp is not None else _critic_profile.get("temperature", _duo_critic_temp),
                                "top_p": _critic_profile.get("top_p", 0.95),
                                "top_k": _critic_profile.get("top_k", 20),
                                "presence_penalty": _critic_profile.get("presence_penalty", 1.5),
                                "repetition_penalty": _critic_profile.get("repetition_penalty", 1.0),
                                "max_tokens": _duo_critic_tok,
                            }
                            if _critic_profile.get("seed") is not None:
                                _critic_tc_payload["seed"] = int(_critic_profile["seed"])
                            _cresp = await _chttpc.post(
                                    f"http://127.0.0.1:{_critic_port}/v1/chat/completions",
                             json=_critic_tc_payload
                                    )
                            if _cresp.status_code in (500, 502, 503, 504) and _critic_post_attempt < 2:
                                await _cresp.aread()
                                await asyncio.sleep(2.0)
                                continue
                            break
                        except httpx.ConnectError:
                            if _critic_post_attempt < 2:
                                await asyncio.sleep(3.0)
                                continue
                            raise
                    if _cresp is None:
                        raise RuntimeError("Critic POST failed after retries")
                    if _cresp.status_code != 200:
                        raise RuntimeError(f"Critic HTTP {_cresp.status_code}: {_cresp.text[:200]}")
                    _cd = _cresp.json()
                    _cmsg = (_cd.get("choices", [{}])[0].get("message", {})
                             if "choices" in _cd else _cd.get("message", {}))
                    _cusage = _cd.get("usage") or {}
                    if _cusage.get("completion_tokens"):
                        # TOKEN-TRACKER (2026-08-25): cached_tokens mitliefern.
                        try:
                            _cc_cached = int((_cusage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                        except Exception:
                            _cc_cached = 0
                        yield await ctx.emit({"type": "usage_meta", "phase": "critic",
                            "completion_tokens": int(_cusage["completion_tokens"]),
                            "prompt_tokens": int(_cusage.get("prompt_tokens") or 0),
                            "cached_tokens": _cc_cached,
                            "gen_ms": int((time.monotonic() - _c_gen_t0) * 1000)})
                    _ctc = _cmsg.get("tool_calls", [])
                    if not _ctc:
                        # No more tool calls — this is the final verdict
                        _parts.append((_cmsg.get("content") or "").strip()
                                      or (_cmsg.get("reasoning_content") or "").strip())
                        break
                    # Execute tool calls, append results
                    _critic_tc_msgs.append(_cmsg)
                    for _ctc_call in _ctc:
                        _cfn   = _ctc_call.get("function", {})
                        _cname = _cfn.get("name", "")
                        _cargs = _parse_tool_args(_cfn.get("arguments", {}))
                        _cres  = await _run_inline_tool(
                            _cname,
                            _cargs,
                            tool_mode="critic_verify",
                            include_websearch=False,
                            workspace_lock=_ws_str,  # CRITIC-LOCK (2026-09-13): was None -> reads/searches unconstrained
                        )
                        if _cname == "run_bash" and not _run_bash_failed(_cres):
                            _verify_last_ok_serial = max(_verify_last_ok_serial, _verify_mutation_serial)
                        _critic_tc_msgs.append({
                            "role": "tool",
                            "content": _cres,
                            "name": _cname,
                            "tool_call_id": _ctc_call.get("id", _cname),
                        })
            except Exception as _cte:
                _parts.append(json.dumps({"approved": False, "issues": [f"Critic-tool-error: {str(_cte)[:80]}"], "verdict": "tool_error"}))
        else:
            # ── Critic Plain Request ──────────────────────────────────
            _critic_plain_payload = {
                "model": critic_mdl,
                "messages": _critic_msgs,
                "tools": [],
                "stream": False,
                "temperature": _duo_critic_temp if _duo_critic_temp is not None else _critic_profile.get("temperature", _duo_critic_temp),
                "top_p": _critic_profile.get("top_p", 0.95),
                "top_k": _critic_profile.get("top_k", 20),
                "presence_penalty": _critic_profile.get("presence_penalty", 1.5),
                "repetition_penalty": _critic_profile.get("repetition_penalty", 1.0),
                "max_tokens": _duo_critic_tok,
            }
            if _critic_profile.get("seed") is not None:
                _critic_plain_payload["seed"] = int(_critic_profile["seed"])
            # MIN-P-EXPLICIT (2026-09-20): see the coder payload — a
            # declared min_p 0.0 must ship (API default is 0.05).
            if "min_p" in _critic_profile:
                _critic_plain_payload["min_p"] = _critic_profile["min_p"]
            if _critic_profile.get("cache_prompt"):
                _critic_plain_payload["cache_prompt"] = True
            _critic_plain_payload = _apply_thinking_kwargs(
                _critic_plain_payload, _critic_profile, _critic_thinking, coder_tool_think=False
            )
            _cresp = None
            _c_gen_t0 = 0.0
            try:
                async with httpx.AsyncClient(timeout=_critic_tc_timeout) as _chttpc_plain:
                    for _critic_post_attempt in range(3):
                        try:
                            _c_gen_t0 = time.monotonic()  # GEN-TIME: Critic-POST-Dauer
                            _cresp = await _chttpc_plain.post(
                                f"http://127.0.0.1:{_critic_port}/v1/chat/completions",
                                json=_critic_plain_payload,
                            )
                            if _cresp.status_code in (500, 502, 503, 504) and _critic_post_attempt < 2:
                                await _cresp.aread()
                                await asyncio.sleep(2.0)
                                continue
                            break
                        except httpx.ConnectError:
                            if _critic_post_attempt < 2:
                                await asyncio.sleep(3.0)
                                continue
                            raise
                    if _cresp is None:
                        raise RuntimeError("Critic POST failed after retries")
                    if _cresp.status_code != 200:
                        raise RuntimeError(f"Critic HTTP {_cresp.status_code}: {_cresp.text[:200]}")
                    _cd = _cresp.json()
                    _cmsg = (_cd.get("choices", [{}])[0].get("message", {})
                             if "choices" in _cd else _cd.get("message", {}))
                    _cusage = _cd.get("usage") or {}
                    if _cusage.get("completion_tokens"):
                        # TOKEN-TRACKER (2026-08-25): cached_tokens mitliefern.
                        try:
                            _cc_cached = int((_cusage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                        except Exception:
                            _cc_cached = 0
                        yield await ctx.emit({"type": "usage_meta", "phase": "critic",
                            "completion_tokens": int(_cusage["completion_tokens"]),
                            "prompt_tokens": int(_cusage.get("prompt_tokens") or 0),
                            "cached_tokens": _cc_cached,
                            "gen_ms": int((time.monotonic() - _c_gen_t0) * 1000)})
                    _parts.append(_cmsg.get("content", "").strip())
            except Exception as _cre:
                _err_d = {"approved": False, "issues": [f"Critic error: {str(_cre)[:80]}"], "verdict": "error"}
                _parts.append(json.dumps(_err_d))

        _critic_raw = "".join(_parts).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled

        # TUNE-PARSER: 'approved=false issues=[P1;P2] verdict=OK'
        _critic_verdict: dict = {}
        if "approved=" in _critic_raw:
            _critic_verdict = _parse_critic_tune(_critic_raw)
        else:
            try:
                jm = re.search(r"\{[\s\S]*?\}", _critic_raw)
                if jm: _critic_verdict = json.loads(jm.group())
            except (json.JSONDecodeError, AttributeError):
                logger.debug("Critic JSON parse failed for: %.200s", _critic_raw)
        if not _critic_raw:
            # A8 (audit 2026-11-03, fail-closed): an empty critic is NOT an
            # approval - it means the completion is unverified. The old code
            # fabricated {"approved": False, "issues": [""]}, which triggered a
            # fix round on an empty string; the fix is an explicit issue, not a
            # silent pass.
            logger.warning("[CRITIC] empty verdict - completion stays unapproved")
            _critic_verdict = {
                "approved": False,
                "issues": ["Critic returned no verdict (empty response). "
                           "Re-run the critic or verify the result manually."],
                "verdict": "empty",
            }
        elif _critic_verdict.get("approved") is None:
            _critic_verdict = {"approved": False, "issues": [_critic_raw[:300]], "verdict": "Parsing error"}

        critic_issues  = _critic_verdict.get("issues", [])
        final_verdict  = _critic_verdict.get("verdict", "")
        _approved      = bool(_critic_verdict.get("approved", False))
        _verify_gate_blocked = False
        if _verify_mutation_serial > _verify_last_ok_serial:
            _verify_gate_blocked = True
            _verify_issue = "Verification missing: run_bash must pass after the latest file changes."
            if _verify_issue not in critic_issues:
                critic_issues = list(critic_issues) + [_verify_issue]
            final_verdict = "verification_required_after_write"
            _approved = False
            _critic_verdict["approved"] = False
            _critic_verdict["issues"] = critic_issues
            _critic_verdict["verdict"] = final_verdict

        yield await ctx.emit({
            "type":     "duo_critic_done",
            "elapsed":  round(time.time() - _t, 1),
            "verdict":  _critic_verdict,
            "approved": _approved,
        })
    finally:
        ps.parts = _parts
        ps.t = _t
        ps.verify_last_ok_serial = _verify_last_ok_serial
        ps.critic_issues = critic_issues
        ps.final_verdict = final_verdict
        ps.approved = _approved
        ps.verify_gate_blocked = _verify_gate_blocked


async def _duo_final_test(ctx, state, _cs, _n_items, *, _eff_run_ws):
    # CODE-MOTION (duo-runner-split): P3 final-test-after-all-chunks
    # moved VERBATIM from run_code_duo (function-level block, same
    # indent). Reads shared objects only (_cs mutated in place).
    _final_test_fb = bool(ctx.duo_config.test_feedback_final)
    if _final_test_fb and _cs.has_written_files and not ctx.aborted():
        yield await ctx.emit({"type": "status", "content": "\U0001f9ea Final test run after all chunks..."})
        try:
            _final_test_result = await _run_test_suite(
                workspace=_eff_run_ws(getattr(ctx, "workspace", ""), state.get("_ws_str", "")),
                timeout=120,
                chat_id=ctx.chat_id or "",
            )
        except Exception as _fte:
            _final_test_result = _TestResult(
                success=False, language="unknown", command="",
                failure_count=0, error_lines=[str(_fte)],
                raw_output="", inject_msg=f"\u26a0\ufe0f Final test runner error: {_fte}"
            )
        yield await ctx.emit({
            "type": "test_result",
            "chunk": "final",
            "total_chunks": _n_items,
            "passed": _final_test_result.is_clean(),
            "language": _final_test_result.language,
            "failures": _final_test_result.failure_count,
            "command": _final_test_result.command,
        })
        if _final_test_result.is_clean():
            yield await ctx.emit({"type": "status", "content": f"\u2705 All tests passed ({_final_test_result.language})"})
        else:
            yield await ctx.emit({"type": "status",
                "content": f"\u274c {_final_test_result.failure_count} test failures in the final run"})
            _cs.completion_notes.append(
                f"Final test: {_final_test_result.failure_count} failure(s) ({_final_test_result.language})"
            )


@dataclasses.dataclass
class _DuoExecRoundState:
    """Tool-round exec in/out locals (moved verbatim from run_code_duo).

    Holds the locals crossing the execute_tool_round seam: the deadline
    (read for the round state, pulled forward when the executor extended
    it), the result object, the run-bash snapshot guard and the two
    by-ref bridges the executor mutates in place. Same unpack/pack
    contract as _DuoPlannerPhaseState.
    """
    duo_deadline_at: float = 0.0
    exec_result: object = None
    timed_out: bool = False
    snap_before: object = None
    last_too_large_ref: list | None = None
    cached_port_ref: list | None = None


# NOTE (2026-10-30): the ~40-keyword-argument wall below is known readability
# debt, deliberately not refactored; tests/test_duo_split_integrity.py guards
# the ps.<field> state round-trip through this helper by AST.
async def _duo_exec_round_task(
    ctx, state, ps: _DuoExecRoundState, *,
    _dr_tcs, _dtool_msgs, _duo_state, _tool_exec_hooks, _tool_ctx_lru,
    _verify_mutation_serial, _verify_last_ok_serial, _last_run_bash_failure,
    _changed_since_failure, _last_learned_insight_sig, _last_too_large_path,
    _attempts_per_file, _wedge_state, _tool_error_retries, _call_sigs,
    _recent_focus_paths, _file_changes, _duo_seen_web_queries,
    _task_complete_blocked, _total_tool_errors_ref, _tc_consecutive,
    _at_nosuite_nudged, _consecutive_reads, _last_read_path,
    _read_ladder_fired, _ws_budget_streak, _ws_disabled, _tool_mode, _duo_ws,
    _ws_str, _explore_ctx, _pre_explore_msgs, _written_files, _contracts_raw,
    _coder_event_q, _cached_coder_port, exec_mdl, _exec_has_thinking,
    _tool_think_auto_mode, _run_id_global, _di,
):
    # CODE-MOTION (duo-runner-split): tool-round launch (run-bash snapshot
    # guard + cache horizon + execute_tool_round task) and the heartbeat
    # wait loop with the result/deadline sync moved VERBATIM from
    # run_code_duo's tool loop; only the unpack/pack wrapper around the
    # body is new. No logic changes.
    _duo_deadline_at = ps.duo_deadline_at
    _exec_result = ps.exec_result
    _snap_before = ps.snap_before
    _last_too_large_ref = ps.last_too_large_ref
    _cached_port_ref = ps.cached_port_ref
    _timed_out = ps.timed_out
    try:
        # ── RUN-BASH-CHANGE-DETECTION (Problem 2) ─────────────
        # _contracts_raw exports, normalized against _ws_str.

        _round_bash_only = (
            {tc.get("function", {}).get("name", "") for tc in (_dr_tcs or [])}
            == {"run_bash"}
        )
        _snap_before = (await asyncio.to_thread(
            _ld_file_snapshot, _ws_str, _explore_ctx, _pre_explore_msgs,
            _written_files, _contracts_raw)) if _round_bash_only else None
        # ── Execute all tool calls via shared executor ──
        # CACHE-HORIZON (2026-09-04): everything < cache_horizon
        # has already been sent (immutable prefix); everything
        # from here is this round and may be mutated freely.
        _round_cache_horizon = len(_dtool_msgs)
        _last_too_large_ref = [_last_too_large_path]
        _cached_port_ref = [_cached_coder_port]
        # READ-BEFORE-EDIT (2026-11-07): run-persistent maps for the freshness
        # nudge; the executor state is rebuilt every round, these dicts are not.
        _reads_seen = state.setdefault("_reads_seen", {})
        _freshness_warned = state.setdefault("_freshness_warned", {})
        _exec_task = asyncio.create_task(execute_tool_round(
            tool_calls=_dr_tcs,
            dtool_msgs=_dtool_msgs,
            round_state=_duo_state,
            hooks=_tool_exec_hooks,
            trs=ToolRoundState(
                cache_horizon=_round_cache_horizon,
                tool_ctx_lru=_tool_ctx_lru,
                duo_deadline_at=_duo_deadline_at,
                grace_extensions_ref=state.setdefault(
                    "_grace_extensions_ref", [0]),
                verify_mutation_serial=_verify_mutation_serial,
                verify_last_ok_serial=_verify_last_ok_serial,
                last_run_bash_failure=_last_run_bash_failure,
                changed_since_failure=_changed_since_failure,
                last_learned_insight_sig=_last_learned_insight_sig,
                last_too_large_path=_last_too_large_ref,
                attempts_per_file=_attempts_per_file,
                wedge_state=_wedge_state,
                tool_error_retries=_tool_error_retries,
                call_sigs=_call_sigs,
                recent_focus_paths=_recent_focus_paths,
                file_changes=_file_changes,
                run_written_files=_written_files,
                duo_seen_web_queries=_duo_seen_web_queries,
                reads_seen=_reads_seen,
                freshness_warned=_freshness_warned,
                cached_coder_port=_cached_port_ref,
                task_complete_blocked_count=_task_complete_blocked,
                total_tool_errors=_total_tool_errors_ref,
                tc_consecutive=_tc_consecutive,
                at_nosuite_nudged=_at_nosuite_nudged,
                consecutive_reads=_consecutive_reads,
                last_read_path=_last_read_path,
                read_ladder_fired=_read_ladder_fired,
                ws_budget_streak=_ws_budget_streak,
                ws_disabled=_ws_disabled,
                allow_send_file=(str(ctx.settings.get("source") or "").startswith("telegram")),
            ),
            tool_mode=_tool_mode,
            duo_ws=_duo_ws,
            workspace_lock=_ws_str,
            exec_model=exec_mdl,
            auto_test_before_complete=bool(ctx.duo_config.test_feedback_final),
            exec_has_thinking=_exec_has_thinking,
            tool_think_auto_mode=_tool_think_auto_mode,
            run_id_global=_run_id_global,
            chat_id=ctx.chat_id,
            subtask_index=_di,
        ))
        _hb_interval = _read_hb_interval(ctx.settings)
        _hb_start = time.monotonic()
        _hb_last = _hb_start
        while True:
            _hb_had = False
            while not _coder_event_q.empty():
                _hb_had = True
                try:
                    yield _coder_event_q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            if _hb_had:
                _hb_last = time.monotonic()
            if _exec_task.done():
                break
            # RUN-DEADLINE-IN-ROUND (2026-10-31, live): the round heads catch
            # the deadline, but one long generation plus a compression could
            # run minutes past it (900 s configured, 1056 s alive). Cancel the
            # executor at the deadline; cancelling the HTTP request also drops
            # the server-side generation.
            if _duo_deadline_at and time.monotonic() >= _duo_deadline_at:
                logger.warning(
                    "[RUN-DEADLINE] deadline reached inside a tool round - cancelling")
                _exec_task.cancel()
                _timed_out = True
                # ABANDON-WITHOUT-AWAIT (2026-10-31, audit): a handler blocked
                # in asyncio.to_thread delivers CancelledError only after the
                # thread returns. Awaiting it would either freeze the run or
                # swallow an outer cancellation; one loop tick delivers the
                # cancel, then the round is abandoned.
                await asyncio.sleep(0)
                yield await ctx.emit({"type": "status",
                    "content": "⏱ Run deadline reached — tool round cancelled."})
                break
            # HEARTBEAT (2026-08-21): keepalive during long tool rounds.
            # elapsed = total time since loop start.
            if time.monotonic() - _hb_last >= _hb_interval:
                _hb_total = int(time.monotonic() - _hb_start)
                _hb_last = time.monotonic()
                yield await ctx.emit({"type": "heartbeat", "elapsed": _hb_total})
            await asyncio.sleep(0.02)
        _exec_result = None if _timed_out else _exec_task.result()
        # DEADLINE-GRACE (2026-09-09): pull the extended
        # deadline back into the runner + state so the outer
        # checks and later phases see it too.
        if _exec_result is not None and _exec_result.deadline_extended_to:
            if _exec_result.deadline_extended_to > _duo_deadline_at:
                _duo_deadline_at = _exec_result.deadline_extended_to
                state["_duo_deadline_at"] = _duo_deadline_at
    finally:
        ps.duo_deadline_at = _duo_deadline_at
        ps.exec_result = _exec_result
        ps.timed_out = _timed_out
        ps.snap_before = _snap_before
        ps.last_too_large_ref = _last_too_large_ref
        ps.cached_port_ref = _cached_port_ref


@dataclasses.dataclass
class _DuoWedgeHandoffState:
    """Wedge-handoff in/out locals (moved verbatim from run_code_duo).

    loop_detected / wedge_escalated / duo_deadline_at cross the seam in
    both directions; break_out is the loop-control signal: the moved
    block's two `break`s exit run_code_duo's tool-round loop, so inside
    the helper they set break_out and return, and the runner breaks.
    """
    loop_detected: bool = False
    wedge_escalated: bool = False
    duo_deadline_at: float = 0.0
    break_out: bool = False


async def _duo_wedge_handoff(
    ctx, state, ps: _DuoWedgeHandoffState, *,
    _exec_result, exec_mdl, _ws_str, _di, _n_items, _subtask, _dtool_ctx,
    _dtool_msgs, _wedge_state, _wedge_handoffs_used, _cached_coder_port,
):
    # CODE-MOTION (duo-runner-split): the WEDGE-HANDOFF block moved VERBATIM
    # from run_code_duo's tool loop; only the unpack/pack wrapper and the
    # break->(break_out, return) translation are new. No logic changes.
    _loop_detected = ps.loop_detected
    _wedge_escalated = ps.wedge_escalated
    _duo_deadline_at = ps.duo_deadline_at
    _break_out = ps.break_out
    try:
        # ── WEDGE-HANDOFF (2026-09-17): fresh-agent handoff ──
        # The executor detected a wedging edit (same file,
        # repeated failures). Delegation, not abort: run the
        # fix agent, feed the outcome back as ONE compact
        # notice, continue the chunk with a clean slate.
        if getattr(_exec_result, "wedge_file", "") and not _loop_detected:
            _wedge_path = str(_exec_result.wedge_file)
            _wedge_max = int(ctx.settings.get("duo_wedge_handoff_max_per_run", 2) or 2)
            if _wedge_handoffs_used[0] >= _wedge_max:
                yield await ctx.emit({"type": "status",
                    "content": f"⚠️ Wedge on {Path(_wedge_path).name} — handoff budget exhausted, escalating to human."})
                _ld_setter(4650); _loop_detected = True
                _wedge_escalated = True
                _break_out = True; return
            yield await ctx.emit({"type": "wedge_handoff",
                "file": _wedge_path,
                "attempt": _wedge_handoffs_used[0] + 1})
            yield await ctx.emit({"type": "status",
                "content": f"🧩 Wedge on {Path(_wedge_path).name} — handing the edit to a fresh-context fix agent…"})
            _wedge_t0 = time.time()
            _handover = _wedge_build_handover(
                path=_wedge_path,
                intent=(f"Plan chunk {_di + 1}/{_n_items}: {str(_subtask)[:300]}" if _subtask else ""),
                attempts=_wedge_attempts_of(_wedge_state, _wedge_path),
                streak=_wedge_streak_of(_wedge_state, _wedge_path),
                threshold=int(_wedge_state.get("threshold", 4)),
            )
            # Same weights as the coder (fresh context, no model
            # switch); duo_wedge_handoff_model is an explicit
            # escape hatch only — never a silent fallback.
            _fix_model = str(ctx.settings.get("duo_wedge_handoff_model", "") or "").strip() or exec_mdl
            _fx = await _wedge_run_fix_agent(
                path=_wedge_path,
                handover=_handover,
                model=_fix_model,
                workspace_lock=_ws_str,
                port=_cached_coder_port,
                max_rounds=int(ctx.settings.get("duo_wedge_handoff_max_rounds", 10) or 10),
                timeout_s=float(ctx.settings.get("duo_wedge_handoff_timeout_s", 900) or 900),
                # CTX-MATCH (2026-10-17): the fix agent defaulted
                # to 8192 while the coder may run at another ctx;
                # ensure_loaded then killed and reloaded the warm
                # slot (manager_load.ensure_loaded) - the opposite of the
                # "NEVER evicts" promise in fix_agent's docstring.
                num_ctx=int(_dtool_ctx or 8192),
            )
            # BUDGET-EXEMPT: the handoff wait does not burn the
            # chunk deadline — push it forward by the elapsed
            # time so a successful handoff cannot time the run out.
            _duo_deadline_at += (time.time() - _wedge_t0)
            state["_duo_deadline_at"] = _duo_deadline_at
            _wedge_handoffs_used[0] += 1
            if _fx.get("status") in ("resolved", "no_fix_needed"):
                # The handoff counts as the successful write on
                # this path (or as verified-correct): reset the
                # streak so the coder gets a clean slate.
                _wedge_note_success(_wedge_state, _wedge_path)
                _wedge_invalidate_sig(_wedge_path, _ws_str)
                _dtool_msgs.append({"role": "user", "content": _wedge_build_resolved(
                    _wedge_path, str(_fx.get("summary", "")),
                    no_fix=(_fx.get("status") == "no_fix_needed"))})
                yield await ctx.emit({"type": "status",
                    "content": f"✅ Wedge handoff on {Path(_wedge_path).name}: {_fx.get('status')}"})
            else:
                _dtool_msgs.append({"role": "user", "content": _wedge_build_escalated(
                    _wedge_path, str(_fx.get("summary", "fix agent failed")))})
                yield await ctx.emit({"type": "status",
                    "content": f"⚠️ Wedge handoff failed on {Path(_wedge_path).name} — escalating to human."})
                _ld_setter(4651); _loop_detected = True
                _wedge_escalated = True
                _break_out = True; return
    finally:
        ps.loop_detected = _loop_detected
        ps.wedge_escalated = _wedge_escalated
        ps.duo_deadline_at = _duo_deadline_at
        ps.break_out = _break_out


@dataclasses.dataclass
class _DuoCtxPressureState:
    """CTX-pressure warn-once state (moved verbatim from run_code_duo)."""
    ctx_pressure_warned: bool = False


async def _duo_ctx_pressure_check(
    ctx, ps: _DuoCtxPressureState, *,
    _dtool_msgs, _dtool_ctx, _coder_real_prompt_tokens, est_tokens,
):
    # CODE-MOTION (duo-runner-split): the CTX-pressure warn-once check moved
    # VERBATIM from run_code_duo's tool loop; only the unpack/pack wrapper
    # is new. No logic changes. est_tokens arrives under its own name: the
    # runner keeps the value in its local _est_tokens and passes it as a
    # plain keyword, so the helper's parameter name stays unambiguous.
    _ctx_pressure_warned = ps.ctx_pressure_warned
    try:
        # ── CTX Pressure Check ─────────────────────────────
        if not _ctx_pressure_warned:
            # GUARD-REAL-BASIS: fresh real value (usage_meta of the
            # last round), fallback estimator — no more coarse
            # sum(len)//3 estimation.
            _guard_tokens = int(_coder_real_prompt_tokens[0] or est_tokens)
            if _guard_tokens > int(_dtool_ctx * 0.72):
                _ctx_pressure_warned = True
                _pressure_msg = {
                    "role": "user",
                    "content": (
                        f"[CTX CRITICAL — ~{_guard_tokens} tokens used, {_dtool_ctx} available]\n"
                        "Stop all read_file calls immediately. "
                        "Write ALL pending changes now using edit_file / write_file. "
                        "Then call task_complete. "
                        "Do NOT read any more files."
                    ),
                }
                _dtool_msgs.append(_pressure_msg)
                yield await ctx.emit({"type": "status",
                    "content": f"\u26a0\ufe0f CTX CRITICAL — ~{_guard_tokens}/{_dtool_ctx} tokens. Coder prompted to finish."})
    finally:
        ps.ctx_pressure_warned = _ctx_pressure_warned



@dataclasses.dataclass




@dataclasses.dataclass




@dataclasses.dataclass





@dataclasses.dataclass
class _DuoDisconnectParkState:
    """Disconnect-park state (moved verbatim from run_code_duo's abort path).

    One field per local the park handler needs. Every field keeps the
    default of the old `_p_*` pre-init because any of them can still be
    unbound when the abort lands mid-setup - run_code_duo's ladders fill
    in what exists (the commit-2270557 NameError-ladder bug class).
    """
    loop_items: list | None = None
    di: int | None = None
    done_tasks: list | None = None
    written_files: list | None = None
    explore_ctx: str = ""
    ws_str: str = ""
    n_items: int = 0


def _duo_disconnect_park(ctx, ps: _DuoDisconnectParkState):
    # CODE-MOTION (duo-runner-split): the disconnect-park call moved VERBATIM
    # from run_code_duo's except (GeneratorExit, asyncio.CancelledError)
    # branch; only the ps.X reads replace the old _p_* locals. No logic
    # changes. Sync on purpose - the abort path must not grow new suspension
    # points.
    try:
        _park_on_disconnect(
            ctx=ctx,
            loop_items=ps.loop_items,
            di=ps.di,
            done_tasks=ps.done_tasks,
            written_files=ps.written_files,
            explore_ctx=ps.explore_ctx,
            ws_str=ps.ws_str,
            n_items=ps.n_items,
        )
    except Exception as _park_err:
        logger.warning("[DISCONNECT-PARK] Handler error: %s", _park_err)


@dataclasses.dataclass
class _DuoSessionEpilogueState:
    """Epilogue state (split 2.2, 2026-11-05): fields the session epilogue
    reads/writes; `carry` for the runner-local bindings it consumes."""
    _ws_str: str = ""
    coder_out: str = ""
    _cs: object = None
    _written_files: list = dataclasses.field(default_factory=list)
    _duo_hard_stop: bool = False
    _duo_timed_out: bool = False
    _loop_detected: bool = False
    _lifetime_tool_rounds: int = 0
    _explore_only_rounds: int = 0
    _est_tokens: int = 0
    _parts: list = dataclasses.field(default_factory=list)
    carry: dict = dataclasses.field(default_factory=dict)
    session_out: str = ""

@dataclasses.dataclass
class _DuoRoundInputState:
    """Round-input state (split 2.4, 2026-11-05): fields the round-input
    build reads/writes; `carry` for the runner-local bindings it consumes.
    `skip_round` is the loop-control signal (empty chunk → runner continue)."""
    _coder_port: object = None
    _di: int = 0
    _n_items: int = 1
    _subtask: str | None = None
    _subtasks: list = dataclasses.field(default_factory=list)
    _done_tasks: list = dataclasses.field(default_factory=list)
    _cs: object = None
    _written_files: list = dataclasses.field(default_factory=list)
    critic_issues: list = dataclasses.field(default_factory=list)
    coder_out: str = ""
    _explore_ctx: str = ""
    _img_plan: dict = dataclasses.field(default_factory=dict)
    _plan_tracker: "PlanTracker | None" = None
    _coder_caps: object = None
    skip_round: bool = False
    _touched_block_persisted: str = ""
    _coder_input: str = ""
    _coder_msgs: list = dataclasses.field(default_factory=list)
    _read_only_task_request: bool = False
    _coder_skipped_images: bool = False
    _coder_imgs_off: bool = False
    _parts: list = dataclasses.field(default_factory=list)
    _t: float = 0.0
    carry: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _DuoInloopPlannerState:
    """Inloop-planner state (split 2.6, 2026-11-05): the thinking gate,
    planner execution and plan-for-coder injection; `carry` for the
    runner-local bindings the block consumes."""
    _dtool_msgs: list = dataclasses.field(default_factory=list)
    _dtool_ctx: int = 0
    _coder_input: str = ""
    _explore_ctx: str = ""
    _plan_tracker: "PlanTracker | None" = None
    _is_first_outer_round: bool = False
    _goal_summary: str = ""
    _duo_timed_out: bool = False
    _run_thinking_now: bool = False
    _inloop_plan_text: str = ""
    carry: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _DuoDtoolSetupState:
    """Dtool-setup state (split 2.5, 2026-11-05): tool-payload budget,
    pass-files bridge, read-guard and goal-pin inputs/outputs; `carry`
    for the coder-load bindings the block consumes."""
    _di: int = 0
    _coder_caps: object = None
    _coder_msgs: list = dataclasses.field(default_factory=list)
    _explore_ctx: str = ""
    _pre_explore_msgs: list = dataclasses.field(default_factory=list)
    _contracts_raw: list = dataclasses.field(default_factory=list)
    _touched_paths: set = dataclasses.field(default_factory=set)
    _tree_ctx: str = ""
    _is_first_outer_round: bool = False
    _ws_str: str = ""
    _touched_block_persisted: str = ""
    _dtool_msgs: list = dataclasses.field(default_factory=list)
    _dtool_ctx: int = 0
    _dtool_opts: dict = dataclasses.field(default_factory=dict)
    _dtool_sys_eff: str = ""
    _tool_read_timeout_s: float = 0.0
    _goal_summary: str = ""
    _goal_pin_msg: dict | None = None
    carry: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _DuoCompressCycleState:
    """Compress-cycle state (split 2.8, 2026-11-05): the tool-round
    compression inputs and the counters it rebinds; `stop` is the
    fail-streak round-loop exit signal (runner does the break)."""
    _call_sigs: list = dataclasses.field(default_factory=list)
    _coder_est_at_real: list = dataclasses.field(default_factory=list)
    _coder_real_prompt_tokens: list = dataclasses.field(default_factory=list)
    _comp_llm_cfg: str = ""
    _comp_llm_timeout_s: int = 180
    _compress_local_only: bool = False
    _compress_threshold: int = 0
    _di: int = 0
    _done_tasks: list = dataclasses.field(default_factory=list)
    _dport: object = None
    _dtc: object = None
    _dtool_ctx: int = 0
    _dtool_sys_eff: str = ""
    _explore_ctx: str = ""
    _goal_pin_msg: dict | None = None
    _guard_decision: object = None
    _guard_tokens: float = 0.0
    _last_test_status: object = None
    _lifetime_tool_rounds: int = 0
    _max_tool_rounds: int = 0
    _n_items: int = 1
    _partial_compression: bool = False
    _plan_result: "PlannerResult | None" = None
    _plan_tracker: "PlanTracker | None" = None
    _read_counts: dict = dataclasses.field(default_factory=dict)
    _seen_explore_paths: set = dataclasses.field(default_factory=set)
    _subtasks: list = dataclasses.field(default_factory=list)
    _tool_ctx_lru: object = None
    _written_files: list = dataclasses.field(default_factory=list)
    _ws_str: str = ""
    _MAX_COMPRESSIONS: int = 0
    _compress_fail_streak: int = 0
    _ctx_compressions: int = 0
    _ctx_critical_warned: bool = False
    _ctx_full_skips: int = 0
    _ctx_pressure_warned: bool = False
    _dtool_msgs: list = dataclasses.field(default_factory=list)
    _est_tokens: int = 0
    _explore_only_rounds: int = 0
    _force_compress_next: bool = False
    _llm_compress_fails: int = 0
    _loop_detected: bool = False
    _no_write_nudged: bool = False
    _no_write_rounds: int = 0
    _partial_escalated: bool = False
    _rule_compress_fails: int = 0
    _rule_compress_used: bool = False
    _total_tool_rounds: int = 0
    stop: bool = False
    carry: dict = dataclasses.field(default_factory=dict)


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


@dataclasses.dataclass
class _DuoRunFinalizeState:
    """Finalize-tail state (split 2.1, 2026-11-05): one field per
    run_code_duo local the finalize tail reads or writes, plus `carry`
    for the runner-local bindings the tail also consumes (exec_mdl,
    final_verdict, critic_issues, _recent_focus_paths, _worker_slots,
    this_file, _warm_workers_fn, _keepalive_vision_fn)."""
    _duo_hard_stop: bool = False
    _graceful_stopped: bool = False
    _duo_timed_out: bool = False
    _loop_detected: bool = False
    _wedge_escalated: bool = False
    _model_load_failed: bool = False
    _pending_send_files: list = dataclasses.field(default_factory=list)
    _written_files: list = dataclasses.field(default_factory=list)
    _done_tasks: list = dataclasses.field(default_factory=list)
    _cs: object = None
    _ws_str: str = ""
    _explore_ctx: str = ""
    _project_state: "ProjectState | None" = None
    _project_state_run_counted: bool = False
    _explore_only_rounds: int = 0
    _lifetime_tool_rounds: int = 0
    _di: int = 0
    _n_items: int = 1
    _loop_items: list = dataclasses.field(default_factory=list)
    carry: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _DuoFinalCleanupState:
    """Final-cleanup state (moved verbatim from run_code_duo's finally).

    One field per run_code_duo local the cleanup reads. exec_mdl /
    _run_id_global / _duo_pinned / _project_state /
    _project_state_run_counted are bound before the mega-try and are
    passed directly; _planner_model lives inside it and is ladder-collected
    by the runner (default None = the old except-swallowed
    UnboundLocalError path).
    """
    _planner_model: str | None = None
    exec_mdl: str = ""
    _run_id_global: str = ""
    _duo_pinned: list | None = None
    _project_state: "ProjectState | None" = None
    _project_state_run_counted: bool = False


async def _duo_final_cleanup(ctx, ps: _DuoFinalCleanupState):
    # CODE-MOTION (duo-runner-split): the mega-try finally body (duo slot
    # unpin, coder-after-run evict, pause/resume cleanup, duo unpin task,
    # project-state save) moved VERBATIM from run_code_duo; only the unpack
    # at entry is new. No logic changes.
    _planner_model = ps._planner_model
    exec_mdl = ps.exec_mdl
    _run_id_global = ps._run_id_global
    _duo_pinned = ps._duo_pinned
    _project_state = ps._project_state
    _project_state_run_counted = ps._project_state_run_counted
    # DUO-SLOT UNPIN (2026-10-01): the run pinned its planner/coder slots
    # so the vision-preprocessing load could not LRU-evict them; release
    # the pin so normal eviction can reclaim VRAM after the run.
    try:
        from backend.llama_server_manager import manager as _lsm_unpin
        for _m in (_planner_model, exec_mdl):
            if _m:
                _lsm_unpin.clear_pin(_m)
    except Exception:
        pass
    # CODER LIFETIME: with
    # duo_coder_after_run = "evict" the coder slot is released here - the
    # next prompt can then load any model/mode without evicting something
    # first. "keep" (default) leaves the slot warm; the idle TTL decides.
    # This finally-block also runs on abort, so an aborted run releases the
    # slot too.
    try:
        if str(ctx.settings.get("duo_coder_after_run", "keep")).strip().lower() == "evict" and exec_mdl:
            from backend.llama_server_manager import manager as _lsm_car
            await _lsm_car.evict(exec_mdl)
            logger.info("[CODER-AFTER-RUN] coder slot evicted (duo_coder_after_run=evict)")
    except (ImportError, AttributeError, TypeError, RuntimeError, OSError) as _car_err:
        logger.debug("[CODER-AFTER-RUN] evict skipped: %s", _car_err)
    # Cleanup Pause/Resume State
    try:
        _rid = _run_id_global
        if _rid:
            from infra.run_control import cleanup_pause
            cleanup_pause(_rid)
    except Exception as _err:
        if isinstance(_err, (
            GeneratorExit,
            asyncio.CancelledError,
            KeyboardInterrupt,
            SystemExit,
        )):
            raise
        logger.debug(
            "[DUO] Suppressed error at cleanup_pause: %s",
            _err, exc_info=True
        )
    _restore_ka = str(ctx.settings.get("smart_preload_keep_alive", "10m"))
    if _duo_pinned:
        spawn(_unpin_duo_fn(ctx, _duo_pinned, _restore_ka))

    if _project_state is not None and ctx.chat_id:
        try:
            from context.project_state import ProjectStateManager
            if not _project_state_run_counted:
                _project_state.total_runs += 1
                _project_state_run_counted = True
            _project_state.last_run_timestamp = datetime.now().isoformat()
            if getattr(_project_state, 'last_run_success', None) is None:
                # SUCCESS-ORDER (2026-09-22): the normal completion path
                # sets last_run_success after this fallback ran — stamping
                # False here logged every completed run as failed first
                # (live: FireWork run ended 'completed' but the interim
                # save said success=False). Mirror the stop reason instead.
                _project_state.last_run_success = (getattr(ctx, "duo_stop_reason", "") == "completed")
            ProjectStateManager().save(_project_state)
            logger.info("[PROJECT] State gesichert in finally (Run #%d, success=%s)",
                        _project_state.total_runs, _project_state.last_run_success)
        except Exception as _save_err:
            logger.warning("[PROJECT] Save in finally failed: %s", _save_err)


async def run_code_duo(ctx) -> AsyncIterator[dict]:
    """Code-duo handler: iterative coder+critic loop."""
    # PLANNER-OFF (2026-11-04): chunking is a planner feature ("Planner
    # creates N subtasks"). With the planner toggle off there is no plan and
    # therefore no chunks; otherwise an old persisted duo_chunking=true kept
    # running the planner phase while the UI hid the row. The coder then gets
    # only the user prompt.
    try:
        if not ctx.duo_config.planner and ctx.duo_config.chunking:
            ctx.duo_config.chunking = False
            logger.warning(
                "[PLANNER-OFF] chunking disabled because the planner is off")
    except AttributeError:
        pass
    state: dict = {
        "coder_mdl": None,
        "critic_mdl": None,
        "exec_mdl": None,
        "_duo_runtime_profile": None,
        "duo_rounds": 1,
        "_planner_step_cap": 6,
        "_duo_run_timeout_s": 300,
        "_duo_deadline_at": 0.0,
        "_duo_timed_out": False,
        "_ctx_peak_tokens": 0,
        "_ctx_limit_seen": 0,
        "_ctx_pressure_peak": 0.0,
        "_ctx_compressions": 0,
        "_ctx_evictions": 0,
        "_tool_round_durations": [],
        "_coder_tc": False,
        "_critic_tc": False,
        "_exec_tc": False,
        "_critic_tools_enabled": False,
        "_duo_pinned": set(),
        "_skip_coder_pin": False,
        "_coder_ctx": 8192,
        "_exec_ctx": 8192,
        "_critic_ctx": 8192,
        "_duo_ws": False,
        "_duo_seen_web_queries": set(),
        "_xtools_ws": [],
        "_tool_think_auto_mode": "off",
        "_exec_supports_thinking": False,
        "_exec_has_thinking": False,
        "_coder_tool_think": False,
        "_avail": set(),
        "_recent_focus_paths": [],
        "_MAX_TREE_DEPTH": 4,
        "_MAX_TREE_FILES": 200,
        "_runtime_ctx_target": 10240,
    }
    async for _ev in _phase_vram(ctx, state):
        yield _ev
    if state.get("_duo_aborted"):
        clear_graceful_stop(ctx.run_id)
        _cleanup_pause_state(ctx.run_id)
        if ctx.chat_id:
            _clear_pause_state(ctx.chat_id)
        _cleanup_governor(ctx.chat_id or ctx.run_id)
        # VRAM-GUARD-DONE (2026-09-15): this used to be a silent return — no
        # done event, so the UI just showed nothing happening ("passiert nix").
        # The vram guard already emitted the reason as a status event; close
        # the stream properly now.
        logger.warning(
            "[VRAM-GUARD] run aborted before any model load (coder ctx/VRAM "
            "vs budget) — sending done (stop_reason=vram_guard)")
        yield await ctx.emit(ctx.done_event(
            round(time.time() - ctx.t_total, 1), "vram_guard",
            **ctx.collect_done_metrics()))
        return

    # Unpack state to locals for Phase B+ backward compatibility
    coder_mdl = state["coder_mdl"]
    critic_mdl = state["critic_mdl"]
    exec_mdl = state["exec_mdl"]
    _duo_runtime_profile = state["_duo_runtime_profile"]
    duo_rounds = state["duo_rounds"]
    _planner_step_cap = state["_planner_step_cap"]
    _duo_run_timeout_s = state["_duo_run_timeout_s"]
    _duo_deadline_at = state["_duo_deadline_at"]
    _duo_timed_out = state["_duo_timed_out"]
    _ctx_peak_tokens = state["_ctx_peak_tokens"]
    _ctx_limit_seen = state["_ctx_limit_seen"]
    _ctx_pressure_peak = state["_ctx_pressure_peak"]
    _ctx_compressions = state["_ctx_compressions"]
    _ctx_evictions = state["_ctx_evictions"]
    _tool_round_durations = state["_tool_round_durations"]
    _coder_tc = state["_coder_tc"]
    _critic_tc = state["_critic_tc"]
    _exec_tc = state["_exec_tc"]
    _critic_tools_enabled = state["_critic_tools_enabled"]
    _duo_pinned = state["_duo_pinned"]
    _skip_coder_pin = state["_skip_coder_pin"]
    _coder_ctx = state["_coder_ctx"]
    _exec_ctx = state["_exec_ctx"]
    _critic_ctx = state["_critic_ctx"]
    _duo_ws = state["_duo_ws"]
    logger.info(
        "[WEBSEARCH-DIAG] coder tools advertised with websearch=%s (setting=%s, server_available=%s)",
        bool(_duo_ws),
        bool(ctx.settings.get("duo_websearch_enabled", False)),
        bool(ctx.websearch_available),
    )
    _duo_seen_web_queries = state["_duo_seen_web_queries"]
    _xtools_ws = state["_xtools_ws"]
    _tool_think_auto_mode = state["_tool_think_auto_mode"]
    _exec_supports_thinking = state["_exec_supports_thinking"]
    _exec_has_thinking = state["_exec_has_thinking"]
    _coder_tool_think = state["_coder_tool_think"]
    _avail = state["_avail"]
    _recent_focus_paths = state["_recent_focus_paths"]
    _MAX_TREE_DEPTH = state["_MAX_TREE_DEPTH"]
    _MAX_TREE_FILES = state["_MAX_TREE_FILES"]
    _runtime_ctx_target = state["_runtime_ctx_target"]


    # WORKSPACE-FIX (2026-08-25 REWORK): Zentrale Aufloesung via
    # utils/workspace_resolve.py. WS-PROVENANCE (2026-10-15): the chat_run
    # resolution (ctx.workspace, request-aware) is authoritative — the old
    # re-resolution here ignored request_workspace and chat_ctx, so a request
    # for calc_app could silently land in last_used (gravSandbox) and the
    # re-sync then re-pointed the process-global ENV for the whole run.
    from utils.workspace_resolve import (
        resolve_workspace as _ws_resolve,
        sync_env_workspace as _ws_sync_env,
        effective_run_workspace as _eff_run_ws,
    )
    if getattr(ctx, "workspace", ""):
        _ws_str, _ws_src = ctx.workspace, "chat_run"
    else:
        _ws_str, _ws_src = _ws_resolve(ctx.settings, None, ctx.user_input)
    _ws_sync_env(_ws_str)
    logger.warning("[WS-RESOLVE] duo pre-phase workspace=%s (source=%s)", _ws_str, _ws_src)
    state["_ws_str"] = _ws_str

    try:
        from core.run_audit import record_run_audit
        record_run_audit(ctx.chat_id, {
            "run_id": ctx.run_id,
            "event": "run_start",
            "pre_explore": bool(ctx.duo_config.pre_explore),
            "planner": bool(ctx.duo_config.planner),
            "chunking": bool(ctx.duo_config.chunking),
            "agentic": bool(ctx.duo_config.agentic_mode),
            "until_finished": bool(ctx.duo_config.until_finished),
            "coder_mdl": str(getattr(ctx.duo_config, "coder_mdl", "") or ""),
            "workspace": str(_ws_str),
        })
    except Exception:
        pass

    # ── ProjectState: chat-bound project state ──
    _project_state = None
    if ctx.chat_id:
        from context.project_state import ProjectStateManager
        _pmgr = ProjectStateManager()
        _project_state = _pmgr.load(ctx.chat_id)
        if _project_state is None:
            _project_state = _pmgr.create(ctx.chat_id, _ws_str)
            logger.info("[PROJECT-INIT] New project: %s", _project_state.project_name)
        else:
            logger.info("[PROJECT-RESUME] %s, Run #%d, %d Steps",
                        _project_state.project_name,
                        _project_state.total_runs + 1,
                        len(_project_state.build_history))
        _current_project_state.set(_project_state)

    # PRE-EXPLORE-GUARD (2026-10-20): ein Fehler in der Exploration hat bisher
    # den kompletten Run abgebrochen. Die statische Repo-Map ist ohnehin schon
    # der Fallback, wenn LLM-Pre-Explore aus ist - also hier genauso behandeln:
    # loggen, dem Nutzer sagen, ohne Exploration weitermachen.
    try:
        async for _ev in _phase_pre_explore(ctx, state):
            yield _ev
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except (RuntimeError, ValueError, TypeError, KeyError, LookupError, OSError,
            asyncio.TimeoutError, UnboundLocalError, NameError) as _pre_exc:
        logger.error("[PRE-EXPLORE-FAILED] %s: %s\n%s",
                     type(_pre_exc).__name__, _pre_exc, traceback.format_exc())
        yield await ctx.emit({
            "type": "status",
            "content": f"⚠ Pre-Explore failed ({type(_pre_exc).__name__}) - "
                       f"continuing with the static repo map only",
        })
    _ws_str = state["_ws_str"]
    _explore_ctx = state.get("_explore_ctx", "")
    _tree_ctx = state.get("_tree_ctx", "")
    _resume_data = state.get("_resume_data")
    _plan_tracker = state.get("_plan_tracker")
    _contracts_raw = state.get("_contracts_raw", [])
    _pre_explore_msgs = state.get("_pre_explore_msgs", [])
    _use_parallel = state.get("_use_parallel", False)
    _worker_slots = state.get("_worker_slots", [])
    _workers_were_loaded = state.get("_workers_were_loaded", False)
    _xexplore_mdl = state.get("_xexplore_mdl", exec_mdl)
    _touched_paths = state.get("_touched_paths", set())


    ctx.pipeline.agents["duo_coder"].model  = coder_mdl
    if not ctx.duo_config.agentic_mode:
        ctx.pipeline.agents["duo_critic"].model = critic_mdl

    _duo_coder_cfg  = ctx.get_effective_config(ctx.this_file, coder_mdl,  "duo_coder",  ctx.use_learned) if ctx.use_learned else {}
    _duo_coder_temp  = float(_duo_coder_cfg.get("temperature", ctx.pipeline.agents["duo_coder"].temperature))
    _duo_coder_tok   = int(_duo_coder_cfg.get("max_tokens",    ctx.pipeline.agents["duo_coder"].max_tokens))
    if not ctx.duo_config.agentic_mode:
                        _duo_critic_cfg = ctx.get_effective_config(ctx.this_file, critic_mdl, "duo_critic", ctx.use_learned) if ctx.use_learned else {}
                        _duo_critic_temp = float(_duo_critic_cfg.get("temperature", ctx.pipeline.agents["duo_critic"].temperature))
                        _duo_critic_tok  = int(_duo_critic_cfg.get("max_tokens",   ctx.pipeline.agents["duo_critic"].max_tokens))
    else:
        _duo_critic_cfg = {}
        _duo_critic_temp = 0.15
        _duo_critic_tok  = 600

    # Prompts: preset-overridable via get_effective_prompt_with_override (from prompts.py)
    _duo_coder_sys  = ctx.get_effective_prompt_with_override("duo_coder", ctx.active_preset, ctx.use_learned)
    if not _duo_coder_sys:
        _duo_coder_sys = _DUO_CODER_SYS_DEFAULT
    if not ctx.duo_config.agentic_mode:
        # Critic mode: coding → TUNE code review (DUO_CRITIC_CODE); general → TUNE general review (DUO_CRITIC_GENERAL)
        _duo_critic_key = "duo_critic_code" if ctx.duo_config.coding_mode else "duo_critic_general"
        _duo_critic_sys = ctx.get_effective_prompt_with_override(_duo_critic_key, ctx.active_preset, ctx.use_learned)
        if not _duo_critic_sys:
            _duo_critic_sys = _DUO_CRITIC_CODE_DEFAULT if ctx.duo_config.coding_mode else _DUO_CRITIC_GEN_DEFAULT
        _critic_thinking = False
        # IMAGE DESCRIPTION FOR CRITIC (2026-10-03): the critic judges the
        # coder's output against the image - without the description it
        # cannot check visual requirements (live: "add a firework like in
        # the image" plans failed silently in review).
        if getattr(ctx, "image_description", "") and not _duo_critic_sys.startswith("[IMAGE DESCRIPTION]"):
            _duo_critic_sys += "\n\n[IMAGE DESCRIPTION - judge visual requirements against this]:\n" + ctx.image_description
        _critic_profile = get_sampling_profile(critic_mdl, _critic_thinking, ctx.settings)
    else:
        _duo_critic_sys = ""
        _critic_thinking = False
        _critic_profile = {}

    # Phase 3: pre-prompt ctx.memory injection (repo-specific learned insights)
    _coder_dyn_hints = ""  # separate accumulator for dynamic runtime hints
    _repo_mem_enabled = bool(ctx.settings.get("duo_repo_memory_enabled", True))
    _repo_mem_top_k = max(1, min(4, int(ctx.settings.get("duo_repo_memory_top_k", 2) or 2)))
    _repo_mem_hits: list[dict] = []
    if _repo_mem_enabled:
        yield await ctx.emit({"type": "status", "content": "🧠 Searching repository memory for insights…"})
        try:
            _repo_mem_hits = ctx.memory.query_repo_insights(
                ctx.user_input,
                trigger_path=_ws_str,
                top_k=_repo_mem_top_k,
                min_score=float(ctx.settings.get("duo_repo_memory_min_score", 0.12) or 0.12),
            )
        except Exception as _e:
            if isinstance(_e, (
                GeneratorExit,
                asyncio.CancelledError,
                KeyboardInterrupt,
                SystemExit,
            )):
                raise
            _repo_mem_hits = []
    if _repo_mem_hits:
        _hint_lines = [f'- {h.get("insight", "")}' for h in _repo_mem_hits if h.get("insight")]
        if _hint_lines:
            _coder_dyn_hints += (
                "\n\n[Memory: Relevant learned repo insights]\n"
                + "\n".join(_hint_lines[:_repo_mem_top_k])
                + "\n[End ctx.memory]\n"
            )
            yield await ctx.emit({
                "type": "status",
                "content": f"🧠 Loaded {min(len(_hint_lines), _repo_mem_top_k)} learned repo insight(s) for this task.",
            })

    # Phase D follow-up: symbol-level retrieval hints (lightweight LSP-like scan)
    _symbol_hint_enabled = bool(ctx.settings.get("duo_symbol_ref_enabled", True))
    _symbol_hint_top_k = max(1, min(4, int(ctx.settings.get("duo_symbol_ref_top_k", 2) or 2)))
    _symbol_hint_max_items = max(40, min(400, int(ctx.settings.get("duo_symbol_ref_max_items", 120) or 120)))
    _symbol_hints: list[dict] = []
    if _symbol_hint_enabled and _ws_str:
        yield await ctx.emit({"type": "status", "content": "🧭 Building symbol reference tree for upcoming modifications…"})
        try:
            _symbol_hints = await _build_symbol_reference_hints(
                ctx.user_input,
                _ws_str,
                top_k=_symbol_hint_top_k,
                max_items=_symbol_hint_max_items,
            )
        except Exception as _e:
            if isinstance(_e, (
                GeneratorExit,
                asyncio.CancelledError,
                KeyboardInterrupt,
                SystemExit,
            )):
                raise
            _symbol_hints = []
    if _symbol_hints:
        _symbol_lines: list[str] = []
        for _h in _symbol_hints:
            _symbol = str(_h.get("symbol", "")).strip()
            _matches = [str(x).strip() for x in (_h.get("matches", []) or []) if str(x).strip()]
            if _symbol and _matches:
                _symbol_lines.append(f"- {_symbol}: " + " | ".join(_matches[:2]))
        if _symbol_lines:
            _coder_dyn_hints += (
                "\n\n[Memory: Relevant symbol references]\n"
                + "\n".join(_symbol_lines[:_symbol_hint_top_k])
                + "\n[End symbol references]\n"
            )
            yield await ctx.emit({
                "type": "status",
                "content": f"🧭 Loaded {min(len(_symbol_lines), _symbol_hint_top_k)} symbol reference hint(s).",
            })

    _matched_skills: list = []
    try:
        from hive_functions.skills import load_skills, match_skills, format_skill_coder, workspace_file_paths
        _skills_loaded = load_skills(_ws_str) if _ws_str else []
        if _skills_loaded:
            _ws_files = workspace_file_paths(_ws_str)
            _matched_skills = match_skills(_skills_loaded, ctx.user_input, _ws_files)
            if _matched_skills:
                _skills_lines = ["[Skills: relevant reusable patterns]"]
                for _sk in _matched_skills[:3]:
                    _skills_lines.append(format_skill_coder(_sk))
                _coder_dyn_hints += "\n\n" + "\n\n".join(_skills_lines) + "\n[End skills]\n"
                yield await ctx.emit({"type": "status",
                    "content": f"🧩 Loaded {min(len(_matched_skills), 3)} matched skill(s)."})
    except Exception as _sk_err:
        if isinstance(_sk_err, (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        logger.debug("[SKILLS] Load/match failed: %s", _sk_err)

    # _coder_tc/_critic_tc: computed above (DUO-1 FIX)


    coder_out       = ""
    critic_issues:  list = []
    final_verdict   = ""
    _duo_hard_stop  = False
    _graceful_stopped = False
    _run_id_global  = ""

    # NOTE: _run_agentic_post_review_async removed — Critic not used in agentic ctx.mode.
    # QC in agentic/chunking is handled by Auto-Test self-fix loop instead.
    # Duo (non-agentic) ctx.mode still uses the blocking Critic below.

    _project_state_run_counted = False

    try:
        _subtasks: list[str] = []
        _plan_thinking = ""
        _plan_content_txt = ""
        _plan_result: "PlannerResult | None" = None
        # INLOOP-PLAN-INIT (audit 2026-11-03): the end-of-run context save
        # reads these; they were bound only inside the planner-off path, so a
        # skipped planner (follow-up/resume/toggle) raised UnboundLocalError
        # and the whole sidecar save (last_run/task_sig/session) was skipped.
        _inloop_plan_text = ""
        _inloop_plan_thinking = ""
        _planner_used_fallback = False
        _planner_fallback_reason = ""
        _planner_parse_mode = "none"
        _planner_context_trimmed = False
        _plan_guard_quality: dict = {}
        # SKIP-CHECK: Skip Planner if skip pressed — go straight to Coder
        _planner_skipped = False
        _fup_task_ctx = state.get("_follow_up_task_ctx", "") or ""
        # FOLLOW-UP-NOPLAN: the coder already gets the previous plan + files
        # through the follow-up hint. Planning again cost a model load and
        # produced generic "clarify scope" steps whenever the plan text did not
        # parse - exactly the "new plan out of nowhere" the follow-up must not
        # do.
        _follow_up_active = bool(state.get("_follow_up_active"))
        if (ctx.duo_config.chunking or ctx.duo_config.planner) and ctx.step_skipped() and not ctx.aborted():
            ctx.clear_step_skip()
            yield await ctx.emit({"type":"status",
                "content": "⏭ Planner skipped — going straight to coder"})
            _planner_skipped = True
            yield await ctx.emit({"type": "planner_done", "summary": "⏭ skipped"})
            ctx.phase_timer.skip("soft_planner")
        if (not (ctx.duo_config.chunking or ctx.duo_config.planner)
                and not ctx.aborted() and not _resume_data
                and not _planner_skipped and not ctx.step_skipped()):
            # PLANNER-VISIBILITY (2026-09-08): this state used to fall through
            # silently — planner toggle off + chunking off skipped the whole
            # phase with zero feedback while the UI still showed a planner
            # model ("planner stays off after preset load").
            yield await ctx.emit({"type": "status",
                "content": "⏹ Planner phase off (planner + chunking toggles both off) — going straight to coder"})
        # PLANNER-MODEL-DEFAULT (2026-09-10): without planner AND without
        # chunking the block below never runs, and the post-explore worker
        # evict hit `UnboundLocalError: _planner_model` (caught as a broad
        # warning; evict/coder-load cleanup was skipped). Default to the
        # coder model: the evict logic then simply excludes the coder itself.
        _planner_model = coder_mdl
        # C2 (2026-11-07): the extracted pre-loop block always receives these
        # two; planner-skipping paths (resume/follow-up) never ran the
        # finally-sync, so initialize them like _planner_model above.
        _plan_ctx_final = 0
        _plan_port = None
        # RESUME-DEFAULT (2026-10-04, live UI test): the plan is computed in
        # the planner block below, which RESUME runs skip entirely - every
        # later read (vision gates, traces, attach) crashed the whole run
        # with UnboundLocalError. Same class as the _coder_port init.
        _img_plan = {"planner": "none", "coder": "none", "warnings": [], "mode": None}
        # IMAGE-PLAN-INDEPENDENT (P1, audit 2026-11-03): the planner phase is
        # the only caller of resolve_image_plan. Follow-ups skip it, so an
        # attached image was dropped although chat_run had announced a real
        # plan. Resolve it here; the planner phase recomputes with its refined
        # model when it actually runs.
        if ctx.images:
            try:
                _img_plan = resolve_image_plan(
                    _planner_model, exec_mdl, ctx.settings, ctx)
            except (ImportError, AttributeError, TypeError, ValueError,
                    KeyError) as _img_exc:
                logger.debug("image plan pre-resolve failed: %s", _img_exc)
        if _follow_up_active and (ctx.duo_config.chunking or ctx.duo_config.planner):
            yield await ctx.emit({"type": "status",
                "content": "🔁 Follow-up: continuing the previous plan - no new plan"})
        if ((ctx.duo_config.chunking or ctx.duo_config.planner) and not ctx.aborted()
                and not _resume_data and not _planner_skipped and not _follow_up_active):
            _plan_ps = _DuoPlannerPhaseState(
                subtasks=_subtasks, plan_thinking=_plan_thinking, plan_content_txt=_plan_content_txt,
                plan_result=_plan_result, planner_used_fallback=_planner_used_fallback,
                planner_fallback_reason=_planner_fallback_reason, planner_parse_mode=_planner_parse_mode,
                planner_context_trimmed=_planner_context_trimmed, plan_guard_quality=_plan_guard_quality,
                planner_model=_planner_model, img_plan=_img_plan, duo_timed_out=_duo_timed_out,
                plan_tracker=_plan_tracker,
            )
            try:
                async for _ev in _duo_planner_phase(
                    ctx, _plan_ps,
                    coder_mdl=coder_mdl, critic_mdl=critic_mdl, exec_mdl=exec_mdl,
                    _ws_str=_ws_str, _explore_ctx=_explore_ctx, _xtools_ws=_xtools_ws,
                    _coder_ctx=_coder_ctx, _contracts_raw=_contracts_raw,
                    _matched_skills=_matched_skills, _planner_step_cap=_planner_step_cap,
                    _project_state=_project_state, _duo_runtime_profile=_duo_runtime_profile,
                    _duo_deadline_at=_duo_deadline_at, _duo_run_timeout_s=_duo_run_timeout_s,
                    _fup_task_ctx=_fup_task_ctx,
                ):
                    yield _ev
            finally:
                _subtasks = _plan_ps.subtasks
                _plan_thinking = _plan_ps.plan_thinking
                _plan_content_txt = _plan_ps.plan_content_txt
                _plan_result = _plan_ps.plan_result
                _planner_used_fallback = _plan_ps.planner_used_fallback
                _planner_fallback_reason = _plan_ps.planner_fallback_reason
                _planner_parse_mode = _plan_ps.planner_parse_mode
                _planner_context_trimmed = _plan_ps.planner_context_trimmed
                _plan_guard_quality = _plan_ps.plan_guard_quality
                _planner_model = _plan_ps.planner_model
                _img_plan = _plan_ps.img_plan
                _plan_port = _plan_ps.plan_port
                _plan_ctx_final = _plan_ps.plan_ctx_final
                _duo_timed_out = _plan_ps.duo_timed_out
                _plan_tracker = _plan_ps.plan_tracker

        _cs = ChunkState()  # ChunkState replaces _done_tasks, _written_files, _coder_outputs, etc.
        _done_tasks = _cs.done_tasks          # backward-compat aliases for the loop
        _written_files = _cs.written_files
        _coder_outputs = _cs.coder_outputs

        # Fortschritt). No-Overwrite-Guard wie im Safety-Net.
        _total_tool_rounds: int = 0
        # C1 (2026-11-07): loop-scoped names read by the finalize/save paths.
        # Defaults here make every former `in dir()` / `locals().get()` probe
        # a plain field read.
        _di = 0
        _n_items = 1
        _loop_items = [{"title": ctx.user_input}]
        _drop_names: list = []
        _dr_dropped_tool_retries = "n/a"
        # SEND-FILE: paths validated by the tool, delivered only after the
        # final verification. Run-persistent across tool rounds.
        _pending_send_files: list[str] = []
        _max_tool_rounds: int = 0    # default before tool-round setup
        _lifetime_tool_rounds: int = 0
        _total_tool_errors_ref: list[int] = [0]
        _connect_error_retries: int = 0
        _force_compress_next: bool = False
        _compress_fail_streak: int = 0      # CONSECUTIVE-FAIL-GUARD: limit 400->compress->fail->restore cycles
        _loop_detected = False   # guard against UnboundLocalError on early exit (abort/timeout)
        _model_load_failed = False  # coder/planner GGUF never came up (VRAM/missing)
        _chunk_budget_exhausted = False  # chunk containment: budget spent (incl. grace)
        _explore_only_rounds: int = 0
                                        # late initialization (depends on tool-round setup)
        _stuck_reason = ""       # guard against UnboundLocalError on early exit
        _ctx_pressure_warned = False  # CTX pressure warn-once guard
        _touched_block_persisted = ""  # Persisted across chunks for pre-explore context
        # P1-3 FIX: Cache coder port across chunk ctx.iterations — coder is pinned,
        # so ensure_loaded is a no-op after first call but costs ~200-300ms each time.
        _cached_coder_port: int | None = None
        _cached_coder_port_ctx: int | None = None
        # LAZY-AGENTIC (2026-10-04, live gate test): in lazy mode the coder
        # load (and with it _coder_port's first assignment) happens inside
        # the tool loop - the attach-time vision check read it one round
        # earlier and crashed the run (UnboundLocalError). Initialize here so
        # every later read is defined; None means "no coder port yet".
        _coder_port: int | None = None
        _auto_tool_promote_streak = 0
        _auto_tool_promote_notified = False
        _last_run_bash_failure: dict | None = None
        _changed_since_failure: set[str] = set()
        _last_learned_insight_sig = ""
        _verify_mutation_serial = 0
        _verify_last_ok_serial = 0
        _task_complete_blocked = [0]
        # RUN-PERSISTENT-REFS (2026-09-11): ToolRoundState is re-created every
        # round; these must survive the whole run, so they live as list refs
        # here and are passed into each round's ToolRoundState.
        _tc_consecutive = [0]          # TC-DE-NAG
        _at_nosuite_nudged = [False]   # SMOKE-NUDGE (once per run)
        _consecutive_reads = [0]       # READ-LADDER
        _last_read_path = [""]         # READ-LADDER (path-reset)
        _read_ladder_fired = [False]   # READ-LADDER
        _ws_budget_streak = [0]        # WEBSEARCH-BUDGET-STOP
        _ws_disabled = [False]         # WEBSEARCH-SOFT-STOP (run-persistent)
        _wedge_handoffs_used = [0]     # WEDGE-HANDOFF: per-run budget ref
        _wedge_escalated = False       # WEDGE-HANDOFF: run stopped via failed/exhausted handoff
        # VERIFY-GATE-OWNER (2026-09-09): own veto counter. The executor's
        # task_complete ladder used to pre-satisfy this gate (shared counter),
        # skipping verification entirely after 3 in-loop blocks.
        _verify_gate_blocks = 0
        _verify_warned = False
        _completion_unverified = False   # completion accepted without a green test
        # laufen via asyncio.gather (tool_executor _PARALLEL_SAFE) in Child-Tasks
        # bliebe _files_read_in_run im Parent None (Compression-Removal + SKIP-
        try:
            import time as _time_epoch
            from tools.runner import _files_read_in_run as _fir_run_ctx
            from tools.runner import _files_seen_in_run as _fsr_run_ctx
            from tools.runner import _files_written_in_run as _fw_run_ctx
            from tools.runner import _run_file_epoch as _epoch_run_ctx
            from tools.runner import _files_written_by_run as _fw_registry
            _fw_registry.pop(str(ctx.run_id), None)
            if _fir_run_ctx.get(None) is None:
                _fir_run_ctx.set(set())
            if _fsr_run_ctx.get(None) is None:
                _fsr_run_ctx.set(set())
            if _fw_run_ctx.get(None) is None:
                _fw_run_ctx.set(set())
            # Fresh epoch per run: send_file may deliver a file produced by
            # run_python/run_bash, not only by write_file.
            _epoch_run_ctx.set(_time_epoch.time())
        except Exception as _rg_err:
            logger.warning("[READ-GUARD] Parent context init failed: %s", _rg_err)
        _last_test_status = ""
        try:
            _auto_tool_promote_cap = int(ctx.settings.get("duo_tool_autopromote_max_rounds", 4))
        except Exception:
            _auto_tool_promote_cap = 4
        _auto_tool_promote_cap = max(0, _auto_tool_promote_cap)

        if _resume_data:
            _cs.load_from_resume(_resume_data)
            # _done_tasks and _written_files are now populated via _cs
            # SEND-FILE-RESUME: restore the explicit send queue from the
            # resume block (dedupe, keep order). A non-empty local list
            # stays authoritative — never clobber it.
            for _rsf in _cs.send_files:
                if _rsf and _rsf not in _pending_send_files:
                    _pending_send_files.append(_rsf)
            _subtasks      = [c.get("title", str(c)) if isinstance(c, dict) else str(c)
                              for c in _resume_data["resume"].get("chunks_remaining", [])]
            if not _subtasks:
                _subtasks = []

        # ── Build Coder System Prompt from modular blocks ───────────
        _has_plan = bool(_subtasks) or bool(_plan_thinking) or bool(
            _plan_result is not None and _plan_result.plan_content
        )
        # EXPLORE-CONTENT-TRUTH (2026-09-06): a static symbol map (created when
        # pre_explore=False) lists only paths/symbols/imports — NO file
        # contents. It is NOT "codebase explored". has_explore_ctx may only be
        # True if the LLM pre-explore produced real results (messages) or
        # architecture contracts. Otherwise _build_duo_coder_sys wrongly
        # picks DUO_CODER_EXPLORED (with "no read_file needed") and the coder
        # blindly overwrites existing files via write_file.
        _explore_has_contents = bool(state.get("_pre_explore_msgs")) or bool(state.get("_contracts_raw"))
        _duo_coder_sys = _build_duo_coder_sys(
            ctx,
            has_plan=_has_plan,
            has_subtasks=bool(_subtasks),
            has_explore_ctx=_explore_has_contents,
        ) + _coder_dyn_hints + build_image_desc_block(getattr(ctx, "image_description", None))
        # VISION-TRUTH GATE pre-prompt (2026-10-04): if the
        # coder slot is ALREADY loaded and really runs without a projector,
        # downgrade the plan BEFORE the notes/parts decide from it. No slot
        # loaded (fresh load below) trusts the plan - the post-load gate
        # re-checks against /props truth.
        _cv_pre = None                 # unknown = no vision claim (strict rule)
        if _img_plan.get("coder") == "raw":
            try:
                from backend.llama_server_manager import manager as _lsm_vgate
                _cslot_pre = await _lsm_vgate._find_loaded(exec_mdl)
                _cv_pre = getattr(_cslot_pre, "vision_active", None) if _cslot_pre is not None else None
                if _cv_pre is False:
                    _img_plan, _vg_warns = effective_image_plan(
                        _img_plan, coder_slot_vision=_cv_pre,
                        has_description=bool(str(getattr(ctx, "image_description", "") or "").strip()))
                    for _w in _vg_warns:
                        logger.warning("[DUO][VISION-GATE] %s", _w)
                        yield await ctx.emit({"type": "status", "content": "⚠ " + _w})
            except (ImportError, AttributeError, TypeError, ValueError) as _vg_err:
                logger.debug("[DUO][VISION-GATE] coder pre-check skipped: %s", _vg_err)
        if _img_plan.get("planner") == "raw" and _img_plan.get("coder") != "raw":
            _duo_coder_sys += (
                "\n\n[IMAGE NOTE]: you cannot see the attached image — the planner saw it. "
                "Put all relevant details (layout, texts, colors, sizes) into the plan.")
        elif _img_plan.get("coder") == "raw":
            # VISION NOTE (2026-10-04): the coder receives the image itself
            # as a content part - acknowledge that so the model knows it can
            # reference what it sees instead of guessing from the plan text.
            # Kept as ONE constant so the post-load gate can retract it.
            # STRICT-VISION (2026-10-17): only when the slot is CONFIRMED to
            # run with its projector. A slot that is not loaded yet (or whose
            # /props read failed) must not produce a "you can see it" claim -
            # the post-load gate adds the note once vision is confirmed.
            _vn_pre = vision_note_for_coder(_img_plan, _cv_pre)
            if _vn_pre:
                _duo_coder_sys += _vn_pre
        # SAVED-PATH NOTE (2026-10-03): the original upload lives in the
        # workspace now - tell the coder once so it can reference/reuse it.
        _img_paths = getattr(ctx, "image_paths", None) or []
        if _img_paths:
            _duo_coder_sys += (
                "\n\n[IMAGE FILES]: the user's uploaded image(s) are saved at: "
                + ", ".join(_img_paths)
                + ". Reference them by path if needed (binary - do not cat it).")
        _follow_up_hint = state.get("_follow_up_hint", "") or ""
        # PLANNER-OFF (2026-11-04): with planner and chunking both off the
        # coder gets only the user prompt - no previous-plan hint either.
        if _follow_up_hint and (ctx.duo_config.planner or ctx.duo_config.chunking):
            _duo_coder_sys += "\n\n" + _follow_up_hint


        # C2 (2026-11-07 maintainability sprint): the pre-loop model/VRAM/
        # evict + tool-setup block moved VERBATIM to core/duo_coder_preloop.
        # Inputs/outputs travel through the state; early exits set
        # ps.stop and the runner returns after draining the generator.
        _pre_ps = _DuoCoderPreloopState(
            done_tasks=_done_tasks, written_files=_written_files,
            subtasks=_subtasks, explore_ctx=_explore_ctx, ws_str=_ws_str,
            coder_mdl=coder_mdl, exec_mdl=exec_mdl,
            planner_model=_planner_model, duo_coder_tok=_duo_coder_tok,
            duo_pinned=_duo_pinned, worker_slots=_worker_slots,
            plan_ctx_final=_plan_ctx_final, plan_port=_plan_port,
            use_parallel=_use_parallel, xexplore_mdl=_xexplore_mdl,
            workers_were_loaded=_workers_were_loaded,
            skip_coder_pin=_skip_coder_pin, img_plan=_img_plan,
            duo_hard_stop=_duo_hard_stop,
        )
        async for _ev in _duo_coder_preloop(ctx, _pre_ps):
            yield _ev
        _duo_coder_sys = _pre_ps.duo_coder_sys
        _always_vision = _pre_ps.always_vision
        _coder_ctx_override = _pre_ps.coder_ctx_override
        _coder_port = _pre_ps.coder_port
        _cached_coder_port = _pre_ps.cached_coder_port
        _cached_coder_port_ctx = _pre_ps.cached_coder_port_ctx
        _img_plan = _pre_ps.img_plan
        _duo_hard_stop = _pre_ps.duo_hard_stop
        if _pre_ps.stop:
            return

        # MONOTONIC-DEADLINE (audit A3): the run deadline lives on
        # time.monotonic() — a wall-clock (NTP) jump used to expire or extend
        # runs spuriously. Every consumer swaps in the same commit; the log
        # line keeps its human-readable wall-clock estimate separately.
        # UNTIL-FINISHED-BOUND: a fixed 24 h (86400 s) was the only limit on
        # an "unlimited" run. The wall clock now comes from
        # duo_until_finished_max_hours (default 24 h, clamped to 1..72).
        _uf_hours = 24.0
        try:
            _uf_hours = max(1.0, min(72.0, float(
                ctx.settings.get("duo_until_finished_max_hours", 24) or 24)))
        except (TypeError, ValueError):
            _uf_hours = 24.0
        _duo_deadline_at = time.monotonic() + (
            _uf_hours * 3600.0 if ctx.duo_config.until_finished else _duo_run_timeout_s
        )
        ctx.phase_timer.start("coder_loop")
        logger.debug(
            "Coder deadline set: +%.0fs (until %s) | phases so far: %s",
            _duo_run_timeout_s,
            time.strftime("%H:%M:%S", time.localtime(time.time() + _duo_run_timeout_s)),
            ctx.phase_timer.ui_summary(),
        )

        # ── Formal Execution State Machine (Phase F) ───────────────
        # DEFENSIVE: If ctx.duo_config.chunking is off, ensure _subtasks is empty —
        # prevents stale subtasks from resume or planner leaking through.
        if not ctx.duo_config.chunking and _subtasks:
            logger.debug("[CHUNKING-DEFENSIVE] ctx.duo_config.chunking=False but %d subtasks remain — clearing", len(_subtasks))
            _subtasks = []
        _loop_items = _subtasks if _subtasks else [None] * duo_rounds
        _n_items    = len(_loop_items)   # cache len — computed 5x in loop else
        
        _coder_ctx_eff = (
            resolve_ctx(ctx.settings.get("duo_coder_ctx_agentic"), coder_mdl, "agentic")
            if ctx.duo_config.agentic_mode
            else resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "coder")
        )
        # VRAM-FALLBACK-CTX-CLAMP (2026-09-07): after a coder VRAM fallback the
        # slot runs at _fb_ctx; _coder_ctx_eff (and thus _dtool_ctx,
        # num_predict, all ctx guards) must follow that slot — otherwise
        # [DUO] Cached port ctx mismatch + immediate LD-SET-2497 stop.
        if _coder_ctx_override and _coder_ctx_eff > int(_coder_ctx_override):
            logger.warning(
                "[CODER-FALLBACK-CTX] clamp coder ctx %d -> %d (VRAM-Fallback, Modell %s)",
                int(_coder_ctx_eff), int(_coder_ctx_override), coder_mdl,
            )
            _coder_ctx_eff = int(_coder_ctx_override)
        _coder_caps = compute_char_caps(_coder_ctx_eff, overrides=ctx.settings.get("duo_caps"))
        # CODER-EXPLORE-WINDOW (2026-08-17): explicit lever next to duo_static_map_chars.
        _coder_explore_override = int(ctx.settings.get("duo_coder_explore_chars", 0) or 0)
        if _coder_explore_override > 0:
            _coder_caps = dataclasses.replace(_coder_caps, explore_inject=_coder_explore_override)
        # Contracts = the rest).
        _coder_map_budget = derive_static_map_budget(
            _coder_ctx_eff,
            int(ctx.settings.get("duo_planner_ctx_target", 0) or 0),
            ctx.settings.get("duo_static_map_chars", 0),
        )
        
        ctx.exec_ctrl = ExecutionController(max_iterations=_n_items)
        
        _accumulated_replan_bonus: int = 0
        _di = 0
        _est_tokens = 0
        _guard_tokens: float = 0.0
        _parts: list = []
        _last_run_bash_failure = None
        while _di < _n_items:
            _subtask = _loop_items[_di]
            _soft_check_done = False  # reset per subtask
            if _subtask:
                yield await ctx.emit({"type": "status",
                    "content": f"🔧 Chunk {_di+1}/{_n_items}: {str(_subtask)[:100]}"})
            ctx.exec_ctrl.iteration = _di + 1
            ctx.exec_ctrl.transition(AgentState.EXPLORE if (_di == 0 and ctx.duo_config.pre_explore) else AgentState.CODING)
            ctx.exec_ctrl.reset_stuck_detection()  # BUG-NO-RESET FIX
            new_transaction().begin()
            # details/semantics see _git_checkpoint_at_chunk_start).
            _ck_out = await _git_checkpoint_at_chunk_start(
                ctx, _ws_str, _di, _n_items, _subtask,
                run_files=list(_cs.written_files or []))
            if _ck_out:
                yield await ctx.emit({"type": "status", "content": _ck_out})
            # it is also used outside (cache invalidation, written_files tracking).
            _file_changes: dict = {}  # path → {"op": str, "lines": int}
            if ctx.aborted():
                ctx.exec_ctrl.abort(StopReason.USER_ABORTED)
                _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, "aborted")
                break
            # EXEC-CTRL-HALT-GUARD: ctx.exec_ctrl.abort() (e.g. MAX_TOOL_ROUNDS, STUCK_IN_LOOP)
            if (ctx.exec_ctrl.state == AgentState.HALTED
                    and ctx.exec_ctrl.stop_reason != StopReason.USER_ABORTED):
                _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, 
                    ctx.exec_ctrl.stop_reason.value if ctx.exec_ctrl.stop_reason else "halted"
                )
                break

            if time.monotonic() >= _duo_deadline_at:
                _duo_timed_out = True
                yield await ctx.emit({
                    "type": "status",
                    "content": (
                        f"⏱ Duo timeout reached ({int(_duo_run_timeout_s)}s). "
                        "Ending with the best intermediate result."
                    ),
                })
                _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, "timeout_guard")
                break

            # ── Resume-abort check: stop button → save resume ─────────────────
            if ctx.chat_id and ctx.is_aborted_chat(ctx.chat_id):
                _remaining_items = [
                    {"title": str(t)} for t in _loop_items[_di:]
                ]
                _did_abort, _abort_sse = await _check_abort_and_maybe_save_resume(
                    chat_id          = ctx.chat_id,
                    workspace        = _ws_str,
                    chunks_total     = _n_items,
                    chunks_done      = _done_tasks,
                    chunks_remaining = _remaining_items,
                    written_files    = _written_files,
                    last_summary     = " | ".join(_done_tasks),
                    plan_msgs        = [],
                    explore_ctx      = _explore_ctx,
                    emit_fn          = ctx.emit,
                    pending_send_files = list(_pending_send_files),
                )
                for _abort_s in _abort_sse:
                    yield _abort_s
                if _did_abort:
                    break

            # ── Graceful-Stop-Check: previous chunk finished cleanly, halt before next ──
            _gs_requested = is_graceful_stop_requested(ctx.run_id)
            if _gs_requested:
                _remaining_items_gs = [
                    {"title": str(t)} for t in _loop_items[_di:]
                ]
                if ctx.chat_id and _remaining_items_gs:
                    _write_resume_block(
                        chat_id=ctx.chat_id,
                        workspace=_ws_str,
                        chunks_total=_n_items,
                        chunks_done=_done_tasks,
                        chunks_remaining=_remaining_items_gs,
                        written_files=_written_files,
                        last_summary=" | ".join(_done_tasks),
                        plan_msgs=[],
                        explore_ctx=_explore_ctx,
                        halt_reason="graceful_stop",
                        graceful_stop_chunk_index=_di - 1 if _di > 0 else None,
                        pending_send_files=list(_pending_send_files),
                    )
                yield await ctx.emit({
                    "type": "run_halted_graceful",
                    "reason": "graceful_after_chunk",
                    "chunks_done": len(_done_tasks),
                    "chunks_remaining": len(_remaining_items_gs),
                    "last_chunk_committed": _di > 0,
                })
                yield await ctx.emit({
                    "type": "status",
                    "content": (
                        f"⏸ Graceful stop: {len(_done_tasks)}/{_n_items} chunks done. "
                        f"Resume saved — send a new message to continue."
                    ),
                })
                clear_graceful_stop(ctx.run_id)
                _graceful_stopped = True
                break

            # ── Manual-Pause-Check: suspend run, keep alive ──────────────────
            if is_pause_requested(ctx.run_id):
                clear_pause_request(ctx.run_id)
                if ctx.chat_id:
                    _persist_pause_state(
                        chat_id=ctx.chat_id,
                        run_id=ctx.run_id,
                        chunks_done=len(_done_tasks),
                        chunks_remaining=_n_items - _di,
                        written_files=list(_written_files),
                    )
                yield await ctx.emit({
                    "type": "run_paused_manual",
                    "run_id": ctx.run_id,
                    "chunks_done": len(_done_tasks),
                    "chunks_remaining": _n_items - _di,
                    "last_completed_chunk": len(_done_tasks),
                })
                _resume_sig = get_resume_signal(ctx.run_id)
                _abort_pause_sig = get_abort_during_pause_signal(ctx.run_id)
                _chat_abort_ev = await _get_abort_event(ctx.chat_id) if ctx.chat_id else None
                _wait_tasks = [
                    spawn(_resume_sig.wait()),
                    spawn(_abort_pause_sig.wait()),
                ]
                if _chat_abort_ev is not None:
                    _wait_tasks.append(asyncio.create_task(_chat_abort_ev.wait()))
                _done_wait, _pending_wait = await asyncio.wait(
                    _wait_tasks, return_when=asyncio.FIRST_COMPLETED,
                )
                for _tw in _pending_wait:
                    _tw.cancel()
                if _pending_wait:
                    await asyncio.gather(*_pending_wait, return_exceptions=True)
                # FIX: consume the resume signal — otherwise a second
                # pause of the same run would wake up immediately (event stays set).
                _resume_sig.clear()
                if ctx.chat_id:
                    _clear_pause_state(ctx.chat_id)
                _aborted_during_pause = (
                    _abort_pause_sig.is_set()
                    or (_chat_abort_ev is not None and _chat_abort_ev.is_set())
                )
                if _aborted_during_pause:
                    _remaining_items_ap = [
                        {"title": str(t)} for t in _loop_items[_di:]
                    ]
                    if ctx.chat_id and _remaining_items_ap:
                        _write_resume_block(
                            chat_id=ctx.chat_id,
                            workspace=_ws_str,
                            chunks_total=_n_items,
                            chunks_done=_done_tasks,
                            chunks_remaining=_remaining_items_ap,
                            written_files=_written_files,
                            last_summary=" | ".join(_done_tasks),
                            plan_msgs=[],
                            explore_ctx=_explore_ctx,
                            halt_reason="user_abort",
                            pending_send_files=list(_pending_send_files),
                        )
                    yield await ctx.emit({
                        "type": "status",
                        "content": (
                            f"⏹ Aborted during pause: {len(_done_tasks)}/{_n_items} chunks done. "
                            f"Resume saved."
                        ),
                    })
                    break
                yield await ctx.emit({
                    "type": "run_resumed_manual",
                    "run_id": ctx.run_id,
                    "chunks_done": len(_done_tasks),
                    "chunks_remaining": _n_items - _di,
                })

            yield await ctx.emit({"type":"duo_round","n":_di+1,"total":_n_items,"subtask":_subtask or None})

            # ── SKIP-CHECK: Skip button skips current phase (not the whole run) ──
            # In the Duo-Loop, skip means "skip this chunk/round and move to next".
            # Auto-consumed: ctx.clear_step_skip() resets so next skip requires another press.
            if ctx.step_skipped() and not ctx.aborted():
                ctx.clear_step_skip()
                # SKIP-GUARD (2026-09-04): A single non-chunk round must not be
                # silently skipped - that would end the run with "Coder 0s"
                # (observed live: skip landed during the planner). Skip only
                # applies in the chunking/multi-round case.
                if not ctx.duo_config.chunking and not _subtask and _n_items <= 1:
                    logger.warning("[SKIP-GUARD] single-round skip ignored - starting coder (subtask=%s)", _subtask)
                    yield await ctx.emit({"type": "status",
                        "content": "⏭ Skip ignored — starting the coder now."})
                else:
                    if _subtask:
                        _cs.mark_chunk_done(_subtask)
                        yield await ctx.emit({"type":"status",
                            "content": f"⏭ Chunk {_di+1} skipped: {_subtask[:50]}"})
                    else:
                        yield await ctx.emit({"type":"status",
                            "content": f"⏭ Round {_di+1} skipped"})
                    _ac_out = await _auto_commit_chunk(ctx.user_input, _subtask, _ws_str,
                                                       ctx.duo_config.git_autocommit,
                                                       files=list(_cs.written_files[-20:]),
                                                       commit_allowed=getattr(ctx, "workspace_commit_allowed", True))
                    if _ac_out:
                        yield await ctx.emit({"type": "status", "content": _ac_out})
                    _di += 1
                    # CHUNK-PERSIST (2026-10-11): write the resume block after every
                    # done chunk - a hard kill (power, crash, taskkill) previously lost
                    # all in-memory chunk progress and the resumed run repeated chunks.
                    _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, "chunk_checkpoint")

                    _cs.reset_test_retries()
                    if _subtask:
                        yield await ctx.emit({"type": "status",
                            "content": f"✅ Chunk {_di}/{_n_items} done — {len(_written_files)} files changed"})
                    continue

            # SPLIT 2.4 (2026-11-05): coder input build + steer drain + read-only
            # header + session budgeting + message assembly + vision attach moved to
            # core/duo_round_input._duo_coder_round_input. An empty chunk sets
            # `_ri_ps.skip_round` and the runner does the continue (loop control).
            _ri_ps = _DuoRoundInputState(
                _di=_di,
                _n_items=_n_items,
                _subtask=_subtask,
                _subtasks=_subtasks,
                _done_tasks=_done_tasks,
                _cs=_cs,
                _written_files=_written_files,
                critic_issues=critic_issues,
                coder_out=coder_out,
                _explore_ctx=_explore_ctx,
                _img_plan=_img_plan,
                _plan_tracker=_plan_tracker,
                _coder_caps=_coder_caps,
                _touched_block_persisted=_touched_block_persisted,
                _coder_port=_coder_port,
                carry={
                    "_coder_map_budget": _coder_map_budget,
                    "coder_mdl": coder_mdl,
                    "exec_mdl": exec_mdl,
                    "_cached_coder_port": _cached_coder_port,
                    "_duo_coder_sys": _duo_coder_sys,
                    "_duo_coder_temp": _duo_coder_temp,
                    "_duo_coder_tok": _duo_coder_tok,
                    "_coder_dyn_hints": _coder_dyn_hints,
                    "_matched_skills": _matched_skills,
                    "_plan_thinking": _plan_thinking,
                    "_plan_result": _plan_result,
                    "_inloop_plan_text": _inloop_plan_text,
                    "_plan_content_txt": _plan_content_txt,
                    "_explore_has_contents": _explore_has_contents,
                    "_final_verdict": final_verdict,
                },
            )
            async for _ev in _duo_coder_round_input(ctx, _ri_ps):
                yield _ev
            if _ri_ps.skip_round:
                continue
            _coder_input = _ri_ps._coder_input
            _coder_msgs = _ri_ps._coder_msgs
            _read_only_task_request = _ri_ps._read_only_task_request
            _coder_skipped_images = _ri_ps._coder_skipped_images
            _coder_imgs_off = _ri_ps._coder_imgs_off
            _img_plan = _ri_ps._img_plan
            _touched_block_persisted = _ri_ps._touched_block_persisted
            _coder_map_budget = _ri_ps.carry["_coder_map_budget"]
            _parts = _ri_ps._parts
            _t = _ri_ps._t
            _tool_round_runtime_error = False
            _tool_round_error_text = ""

            _file_written_prev = bool(_written_files)
            _is_first_outer_round = (_di == 0 and not _subtasks) or (_subtasks and _di == 0 and not _done_tasks)
            _chunking_active = bool(_subtasks)
            _tool_round_base = (
                (_chunking_active and ctx.duo_config.tool_rounds > 0)
                or (not _chunking_active and _di < ctx.duo_config.tool_rounds)
                or (bool(_pre_explore_msgs) and _is_first_outer_round)
            )
            _auto_tool_promote_hit = bool(_coder_tc and _file_written_prev and not _tool_round_base)
            _auto_tool_promote_allowed = (
                _auto_tool_promote_cap == 0
                or _auto_tool_promote_streak < _auto_tool_promote_cap
            )
            _auto_tool_promote_active = _auto_tool_promote_hit and _auto_tool_promote_allowed
            if _auto_tool_promote_hit and not _auto_tool_promote_allowed and not _auto_tool_promote_notified:
                _auto_tool_promote_notified = True
                yield await ctx.emit({
                    "type": "status",
                        "content": (
                            f"⏹ Auto tool promotion capped ({_auto_tool_promote_cap} round(s)). "
                            "Further steps run without the tool loop."
                        ),
                })
            _is_tool_round = (_tool_round_base or _auto_tool_promote_active) and _coder_tc
            if _auto_tool_promote_active:
                _auto_tool_promote_streak += 1
            elif _tool_round_base:
                _auto_tool_promote_streak = 0
            if _read_only_task_request and _explore_ctx:
                _is_tool_round = False
                _auto_tool_promote_streak = 0
                yield await ctx.emit({
                    "type": "status",
                    "content": "\U0001f512 Read-only request detected: using explore context without tool calls.",
                })
                # Last user message wins over any system "must call a tool" text.
                try:
                    _coder_msgs = _coder_msgs + [
                        {"role": "user", "content": _READONLY_CODER_NOTE}
                    ]
                except Exception:
                    pass
            _loop_detected = False
            if _is_tool_round:
                _tm_what = (_subtask[:52] + '…') if _subtask and len(_subtask) > 52 else (_subtask or 'Writing code')
                _tm_of   = f'/{_n_items}' if _n_items > 1 else ''
                yield await ctx.emit({"type": "status",
                                  "content": f"\U0001f527 Chunk {_di+1}{_tm_of}: {_tm_what} — tool mode ({coder_mdl.split(chr(58))[0]})"})
                # OUTSOURCED (split 2.5): dtool payload budget + pass-files
                # bridge + read-guard + goal-pin → _duo_dtool_setup().
                _ds_ps = _DuoDtoolSetupState(
                    _di=_di,
                    _coder_caps=_coder_caps,
                    _coder_msgs=_coder_msgs,
                    _explore_ctx=_explore_ctx,
                    _pre_explore_msgs=_pre_explore_msgs,
                    _contracts_raw=_contracts_raw,
                    _touched_paths=_touched_paths,
                    _tree_ctx=_tree_ctx,
                    _is_first_outer_round=_is_first_outer_round,
                    _ws_str=_ws_str,
                    _touched_block_persisted=_touched_block_persisted,
                    carry={
                        "coder_mdl": coder_mdl,
                        "_duo_coder_sys": _duo_coder_sys,
                        "_duo_coder_tok": _duo_coder_tok,
                        "_duo_runtime_profile": _duo_runtime_profile,
                        "_coder_ctx_eff": _coder_ctx_eff,
                        "_coder_map_budget": _coder_map_budget,
                        "_plan_result": _plan_result,
                    },
                )
                async for _ev in _duo_dtool_setup(ctx, _ds_ps):
                    yield _ev
                _dtool_msgs = _ds_ps._dtool_msgs
                _dtool_ctx = _ds_ps._dtool_ctx
                _dtool_opts = _ds_ps._dtool_opts
                _dtool_sys_eff = _ds_ps._dtool_sys_eff
                _tool_read_timeout_s = _ds_ps._tool_read_timeout_s
                _goal_summary = _ds_ps._goal_summary
                _goal_pin_msg = _ds_ps._goal_pin_msg
                _touched_block_persisted = _ds_ps._touched_block_persisted
                _coder_map_budget = _ds_ps.carry["_coder_map_budget"]

                # OUTSOURCED (split 2.6): thinking gate + inloop planner +
                # plan-tracker rebuild + plan-for-coder injection.
                _ip_ps = _DuoInloopPlannerState(
                    _dtool_msgs=_dtool_msgs,
                    _dtool_ctx=_dtool_ctx,
                    _coder_input=_coder_input,
                    _explore_ctx=_explore_ctx,
                    _plan_tracker=_plan_tracker,
                    _is_first_outer_round=_is_first_outer_round,
                    _goal_summary=_goal_summary,
                    _duo_timed_out=_duo_timed_out,
                    carry={
                        "exec_mdl": exec_mdl,
                        "coder_mdl": coder_mdl,
                        "_planner_step_cap": _planner_step_cap,
                        "_exec_has_thinking": _exec_has_thinking,
                        "_fup_task_ctx": _fup_task_ctx,
                        "_has_plan": _has_plan,
                        "_duo_deadline_at": _duo_deadline_at,
                        "_duo_run_timeout_s": _duo_run_timeout_s,
                    },
                )
                async for _ev in _duo_inloop_planner(ctx, _ip_ps):
                    yield _ev
                _dtool_msgs = _ip_ps._dtool_msgs
                _run_thinking_now = _ip_ps._run_thinking_now
                _inloop_plan_text = _ip_ps._inloop_plan_text
                _duo_timed_out = _ip_ps._duo_timed_out
                # PLAN-TO-EXEC DEBUG: Log state after planning step to diagnose early-exit.
                logger.warning(
                    "[PLAN-TO-EXEC] _is_tool_round=%s ctx.aborted=%s _dtool_msgs_len=%d "
                    "_run_thinking_now=%s exec_mdl=%s",
                    _is_tool_round, ctx.aborted(), len(_dtool_msgs),
                    _run_thinking_now, exec_mdl,
                )

                try:
                    from core.run_audit import audit_plan_payload_facts
                    audit_plan_payload_facts(
                        ctx.chat_id, ctx.run_id,
                        dtool_msgs=_dtool_msgs,
                        has_plan_flag=_has_plan,
                        is_bridge=bool(_pre_explore_msgs),
                        pre_explore_msgs_count=len(_pre_explore_msgs or []),
                        explore_ctx_len=len(_explore_ctx or ""),
                        plan_content_len=len(_plan_result.plan_content or "") if _plan_result is not None else 0,
                        thinking_len=len(_plan_thinking or ""),
                        chunking=bool(ctx.duo_config.chunking),
                        is_first_outer_round=_is_first_outer_round,
                    )
                except Exception:
                    pass

                if _is_tool_round and _di == 0 and not critic_issues:
                    _test_hint = _build_test_hint(_ws_str)
                    _hint_content = (
                        f"IMPORTANT: After writing or patching all files, verify the result.\n"
                        f"{_test_hint}\n"
                        "Do NOT finish without at least one run_bash verification call."
                    )
                    if _dtool_msgs and _dtool_msgs[-1].get("role") == "user":
                        _dtool_msgs[-1]["content"] = str(_dtool_msgs[-1].get("content", "")).rstrip() + "\n\n" + _hint_content
                    else:
                        _dtool_msgs.append({"role": "user", "content": _hint_content})
                # CRITIC-INJECT (double-user-turn FIX)
                if _di > 0 and critic_issues and not _subtask:
                    _issues_block = "\n".join(f"{i+1}. {iss}" for i, iss in enumerate(critic_issues))
                    _critic_inject_content = (
                        f"The code reviewer found these specific issues — fix ALL of them:\n"
                        f"{_issues_block}\n\n"
                        f"Use the tools to read the current files, apply the fixes with "
                        f"edit_file, then run tests to confirm. Do not ask — just fix."
                    )
                    if _dtool_msgs and _dtool_msgs[-1].get("role") == "user":
                        _dtool_msgs[-1]["content"] = (
                            str(_dtool_msgs[-1].get("content", "")).rstrip()
                            + "\n\n" + _critic_inject_content
                        )
                    else:
                        _dtool_msgs.append({"role": "user", "content": _critic_inject_content})
                # UNBOUND FIX: initialize _dtc_owned/_dtc before try —
                _dtc_owned = False
                _dtc = None
                _dport = None  # FIX: prevent UnboundLocalError if load fails in else branch
                _duo_loop: "AgenticToolLoop | None" = None
                try:
                    from backend.llama_server_manager import manager as _lsm3
                    _port_ps = _DuoCoderPortCacheState(
                        dport=_dport, cached_coder_port=_cached_coder_port,
                        cached_coder_port_ctx=_cached_coder_port_ctx,
                    )
                    try:
                        async for _ev in _duo_resolve_coder_port_cache(
                            ctx, _port_ps, exec_mdl=exec_mdl,
                            _dtool_opts=_dtool_opts, _img_plan=_img_plan,
                            _always_vision=_always_vision, _lsm3=_lsm3,
                        ):
                            yield _ev
                    finally:
                        _dport = _port_ps.dport
                        _cached_coder_port = _port_ps.cached_coder_port
                        _cached_coder_port_ctx = _port_ps.cached_coder_port_ctx
                    if _dport is None:
                        # MODEL-LOAD-FAIL (2026-10-04, live log): a coder the
                        # GPU cannot host is a resource error, not a loop -
                        # loop_detected mislabeled it and the AUTO-STOP
                        # diagnose printed misleading counters.
                        _model_load_failed = True
                        yield await ctx.emit({"type": "status",
                            "content": f"⛔ Coder model {exec_mdl} could not be loaded "
                                       f"(VRAM/ctx). Lower the coder context in the agent "
                                       f"tab, pick a smaller model, or free VRAM - run stopped."})
                        break
                    _slotctx_ps = _DuoSlotCtxState(
                        dtool_ctx=_dtool_ctx,
                        cached_coder_port_ctx=_cached_coder_port_ctx,
                    )
                    await _duo_query_actual_slot_ctx(
                        _slotctx_ps, _lsm3=_lsm3, _dport=_dport,
                        _dtool_opts=_dtool_opts, _duo_coder_tok=_duo_coder_tok,
                    )
                    _dtool_ctx = _slotctx_ps.dtool_ctx
                    _cached_coder_port_ctx = _slotctx_ps.cached_coder_port_ctx
                    _actual_ctx = _slotctx_ps.actual_ctx
                    # CTX-FLOOR (2026-10-31, audit): the next chunk rebuilt
                    # _dtool_opts from the requested ctx and killed the warm
                    # slot ("ctx mismatch"). Pin the effective coder ctx to
                    # what the slot actually runs; the UI value stays the
                    # target for the next load.
                    _pinned_ctx = _clamp_coder_ctx_to_slot(_coder_ctx_eff, _actual_ctx)
                    if _pinned_ctx != _coder_ctx_eff:
                        _coder_ctx_override = _pinned_ctx
                        _coder_ctx_eff = _pinned_ctx
                        logger.warning(
                            "[CTX-DOWNSHIFT] coder ctx pinned to the actual slot "
                            "ctx %d for the rest of the run", _pinned_ctx)
                    _slot_degraded = _slotctx_ps.slot_degraded
                    _slot_ctx_floor_stop = _slotctx_ps.slot_ctx_floor_stop
                    _dtool_ctx_requested = _slotctx_ps.dtool_ctx_requested
                    _dtc_owned = getattr(getattr(ctx.pipeline, 'ollama', None), '_client', None) is None
                    _dtc = getattr(getattr(ctx.pipeline, 'ollama', None), '_client', None) or httpx.AsyncClient(
                        limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                        timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=5.0),
                    )
                    _stored_threshold = int(ctx.settings.get("duo_compress_threshold", 0))
                    # TOOLCALL-SANITIZE high-water mark (2026-11-07 audit):
                    # the history is append-only between compressions, so only
                    # messages from here on need the JSON scan. Reset to 0 when
                    # compression replaces _dtool_msgs.
                    _sanitize_upto = 0
                    # CACHE-FRIENDLY + DYNAMIC OUTPUT RESERVE (2026-09-05):
                    # effective threshold = auto_floor*ctx (P1). The previous
                    # P2 deduction (ctx - max_tokens - reserve) is gone because
                    # every tool round clamps its output budget per request to
                    # the free space (clamp_request_max_tokens) -> prompt+output
                    # <= ctx is guaranteed even for large max_tokens. So it
                    # compresses at ~78% (instead of ~67% at max_tokens 12000).
                    # A UI override (>0) still sets the threshold exactly.
                    # FLOOR 0.70 (2026-09-08, live-log evidence): the HTTP-400
                    # overflow zone starts at ~72-73% of ctx (prompt + max_tokens
                    # + template overhead) — a 0.78 threshold sat ABOVE it, so
                    # every cycle went through the expensive force path
                    # (6 compressions in 50min on 2026-09-07) instead of a
                    # planned threshold compression. 0.70 fires clearly before
                    # the 400 zone; ctx_guard's module default is 0.72.
                    _duo_compress_floor = float(ctx.settings.get("duo_compress_auto_floor", 0.70) or 0.70)
                    _duo_out_budget = int(_dtool_opts.get("num_predict", 1024) or 1024)
                    # Reserve cap only as a small overflow backstop (P1 is meant to bind).
                    _duo_reserve_cap = max(1, min(_duo_out_budget, 4096))
                    _compress_threshold = _resolve_compress_threshold(
                        ctx_tokens=_dtool_ctx,
                        num_predict=_duo_reserve_cap,
                        ui_threshold=_stored_threshold,
                        auto_floor=_duo_compress_floor,
                        overflow_reserve=int(ctx.settings.get("duo_compress_overflow_reserve", 1024) or 1024),
                    )

                    _partial_compression = bool(ctx.settings.get("duo_partial_compression", False))
                    _comp_llm_cfg = str(ctx.settings.get("duo_compress_model") or "").strip()
                    if _comp_llm_cfg.lower() == "auto":
                        # Auto (Coder): the executor model summarizes — no
                        # separate light-model load (default path below).
                        _comp_llm_cfg = ""
                    _comp_llm_timeout_s = int(ctx.settings.get("duo_compress_llm_timeout_s", 180) or 180)
                    _compress_local_only = bool(ctx.settings.get("duo_compress_local_only", False))
                    _budg_ps = _DuoToolBudgetsState(
                        xtools_ws=_xtools_ws,
                        compress_threshold=_compress_threshold,
                    )
                    await _duo_resolve_tool_budgets(
                        ctx, state, _budg_ps, exec_mdl=exec_mdl,
                        _dtool_ctx=_dtool_ctx, _duo_out_budget=_duo_out_budget,
                        _duo_ws=_duo_ws, _stored_threshold=_stored_threshold,
                        duo_rounds=duo_rounds,
                        _duo_runtime_profile=_duo_runtime_profile,
                        _accumulated_replan_bonus=_accumulated_replan_bonus,
                        _cached_coder_port=_cached_coder_port,
                    )
                    _xtools_ws = _budg_ps.xtools_ws
                    _compress_threshold = _budg_ps.compress_threshold
                    _max_tool_rounds = _budg_ps.max_tool_rounds
                    _max_tool_rounds_cfg = _budg_ps.max_tool_rounds_cfg
                    _loop_detected = False
                    _chunk_budget_exhausted = False
                    _partial_escalated = False  # partial no-shrink -> next attempt runs full
                    # PER-CHUNK-BUDGET: rounds spent in earlier chunks must not
                    # shrink this chunk's budget (compression resets at :3830
                    # stay; lifetime totals live in _lifetime_tool_rounds).
                    _total_tool_rounds = 0
                    _force_compress_next = False
                    _compress_fail_streak = 0
                    _duo_state = DuoRoundState(think_runtime=_coder_tool_think, exec_model=exec_mdl,
                                               dtool_opts=_dtool_opts, tool_read_timeout_s=_tool_read_timeout_s,
                                               cached_port=_cached_coder_port, current_port=_dport)
                    _dr_think_only_retries = 0
                    _zero_activity_nudged = False
                    # NO-THINK-RETRY (2026-09-10): after repeated think-only
                    # rounds, disable template thinking for the next attempt
                    # (chat_template_kwargs enable_thinking=false) so the token
                    # budget goes to an actual tool call (live: spark-1.7b
                    # burned every round on reasoning, never called a tool).
                    _coder_no_think = False
                    _dr_invalid_tool_retries = 0     # tracks rounds where ALL tool-calls were rejected
                    _dr_dropped_tool_retries = 0     # DROPPED-FIX: rounds where tool-calls had malformed JSON args
                    _limit_warned = False  # round-limit warning: inject only once
                    _replan_bonus_granted = False  # extra rounds: grant once per run
                    _ctx_full_skips = 0  # consecutive min-viable-budget skips (stop guard)
                    _last_force_compress_round = -10  # STORM-GUARD (2026-10-30): last CTX-FULL force round
                    _replan_count = 0              # hard cap: max replans per run
                    _grace_round_active = False  # budget exhaustion grace round
                    _grace_round_used   = False  # hard exit if model ignores grace prompt
                    _verify_warned      = False  # only inject verify warning once per run
                    _swa_triggered_compress = False  # only fire proactive SWA compress once per run
                    _read_counts: dict[str, int] = {}   # file → consecutive reads without edit
                    _last_write_round: int = 0          # last tool-round that had write/edit/patch
                    _explore_only_rounds: int = 0       # consecutive rounds with only explore tools
                    _no_write_rounds: int = 0           # consecutive rounds WITHOUT any file write (diagnose-flailing guard)
                    _round_wrote_file: bool = False     # did the current round successfully write/edit a file?
                    _no_write_nudged: bool = False      # nudge already injected once for this streak
                    _seen_explore_paths: set = set()
                    _total_edit_lines: int = 0
                    _ctx_pressure_warned = False
                    _ctx_critical_warned = False
                    # CACHE-FRIENDLY (2026-09-04): Cap from settings, much
                    # higher than the old 6, so long 32k+ runs don't tip into
                    # the no-reset mode (which now only knows in-place churn).
                    _MAX_COMPRESSIONS = max(1, int(ctx.settings.get("duo_max_compressions", 40) or 40))
                    # Compression counters for telemetry + rule-based
                    # fallback (LLM fail -> 2x -> rule-based).
                    _llm_compress_fails = 0
                    _rule_compress_fails = 0
                    _rule_compress_used = False
                    _call_sigs: list = []  # loop detection: incremental signatures
                    _last_too_large_path: str = ""
                    _attempts_per_file: dict = {}
                    # WEDGE-HANDOFF (2026-09-17): per-chunk detection state,
                    # passed as the same object into every ToolRoundState.
                    # None = feature off (executor skips all wedge accounting).
                    _wedge_state = (
                        _wedge_new_state(threshold=int(ctx.settings.get("duo_wedge_handoff_streak_threshold", 4) or 4))
                        if bool(ctx.settings.get("duo_wedge_handoff_enabled", False)) else None
                    )
                    _tool_error_retries: dict = {}
                    _tool_ctx_lru = ToolContextLRU(default_ttl=int(ctx.settings.get("duo_tool_output_ttl", 3) or 3))
                    # P2-7 FIX: Use module-level keyword tuple + reuse _read_only_task_request
                    _read_only_task = _read_only_task_request
                    _tool_mode = resolve_coder_tool_mode(
                        ctx.settings, read_only=_read_only_task)
                    _active_tools = _filter_tools_for_mode(
                        _xtools_ws,
                        mode=_tool_mode,
                        include_websearch=_duo_ws,
                    )
                    if _read_only_task:
                        yield await ctx.emit({
                            "type": "status",
                            "content": "\U0001f512 Read-only mode active: only read/research tools allowed.",
                        })
                    # chat_template_kwargs.enable_thinking im Tool-Payload unten.
                    # Set run_id ContextVar for ask_user tool handler
                    from tools.runner import _current_run_id, _pause_timeout_s, _web_search_count, _install_count
                    # ASK-USER-RUN-ID-FALLBACK (2026-09-02): agentic/workspace runs
                    # have chat_id=None (RUN-ENTRY logs chat_id=None) yet carry a
                    # real run_id that the UI uses for pause/resume. Keying the
                    # ask_user pause on chat_id alone left run_id=None -> no pause
                    # event under the UI's run_id -> /resume returned 404.
                    # APPROVAL-SCOPE FIX (2026-09-20): run_id FIRST. The UI polls
                    # /approval/pending with the run_id from the run_id event
                    # (the registered id, e.g. 1789...-hex); keying approvals on
                    # a persisted chat_id made the poll miss the pending entry,
                    # wipe the staged card and park the run forever (live:
                    # edit_file approval pending 15+ min, no card, "Processing
                    # context 941s").
                    _run_id_global = ctx.run_id or ctx.chat_id
                    _current_run_id.set(_run_id_global)
                    _pause_timeout_s.set(ctx.duo_config.pause_timeout_s)
                    _web_search_count.set([0])
                    # FETCH-BUDGET (2026-10-14, phase 7): own pool for web_fetch.
                    from tools.runner import _web_fetch_count as _wf_count
                    _wf_count.set([0])
                    _install_count.set([0])
                    from infra.ask_user_governor import configure_run as _configure_governor
                    _configure_governor(_run_id_global, until_finished=ctx.duo_config.until_finished, settings=ctx.settings)
                    # ── Build hooks for shared tool executor ──
                    async def _stuck_handler(tn, ta, tr):
                        # 1.4.x: the body lives in core/duo/preflight.py. Scalars come
                        # back as a tuple (a closure cannot rebind them from outside);
                        # _read_counts is the same dict, so its in-place edits stand.
                        nonlocal _loop_detected, _last_write_round, _round_wrote_file
                        _handled, _ld, _lwr, _rwf = await stuck_handler(
                            tn, ta, tr,
                            ctx=ctx, ws_str=_ws_str,
                            total_tool_rounds=_total_tool_rounds,
                            read_counts=_read_counts,
                            last_write_round=_last_write_round,
                            round_wrote_file=_round_wrote_file,
                            ld_setter=_ld_setter,
                        )
                        _loop_detected, _last_write_round, _round_wrote_file = _ld, _lwr, _rwf
                        return _handled
                    # ── Coder live streaming (2026-08-18) ──────────────────────
                    # never blocks (queue never fills up). Pattern like the planner bridge.
                    _coder_event_q: asyncio.Queue = asyncio.Queue(maxsize=20)
                    _coder_real_prompt_tokens: list = [0]
                    # est of the messages that produced the last real value
                    # (calibration basis for the chars/4 estimate)
                    _coder_est_at_real: list = [0]
                    # len(dtool_msgs) when the last real value was captured —
                    # everything appended after that POST is not in the server
                    # value (DELTA-ON-REAL, audit phase 2, 2026-10-14).
                    _coder_real_at_msgs_len: list = [0]
                    _coder_real_cached_tokens: list = [0]
                    _prev_msg_sig: list = [None]

                    async def _coder_emit_fn(event: dict) -> str:
                        if isinstance(event, dict) and event.get("type") == "usage_meta" and event.get("prompt_tokens"):
                            _coder_real_prompt_tokens[0] = int(event["prompt_tokens"])
                            _coder_est_at_real[0] = _estimate_ctx_tokens(_dtool_msgs)
                            _coder_real_at_msgs_len[0] = len(_dtool_msgs)
                            _coder_real_cached_tokens[0] = int(event.get("cached_tokens") or 0)
                        _sse = await ctx.emit(event)
                        try:
                            _coder_event_q.put_nowait(_sse)
                        except asyncio.QueueFull:
                            pass
                        return _sse

    # WORKING SET (audit 2026-10-24): the run's focus files (last edits/
    # reads) are never masked — evicting the file the model is editing
    # forces memory-based edits. Non-focus history pays instead.
                    _tool_exec_hooks = ToolExecHooks(
                        emit=_coder_emit_fn,
                        is_aborted=lambda cid: bool(ctx.is_aborted_chat(cid)),
                        on_tool_result=_stuck_handler,
                        remember_insight=ctx.memory.remember_repo_insight,
                        evict_model=partial(evict_model_handler, ctx),
                    )
                    _round_budget0 = _max_tool_rounds
                    # +2 slots: the grace round plus one nudge-retry round; the
                    # extra slots stay gated on _grace_round_active at the top.
                    for _dr in range(_max_tool_rounds + 2):
                        _round_t0 = time.monotonic()
                        if ctx.aborted() or ctx.is_aborted_chat(ctx.chat_id) or _loop_detected:
                            break
                        if _dr >= _round_budget0 and not _grace_round_active:
                            break
                        if time.monotonic() >= _duo_deadline_at:
                            _duo_timed_out = True
                            _ld_setter(2677); _loop_detected = True
                            yield await ctx.emit({
                                "type": "status",
                                "content": "⏱ Tool loop ended due to run timeout.",
                            })
                            break
                        # MID-RUN STEER (tool-round boundary): messages queued
                        # while THIS chunk runs land in the very next round as
                        # user messages — same channel as the zero-activity
                        # nudge below.
                        _round_steer = drain_steer_messages(ctx.run_id)
                        if (any(_st["images"] for _st in _round_steer)
                                and not bool(ctx.settings.get("duo_coder_images_off", False))
                                and str(ctx.settings.get("duo_image_mode") or "") != "preprocess"):
                            # STEER-IMAGE UPGRADE (2026-10-06, live T2): see
                            # the chunk-boundary drain — upgrade the coder
                            # slot before the image parts reach the POST.
                            try:
                                from backend.llama_server_manager import manager as _mgr_vu2
                                yield await ctx.emit({"type": "status", "content":
                                    "🖼 image steer received — loading vision support (~15 s)…"})
                                await _mgr_vu2.upgrade_port_to_vision(int(_dport))
                            except (RuntimeError, OSError, httpx.HTTPError) as _vu_err2:
                                logger.warning("[STEER-IMAGE] vision upgrade failed: %s", _vu_err2)
                        for _st in _round_steer:
                            yield await ctx.emit({"type": "steer",
                                                  "content": str(_st["text"] or ""),
                                                  "images": len(_st["images"] or [])})
                        if _round_steer:
                            for _st in _round_steer:
                                # AUDIT (2026-10-08): same drop rule as the
                                # chunk-boundary drain - images_off and
                                # preprocess mode keep the coder text-only,
                                # raw image parts would hit the API without
                                # a projector.
                                if _st["images"] and (
                                        bool(ctx.settings.get("duo_coder_images_off", False))
                                        or str(ctx.settings.get("duo_image_mode") or "") == "preprocess"):
                                    yield await ctx.emit({"type": "status", "content":
                                        "⚠ Image steer dropped - the coder stays text-only (raw mode + images allowed)."})
                                    continue
                                _dtool_msgs.append(
                                    _build_steer_user_message(_st["text"], _st["images"]))
                        from tools.runner import _ask_user_gate, _ask_user_throttled_count
                        if ctx.duo_config.until_finished:
                            if _cs.test_retries >= _cs.max_test_retries:
                                _ask_user_gate.set("open")
                                _ask_user_throttled_count.set(0)
                            else:
                                _ask_user_gate.set("throttled_autonomous")
                        else:
                            _ask_user_gate.set("open")
                            _ask_user_throttled_count.set(0)
                        # W4a (2026-11-07): health check + repo-map pin +
                        # calibrated guard tokens + GUARD C live in
                        # core/duo_loop_guards.
                        _lg_ps = _DuoLoopGuardsState(
                            exec_mdl=exec_mdl, actual_ctx=_actual_ctx,
                            dtool_ctx_requested=_dtool_ctx_requested,
                            slot_degraded=_slot_degraded,
                            total_tool_rounds=_total_tool_rounds,
                            dtool_msgs=_dtool_msgs, dtool_opts=_dtool_opts,
                            coder_real_prompt_tokens=_coder_real_prompt_tokens,
                            coder_est_at_real=_coder_est_at_real,
                            coder_real_at_msgs_len=_coder_real_at_msgs_len,
                            dport=_dport,
                            cached_coder_port=_cached_coder_port,
                            cached_coder_port_ctx=_cached_coder_port_ctx,
                            est_tokens=_est_tokens,
                            guard_tokens=_guard_tokens,
                            loop_detected=_loop_detected,
                        )
                        async for _ev in _duo_loop_guards(ctx, _lg_ps):
                            yield _ev
                        _dport = _lg_ps.dport
                        _cached_coder_port = _lg_ps.cached_coder_port
                        _cached_coder_port_ctx = _lg_ps.cached_coder_port_ctx
                        _est_tokens = _lg_ps.est_tokens
                        _guard_tokens = _lg_ps.guard_tokens
                        _loop_detected = _lg_ps.loop_detected
                        if _lg_ps.action == "break":
                            break
                        _ctx_peak_tokens = max(_ctx_peak_tokens, int(_est_tokens))
                        _ctx_limit_seen = max(_ctx_limit_seen, int(_dtool_ctx))
                        if _dtool_ctx > 0:
                            _ctx_pressure_peak = max(_ctx_pressure_peak, float(_est_tokens) / float(_dtool_ctx))
                            # SWA reprefill warning for sliding-window models (Qwen3.6)
                            _swa_win = 0
                            try:
                                from backend.llama_server_manager import manager as _lsm_swa
                                for _s in _lsm_swa._slots:
                                    if _s.port == _dport and _s.swa_window:
                                        _swa_win = _s.swa_window
                                        break
                            except Exception:
                                pass
                            _swa_warn = bool(_swa_win and _est_tokens > _swa_win * 0.85)
                            yield await ctx.emit({
                                "type": "ctx_meter",
                                "est_tokens": int(_coder_real_prompt_tokens[0] or _est_tokens),
                                "real_tokens": int(_coder_real_prompt_tokens[0]),
                                "ctx_limit": int(_dtool_ctx),
                                "cached_tokens": int(_coder_real_cached_tokens[0]),
                                "compressing": False,
                                "swa_window": _swa_win,
                                "swa_warning": _swa_warn,
                            })
                            if _swa_warn:
                                yield await ctx.emit({
                                    "type": "system_warning",
                                    "code": "swa_reprefill_zone",
                                    "message": f"Context ({int(_est_tokens)} tok) approaching SWA window ({_swa_win} tok) — reprefill slowdown expected",
                                })
                                if not _swa_triggered_compress:
                                    _swa_triggered_compress = True
                                    if _ctx_compressions < _MAX_COMPRESSIONS:
                                        _force_compress_next = True
                        _ctx_pct = int((_guard_tokens / _dtool_ctx) * 100) if _dtool_ctx > 0 else 0
                        if _ctx_pct >= 85 and not _ctx_critical_warned:
                            # to write to messages[0]. Proof: [MSGSIG-CHANGE] round=23
                            # (system 8619→8758) → [CACHE-MISS] prompt=40302 cached=0 →
                            # few new tokens (cache reuse survives).
                            _dtool_msgs.append({
                                "role": "user",
                                "content": (
                                    "[RUNTIME NOTICE] "
                                    f"[CTX CRITICAL: ~{_ctx_pct}% full] Stop reading new files. "
                                    "Complete current edits and call task_complete."
                                ),
                            })
                            _ctx_critical_warned = True
                        elif _ctx_pct >= 60 and not _ctx_pressure_warned:
                            _dtool_msgs.append({
                                "role": "user",
                                "content": (
                                    "[RUNTIME NOTICE] "
                                    f"[CTX: ~{_ctx_pct}% full] Avoid large reads. "
                                    "Prefer targeted read_file(start_line/end_line) "
                                    "and search_code over full-file reads."
                                ),
                            })
                            _ctx_pressure_warned = True
                        # CACHE-FRIENDLY (2026-09-04): compression is the
                        # primary shrink. In-place semantic eviction is ONLY
                        # the emergency valve when no compression is left
                        # (cap reached) AND the context is >90% full. Between
                        # two compressions the history stays append-only ->
                        # the llama.cpp prefix cache stays alive and rounds
                        # are fast (tail prefill only).
                        _can_compress = (_MAX_COMPRESSIONS != -1 or _force_compress_next)
                        # LEGACY-REMOVAL (2026-09-09): the in-place-eviction A/B branch
                        # (duo_cache_friendly_ctx=False) is gone; compression-first is the
                        # only regime.
                        _guard_decision = _decide_context_action(
                            guard_tokens=int(_guard_tokens),
                            ctx_tokens=int(_dtool_ctx),
                            threshold=int(_compress_threshold),
                            can_compress=_can_compress,
                            force_compress=bool(_force_compress_next),
                        )
                        _compress_ok = (_guard_decision.action == "compress")
                        # First-round protection: the initial context
                        # (explore/static map) may only be compressed via
                        # threshold after >=1 tool round (emergency >90%
                        # still OK).
                        if _compress_ok and _guard_decision.reason == "threshold" and _total_tool_rounds <= 0:
                            _compress_ok = False
                        if not _compress_ok and _guard_decision.action == "emergency_evict":
                            _est_before_evict = int(_est_tokens)
                            _evicted_n = _evict_stale_tool_outputs(
                                messages=_dtool_msgs,
                                lru=_tool_ctx_lru,
                                target_token_budget=int(_dtool_ctx * 0.70),
                                hard_floor_tokens=int(_dtool_ctx * 0.62),
                                protect_paths=_recent_focus_paths,
                            )
                            if _evicted_n > 0:
                                _ctx_evictions += int(_evicted_n)
                                _est_tokens = _calib_tokens(_coder_real_prompt_tokens, _coder_est_at_real, _estimate_ctx_tokens(_dtool_msgs))
                                # METER-FIX (2026-08-25): the real value is now
                                # STALE (pre-eviction). Reset -> naechster
                                # ctx_meter nutzt den frischen Schaetzer.
                                _coder_real_prompt_tokens[0] = 0
                                _coder_est_at_real[0] = 0
                                logger.warning(
                                    "[CTX-EVICT-EMERGENCY] est_before=%d evicted=%d est_after=%d ctx=%d target=%d hard_floor=%d real_before=%d reason=%s",
                                    _est_before_evict, _evicted_n, int(_est_tokens), int(_dtool_ctx),
                                    int(_dtool_ctx * 0.70), int(_dtool_ctx * 0.62), int(_guard_tokens),
                                    _guard_decision.reason,
                                )
                                yield await ctx.emit({
                                    "type": "status",
                                    "content": (
                                        f"🧹 Emergency context eviction (no compression left): {_evicted_n} "
                                        "stale tool output(s) replaced by recall markers."
                                    ),
                                })
                            _guard_decision = _decide_context_action(
                                guard_tokens=int(_guard_tokens),
                                ctx_tokens=int(_dtool_ctx),
                                threshold=int(_compress_threshold),
                                can_compress=_can_compress,
                                force_compress=bool(_force_compress_next),
                            )
                            _compress_ok = (_guard_decision.action == "compress")
                            # First-round protection: the initial context
                            # (explore/static map) may only be compressed via
                            # threshold after >=1 tool round (emergency >90%
                            # still OK).
                            if _compress_ok and _guard_decision.reason == "threshold" and _total_tool_rounds <= 0:
                                _compress_ok = False
                            if not _compress_ok and _guard_decision.action == "emergency_evict":
                                _est_before_evict = int(_est_tokens)
                                _evicted_n = _evict_stale_tool_outputs(
                                    messages=_dtool_msgs,
                                    lru=_tool_ctx_lru,
                                    target_token_budget=int(_dtool_ctx * 0.70),
                                    hard_floor_tokens=int(_dtool_ctx * 0.62),
                                    protect_paths=_recent_focus_paths,
                                )
                                if _evicted_n > 0:
                                    _ctx_evictions += int(_evicted_n)
                                    _est_tokens = _calib_tokens(_coder_real_prompt_tokens, _coder_est_at_real, _estimate_ctx_tokens(_dtool_msgs))
                                    # METER-FIX (2026-08-25): the real value is now
                                    # STALE (pre-eviction). Reset -> naechster
                                    # ctx_meter nutzt den frischen Schaetzer.
                                    _coder_real_prompt_tokens[0] = 0
                                    _coder_est_at_real[0] = 0
                                    logger.warning(
                                        "[CTX-EVICT-EMERGENCY] est_before=%d evicted=%d est_after=%d ctx=%d target=%d hard_floor=%d real_before=%d reason=%s",
                                        _est_before_evict, _evicted_n, int(_est_tokens), int(_dtool_ctx),
                                        int(_dtool_ctx * 0.70), int(_dtool_ctx * 0.62), int(_guard_tokens),
                                        _guard_decision.reason,
                                    )
                                    yield await ctx.emit({
                                        "type": "status",
                                        "content": (
                                            f"🧹 Emergency context eviction (no compression left): {_evicted_n} "
                                            "stale tool output(s) replaced by recall markers."
                                        ),
                                    })
                        # MASK-FIRST (2026-10-14, audit phase 4): the
                        # deterministic observation masking (stale tool
                        # outputs -> recall markers, same machinery as the
                        # emergency valve) runs BEFORE the LLM summary
                        # decision. No LLM call, no slot evict, bounded to
                        # the threshold target — and it can CLEAR
                        # _compress_ok, so the LLM compression below only
                        # fires when masking alone was not enough.
                        # Settings toggle: duo_mask_before_compress.
                        if _compress_ok and ctx.settings.get("duo_mask_before_compress", True):
                            try:
                                _mask_n = _evict_stale_tool_outputs(
                                    messages=_dtool_msgs,
                                    lru=_tool_ctx_lru,
                                    target_token_budget=int(_compress_threshold),
                                    hard_floor_tokens=int(_dtool_ctx * 0.62),
                                    protect_paths=_recent_focus_paths,
                                )
                            except Exception as _mask_err:
                                logger.warning("[CTX-MASK] failed: %s", _mask_err)
                                _mask_n = 0
                            if _mask_n > 0:
                                _est_tokens = _calib_tokens(_coder_real_prompt_tokens, _coder_est_at_real, _estimate_ctx_tokens(_dtool_msgs))
                                _guard_tokens = max(
                                    int(_coder_real_prompt_tokens[0] or 0), int(_est_tokens))
                                yield await ctx.emit({"type": "status",
                                    "content": f"🧹 Masked {_mask_n} stale tool output(s) — no summary needed."})
                                logger.info(
                                    "[CTX-MASK] masked=%d est_after=%d threshold=%d (LLM compression skipped)",
                                    _mask_n, int(_est_tokens), int(_compress_threshold),
                                )
                                if _guard_tokens <= int(_compress_threshold):
                                    _compress_ok = False
                        if _compress_ok:
                            # OUTSOURCED (split 2.8): the compression cycle → _duo_compress_cycle();
                            # the fail-streak round-loop exit comes back as ps.stop.
                            _cc_ps = _DuoCompressCycleState(
                                _call_sigs=_call_sigs,
                                _coder_est_at_real=_coder_est_at_real,
                                _coder_real_prompt_tokens=_coder_real_prompt_tokens,
                                _comp_llm_cfg=_comp_llm_cfg,
                                _comp_llm_timeout_s=_comp_llm_timeout_s,
                                _compress_local_only=_compress_local_only,
                                _compress_threshold=_compress_threshold,
                                _di=_di,
                                _done_tasks=_done_tasks,
                                _dport=_dport,
                                _dtc=_dtc,
                                _dtool_ctx=_dtool_ctx,
                                _dtool_sys_eff=_dtool_sys_eff,
                                _explore_ctx=_explore_ctx,
                                _goal_pin_msg=_goal_pin_msg,
                                _guard_decision=_guard_decision,
                                _guard_tokens=_guard_tokens,
                                _last_test_status=_last_test_status,
                                _lifetime_tool_rounds=_lifetime_tool_rounds,
                                _max_tool_rounds=_max_tool_rounds,
                                _n_items=_n_items,
                                _partial_compression=_partial_compression,
                                _plan_result=_plan_result,
                                _plan_tracker=_plan_tracker,
                                _read_counts=_read_counts,
                                _seen_explore_paths=_seen_explore_paths,
                                _subtasks=_subtasks,
                                _tool_ctx_lru=_tool_ctx_lru,
                                _written_files=_written_files,
                                _ws_str=_ws_str,
                                _MAX_COMPRESSIONS=_MAX_COMPRESSIONS,
                                _compress_fail_streak=_compress_fail_streak,
                                _ctx_compressions=_ctx_compressions,
                                _ctx_critical_warned=_ctx_critical_warned,
                                _ctx_full_skips=_ctx_full_skips,
                                _ctx_pressure_warned=_ctx_pressure_warned,
                                _dtool_msgs=_dtool_msgs,
                                _est_tokens=_est_tokens,
                                _explore_only_rounds=_explore_only_rounds,
                                _force_compress_next=_force_compress_next,
                                _llm_compress_fails=_llm_compress_fails,
                                _loop_detected=_loop_detected,
                                _no_write_nudged=_no_write_nudged,
                                _no_write_rounds=_no_write_rounds,
                                _partial_escalated=_partial_escalated,
                                _rule_compress_fails=_rule_compress_fails,
                                _rule_compress_used=_rule_compress_used,
                                _total_tool_rounds=_total_tool_rounds,
                                carry={"exec_mdl": exec_mdl},
                            )
                            async for _ev in _duo_compress_cycle(ctx, _cc_ps):
                                yield _ev
                            _MAX_COMPRESSIONS = _cc_ps._MAX_COMPRESSIONS
                            _compress_fail_streak = _cc_ps._compress_fail_streak
                            _ctx_compressions = _cc_ps._ctx_compressions
                            _ctx_critical_warned = _cc_ps._ctx_critical_warned
                            _ctx_full_skips = _cc_ps._ctx_full_skips
                            _ctx_pressure_warned = _cc_ps._ctx_pressure_warned
                            _dtool_msgs = _cc_ps._dtool_msgs
                            _sanitize_upto = 0  # compression rewrote the history
                            _est_tokens = _cc_ps._est_tokens
                            _explore_only_rounds = _cc_ps._explore_only_rounds
                            _force_compress_next = _cc_ps._force_compress_next
                            _llm_compress_fails = _cc_ps._llm_compress_fails
                            _loop_detected = _cc_ps._loop_detected
                            _no_write_nudged = _cc_ps._no_write_nudged
                            _no_write_rounds = _cc_ps._no_write_rounds
                            _partial_escalated = _cc_ps._partial_escalated
                            _rule_compress_fails = _cc_ps._rule_compress_fails
                            _rule_compress_used = _cc_ps._rule_compress_used
                            _total_tool_rounds = _cc_ps._total_tool_rounds
                            if _cc_ps.stop:
                                break
                        if _duo_state.think_runtime:
                            _tool_thinking_budget = _calculate_thinking_tokens(
                                exec_mdl,
                                ctx.settings,
                                input_tokens=_guard_tokens,
                                available_ctx=_dtool_ctx,
                                agent_name="duo_coder"
                            )
                            # (e.g. context full), thinking per _apply_thinking_kwargs in the
                            if _tool_thinking_budget == 0:
                                _duo_state.think_runtime = False
                        else:
                            _tool_thinking_budget = 0
                        _thinking = bool(_tool_thinking_budget > 0)
                        _profile = get_sampling_profile(exec_mdl, _thinking, ctx.settings)
                        # Guarantee system message at position 0 (Jinja template requires it)
                        if _dtool_msgs and _dtool_msgs[0].get("role") != "system":
                            _sys = next((m for m in _dtool_msgs if m.get("role") == "system"), None)
                            if _sys:
                                _dtool_msgs.remove(_sys)
                                _dtool_msgs.insert(0, _sys)
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(
                                "[ROLE-DUMP] round=%d system_count=%d roles=%s",
                                _di, sum(1 for m in _dtool_msgs if m.get("role") == "system"),
                                json.dumps([m.get("role") for m in _dtool_msgs]),
                            )
                        # D3-NOISE-FIX (2026-08-22): pure tail append (new messages
                        # SCAN-GATE (2026-09-09): pure diagnostics — the per-message signature
                        # scan only runs with DEBUG logging (was an O(n) scan every round).
                        if logger.isEnabledFor(logging.DEBUG):
                            try:
                                _sig_now = [(m.get("role", ""), len(str(m.get("content", "") or ""))) for m in _dtool_msgs]
                                _prev_sig = _prev_msg_sig[0] if _prev_msg_sig else None
                                _changed_early = []
                                if _prev_sig:
                                    _cum_chars = 0
                                    for _mi in range(max(len(_sig_now), len(_prev_sig))):
                                        if _mi >= len(_prev_sig):
                                            break
                                        _s_cur = _sig_now[_mi] if _mi < len(_sig_now) else None
                                        _s_prev = _prev_sig[_mi]
                                        if _s_cur != _s_prev:
                                            if _cum_chars / _CHARS_PER_TOKEN < 10000:
                                                _changed_early.append((_mi, int(_cum_chars / _CHARS_PER_TOKEN), _s_prev, _s_cur))
                                            break
                                        if _s_cur:
                                            _cum_chars += _s_cur[1]
                                _prev_msg_sig[0] = _sig_now
                                if _changed_early:
                                    logger.warning(
                                        "[MSGSIG-CHANGE] round=%d change below 10k tokens: %s (msgs=%d)",
                                        _dr, _changed_early[:5], len(_sig_now),
                                    )
                            except Exception as _sig_err:
                                logger.debug("[MSGSIG-CHANGE] Signature error: %s", _sig_err)
                        # DYNAMIC OUTPUT RESERVE (2026-09-05): max_tokens per
                        # tool round is clamped to the free space (est-based,
                        # worst-case factor 1.35). Full budgets stay as long
                        # as there is room; near the compression threshold
                        # the output budget shrinks instead of overflowing.
                        _est_now = _calib_tokens(_coder_real_prompt_tokens, _coder_est_at_real, _estimate_ctx_tokens(_dtool_msgs))
                        _max_tokens_round = _clamp_request_max_tokens(
                            est_tokens=int(_est_now),
                            ctx_tokens=int(_dtool_ctx),
                            max_output=int(_dtool_opts.get("num_predict", 1024) or 1024),
                            # CLAMP-RESERVE-FIX (2026-09-09): honor the same
                            # reserve knobs the threshold math uses instead of
                            # the hardwired 768 default.
                            reserve_tokens=max(
                                768,
                                int(ctx.settings.get("duo_compress_overflow_reserve", 1024) or 1024),
                            ),
                        )
                        if _dtool_ctx > 0 and _max_tokens_round > int(_dtool_ctx):
                            _max_tokens_round = int(_dtool_ctx)
                        # MIN-VIABLE-GUARD (2026-09-06): with a sub-~1k budget a
                        # tool call can never finish -> sending it truncates JSON
                        # args mid-write and poisons history (llama 500 on the next
                        # request). Compress first instead of generating garbage.
                        # VIABLE-FLOOR (2026-10-31, live): the old fixed
                        # 1024 let a 2404-token budget through for an
                        # edit-heavy round; the edit was cut, dropped and
                        # retried with the same budget. Scale the floor with
                        # the round's own request instead.
                        _min_viable_budget = _min_viable_tool_budget(
                            int(_dtool_opts.get("num_predict", 1024) or 1024))
                        if int(_max_tokens_round) >= _min_viable_budget:
                            _ctx_full_skips = 0
                        else:
                            _ctx_full_skips += 1
                            if _ctx_full_skips >= 4:
                                logger.error(
                                    "[CTX-FULL] 4 consecutive rounds without enough headroom "
                                    "(est=%d ctx=%d) - compression cannot shrink enough",
                                    int(_est_now), int(_dtool_ctx),
                                )
                                yield await ctx.emit({"type": "status",
                                    "content": "⛔ Context cannot be reduced enough for a tool round — run stopped."})
                                _ld_setter(3726); _loop_detected = True
                                break
                            if (_can_compress or _guard_tokens > int(_dtool_ctx * 0.90)) and _total_tool_rounds > 0:
                                # STORM-GUARD (2026-10-30, live 22.10.: 40 force
                                # compressions in 56 s because local-only
                                # compression never got ahead of the tool-output
                                # inflow): a force compression may fire only
                                # every MIN_GAP rounds; in between the run
                                # degrades to the tiny-budget fallback instead
                                # of looping.
                                from context.ctx_guard import allow_force_compress as _afc
                                # EMERGENCY (audit 30.10): at >90% ctx or
                                # >=2 rounds without a viable budget the brake
                                # must not block (head <= 0 would fire a
                                # 512-token request against the wall and
                                # poison the tool-call history).
                                _ctx_emergency = (
                                    _guard_tokens > int(_dtool_ctx * 0.90)
                                    or _ctx_full_skips >= 2
                                )
                                if not _afc(_dr, _last_force_compress_round, 3,
                                            emergency=_ctx_emergency):
                                    logger.warning(
                                        "[CTX-FULL] storm guard: force compression throttled "
                                        "(last=%d round=%d) - skipping this round instead of "
                                        "shipping a doomed budget",
                                        _last_force_compress_round, _dr,
                                    )
                                    continue
                                else:
                                    logger.warning(
                                        "[CTX-FULL] insufficient headroom for a safe tool round "
                                        "(max_tokens=%d est=%d real=%d skip=%d/4) - compressing first",
                                        int(_max_tokens_round), int(_est_now), int(_guard_tokens),
                                        _ctx_full_skips,
                                    )
                                    yield await ctx.emit({"type": "status",
                                        "content": f"⛽ Context too full for a safe tool round — compressing first. ({_ctx_full_skips}/4)"})
                                    _force_compress_next = True
                                    _last_force_compress_round = _dr
                                    continue
                            # Compression exhausted & not at emergency level: keep
                            # the old tiny-budget behaviour instead of looping.
                            _max_tokens_round = max(int(_max_tokens_round), 512)
                        # TOOLCALL-SANITIZE (2026-09-06): never send assistant
                        # tool_calls with malformed JSON args (llama 500 re-parse).
                        try:
                            from core.toolcall_sanitize import sanitize_invalid_tool_call_history as _sanitize_tc
                            _sanitized_tc = _sanitize_tc(_dtool_msgs, start=_sanitize_upto)
                            _sanitize_upto = len(_dtool_msgs)
                            if _sanitized_tc:
                                logger.warning(
                                    "[TOOLCALL-REPAIR] removed %d invalid tool-call entrie(s) from history before POST",
                                    _sanitized_tc,
                                )
                        except Exception as _san_err:
                            logger.debug("[TOOLCALL-REPAIR] skipped: %s", _san_err)
                        _tool_payload = {
                            "model": exec_mdl, "messages": _dtool_msgs,
                            "tools": _active_tools, "stream": True,
                            # A-P2-7: real eval_counts from the llama.cpp server (usage
                            "stream_options": {"include_usage": True},
                            "tool_choice": "auto",
                            # TEMP-PRIORITY (2026-09-01): the Agent-card temperature
                            # (_duo_coder_temp) wins over the model sampling profile.
                            "temperature": _duo_coder_temp if _duo_coder_temp is not None else _profile.get("temperature", 0.6),
                            "top_p": _profile.get("top_p", 0.95),
                            "top_k": _profile.get("top_k", 20),
                            "presence_penalty": _profile.get("presence_penalty", 1.5),
                            "repetition_penalty": _profile.get("repetition_penalty", 1.0),
                            # OUTPUT-LIMIT-POLICY (2026-08-12): max_tokens = visible
                            "max_tokens": _max_tokens_round,
                            "thinking": _thinking,
                            "thinking_budget": max(0, _tool_thinking_budget),
                        }
                        if _profile.get("seed") is not None:
                            _tool_payload["seed"] = int(_profile["seed"])
                        # MIN-P-EXPLICIT (2026-09-20): min_p 0.0 must SHIP — the
                        # llama-server API default is 0.05, and dropping a
                        # declared 0.0 re-enables exactly the repetition filter
                        # some model cards (MiniCPM5) warn against.
                        if "min_p" in _profile:
                            _tool_payload["min_p"] = _profile["min_p"]
                        if _profile.get("cache_prompt"):
                            _tool_payload["cache_prompt"] = True
                        if _coder_no_think and not _duo_state.think_runtime:
                            # template-level thinking off (Spark/Qwen3-style
                            # templates; unknown keys are ignored by others).
                            # PHASE-3 CACHE (2026-10-14, audit): reactive-think
                            # (think_runtime=True) overrides no-think so the
                            # re-enable happens payload-side — mutating system
                            # messages instead killed the whole prefix cache.
                            _tool_payload["chat_template_kwargs"] = {"enable_thinking": False}
                        _tool_payload = _apply_thinking_kwargs(
                            _tool_payload, _profile, _thinking,
                            _coder_tool_think and _tool_thinking_budget > 0,
                        )
                        # delta.thinking/reasoning_content → ctx.emit as thinking_token + accumulate.
                        # AgenticToolLoop: POST+retry+SSE parse. Shares _duo_state in-place.
                        from core.agentic_tool_loop import AgenticToolLoop
                        from core.tool_loop import ToolLoopConfig
                        _duo_loop = AgenticToolLoop(
                            # LOG-MODEL (2026-10-17, live): the config carried no
                            # model name, so the per-round trace read
                            # "[TOOL-LOOP] model= port=8102" - a live run could
                            # not be attributed to a model from the log alone.
                            config=ToolLoopConfig(stream=True, max_post_attempts=20,
                                                  model=exec_mdl),
                            http_client=_dtc, round_state=_duo_state, emit_fn=_coder_emit_fn,
                            workspace=_ws_str, run_id=(getattr(ctx, "chat_id", None) or getattr(ctx, "run_id", "") or ""),
                        )
                        _hb_interval = _read_hb_interval(ctx.settings)
                        # IN-USE GUARD (2026-10-17, live): while this round is
                        # running, the coder slot must be un-evictable even
                        # after the run's pin is released - the pre-flight used
                        # to kill exactly this slot for another role's load and
                        # the round died with ConnectError.
                        _inuse_slot = None
                        try:
                            from backend.llama_server_manager import manager as _lsm_inuse
                            # _find_loaded is a coroutine: without await this
                            # was a truthy coroutine object, mark_in_flight
                            # raised AttributeError, the except swallowed it and
                            # the in-use guard was silently OFF (live
                            # 2026-10-27: RuntimeWarning was never awaited).
                            _inuse_slot = await _lsm_inuse._find_loaded(exec_mdl)
                            if _inuse_slot is not None:
                                _inuse_slot.mark_in_flight()
                        except (ImportError, AttributeError, TypeError):
                            _inuse_slot = None
                        _post_task = asyncio.create_task(
                            _duo_loop.post_with_retry(_tool_payload, dtool_msgs=_dtool_msgs, ctx=ctx, _parts=_parts)
                        )
                        _hb_start = time.monotonic()
                        _hb_last = _hb_start
                        while True:
                            _hb_had = False
                            while not _coder_event_q.empty():
                                _hb_had = True
                                try:
                                    yield _coder_event_q.get_nowait()
                                except asyncio.QueueEmpty:
                                    break
                            if _hb_had:
                                _hb_last = time.monotonic()
                            if _post_task.done():
                                break
                            # HEARTBEAT (2026-08-21): during long prompt processing
                            if time.monotonic() - _hb_last >= _hb_interval:
                                _hb_total = int(time.monotonic() - _hb_start)
                                _hb_last = time.monotonic()
                                # PREFILL-METER (2026-09-20): attach live llama-server
                                # prefill progress when one is running (post-compress
                                # re-prefill, ctx reload) so the UI shows percent + rate
                                # instead of a blind elapsed counter.
                                _hb_ev = {"type": "heartbeat", "elapsed": _hb_total}
                                # F4 (2026-11-07): meter is async now — discovery +
                                # tail read run via to_thread, off the loop.
                                _hb_pf = await _llama_prefill_progress(_dport)
                                if _hb_pf:
                                    _hb_ev["prefill"] = _hb_pf
                                yield await ctx.emit(_hb_ev)
                            await asyncio.sleep(0.02)
                        # TASK-DEATH FIX (2026-10-10): post_with_retry can die
                        # with an unhandled RuntimeError (in-loop ensure_loaded
                        # after ctx mismatch / evict). .result() re-raises into
                        # the surrounding frame, which swallowed it silently -
                        # six runs ended with no usage, no [CACHE], no error
                        # message (asyncio only logged the orphaned task).
                        # Surface it and end the run with a real reason.
                        try:
                            _result = _post_task.result()
                        except Exception as _pt_exc:
                            logger.warning(
                                "[CODER-ROUND-FAIL] post task died: %s: %s",
                                type(_pt_exc).__name__, str(_pt_exc)[:200],
                            )
                            yield await ctx.emit({"type": "status",
                                "content": f"⛔ Coder round failed ({type(_pt_exc).__name__}): {str(_pt_exc)[:140]}"})
                            ctx.phase_timer.skip("coder_loop")
                            if _inuse_slot is not None:
                                try:
                                    _inuse_slot.clear_in_flight()
                                except (AttributeError, TypeError):
                                    pass
                            clear_graceful_stop(ctx.run_id)
                            _cleanup_pause_state(ctx.run_id)
                            if ctx.chat_id:
                                _clear_pause_state(ctx.chat_id)
                            _cleanup_governor(ctx.chat_id or ctx.run_id)
                            ctx.duo_stop_reason = "runtime_error"
                            yield await ctx.emit(ctx.done_event(
                                round(time.time() - ctx.t_total, 1),
                                ctx.duo_stop_reason,
                                **ctx.collect_done_metrics(),
                            ))
                            return
                        # _duo_state mutated in-place by AgenticToolLoop — no sync needed
                        if _inuse_slot is not None:
                            try:
                                _inuse_slot.clear_in_flight()
                            except (AttributeError, TypeError):
                                pass
                        _dport = _result["dport"]
                        _cached_coder_port = _duo_state.cached_port
                        _dr_msg = _result["dr_msg"]
                        _dr_content_parts = _result["dr_content_parts"]
                        _dr_thinking_parts = _result["dr_thinking_parts"]
                        _dr_tool_calls_acc = _result["dr_tool_calls_acc"]
                        _dr_stream_ok = _result.get("dr_stream_ok", False)
                        if _result.get("loop_detected"):
                            _logger = logger
                            logger.warning(
                                "[LD-SRC] AgenticToolLoop loop_detected — result keys=%s dr_stream_ok=%s",
                                sorted(k for k in _result.keys() if k != "dr_content_parts"),
                                _result.get("dr_stream_ok"),
                            )
                            _ld_setter(3022); _loop_detected = True
                            break
                        if _result.get("force_compress"):
                            _force_compress_next = True
                            continue
                        if not _dr_stream_ok:
                            raise RuntimeError("Tool-POST: stream not received — no error detail")
                        _duo_state.http_404_retries = 0
                        _dr_content = _dr_msg.get("content") or ""
                        if _dr_content and "<think" in _dr_content:
                            _dr_content = _preprocess_think_blocks(_dr_content)
                            _dr_msg = {**_dr_msg, "content": _dr_content}
                        # Strip ```json ... ``` Markdown fences (Qwen3.6 wraps tool calls)
                        if _dr_content and "```" in _dr_content:
                            _dr_content = _RE_JSON_FENCE.sub(lambda m: m.group(1), _dr_content)
                            _dr_msg = {**_dr_msg, "content": _dr_content}
                        _dr_tcs = _dr_msg.get("tool_calls", [])
                        _drop_len = (" — finish_reason=length (output token limit reached)"
                                     if _result.get("dr_finish_reason") == "length" else "")
                        # VALIDATE-EXTRACT (2026-09-30): the tool-call repair/
                        # salvage block moved VERBATIM into utils.tool.validate_
                        # tool_calls — a pure function (no ctx/emit/state) so
                        # fix_agent, subagent_lite and the chat loop can reuse
                        # the exact same behavior; the write-tools suite pins it.
                        from utils.tool import validate_tool_calls as _vtc_val
                        try:
                            from tools.runner import _get_write_budget as _gwb_v
                            _wb_tok, _wb_cpt = _gwb_v() or (None, None)
                        except Exception:
                            _wb_tok = _wb_cpt = None
                        _vres = _vtc_val(
                            _dr_tcs, finish_reason=_result.get("dr_finish_reason"),
                            model=coder_mdl, token_budget=_wb_tok, chars_per_token=_wb_cpt)
                        _validated_tcs = _vres["tool_calls"]
                        _drop_notices = _vres["drop_notices"]
                        _drop_names = _vres["dropped_names"]
                        _salvage_notes = _vres["salvage_notes"]
                        if _result.get("dr_finish_reason") in ("length", "drift"):
                            # countable truncation event for the A/B benchmark,
                            # with overrun-class discrimination: write-overrun
                            # (had_tool_calls) vs reasoning-overrun (no call).
                            # had_ counts pre-validation, has_ post-validation —
                            # a dropped salvage must not read as reasoning-only.
                            # reasoning_tokens is n/a when the server does not
                            # report completion_tokens_details (llama.cpp b11191
                            # likely doesn't) — 0 would read as "no reasoning"
                            # although the value is unknown; think_chars (SSE
                            # reasoning deltas) covers the reasoning size either way
                            _ru = _result.get("dr_usage_final") or {}
                            _rtd = (_ru.get("completion_tokens_details") or {}
                                    if isinstance(_ru, dict) else {})
                            _rt_raw = _rtd.get("reasoning_tokens") if isinstance(_rtd, dict) else None
                            _think_chars = sum(len(p) for p in (_result.get("dr_thinking_parts") or []))
                            logger.warning(
                                "[WRITE-TRUNCATION] model=%s round=%s finish_reason=%s "
                                "had_tool_call=%s has_tool_call=%s content_len=%d "
                                "reasoning_tokens=%s think_chars=%d",
                                coder_mdl, _dr, _result.get("dr_finish_reason"),
                                _vres["meta"]["had_tool_calls"],
                                _vres["meta"]["has_tool_call"],
                                len(_dr_msg.get("content") or ""),
                                ("n/a" if _rt_raw is None else _rt_raw), _think_chars)
                        # TRUNCATED-WRITE-MARKER (2026-10-11, v2): a write_file cut at the output
                        # limit was first dropped - but the salvage already holds ~90% of the
                        # content (live: 32638 chars/844 lines) and the model just retried the
                        # same oversized write three times anyway. Now: the salvaged content IS
                        # written with an explicit truncation marker appended, and the model
                        # continues from the marker with edit_file / write_file_append.
                        if _result.get("dr_finish_reason") == "length":
                            _MARK = ("\n\n"
                                     "<<<TRUNCATION_MARKER: the generator hit the output limit at this "
                                     "exact point. Everything above was generated and saved; the file "
                                     "ends here and MAY be incomplete. Continue the implementation with "
                                     "edit_file or write_file_append.>>>\n")
                            _salvaged_paths = []
                            for _wi in reversed(range(len(_validated_tcs))):
                                _wtc = _validated_tcs[_wi]
                                if str(((_wtc.get("function") or {}).get("name")) or "") not in ("write_file", "write_file_append"):
                                    continue
                                try:
                                    _wa = _wtc["function"].get("arguments", "")
                                    _ad = json.loads(_wa) if isinstance(_wa, str) else dict(_wa or {})
                                    _ad["content"] = (str(_ad.get("content", "")) + _MARK)
                                    _wtc["function"]["arguments"] = json.dumps(_ad, ensure_ascii=False)
                                    _salvaged_paths.append(str(_ad.get("path", "") or "?"))
                                except Exception:
                                    _validated_tcs.pop(_wi)
                            if _salvaged_paths:
                                state["_cut_write_attempts"] = int(state.get("_cut_write_attempts", 0)) + 1
                                _drop_notices.append(
                                    "⚠ write_file hit the output limit and was cut - the partial content "
                                    "WAS written to " + ", ".join(_salvaged_paths) + " with a "
                                    "<<<TRUNCATION_MARKER>>> at the end. The marker sits in a possibly broken "
                                    "syntax context: first edit_file it away together with any broken tail, "
                                    "then continue with edit_file or write_file_append. Do NOT rewrite the "
                                    "whole file.")
                            elif not _validated_tcs:
                                _drop_notices.append(
                                    "⛔ write_file was cut at the output limit and could not be salvaged - "
                                    "nothing was written. Re-send in smaller pieces: write_file the first "
                                    "half, then write_file_append the rest.")
                        if _drop_notices:
                            _dtool_msgs.append({"role": "user",
                                                 "content": "\n".join(_drop_notices)})
                        _dr_tcs = _validated_tcs
                        if _validated_tcs:
                            _dr_dropped_tool_retries = 0
                        if not _dr_tcs:
                            # TOOL-CALL-MISS (2026-10-30, live Groot run): 7/7
                            # coder rounds emitted tool calls as plain text and
                            # nothing parsed; nine offline probes against the
                            # same slot parsed fine, so the request state of the
                            # failing round is the only remaining evidence.
                            # One WARNING per miss - never per token.
                            if _is_tool_round:
                                try:
                                    import hashlib as _hl
                                    _miss_usage = _result.get("dr_usage_final") or {}
                                    _miss_tools = _tool_payload.get("tools") or []
                                    _miss_tools_json = json.dumps(
                                        _miss_tools, sort_keys=True, ensure_ascii=False)
                                    _miss_content = str(_dr_msg.get("content") or "")
                                    _miss_reason = "".join(
                                        _result.get("dr_thinking_parts") or [])
                                    # RAW (2026-10-31, audit): _dr_msg has
                                    # already been through <think> preprocessing
                                    # and fence stripping; the incident needs the
                                    # untouched SSE accumulation too.
                                    _miss_raw = "".join(
                                        _result.get("dr_content_parts") or [])
                                    logger.warning(
                                        "[TOOL-CALL-MISS] round=%s model=%s "
                                        "thinking=%s ctk=%s tools=%d schema_chars=%d "
                                        "hash=%s tool_choice=%s slot_ctx=%s req_ctx=%s "
                                        "prompt_tokens=%s cached=%s finish=%s "
                                        "completion=%s max_tokens=%s "
                                        "temp=%s presence=%s top_p=%s top_k=%s min_p=%s seed=%s "
                                        "content[0:500]=%r reasoning[0:500]=%r "
                                        "raw[0:500]=%r",
                                        _dr, coder_mdl, _thinking,
                                        _tool_payload.get("chat_template_kwargs"),
                                        len(_miss_tools), len(_miss_tools_json),
                                        _hl.sha256(_miss_tools_json.encode(
                                            "utf-8")).hexdigest()[:12],
                                        _tool_payload.get("tool_choice"),
                                        _dtool_opts.get("num_ctx"),
                                        _coder_ctx_eff,
                                        _miss_usage.get("prompt_tokens"),
                                        _miss_usage.get("cached_tokens"),
                                        _result.get("dr_finish_reason"),
                                        _miss_usage.get("completion_tokens"),
                                        _tool_payload.get("max_tokens"),
                                        _tool_payload.get("temperature"),
                                        _tool_payload.get("presence_penalty"),
                                        _tool_payload.get("top_p"),
                                        _tool_payload.get("top_k"),
                                        _tool_payload.get("min_p"),
                                        _tool_payload.get("seed"),
                                        _miss_content[:500],
                                        _miss_reason[:500],
                                        _miss_raw[:500],
                                    )
                                except (TypeError, ValueError,
                                        AttributeError, KeyError) as _miss_err:
                                    logger.debug(
                                        "[TOOL-CALL-MISS] log failed: %s", _miss_err)
                            # explored. Now: prompt targeted repair, own
                            # counter against endless loops.
                            if _drop_notices:
                                _dr_think_only_retries = 0
                                _dr_dropped_tool_retries += 1
                                if _dr_dropped_tool_retries >= 3:
                                    _ld_setter(3089); _loop_detected = True
                                    yield await ctx.emit({"type": "status",
                                        "content": f"⚠ Tool-call args {_dr_dropped_tool_retries}x malformed/truncated — loop aborted."})
                                    break
                                if _should_compress_after_truncated_drop(
                                        _result.get("dr_finish_reason"),
                                        _drop_names):
                                    # A truncated edit_file cannot be applied
                                    # half-way, so the drop is right - but a
                                    # retry with the SAME clamped budget
                                    # truncates again by construction. Give
                                    # the next round fresh headroom instead.
                                    _force_compress_next = True
                                    _dtool_msgs.append({"role": "user", "content": (
                                        "[OUTPUT LIMIT] The edit_file was cut at "
                                        f"this round's output budget "
                                        f"({int(_max_tokens_round)} tokens) and was "
                                        "NOT applied half-way. Compressing first; "
                                        "then split the edit into smaller edit_file "
                                        "calls (or use write_file_append).")})
                                    logger.warning(
                                        "[TOOL-TRUNCATION] edit_file dropped at "
                                        "finish=length (budget=%d) - forcing "
                                        "compression instead of an identical retry",
                                        int(_max_tokens_round))
                                    continue
                                if _dr < _max_tool_rounds - 1:
                                    # write_file_append; edit_file → small SEARCH/REPLACE).
                                    yield await ctx.emit({"type": "status",
                                        "content": (
                                            f"⚠ Tool call {_dr_dropped_tool_retries}/3 truncated "
                                            f"({'/'.join(_drop_names[:3]) or '?'}): JSON args malformed/truncated"
                                            f"{_drop_len}. Model should write large writes in steps."
                                        )})
                                    _dtool_msgs.append({"role": "user",
                                        "content": _build_dropped_tool_retry(_drop_names)})
                                    continue
                            _final = _re_think_cleanup.sub(" ", (_dr_msg.get("content") or "")).strip()
                            if _final:
                                if not _soft_check_done and _file_changes:
                                    _sc = _build_soft_check(_file_changes, _ws_str)
                                    if _sc:
                                        _dtool_msgs.append({"role": "user", "content": _sc})
                                        _soft_check_done = True
                                        _dr_think_only_retries = 0
                                        continue
                                _parts.append(_final)
                                _dr_think_only_retries += 1
                                _coder_no_think = True
                                if _dr_think_only_retries > 2:
                                    # FINAL-SUMMARY-ACCEPT (2026-09-10): when real
                                    # work exists and the model delivers a substantial
                                    # text summary, this is the task's conclusion —
                                    # not a loop. Abort only when nothing was written
                                    # (live: spark-1.7b finished with a verification
                                    # summary and got killed as a 'loop').
                                    # substantial summary counts even without
                                    # own writes (verification-only follow-up runs)
                                    # ZERO-ACTIVITY GUARD (2026-09-20): a summary with
                                    # zero reads AND zero writes is not a conclusion —
                                    # the model just echoed the briefing (live: Hermes
                                    # restated "[Plan Briefing - IMPLEMENT THIS]" for 3
                                    # rounds, the text was accepted as the answer and
                                    # the run completed without touching a file).
                                    # Accepting now requires tool activity; a
                                    # zero-activity summary gets one targeted
                                    # start-working nudge before the loop abort.
                                    _any_activity = bool(_file_changes) or bool(_seen_explore_paths)
                                    if _any_activity and ((_file_changes and len(_final) >= 80) or len(_final) >= 200):
                                        yield await ctx.emit({"type": "status",
                                            "content": "✅ Coder concluded with a final summary — accepting it as the answer."})
                                        break
                                    if (not _any_activity and not _zero_activity_nudged
                                            and _dr < _max_tool_rounds - 1):
                                        _zero_activity_nudged = True
                                        _dr_think_only_retries = 0
                                        _dtool_msgs.append({"role": "user", "content": (
                                            "[ZERO PROGRESS] You replied with text but have not read "
                                            "or changed any file. Start implementing NOW: read_file the "
                                            "relevant files, then edit_file the changes. Call "
                                            "task_complete only once the work is done (or if the task "
                                            "is genuinely answer-only).")})
                                        continue
                                    _ld_setter(3089); _loop_detected = True
                                    yield await ctx.emit({"type": "status",
                                        "content": f"⚠ Coder replies with text only, no tool call ({_dr_think_only_retries}x) — loop aborted."})
                                    break
                                if _dr < _max_tool_rounds - 1:
                                    _nudge = (
                                        "If the task is complete, call task_complete now with a brief status summary. "
                                        "Only call edit_file/run_bash if something is still missing."
                                    ) if _file_changes else (
                                        "You must call a tool now. Do not explain — use edit_file or run_bash directly.")
                                    _dtool_msgs.append({"role": "user", "content": _nudge})
                                    continue
                            elif not _parts:
                                # EMPTY-RETRY (2026-09-20): this branch used to
                                # fall through to the round-loop break, so a
                                # single empty coder answer ENDED the run as
                                # "completed" with zero work done (live: Hermes
                                # emitted reasoning only, no content, no tool
                                # call). Retry with a nudge, same escalation as
                                # the non-empty no-tool path.
                                _dr_think_only_retries += 1
                                _coder_no_think = True
                                logger.warning(
                                    "[CODER-EMPTY-RAW] round=%d finish=%s content=%r think=%r",
                                    _dr, _result.get("dr_finish_reason"),
                                    (_dr_msg.get("content") or "")[:300],
                                    "".join(_dr_thinking_parts or [])[:300],
                                )
                                if _dr_think_only_retries > 2:
                                    _ld_setter(3113); _loop_detected = True
                                    yield await ctx.emit({"type": "status",
                                        "content": f"⚠ Coder returns empty answers ({_dr_think_only_retries}x) — loop aborted."})
                                    break
                                if _dr < _max_tool_rounds - 1:
                                    yield await ctx.emit({"type": "token", "content":
                                        f"[Tool loop: empty answer from {coder_mdl} — retrying with a nudge]"})
                                    _dtool_msgs.append({
                                        "role": "user",
                                        "content": "You returned an empty response. Call a tool now — edit_file or run_bash.",
                                    })
                                    continue
                                _parts.append(f"[Tool loop: empty answer from {coder_mdl} — check llama-server log]")
                            else:
                                _dr_think_only_retries += 1
                                _coder_no_think = True
                                logger.warning(
                                    "[CODER-EMPTY-RAW] round=%d finish=%s content=%r think=%r",
                                    _dr, _result.get("dr_finish_reason"),
                                    (_dr_msg.get("content") or "")[:300],
                                    "".join(_dr_thinking_parts or [])[:300],
                                )
                                if _dr_think_only_retries > 2:
                                    _ld_setter(3113); _loop_detected = True
                                    yield await ctx.emit({"type": "status",
                                        "content": f"⚠ Coder returns empty answers ({_dr_think_only_retries}x) — loop aborted."})
                                    break
                                if _dr < _max_tool_rounds - 1:
                                    _dtool_msgs.append({
                                        "role": "user",
                                        "content": "You returned an empty response. Call a tool now — edit_file or run_bash.",
                                    })
                                    continue
                            break
                        _dtool_msgs.append(_dr_msg)
                        _dr_think_only_retries = 0
                        _dr_invalid_tool_retries = 0
                        if _grace_round_active and _dr_tcs:
                            _non_tc = [tc for tc in _dr_tcs
                                       if tc.get("function", {}).get("name", "") != "task_complete"]
                            if _non_tc:
                                if _grace_round_used:
                                    _dtool_msgs.append({"role": "user", "content": (
                                        "[GRACE ROUND EXPIRED] Grace round already used — exiting now. "
                                        "The run will end."
                                    )})
                                    _chunk_budget_exhausted = True
                                    _ld_setter(3137); _loop_detected = True
                                    break
                                _grace_round_used = True
                                _non_tc_names = [tc.get("function", {}).get("name", "?") for tc in _non_tc]
                                _dtool_msgs.append({"role": "user", "content": (
                                    f"[GRACE ROUND] Call ONLY task_complete — no other tools. "
                                    f"Your last round bundled {len(_non_tc)} other tool(s) "
                                    f"({', '.join(_non_tc_names[:5])}) which were rejected."
                                )})
                                _dr_think_only_retries += 1
                                continue
                        _exec_ps = _DuoExecRoundState(duo_deadline_at=_duo_deadline_at)
                        try:
                            async for _ev in _duo_exec_round_task(
                                ctx, state, _exec_ps,
                                _dr_tcs=_dr_tcs, _dtool_msgs=_dtool_msgs,
                                _duo_state=_duo_state,
                                _tool_exec_hooks=_tool_exec_hooks,
                                _tool_ctx_lru=_tool_ctx_lru,
                                _verify_mutation_serial=_verify_mutation_serial,
                                _verify_last_ok_serial=_verify_last_ok_serial,
                                _last_run_bash_failure=_last_run_bash_failure,
                                _changed_since_failure=_changed_since_failure,
                                _last_learned_insight_sig=_last_learned_insight_sig,
                                _last_too_large_path=_last_too_large_path,
                                _attempts_per_file=_attempts_per_file,
                                _wedge_state=_wedge_state,
                                _tool_error_retries=_tool_error_retries,
                                _call_sigs=_call_sigs,
                                _recent_focus_paths=_recent_focus_paths,
                                _file_changes=_file_changes,
                                _duo_seen_web_queries=_duo_seen_web_queries,
                                _task_complete_blocked=_task_complete_blocked,
                                _total_tool_errors_ref=_total_tool_errors_ref,
                                _tc_consecutive=_tc_consecutive,
                                _at_nosuite_nudged=_at_nosuite_nudged,
                                _consecutive_reads=_consecutive_reads,
                                _last_read_path=_last_read_path,
                                _read_ladder_fired=_read_ladder_fired,
                                _ws_budget_streak=_ws_budget_streak,
                                _ws_disabled=_ws_disabled,
                                _tool_mode=_tool_mode, _duo_ws=_duo_ws,
                                _ws_str=_ws_str, _explore_ctx=_explore_ctx,
                                _pre_explore_msgs=_pre_explore_msgs,
                                _written_files=_written_files,
                                _contracts_raw=_contracts_raw,
                                _coder_event_q=_coder_event_q,
                                _cached_coder_port=_cached_coder_port,
                                exec_mdl=exec_mdl,
                                _exec_has_thinking=_exec_has_thinking,
                                _tool_think_auto_mode=_tool_think_auto_mode,
                                _run_id_global=_run_id_global, _di=_di,
                            ):
                                yield _ev
                        finally:
                            _duo_deadline_at = _exec_ps.duo_deadline_at
                            _exec_result = _exec_ps.exec_result
                            _duo_timed_out = _exec_ps.timed_out
                            _snap_before = _exec_ps.snap_before
                            _last_too_large_ref = _exec_ps.last_too_large_ref
                            _cached_port_ref = _exec_ps.cached_port_ref
                        _wedge_ps = _DuoWedgeHandoffState(
                            loop_detected=_loop_detected,
                            wedge_escalated=_wedge_escalated,
                            duo_deadline_at=_duo_deadline_at,
                        )
                        try:
                            async for _ev in _duo_wedge_handoff(
                                ctx, state, _wedge_ps,
                                _exec_result=_exec_result, exec_mdl=exec_mdl,
                                _ws_str=_ws_str, _di=_di, _n_items=_n_items,
                                _subtask=_subtask, _dtool_ctx=_dtool_ctx,
                                _dtool_msgs=_dtool_msgs,
                                _wedge_state=_wedge_state,
                                _wedge_handoffs_used=_wedge_handoffs_used,
                                _cached_coder_port=_cached_coder_port,
                            ):
                                yield _ev
                        finally:
                            _loop_detected = _wedge_ps.loop_detected
                            _wedge_escalated = _wedge_ps.wedge_escalated
                            _duo_deadline_at = _wedge_ps.duo_deadline_at
                            _break_out = _wedge_ps.break_out
                        if _break_out:
                            break
                        _last_too_large_path = _last_too_large_ref[0]
                        _cached_coder_port = _cached_port_ref[0]
                        # DEADLINE-CANCEL (2026-10-31, audit): a cancelled
                        # round carries no result object, and every field
                        # below used to be dereferenced unconditionally
                        # (AttributeError instead of a clean timeout).
                        if _exec_result is None:
                            _duo_timed_out = True
                            yield await ctx.emit({"type": "status",
                                "content": "⏱ Run deadline reached — ending the run."})
                            break
                        _verify_mutation_serial = _exec_result.verify_mutation_serial
                        _verify_last_ok_serial = _exec_result.verify_last_ok_serial
                        for _sfp in _exec_result.send_files:
                            if _sfp and _sfp not in _pending_send_files:
                                _pending_send_files.append(_sfp)
                            # SEND-FILE-RESUME: mirror into ChunkState so a
                            # later resume block/checkpoint carries the queue.
                            if _sfp and _sfp not in _cs.send_files:
                                _cs.send_files.append(_sfp)
                        if _exec_result.extra_user_msg:
                            # SEND-FILE: hard next-step nudge after a delivery.
                            _dtool_msgs.append({
                                "role": "user",
                                "content": _exec_result.extra_user_msg})
                            _exec_result.extra_user_msg = ""
                        _last_run_bash_failure = _exec_result.last_run_bash_failure
                        _changed_since_failure = _exec_result.changed_since_failure
                        _last_learned_insight_sig = _exec_result.last_learned_insight_sig
                        _file_changes = _exec_result.file_changes
                        # safety net: adopt file_changes per round —
                        # abort paths would otherwise lose all writes (observed live 13:25).
                        _recent_focus_paths = _exec_result.recent_focus_paths_updated
                        for _wfm in re.findall(r"\[(?:Datei geschrieben|Written|Appended|(?:edit_file|write_file): (?:created|rewrote) ')([^\s'\(\]]+)", "".join(_parts)):
                            _wp = _wfm.strip()
                            if _wp and _wp not in _written_files: _written_files.append(_wp)
                        for _wfm2 in re.findall(r"\[patch_file: ([^\s]+) patched", "".join(_parts)):
                            _wp2 = _wfm2.strip()
                            if _wp2 and _wp2 not in _written_files: _written_files.append(_wp2)
                        # P1-FIX (2026-08-10): ops "write" (write_file) and "append"
                        for _wfm3, _vd3 in _file_changes.items():
                            if _vd3.get("op") in ("created", "rewrote", "edited", "write", "append") and _wfm3 not in _written_files:
                                _written_files.append(_wfm3)
                        _total_edit_lines += _sum_edit_lines(_file_changes)
                        # Track written files
                        # Plan tracker references
                        _last_tool_name = _exec_result.last_tool_name
                        _last_tool_result = _exec_result.last_tool_result
                        _ctxp_ps = _DuoCtxPressureState(ctx_pressure_warned=_ctx_pressure_warned)
                        try:
                            async for _ev in _duo_ctx_pressure_check(
                                ctx, _ctxp_ps,
                                _dtool_msgs=_dtool_msgs, _dtool_ctx=_dtool_ctx,
                                _coder_real_prompt_tokens=_coder_real_prompt_tokens,
                                est_tokens=_est_tokens,
                            ):
                                yield _ev
                        finally:
                            _ctx_pressure_warned = _ctxp_ps.ctx_pressure_warned
                        # Check if ALL tool-calls in this round were rejected/errored
                        _round_tool_results = []
                        for _msg in reversed(_dtool_msgs):
                            if _msg.get("role") != "tool":
                                break
                            _round_tool_results.append(_msg)
                        if _round_tool_results and _dr_tcs:
                            _all_rejected = all(
                                _msg.get("content", "").startswith("[TOOL_ERROR")
                                for _msg in _round_tool_results
                            )
                            if _all_rejected:
                                _dr_invalid_tool_retries += 1
                                if _dr_invalid_tool_retries >= 3:
                                    yield await ctx.emit({"type": "status",
                                        "content": "\u26a0\ufe0f All tool-calls rejected 3 rounds in a row — invalid-tool loop detected. Stopping."})
                                    _ld_setter(3298); _loop_detected = True
                                    break
                            else:
                                _dr_invalid_tool_retries = 0
                        # Capture last test status for compression summary
                        for _msg in reversed(_dtool_msgs[-8:]):
                            if (_msg.get("role") == "tool"
                                    and _msg.get("name") == "run_bash"
                                    and _msg.get("content")
                                    and any(_tk in str(_msg.get("content", "")) for _tk in ("pytest", "npm test", "cargo test", "go test"))):
                                _last_test_status = str(_msg["content"])[:200]
                                break
                        if _salvage_notes:
                            for _snote in _salvage_notes:
                                _dtool_msgs.append({"role": "user", "content": _snote})
                            _salvage_notes.clear()
                        if _exec_result.loop_detected:
                            _ld_setter(3314); _loop_detected = True
                            if _exec_result.duo_timed_out:
                                _duo_timed_out = True
                        if _exec_result.task_complete_called:
                            # C3 (2026-11-07 maintainability sprint): the
                            # verify gate lives in core/duo_task_complete_gate.
                            _tc_ps = _DuoTaskCompleteGateState(
                                ctx=ctx, exec_result=_exec_result,
                                written_files=_written_files,
                                dtool_msgs=_dtool_msgs, ws_str=_ws_str,
                                verify_mutation_serial=_verify_mutation_serial,
                                state=state, verify_warned=_verify_warned,
                                verify_last_ok_serial=_verify_last_ok_serial,
                                verify_gate_blocks=_verify_gate_blocks,
                                completion_unverified=_completion_unverified,
                                duo_deadline_at=_duo_deadline_at,
                                grace_round_active=_grace_round_active,
                            )
                            async for _ev in _duo_task_complete_gate(ctx, _tc_ps):
                                yield _ev
                            _verify_warned = _tc_ps.verify_warned
                            _verify_last_ok_serial = _tc_ps.verify_last_ok_serial
                            _verify_gate_blocks = _tc_ps.verify_gate_blocks
                            _completion_unverified = _tc_ps.completion_unverified
                            _duo_deadline_at = _tc_ps.duo_deadline_at
                            _grace_round_active = _tc_ps.grace_round_active
                            if _tc_ps.action == "break":
                                break
                        _total_tool_rounds += 1
                        _lifetime_tool_rounds += 1
                        # Exploration-loop detection: track rounds with only explore tools
                        _round_tool_names = {
                            tc.get("function", {}).get("name", "") for tc in (_dr_tcs or [])
                        }
                        if _round_tool_names and _round_tool_names.issubset(_EXPLORE_TOOLS):
                            # read_file alternating, Astro structure backend→frontend→
                            _new_explore = _collect_new_explore_paths(_dr_tcs, _seen_explore_paths)
                            if not _new_explore:
                                _explore_only_rounds += 1
                            _total_exports = sum(len(c.get("exports", [])) for c in (_contracts_raw or []))
                            _any_fallback = any(c.get("_fallback") for c in (_contracts_raw or []) if isinstance(c, dict))
                            _explore_was_partial = not _explore_ctx or _total_exports < 3 or _any_fallback
                            _partial_bonus = 6 if _explore_was_partial else 4
                            _READ_ONLY_THRESHOLD = _partial_bonus if _total_exports == 0 else (5 if _any_fallback else _partial_bonus)
                            _READ_ONLY_THRESHOLD = max(
                                2, _READ_ONLY_THRESHOLD + _explore_size_tolerance(exec_mdl)
                            )
                            if ctx.duo_config.until_finished:
                                _READ_ONLY_THRESHOLD *= 2
                            # EXPLORE-METER (2026-09-20): this used to log
                            # "[LOOP-DETECT] _explore_only_rounds=0 ..." on
                            # WARNING after EVERY read round — looked like a
                            # loop alarm during perfectly healthy chunked
                            # reads. Only report once the streak climbs.
                            if _explore_only_rounds:
                                logger.warning("[EXPLORE-METER] explore_only_rounds=%d/%d (exports=%d partial=%s fallback=%s)",
                                               _explore_only_rounds, _READ_ONLY_THRESHOLD,
                                               _total_exports, _explore_was_partial, _any_fallback)
                            if _explore_only_rounds >= _READ_ONLY_THRESHOLD:
                                await ctx.emit({"type": "status",
                                    "content": f"⚠ Aborting: {_explore_only_rounds} explore-only rounds without progress."})
                                await ctx.emit({"type": "token",
                                    "content": f"\n⚠ [Until-Finished] {_explore_only_rounds} consecutive explore-only rounds — no progress. Stopping."})
                                _ld_setter(3408); _loop_detected = True
                                break
                        elif _round_tool_names & _WRITE_TOOLS:
                            _bash_changed = None
                            if _round_tool_names == {"run_bash"} and _snap_before is not None:
                                _snap_after = await asyncio.to_thread(
            _ld_file_snapshot, _ws_str, _explore_ctx, _pre_explore_msgs,
            _written_files, _contracts_raw)
                                from hive_functions.chunking import compute_bash_changed
                                _bash_changed = compute_bash_changed(_snap_before, _snap_after)
                            from hive_functions.chunking import resolve_explore_reset
                            _explore_only_rounds = resolve_explore_reset(
                                _round_tool_names, _bash_changed, _explore_only_rounds
                            )
                        if _round_wrote_file:
                            _no_write_rounds = 0
                            _no_write_nudged = False
                        else:
                            _no_write_rounds += 1
                        _round_wrote_file = False
                        _no_write_threshold = 14 if ctx.duo_config.until_finished else 8
                        if _no_write_rounds >= _no_write_threshold and not _no_write_nudged:
                            _no_write_nudged = True
                            _nudge = (
                                f"[NO PROGRESS] {_no_write_rounds} tool round(s) without any file edit. "
                                "If you are stuck diagnosing an issue, verify your last edit did not "
                                "corrupt a file (re-read it with read_file), or call "
                                "task_complete(status='blocked', reason='...') instead of looping."
                            )
                            _dtool_msgs.append({"role": "user", "content": _nudge})
                            await ctx.emit({"type": "status",
                                "content": f"⚠ {_no_write_rounds} rounds without file changes — hint injected."})
                        ctx.exec_ctrl.sync_tool_rounds(_total_tool_rounds)
                        if not _limit_warned and not ctx.duo_config.until_finished:
                            _rounds_left = _max_tool_rounds - _total_tool_rounds
                            if 1 <= _rounds_left <= 2:
                                _limit_warned = True
                                _dtool_msgs.append({"role": "user", "content": (
                                    f"[ROUND LIMIT] Only {_rounds_left} tool round(s) remaining. "
                                    f"Prioritize: finish the current file edit, verify with run_bash, "
                                    f"then stop. Do NOT start new large operations."
                                )})
                        _tool_round_durations.append(round(max(0.0, time.monotonic() - _round_t0), 3))
                        # PATCH-1: Plan tracker — update progress per tool round
                        # extended with deviation detection: hard/soft rules, streak tracking,
                        if _plan_tracker is not None and not _plan_tracker.is_finished:
                            if _last_tool_name and _plan_tracker.should_advance(
                                    _last_tool_name, _last_tool_result,
                                    touched_paths=list((_file_changes or {}).keys())):
                                _plan_tracker.advance(_last_tool_name)
                                logger.info(
                                    "[PlanTracker] Step advanced after '%s' -> step %d/%d",
                                    _last_tool_name,
                                    _plan_tracker._current_step_index() + 1,
                                    _plan_tracker.total,
                                )
                            _touched = list(_file_changes.keys()) if _file_changes else []
                            _plan_tracker.tick(
                                tool_name=_last_tool_name,
                                tool_result=_last_tool_result,
                                touched_paths=_touched,
                                file_changes_count=len(_file_changes or {}),
                            )
                            if _plan_tracker.needs_replan():
                                _replan_count += 1
                                if _replan_count >= 4:
                                    _plan_tracker._stall_ticks += 1
                                    if _plan_tracker._stall_ticks >= 6:
                                        yield await ctx.emit({
                                            "type": "status",
                                            "content": (
                                                "⛔ [CRITICAL STALL] Maximum replans reached, "
                                                "no progress detected for 6+ rounds. "
                                                "Call task_complete(status='blocked', reason='max_replans_exhausted')."
                                            ),
                                        })
                                        _dtool_msgs.append({"role": "user", "content": (
                                            "[CRITICAL STALL] All replans exhausted without progress. "
                                            "Call task_complete(status='blocked', reason='max_replans_exhausted') "
                                            "immediately. Do NOT read or write files."
                                        )})
                                    else:
                                        yield await ctx.emit({
                                            "type": "status",
                                            "content": (
                                                f"⛔ Replan cap reached ({_replan_count}/4) — "
                                                "tracking stalls ({_plan_tracker._stall_ticks}/6). "
                                                "No further replan directives."
                                            ),
                                        })
                                    _plan_tracker._initial_read_phase = False
                                    _plan_tracker._plan.deviation.status = "NONE"
                                    _plan_tracker._plan.deviation.streak = 0
                                    _plan_tracker._plan.deviation.reasons.clear()
                                    continue
                                _replan_msg = _plan_tracker.replan_prompt(
                                    written_files=_written_files,
                                    file_changes=_file_changes,
                                )
                                _streak = _plan_tracker._plan.deviation.streak
                                logger.warning(
                                    "[PlanTracker] Deviation streak %d -- replan triggered. Last tool: '%s'",
                                    _streak,
                                    _last_tool_name,
                                )
                                yield await ctx.emit({
                                    "type": "status",
                                    "content": (
                                        f"⚠️ Plan deviation streak {_streak}/3 — "
                                        "injecting replan directive"
                                    ),
                                })
                                if not _replan_bonus_granted:
                                    _replan_bonus_granted = True
                                    _bonus = min(4, _max_tool_rounds_cfg // 4)
                                    _max_tool_rounds = min(_max_tool_rounds + _bonus, _max_tool_rounds_cfg)
                                    _accumulated_replan_bonus += _bonus
                                    logger.info("[REPLAN] Granted %d extra rounds. New budget: %d",
                                                _bonus, _max_tool_rounds)
                                if _dtool_msgs and _dtool_msgs[-1].get("role") == "tool":
                                    from hive_functions.prompts import PROMPTS
                                    # [RUNTIME NOTICE]-fix above).
                                    _replan_directive = PROMPTS.get("duo_coder_replan", "")
                                    if _replan_directive:
                                        _dtool_msgs.append({
                                            "role": "user",
                                            "content": _replan_directive,
                                        })
                                    _dtool_msgs.append({
                                        "role": "user",
                                        "content": _replan_msg,
                                    })
                                # Reset deviation state so tracker doesn't re-trigger
                                # on the next round. If the model truly is off-plan,
                                # deviations will re-emerge naturally.
                                _plan_tracker._plan.deviation.status = "NONE"
                                _plan_tracker._plan.deviation.streak = 0
                                _plan_tracker._plan.deviation.reasons.clear()
                                _plan_tracker._initial_read_phase = True
                                _plan_tracker._initial_reads = 0
                                ctx.exec_ctrl.reset_stuck_detection()
                            else:
                                _p1_reminder = _plan_tracker.reminder()
                                if _p1_reminder:
                                    logger.info(
                                        "[PlanTracker] Reminder: %s",
                                        (_p1_reminder[:120] + '...') if len(_p1_reminder) > 120 else _p1_reminder,
                                    )
                                if (_p1_reminder
                                        and _dtool_msgs
                                        and _dtool_msgs[-1].get("role") == "tool"
                                        and not _plan_tracker.is_finished):
                                    _dtool_msgs.append({
                                        "role": "user",
                                        "content": _p1_reminder.strip(),
                                    })
                        if (not _loop_detected and not ctx.aborted()
                                and _total_tool_rounds >= _max_tool_rounds):
                            if _verify_mutation_serial > _verify_last_ok_serial and _file_changes:
                                _uv_files = ", ".join(sorted(_file_changes.keys())[:10])
                                _uv_count = len(_file_changes)
                                if _uv_count > 10:
                                    _uv_files += f" and {_uv_count - 10} more"
                                yield await ctx.emit({
                                    "type": "status",
                                    "content": f"\u26a0\ufe0f Budget exhausted with {_uv_count} unverified file(s): {_uv_files} — changes may be broken.",
                                })
                            if not _grace_round_active:
                                # GRACE-ROUND-FIX: the round loop runs two extra
                                # slots; this arms the first one. The old code set
                                # _loop_detected + abort here, so the grace prompt
                                # was appended but the round never ran.
                                _grace_round_active = True
                                _max_tool_rounds += 1
                                _dtool_msgs.append({"role": "user", "content": (
                                    "[GRACE ROUND] You have 1 final round. "
                                    "Call task_complete now with what was completed, "
                                    "any blockers, and build_status. No other tool calls."
                                )})
                                yield await ctx.emit({
                                    "type": "status",
                                    "content": f"\u23f8 Tool budget exhausted ({_total_tool_rounds}/{_max_tool_rounds - 1} rounds) \u2014 granting one grace round to wrap up.",
                                })
                            else:
                                # CHUNK-CONTAINMENT: grace round spent without
                                # task_complete -> end THIS chunk only. No
                                # exec_ctrl.abort anymore: the run continues with
                                # the next chunk (gate after the round loop).
                                _chunk_budget_exhausted = True
                                _ld_setter(3551)
                                yield await ctx.emit({
                                    "type": "status",
                                    "content": f"\u23f9 Tool budget exhausted ({_total_tool_rounds}/{_max_tool_rounds - 1} rounds incl. grace) \u2014 ending this chunk.",
                                })
                                break
                except Exception as _dce:
                    if isinstance(_dce, (
                        GeneratorExit,
                        asyncio.CancelledError,
                        KeyboardInterrupt,
                        SystemExit,
                    )):
                        raise
                    logger.debug(
                        "[DUO] Suppressed error at coder dce outer: %s",
                        _dce, exc_info=True
                    )
                    _dce_type = type(_dce).__name__
                    _dce_msg  = str(_dce) or "(no message)"
                    if _dce_type in ("ReadError", "RemoteProtocolError", "ConnectError", "ReadTimeout", "TimeoutException") and not ctx.aborted():
                        _connect_error_retries += 1
                        if _connect_error_retries > 3:
                            logger.error("[DUO] ConnectError retries exhausted (%d) — aborting tool loop", _connect_error_retries)
                            for _sse in _drain_thinking_rescue(_duo_loop):
                                yield _sse
                            yield await ctx.emit({"type": "status",
                                "content": f"⛔ Connection error after {_connect_error_retries} retries — stopping."})
                            _ld_setter(3601); _loop_detected = True
                            break
                        yield await ctx.emit({"type": "status",
                            "content": f"⚠️ Server connection error ({_dce_type}) — waiting and retrying ({_connect_error_retries}/3)…"})
                        await asyncio.sleep(3.0)
                        try:
                            from backend.llama_server_manager import manager as _lsm_retry
                            await _lsm_retry.evict(exec_mdl)
                            await _lsm_retry.ensure_loaded(exec_mdl, num_ctx=_dtool_opts.get("num_ctx", 4096), n_parallel=1, ctx_graceful=False, pin=True)
                            _dport = _cached_coder_port or await _lsm_retry.ensure_loaded(exec_mdl, num_ctx=_dtool_opts.get("num_ctx", 4096), n_parallel=1, ctx_graceful=False, pin=True)
                            ctx.exec_ctrl.sync_tool_rounds(_total_tool_rounds)
                            continue
                        except Exception as _retry_err:
                            if isinstance(_retry_err, (
                                GeneratorExit,
                                asyncio.CancelledError,
                                KeyboardInterrupt,
                                SystemExit,
                            )):
                                raise
                            logger.debug(
                                "[DUO] Suppressed error at coder dce retry inner: %s",
                                _retry_err, exc_info=True
                            )
                            _dce_msg = f"{_dce_msg} | retry failed: {str(_retry_err)[:60]}"
                    _err = f"[Tool round error ({_dce_type}): {_dce_msg[:120]}]"
                    _parts.append(_err)
                    for _sse in _drain_thinking_rescue(_duo_loop):
                        yield _sse
                    yield await ctx.emit({"type": "token", "content": _err})
                    _tool_round_runtime_error = True
                    _tool_round_error_text = _err
                    _ld_setter(3636); _loop_detected = True
                finally:
                    if _dtc_owned and _dtc is not None:
                        try:
                            await _dtc.aclose()
                        except Exception:
                            pass

                if _file_changes:
                    _created = sum(1 for v in _file_changes.values() if v.get("op") == "created")
                    _rewrote = sum(1 for v in _file_changes.values() if v.get("op") == "rewrote")
                    _edited = sum(1 for v in _file_changes.values() if v.get("op") == "edited")
                    _appended = sum(1 for v in _file_changes.values() if v.get("op") == "append")
                    _total_added = sum(v.get("lines_added", 0) for v in _file_changes.values())
                    _total_removed = sum(v.get("lines_removed", 0) for v in _file_changes.values())
                    yield await ctx.emit({"type": "files_summary",
                                          "files": [{"path": p, **v}
                                                    for p, v in _file_changes.items()],
                                          "n_files": len(_file_changes),
                                          "summary": {
                                              "created": _created,
                                              "edited": _edited,
                                              "rewrote": _rewrote,
                                              "appended": _appended,
                                              "lines_added": _total_added,
                                              "lines_removed": _total_removed,
                                          }})
            else:
                for _coder_attempt in range(2):
                    _parts_attempt = []
                    try:
                        async for _tok in ctx.pipeline_chat_stream(coder_mdl, _coder_msgs, _duo_coder_temp, _duo_coder_tok,
                                                                   agent_role="duo_coder",
                                                                   force_ctx=_runtime_ctx_target,
                                                                    think=_coder_tool_think if _exec_supports_thinking else None):
                            if ctx.aborted() or (ctx.chat_id and ctx.is_aborted_chat(ctx.chat_id)):
                                break
                            _parts_attempt.append(_tok)
                            yield await ctx.emit({"type": "token", "content": _tok})
                        _parts.extend(_parts_attempt)
                        break
                    except Exception as _ce:
                        if _coder_attempt == 0 and _is_retryable_ollama_err(_ce):
                            yield await ctx.emit({"type": "status",
                                              "content": f"⚠️ Coder error ({str(_ce)[:60]}), retrying in 3s…"})
                            await asyncio.sleep(3.0)
                        else:
                            _err = f"[Coder error: {str(_ce)[:120]}]"
                            _parts.append(_err)
                            yield await ctx.emit({"type": "token", "content": _err})
                            break

            coder_out = "".join(_parts).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled
            if _is_tool_round:
                coder_out = re.sub(r"\n🔧 `[^\n]+`\n", "\n", coder_out).strip()
                coder_out = coder_out.strip()
                coder_out = re.sub(r"\[(?:Written|Datei geschrieben|patch_file|edit_file|run_bash|find_files)[^\]]*\]", "", coder_out).strip()
            if _is_tool_round:
                _parts_raw = "".join(_parts)
                _written_now_write = re.findall(r"\[(?:Datei geschrieben|Written|Appended): ([^\(\]\s]+)", _parts_raw)
                _written_now_patch = re.findall(r"\[patch_file: ([^\s]+) patched", _parts_raw)
                _written_now_edit = re.findall(r"\[(?:edit_file|write_file): (?:created '|rewrote '|')([^']+)'", _parts_raw)
                _all_written_now = _written_now_write + _written_now_patch + _written_now_edit
                if _all_written_now:
                    _file_parts_critic = []
                    for _wp in _all_written_now[-3:]:
                        _wp = _wp.strip()
                        if not _wp:
                            continue
                        try:
                            # SIZE-CAP (2026-11-07 audit): the critic only
                            # consumes the first 2000 chars; read_text() pulled
                            # whole generated files every write round.
                            with Path(_wp).open("r", encoding="utf-8",
                                                errors="replace") as _fh:
                                _fc = _fh.read(2000)
                            if _fc:
                                _file_parts_critic.append(f"# {_wp}\n{_fc}")
                        except Exception as _err:
                            if isinstance(_err, (
                                GeneratorExit,
                                asyncio.CancelledError,
                                KeyboardInterrupt,
                                SystemExit,
                            )):
                                raise
                            logger.debug(
                                "[DUO] Suppressed error at critic file-read: %s",
                                _err, exc_info=True
                            )
                    if _file_parts_critic:
                        coder_out = "\n\n".join(_file_parts_critic)
                    elif _written_now_write:
                        try:
                            _file_content = Path(_written_now_write[-1].strip()).read_text(encoding="utf-8", errors="replace")
                            if _file_content:
                                coder_out = _file_content
                        except Exception as _err:
                            if isinstance(_err, (
                                GeneratorExit,
                                asyncio.CancelledError,
                                KeyboardInterrupt,
                                SystemExit,
                            )):
                                raise
                            logger.debug(
                                "[DUO] Suppressed error at critic file-read fallback: %s",
                                _err, exc_info=True
                            )
                            pass
            yield await ctx.emit({"type": "duo_coder_done", "elapsed": round(time.time() - _t, 1)})
            if _file_changes or _written_files:
                try:
                    await _pre_explore_cache_invalidate_workspace(_ws_str)
                except Exception as _err:
                    if isinstance(_err, (
                        GeneratorExit,
                        asyncio.CancelledError,
                        KeyboardInterrupt,
                        SystemExit,
                    )):
                        raise
                    logger.debug(
                        "[DUO] Suppressed error at cache invalidation: %s",
                        _err, exc_info=True
                    )
            if _subtask:
                if coder_out and coder_out not in _coder_outputs:
                    _coder_outputs.append(coder_out)

            if _tool_round_runtime_error:
                _duo_hard_stop = True
                final_verdict = final_verdict or "tool_round_error"
                yield await ctx.emit({
                    "type": "status",
                    "content": "⛔ Tool round error detected — stopping the run immediately.",
                })
                if not coder_out and _tool_round_error_text:
                    coder_out = _tool_round_error_text
                _remaining_items_hs = [
                    {"title": str(t)} for t in _loop_items[_di:]
                ]
                if ctx.chat_id and _remaining_items_hs:
                    _write_resume_block(
                        chat_id=ctx.chat_id,
                        workspace=_ws_str,
                        chunks_total=_n_items,
                        chunks_done=_done_tasks,
                        chunks_remaining=_remaining_items_hs,
                        written_files=_written_files,
                        last_summary=" | ".join(_done_tasks),
                        plan_msgs=[],
                        explore_ctx=_explore_ctx,
                        halt_reason="tool_round_error",
                        pending_send_files=list(_pending_send_files),
                    )
                    yield await ctx.emit({
                        "type": "status",
                        "content": (
                            f"⚠ Resume block saved ({len(_remaining_items_hs)} chunks "
                            "remaining) — a new run continues from the failed chunk."
                        ),
                    })
                break

            #     ctx.exec_ctrl.abort(STUCK_IN_LOOP) → loop-head HALTED guard (fix A).
            # LOOP-DETECTED-OUTER-FIX: two loop-detect sources set _loop_detected:
            # (1) stuck/read-count path (:2419/:2430/:2448) — additionally with
            #     ctx.exec_ctrl.abort(STUCK_IN_LOOP) → loop-head HALTED guard (fix A).
            # (2) explore-counter path (:3090-3096) — ONLY _loop_detected + break,
            #     NO exec_ctrl.abort (state stays non-HALTED).
            # Both end here: without this guard the inner tool-loop break would
            # fall through to the critic block and the next subtask/retry would run.
            # Fix: break immediately after the critic-relevant cleanup when _loop_detected.
            if _chunk_budget_exhausted:
                # CHUNK-CONTAINMENT: this chunk spent its whole budget (incl.
                # grace round) without task_complete. Move on to the next chunk
                # instead of halting the run (old behavior: MAX_TOOL_ROUNDS
                # aborted exec_ctrl -> head guard ended the entire run).
                if _di < _n_items - 1:
                    yield await ctx.emit({"type": "status",
                        "content": f"⏭ Chunk {_di+1} ended (tool budget exhausted) — moving to chunk {_di+2}/{_n_items}"})
                    _cs.reset_test_retries()
                    _di += 1
                    # CHUNK-PERSIST (2026-10-14): the containment decision is
                    # final - the resume block must reflect it, or a hard kill
                    # re-runs the deliberately skipped chunk.
                    _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, "chunk_checkpoint")
                    continue
                yield await ctx.emit({"type": "status",
                    "content": "⏹ Tool budget exhausted — last chunk ended."})
                break
            if _loop_detected:
                final_verdict = final_verdict or "loop_detected"
                # multiple setters stamp finished runs as loop_detected (observed live
                try:
                    _ld_locals = locals()
                    logger.warning(
                        "[LOOP-DETECT-STOP] timed_out=%s think_only=%s compress_streak=%s "
                        "explore_only=%s runtime_error=%s grace=%s verify_warned=%s invalid_tool=%s "
                        "force_compress=%s dropped_tool_retries=%s slot_ctx_floor=%s drop_names=%s last_msgs=%s",
                        _duo_timed_out,
                        _ld_locals.get("_dr_think_only_retries", "n/a"),
                        _ld_locals.get("_compress_fail_streak", "n/a"),
                        _ld_locals.get("_explore_only_rounds", "n/a"),
                        _ld_locals.get("_tool_round_runtime_error", False),
                        _ld_locals.get("_grace_round_active", False),
                        _ld_locals.get("_verify_warned", False),
                        _ld_locals.get("_dr_invalid_tool_retries", "n/a"),
                        _ld_locals.get("_force_compress_next", "n/a"),
                        _ld_locals.get("_dr_dropped_tool_retries", "n/a"),
                        _ld_locals.get("_slot_ctx_floor_stop", False),
                        _ld_locals.get("_drop_names", []),
                        [str(m.get("content", ""))[:80] for m in (_dtool_msgs or [])[-8:]],
                    )
                except Exception:
                    pass
                _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, "wedge_escalated" if _wedge_escalated
                                  else ("timeout_guard" if _duo_timed_out else "loop_detected"))
                break

            _is_last = (_di == _n_items - 1)

            if _duo_timed_out:
                final_verdict = final_verdict or "timeout_guard"
                _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, "timeout_guard")
                break

            # ── SKIP-CHECK: Skip Critic (auto-approve current chunk) ──────
            # If skip was pressed during/after Coder, skip Critic review
            # and auto-approve → move to next chunk or finish.
            if ctx.step_skipped() and not ctx.aborted():
                ctx.clear_step_skip()
                critic_issues = []
                final_verdict = "Skipped (⏭)"
                if _subtask:
                    _cs.mark_chunk_done(_subtask)
                yield await ctx.emit({"type": "duo_critic_done",
                                  "elapsed": 0,
                                  "verdict": {"approved": True, "issues": [], "verdict": "Skipped"},
                                  "approved": True})
                yield await ctx.emit({"type":"status",
                    "content": f"⏭ Critic skipped — chunk {_di+1} auto-approved"})
                if _is_last:
                    break
                _di += 1
                _cs.reset_test_retries()
                _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, "chunk_checkpoint")
                continue

            # ── Critic ─────────────────────────────────────────────────────
            # Critic removed from agentic: same-model review is a no-op (same blind spots),
            # and separate critic model doesn't fit in VRAM. Auto-Test self-fix loop
            # provides deterministic quality control after each chunk.
            if ctx.duo_config.agentic_mode:
                final_verdict = "Agentic — Auto-Test QC"
                if _subtask and _subtask not in _done_tasks:
                    _done_tasks.append(_subtask)
                # ── Auto-Test Self-Fix (Agentic Path) ──────────────────────
                # Delegates to ChunkState for the self-fix/re-awaken pattern.
                critic_issues = []  # CROSS-CHUNK-CLEANUP
                _duo_test_feedback = bool(ctx.duo_config.test_feedback_chunk)
                # SKIP-CHECK: Skip auto-test if skip pressed during Coder
                if ctx.step_skipped() and not ctx.aborted():
                    ctx.clear_step_skip()
                    yield await ctx.emit({"type":"status",
                            "content": f"⏭ Auto-test skipped for chunk {_di+1}"})
                elif _duo_test_feedback and _cs.has_written_files and not ctx.aborted():
                    _ws_path = _eff_run_ws(getattr(ctx, "workspace", ""), state.get("_ws_str", ""))
                    _chunk_test_result, _is_clean = await _cs.run_auto_test(
                        workspace=_ws_path,
                        di=_di,
                        n_items=_n_items,
                        emit_fn=ctx.emit,
                        test_timeout=90,
                    )
                    if _is_clean:
                        yield await ctx.emit({"type": "status",
                            "content": f"\u2705 Tests passed ({_chunk_test_result.language})"})
                    else:
                        _action = _cs.handle_auto_test_result(
                            test_result=_chunk_test_result,
                            subtask=_subtask,
                            di=_di,
                            n_items=_n_items,
                            emit_fn=ctx.emit,
                            chat_id=ctx.chat_id or "",
                        )
                        if _action == ChunkAction.CONTINUE:
                            # Check for flaky-test rerun without LLM
                            if _cs.rerun_without_fix:
                                _cs.rerun_without_fix = False
                                yield await ctx.emit({"type": "status",
                                    "content": f"🔁 Flaky test detected — re-running without LLM ({_cs.test_retries}/2)"})
                                continue  # re-run auto-test on same chunk, no LLM call
                            # RE-AWAKEN: Stay on same chunk, fix test failures
                            yield await ctx.emit({"type": "status",
                                "content": f"\U0001f501 Chunk {_di+1}: {_chunk_test_result.failure_count} test failures — fix round ({_cs.test_retries}/{_cs.max_test_retries})"})
                            continue  # DON'T increment _di — re-process same chunk
                        else:
                            # Max retries reached or INCREMENT
                            _cs.rerun_without_fix = False
                            yield await ctx.emit({"type": "status",
                                "content": f"\u26a0\ufe0f Chunk {_di+1}: test failures remain after {_cs.max_test_retries+1} attempts — note and continue"})
                _ac_out = await _auto_commit_chunk(ctx.user_input, _subtask, _ws_str,
                                                   ctx.duo_config.git_autocommit,
                                                   files=list(_cs.written_files[-20:]),
                                                   commit_allowed=getattr(ctx, "workspace_commit_allowed", True))
                if _ac_out:
                    yield await ctx.emit({"type": "status", "content": _ac_out})
                _di += 1
                _cs.reset_test_retries()
                _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, "chunk_checkpoint")
                continue

            # HIGH-CONFIDENCE AUTO-APPROVE:
            _coder_lines = len(coder_out.splitlines())
            _auto_approve_limit = 25 if ctx.duo_config.coding_mode else 60
            _auto_approve = (
                _di == 0
                and not critic_issues
                and _coder_lines < _auto_approve_limit    # short output
                and _total_edit_lines <= _auto_approve_limit
            )
            if _auto_approve:
                critic_issues = []
                final_verdict = "Auto-approved (short output, first round)"
                yield await ctx.emit({"type": "duo_critic_done",
                                  "elapsed": 0,
                                  "verdict": {"approved": True, "issues": [], "verdict": "Auto-approved"},
                                  "approved": True})
                # CHUNK-DONE TIMING (2026-10-27): mark AFTER the auto-test,
                # not before - a failing test re-processes this chunk, and a
                # pre-marked chunk would count as done while broken.
                _ac_out = await _auto_commit_chunk(ctx.user_input, _subtask, _ws_str,
                                                   ctx.duo_config.git_autocommit,
                                                   files=list(_cs.written_files[-20:]),
                                                   commit_allowed=getattr(ctx, "workspace_commit_allowed", True))
                if _ac_out:
                    yield await ctx.emit({"type": "status", "content": _ac_out})
                if _subtask:
                    _cs.mark_chunk_done(_subtask)
                if _is_last:
                    break
                _di += 1
                _cs.reset_test_retries()
                _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, "chunk_checkpoint")
                continue
            _critic_ps = _DuoCriticReviewState(
                parts=_parts, verify_last_ok_serial=_verify_last_ok_serial,
                critic_issues=critic_issues, final_verdict=final_verdict,
            )
            try:
                async for _ev in _duo_critic_review(
                    ctx, _critic_ps,
                    coder_mdl=coder_mdl, coder_out=coder_out, critic_mdl=critic_mdl,
                    _ws_str=_ws_str, _di=_di, _subtask=_subtask,
                    _duo_critic_sys=_duo_critic_sys, _duo_critic_temp=_duo_critic_temp,
                    _duo_critic_tok=_duo_critic_tok, _critic_profile=_critic_profile,
                    _critic_thinking=_critic_thinking, _critic_tools_enabled=_critic_tools_enabled,
                    _verify_mutation_serial=_verify_mutation_serial,
                ):
                    yield _ev
            finally:
                _parts = _critic_ps.parts
                _t = _critic_ps.t
                _verify_last_ok_serial = _critic_ps.verify_last_ok_serial
                critic_issues = _critic_ps.critic_issues
                final_verdict = _critic_ps.final_verdict
                _approved = _critic_ps.approved
                _verify_gate_blocked = _critic_ps.verify_gate_blocked

            # CRITIC-GATE FAIRNESS (2026-10-27): the old `_approved or _is_last`
            # waved the LAST chunk through unreviewed - exactly the chunk that
            # usually carries the integration work. The last chunk needs the
            # same approval as every other; `_is_last` only removes the
            # continue-to-next-chunk part, not the gate.
            _can_finish_round = _approved and not _verify_gate_blocked
            if _can_finish_round:
                if _subtask:
                    critic_issues = _cs.critic_issues  # cleared by mark_chunk_done
                    _rem = _n_items - _di - 1
                    yield await ctx.emit({"type":"status",
                        "content":f"\u2713 {_di+1}/{_n_items}: {_subtask[:50]}" + (f" ({_rem} more)" if _rem else " (done)")})
                    # B8.3 AUTO-COMMIT: chunk completion (critic approve). Fires
                    _ac_out = await _auto_commit_chunk(ctx.user_input, _subtask, _ws_str,
                                                       ctx.duo_config.git_autocommit,
                                                       files=list(_cs.written_files[-20:]),
                                                       commit_allowed=getattr(ctx, "workspace_commit_allowed", True))
                    if _ac_out:
                        yield await ctx.emit({"type": "status", "content": _ac_out})
                    # CHUNK-DONE TIMING (2026-10-27): the chunk is marked done
                    # AFTER the auto-test verdict below - a failing test
                    # re-processes it; a pre-marked chunk would count as done
                    # while broken. Also: mark_chunk_done clears critic_issues
                    # (kept by reading them here once, before the test).
                    critic_issues = _cs.critic_issues
                    # ── P3: Auto-Test after chunk completion (Self-Awakening) ──────────
                    # Delegates to ChunkState for the self-fix/re-awaken pattern.
                    _duo_test_feedback = bool(ctx.duo_config.test_feedback_chunk)
                    # SKIP-CHECK: Skip auto-test if skip pressed after Critic
                    if ctx.step_skipped() and not ctx.aborted():
                        ctx.clear_step_skip()
                        yield await ctx.emit({"type":"status",
                        "content": f"⏭ Auto-test skipped for chunk {_di+1}"})
                        _di += 1
                        _cs.reset_test_retries()
                        _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, "chunk_checkpoint")
                        if _is_last:
                            break
                    elif _duo_test_feedback and _cs.has_written_files and not ctx.aborted():
                        _ws_path = _eff_run_ws(getattr(ctx, "workspace", ""), state.get("_ws_str", ""))
                        _chunk_test_result, _is_clean = await _cs.run_auto_test(
                            workspace=_ws_path,
                            di=_di,
                            n_items=_n_items,
                            emit_fn=ctx.emit,
                            test_timeout=90,
                            chat_id=ctx.chat_id or "",
                        )
                        if _is_clean:
                            yield await ctx.emit({"type": "status",
                                "content": f"\u2705 Tests passed ({_chunk_test_result.language})"})
                            # CHUNK-DONE TIMING (2026-10-27): the verdict is
                            # clean - NOW the chunk counts as done.
                            if _subtask:
                                _cs.mark_chunk_done(_subtask)
                            _di += 1
                            _cs.reset_test_retries()
                            _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, "chunk_checkpoint")
                            if _is_last:
                                break
                        else:
                            _action = _cs.handle_auto_test_result(
                                test_result=_chunk_test_result,
                                subtask=_subtask,
                                di=_di,
                                n_items=_n_items,
                                emit_fn=ctx.emit,
                                chat_id=ctx.chat_id or "",
                            )
                            if _action == ChunkAction.CONTINUE:
                                if _cs.rerun_without_fix:
                                    _cs.rerun_without_fix = False
                                    yield await ctx.emit({"type": "status",
                                        "content": f"🔁 Flaky test detected — re-running without LLM ({_cs.test_retries}/2)"})
                                    continue
                                # RE-AWAKEN: Stay on same chunk, fix test failures
                                yield await ctx.emit({"type": "status",
                                "content": f"\U0001f501 Chunk {_di+1}: {_chunk_test_result.failure_count} test failures — fix round ({_cs.test_retries}/{_cs.max_test_retries})"})
                                continue  # DON'T increment _di
                            else:
                                # Max retries reached — mark as completion note, move on
                                _cs.rerun_without_fix = False
                                yield await ctx.emit({"type": "status",
                                "content": f"\u26a0\ufe0f Chunk {_di+1}: test failures persist after {_cs.max_test_retries+1} attempts — note and continue"})
                                _di += 1
                                _cs.reset_test_retries()
                                _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, "chunk_checkpoint")
                                if _is_last:
                                    break
                    else:
                        # No auto-test or no files written — normal completion
                        _di += 1
                        _cs.reset_test_retries()
                        _halt_fn(ctx, _ws_str, _explore_ctx, _done_tasks, _written_files, _di, _loop_items, _n_items, "chunk_checkpoint")
                        if _is_last:
                            break
                else:
                    _status = f"✓ Critic: code approved — round {_di + 1}" if _approved else f"⚑ Last pass — round {_di + 1} completed"
                    yield await ctx.emit({"type": "status", "content": _status})
                    break
            elif _verify_gate_blocked:
                if get_transaction().has_changes:
                    ctx.exec_ctrl.transition(AgentState.ROLLBACK)
                    _restored = get_transaction().rollback()
                    yield await ctx.emit({
                        "type": "status",
                        "content": f"\u23ea Rollback: changes discarded ({len(_restored)} file(s)), because verification failed.",
                    })
                yield await ctx.emit({
                    "type": "status",
                    "content": "\u26d4 Verification required: run_bash must pass after the latest file changes.",
                })
                if _subtask:
                    _cs.set_verification_fix_override(_subtask)
            elif _subtask and not _approved:
                if get_transaction().has_changes:
                    ctx.exec_ctrl.transition(AgentState.ROLLBACK)
                    _restored = get_transaction().rollback()
                    yield await ctx.emit({
                        "type": "status",
                        "content": f"\u23ea Rollback: changes discarded ({len(_restored)} file(s)), because critic rejected.",
                    })
                yield await ctx.emit({"type":"status",
                    "content":f"\u21bb {_di+1}/{_n_items}: {_subtask[:50]} — issues found, fix round running…"})
                _cs.set_critic_fix_override(_subtask, critic_issues)
                critic_issues = _cs.critic_issues  # cleared by set_critic_fix_override

        # SPLIT 2.3 (2026-11-05): run-timeout token + final verification hard
        # stop moved to core/duo_finalgate._duo_final_verify_gate (state syncs
        # `_duo_hard_stop`, `final_verdict`, `critic_issues` back).
        _fg_ps = _DuoFinalGateState(
            _duo_timed_out=_duo_timed_out,
            _written_files=_written_files,
            _verify_mutation_serial=_verify_mutation_serial,
            _verify_last_ok_serial=_verify_last_ok_serial,
            _verify_warned=_verify_warned,
            _ws_str=_ws_str,
            _explore_ctx=_explore_ctx,
            _di=_di,
            _n_items=_n_items,
            _loop_items=_loop_items,
            _done_tasks=_done_tasks,
            _pending_send_files=_pending_send_files,
            critic_issues=critic_issues,
        )
        async for _ev in _duo_final_verify_gate(ctx, _fg_ps):
            yield _ev
        _duo_hard_stop = _fg_ps._duo_hard_stop
        final_verdict = _fg_ps.final_verdict
        critic_issues = _fg_ps.critic_issues

    except (GeneratorExit, asyncio.CancelledError):
        _park_ps = _DuoDisconnectParkState()
        try: _park_ps.loop_items = _loop_items
        except (NameError, UnboundLocalError): pass
        try: _park_ps.di = _di
        except (NameError, UnboundLocalError): pass
        try: _park_ps.done_tasks = _done_tasks
        except (NameError, UnboundLocalError): pass
        try: _park_ps.written_files = _written_files
        except (NameError, UnboundLocalError): pass
        try: _park_ps.explore_ctx = _explore_ctx
        except (NameError, UnboundLocalError): pass
        try: _park_ps.ws_str = _ws_str
        except (NameError, UnboundLocalError): pass
        try: _park_ps.n_items = _n_items
        except (NameError, UnboundLocalError): pass
        _duo_disconnect_park(ctx, _park_ps)
        raise

    finally:
        # CODE-MOTION (duo-runner-split): the cleanup body lives in
        # _duo_final_cleanup now; the state object carries every runner
        # local it reads. _planner_model is the only one bound inside the
        # mega-try, so it is ladder-collected exactly like the park fields
        # (the commit-2270557 NameError-ladder bug class).
        _cleanup_ps = _DuoFinalCleanupState(
            exec_mdl=exec_mdl,
            _run_id_global=_run_id_global,
            _duo_pinned=_duo_pinned,
            _project_state=_project_state,
            _project_state_run_counted=_project_state_run_counted,
        )
        try: _cleanup_ps._planner_model = _planner_model
        except (NameError, UnboundLocalError): pass
        await _duo_final_cleanup(ctx, _cleanup_ps)

    # ── P3: Final Test after all chunks ──────────────────────────
    async for _ev in _duo_final_test(ctx, state, _cs, _n_items, _eff_run_ws=_eff_run_ws):
        yield _ev

    # SPLIT 2.2 (2026-11-05): session epilogue (loop auto-summary, chat
    # context, read stats, run counter, soul trigger, ratings, file
    # summary, pipeline synthesis) moved to core/duo_epilogue. The single
    # session-write point (`ps.session_out`) stays here.
    _epi_ps = _DuoSessionEpilogueState(
        _ws_str=_ws_str,
        coder_out=coder_out,
        _cs=_cs,
        _written_files=_written_files,
        _duo_hard_stop=_duo_hard_stop,
        _duo_timed_out=_duo_timed_out,
        _loop_detected=_loop_detected,
        _lifetime_tool_rounds=_lifetime_tool_rounds,
        _explore_only_rounds=_explore_only_rounds,
        _est_tokens=_est_tokens,
        _parts=_parts,
        carry={
            "_plan_result": _plan_result,
            "_inloop_plan_text": _inloop_plan_text,
            "_touched_paths": _touched_paths,
            "_total_tool_errors_ref": _total_tool_errors_ref,
            "_last_run_bash_failure": _last_run_bash_failure,
            "coder_mdl": coder_mdl,
            "final_verdict": final_verdict,
            "run_peer_ratings": run_peer_ratings,
        },
    )
    async for _ev in _duo_session_epilogue(ctx, _epi_ps):
        yield _ev
    _duo_session_out = _epi_ps.session_out
    # SPLIT 2.1 (2026-11-05): the closing sequence (stop-reason ladder,
    # final auto-commit/push, checkpoint consolidation, resume safety net,
    # project save, run audit, send-file delivery, done, cleanup) moved
    # verbatim to core/duo_finalize._duo_run_finalize. `carry` threads the
    # runner-local bindings the tail also consumes; all fields sync back.
    _fin_ps = _DuoRunFinalizeState(
        _duo_hard_stop=_duo_hard_stop,
        _graceful_stopped=_graceful_stopped,
        _duo_timed_out=_duo_timed_out,
        _loop_detected=_loop_detected,
        _wedge_escalated=_wedge_escalated,
        _model_load_failed=_model_load_failed,
        _ws_str=_ws_str,
        _explore_ctx=_explore_ctx,
        _pending_send_files=_pending_send_files,
        _written_files=_written_files,
        _done_tasks=_done_tasks,
        _cs=_cs,
        _project_state=_project_state,
        _project_state_run_counted=_project_state_run_counted,
        _explore_only_rounds=_explore_only_rounds,
        _lifetime_tool_rounds=_lifetime_tool_rounds,
        _di=_di,
        _n_items=_n_items,
        _loop_items=_loop_items,
        carry={
            "exec_mdl": exec_mdl,
            "final_verdict": final_verdict,
            "critic_issues": critic_issues,
            "_recent_focus_paths": _recent_focus_paths,
            "_drop_names": _drop_names,
            "_dr_dropped_tool_retries": _dr_dropped_tool_retries,
            "_worker_slots": _worker_slots,
            "this_file": ctx.this_file,
            "_warm_workers_fn": _warm_workers_fn,
            "_keepalive_vision_fn": _keepalive_vision_fn,
        },
    )
    try:
        async for _ev in _duo_run_finalize(ctx, _fin_ps):
            yield _ev
    finally:
        _duo_hard_stop = _fin_ps._duo_hard_stop
        _graceful_stopped = _fin_ps._graceful_stopped
        _duo_timed_out = _fin_ps._duo_timed_out
        _loop_detected = _fin_ps._loop_detected
        _wedge_escalated = _fin_ps._wedge_escalated
        _model_load_failed = _fin_ps._model_load_failed
        _ws_str = _fin_ps._ws_str
        _explore_ctx = _fin_ps._explore_ctx
        _pending_send_files = _fin_ps._pending_send_files
        _written_files = _fin_ps._written_files
        _done_tasks = _fin_ps._done_tasks
        _cs = _fin_ps._cs
        _project_state = _fin_ps._project_state
        _project_state_run_counted = _fin_ps._project_state_run_counted
        _explore_only_rounds = _fin_ps._explore_only_rounds
        _lifetime_tool_rounds = _fin_ps._lifetime_tool_rounds
        _di = _fin_ps._di
        _n_items = _fin_ps._n_items
        _loop_items = _fin_ps._loop_items
