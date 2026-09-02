from __future__ import annotations
import asyncio, hashlib, json, logging, os, re, sys, time, uuid
from datetime import datetime
from pathlib import Path
import dataclasses
import httpx

logger = logging.getLogger("hivemind.duo")

# ── VRAM / Backend ──
from vram.loader import (
    _bk_evict, _bk_load, _get_loaded_models_set, _refresh_judge_keepalive,
)

# ── Learning ──
from learning.peer_ratings import run_peer_ratings

# ── Context ──
from context.chat import (
    _load_chat_context, _chat_context_valid, _mutate_chat_context, _save_chat_context,
)
from context.resume import _check_abort_and_maybe_save_resume, _write_resume_block, _load_resume_block
from infra.run_control import (
    is_graceful_stop_requested, clear_graceful_stop,
    is_pause_requested, clear_pause_request,
    get_resume_signal, get_abort_during_pause_signal,
    cleanup_pause_state as _cleanup_pause_state,
    _get_abort_event,
)
from context.pause_state import (
    persist_pause_state as _persist_pause_state,
    clear_pause_state as _clear_pause_state,
)
from infra.ask_user_governor import cleanup_governor as _cleanup_governor
from context.compression import _evict_stale_tool_outputs, _compress_tool_context

# ── Tools ──
from tools.definitions import _get_inline_tools, _filter_tools_for_mode
from tools.runner import _run_inline_tool, _current_project_state
from tools.workspace import new_transaction, get_transaction

# ── Utils ──
from utils.tool import parse_tool_args as _parse_tool_args, run_bash_failed as _run_bash_failed
from utils.token import estimate_ctx_tokens as _estimate_ctx_tokens
from utils.file import normalize_tool_path as _normalize_tool_path

# ── SSE ──
from sse.events import (
    make_tool_call_event as _make_tool_call_event,
    make_tool_result_event as _make_tool_result_event,
)

# ── Explore ──
from explore.cache import (
    _pre_explore_cache, _explore_cache_key, _explore_cache_valid,
    _pre_explore_cache_set, _get_pre_explore_lock,
    _pre_explore_cache_invalidate_workspace,
    _PRE_EXPLORE_CACHE_TTL, _explore_extract_files,
)

# ── Hive Functions ──
from hive_functions.tree_scout import (
    get_workspace_tree, partition_tree_async, parse_contract_summary,
    TREE_HEADER_PREFIX,
)
from hive_functions.prompts import (
    EXPLORE_CODEBASE_PROMPT, DUO_CRITIC_TOOLS_SYSTEM, PROMPTS,
)
from hive_functions.ctx_utils import (
    compute_content_budget, ContextBudget, explore_to_planner_ctx,
    compute_char_caps, budget_session_msgs, extract_known_files,
    budget_explore_window, derive_static_map_budget,
)
from hive_functions.planner import (
    run_planner, run_inloop_planner, PlannerResult,
    make_thinking_planner_sys, make_planner_sys, fallback_planner_steps,
    make_planner_analysis_sys,
)
from hive_functions.chunking import ChunkState, build_chunk_context, ChunkAction
from hive_functions.loop_machine import AgentState, StopReason, ExecutionController
from hive_functions.memory import ToolContextLRU
from hive_functions.test_runner import run_tests as _run_test_suite, TestResult as _TestResult
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

# ── Imports from extracted helpers ─────────────────────────────────────
from core.duo_helpers import (
    DEFAULT_VRAM_BUDGET_GB, _READ_ONLY_KEYWORDS, RE_THINK_CLEANUP as _re_think_cleanup,
    _preprocess_think_blocks, _inject_no_think_directive, _resolve_tool_budget, _resolve_tool_read_timeout_seconds,
    _calculate_thinking_tokens, _build_duo_coder_sys,
    _run_parallel_pre_explore, _build_symbol_reference_hints,
    _compress_fail_streak_update, _explore_size_tolerance, _collect_new_explore_paths,
    read_loop_key,
    _should_escalate_pass_files,     _sum_edit_lines, _build_dropped_tool_retry,
    _RE_WIN_PATH, _RE_UNIX_PATH, _RE_FILE_EXT,
    _RE_CRITIC_APPROVED, _RE_CRITIC_VERDICT, _RE_CRITIC_ISSUES,
    update_model_capability_overrides,
)

from core.plan_tracker import build_tracker_from_planner as _build_tracker_from_planner

# ── Extracted pure utilities (core/duo/_utils.py) ──
from core.duo._utils import (
    _merge_down, _split_paths_by_parent, _parse_critic_tune,
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


def _build_os_hint() -> str:
    """OS-HINT (2026-09-02): platform-specific run_bash guidance, appended to the
    coder system prompt. The static coder prompt blocks are OS-neutral now, so
    Linux/other platforms never receive Windows-only instructions."""
    import sys as _sys
    if _sys.platform == "win32":
        return (
            "\nCRITICAL - Windows PowerShell. Use PowerShell-native commands in "
            "run_bash - NOT bash/Linux commands. Example mappings: "
            "'ls' -> 'Get-ChildItem', 'cat' -> 'Get-Content', 'rm' -> 'Remove-Item', "
            "'&&' -> '; if ($?) {'. Use PowerShell syntax ONLY.\n"
        )
    if _sys.platform.startswith("linux") or _sys.platform == "darwin":
        return (
            "\nRUNTIME SHELL: bash/sh on Unix. Use bash commands in run_bash "
            "(ls, cat, grep, ...) - not PowerShell.\n"
        )
    return ""


def _read_hb_interval(settings: dict | None) -> int:
    try:
        _iv = int((settings or {}).get(_DUO_HEARTBEAT_KEY, _DUO_HEARTBEAT_DEFAULT) or _DUO_HEARTBEAT_DEFAULT)
        return max(3, _iv)
    except Exception:
        return _DUO_HEARTBEAT_DEFAULT


async def _await_with_hb(
    coro_factory,
    *,
    timeout: float,
    emit_fn,
    label: str = "Model",
    interval: float = 10.0,
):


    _task = asyncio.create_task(coro_factory())
    _t0 = time.monotonic()
    try:
        while True:
            _el = int(time.monotonic() - _t0)
            try:
                await emit_fn({"type": "status", "content": f"⏳ {label} … ({_el}s)"})
            except Exception:
                pass
            if _el >= timeout:
                raise asyncio.TimeoutError()
            _done, _pending = await asyncio.wait({_task}, timeout=interval)
            if _done:
                return _task.result()
    finally:
        if not _task.done():
            _task.cancel()


def _park_on_disconnect(
    *,
    ctx,
    loop_items=None,
    di=None,
    done_tasks=None,
    written_files=None,
    explore_ctx="",
    ws_str="",
    n_items=0,
):
    if ctx.chat_id is None:
        return
    try:
        _rem: list = []
        if isinstance(loop_items, list) and isinstance(di, int) and 0 <= di < len(loop_items):
            _rem = [{"title": str(t)} for t in loop_items[di:]]
        if _rem:
            _write_resume_block(
                chat_id=ctx.chat_id,
                workspace=str(ws_str or ""),
                chunks_total=int(n_items or 0),
                chunks_done=list(done_tasks or []),
                chunks_remaining=_rem,
                written_files=list(written_files or []),
                last_summary=" | ".join(str(t) for t in (done_tasks or [])),
                plan_msgs=[],
                explore_ctx=str(explore_ctx or ""),
                halt_reason="disconnect",
            )
        _save_chat_context(ctx.chat_id, {
            **(_load_chat_context(ctx.chat_id) or {}),
            "last_run": {
                "task": str(getattr(ctx, "user_input", ""))[:8000],
                "stop_reason": "disconnect",
                "written_files": sorted(set(written_files or []))[:50],
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "success": False,
            },
        })
        logger.warning(
            "[DISCONNECT-PARK] Run parked chat=%s chunks_remaining=%d written_files=%d halt_reason=disconnect",
            ctx.chat_id, len(_rem), len(list(written_files or [])),
        )
        try:
            from infra.notify import notify
            notify(
                "HiveMind — Run interrupted (browser closed)",
                f"chat={ctx.chat_id}, {len(list(written_files or []))} file(s) written. "
                "Send a message to continue.",
                dedup_sig=f"interrupt:{ctx.chat_id}",
            )
        except Exception:
            pass
        logger.warning(
            "\n" + "=" * 62 + "\n"
            + "  BROWSER CLOSED — RUN PARKED\n"
            + f"  chat={ctx.chat_id} | files_written={len(list(written_files or []))} | chunks_remaining={len(_rem)}\n"
            + "  Resume: open the chat and send a message to continue.\n"
            + "=" * 62
        )
    except Exception as _park_err:
        logger.warning("[DISCONNECT-PARK] Park failed: %s", _park_err)


def _build_touched_context(msgs: list, touched_paths: set) -> str:
    """Extract file contents for touched files from Pre-Explore messages via forward-pass."""
    _path_contents: dict[str, str] = {}
    _pending_path = ""
    for _m in msgs:
        if _m.get("role") == "assistant" and "tool_calls" in _m:
            _pending_path = ""
            for _tc in _m.get("tool_calls", []):
                _args = _parse_tool_args(_tc.get("function", {}).get("arguments", {}))
                _tp = _args.get("path", "").replace("\\", "/").lower()
                for _t in touched_paths:
                    if _t in _tp or _tp in _t:
                        _pending_path = _t
                        break
                if _pending_path:
                    break
        elif _m.get("role") == "tool" and _m.get("name") == "read_file" and _pending_path:
            _content = str(_m.get("content", ""))
            if _content.strip():
                _path_contents[_pending_path] = _content[:3000]
            _pending_path = ""
    if not _path_contents:
        return ""
    _blocks = []
    for _p in sorted(_path_contents.keys()):
        _blocks.append(f"### {_p}\n```\n{_path_contents[_p]}\n```")
    return "\n\n## Relevant Files (touched by task)\n\n" + "\n\n".join(_blocks)


# ═══════════════════════════════════════════════════════════════════
# DUO RUNNER
# ═══════════════════════════════════════════════════════════════════


def _drain_thinking_rescue(duo_loop) -> list:


    if duo_loop is None:
        return []
    _events = list(getattr(duo_loop, "_pending_events", []) or [])
    duo_loop._pending_events.clear()
    _th = getattr(duo_loop, "_dr_thinking_parts", []) or []
    if _th:
        logger.warning(
            "[DUO] Tool round aborted — %d thinking chunks (%d chars) kept in memory",
            len(_th), sum(len(t) for t in _th),
        )
    return _events


def _build_dtool_base(system_content, explore_history, plan_content, bridge_msg):
    """Build the initial coder tool-loop message list.

    Guarantees exactly 1 system message at index 0.
    """
    if plan_content:
        logger.warning(
            "[PLAN-INJECT] bridge branch: plan injected into coder context (%d chars)",
            len(plan_content),
        )
    else:
        logger.warning("[PLAN-INJECT] bridge branch: NO plan present (empty/None)")
    return [
        {"role": "system", "content": system_content},
        *explore_history,
        *([{"role": "assistant", "content": f"[IMPLEMENTATION PLAN]\n{plan_content}"}]
          if plan_content else []),
        bridge_msg,
    ]


def _inject_plan_into_coder_msgs(dtool_msgs, plan_result, *, chunking: bool, is_first_outer_round: bool):


    if (
        plan_result is not None
        and getattr(plan_result, "plan_content", None)
        and not chunking
        and is_first_outer_round
    ):
        logger.warning(
            "[PLAN-INJECT] non-bridge branch: plan injected into coder context (%d chars, first_outer_round=%s)",
            len(plan_result.plan_content), is_first_outer_round,
        )
        dtool_msgs.append({
            "role": "assistant",
            "content": f"[IMPLEMENTATION PLAN]\n{plan_result.plan_content}",
        })
        return True
    logger.warning(
        "[PLAN-INJECT] non-bridge branch: injection skipped "
        "(plan=%s chunking=%s first_round=%s)",
        bool(plan_result is not None and getattr(plan_result, "plan_content", None)),
        chunking, is_first_outer_round,
    )
    return False


def _build_plan_anchor_text(subtasks: list, plan_tracker) -> str:


    if subtasks:
        return ", ".join(str(t) for t in subtasks)
    if plan_tracker is not None and getattr(plan_tracker, "total", 0) > 0:
        _anchor_steps = []
        for _aps in getattr(plan_tracker, "_plan", None).steps:
            _aps_paths = ", ".join(getattr(_aps, "expected_paths", []) or []) or "?"
            _anchor_steps.append(
                f"step {getattr(_aps, 'id', '?')}: {getattr(_aps, 'intent', '') or ''} → {_aps_paths}"
            )
        return "; ".join(_anchor_steps)
    return ""


def _strip_stale_ctx_notices(msgs: list) -> list:
    """COMPRESS-CLEANUP-FIX (2026-08-31): removes stale CTX warning user
    messages from the message list.

    Live finding: after a compression, the "[RUNTIME NOTICE] [CTX CRITICAL:
    ~85% full] ... Stop reading new files. Complete current edits and call
    task_complete." message injected before the compression stayed at the
    tail. The context had just been shrunk to ~15% — the warning contradicted
    the fresh state and pushed the coder to a premature task_complete
    (-> loop_detected stop a few rounds after the compression).

    ONLY user/system messages with the CTX markers are removed; tool/coder
    messages stay untouched.
    """
    _markers = ("[RUNTIME NOTICE]", "[CTX CRITICAL:", "[CTX: ~", "[CTX:")
    return [
        m for m in msgs
        if not (
            m.get("role") in ("user", "system")
            and any(str(m.get("content") or "").startswith(_mk) for _mk in _markers)
        )
    ]


def _apply_thinking_kwargs(payload, profile, thinking, coder_tool_think):


    if profile.get("preserve_thinking") and thinking:
        payload.setdefault("chat_template_kwargs", {})["preserve_thinking"] = True
    if not coder_tool_think:
        payload.setdefault("chat_template_kwargs", {})["enable_thinking"] = False
    return payload


async def _auto_commit_chunk(user_input: str, subtask: str, workspace: str,
                             git_enabled: bool, files: list[str] | None = None) -> str:


    if not git_enabled or not workspace:
        return ""
    try:
        from hive_functions.git_tools import (
            exec_git_commit, exec_git_squash_checkpoints,
        )
        try:
            from settings import load_settings as _ls
            _prefix = (str((_ls() or {}).get("git_commit_prefix", "") or "").strip()
                       or "hivemind:")
        except Exception:
            _prefix = "hivemind:"
        _label = (subtask or user_input or "workspace")[:60]
        _message = f"{_prefix} {_label}"

        _fold_out = await exec_git_squash_checkpoints(_message, workspace)
        if _fold_out.startswith(("✅", "❌")):
            return "" if _fold_out.startswith("ℹ️") else _fold_out

        _out = await exec_git_commit(message=_message, workspace=workspace,
                                     files=files or None)
        if _out.startswith(("ℹ️", "⚠️")):
            return ""
        return _out
    except Exception as _ac_err:
        logger.warning("[AUTO-COMMIT] failed: %s", _ac_err)
        return ""


def _git_checkpoints_enabled(ctx) -> bool:

    try:
        from settings import load_settings as _ls
        return bool((_ls() or {}).get("duo_git_checkpoints", True))
    except Exception:
        return True


async def _git_checkpoint_at_chunk_start(ctx, _ws_str: str, _di: int,
                                         _n_items: int, subtask) -> str:


    if not _ws_str or not _git_checkpoints_enabled(ctx):
        return ""
    try:
        from hive_functions.git_tools import exec_git_checkpoint as _gck
        if ctx.duo_config.git_autocommit:
            _label = f"chunk {_di + 1}/{_n_items}: {str(subtask)[:40]}"
        else:
            if getattr(ctx, "_git_session_cp_done", False):
                return ""
            _label = "session start"
        _out = await _gck(_label, _ws_str)
        if _out and not ctx.duo_config.git_autocommit:
            ctx._git_session_cp_done = True
        return _out
    except Exception as _ck_err:
        logger.debug("[GIT-CHECKPOINT] skipped: %s", _ck_err)
        return ""


def _ld_setter(src_line: int) -> None:
    logger.warning("[LD-SET] _loop_detected gesetzt bei Zeile %d", src_line)


async def run_code_duo(ctx):
    """Code-duo handler: iterative coder+critic loop."""
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
    from utils.workspace_resolve import (
        resolve_workspace as _ws_resolve,
        sync_env_workspace as _ws_sync_env,
    )
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

    async for _ev in _phase_pre_explore(ctx, state):
        yield _ev
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
        _plan_result: "PlannerResult | None" = None
        _planner_used_fallback = False
        _planner_fallback_reason = ""
        _planner_parse_mode = "none"
        _planner_context_trimmed = False
        _plan_guard_quality: dict = {}
        # SKIP-CHECK: Skip Planner if skip pressed — go straight to Coder
        _planner_skipped = False
        _fup_task_ctx = state.get("_follow_up_task_ctx", "") or ""
        if (ctx.duo_config.chunking or ctx.duo_config.planner) and ctx.step_skipped() and not ctx.aborted():
            ctx.clear_step_skip()
            yield await ctx.emit({"type":"status",
                "content": "⏭ Planner skipped — going straight to coder"})
            _planner_skipped = True
            yield await ctx.emit({"type": "planner_done", "summary": "⏭ skipped"})
            ctx.phase_timer.skip("soft_planner")
        if (ctx.duo_config.chunking or ctx.duo_config.planner) and not ctx.aborted() and not _resume_data and not _planner_skipped:
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

            try:
                _lsm_plan_pre = None
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
                    await asyncio.sleep(2.5)
                try:
                    from backend.llama_vram_table import wait_for_vram_reclaim, vram_of_moe
                    _planner_vram = vram_of_moe(_planner_model, _plan_ctx_final) * 1024 + 768
                    await _await_with_hb(
                        lambda: wait_for_vram_reclaim(int(_planner_vram), timeout_sec=45),
                        timeout=60.0,
                        emit_fn=ctx.emit,
                        label="Waiting for VRAM to free up",
                        interval=5.0,
                    )
                except Exception:
                    pass
                _lsm_plan_pre._planner_critical_phase = True
                # fallback to the lighter coder model to keep Planner responsive.
                # P1-1 FIX: Capture port from first ensure_loaded — eliminates redundant
                # second ensure_loaded call (was wasting ~300-500ms on /health re-check).
                _plan_port: int | None = None
                try:
                    _ensure_timeout = float(ctx.settings.get("duo_planner_ttl_seconds", 0) or 0) or float(ctx.settings.get("duo_planner_ensure_load_timeout_s", 450.0) or 450.0)
                    _plan_port = await _await_with_hb(
                        lambda: _lsm_plan_pre.ensure_loaded(_planner_model, num_ctx=_plan_ctx_final, n_parallel=1),
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
                except Exception as _pre_load_err:
                    logger.warning("Planner pre-load failed: %s", _pre_load_err, exc_info=True)
                    _err_short = str(_pre_load_err)[:120]
                    _planner_fb = str(ctx.settings.get("duo_coder_fallback_model", "") or "").strip()
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
                        _lsm_port_res.ensure_loaded(_planner_model, num_ctx=_plan_ctx_final, n_parallel=1),
                        timeout=30.0,
                    )
                except Exception as _port_err:
                    logger.warning("[Planner] Port resolution failed: %s", _port_err)

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

            async def _emit_chunk_planner_heartbeat():
                _elapsed = 0
                await asyncio.sleep(1.0)
                while not _planner_hb_stop.is_set() and not ctx.aborted():
                    if _planner_hb_stop.is_set() or ctx.aborted():
                        break
                    _elapsed += 1
                    try:
                        # put_nowait: never blocks; raises QueueFull if maxsize
                        # hit (slow consumer) — silently drop, keep looping.
                        _planner_hb_queue.put_nowait({
                            "type": "planner_thinking",
                            "elapsed": _elapsed,
                            "model": _planner_model,
                        })
                    except asyncio.QueueFull:
                        pass  # consumer can't keep up — drop heartbeat
                    except Exception:
                        break
                    await asyncio.sleep(1.0)

            _planner_hb_task = asyncio.create_task(_emit_chunk_planner_heartbeat())
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
            _exec_ctx = (int(ctx.settings.get("duo_planner_ctx_target", 0) or 0) or resolve_ctx(ctx.settings.get("duo_coder_ctx_agentic"), coder_mdl, "agentic")) if ctx.duo_config.agentic_mode else resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "coder")
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
            _runtime_profile = ctx.resolve_duo_runtime_profile(
                None, important_task=False, until_finished=ctx.duo_config.until_finished,
            )
            _content_tokens = compute_content_budget(_exec_ctx, profile=_runtime_profile)
            _ctx_budget = ContextBudget.from_content_tokens(_content_tokens).with_static_map_override(
                ctx.settings.get("duo_static_map_chars", 0)
            )

            # Planner gets structured architecture (NOT raw TOML)
            _planner_ctx = explore_to_planner_ctx(_explore_ctx, task=ctx.user_input, budget=_ctx_budget)

            _planner_task = ctx.user_input
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

            async def _plan_emit_fn(event: dict):
                await _plan_event_q.put(event)

            def _plan_aborted_fn():
                return ctx.aborted()

            async def _plan_heartbeat_fn():
                # Drain planner heartbeat queue into the event bridge
                while not _planner_hb_queue.empty():
                    try:
                        _hb = _planner_hb_queue.get_nowait()
                        await _plan_event_q.put(_hb)
                    except asyncio.QueueFull:
                        pass

            # ── Port check ──────────────────────────────────────────────────────
            _plan_result: PlannerResult | None = None
            _plan_thinking = ""
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
                    aborted_fn=_plan_aborted_fn,
                    emit_fn=_plan_emit_fn,
                    heartbeat_fn=_plan_heartbeat_fn,
                ))

                try:
                    # Yield events from the bridge as they arrive
                    while not _plan_task.done() or not _plan_event_q.empty():
                        # Enforce the run deadline even while the planner runs;
                        # otherwise a 600s planner overshoots a 90-420s run timeout.
                        if _duo_deadline_at and time.time() >= _duo_deadline_at:
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
                        _planner_fallback_reason = "run_deadline"
                        _planner_parse_mode = "timeout"
                except Exception as _plan_exc:
                    logger.error("[Planner] Exception: %s", _plan_exc, exc_info=True)
                    _subtasks = []
                    _plan_thinking = ""
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
                                _project_state.open_tasks = [
                                    re.sub(r"^\d+\.\s*", "", l).strip()
                                    for l in _ot.group(1).strip().split("\n") if l.strip()
                                ]
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
                logger.debug("[CHUNKING-GUARD] ctx.duo_config.chunking=False - %d subtasks discarded, planner briefing kept", len(_subtasks))
                _subtasks = []

            if not ctx.phase_timer.is_ended("soft_planner"):
                _planner_status = "ok" if _subtasks else "empty"
                if not _subtasks and _plan_result is not None:
                    _planner_status += f"(reason={getattr(_plan_result, 'fallback_reason', None) or 'none'})"
                ctx.phase_timer.end("soft_planner", status=_planner_status)
            # When chunking is off, don't send chunks to the UI — prevents
            # confusing "CHUNK-PLAN" panel when only a briefing was requested.
            _emit_chunks = list(_subtasks) if ctx.duo_config.chunking else []
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

        _cs = ChunkState()  # ChunkState replaces _done_tasks, _written_files, _coder_outputs, etc.
        _done_tasks = _cs.done_tasks          # backward-compat aliases for the loop
        _written_files = _cs.written_files
        _coder_outputs = _cs.coder_outputs

        # Fortschritt). No-Overwrite-Guard wie im Safety-Net.
        def _write_pre_loop_resume(halt_reason: str = "hard_stop"):
            try:
                if ctx.chat_id and not _load_resume_block(ctx.chat_id):
                    _write_resume_block(
                        chat_id=ctx.chat_id,
                        workspace=_ws_str,
                        chunks_total=1,
                        chunks_done=_done_tasks,
                        chunks_remaining=[{"title": ctx.user_input}],
                        written_files=_written_files,
                        last_summary=" | ".join(_done_tasks),
                        plan_msgs=[],
                        explore_ctx=_explore_ctx,
                        halt_reason=halt_reason,
                    )
            except Exception as _rl_err:
                if isinstance(_rl_err, (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                logger.warning("[PRE-LOOP-RESUME] Write failed: %s", _rl_err)
        _total_tool_rounds: int = 0
        _max_tool_rounds: int = 0    # default before tool-round setup
        _lifetime_tool_rounds: int = 0
        _total_tool_errors_ref: list[int] = [0]
        _connect_error_retries: int = 0
        _force_compress_next: bool = False
        _compress_fail_streak: int = 0      # CONSECUTIVE-FAIL-GUARD: limit 400->compress->fail->restore cycles
        _loop_detected = False   # guard against UnboundLocalError on early exit (abort/timeout)
        _explore_only_rounds: int = 0
                                        # late initialization (depends on tool-round setup)
        _stuck_reason = ""       # guard against UnboundLocalError on early exit
        _ctx_pressure_warned = False  # CTX pressure warn-once guard
        _touched_block_persisted = ""  # Persisted across chunks for pre-explore context
        # P1-3 FIX: Cache coder port across chunk ctx.iterations — coder is pinned,
        # so ensure_loaded is a no-op after first call but costs ~200-300ms each time.
        _cached_coder_port: int | None = None
        _cached_coder_port_ctx: int | None = None
        _auto_tool_promote_streak = 0
        _auto_tool_promote_notified = False
        _last_run_bash_failure: dict | None = None
        _changed_since_failure: set[str] = set()
        _last_learned_insight_sig = ""
        _verify_mutation_serial = 0
        _verify_last_ok_serial = 0
        _task_complete_blocked = [0]
        _verify_warned = False
        # laufen via asyncio.gather (tool_executor _PARALLEL_SAFE) in Child-Tasks
        # bliebe _files_read_in_run im Parent None (Compression-Removal + SKIP-
        try:
            from tools.runner import _files_read_in_run as _fir_run_ctx
            from tools.runner import _files_seen_in_run as _fsr_run_ctx
            from tools.runner import _files_written_in_run as _fw_run_ctx
            if _fir_run_ctx.get(None) is None:
                _fir_run_ctx.set(set())
            if _fsr_run_ctx.get(None) is None:
                _fsr_run_ctx.set(set())
            if _fw_run_ctx.get(None) is None:
                _fw_run_ctx.set(set())
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
            _subtasks      = [c.get("title", str(c)) if isinstance(c, dict) else str(c)
                              for c in _resume_data["resume"].get("chunks_remaining", [])]
            if not _subtasks:
                _subtasks = []

        # ── Build Coder System Prompt from modular blocks ───────────
        _has_plan = bool(_subtasks) or bool(_plan_thinking) or bool(
            _plan_result is not None and _plan_result.plan_content
        )
        _duo_coder_sys = _build_duo_coder_sys(
            ctx,
            has_plan=_has_plan,
            has_subtasks=bool(_subtasks),
            has_explore_ctx=bool(_explore_ctx),
        ) + _coder_dyn_hints
        _follow_up_hint = state.get("_follow_up_hint", "") or ""
        if _follow_up_hint:
            _duo_coder_sys += "\n\n" + _follow_up_hint

        # Writes were being truncated by the token limit. Instruct proactively:
        # from the budget boundary use write_file + write_file_append.
        try:
            _wb_hint_budget = max(1024, int(_duo_coder_tok))
            _wb_hint_cpt = float(ctx.settings.get("duo_write_chars_per_token", 2.5))
            _wb_hint_safe = max(500, int(_wb_hint_budget * _wb_hint_cpt) - 2000)
            _duo_coder_sys += (
                f"\n\nOUTPUT BUDGET: max {_wb_hint_budget} tokens per answer. "
                f"If you expect more than ~{_wb_hint_safe} characters of content "
                f"for a write_file call, ALWAYS write incrementally: write_file with "
                f"the first part, then write_file_append for each further part "
                f"(each part well below ~{_wb_hint_safe} characters). An oversized "
                f"single call will be cut off at the token limit and discarded entirely."
            )
        except Exception:
            pass

        try:
            _planner_is_coder = (
                _planner_model is not None
                and _planner_model == coder_mdl
            )
        except (NameError, UnboundLocalError):
            _planner_is_coder = False
        try:
            _plan_port_available = _plan_port is not None
        except (NameError, UnboundLocalError):
            _plan_port_available = False
        # STALE-PORT-GUARD (2026-08-31): "Planner=Coder" may only take over the
        # planner port as coder port if a server is really listening there.
        # Otherwise the coder inherits a phantom slot (dead port) and crashes
        # immediately with ConnectError (observed live in the planner path).
        _plan_port_alive = False
        if _plan_port_available:
            try:
                from backend.llama_server_manager import manager as _lsm_pv
                _plan_port_alive = await _lsm_pv._port_alive(_plan_port)
            except Exception:
                _plan_port_alive = False
        if _planner_is_coder and _plan_port_available and _plan_port_alive:
            _cached_coder_port = _plan_port
            _cached_coder_port_ctx = _plan_ctx_final  # CTX-GUARD: pin planner ctx to port
            logger.info("[Planner=Coder] Model stays in VRAM - no reload needed")
            yield await ctx.emit({
                "type": "status",
                "content": (
                    f"⚡ Planner=Coder — {_planner_model.split(':')[0]} stays in VRAM, "
                    "no reload needed"
                ),
            })
        elif _planner_is_coder and _plan_port_available and not _plan_port_alive:
            logger.warning(
                "[Planner=Coder] Planner port %s dead (phantom slot) — cache discarded, "
                "normal coder load runs.",
                _plan_port,
            )
            yield await ctx.emit({
                "type": "status",
                "content": "⚠ Planner=Coder port unreachable — reloading coder model",
            })

        try:
            _scp = _skip_coder_pin
        except (NameError, UnboundLocalError):
            _scp = False
        from backend.llama_server_manager import manager as _lsm2
        from backend.llama_server_manager import VRAMPreFlightError as _VRAMPreFlightError
        _coder_load_ok = False
        if (_explore_ctx or _workers_were_loaded) and _scp:
            try:
                yield await ctx.emit({"type": "status", "content": "🧹 Freeing VRAM (unloading worker models)…"})
                _evicted_workers = 0
                if _use_parallel:
                    for _ws_entry in _worker_slots:
                        _wm = _ws_entry.get("model", "")
                        _wm_base = _wm.rsplit("#", 1)[0] if "#" in _wm else _wm
                        if _wm_base != exec_mdl:
                            await _lsm2.evict(_wm)
                            _evicted_workers += 1
                if _xexplore_mdl != exec_mdl:
                    await _lsm2.evict(_xexplore_mdl)
                    _evicted_workers += 1
                try:
                    _loaded_after = await _lsm2.list_loaded()
                    _exec_base = exec_mdl.rsplit("#", 1)[0] if "#" in exec_mdl else exec_mdl
                    _planner_base = _planner_model.rsplit("#", 1)[0] if "#" in _planner_model else _planner_model
                    for _lm in _loaded_after:
                        _lname = str(_lm.get("name") or _lm.get("model") or "")
                        if not _lname:
                            continue
                        _lbase = _lname.rsplit("#", 1)[0] if "#" in _lname else _lname
                        if _lbase != _exec_base and _lbase != _planner_base:
                            await _lsm2.evict(_lname)
                            _evicted_workers += 1
                except Exception:
                    pass
                # hits a dead port → TCP close → frontend: "Error in input stream".
                if _evicted_workers > 0:
                    await asyncio.sleep(1.5)
                if _planner_is_coder and _plan_port_available and _plan_port_alive:
                    _coder_load_ok = True
                    _coder_ctx_post = (
                        resolve_ctx(ctx.settings.get("duo_coder_ctx_agentic"), coder_mdl, "agentic")
                        if ctx.duo_config.agentic_mode
                        else resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "coder")
                    )
                    yield await ctx.emit({"type": "status",
                        "content": f"✅ Coder ready ({exec_mdl.split(':')[0]} — Planner=Coder, no reload)"})
                    # FIX (2026-08-31): The fast path did not emit the "agent"
                    # event (coder phase switch in the frontend) → after
                    # "coder ready" nothing happened in the UI anymore.
                    if _subtasks:
                        yield await ctx.emit({"type": "status",
                            "content": f"🖊️ Coder active — {len(_subtasks)} subtask(s), starting with chunk 1"})
                    yield await ctx.emit({
                        "type": "agent",
                        "content": "Code",
                        "model": exec_mdl.split(":")[0] if exec_mdl else "Agentic",
                        "role": "Writes code",
                    })
                else:
                    _coder_ctx_post = (
                        resolve_ctx(ctx.settings.get("duo_coder_ctx_agentic"), coder_mdl, "agentic")
                        if ctx.duo_config.agentic_mode
                        else resolve_ctx(ctx.settings.get("duo_coder_ctx_normal"), coder_mdl, "coder")
                    )
                    yield await ctx.emit({"type": "status",
                        "content": f"⏳ Loading coder ({exec_mdl.split(':')[0]}, ctx={_coder_ctx_post})…"})
                    _coder_load_ok = False
                    _coder_ctx_try = _coder_ctx_post
                    _coder_ttl_s = float(ctx.settings.get("duo_coder_ttl_seconds", 0) or 0)
                    _coder_load_timeout = _coder_ttl_s if _coder_ttl_s > 0 else 150.0
                    for _cl_attempt in range(3):
                        try:
                            yield await ctx.emit({"type": "status",
                                "content": f"⏳ Loading coder ({exec_mdl.split(':')[0]}, ctx={_coder_ctx_try})…"})
                            _coder_port = await asyncio.wait_for(
                                _lsm2.ensure_loaded(exec_mdl, num_ctx=_coder_ctx_try, n_parallel=1),
                                timeout=_coder_load_timeout,
                            )
                            _coder_load_ok = True
                            _coder_ctx_post = _coder_ctx_try
                            _cached_coder_port = _coder_port
                            _cached_coder_port_ctx = _coder_ctx_try
                            _duo_pinned.add(exec_mdl)
                            break
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Coder load timeout (attempt %d/3) ctx=%d",
                                _cl_attempt + 1, _coder_ctx_try,
                            )
                            yield await ctx.emit({
                                "type": "status",
                                "content": (
                                    f"⛔ Coder load timeout at ctx={_coder_ctx_try} — "
                                    "ctx too large for model/VRAM. Please reduce ctx."
                                ),
                            })
                            ctx.phase_timer.skip("coder_loop")
                            clear_graceful_stop(ctx.run_id)
                            _cleanup_pause_state(ctx.run_id)
                            if ctx.chat_id:
                                _clear_pause_state(ctx.chat_id)
                            _cleanup_governor(ctx.chat_id or ctx.run_id)
                            _duo_hard_stop = True
                            _write_pre_loop_resume()
                            ctx.duo_stop_reason = "hard_stop"
                            yield await ctx.emit(ctx.done_event(
                                round(time.time() - ctx.t_total, 1),
                                ctx.duo_stop_reason,
                                **ctx.collect_done_metrics(),
                            ))
                            return
                        except _VRAMPreFlightError as _vpf:
                            # dominant pre-flight block → further ctx reduction
                            # attempts are structurally pointless (3x 4s+ wait + error
                            # return never reached — AUDIT-FIX 2026-08-04).
                            # occupancy (~3.6 GB, browser/desktop) blocked hermes
                            _fb_model = str(ctx.settings.get("duo_coder_fallback_model", "") or "").strip()
                            if _fb_model and _fb_model != exec_mdl:
                                logger.warning(
                                    "[CODER-VRAM-FALLBACK] %s not loadable (free=%d MiB, external=%d MiB) — "
                                    "trying fallback %s",
                                    exec_mdl, _vpf.free_mib, _vpf.external_usage_est_mib, _fb_model,
                                )
                                yield await ctx.emit({"type": "status",
                                    "content": (
                                        f"⏳ {exec_mdl.split(':')[0]} does not fit into VRAM "
                                        f"(free={_vpf.free_mib} MiB) — fallback to {_fb_model}…"
                                    )})
                                _fb_ctx = min(_coder_ctx_try, 10240)
                                try:
                                    _coder_port = await asyncio.wait_for(
                                        _lsm2.ensure_loaded(_fb_model, num_ctx=_fb_ctx, n_parallel=1),
                                        timeout=150.0,
                                    )
                                    _coder_load_ok = True
                                    _coder_ctx_post = _fb_ctx
                                    _cached_coder_port = _coder_port
                                    _cached_coder_port_ctx = _fb_ctx
                                    exec_mdl = _fb_model
                                    coder_mdl = _fb_model
                                    _duo_pinned.add(_fb_model)
                                    yield await ctx.emit({"type": "status",
                                        "content": f"✅ Coder fallback active: {_fb_model} (ctx={_fb_ctx})"})
                                    break
                                except Exception as _fb_err:
                                    logger.warning(
                                        "[CODER-VRAM-FALLBACK] Fallback %s failed: %s",
                                        _fb_model, str(_fb_err)[:120],
                                    )
                                    yield await ctx.emit({"type": "status",
                                        "content": f"⛔ Fallback {_fb_model} also not loadable."})
                            _duo_hard_stop = True
                            yield await ctx.emit({"type": "status",
                                "content": (
                                    f"⛔ Coder ({exec_mdl.split(':')[0]}) not loadable: "
                                    f"~{_vpf.external_usage_est_mib} MiB external usage"
                                    + (" — impossible even with small ctx (fixed costs)"
                                       if _vpf.fixed_cost_dominant else "")
                                    + ". Close GPU-heavy applications or switch model — "
                                    "run ended with a resume block."
                                )})
                            _write_pre_loop_resume()
                            break
                        except Exception as _cl_err:
                            if ctx.aborted():
                                yield await ctx.emit({"type": "status",
                                    "content": "⏹ Coder load aborted (run was stopped)"})
                                ctx.phase_timer.skip("coder_loop")
                                clear_graceful_stop(ctx.run_id)
                                _cleanup_pause_state(ctx.run_id)
                                if ctx.chat_id:
                                    _clear_pause_state(ctx.chat_id)
                                _cleanup_governor(ctx.chat_id or ctx.run_id)
                                # to the in-loop abort) + done_event.
                                _write_pre_loop_resume("aborted")
                                ctx.duo_stop_reason = "aborted"
                                yield await ctx.emit(ctx.done_event(
                                    round(time.time() - ctx.t_total, 1),
                                    ctx.duo_stop_reason,
                                    **ctx.collect_done_metrics(),
                                ))
                                return
                            logger.warning(
                                "Coder-load attempt %d/3 failed (%s): %s",
                                _cl_attempt + 1, type(_cl_err).__name__, str(_cl_err)[:120],
                            )
                            if _cl_attempt < 2:
                                _next_ctx = max(8192, int(_coder_ctx_try / 2))
                                yield await ctx.emit({"type": "status",
                                    "content": f"⟳ Coder load error ({type(_cl_err).__name__}), retry {_cl_attempt+2}/3 with ctx={_next_ctx} in 4s…"})
                                await asyncio.sleep(4.0)
                                _coder_ctx_try = _next_ctx
                                if ctx.aborted():
                                    yield await ctx.emit({"type": "status",
                                        "content": "⏹ Coder load aborted (run was stopped)"})
                                    ctx.phase_timer.skip("coder_loop")
                                    clear_graceful_stop(ctx.run_id)
                                    _cleanup_pause_state(ctx.run_id)
                                    if ctx.chat_id:
                                        _clear_pause_state(ctx.chat_id)
                                    _cleanup_governor(ctx.chat_id or ctx.run_id)
                                    _write_pre_loop_resume("aborted")
                                    ctx.duo_stop_reason = "aborted"
                                    yield await ctx.emit(ctx.done_event(
                                        round(time.time() - ctx.t_total, 1),
                                        ctx.duo_stop_reason,
                                        **ctx.collect_done_metrics(),
                                    ))
                                    return
                    if not _coder_load_ok:
                        _duo_hard_stop = True
                        _write_pre_loop_resume()
                        yield await ctx.emit({"type": "status",
                            "content": f"⛔ Coder ({exec_mdl.split(':')[0]}) could not be loaded — run aborted."})
                        ctx.phase_timer.skip("coder_loop")
                        clear_graceful_stop(ctx.run_id)
                        _cleanup_pause_state(ctx.run_id)
                        if ctx.chat_id:
                            _clear_pause_state(ctx.chat_id)
                        _cleanup_governor(ctx.chat_id or ctx.run_id)
                        yield await ctx.emit({"type": "status", "content": "Coder-Load final failure: process aborted."})
                        ctx.duo_stop_reason = "hard_stop"
                        yield await ctx.emit(ctx.done_event(
                            round(time.time() - ctx.t_total, 1),
                            ctx.duo_stop_reason,
                            **ctx.collect_done_metrics(),
                        ))
                        return
                    yield await ctx.emit({"type": "status",
                        "content": f"✅ Coder ready ({exec_mdl.split(':')[0]})"})
                    if _subtasks:
                        yield await ctx.emit({"type": "status",
                            "content": f"🖊️ Coder active — {len(_subtasks)} subtask(s), starting with chunk 1"})
                    yield await ctx.emit({
                        "type": "agent",
                        "content": "Code",
                        "model": exec_mdl.split(":")[0] if exec_mdl else "Agentic",
                        "role": "Writes code",
                    })
            except Exception as _evict_err:
                # hits a dead port → TCP close → frontend: "Error in input stream".
                logger.warning("Worker-evict/coder-load after explore failed: %s", _evict_err)
                if not _coder_load_ok:
                    yield await ctx.emit({"type": "status",
                        "content": f"⛔ Coder could not be loaded after evict error: {str(_evict_err)[:80]}"})
                    ctx.phase_timer.skip("coder_loop")
                    clear_graceful_stop(ctx.run_id)
                    _cleanup_pause_state(ctx.run_id)
                    if ctx.chat_id:
                        _clear_pause_state(ctx.chat_id)
                    _cleanup_governor(ctx.chat_id or ctx.run_id)
                    # AUDIT-FIX 2026-08-04: Evict-Exception + Load-Fail — hard_stop +
                    _duo_hard_stop = True
                    _write_pre_loop_resume()
                    ctx.duo_stop_reason = "hard_stop"
                    yield await ctx.emit(ctx.done_event(
                        round(time.time() - ctx.t_total, 1),
                        ctx.duo_stop_reason,
                        **ctx.collect_done_metrics(),
                    ))
                    return

        _duo_deadline_at = time.time() + (
            86400.0 if ctx.duo_config.until_finished else _duo_run_timeout_s
        )
        ctx.phase_timer.start("coder_loop")
        logger.debug(
            "Coder deadline set: +%.0fs (until %s) | phases so far: %s",
            _duo_run_timeout_s,
            time.strftime("%H:%M:%S", time.localtime(_duo_deadline_at)),
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
        def _halt_with_resume(halt_reason: str):
            _rem_halt = [{"title": str(t)} for t in _loop_items[_di:]]
            if ctx.chat_id and _rem_halt:
                _write_resume_block(
                    chat_id=ctx.chat_id,
                    workspace=_ws_str,
                    chunks_total=_n_items,
                    chunks_done=_done_tasks,
                    chunks_remaining=_rem_halt,
                    written_files=_written_files,
                    last_summary=" | ".join(_done_tasks),
                    plan_msgs=[],
                    explore_ctx=_explore_ctx,
                    halt_reason=halt_reason,
                )
        while _di < _n_items:
            if _subtasks and not bool(ctx.settings.get("duo_chunking", False)):
                yield await ctx.emit({"type": "status",
                    "content": "⚙️ Chunking disabled in settings — current run continues with chunking (value from start)"})
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
                ctx, _ws_str, _di, _n_items, _subtask)
            if _ck_out:
                yield await ctx.emit({"type": "status", "content": _ck_out})
            # it is also used outside (cache invalidation, written_files tracking).
            _file_changes: dict = {}  # path → {"op": str, "lines": int}
            if ctx.aborted():
                ctx.exec_ctrl.abort(StopReason.USER_ABORTED)
                _halt_with_resume("aborted")
                break
            # EXEC-CTRL-HALT-GUARD: ctx.exec_ctrl.abort() (e.g. MAX_TOOL_ROUNDS, STUCK_IN_LOOP)
            if (ctx.exec_ctrl.state == AgentState.HALTED
                    and ctx.exec_ctrl.stop_reason != StopReason.USER_ABORTED):
                _halt_with_resume(
                    ctx.exec_ctrl.stop_reason.value if ctx.exec_ctrl.stop_reason else "halted"
                )
                break

            if time.time() >= _duo_deadline_at:
                _duo_timed_out = True
                yield await ctx.emit({
                    "type": "status",
                    "content": (
                        f"⏱ Duo timeout reached ({int(_duo_run_timeout_s)}s). "
                        "Ending with the best intermediate result."
                    ),
                })
                _halt_with_resume("timeout_guard")
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
                    asyncio.create_task(_resume_sig.wait()),
                    asyncio.create_task(_abort_pause_sig.wait()),
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
                if _subtask:
                    _cs.mark_chunk_done(_subtask)
                    yield await ctx.emit({"type":"status",
                        "content": f"⏭ Chunk {_di+1} skipped: {_subtask[:50]}"})
                else:
                    yield await ctx.emit({"type":"status",
                        "content": f"⏭ Round {_di+1} skipped"})
                _ac_out = await _auto_commit_chunk(ctx.user_input, _subtask, _ws_str,
                                                   ctx.duo_config.git_autocommit,
                                                   files=list(_cs.written_files[-20:]))
                if _ac_out:
                    yield await ctx.emit({"type": "status", "content": _ac_out})
                _di += 1
                _cs.reset_test_retries()
                if _subtask:
                    yield await ctx.emit({"type": "status",
                        "content": f"✅ Chunk {_di}/{_n_items} done — {len(_written_files)} files changed"})
                continue

            # ── Coder ──────────────────────────────────────────────────────
            if _subtask:
                # Chunking mode: ChunkState.prepare_coder_input() handles
                # fix override and context building.
                _chunk_known_files = extract_known_files(
                    _explore_ctx,
                    cap_chars=_coder_caps.known_files,
                    subtask=_subtask,
                )
                _coder_input = _cs.prepare_coder_input(
                    user_input=ctx.user_input,
                    explore_ctx=_explore_ctx,
                    subtask=_subtask,
                    di=_di,
                    n_items=_n_items,
                    all_subtasks=_subtasks,
                    explore_cap=_coder_caps.explore_inject,
                    static_map_chars=_coder_map_budget,
                    known_files=_chunk_known_files,
                )
                if not _coder_input:
                    # Fallback: should not happen, but safety net
                    _coder_input = build_chunk_context(
                        user_input=ctx.user_input,
                        explore_ctx=_explore_ctx,
                        done_tasks=_done_tasks,
                        written_files=_written_files,
                        subtask=_subtask,
                        di=_di,
                        n_items=_n_items,
                        critic_issues=critic_issues,
                        all_subtasks=_subtasks,
                        explore_cap=_coder_caps.explore_inject,
                        static_map_chars=_coder_map_budget,
                        known_files=_chunk_known_files,
                    )
                # O2: Empty chunk (no files, empty subtask) — skip
                if _coder_input is None:
                    logger.warning("[CHUNKING] Skipping empty chunk %s/%s", _di + 1, _n_items)
                    continue
            elif _di == 0:
                _coder_input = ctx.user_input
                if ctx.image_description:
                    _coder_input += f"\n\n[Image description]:\n{ctx.image_description}"
                if _explore_ctx:
                    _explore_rule = (
                        "WORKSPACE PRE-EXPLORED — the static repo-map and architecture contracts below are confirmed.\n"
                        "The STATIC REPO-MAP section shows file paths, symbols, and imports (deterministic, ground truth).\n"
                        "Use paths from the map directly. Do NOT re-run list_dir or find_files on listed directories.\n"
                        "If a file is already covered by pre-exploration:\n"
                        "  → call edit_file directly (no read_file needed).\n"
                        "Only call read_file if the file was NOT in the pre-exploration results.\n\n"
                    )
                    _MAX_EXPLORE_INJECT = _coder_caps.explore_inject
                    _explore_inject = _explore_ctx
                    if len(_explore_inject) > _MAX_EXPLORE_INJECT:
                        # MARKER-AWARE (2026-08-17): Static map first (up to
                        _explore_inject = budget_explore_window(
                            _explore_inject, _MAX_EXPLORE_INJECT, _coder_map_budget,
                        )
                    _coder_input += f"\n\n[Codebase analysis]:\n{_explore_rule}{_explore_inject}"
            else:
                _issues_txt = "\n".join(f"- {iss}" for iss in critic_issues) if critic_issues else final_verdict
                _coder_input = (
                    f"Original request:\n{ctx.user_input}\n\n"
                    f"Current code:\n{coder_out}\n\n"
                    f"Issues from code reviewer:\n{_issues_txt}\n\n"
                    f"Fix ALL listed issues. Write the complete corrected code."
                )

            if _subtasks:
                _exec_header = (
                    "[EXECUTION MODE — DO NOT CREATE YOUR OWN PLAN]\n"
                    "The plan below is ALREADY DECIDED. Your ONLY job is to implement it.\n"
                    "Start with a tool call NOW. Do not restate, summarize, or re-plan.\n"
                )
                _plan_preview = "\n".join(f"  {i+1}. {st}" for i, st in enumerate(_subtasks))
                _coder_input = (
                    _exec_header + "\n" + _coder_input +
                    f"\n\n[Plan — {len(_subtasks)} subtasks, current: {_di+1}/{_n_items}]:\n{_plan_preview}"
                )
            elif _plan_thinking:
                _exec_header = (
                    "[EXECUTION MODE — DO NOT CREATE YOUR OWN PLAN]\n"
                    "The briefing below is ALREADY DONE. Your ONLY job is to IMPLEMENT it.\n"
                    "Start with a tool call NOW. Do not restate or summarize it.\n"
                )
                _coder_input = (
                    _exec_header + "\n" + _coder_input +
                    f"\n\n[Plan Briefing — IMPLEMENT THIS]:\n{_plan_thinking}"
                )

            if _di == 0:
                # fixed 600/1200-char truncation.
                _sess_budgeted = budget_session_msgs(
                    ctx.pipeline_sess_msgs,
                    budget_chars=_coder_caps.session_budget,
                    user_cap=_coder_caps.session_user,
                    assistant_cap=_coder_caps.session_assistant,
                )
                _coder_msgs = ctx.make_messages(
                    ctx.pipeline, _duo_coder_sys, _coder_input, [],
                    use_session=True,
                    use_memory=False,
                    cached_mem_ctx=ctx.pipeline_mem_ctx,
                    cached_sess_msgs=_sess_budgeted,  # COMPRESSION-FIX + BUDGET
                )
            else:
                _coder_msgs = [
                    {"role": "system", "content": _duo_coder_sys},
                    {"role": "user",   "content": _coder_input},
                ]

            _prompt_prev = (_coder_input or "")[:500].replace("\n", " ").strip()
            if len(_coder_input or "") > 500:
                _prompt_prev += "…"
            # ── Inject pre-explore touched-file contents for all chunks (was only chunk 1) ──
            if _touched_block_persisted and _di > 0:
                _coder_input = (
                    "[Pre-Explore File Contents]\n"
                    + _touched_block_persisted
                    + "\n\n" + _coder_input
                )
            yield await ctx.emit({"type": "duo_coder", "model": coder_mdl, "round": _di + 1,
                              "subtask": _subtask or None, "n_total": _n_items,
                              "prompt_preview": _prompt_prev})
            _parts, _t = [], time.time()
            _tool_round_runtime_error = False
            _tool_round_error_text = ""

            _file_written_prev = bool(_written_files)
            _ui_lc = (ctx.user_input or "").lower()
            # P2-7 FIX: Use module-level keyword tuple instead of inline duplication.
            _read_only_task_request = any(_p in _ui_lc for _p in _READ_ONLY_KEYWORDS)
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
            _loop_detected = False
            if _is_tool_round:
                _tm_what = (_subtask[:52] + '…') if _subtask and len(_subtask) > 52 else (_subtask or 'Writing code')
                _tm_of   = f'/{_n_items}' if _n_items > 1 else ''
                yield await ctx.emit({"type": "status",
                                  "content": f"\U0001f527 Chunk {_di+1}{_tm_of}: {_tm_what} — tool mode ({coder_mdl.split(chr(58))[0]})"})
                # _dtool_sys_eff: Coder-Prompt + OS-Hint + Workspace.
                # Workflow instructions are already in the modular prompt blocks
                # (DUO_CODER_EXPLORED / DUO_CODER_UNEXPLORED) — no ad-hoc concat.
                _os_hint = _build_os_hint()
                _dtool_sys_eff = _duo_coder_sys + _os_hint + (
                    f"\n\nWorkspace root: {str(_ws_str).replace(chr(92), '/')}\n"
                    "Prefix every file path in tool calls with this exact absolute "
                    "path, written with forward slashes.\n"
                )
                # (see _apply_thinking_kwargs when building the tool payload below).
                # TOKEN BUDGET:
                #
                #   Normal:  exec_mdl + critic simultaneously → less room
                #
                #
                # Calculation qwen3.5:4b:
                #   Agentic: alone → ctx=12288, max_tokens=7000
                #     KV@12288: ~1.5GB → total 4.2+1.5=5.7GB < 7.5GB ✓
                #   Normal: +deepseek(1.3GB) → ctx=10240, max_tokens=6000
                #     KV@10240: ~1.2GB → total 4.2+1.2+1.3=6.7GB < 7.5GB ✓
                #     12288-5000=7288 free → max_tokens=7000 fits ✓
                if ctx.duo_config.agentic_mode:
                    _dtool_ctx = _coder_ctx_eff
                else:
                    _dtool_ctx = _coder_ctx_eff
                if ctx.duo_config.until_finished and _di == 0:
                    yield await ctx.emit({
                        "type": "status",
                        "content": f"♾ Until-Finished profile: dedicated coder ctx active ({_dtool_ctx})",
                    })
                # OUTPUT-LIMIT-POLICY (2026-08-12, user decision): tool round
                # output limit = visible agent setting duo_coder max_tokens
                _dtool_opts = {"temperature": 0.6,
                               "num_predict": min(max(int(_duo_coder_tok), 1024), _dtool_ctx),
                               "num_ctx": _dtool_ctx}
                logger.info(
                    "[CODER-BUDGET] model=%s num_predict=%d ctx=%d agent_max_tokens=%d "
                    "effective_max_tokens=%d source=%s preset=%s",
                    coder_mdl, int(_dtool_opts["num_predict"]), int(_dtool_ctx),
                    int(ctx.pipeline.agents["duo_coder"].max_tokens),
                    int(_duo_coder_tok),
                    ("effective_config(learning)" if ctx.use_learned else "agents-setting"),
                    str(ctx.settings.get("active_preset", "") or ""),
                )
                # write_file_append char limits tied to the real output budget).
                try:
                    from tools.runner import _set_write_budget as _set_wb
                    _set_wb(min(max(int(_duo_coder_tok), 1024), int(_dtool_ctx)),
                            float(ctx.settings.get("duo_write_chars_per_token", 2.5)))
                except Exception:
                    pass
                _tool_read_timeout_s = _resolve_tool_read_timeout_seconds(
                    profile=_duo_runtime_profile,
                    until_finished=ctx.duo_config.until_finished,
                )

                _already_read: list[str] = []
                _pass_mode = str(ctx.duo_config.pass_explore_files).lower()
                _ERROR_PREFIXES = ("[Pre-Explore timeout", "[Exploration failed",
                                   "[PRE-EXPLORE", "[PARTIAL]", "[ERROR")
                if _pre_explore_msgs and _is_first_outer_round \
                   and _explore_ctx and not str(_explore_ctx).startswith(_ERROR_PREFIXES):
                    # PRE-EXPLORE CONTEXT FIX (lightweight):
                    #
                    #   [system, user("contents follow ABOVE"), *explore_msgs[2:], final_impl]
                    #
                    #
                    for _pm in _pre_explore_msgs:
                        if _pm.get("role") == "assistant":
                            for _tc in (_pm.get("tool_calls") or []):
                                _tc_args = _parse_tool_args(_tc.get("function", {}).get("arguments", {}))
                                _p = _tc_args.get("path", "")
                                if _p and _p not in _already_read:
                                    _already_read.append(_p)
                    _already_read_str = "\n".join(f"  - {p}" for p in _already_read[:30]) or "  (see tool results above)"

                    _BRIDGE_EXPLORE_CAP = _coder_caps.explore_inject  # CTX-AWARE (2026-08-12)
                    _bridge_explore = _explore_ctx or ""
                    if len(_bridge_explore) > _BRIDGE_EXPLORE_CAP:
                        # MARKER-AWARE (2026-08-17): like direct/chunk inject —
                        # map first up to _coder_map_budget, contracts = rest.
                        _bridge_explore = budget_explore_window(
                            _bridge_explore, _BRIDGE_EXPLORE_CAP, _coder_map_budget,
                        )
                    _bridge_msg = {
                        "role": "user",
                        "content": (
                            (f"## Workspace Structure\n{_tree_ctx}\n\n" if _tree_ctx else "") +
                            f"STOP EXPLORING — START IMPLEMENTING NOW.\n\n"
                            f"The tool calls above are your completed codebase exploration.\n"
                            f"Files already read (avoid re-reading unless a tool error requires it):\n{_already_read_str}\n\n"
                            f"Task: {ctx.user_input}\n\n"
                            f"Plan from exploration:\n{_bridge_explore}\n\n"
                            f"Workspace root: {str(_ws_str).replace(chr(92), '/')}\n"
                            f"Prefix every file path in tool calls with this exact "
                            f"absolute path, written with forward slashes.\n\n"
                            f"Rules:\n"
                            f"- write_file(path, edits) writes a file's FULL content — use it to create a NEW file "
                            f"or to overwrite an existing file completely.\n"
                            f"- To change only PART of an existing file, use edit_file with SEARCH/REPLACE blocks (preferred):\n"
                            f"    edit_file(path=<file>, edits=\"<<<<<<< SEARCH\\n<exact old code>\\n=======\\n<new code>\\n>>>>>>> REPLACE\")\n"
                            f"  For a single small exact-text change, patch_file(path=<file>, old_str=<exact>, new_str=<new>) is also OK.\n"
                            f"- If a tool returns READ_REQUIRED: call read_file once on that path, then retry.\n"
                            f"- Do NOT call list_dir or find_files on directories already shown above.\n"
                            f"- After writing/editing, run_bash to verify."
                        )
                    }
                    # Bridge message always injects _explore_ctx (contracts) into coder prompt.
                    # Toggle controls whether FILE CONTENTS from pre-explore are also included.
                    _explore_history_full = [
                        m for m in (_pre_explore_msgs[2:] if len(_pre_explore_msgs) > 2 else [])
                        if not (m.get("role") == "user"
                                and str(m.get("content", "")).startswith(TREE_HEADER_PREFIX))
                    ]
                    _explore_history = _explore_history_full
                    if _explore_history:
                        _history_chars = sum(len(str(m.get("content", ""))) for m in _explore_history)
                        if _history_chars > 8000:
                            _explore_history_capped: list = []
                            _budget = 8000
                            for m in reversed(_explore_history):
                                _msg_len = len(str(m.get("content", "")))
                                if _budget - _msg_len < 0:
                                    break
                                _explore_history_capped.insert(0, m)
                                _budget -= _msg_len
                            _explore_history = _explore_history_capped
                    _dtool_base = _build_dtool_base(
                        _dtool_sys_eff,
                        _explore_history,
                        _plan_result.plan_content if (_plan_result and _plan_result.plan_content
                                                      and not ctx.duo_config.chunking) else "",
                        _bridge_msg,
                    )
                    # loop_detected rejected). For important runs (important_task
                    # passthrough mode escalates to "all". Explicit "none"
                    _n_explore_files = sum(
                        len(c.get("files_read", [])) for c in (_contracts_raw or [])
                    ) if _contracts_raw else 0
                    _explore_total_chars = sum(
                        len(str(m.get("content", ""))) for m in (_pre_explore_msgs or [])
                    )
                    _pass_escalated = _should_escalate_pass_files(
                        ctx.duo_config.important_task,
                        ctx.duo_config.until_finished,
                        _n_explore_files,
                        _explore_total_chars,
                        _pass_mode,
                    )
                    if not _pass_escalated and _pass_mode == "touched" and not _touched_paths and len(_pre_explore_msgs) > 2:
                        logger.warning("[PASS-FILES] touched_paths=0 despite explore - escalating to 'all'")
                        _pass_escalated = True
                    if _pass_escalated:
                        _pass_mode = "all"
                    logger.warning(
                        "[PASS-FILES] mode=%s touched_paths=%d n_files=%d history_chars=%d escalate=%s",
                        _pass_mode, len(_touched_paths), _n_explore_files,
                        _explore_total_chars, _pass_escalated,
                    )
                    if _pass_mode in ("all", "true", "1") and len(_pre_explore_msgs) > 2:
                        _ALL_BUDGET = 16000
                        _all_block = ""
                        if _touched_paths:
                            _touched_block = _build_touched_context(_pre_explore_msgs[2:], _touched_paths)
                            if _touched_block:
                                _all_block = _touched_block + "\n\n## Additional Pre-Explore History\n\n"
                        _remain_budget = _ALL_BUDGET - len(_all_block)
                        if _remain_budget > 0:
                            _hist_text = "\n".join(
                                json.dumps(m, ensure_ascii=False) for m in _explore_history_full
                            )
                            if len(_hist_text) > _remain_budget:
                                _hist_text = _hist_text[:_remain_budget].rsplit("\n", 1)[0] + "\n[... truncated]"
                            _all_block += _hist_text
                        _bridge_msg["content"] += "\n\n" + _all_block
                        _dtool_msgs = _dtool_base
                    elif _pass_mode == "touched" and _touched_paths and len(_pre_explore_msgs) > 2:
                        _touched_block = _build_touched_context(_pre_explore_msgs[2:], _touched_paths)
                        if _touched_block:
                            _bridge_msg["content"] += _touched_block
                        _dtool_msgs = _dtool_base
                    elif _pass_mode == "none":
                        _dtool_msgs = _dtool_base
                    else:
                        _dtool_msgs = _dtool_base
                        # Inject capped touched-file contents even in default mode
                        # so the coder has key files inline and doesn't re-read them.
                        if _touched_paths and len(_pre_explore_msgs) > 2:
                            _touched_block = _build_touched_context(_pre_explore_msgs[2:], _touched_paths)
                            if _touched_block:
                                if len(_touched_block) > 8000:
                                    _touched_block = _touched_block[:8000].rsplit("\n\n", 1)[0] + "\n\n[... truncated]"
                                _bridge_msg["content"] += _touched_block
                                _touched_block_persisted = _touched_block
                                from tools.runner import _files_in_context; _files_in_context.set(set(_touched_paths))
                else:
                    _dtool_msgs = [{**m,"content":_dtool_sys_eff} if m.get("role")=="system" else m for m in _coder_msgs]
                    _inject_plan_into_coder_msgs(
                        _dtool_msgs, _plan_result,
                        chunking=ctx.duo_config.chunking,
                        is_first_outer_round=_is_first_outer_round,
                    )

                # Pre-populate Read-Guard with files already read by pre-explore.
                # Without this, edit_file on existing files gets READ_REQUIRED
                # even though pre-explore already read them.
                if _already_read:
                    from tools.runner import _files_read_in_run, _files_in_context
                    _read_set = _files_read_in_run.get(None)
                    if _read_set is None:
                        _read_set = set()
                        _files_read_in_run.set(_read_set)
                    if _pass_mode in ("all", "true", "1"):
                        _ctx_set = _files_in_context.get(None)
                        if _ctx_set is None:
                            _ctx_set = set()
                            _files_in_context.set(_ctx_set)
                    elif _pass_mode == "touched":
                        _ctx_set = _files_in_context.get(None)
                        if _ctx_set is None:
                            _ctx_set = set()
                            _files_in_context.set(_ctx_set)
                    else:
                        _ctx_set = _files_in_context.get(None)
                        if _ctx_set is None:
                            _ctx_set = set() if _touched_paths else None
                            if _ctx_set is not None:
                                _files_in_context.set(_ctx_set)
                    for _p in _already_read:
                        _wp = _normalize_tool_path(_p, _ws_str)
                        if _wp:
                            _read_set.add(_wp)
                            if _ctx_set is not None and (_pass_mode in ("all", "true", "1") or _wp in _touched_paths):
                                _ctx_set.add(_wp)
                    # Add touched_paths that were NOT in _already_read
                    # (intersection→union fix: _already_read ∩ _touched_paths
                    # only covered files read by pre-explore workers that were
                    # also marked as touched. Files touched but not explicitly
                    # read_file'd by workers were missing from _ctx_set.)
                    if _ctx_set is not None and _touched_paths:
                        for _tp in _touched_paths:
                            _wp = _normalize_tool_path(str(_tp), _ws_str)
                            if _wp:
                                _ctx_set.add(_wp)

                # Inject persisted read-guard state from .context.json
                # so follow-up runs within the same session remember files
                # read/edited by the previous run.
                # Only inject if the task matches (same task_sig) to avoid
                # cross-task pollution from unrelated runs in the same chat.
                _persisted = _load_chat_context(ctx.chat_id) or {}
                _task_sig_current = hashlib.md5(ctx.user_input[:60].encode()).hexdigest()[:8]
                _task_sig_persisted = _persisted.get("task_sig", "")
                if _task_sig_persisted == _task_sig_current:
                    _persisted_read = set(_persisted.get("files_read_in_run", []))
                    _persisted_touched = set(_persisted.get("touched_paths", []))
                else:
                    _persisted_read = set()
                    _persisted_touched = set()
                if _persisted_read or _persisted_touched:
                    from tools.runner import _files_read_in_run, _files_in_context
                    _read_set = _files_read_in_run.get(None)
                    if _read_set is None:
                        _read_set = set()
                        _files_read_in_run.set(_read_set)
                    _read_set.update(_persisted_read)
                    _ctx_set = _files_in_context.get(None)
                    if _ctx_set is None:
                        _ctx_set = set()
                        _files_in_context.set(_ctx_set)
                    _ctx_set.update(_persisted_touched)
                    _ctx_set.update(_persisted_read)

                _contracts_usable = any(
                    c.get("exports") or c.get("role") or c.get("purpose")
                    for c in (_contracts_raw or []) if isinstance(c, dict)
                )
                _explore_usable = bool(_explore_ctx and len(_explore_ctx) > 500 and _contracts_usable)
                _plan_content_available = bool(
                    _plan_result is not None and _plan_result.plan_content
                )
                if not _explore_usable and _is_first_outer_round and not _plan_content_available:
                    _explore_fallback_msg = {
                        "role": "user",
                        "content": (
                            "[PRE-EXPLORE MISSING] No exploration context available.\n"
                            "Your first 3 tool calls MUST be exploration only:\n"
                            "  1. find_files or search_code to locate relevant files.\n"
                            "  2. read_file on the 2 most likely entry points.\n"
                            "  3. Then proceed with the task.\n"
                            f"Task: {ctx.user_input[:300]}"
                        ),
                    }
                    _dtool_msgs.append(_explore_fallback_msg)

                _goal_summary = ctx.user_input[:_coder_caps.goal_pin]  # CTX-AWARE (2026-08-12)
                _goal_pin_msg: dict | None = {
                    "role":    "user",
                    "content": (
                        f"[GOAL — this is your fixed objective for this chunk, "
                        f"do not lose track of it]\n{_goal_summary}"
                    ),
                }
                if _dtool_msgs and _goal_pin_msg:
                    if len(_dtool_msgs) > 1 and _dtool_msgs[1].get("role") == "user":
                        _existing_content = _dtool_msgs[1].get("content", "")
                        _dtool_msgs[1] = {
                            **_dtool_msgs[1],
                            "content": _goal_pin_msg["content"] + "\n\n" + str(_existing_content)
                        }
                    else:
                        _dtool_msgs = (
                            [_dtool_msgs[0], _goal_pin_msg]
                            + _dtool_msgs[1:]
                        )

                # ctx.duo_config.thinking_per_chunk=True: Thinking in EVERY chunk.
                # New:
                #   - ctx.duo_config.thinking_per_chunk=True → thinking before EVERY chunk (chunking active)
                _run_thinking_now = (
                    (ctx.duo_config.agentic_thinking or ctx.duo_config.thinking_per_chunk)
                    and not ctx.aborted()
                    and not ctx.duo_config.planner
                )
                if _run_thinking_now:
                    ctx.exec_ctrl.transition(AgentState.PLANNING)
                    ctx.exec_ctrl.reset_stuck_detection()  # BUG-NO-RESET FIX
                    yield await ctx.emit({"type": "status",
                        "content": f"🧠 Planning ({coder_mdl.split(':')[0]}, Thinking)…"})
                    # OUTSOURCED: Inloop planner delegated to run_inloop_planner().
                    _plan_seed_task = _coder_input or ctx.user_input
                    if _is_first_outer_round and _fup_task_ctx:
                        _plan_seed_task = (
                            _plan_seed_task
                            + "\n\n[Previous task in this chat — context for this follow-up]\n"
                            + _fup_task_ctx
                        )
                    _plan_seed_ctx = _explore_ctx if _is_first_outer_round else ""
                    _plan_port_inloop = None
                    try:
                        from backend.llama_server_manager import manager as _lsm3
                        _plan_port_inloop = await _lsm3.ensure_loaded(exec_mdl, num_ctx=_dtool_ctx, n_parallel=1)
                    except Exception as _port_err:
                        logger.warning("Inloop planner: port not available (%s)", _port_err)
                    _inloop_plan_text = ""
                    _inloop_plan_thinking = ""
                    _inloop_plan_fallback = False
                    if _plan_port_inloop is not None:
                        # Compute thinking budget
                        _exec_thinking_budget = _calculate_thinking_tokens(
                            exec_mdl,
                            ctx.settings,
                            input_tokens=len((_make_duo_thinking_planner_sys(_planner_step_cap, model_name=exec_mdl) or "") + (_plan_seed_task or "")) // 3,
                            available_ctx=_dtool_ctx,
                            agent_name="planner",
                        )
                        # Event-Bridge: run_inloop_planner emit_fn → asyncio.Queue → yield await ctx.emit
                        _inloop_event_q: asyncio.Queue = asyncio.Queue(maxsize=50)
                        async def _inloop_plan_emit_fn(event: dict):
                            try:
                                _inloop_event_q.put_nowait(event)
                            except asyncio.QueueFull:
                                pass
                        try:
                            _inloop_plan_task = asyncio.create_task(
                                run_inloop_planner(
                                    task=_plan_seed_task,
                                    explore_ctx=_plan_seed_ctx,
                                    planner_model=exec_mdl,
                                    planner_port=_plan_port_inloop,
                                    step_cap=_planner_step_cap,
                                    use_thinking=_exec_has_thinking,
                                    thinking_budget=_exec_thinking_budget,
                                    planner_ctx=_dtool_ctx,
                                    # Hard-cap the content budget; thinking budget comes in planner.py
                                    max_output_tokens=min(_dtool_ctx, 4096),
                                    websearch_available=ctx.settings.get("duo_websearch_enabled", False) and ctx.websearch_available,
                                    settings=ctx.settings,
                                    aborted_fn=ctx.aborted,
                                    emit_fn=_inloop_plan_emit_fn,
                                )
                            )
                            # Yield events from the bridge while planner runs
                            while not _inloop_plan_task.done() or not _inloop_event_q.empty():
                                if _duo_deadline_at and time.time() >= _duo_deadline_at:
                                    _duo_timed_out = True
                                    logger.warning(
                                        "[INLOOP-PLANNER] Run deadline exceeded - planner aborted (run timeout %ss)",
                                        _duo_run_timeout_s,
                                    )
                                    _inloop_plan_task.cancel()
                                    break
                                while not _inloop_event_q.empty():
                                    try:
                                        _evt = _inloop_event_q.get_nowait()
                                        yield await ctx.emit(_evt)
                                    except asyncio.QueueEmpty:
                                        break
                                await asyncio.sleep(0.3)
                            try:
                                _inloop_plan_text, _inloop_plan_thinking, _inloop_plan_fallback = _inloop_plan_task.result()
                            except asyncio.CancelledError:
                                _inloop_plan_text = ""
                                _inloop_plan_thinking = ""
                                _inloop_plan_fallback = True
                        except Exception as _plan_err:
                            _plan_err_name = type(_plan_err).__name__
                            _plan_err_msg = (str(_plan_err).strip() or repr(_plan_err))[:220]
                            logger.warning(
                                "Thinking-planning step error (%s): %s",
                                _plan_err_name,
                                _plan_err_msg,
                            )
                            _inloop_plan_text = _fallback_planner_steps(
                                _goal_summary or ctx.user_input,
                                max_steps=max(3, min(6, _planner_step_cap)),
                            )
                            _inloop_plan_fallback = True
                            yield await ctx.emit({"type": "status",
                                "content": (
                                    f"⚠ Thinking plan error ({_plan_err_name})"
                                    + (f": {_plan_err_msg[:90]}" if _plan_err_msg else "")
                                    + " - fallback plan active"
                                )})
                    else:
                        _inloop_plan_text = _fallback_planner_steps(
                            _goal_summary or ctx.user_input,
                            max_steps=max(3, min(6, _planner_step_cap)),
                        )
                        _inloop_plan_fallback = True
                        yield await ctx.emit({"type": "status",
                            "content": "⚠ Planner: no model port available — fallback plan active"})
                    _plan_label = "Plan (fallback)" if _inloop_plan_fallback else "Plan"
                    if _inloop_plan_thinking and not _inloop_plan_fallback:
                        _plan_label = "Plan (from thinking)" if not _inloop_plan_text else "Plan"

                    # PATCH-1: rebuild the plan tracker from inloop planner output.
                    if (_plan_tracker is not None
                            and _inloop_plan_text
                            and not _inloop_plan_fallback):
                        try:
                            _plan_tracker.rebuild_from_plan(_inloop_plan_text)
                            logger.debug("[PlanTracker] Rebuilt from inloop planner: %d steps", _plan_tracker.total)
                            logger.info(
                                "[PlanTracker] Inloop planner produced %d parseable step(s) (fallback=%s)",
                                _plan_tracker.total,
                                _inloop_plan_fallback,
                            )
                        except Exception as _err:
                            if isinstance(_err, (
                                GeneratorExit,
                                asyncio.CancelledError,
                                KeyboardInterrupt,
                                SystemExit,
                            )):
                                raise
                            logger.debug(
                                "[DUO] Suppressed error at plan-tracker rebuild: %s",
                                _err, exc_info=True
                            )

                    _dtool_msgs.append({"role": "assistant", "content": _inloop_plan_text})
                    _dtool_msgs.append({"role": "user", "content":
                        "Good. Now execute this plan step by step using the tools."})
                    yield await ctx.emit({"type": "token",
                        "content": "\n**" + _plan_label + ":**\n" + _inloop_plan_text + "\n\n"})
                    yield await ctx.emit({
                        "type": "planner_done",
                        "summary": "Planning complete" + (" (fallback)" if _inloop_plan_fallback else " — execution starts"),
                    })
                    if _inloop_plan_fallback:
                        logger.warning(
                            "[PlanTracker] Inloop planner fallback active -- no parseable plan from the 35B "
                            "(plan_text_len=%d, plan_thinking_len=%d)",
                            len(_inloop_plan_text) if _inloop_plan_text else 0,
                            len(_inloop_plan_thinking) if _inloop_plan_thinking else 0,
                        )
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
                        f"patch_file, then run tests to confirm. Do not ask — just fix."
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
                    # P1-3 FIX: Cache coder port — coder is pinned (keep_alive=-1), so
                    # ensure_loaded is a no-op after the first call but still costs
                    # ~200-300ms per /health re-check on Vulkan.
                    if _cached_coder_port is not None:
                        _target_ctx = _dtool_opts.get("num_ctx", 4096)
                        if _cached_coder_port_ctx is not None and _cached_coder_port_ctx != _target_ctx:
                            logger.info(
                                "[DUO] Cached port ctx mismatch (cached=%s, needed=%s) — force reload",
                                _cached_coder_port_ctx, _target_ctx,
                            )
                            _cached_coder_port = None
                            _cached_coder_port_ctx = None
                            try:
                                from backend.llama_vram_table import (
                                    wait_for_vram_reclaim as _wvr_reclaim,
                                    vram_of_moe as _wvr_moe,
                                )
                                _wvr_target = int(_wvr_moe(exec_mdl, _target_ctx) * 1024 + 768)
                                await _wvr_reclaim(_wvr_target, timeout_sec=45)
                            except Exception:
                                pass
                        else:
                            _dport = _cached_coder_port
                            # Touch keep-alive so idle monitor doesn't kill model mid-run
                            try:
                                await _lsm3.touch_if_loaded(exec_mdl)
                            except Exception as _err:
                                if isinstance(_err, (
                                    GeneratorExit,
                                    asyncio.CancelledError,
                                    KeyboardInterrupt,
                                    SystemExit,
                                )):
                                    raise
                                logger.debug(
                                    "[DUO] Suppressed error at keep-alive touch: %s",
                                    _err, exc_info=True
                                )
                    else:
                        for _connect_attempt in range(3):
                            try:
                                _dport = await _lsm3.ensure_loaded(exec_mdl, num_ctx=_dtool_opts.get("num_ctx", 4096), n_parallel=1)
                                break
                            except Exception as _ce:
                                if _connect_attempt < 2:
                                    yield await ctx.emit({"type": "status",
                                        "content": f"⟳ Model unreachable, attempt {_connect_attempt+2}/3 in 3s…"})
                                    await asyncio.sleep(3.0)
                                else:
                                    _dport = None
                                    logger.warning("[DUO] ensure_loaded for %s failed after 3 attempts: %s",
                                                   exec_mdl, _ce)
                        _cached_coder_port = _dport
                        _cached_coder_port_ctx = _dtool_opts.get("num_ctx", 4096)
                    if _dport is None:
                        _ld_setter(2497); _loop_detected = True
                        yield await ctx.emit({"type": "status",
                            "content": f"⛔ Coder model {exec_mdl} could not be loaded "
                                       f"(VRAM/ctx?) — run stopped."})
                        break
                    _dtc_owned = getattr(getattr(ctx.pipeline, 'ollama', None), '_client', None) is None
                    _dtc = getattr(getattr(ctx.pipeline, 'ollama', None), '_client', None) or httpx.AsyncClient(
                        limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                        timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=5.0),
                    )
                    _stored_threshold = int(ctx.settings.get("duo_compress_threshold", 0))
                    _dynamic_threshold = _dtool_ctx - _dtool_opts.get("num_predict", 800) - 8200
                    _plan_state = ""  # initialized before loop; updated during compression
                    _compress_threshold = max(
                        _stored_threshold if _stored_threshold > 0 else _dynamic_threshold,
                        int(_dtool_ctx * 0.8)
                    )
                    _max_tool_rounds_cfg = int(ctx.settings.get("duo_max_tool_rounds", 64))
                    _max_tool_rounds_cfg = 8 if duo_rounds > 1 else _max_tool_rounds_cfg
                    _max_tool_rounds = _resolve_tool_budget(
                        _max_tool_rounds_cfg,
                        ctx.duo_config.until_finished,
                        profile=_duo_runtime_profile,
                    ) + _accumulated_replan_bonus
                    _loop_detected = False
                    _duo_state = DuoRoundState(think_runtime=_coder_tool_think, exec_model=exec_mdl,
                                               dtool_opts=_dtool_opts, tool_read_timeout_s=_tool_read_timeout_s,
                                               cached_port=_cached_coder_port, current_port=_dport)
                    _dr_think_only_retries = 0
                    _dr_invalid_tool_retries = 0     # tracks rounds where ALL tool-calls were rejected
                    _dr_dropped_tool_retries = 0     # DROPPED-FIX: rounds where tool-calls had malformed JSON args
                    _limit_warned = False  # round-limit warning: inject only once
                    _replan_bonus_granted = False  # extra rounds: grant once per run
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
                    _MAX_COMPRESSIONS = 6  # raised from 3 — 40+ round tasks need more headroom
                    _call_sigs: list = []  # loop detection: incremental signatures
                    _last_too_large_path: str = ""
                    _attempts_per_file: dict = {}
                    _tool_error_retries: dict = {}
                    _tool_ctx_lru = ToolContextLRU(default_ttl=int(ctx.settings.get("duo_tool_output_ttl", 3) or 3))
                    # P2-7 FIX: Use module-level keyword tuple + reuse _read_only_task_request
                    _read_only_task = _read_only_task_request
                    _tool_mode = "duo_readonly" if _read_only_task else "duo_full"
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
                    _run_id_global = ctx.chat_id or ctx.run_id
                    _current_run_id.set(_run_id_global)
                    _pause_timeout_s.set(ctx.duo_config.pause_timeout_s)
                    _web_search_count.set([0])
                    _install_count.set([0])
                    from infra.ask_user_governor import configure_run as _configure_governor
                    _configure_governor(_run_id_global, until_finished=ctx.duo_config.until_finished, settings=ctx.settings)
                    # ── Build hooks for shared tool executor ──
                    async def _stuck_handler(tn, ta, tr):
                        nonlocal _loop_detected, _read_counts, _last_write_round, _explore_only_rounds, _round_wrote_file
                        if tn == "run_bash":
                            ctx.exec_ctrl.transition(AgentState.VERIFY)
                            ctx.exec_ctrl.record_output(tr, tool_name="run_bash")
                            if ctx.exec_ctrl.is_stuck():
                                await ctx.emit({"type": "token", "content": f"\n⚠ [Until-Finished] Same test output {ctx.exec_ctrl.max_repeats}× — no progress. Declaring blocker and stopping.\n"})
                                _ld_setter(2604); _loop_detected = True
                                ctx.exec_ctrl.abort(StopReason.STUCK_IN_LOOP)
                                return True
                        elif tn in ("patch_file", "edit_file"):
                            _read_counts.clear()
                            _last_write_round = _total_tool_rounds
                            ctx.exec_ctrl.transition(AgentState.VERIFY)
                            # Failed/blocked edits (e.g. READ_REQUIRED) must not
                            # count as progress. 2x BLOCKED + 1x successful edit = "3x identical"
                            from tools.errors import tool_call_failed as _tcf_stuck
                            if not _tcf_stuck(tr, tn):
                                _round_wrote_file = True
                                # — false-positive stop in chunk 4 with 2/8 chunks done.
                                ctx.exec_ctrl.reset_stuck_detection()
                            if ctx.exec_ctrl.is_stuck():
                                await ctx.emit({"type": "token", "content": f"\n⚠ [Until-Finished] Same {tn} on same file {ctx.exec_ctrl.max_repeats}× — no progress. Declaring blocker and stopping.\n"})
                                _ld_setter(2623); _loop_detected = True
                                ctx.exec_ctrl.abort(StopReason.STUCK_IN_LOOP)
                                return True
                        elif tn == "read_file":
                            _raw_fp = str(ta.get("path", "") or "")
                            fp = os.path.normcase(os.path.normpath(_raw_fp)).replace("\\", "/")
                            for _pfx in (
                                (os.path.normcase(os.path.normpath(_ws_str or "")).replace("\\", "/") + "/"),
                                "/workspace/",
                                "./",
                            ):
                                if fp.startswith(_pfx):
                                    fp = fp[len(_pfx):]
                                    break
                            # blocked by the FILE_TOO_LARGE_NEED_RANGE guard against full reads;
                            # 4 chunk reads of the same file = false-positive read loop
                            # (coder 4m53s, loop_detected, written_files=0). New: range-aware
                            # key via read_loop_key — different ranges = progress,
                            _rk = read_loop_key(fp, ta.get("start_line"), ta.get("end_line"), tr)
                            if _rk is not None:
                                _read_counts[_rk] = _read_counts.get(_rk, 0) + 1
                                if _read_counts[_rk] >= 4 and (_total_tool_rounds - _last_write_round) >= 4:
                                    await ctx.emit({"type": "token",
                                        "content": f"\n⚠ [Until-Finished] Read {fp} {_read_counts[_rk]}× without edits — read-loop detected. Stopping."})
                                    _loop_detected = True
                                    ctx.exec_ctrl.abort(StopReason.STUCK_IN_LOOP)
                                    return True
                        elif tn in ("write_file", "write_file_append", "replace_lines"):
                            _read_counts.clear()
                            _last_write_round = _total_tool_rounds
                            # WRITE-PROGRESS-FIX (Run 14): like edit_file — a successful
                            from tools.errors import tool_call_failed as _tcf_stuck2
                            if not _tcf_stuck2(tr, tn):
                                _round_wrote_file = True
                                ctx.exec_ctrl.reset_stuck_detection()
                        elif tn in ("find_files", "search_code", "list_dir"):
                            pass  # counted at round-level below
                        elif tn == "run_tests":
                            # 06:34): green verification = progress — invalidates
                            # calls in between get.
                            from tools.errors import tool_call_failed as _tcf_stuck3
                            if not _tcf_stuck3(tr, tn):
                                ctx.exec_ctrl.reset_stuck_detection()
                        return False
                    async def _evict_model_handler(model: str):
                        if not ctx.duo_config.evict_on_pause:
                            return
                        try:
                            from backend.llama_server_manager import manager as _lsm_pause
                            await _lsm_pause.evict(model)
                            await ctx.emit({"type": "status", "content": "💤 Model unloaded from VRAM during pause."})
                        except Exception as _evict_err:
                            logger.warning(f"evict_on_pause failed: {_evict_err}")
                    # ── Coder live streaming (2026-08-18) ──────────────────────
                    # never blocks (queue never fills up). Pattern like the planner bridge.
                    _coder_event_q: asyncio.Queue = asyncio.Queue(maxsize=20)
                    _coder_real_prompt_tokens: list = [0]
                    _prev_msg_sig: list = [None]

                    async def _coder_emit_fn(event: dict) -> str:
                        if isinstance(event, dict) and event.get("type") == "usage_meta" and event.get("prompt_tokens"):
                            _coder_real_prompt_tokens[0] = int(event["prompt_tokens"])
                        _sse = await ctx.emit(event)
                        try:
                            _coder_event_q.put_nowait(_sse)
                        except asyncio.QueueFull:
                            pass
                        return _sse

                    _tool_exec_hooks = ToolExecHooks(
                        emit=_coder_emit_fn,
                        is_aborted=lambda cid: bool(ctx.is_aborted_chat(cid)),
                        on_tool_result=_stuck_handler,
                        remember_insight=ctx.memory.remember_repo_insight,
                        evict_model=_evict_model_handler,
                    )
                    for _dr in range(_max_tool_rounds):
                        _round_t0 = time.time()
                        if ctx.aborted() or ctx.is_aborted_chat(ctx.chat_id) or _loop_detected:
                            break
                        if time.time() >= _duo_deadline_at:
                            _duo_timed_out = True
                            _ld_setter(2677); _loop_detected = True
                            yield await ctx.emit({
                                "type": "status",
                                "content": "⏱ Tool loop ended due to run timeout.",
                            })
                            break
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
                        if _total_tool_rounds > 0 and _total_tool_rounds % 10 == 0:
                            _hc_ok = False
                            try:
                                _hc_url = f"http://127.0.0.1:{_dport}/v1/models"
                                async with httpx.AsyncClient(timeout=httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)) as _hc_client:
                                    _hc_r = await _hc_client.get(_hc_url)
                                    _hc_ok = _hc_r.status_code < 500
                            except Exception:
                                pass
                            if not _hc_ok:
                                logger.warning("[HEALTH-CHECK] llama-server port %s unreachable - trying reload", _dport)
                                try:
                                    from backend.llama_server_manager import manager as _lsm_hc
                                    await _lsm_hc.evict(exec_mdl)
                                    _dport = await _lsm_hc.ensure_loaded(exec_mdl, num_ctx=_dtool_opts.get("num_ctx", 4096), n_parallel=1)
                                    _cached_coder_port = _dport
                                    _cached_coder_port_ctx = _dtool_opts.get("num_ctx", 4096)  # CTX-GUARD
                                    yield await ctx.emit({"type": "status",
                                        "content": "🩺 Health check: server restarted."})
                                except Exception as _hc_exc:
                                    logger.error("[HEALTH-CHECK] Server reload failed: %s", _hc_exc)
                                    _ld_setter(2716); _loop_detected = True
                                    yield await ctx.emit({"type": "status",
                                        "content": f"⛔ Server dead after health check (port {_dport}) — run aborted."})
                                    break
                        # (tool-result messages) — now the central repaired function.
                        _est_tokens = _estimate_ctx_tokens(_dtool_msgs)
                        # GUARD-REAL-BASIS (2026-08-26): all guard decisions on the
                        # (evict/compress/90%/72%/notices) use the real
                        # prompt_tokens value of the last round, fallback to the
                        # estimator (round 1 / backend without usage_meta).
                        _guard_tokens = int(_coder_real_prompt_tokens[0] or _est_tokens)
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
                                "ctx_limit": int(_dtool_ctx),
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
                        # Phase 2: semantic LRU eviction of stale tool outputs.
                        # Keep conversational turns, evict low-TTL tool payloads first.
                        _near_limit = int(_dtool_ctx * 0.78)
                        if _guard_tokens > _near_limit:
                            _est_before_evict = int(_est_tokens)
                            _evicted_n = _evict_stale_tool_outputs(
                                messages=_dtool_msgs,
                                lru=_tool_ctx_lru,
                                target_token_budget=int(_dtool_ctx * 0.70),
                                hard_floor_tokens=int(_dtool_ctx * 0.62),
                            )
                            if _evicted_n > 0:
                                _ctx_evictions += int(_evicted_n)
                                _est_tokens = _estimate_ctx_tokens(_dtool_msgs)
                                # METER-FIX (2026-08-25): the real value is now STALE
                                # (pre-eviction). Without a reset, the context meter
                                # shows the old (too high) reading at the next round
                                # start until a new usage_meta arrives.
                                _coder_real_prompt_tokens[0] = 0
                                logger.warning(
                                    "[CTX-EVICT] est_before=%d near_limit=%d evicted=%d est_after=%d ctx=%d target=%d hard_floor=%d real_before=%d",
                                    _est_before_evict, _near_limit, _evicted_n, int(_est_tokens), int(_dtool_ctx),
                                    int(_dtool_ctx * 0.70), int(_dtool_ctx * 0.62), int(_guard_tokens),
                                )
                                yield await ctx.emit({
                                    "type": "status",
                                    "content": (
                                        f"🧹 Semantic context eviction: {_evicted_n} stale tool output(s) "
                                        "replaced by recall markers."
                                    ),
                                })
                        _compress_ok = (
                            (_MAX_COMPRESSIONS != -1 or _force_compress_next)  # sentinel bypass for structural fallback
                            and (
                                (_total_tool_rounds > 0 and _guard_tokens > _compress_threshold)
                                or _guard_tokens > int(_dtool_ctx * 0.90)
                                or _force_compress_next
                            )
                        )
                        if _compress_ok:
                            # it's in hivemind.log.
                            _compress_reason = (
                                "force" if _force_compress_next else
                                ("90pct" if _guard_tokens > int(_dtool_ctx * 0.90) else "threshold")
                            )
                            logger.warning(
                                "[CTX-COMPRESS] trigger=%s est=%d threshold=%d ctx=%d rounds=%d real=%d",
                                _compress_reason, int(_est_tokens), int(_compress_threshold),
                                int(_dtool_ctx), _total_tool_rounds, int(_guard_tokens),
                            )
                            _force_compress_next = False
                            yield await ctx.emit({"type": "status",
                                "content": f"🗜 Context compression ({_est_tokens} est. tokens → compressing...)"})
                            yield await ctx.emit({"type": "ctx_meter",
                                "est_tokens": int(_coder_real_prompt_tokens[0] or _est_tokens), "ctx_limit": _dtool_ctx,
                                "compressing": True})
                            _sys_for_compress = _dtool_sys_eff
                            _plan_state = ""
                            if _plan_tracker and _plan_tracker.total > 0:
                                _cur = _plan_tracker._current_step()
                                _cur_label = _cur.intent if _cur else "?"
                                _done = len(_plan_tracker._plan.completed_step_ids)
                                _plan_state = (
                                    f"Plan: step {_done + 1}/{_plan_tracker.total} "
                                    f"— {_cur_label}"
                                )
                            # PLAN-ANCHOR-FIX (2.2): anchor text via pure module function
                            # (testable, identical logic — see _build_plan_anchor_text).
                            _plan_anchor_text = _build_plan_anchor_text(_subtasks, _plan_tracker)
                            _msgs_before_compress = _dtool_msgs
                            _est_tokens_before_compress = _estimate_ctx_tokens(_dtool_msgs)
                            _dtool_msgs, _condensed_files, _compress_usage = await _compress_tool_context(
                                messages=_dtool_msgs,
                                model=exec_mdl,
                                port=_dport,
                                client=_dtc,
                                system_prompt=_sys_for_compress,
                                original_task=ctx.user_input,
                                written_files=_written_files,
                                done_tasks=_done_tasks,
                                goal_pin=_goal_pin_msg,
                                keep_recent_msgs=(18 if ctx.duo_config.until_finished else 12),
                                plan_state=_plan_state,
                                plan_anchor_text=_plan_anchor_text,
                                last_test_status=_last_test_status,
                                explore_ctx=_explore_ctx,
                                tool_rounds=_total_tool_rounds,
                                max_tool_rounds=_max_tool_rounds,
                            )
                            if _compress_usage and _compress_usage.get("completion_tokens"):
                                yield await ctx.emit({"type": "usage_meta", "phase": "coder",
                                    "completion_tokens": int(_compress_usage["completion_tokens"]),
                                    "prompt_tokens": int(_compress_usage.get("prompt_tokens") or 0),
                                    # TOKEN-TRACKER (2026-08-25): compression request
                                    "cached_tokens": int(_compress_usage.get("cached_tokens") or 0),
                                    "gen_ms": int(_compress_usage.get("gen_ms") or 0)})
                            if _condensed_files:
                                from tools.runner import _files_read_in_run as _fic_read_ctx
                                _read_set_c = _fic_read_ctx.get(None)
                                if _read_set_c:
                                    _removed_read = set()
                                    for _cfp in _condensed_files:
                                        _cfp_norm = _normalize_tool_path(_cfp, _ws_str)
                                        if _cfp_norm and _cfp_norm in _read_set_c:
                                            _read_set_c.discard(_cfp_norm)
                                            _removed_read.add(_cfp_norm)
                                    if _removed_read:
                                        logger.warning(
                                            "[READ-GUARD] Compression: removed %d compacted read_file paths from the read guard (keep for tail remains)",
                                            len(_removed_read),
                                        )
                            # Validate compression summary quality
                            _cs_summary = (_dtool_msgs[1].get("content", "") if len(_dtool_msgs) >= 2 else "")
                            from context.compression import _validate_compression_summary
                            _known_parts = re.findall(r'partition\s*=\s*"([^"]+)"', _explore_ctx or "")
                            _cs_valid = _validate_compression_summary(
                                _cs_summary, _written_files, _done_tasks,
                                known_partitions=_known_parts,
                                plan_anchor=_plan_anchor_text)
                            if not _cs_valid:
                                logger.warning("[COMPRESSION] Summary failed validation — using enriched fallback")
                            # BUG-2 FIX: Compress-Fallback erkennen → UI-Warning.
                            if not _cs_valid:
                                # Allow structural fallback (no "COMPRESSION FAILED" sentinel)
                                _cs_valid = "COMPRESSION FAILED" not in (
                                    _dtool_msgs[-1].get("content", "") or ""
                                )
                            if not _cs_valid:
                                yield await ctx.emit({"type": "status",
                                    "content": "⚠ Compression failed — keeping uncompressed context"})
                                _dtool_msgs = _msgs_before_compress  # Restore original, don't inject degraded summary
                            else:
                                _ctx_compressions += 1
                                if _ctx_compressions < _MAX_COMPRESSIONS:
                                    _total_tool_rounds = 0
                                    ctx.exec_ctrl.sync_tool_rounds(0)
                                    if _lifetime_tool_rounds >= _max_tool_rounds * 6:
                                        _MAX_COMPRESSIONS = -1
                                        yield await ctx.emit({
                                            "type": "system_warning",
                                            "code": "lifetime_tool_cap",
                                            "message": f"Lifetime tool-round cap reached ({_lifetime_tool_rounds} rounds) — no further compressions allowed",
                                        })
                                else:
                                    _force_compress_next = False
                                    _MAX_COMPRESSIONS = -1
                                    yield await ctx.emit({
                                        "type": "system_warning",
                                        "code": "compression_cap_reached",
                                        "message": "Context compression cap reached — oldest context may be lost",
                                    })
                                _ctx_pressure_warned = False
                                _ctx_critical_warned = False
                            # can run up to the run deadline (24h in until_finished). Success = >=10%
                            _est_tokens_after_compress = _estimate_ctx_tokens(_dtool_msgs)
                            _compress_fail_streak, _compress_stop = _compress_fail_streak_update(
                                _compress_fail_streak,
                                _est_tokens_after_compress < _est_tokens_before_compress * 0.90,
                                3,
                            )
                            if _compress_stop:
                                _ld_setter(2906); _loop_detected = True
                                yield await ctx.emit({
                                    "type": "status",
                                    "content": "⛔ Context compression repeatedly fails to shrink the context (3x) — run stopped.",
                                })
                                break
                            _est_tokens = _est_tokens_after_compress
                            # METER-FIX (2026-08-25): real prompt tokens are STALE
                            # after the compression (pre-compress value). The reset
                            # forces the next ctx_meter onto the fresh heuristic,
                            # until the next usage_meta delivers the new real value.
                            _coder_real_prompt_tokens[0] = 0
                            # D1-DIAG: log the result (shrinkage).
                            logger.warning(
                                "[CTX-COMPRESS] done before=%d after=%d condensed_files=%d",
                                int(_est_tokens_before_compress), int(_est_tokens_after_compress),
                                len(_condensed_files or []),
                            )
                            yield await ctx.emit({
                                "type": "status",
                                "content": f"✅ Context compressed ({_est_tokens_before_compress} → {_est_tokens_after_compress} est. tokens)",
                            })
                            yield await ctx.emit({
                                "type": "ctx_meter",
                                "est_tokens": int(_est_tokens_after_compress),
                                "ctx_limit": int(_dtool_ctx),
                                "compressing": False,
                            })
                            _tool_ctx_lru.reset()
                            # COMPRESS-CLEANUP-FIX (2026-08-31): after a successful
                            # compression, pre-compression loop-detection signatures
                            # and stale CTX warnings are stale.
                            # 1) clear call_sigs: pre-compression signatures must not
                            #    form ABAB/3x with post-compression calls
                            #    (observed live: compression -> 3 calls -> loop_detected).
                            # 2) remove stale [RUNTIME NOTICE]/[CTX CRITICAL] messages
                            #    (the context just became free — the warning contradicts).
                            # 3) re-inject the plan pin: the coder must know after the
                            #    compression which subtask is running and what still
                            #    comes (the fallback summary loses the anchor otherwise).
                            try:
                                _call_sigs.clear()
                                _read_counts.clear()
                                _seen_explore_paths.clear()
                                _explore_only_rounds = 0
                                _no_write_rounds = 0
                                _no_write_nudged = False
                                _dtool_msgs = _strip_stale_ctx_notices(_dtool_msgs)
                                logger.warning("[COMPRESS-CLEANUP] call_sigs cleared + stale CTX notices removed (msgs=%d)", len(_dtool_msgs))
                            except Exception as _cc_err:
                                logger.debug("[COMPRESS-CLEANUP] Cleanup failed: %s", _cc_err)
                            # PLAN-REINJECT-FIX (2026-08-31): plan pin as its own
                            # user message, so the coder keeps plan overview even
                            # with LLM-fallback compression. _plan_anchor_text
                            # already flowed into the summary (compression.py);
                            # this pin additionally provides done/current/remaining as
                            # a compact checklist.
                            try:
                                if _subtasks and _di is not None and _n_items:
                                    _pin_lines = []
                                    for _pi, _pt in enumerate(_subtasks):
                                        if _pi < _di:
                                            _pin_lines.append(f"  {_pi+1}. \u2713 {str(_pt)[:120]}")
                                        elif _pi == _di:
                                            _pin_lines.append(f"  {_pi+1}. \u2192 {str(_pt)[:120]}  \u25c0 YOU ARE HERE")
                                        else:
                                            _pin_lines.append(f"  {_pi+1}. \u25cb {str(_pt)[:120]}")
                                    if _pin_lines:
                                        _dtool_msgs.append({"role": "user", "content":
                                            f"[PLAN-PIN - current subtask {_di+1}/{_n_items}]\n"
                                            + "\n".join(_pin_lines)
                                        })
                                        logger.warning("[COMPRESS-PLAN-PIN] %d subtasks re-injected (current=%d)", _n_items, _di + 1)
                            except Exception as _pp_err:
                                logger.debug("[COMPRESS-PLAN-PIN] Pin failed: %s", _pp_err)
                            # READ-GUARD-FIX: update _files_in_context with paths from the compression summary
                            # so the model doesn't re-read files already summarized.
                            # empty pre-explore (static-map fallback) + first compression crashed
                            if _cs_summary:
                                from tools.runner import _files_in_context as _fic_guard
                                _ctx_set = _fic_guard.get(None)
                                if _ctx_set is not None and _ws_str:
                                    _compressed_paths = set()
                                    for _pat in (_RE_WIN_PATH, _RE_UNIX_PATH):
                                        for _m in _pat.finditer(_cs_summary):
                                            _compressed_paths.add(_m.group().replace("\\", "/"))
                                    for _cp in _compressed_paths:
                                        _wp = _normalize_tool_path(_cp, _ws_str)
                                        if _wp:
                                            _ctx_set.add(_wp)
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
                        import json as _json_roles
                        logger.debug(
                            "[ROLE-DUMP] round=%d system_count=%d roles=%s",
                            _di, sum(1 for m in _dtool_msgs if m.get("role") == "system"),
                            _json_roles.dumps([m.get("role") for m in _dtool_msgs]),
                        )
                        # D3-NOISE-FIX (2026-08-22): pure tail append (new messages
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
                                        if _cum_chars / 3.5 < 10000:
                                            _changed_early.append((_mi, int(_cum_chars / 3.5), _s_prev, _s_cur))
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
                            "max_tokens": min(int(_dtool_opts.get("num_predict", 2048) or 2048), int(_dtool_ctx)),
                            "thinking": _thinking,
                            "thinking_budget": max(0, _tool_thinking_budget),
                        }
                        if _profile.get("seed") is not None:
                            _tool_payload["seed"] = int(_profile["seed"])
                        if _profile.get("min_p", 0.0) != 0.0:
                            _tool_payload["min_p"] = _profile["min_p"]
                        if _profile.get("cache_prompt"):
                            _tool_payload["cache_prompt"] = True
                        _tool_payload = _apply_thinking_kwargs(
                            _tool_payload, _profile, _thinking,
                            _coder_tool_think and _tool_thinking_budget > 0,
                        )
                        # delta.thinking/reasoning_content → ctx.emit as thinking_token + accumulate.
                        # AgenticToolLoop: POST+retry+SSE parse. Shares _duo_state in-place.
                        from core.agentic_tool_loop import AgenticToolLoop
                        from core.tool_loop import ToolLoopConfig
                        _duo_loop = AgenticToolLoop(
                            config=ToolLoopConfig(stream=True, max_post_attempts=20),
                            http_client=_dtc, round_state=_duo_state, emit_fn=_coder_emit_fn,
                        )
                        _post_task = asyncio.create_task(
                            _duo_loop.post_with_retry(_tool_payload, dtool_msgs=_dtool_msgs, ctx=ctx, _parts=_parts)
                        )
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
                            if _post_task.done():
                                break
                            # HEARTBEAT (2026-08-21): during long prompt processing
                            if time.monotonic() - _hb_last >= _hb_interval:
                                _hb_total = int(time.monotonic() - _hb_start)
                                _hb_last = time.monotonic()
                                yield await ctx.emit({"type": "heartbeat", "elapsed": _hb_total})
                            await asyncio.sleep(0.02)
                        _result = _post_task.result()
                        # _duo_state mutated in-place by AgenticToolLoop — no sync needed
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
                        _RE_JSON_FENCE = re.compile(
                            r'```(?:json)?\s*(\{.*?\})\s*```',
                            re.DOTALL,
                        )
                        if _dr_content and "```" in _dr_content:
                            _dr_content = _RE_JSON_FENCE.sub(lambda m: m.group(1), _dr_content)
                            _dr_msg = {**_dr_msg, "content": _dr_content}
                        _dr_tcs = _dr_msg.get("tool_calls", [])
                        # Validate assembled tool calls (SSE stream may have dropped)
                        _validated_tcs = []
                        _drop_notices = []
                        _drop_names = []
                        _salvage_notes = []
                        _drop_len = ""
                        if _result.get("dr_finish_reason") == "length":
                            _drop_len = " — finish_reason=length (output token limit reached)"
                        for _vtc in _dr_tcs:
                            _vname = (_vtc.get("function", {}).get("name", "") or "").strip()
                            _vargs = _vtc.get("function", {}).get("arguments", "")
                            if not _vname:
                                _drop_notices.append(
                                    "[DROPPED: tool call with no name — stream interrupted]"
                                )
                                continue
                            try:
                                json.loads(_vargs) if _vargs else {}
                            except (json.JSONDecodeError, TypeError):
                                from utils.tool import _repair_json_backslashes as _repair_bs
                                try:
                                    _repaired_args = _repair_bs(_vargs)
                                    json.loads(_repaired_args)
                                    _vtc = dict(_vtc)
                                    _vtc["function"] = dict(_vtc.get("function") or {})
                                    _vtc["function"]["arguments"] = _repaired_args
                                except (json.JSONDecodeError, TypeError):
                                    # truncation at the output limit — salvageable prefix
                                    _salv = None
                                    if _vname in ("write_file", "write_file_append"):
                                        try:
                                            from utils.tool import salvage_truncated_write_args as _salvage_args
                                            _salv = _salvage_args(_vargs, _vname)
                                        except Exception:
                                            _salv = None
                                    if _salv:
                                        _vtc = dict(_vtc)
                                        _vtc["function"] = dict(_vtc.get("function") or {})
                                        _vtc["function"]["arguments"] = json.dumps(
                                            _salv["args"], ensure_ascii=False)
                                        _vtc["_salvage"] = _salv
                                        _salv_path = _salv["args"].get("path", "?")
                                        _salvage_notes.append(
                                            f"[WRITE-SALVAGE] {_vname} for '{_salv_path}' was cut off at the "
                                            f"output limit — {_salv['_salvaged_chars']} "
                                            f"chars ({_salv['_salvaged_lines']} lines) were salvaged "
                                            f"and written. Continue with ONLY the missing remainder via "
                                            f"write_file_append: start EXACTLY at char position "
                                            f"{_salv['_salvaged_chars']} (end of line "
                                            f"{_salv['_salvaged_lines']}), without repeating the already "
                                            f"written part. Split into chunks of max ~15000 chars."
                                        )
                                        logger.warning(
                                            "[WRITE-SALVAGE] %s '%s' salvaged: %d chars, %d lines",
                                            _vname, _salv_path,
                                            _salv["_salvaged_chars"], _salv["_salvaged_lines"])
                                    else:
                                        _drop_names.append(_vname)
                                        _drop_notices.append(
                                            f"[DROPPED: tool call '{_vname}' had malformed JSON args "
                                            f"— stream interrupted{_drop_len}]"
                                        )
                                        continue
                            _validated_tcs.append(_vtc)
                        if _drop_notices:
                            _dtool_msgs.append({"role": "user",
                                                 "content": "\n".join(_drop_notices)})
                        _dr_tcs = _validated_tcs
                        if _validated_tcs:
                            _dr_dropped_tool_retries = 0
                        if not _dr_tcs:
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
                                if _dr_think_only_retries > 2:
                                    _ld_setter(3089); _loop_detected = True
                                    yield await ctx.emit({"type": "status",
                                        "content": f"⚠ Coder replies with text only, no tool call ({_dr_think_only_retries}x) — loop aborted."})
                                    break
                                if _dr < _max_tool_rounds - 1:
                                    _dtool_msgs.append({
                                        "role": "user",
                                        "content": "You must call a tool now. Do not explain — use edit_file or run_bash directly.",
                                    })
                                    continue
                            elif not _parts:
                                _empty_err = f"[Tool loop: empty answer from {coder_mdl} — check llama-server log]"
                                _parts.append(_empty_err)
                                yield await ctx.emit({"type": "token", "content": _empty_err})
                            else:
                                _dr_think_only_retries += 1
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
                        # ── RUN-BASH-CHANGE-DETECTION (Problem 2) ─────────────
                        # _contracts_raw exports, normalized against _ws_str.
                        def _loop_detect_file_snapshot():
                            try:
                                from hive_functions.chunking import (
                                    build_file_signature as _bfs,
                                    normalize_resume_candidate_path as _nrcp,
                                )
                                _cands: set = set()
                                for _wf in _written_files or []:
                                    _cands.add(str(_wf))
                                try:
                                    _cands.update(
                                        (_explore_extract_files(_explore_ctx, _pre_explore_msgs) or {}).keys()
                                    )
                                except Exception:
                                    pass
                                for _ct in (_contracts_raw or []):
                                    if not isinstance(_ct, dict):
                                        continue
                                    for _e in (_ct.get("exports") or []):
                                        if isinstance(_e, dict):
                                            _p = str(_e.get("path") or _e.get("file") or "")
                                            if _p:
                                                _cands.add(_p)
                                        elif isinstance(_e, str) and _e.strip():
                                            _cands.add(_e.strip())
                                _out: dict = {}
                                for _cp in _cands:
                                    _np = _nrcp(_cp, Path(_ws_str))
                                    if not _np:
                                        continue
                                    _sig = _bfs(_np)
                                    if _sig:
                                        _out[_np] = _sig
                                return _out
                            except Exception as _ld_err:
                                logger.debug("[LOOP-DETECT] File snapshot failed: %s", _ld_err)
                                return None

                        _round_bash_only = (
                            {tc.get("function", {}).get("name", "") for tc in (_dr_tcs or [])}
                            == {"run_bash"}
                        )
                        _snap_before = _loop_detect_file_snapshot() if _round_bash_only else None
                        # ── Execute all tool calls via shared executor ──
                        _last_too_large_ref = [_last_too_large_path]
                        _cached_port_ref = [_cached_coder_port]
                        _exec_task = asyncio.create_task(execute_tool_round(
                            tool_calls=_dr_tcs,
                            dtool_msgs=_dtool_msgs,
                            round_state=_duo_state,
                            hooks=_tool_exec_hooks,
                            trs=ToolRoundState(
                                tool_ctx_lru=_tool_ctx_lru,
                                duo_deadline_at=_duo_deadline_at,
                                verify_mutation_serial=_verify_mutation_serial,
                                verify_last_ok_serial=_verify_last_ok_serial,
                                last_run_bash_failure=_last_run_bash_failure,
                                changed_since_failure=_changed_since_failure,
                                last_learned_insight_sig=_last_learned_insight_sig,
                                last_too_large_path=_last_too_large_ref,
                                attempts_per_file=_attempts_per_file,
                                tool_error_retries=_tool_error_retries,
                                call_sigs=_call_sigs,
                                recent_focus_paths=_recent_focus_paths,
                                file_changes=_file_changes,
                                duo_seen_web_queries=_duo_seen_web_queries,
                                cached_coder_port=_cached_port_ref,
                                task_complete_blocked_count=_task_complete_blocked,
                                total_tool_errors=_total_tool_errors_ref,
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
                            # HEARTBEAT (2026-08-21): keepalive during long tool rounds.
                            # elapsed = total time since loop start.
                            if time.monotonic() - _hb_last >= _hb_interval:
                                _hb_total = int(time.monotonic() - _hb_start)
                                _hb_last = time.monotonic()
                                yield await ctx.emit({"type": "heartbeat", "elapsed": _hb_total})
                            await asyncio.sleep(0.02)
                        _exec_result = _exec_task.result()
                        _last_too_large_path = _last_too_large_ref[0]
                        _cached_coder_port = _cached_port_ref[0]
                        _verify_mutation_serial = _exec_result.verify_mutation_serial
                        _verify_last_ok_serial = _exec_result.verify_last_ok_serial
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
                        # ── CTX Pressure Check ─────────────────────────────
                        if not _ctx_pressure_warned:
                            # GUARD-REAL-BASIS: fresh real value (usage_meta of the
                            # last round), fallback estimator — no more coarse
                            # sum(len)//3 estimation.
                            _guard_tokens = int(_coder_real_prompt_tokens[0] or _est_tokens)
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
                            # Verify-Gate (Agentic-Mode): ensure run_bash ran after last write
                            if ctx.duo_config.agentic_mode:
                                _needs_verify = (
                                    _verify_mutation_serial > _verify_last_ok_serial
                                    and bool(_written_files)
                                    and not _verify_warned
                                )
                                if _needs_verify and _task_complete_blocked[0] >= 2:
                                    _task_complete_blocked[0] += 1
                                    logger.warning(
                                        "[VERIFY-GATE] task_complete accepted after %d blocks "
                                        "(verification not possible in this environment)",
                                        _task_complete_blocked[0],
                                    )
                                elif _needs_verify:
                                    _exec_result.task_complete_called = False
                                    _task_complete_blocked[0] += 1
                                    _verify_warned = True
                                    # vague "run your tests" — baseline FAIL repo_peon r1
                                    # run_tests → no-suite fallback run_bash → task_complete.
                                    _dtool_msgs.append({"role": "user", "content": (
                                        "[VERIFY REQUIRED] You called task_complete but the last file write "
                                        "has not been followed by a successful test run.\n"
                                        "Do this NOW, in this order:\n"
                                        "1. Call run_tests. If it reports no suite, run the project's "
                                        "documented check via run_bash instead (e.g. python selftest.py).\n"
                                        "2. Ensure it exits with code 0.\n"
                                        "3. Then call task_complete again with status "
                                        "{completed, blockers, build_status}."
                                    )})
                            # Fall-through: re-check after Verify injection
                            if not _exec_result.task_complete_called:
                                pass  # Verify blocked the completion — loop continues
                            else:
                                # TC-LOOP-FIX: an accepted task_complete ends the
                                # tool loop immediately (observed live Run 10: coder called
                                # task_complete 4x+ in a row, chunk path never reached).
                                # Verify that files claimed in task_complete actually exist on disk
                                _missing = []
                                for _fp in sorted(_written_files or []):
                                    _abs = _fp if os.path.isabs(_fp) else os.path.join(_ws_str, _fp)
                                    if not os.path.exists(_abs):
                                        _missing.append(_fp)
                                if _missing:
                                    logger.warning(
                                        "[TASK_COMPLETE VERIFY] %d file(s) claimed but missing: %s",
                                        len(_missing), _missing[:10],
                                    )
                                _grace_round_active = False
                                break
                        _total_tool_rounds += 1
                        _lifetime_tool_rounds += 1
                        # Exploration-loop detection: track rounds with only explore tools
                        _EXPLORE_TOOLS = {"find_files", "search_code", "list_dir", "read_file"}
                        _WRITE_TOOLS = {"write_file", "edit_file", "patch_file", "write_file_append",
                                        "replace_lines", "undo_last", "run_bash", "run_python"}
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
                            logger.warning("[LOOP-DETECT] _explore_only_rounds=%d _total_exports=%d _explore_was_partial=%s any_fallback=%s threshold=%d _explore_ctx_len=%d",
                                           _explore_only_rounds, _total_exports, _explore_was_partial, _any_fallback, _READ_ONLY_THRESHOLD,
                                           len(_explore_ctx) if _explore_ctx else 0)
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
                                _snap_after = _loop_detect_file_snapshot()
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
                        _tool_round_durations.append(round(max(0.0, time.time() - _round_t0), 3))
                        # PATCH-1: Plan tracker — update progress per tool round
                        # extended with deviation detection: hard/soft rules, streak tracking,
                        if _plan_tracker is not None and not _plan_tracker.is_finished:
                            if _last_tool_name and _plan_tracker.should_advance(_last_tool_name, _last_tool_result):
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
                            ctx.exec_ctrl.abort(StopReason.MAX_TOOL_ROUNDS)
                            _ld_setter(3551); _loop_detected = True
                            if _verify_mutation_serial > _verify_last_ok_serial and _file_changes:
                                _uv_files = ", ".join(sorted(_file_changes.keys())[:10])
                                _uv_count = len(_file_changes)
                                if _uv_count > 10:
                                    _uv_files += f" and {_uv_count - 10} more"
                                yield await ctx.emit({
                                    "type": "status",
                                    "content": f"\u26a0\ufe0f Budget exhausted with {_uv_count} unverified file(s): {_uv_files} — changes may be broken.",
                                })
                            yield await ctx.emit({
                                "type": "status",
                                "content": f"\u23f9 Tool budget exhausted ({_total_tool_rounds}/{_max_tool_rounds} rounds) \u2014 stopping.",
                            })
                            if not _grace_round_active:
                                _grace_round_active = True
                                _max_tool_rounds += 1
                                _dtool_msgs.append({"role": "user", "content": (
                                    "[GRACE ROUND] You have 1 final round. "
                                    "Call task_complete now with what was completed, "
                                    "any blockers, and build_status. No other tool calls."
                                )})
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
                            await _lsm_retry.ensure_loaded(exec_mdl, num_ctx=_dtool_opts.get("num_ctx", 4096), n_parallel=1)
                            _dport = _cached_coder_port or await _lsm_retry.ensure_loaded(exec_mdl, num_ctx=_dtool_opts.get("num_ctx", 4096), n_parallel=1)
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
                            _fc = Path(_wp).read_text(encoding="utf-8", errors="replace")
                            if _fc:
                                _file_parts_critic.append(f"# {_wp}\n{_fc[:2000]}")
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
            if _loop_detected:
                final_verdict = final_verdict or "loop_detected"
                # multiple setters stamp finished runs as loop_detected (observed live
                try:
                    _ld_locals = locals()
                    logger.warning(
                        "[LOOP-DETECT-STOP] timed_out=%s think_only=%s compress_streak=%s "
                        "explore_only=%s runtime_error=%s grace=%s verify_warned=%s invalid_tool=%s "
                        "force_compress=%s dropped_tool_retries=%s drop_names=%s last_msgs=%s",
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
                        _ld_locals.get("_drop_names", []),
                        [str(m.get("content", ""))[:80] for m in (_dtool_msgs or [])[-8:]],
                    )
                except Exception:
                    pass
                _halt_with_resume("timeout_guard" if _duo_timed_out else "loop_detected")
                break

            _is_last = (_di == _n_items - 1)

            if _duo_timed_out:
                final_verdict = final_verdict or "timeout_guard"
                _halt_with_resume("timeout_guard")
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
                    _ws_path = str(Path(os.environ.get("HIVEMIND_WORKSPACE", ".")).resolve())
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
                                                   files=list(_cs.written_files[-20:]))
                if _ac_out:
                    yield await ctx.emit({"type": "status", "content": _ac_out})
                _di += 1
                _cs.reset_test_retries()
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
                if _subtask:
                    _cs.mark_chunk_done(_subtask)
                    _ac_out = await _auto_commit_chunk(ctx.user_input, _subtask, _ws_str,
                                                       ctx.duo_config.git_autocommit,
                                                       files=list(_cs.written_files[-20:]))
                    if _ac_out:
                        yield await ctx.emit({"type": "status", "content": _ac_out})
                if _is_last:
                    break
                _di += 1
                _cs.reset_test_retries()
                continue
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
                            _parts.append(_cmsg.get("content", "").strip())
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
                if _critic_profile.get("min_p", 0.0) != 0.0:
                    _critic_plain_payload["min_p"] = _critic_profile["min_p"]
                if _critic_profile.get("cache_prompt"):
                    _critic_plain_payload["cache_prompt"] = True
                _critic_plain_payload = _apply_thinking_kwargs(
                    _critic_plain_payload, _critic_profile, _critic_thinking, coder_tool_think=False
                )
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
            if _critic_verdict.get("approved") is None:
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

            _can_finish_round = (_approved or _is_last) and not _verify_gate_blocked
            if _can_finish_round:
                if _subtask:
                    _cs.mark_chunk_done(_subtask)
                    critic_issues = _cs.critic_issues  # cleared by mark_chunk_done
                    _rem = _n_items - _di - 1
                    yield await ctx.emit({"type":"status",
                        "content":f"\u2713 {_di+1}/{_n_items}: {_subtask[:50]}" + (f" ({_rem} more)" if _rem else " (done)")})
                    # B8.3 AUTO-COMMIT: chunk completion (critic approve). Fires
                    _ac_out = await _auto_commit_chunk(ctx.user_input, _subtask, _ws_str,
                                                       ctx.duo_config.git_autocommit,
                                                       files=list(_cs.written_files[-20:]))
                    if _ac_out:
                        yield await ctx.emit({"type": "status", "content": _ac_out})
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
                        if _is_last:
                            break
                    elif _duo_test_feedback and _cs.has_written_files and not ctx.aborted():
                        _ws_path = str(Path(os.environ.get("HIVEMIND_WORKSPACE", ".")).resolve())
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
                            _di += 1
                            _cs.reset_test_retries()
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
                                if _is_last:
                                    break
                    else:
                        # No auto-test or no files written — normal completion
                        _di += 1
                        _cs.reset_test_retries()
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

        if _duo_timed_out:
            if _written_files:
                yield await ctx.emit({
                    "type": "token",
                    "content": (
                        f"\n[Run-Timeout-Guard: intermediate state output — {len(_written_files)} file(s) on disk. "
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
            _verify_mutation_serial > _verify_last_ok_serial
            and not ctx.aborted()
            and not _verify_warned
        ):
            _duo_hard_stop = True
            _verify_issue_final = "Verification missing: run_bash must pass after latest file changes."
            if _verify_issue_final not in critic_issues:
                critic_issues.append(_verify_issue_final)
            if not final_verdict:
                final_verdict = "verification_required_after_write"
            yield await ctx.emit({
                "type": "status",
                "content": "⛔ Run not marked completed: verification after file changes is missing.",
            })
            _vg_start = _di if _di < _n_items else _n_items - 1
            _rem_vg = [{"title": str(t)} for t in _loop_items[_vg_start:]]
            _rem_vg_titles = {r["title"] for r in _rem_vg}
            _done_vg = [t for t in _done_tasks if str(t) not in _rem_vg_titles]
            if ctx.chat_id and _rem_vg:
                _write_resume_block(
                    chat_id=ctx.chat_id,
                    workspace=_ws_str,
                    chunks_total=_n_items,
                    chunks_done=_done_vg,
                    chunks_remaining=_rem_vg,
                    written_files=_written_files,
                    last_summary=" | ".join(_done_vg),
                    plan_msgs=[],
                    explore_ctx=_explore_ctx,
                    halt_reason="verification_required",
                )
        elif _verify_mutation_serial > _verify_last_ok_serial and not ctx.aborted():
            logger.warning(
                "[VERIFY-GATE-FINAL] no hard stop (coder warned): written_files=%d _verify_warned=True (A-P0-2 fallback)",
                len(_written_files or []),
            )

    except (GeneratorExit, asyncio.CancelledError):
        _p_loop_items = _p_di = None
        _p_done_tasks = _p_written = None
        _p_explore_ctx = ""
        _p_ws_str = ""
        _p_n_items = 0
        try: _p_loop_items = _loop_items
        except (NameError, UnboundLocalError): pass
        try: _p_di = _di
        except (NameError, UnboundLocalError): pass
        try: _p_done_tasks = _done_tasks
        except (NameError, UnboundLocalError): pass
        try: _p_written = _written_files
        except (NameError, UnboundLocalError): pass
        try: _p_explore_ctx = _explore_ctx
        except (NameError, UnboundLocalError): pass
        try: _p_ws_str = _ws_str
        except (NameError, UnboundLocalError): pass
        try: _p_n_items = _n_items
        except (NameError, UnboundLocalError): pass
        try:
            _park_on_disconnect(
                ctx=ctx,
                loop_items=_p_loop_items,
                di=_p_di,
                done_tasks=_p_done_tasks,
                written_files=_p_written,
                explore_ctx=_p_explore_ctx,
                ws_str=_p_ws_str,
                n_items=_p_n_items,
            )
        except Exception as _park_err:
            logger.warning("[DISCONNECT-PARK] Handler error: %s", _park_err)
        raise

    finally:
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
            async def _unpin_duo(_pinned=_duo_pinned, _ka=_restore_ka):
                async with httpx.AsyncClient(timeout=5.0) as _uc:
                    for _um in _pinned:
                        try:
                            _unpin_ctx = ctx.get_num_ctx(_um)
                            _unpin_payload: dict = {"model": _um, "keep_alive": _ka, "stream": False}
                            # rnj-1:8b default=32768 → ~2GB extra KV-Cache → VRAM-Spike auf 8GB GPU
                            if _unpin_ctx:
                                _unpin_payload["options"] = {"num_ctx": _unpin_ctx, "num_predict": 0}
                            await _bk_load(_um, keep_alive=_restore_ka, num_ctx=_unpin_ctx)
                        except Exception as _err:
                            if isinstance(_err, (
                                GeneratorExit,
                                asyncio.CancelledError,
                                KeyboardInterrupt,
                                SystemExit,
                            )):
                                raise
                            logger.debug(
                                "[DUO] Suppressed error at unpin keep-alive: %s",
                                _err, exc_info=True
                            )
            asyncio.create_task(_unpin_duo())

        if _project_state is not None and ctx.chat_id:
            try:
                from context.project_state import ProjectStateManager
                if not _project_state_run_counted:
                    _project_state.total_runs += 1
                    _project_state_run_counted = True
                _project_state.last_run_timestamp = datetime.now().isoformat()
                if getattr(_project_state, 'last_run_success', None) is None:
                    _project_state.last_run_success = False
                ProjectStateManager().save(_project_state)
                logger.info("[PROJECT] State gesichert in finally (Run #%d, success=%s)",
                            _project_state.total_runs, _project_state.last_run_success)
            except Exception as _save_err:
                logger.warning("[PROJECT] Save in finally failed: %s", _save_err)

    # ── P3: Final Test after all chunks ──────────────────────────
    _final_test_fb = bool(ctx.duo_config.test_feedback_final)
    if _final_test_fb and _cs.has_written_files and not ctx.aborted():
        yield await ctx.emit({"type": "status", "content": "\U0001f9ea Final test run after all chunks..."})
        try:
            _final_test_result = await _run_test_suite(
                workspace=str(Path(os.environ.get("HIVEMIND_WORKSPACE", ".")).resolve()),
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

    if _loop_detected:
        _written_list = sorted(_written_files or [])
        _auto_summary = (
            f"[AUTO-STOP] Run terminated (loop_detected).\n"
            f"Files written ({len(_written_list)}): "
            + (", ".join(_written_list[:12]) + ("..." if len(_written_list) > 12 else "")
               if _written_list else "none")
            + f"\nTool rounds used: {_total_tool_rounds}/{_max_tool_rounds}."
        )
        _drop_diag_names = []
        _drop_diag_retries = "n/a"
        try:
            _dd_locals = locals()
            _drop_diag_names = list(_dd_locals.get("_drop_names") or [])[-8:]
            _drop_diag_retries = _dd_locals.get("_dr_dropped_tool_retries", "n/a")
        except Exception:
            pass
        logger.warning(
            "[AUTO-STOP] loop_detected Diagnose: tool_errors=%d explore_only_rounds=%d "
            "last_bash_failure=%s written_files=%d dropped_tool_retries=%s drop_names=%s",
            (_total_tool_errors_ref[0] if _total_tool_errors_ref else -1),
            _explore_only_rounds,
            _last_run_bash_failure,
            len(_written_list),
            _drop_diag_retries,
            _drop_diag_names,
        )
        yield await ctx.emit({"type": "token", "content": "\n\n" + _auto_summary})
        try:
            _auto_parts = _parts
        except (NameError, UnboundLocalError):
            _auto_parts = []
        _auto_parts.append(_auto_summary)

    ctx.memory.add_to_session("user", ctx.user_input)
    # Persist files_read + touched_paths from this run into .context.json
    # so follow-up runs within the same session retain read-guard state.
    if ctx.chat_id and not ctx.is_aborted_chat(ctx.chat_id):
        try:
            from tools.runner import _files_read_in_run
            _read_final = _files_read_in_run.get(None)
            _last_plan = ((_plan_result.plan_content if _plan_result else "") or _inloop_plan_text or "")[:8000]
            if _duo_timed_out:
                _eff_stop = "timeout_guard"
            elif _loop_detected:
                _eff_stop = "loop_detected"
            elif ctx.aborted():
                _eff_stop = "aborted"
            else:
                _eff_stop = "completed"
            _save_chat_context(ctx.chat_id, {
                **(_load_chat_context(ctx.chat_id) or {}),
                "files_read_in_run": list(_read_final) if _read_final else [],
                "touched_paths": list(_touched_paths) if _touched_paths else [],
                "task_sig": hashlib.md5(ctx.user_input[:60].encode()).hexdigest()[:8],
                # P1-2 (2026-08-12): Session pro Chat persistieren (Restart-sicher).
                # Injektion via compute_char_caps/budget_session_msgs.
                "session": ctx.memory.get_session_messages(limit=6, user_cap=8000, assistant_cap=8000),
                "last_run": {
                    "task": ctx.user_input[:8000],
                    "stop_reason": _eff_stop,
                    "written_files": sorted(set(_written_files or []))[:50],
                    "plan": _last_plan,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "success": bool(_eff_stop == "completed"),
                },
            })
        except Exception as _ctx_err:
            logger.debug("[CTX] Chat context save failed: %s", _ctx_err)

    # D4-DIAG (2026-08-21): Re-Read-Inflation messen — wiederholtes read_file
    try:
        _parts_joined = "".join(_parts) if isinstance(_parts, list) else ""
        _rf_calls = len(re.findall(r"read_file\s*\(", _parts_joined))
        _rf_paths = set(re.findall(r"read_file\([^)]*path\s*[:=]\s*['\"]([^'\"]+)['\"]", _parts_joined))
        _rf_bytes = 0
        for _rfm in re.finditer(
            r"\n?\[[^\]]*(?:total lines|lines \d+-\d+)[^\]]*\]\n(.*?)(?=\n\[[^\]]*(?:total lines|lines \d+-\d+)[^\]]*\]|$)",
            _parts_joined, re.S,
        ):
            _rf_bytes += len(_rfm.group(1))
        logger.warning(
            "[READ-STATS] read_file_calls=%d unique_paths=%d re_read_ratio=%.1f approx_bytes=%d est_ctx=%d",
            _rf_calls, len(_rf_paths),
            ((_rf_calls / len(_rf_paths)) if _rf_paths else 0.0),
            _rf_bytes, int(_est_tokens or 0),
        )
    except Exception as _rs_err:
        logger.debug("[READ-STATS] Evaluation failed: %s", _rs_err)

    # direct_runner.py:117 / pipeline_runner.py:775.
    try:
        _duo_run_count = ctx.increment_run_counter()
    except Exception as _rc_err:
        _duo_run_count = 0
        logger.warning("[RUN-COUNTER] Duo increment failed: %s", _rc_err)

    # soul_evolve_agent.enabled + Run-Intervall (server.py).
    if _duo_run_count:
        try:
            asyncio.create_task(ctx.maybe_trigger_soul_evolution(_duo_run_count))
        except Exception as _se_err:
            logger.warning("[SOUL] Evolution trigger failed: %s", _se_err)


    # KRIT-3 FIX: _duo_session_out as the single session-write point.
    # Old: ctx.memory.add_to_session("assistant", coder_out) in the if-not-duo branch +
    #      ctx.memory.add_to_session("assistant", _combined_session) in the synth branch
    # CHUNKING FIX: Synthesis gets the combined output of all subtasks.
    # ChunkState.combine_outputs() handles the merging + completion notes.
    _combined_coder_out = _cs.combine_outputs(fallback_coder_out=coder_out)
    _duo_session_out = _combined_coder_out

    _ratings_agentic = bool(ctx.settings.get("duo_peer_ratings_agentic", False))
    _rating_source = _combined_coder_out or coder_out
    if _rating_source and not ctx.aborted() and not _duo_hard_stop and (not ctx.duo_config.agentic_mode or _ratings_agentic):
        _duo_rating_out = {
            "duo_coder":  _rating_source,
            "duo_critic": final_verdict,
            "duo_coder_model": coder_mdl,
        }
        asyncio.create_task(run_peer_ratings(
            ctx.run_id, ctx.user_input, _duo_rating_out, ctx.use_learned,
            rating_mode="duo",   # Duo-spezifische Rating-Paare
            has_images=bool(ctx.images),
        ))

    # ── File-Change Summary ──
    if _cs.has_written_files:
        _fc_summary = ", ".join(list(_written_files)[:8]) if _written_files else ""
        _fc_more = f" +{len(_written_files)-8} more" if len(_written_files or []) > 8 else ""
        yield await ctx.emit({"type": "status",
            "content": f"📁 {len(_written_files or [])} file(s) changed: {_fc_summary}{_fc_more}"})

    if ctx.duo_config.use_pipeline and not ctx.duo_config.agentic_mode and _combined_coder_out and not ctx.aborted() and not _duo_hard_stop:
        yield await ctx.emit({"type": "status", "content": "⚙ Duo → Pipeline: synthesizer analysis..."})
        yield await ctx.emit({"type": "pipeline_start", "content": ctx.user_input})
        _duo_pipeline_input = (
            f"Task: {ctx.user_input}\n\n"
            f"The following code was generated by the Duo mode:\n\n{_combined_coder_out}\n\n"
            f"Review the code and list OPEN POINTS as concrete developer tasks."
        )
        _pipe_synth = ctx.pipeline.agents.get("synthesizer")
        if _pipe_synth:
            _pipe_sys = (
                ctx.get_effective_prompt_with_override("duo_synthesizer", ctx.active_preset, ctx.use_learned)
                or ctx.get_effective_prompt_with_override("synthesizer", ctx.active_preset, ctx.use_learned)
            )
            _pipe_msgs = ctx.make_messages(ctx.pipeline, _pipe_sys, _duo_pipeline_input, [], False, True,
                                        cached_mem_ctx=ctx.pipeline_mem_ctx)
            yield await ctx.emit({"type": "agent", "content": "Synthesis",
                              "model": ctx.registry_get("synthesizer"),
                              "role": "Final quality assessment"})
            _pparts, _pt = [], time.time()
            try:
                async for _tok in ctx.pipeline_chat_stream(_pipe_synth.model, _pipe_msgs,
                                                        _pipe_synth.temperature, _pipe_synth.max_tokens):
                    if ctx.aborted(): break
                    _pparts.append(_tok)
                    yield await ctx.emit({"type": "token", "content": _tok})
            except Exception as _pe:
                yield await ctx.emit({"type": "token", "content": f"[Pipeline-Error: {str(_pe)[:80]}]"})
            _pipe_out = "".join(_pparts).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled
            yield await ctx.emit({"type": "agent_done", "elapsed": round(time.time() - _pt, 1)})
            _duo_session_out = (
                f"[Code generated for: {ctx.user_input[:80]}{'...' if len(ctx.user_input) > 80 else ''}]\n\n"
                f"{_pipe_out}"
            )

    ctx.memory.add_to_session("assistant", _duo_session_out)  # single write point
    if ctx.aborted() or (ctx.chat_id and ctx.is_aborted_chat(ctx.chat_id)):
        ctx.duo_stop_reason = "aborted"
    elif _graceful_stopped:
        ctx.duo_stop_reason = "graceful_stop"
    elif _duo_timed_out:
        ctx.duo_stop_reason = "timeout"
    elif _duo_hard_stop:
        ctx.duo_stop_reason = "hard_stop"
    elif _loop_detected:
        ctx.duo_stop_reason = "loop_detected"
    elif ctx.exec_ctrl.state == AgentState.HALTED:
        ctx.duo_stop_reason = (
            ctx.exec_ctrl.stop_reason.value if ctx.exec_ctrl.stop_reason else "halted"
        )
    else:
        ctx.duo_stop_reason = "completed"
    # ── B8.3 FINAL AUTO-COMMIT (Run-Abschluss) ─────────────────────────────
    if (ctx.duo_config.git_autocommit
            and ctx.duo_stop_reason in ("completed", "graceful_stop")
            and _ws_str):
        _ac_final = await _auto_commit_chunk(ctx.user_input, "", _ws_str, True)
        if _ac_final:
            yield await ctx.emit({"type": "status", "content": _ac_final})
    elif (_git_checkpoints_enabled(ctx)
          and not ctx.duo_config.git_autocommit
          and ctx.duo_stop_reason in ("completed", "graceful_stop")
          and _ws_str):
        try:
            from hive_functions.git_tools import exec_git_squash_checkpoints as _sq
            try:
                from settings import load_settings as _ls
                _pfx = (str((_ls() or {}).get("git_commit_prefix", "") or "").strip()
                        or "hivemind:")
            except Exception:
                _pfx = "hivemind:"
            _sq_out = await _sq(f"{_pfx} checkpoint: session consolidated",
                                _ws_str, consolidate_only=True)
            if _sq_out:
                yield await ctx.emit({"type": "status", "content": _sq_out})
        except Exception as _sq_err:
            logger.debug("[GIT-CONSOLIDATE] skipped: %s", _sq_err)
    # AUDIT-FIX D2: resume safety net — on every non-complete stop
    # (abort/timeout/hard_stop/loop) write a resume block, so the
    # next start can continue. Only if none exists yet.
    if (ctx.chat_id
            and ctx.duo_stop_reason not in ("completed", "graceful_stop")):
        try:
            _di_safe = locals().get("_di", 0)
            _n_items_safe = locals().get("_n_items", 1)
            _loop_items_safe = locals().get("_loop_items") or [{"title": ctx.user_input}]
            if _di_safe < _n_items_safe:
                if not _load_resume_block(ctx.chat_id):
                    _rem_d2 = [{"title": str(t)} for t in _loop_items_safe[_di_safe:]]
                    if _rem_d2:
                        _write_resume_block(
                            chat_id=ctx.chat_id,
                            workspace=_ws_str,
                            chunks_total=_n_items_safe,
                            chunks_done=_done_tasks,
                            chunks_remaining=_rem_d2,
                            written_files=_written_files,
                            last_summary=" | ".join(_done_tasks),
                            plan_msgs=[],
                            explore_ctx=_explore_ctx,
                            halt_reason=ctx.duo_stop_reason,
                        )
        except Exception as _d2_err:
            if isinstance(_d2_err, (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            logger.warning("[RESUME-SAFETY-NET] Write failed: %s", _d2_err)
    if ctx.chat_id and ctx.duo_stop_reason == "completed":
        ctx.clear_resume_block(ctx.chat_id)
    ctx.phase_timer.end("coder_loop", status=ctx.duo_stop_reason)
    logger.info("Phase-Timing: %s", ctx.phase_timer.ui_summary())

    # ── ProjectState: persist run result ──
    if _project_state is not None and ctx.chat_id:
        try:
            from context.project_state import ProjectStateManager
            _project_state.last_run_timestamp = datetime.now().isoformat()
            _project_state.last_run_success = (ctx.duo_stop_reason == "completed")
            if not _project_state_run_counted:
                _project_state.total_runs += 1
                _project_state_run_counted = True
            ProjectStateManager().save(_project_state)
            logger.info("[PROJECT] State saved after run #%d (%s)",
                        _project_state.total_runs, ctx.duo_stop_reason)
        except Exception as _ps_err:
            logger.warning("[PROJECT] Normal save failed: %s", _ps_err)

    try:
        from core.run_audit import record_run_audit
        record_run_audit(ctx.chat_id, {
            "run_id": ctx.run_id,
            "event": "run_end",
            "stop_reason": str(ctx.duo_stop_reason or ""),
            "written_files": sorted(_written_files or []),
            "loop_detected": bool(_loop_detected),
            "explore_only_rounds": int(_explore_only_rounds),
            "total_tool_rounds": int(_total_tool_rounds),
        })
    except Exception:
        pass

    yield await ctx.emit(ctx.done_event(round(time.time() - ctx.t_total, 1), ctx.duo_stop_reason, **ctx.collect_done_metrics()))
    ctx.unregister_abort(ctx.run_id)
    ctx.unregister_step_skip(ctx.run_id)  # P7 FIX: was missing — step_skip event leaked after Duo runs
    clear_graceful_stop(ctx.run_id)
    _cleanup_pause_state(ctx.run_id)
    if ctx.chat_id:
        _clear_pause_state(ctx.chat_id)
    _cleanup_governor(ctx.chat_id or ctx.run_id)
    _current_project_state.set(None)
    # AUDIT-FIX 2026-08-03 (preload_workers_after_run, strenges Opt-in, Default False):
    if bool(ctx.settings.get("preload_workers_after_run", False)):
        _wk_slots_run = _worker_slots or []
        if _wk_slots_run:
            async def _warm_workers_after_run(_slots=list(_wk_slots_run), _coder_mdl=exec_mdl):
                try:
                    from backend.llama_server_manager import manager as _lsm_aw
                    try:
                        await _lsm_aw.evict(_coder_mdl)
                    except Exception:
                        pass
                    for _wsk in _slots:
                        try:
                            await _lsm_aw.ensure_loaded(
                                str(_wsk.get("model", "")),
                                num_ctx=int(_wsk.get("num_ctx") or _wsk.get("ctx") or 16384),
                                n_parallel=int(_wsk.get("n_parallel", 1) or 1),
                            )
                        except Exception as _err:
                            if isinstance(_err, (
                                GeneratorExit,
                                asyncio.CancelledError,
                                KeyboardInterrupt,
                                SystemExit,
                            )):
                                raise
                            logger.debug(
                                "[DUO] Suppressed error at after-run worker warmup: %s",
                                _err, exc_info=True
                            )
                except Exception:
                    pass
            asyncio.create_task(_warm_workers_after_run())
    asyncio.create_task(_refresh_judge_keepalive())
    # ── Insight Extractor: Post-loop structured learning ──────────────────
    # Fire-and-forget background task — extracts structured insights from the
    # completed agentic loop using the INSIGHT_EXTRACTOR prompt.
    if _written_files and ctx.duo_stop_reason == "completed" and not ctx.aborted():
        asyncio.create_task(ctx.run_insight_extractor(
            task=ctx.user_input,
            written_files=list(_written_files),
            critic_verdict=final_verdict,
            critic_issues=list(critic_issues),
            workspace=_ws_str,
        ))
    # ── Skill Distillation: Run after every completed Duo loop ─────────────
    # Previously only ran during soul evolution (too infrequent).
    # Now runs after each Duo completion so insights get decayed/merged promptly.
    if ctx.pipeline.memory and ctx.duo_stop_reason == "completed":
        asyncio.create_task(asyncio.to_thread(
            ctx.run_soul_cycle, ctx.pipeline.memory, ctx.settings, list(_recent_focus_paths), ctx.this_file
        ))
    # ── Skill-Destillation (Phase 2): wiederkehrende Muster → echte Skills ──
    # Gated durch soul_skill_writing (umgewidmet zu "Auto-Destillation"). Max 1
    if (ctx.pipeline.memory and ctx.duo_stop_reason == "completed"
            and not ctx.aborted() and ctx.settings.get("soul_skill_writing", False)):
        asyncio.create_task(ctx.run_skill_distillation(_ws_str))
    # Vision-Slot warmhalten
    _vis_mdl = ctx.vision_cfg.get("model", "") if ctx.vision_cfg.get("enabled") else ""
    if _vis_mdl:
        async def _keepalive_vision():
            try:
                from backend.llama_server_manager import manager as _kvm
                await _kvm.touch_if_loaded(_vis_mdl)
            except Exception:
                pass
        asyncio.create_task(_keepalive_vision())
